"""Server-only reads and direct progress POSTs with a failed-upload queue."""

import threading

from account.progress import _offset
from account.operation_lock import progress_operation
from account.session import AuthenticationError, RateLimitError, ValidationError


class ProgressSync:
    def __init__(self, store, api, account_id, is_active=lambda: True):
        self._store = store
        self._api = api
        self._account_id = account_id
        self._is_active = is_active
        self._write_lock = threading.Lock()

    def prepare_playback(self, video_id):
        if not self._is_active():
            return {"state": "authentication_required", "offset": None}
        try:
            remote = self._api.read(video_id)
        except AuthenticationError:
            return {"state": "authentication_required", "offset": None}
        except Exception:
            return {"state": "unavailable", "offset": None}
        if not self._is_active():
            return {"state": "authentication_required", "offset": None}
        if remote.status == "absent":
            return {"state": "absent", "offset": None}
        if remote.status != "valid":
            return {"state": "unavailable", "offset": None}
        return {"state": "valid", "offset": remote.offset,
                "marked_as_watched": remote.marked_as_watched}

    def record_position(self, video_id, offset, duration=0, event="tick"):
        if event not in {"pause", "stop", "end"}:
            return "ignored"
        offset = _offset(offset)
        return self._write_or_queue(video_id, offset)

    def mark_watched(self, video_id, duration):
        """Explicit user command: set account progress to the known video end.

        This is never used by playback sampling or automatic completion.
        """
        duration = _offset(duration)
        if duration <= 0:
            raise ValueError("A positive video duration is required")
        return self._write_or_queue(video_id, duration)

    def _write_or_queue(self, video_id, offset):
        # No detached uploader: stop waits for its bounded POST before Kodi
        # tears the interpreter down. The queue contains failures only.
        with self._write_lock, progress_operation(self._store.path) as acquired:
            if not acquired:
                self._store.enqueue(self._account_id, video_id, offset)
                return "pending"
            previous = self._store.pending(self._account_id, video_id)
            state = self._post(video_id, offset)
            if state != "synchronized":
                self._store.enqueue(self._account_id, video_id, offset)
                return state
            return "synchronized" if self._store.remove(previous) else "pending"

    def flush(self, video_id):
        with self._write_lock, progress_operation(self._store.path) as acquired:
            if not acquired:
                return "pending"
            record = self._store.pending(self._account_id, video_id)
            if record is None:
                return "synchronized"
            state = self._post(video_id, record["offset"])
            if state != "synchronized":
                return state
            return "synchronized" if self._store.remove(record) else "pending"

    def flush_pending(self, exclude_video_id=None):
        # Bound end-of-playback retries; later successful stops retry the rest.
        for record in self._store.pending_uploads(self._account_id)[:3]:
            if record["video_id"] == exclude_video_id:
                continue
            state = self.flush(record["video_id"])
            if state != "synchronized":
                break

    def _post(self, video_id, offset):
        if not self._is_active():
            return "authentication_required"
        try:
            self._api.write(video_id, offset)
        except AuthenticationError:
            return "authentication_required"
        except ValidationError:
            return "invalid"
        except RateLimitError:
            return "rate_limited"
        except Exception:
            return "pending"
        # A logout/switch while the request was in flight must not acknowledge
        # records from a session which is no longer active.
        return "synchronized" if self._is_active() else "authentication_required"
