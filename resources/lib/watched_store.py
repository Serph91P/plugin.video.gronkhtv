import json


def read_marker(vfs, path):
    readable_path = path if vfs.exists(path) else f"{path}.bak"
    if not vfs.exists(readable_path):
        return False
    try:
        with vfs.File(readable_path) as marker_file:
            return json.loads(marker_file.read()) is True
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        return False


def write_marker(vfs, directory, path):
    if not vfs.exists(directory) and not vfs.mkdirs(directory):
        raise OSError(f"Could not create watched directory: {directory}")

    temporary_path = f"{path}.tmp"
    backup_path = f"{path}.bak"
    if vfs.exists(temporary_path):
        vfs.delete(temporary_path)
    moved_previous = False
    try:
        with vfs.File(temporary_path, "w") as marker_file:
            if marker_file.write(json.dumps(True)) is False:
                raise OSError(f"Could not write temporary watched marker: {temporary_path}")
        if vfs.exists(path):
            if vfs.exists(backup_path) and not vfs.delete(backup_path):
                raise OSError(f"Could not remove stale watched backup: {backup_path}")
            if not vfs.rename(path, backup_path):
                raise OSError(
                    f"Could not atomically replace watched marker: {path} (staging failed)"
                )
            moved_previous = True
        if not vfs.rename(temporary_path, path):
            raise OSError(f"Could not atomically replace watched marker: {path}")
        if vfs.exists(backup_path):
            vfs.delete(backup_path)
    except Exception:
        if moved_previous and not vfs.exists(path):
            vfs.rename(backup_path, path)
        if vfs.exists(temporary_path):
            vfs.delete(temporary_path)
        raise


def remove_marker(vfs, path):
    backup_path = f"{path}.bak"
    backup_removed = not vfs.exists(backup_path) or vfs.delete(backup_path)
    if not backup_removed:
        return False
    return not vfs.exists(path) or vfs.delete(path)
