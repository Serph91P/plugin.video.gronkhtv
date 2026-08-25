import json
import math


def read_record(vfs, path, legacy_duration=0):
    readable_path = path if vfs.exists(path) else f"{path}.bak"
    if not vfs.exists(readable_path):
        return None
    try:
        with vfs.File(readable_path) as resume_file:
            content = resume_file.read()
        value = json.loads(content)
        if isinstance(value, dict):
            return _validated_record(value.get("position"), value.get("duration"))
        return _validated_record(value, legacy_duration)
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return None


def write_record(vfs, directory, path, position, duration):
    record = _validated_record(position, duration)
    if record is None:
        raise ValueError("Resume position and duration must be finite non-negative numbers")
    if not vfs.exists(directory) and not vfs.mkdirs(directory):
        raise OSError(f"Could not create resume directory: {directory}")

    temporary_path = f"{path}.tmp"
    backup_path = f"{path}.bak"
    if vfs.exists(temporary_path):
        vfs.delete(temporary_path)
    moved_previous = False
    try:
        content = json.dumps(record, sort_keys=True, separators=(",", ":"))
        with vfs.File(temporary_path, "w") as resume_file:
            if resume_file.write(content) is False:
                raise OSError(f"Could not write temporary resume record: {temporary_path}")
        if vfs.exists(path):
            if vfs.exists(backup_path) and not vfs.delete(backup_path):
                raise OSError(f"Could not remove stale resume backup: {backup_path}")
            if not vfs.rename(path, backup_path):
                raise OSError(
                    f"Could not atomically replace resume record: {path} (staging failed)"
                )
            moved_previous = True
        if not vfs.rename(temporary_path, path):
            raise OSError(f"Could not atomically replace resume record: {path}")
        if vfs.exists(backup_path):
            vfs.delete(backup_path)
    except Exception:
        if moved_previous and not vfs.exists(path):
            vfs.rename(backup_path, path)
        if vfs.exists(temporary_path):
            vfs.delete(temporary_path)
        raise


def remove_record(vfs, path):
    backup_path = f"{path}.bak"
    backup_removed = not vfs.exists(backup_path) or vfs.delete(backup_path)
    if not backup_removed:
        return False
    return not vfs.exists(path) or vfs.delete(path)


def is_completed(record):
    validated = _validated_record(record.get("position"), record.get("duration"))
    return bool(
        validated
        and validated["duration"] > 0
        and validated["position"] / validated["duration"] >= 0.95
    )


def _validated_record(position, duration):
    if isinstance(position, bool) or isinstance(duration, bool):
        return None
    try:
        position = float(position)
        duration = float(duration)
    except (TypeError, ValueError):
        return None
    if (
        not math.isfinite(position)
        or position < 0
        or not math.isfinite(duration)
        or duration < 0
    ):
        return None
    return {"position": position, "duration": duration}
