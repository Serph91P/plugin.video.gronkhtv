"""Bounded, cancellable account-resume read for the Kodi start path."""

import threading
import time


class ResumeStartController:
    """Read one remote resume value without permitting a late seek.

    The controller has no Kodi dependency.  The caller waits only before it has
    started playback; after a timeout or abort the immutable fallback result is
    retained even when the daemon request eventually returns.
    """

    def __init__(self, progress_sync, video_id, clock=time.monotonic):
        self._progress_sync = progress_sync
        self._video_id = video_id
        self._clock = clock
        self._done = threading.Event()
        self._lock = threading.Lock()
        self._result = None
        self._cancelled = False

    def resolve(self, local_offset, timeout_seconds=3, explicit_offset=None, aborted=None):
        if explicit_offset is not None:
            return {"state": "explicit", "offset": explicit_offset}
        if self._progress_sync is None or not self._video_id:
            return {"state": "disabled", "offset": local_offset}
        # An account read cannot fall back to a private resume, even on timeout.
        local_offset = 0
        if aborted is not None and aborted():
            return {"state": "aborted", "offset": local_offset}
        thread = threading.Thread(target=self._read, daemon=True)
        thread.start()
        deadline = self._clock() + timeout_seconds
        while not self._done.is_set():
            if aborted is not None and aborted():
                return self._fallback("aborted", local_offset)
            remaining = deadline - self._clock()
            if remaining <= 0:
                return self._fallback("timeout", local_offset)
            self._done.wait(min(remaining, 0.05))
        with self._lock:
            result = self._result
        if result is None or result.get("state") != "valid":
            return self._fallback(
                result.get("state", "unavailable") if result else "unavailable",
                local_offset,
            )
        return result

    def result(self):
        with self._lock:
            return self._result

    def _fallback(self, state, local_offset):
        with self._lock:
            self._cancelled = True
            self._result = {"state": state, "offset": local_offset}
            return dict(self._result)

    def _read(self):
        try:
            result = self._progress_sync.prepare_playback(self._video_id)
        except Exception:
            result = {"state": "unavailable", "offset": None}
        with self._lock:
            if not self._cancelled:
                self._result = result
        self._done.set()
