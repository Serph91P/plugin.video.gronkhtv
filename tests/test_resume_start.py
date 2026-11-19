import sys
import threading
import time
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parents[1] / "resources" / "lib"
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
    assert controller.resolve(local_offset=12, timeout_seconds=0.2) == {"state": "valid", "offset": 42}
    assert sync.calls == ["video-a"]


@pytest.mark.parametrize("offset", [0, 75])
def test_explicit_start_or_chapter_never_waits_for_or_uses_remote_resume(offset):
    sync = Sync({"state": "valid", "offset": 42})
    controller = ResumeStartController(sync, "video-a")
    assert controller.resolve(local_offset=12, timeout_seconds=0.2, explicit_offset=offset) == {"state": "explicit", "offset": offset}
    assert sync.calls == []


def test_timeout_returns_no_private_resume_and_late_response_cannot_replace_it():
    release = threading.Event()
    controller = ResumeStartController(Sync({"state": "valid", "offset": 42}, wait=release), "video-a")
    assert controller.resolve(local_offset=12, timeout_seconds=0.01) == {"state": "timeout", "offset": 0}
    release.set()
    time.sleep(0.02)
    assert controller.result() == {"state": "timeout", "offset": 0}


def test_abort_returns_no_private_resume_without_waiting_for_read():
    sync = Sync({"state": "valid", "offset": 42})
    controller = ResumeStartController(sync, "video-a")
    assert controller.resolve(local_offset=12, timeout_seconds=0.2, aborted=lambda: True) == {"state": "aborted", "offset": 0}
    assert sync.calls == []


@pytest.mark.parametrize("state", ["absent", "unavailable", "authentication_required"])
def test_absent_failed_or_malformed_read_cannot_restore_private_offset(state):
    controller = ResumeStartController(Sync({"state": state, "offset": None}), "video-a")
    assert controller.resolve(local_offset=12, timeout_seconds=0.2) == {"state": state, "offset": 0}


def test_guest_or_optout_keeps_existing_local_behavior():
    assert ResumeStartController(None, None).resolve(local_offset=12) == {"state": "disabled", "offset": 12}
