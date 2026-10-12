import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "resources" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from account.resume_start import ResumeStartController


class Sync:
    def __init__(self, result=None, wait=None):
        self.result = result or {"state": "absent", "offset": None}
        self.wait = wait
        self.calls = []

    def prepare_playback(self, video_id):
        self.calls.append(video_id)
        if self.wait is not None:
            self.wait.wait()
        return self.result


def test_normal_start_uses_remote_account_offset_before_deadline():
    sync = Sync({"state": "valid", "offset": 42})

    controller = ResumeStartController(sync, "video-a")

    assert controller.resolve(local_offset=12, timeout_seconds=0.2) == {
        "state": "valid",
        "offset": 42,
    }
    assert sync.calls == ["video-a"]


def test_explicit_start_or_chapter_never_waits_for_or_uses_remote_resume():
    sync = Sync({"state": "valid", "offset": 42})
    controller = ResumeStartController(sync, "video-a")

    assert controller.resolve(local_offset=12, timeout_seconds=0.2, explicit_offset=0) == {
        "state": "explicit",
        "offset": 0,
    }
    assert sync.calls == []


def test_timeout_returns_local_offset_and_late_response_cannot_replace_it():
    release = threading.Event()
    sync = Sync({"state": "valid", "offset": 42}, wait=release)
    controller = ResumeStartController(sync, "video-a")

    assert controller.resolve(local_offset=12, timeout_seconds=0.01) == {
        "state": "timeout",
        "offset": 12,
    }
    release.set()
    time.sleep(0.02)

    assert controller.result() == {"state": "timeout", "offset": 12}


def test_abort_returns_local_offset_without_waiting_for_read():
    sync = Sync({"state": "valid", "offset": 42})
    controller = ResumeStartController(sync, "video-a")

    assert controller.resolve(local_offset=12, timeout_seconds=0.2, aborted=lambda: True) == {
        "state": "aborted",
        "offset": 12,
    }
    assert sync.calls == []


def test_malformed_or_failed_read_keeps_local_offset():
    sync = Sync({"state": "unavailable", "offset": None})
    controller = ResumeStartController(sync, "video-a")

    assert controller.resolve(local_offset=12, timeout_seconds=0.2) == {
        "state": "unavailable",
        "offset": 12,
    }


def test_conflict_reaches_the_foreground_without_a_silent_resume_choice():
    controller = ResumeStartController(
        Sync({"state": "conflict", "offset": 12, "remote_offset": 42}), "video-a"
    )

    assert controller.resolve(local_offset=12, timeout_seconds=0.2) == {
        "state": "conflict",
        "offset": 12,
        "remote_offset": 42,
    }
