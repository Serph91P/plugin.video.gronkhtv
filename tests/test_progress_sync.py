import sys
from pathlib import Path

import pytest

LIB = Path(__file__).resolve().parents[1] / "resources" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from account.progress import RemoteProgress
from account.progress_sync import ProgressSync
from account.session import AuthenticationError, RateLimitError, ValidationError
from account.sync_store import SyncStore


class API:
    def __init__(self, remote=None):
        self.remote = remote or RemoteProgress(status="absent")
        self.reads = []
        self.writes = []
        self.error = None

    def read(self, video_id):
        self.reads.append(video_id)
        return self.remote

    def write(self, video_id, offset):
        self.writes.append((video_id, offset))
        if self.error:
            raise self.error


def setup_sync(tmp_path, api=None, is_active=lambda: True):
    store = SyncStore(tmp_path / "sync.sqlite3")
    api = api or API()
    return ProgressSync(store, api, "account-a", is_active=is_active), store, api


def test_resume_uses_only_get_even_when_failed_upload_is_pending(tmp_path):
    sync, store, api = setup_sync(tmp_path, API(RemoteProgress("valid", 99, marked_as_watched=True)))
    store.enqueue("account-a", "video-a", 2.125)
    before = store.pending("account-a", "video-a")
    assert sync.prepare_playback("video-a") == {
        "state": "valid", "offset": 99, "marked_as_watched": True,
    }
    assert store.pending("account-a", "video-a") == before
    assert api.writes == []


@pytest.mark.parametrize("offset", [0, 2.125, 12, 500])
@pytest.mark.parametrize("event", ["pause", "stop", "end"])
def test_playback_events_post_immediately_without_get_or_durable_progress(tmp_path, offset, event):
    sync, store, api = setup_sync(tmp_path)
    assert sync.record_position("video-a", offset, 500, event=event) == "synchronized"
    assert api.writes == [("video-a", offset)]
    assert api.reads == []
    assert store.pending("account-a", "video-a") is None


@pytest.mark.parametrize("event", ["tick", "seek"])
def test_sampling_does_not_write_or_enqueue(tmp_path, event):
    sync, store, api = setup_sync(tmp_path)
    sync.record_position("video-a", 12, 500, event=event)
    assert api.writes == []
    assert store.pending_uploads("account-a") == []


@pytest.mark.parametrize("error,state", [
    (RuntimeError("offline"), "pending"),
    (AuthenticationError("expired"), "authentication_required"),
    (RateLimitError("busy"), "rate_limited"),
    (ValidationError("rejected"), "invalid"),
])
def test_only_failed_posts_are_durable_and_retry_without_preflight_get(tmp_path, error, state):
    sync, store, api = setup_sync(tmp_path)
    api.error = error
    assert sync.record_position("video-a", 12.125, 500, event="stop") == state
    assert SyncStore(store.path).pending("account-a", "video-a")["offset"] == 12.125
    api.error = None
    assert sync.flush("video-a") == "synchronized"
    assert api.reads == []
    assert api.writes == [("video-a", 12.125), ("video-a", 12.125)]
    assert store.pending("account-a", "video-a") is None


def test_latest_actual_position_replaces_failed_upload_including_reverse_and_zero(tmp_path):
    sync, store, api = setup_sync(tmp_path)
    api.error = RuntimeError("offline")
    for offset in (100, 2.125, 0):
        sync.record_position("video-a", offset, 500, event="stop")
    assert store.pending("account-a", "video-a")["offset"] == 0
    api.error = None
    sync.record_position("video-a", 9, 500, event="pause")
    assert store.pending("account-a", "video-a") is None


def test_retry_is_account_isolated_and_newer_queue_item_survives_late_success(tmp_path):
    sync, store, api = setup_sync(tmp_path)
    store.enqueue("account-a", "video-a", 12)
    store.enqueue("account-b", "video-a", 88)
    api.write = lambda *args: store.enqueue("account-a", "video-a", 2)
    assert sync.flush("video-a") == "pending"
    assert store.pending("account-a", "video-a")["offset"] == 2
    assert store.pending("account-b", "video-a")["offset"] == 88


def test_disabled_or_replaced_session_never_reads_writes_or_acknowledges(tmp_path):
    sync, store, api = setup_sync(tmp_path, is_active=lambda: False)
    store.enqueue("account-a", "video-a", 12)
    assert sync.prepare_playback("video-a")["state"] == "authentication_required"
    sync.record_position("video-a", 20, 500, event="stop")
    assert sync.flush("video-a") == "authentication_required"
    assert api.reads == api.writes == []
    assert store.pending("account-a", "video-a")["offset"] == 20


def test_account_change_during_get_does_not_publish_old_account_data(tmp_path):
    active = [True]
    sync, _, api = setup_sync(tmp_path, is_active=lambda: active[0])
    def read(video_id):
        active[0] = False
        return RemoteProgress("valid", 42)
    api.read = read
    assert sync.prepare_playback("video-a") == {"state": "authentication_required", "offset": None}


@pytest.mark.parametrize("remote,state", [(RemoteProgress("absent"), "absent"), (RemoteProgress("broken"), "unavailable")])
def test_absent_is_distinct_from_failed_or_malformed_reads(tmp_path, remote, state):
    sync, store, api = setup_sync(tmp_path, API(remote))
    store.enqueue("account-a", "video-a", 12)
    assert sync.prepare_playback("video-a") == {"state": state, "offset": None}
    api.read = lambda video_id: (_ for _ in ()).throw(RuntimeError("offline"))
    assert sync.prepare_playback("video-a") == {"state": "unavailable", "offset": None}
    assert store.pending("account-a", "video-a")["offset"] == 12


def test_retry_all_sends_only_this_accounts_failed_uploads(tmp_path):
    sync, store, api = setup_sync(tmp_path)
    store.enqueue("account-a", "video-a", 0)
    store.enqueue("account-a", "video-b", 42)
    store.enqueue("account-b", "video-c", 88)
    sync.flush_pending()
    assert api.writes == [("video-a", 0), ("video-b", 42)]
    assert store.pending_uploads("account-a") == []
    assert store.pending("account-b", "video-c")["offset"] == 88
