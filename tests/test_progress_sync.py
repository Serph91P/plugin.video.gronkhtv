import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "resources" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from account.progress import RemoteProgress
from account.progress_sync import ProgressSync
from account.session import AuthenticationError, SessionError
from account.sync_store import SyncStore


class API:
    def __init__(self, read_result=None):
        self.read_result = read_result or RemoteProgress(status="absent")
        self.writes = []
        self.reads = []

    def read(self, video_id):
        self.reads.append(video_id)
        return self.read_result

    def write(self, video_id, offset):
        self.writes.append((video_id, offset))


def _sync(tmp_path, api=None):
    store = SyncStore(tmp_path / "sync.sqlite3")
    epoch = store.activate("account-a", enabled=True)
    return ProgressSync(store, api or API(), "account-a", epoch), store


def test_recording_a_real_reverse_seek_and_zero_keeps_latest_pending_snapshot(tmp_path):
    sync, store = _sync(tmp_path)

    sync.record_position("video-a", 100, 500, event="tick")
    sync.record_position("video-a", 0, 500, event="seek")

    assert store.pending("account-a", "video-a")["offset"] == 0


def test_flush_confirms_only_the_same_account_epoch_and_latest_revision(tmp_path):
    api = API()
    sync, store = _sync(tmp_path, api)
    sync.record_position("video-a", 12, 500, event="pause")

    assert sync.flush("video-a") == "synchronized"
    assert api.reads == ["video-a"]
    assert api.writes == [("video-a", 12)]
    assert store.pending("account-a", "video-a") is None


def test_remote_change_after_confirmed_base_requires_conflict_choice(tmp_path):
    api = API(RemoteProgress(status="absent"))
    sync, store = _sync(tmp_path, api)
    sync.record_position("video-a", 12, 500, event="pause")
    assert sync.flush("video-a") == "synchronized"
    sync.record_position("video-a", 20, 500, event="pause")
    api.read_result = RemoteProgress(status="valid", offset=99, updated_at="later")

    assert sync.flush("video-a") == "conflict"
    assert api.writes == [("video-a", 12)]
    assert store.pending("account-a", "video-a")["offset"] == 20


def test_prepare_playback_surfaces_a_remote_conflict_for_a_foreground_choice(tmp_path):
    api = API(RemoteProgress(status="absent"))
    sync, store = _sync(tmp_path, api)
    sync.record_position("video-a", 12, 500, event="pause")
    assert sync.flush("video-a") == "synchronized"
    sync.record_position("video-a", 20, 500, event="pause")
    api.read_result = RemoteProgress(status="valid", offset=99, updated_at="later")

    assert sync.prepare_playback("video-a") == {
        "state": "conflict",
        "offset": 20,
        "remote_offset": 99,
    }
    assert store.pending("account-a", "video-a")["offset"] == 20


def test_choosing_kodi_progress_rebases_the_pending_snapshot_before_upload(tmp_path):
    api = API(RemoteProgress(status="absent"))
    sync, store = _sync(tmp_path, api)
    sync.record_position("video-a", 12, 500, event="pause")
    assert sync.flush("video-a") == "synchronized"
    sync.record_position("video-a", 20, 500, event="pause")
    api.read_result = RemoteProgress(status="valid", offset=99, updated_at="later")

    assert sync.prepare_playback("video-a")["state"] == "conflict"
    assert sync.resolve_conflict("video-a", "kodi") == 20
    assert sync.flush("video-a") == "synchronized"
    assert api.writes == [("video-a", 12), ("video-a", 20)]
    assert store.pending("account-a", "video-a") is None


def test_keep_kodi_conflict_upload_confirms_integer_wire_offset_for_integer_echo(tmp_path):
    class Session:
        def __init__(self):
            self.calls = []
            self.responses = [
                {"data": {"watch_progress": {"offset": 100, "updated_at": "before"}}},
                {"data": {}},
                {"data": {"watch_progress": {"offset": 90, "updated_at": "external"}}},
                {"data": {"watch_progress": {"offset": 90, "updated_at": "external"}}},
                {"data": {}},
                {"data": {"watch_progress": {"offset": 120, "updated_at": "echo"}}},
                {"data": {}},
            ]

        def request_json(self, path, method="GET", payload=None, timeout=None):
            self.calls.append((path, method, payload, timeout))
            return self.responses.pop(0)

    from account.progress import ProgressClient

    video_id = "b0b3d510-1da2-4bcb-9d6c-9d13d23b38b1"
    session = Session()
    sync, store = _sync(tmp_path, ProgressClient(session))
    sync.record_position(video_id, 120.058, 500, event="pause")
    assert sync.flush(video_id) == "synchronized"

    sync.record_position(video_id, 120.5, 500, event="pause")
    assert sync.prepare_playback(video_id)["state"] == "conflict"
    assert sync.resolve_conflict(video_id, "kodi") == 120.5
    assert sync.flush(video_id) == "synchronized"

    sync.record_position(video_id, 120.9, 500, event="pause")
    assert sync.flush(video_id) == "synchronized"
    assert store.pending("account-a", video_id) is None
    assert [call[2] for call in session.calls if call[1] == "POST"] == [
        {"offset": 120},
        {"offset": 120},
        {"offset": 120},
    ]


def test_failed_preflight_read_keeps_pending_and_does_not_write(tmp_path):
    api = API()
    sync, store = _sync(tmp_path, api)
    sync.record_position("video-a", 12, 500, event="pause")
    api.read = lambda video_id: (_ for _ in ()).throw(RuntimeError("offline"))

    assert sync.flush("video-a") == "pending"
    assert api.writes == []
    assert store.pending("account-a", "video-a")["offset"] == 12


def test_auth_failure_never_clears_pending_or_invalidates_another_account(tmp_path):
    api = API()
    sync, store = _sync(tmp_path, api)
    sync.record_position("video-a", 12, 500, event="pause")
    api.write = lambda video_id, offset: (_ for _ in ()).throw(AuthenticationError("expired"))

    assert sync.flush("video-a") == "authentication_required"
    assert store.pending("account-a", "video-a")["offset"] == 12
    store.activate("account-b", enabled=True)
    assert store.context()["active_account_id"] == "account-b"


def test_account_switch_after_lease_fences_the_write_before_http(tmp_path):
    class FenceStore(SyncStore):
        def claim_upload(self, account_id, lease_seconds, video_id=None, revision=None):
            lease = super().claim_upload(account_id, lease_seconds, video_id, revision)
            self.activate("account-b", enabled=True)
            return lease

    store = FenceStore(tmp_path / "sync.sqlite3")
    epoch = store.activate("account-a", enabled=True)
    api = API()
    sync = ProgressSync(store, api, "account-a", epoch)
    sync.record_position("video-a", 12, 500, event="pause")

    assert sync.flush("video-a") == "pending"
    assert api.writes == []
    assert store.pending("account-a", "video-a")["offset"] == 12


def test_stale_context_is_fenced_before_preflight_read_and_keeps_pending(tmp_path):
    api = API()
    sync, store = _sync(tmp_path, api)
    sync.record_position("video-a", 12, 500, event="pause")
    store.activate("account-b", enabled=True)

    assert sync.flush("video-a") == "pending"
    assert api.reads == []
    assert api.writes == []
    assert store.pending("account-a", "video-a")["offset"] == 12


def test_stale_context_is_fenced_before_preflight_read_after_logout(tmp_path):
    api = API()
    sync, store = _sync(tmp_path, api)
    sync.record_position("video-a", 12, 500, event="pause")
    store.deactivate()

    assert sync.flush("video-a") == "pending"
    assert api.reads == []
    assert api.writes == []
    assert store.pending("account-a", "video-a")["offset"] == 12


@pytest.mark.parametrize(
    "invalidate",
    [
        lambda store: store.activate("account-b", enabled=True),
        lambda store: store.deactivate(),
        lambda store: store.activate("account-a", enabled=True),
    ],
    ids=["switch", "logout", "same-account-new-epoch"],
)
def test_context_lost_while_persisting_an_absent_read_keeps_pending_without_raising(tmp_path, invalidate):
    class PersistFenceStore(SyncStore):
        def observe_remote(self, account_id, epoch, video_id, remote):
            invalidate(self)
            return super().observe_remote(account_id, epoch, video_id, remote)

    store = PersistFenceStore(tmp_path / "sync.sqlite3")
    epoch = store.activate("account-a", enabled=True)
    api = API(RemoteProgress(status="absent"))
    sync = ProgressSync(store, api, "account-a", epoch)
    sync.record_position("video-a", 12, 500, event="pause")

    assert sync.flush("video-a") == "pending"
    assert api.reads == ["video-a"]
    assert api.writes == []
    assert store.pending("account-a", "video-a")["offset"] == 12


@pytest.mark.parametrize(
    "remote",
    [
        RemoteProgress(status="absent"),
        RemoteProgress(status="valid", offset=42, updated_at="2026-10-11T00:00:00Z"),
    ],
    ids=["absent", "valid"],
)
@pytest.mark.parametrize(
    "invalidate",
    [
        lambda store: store.activate("account-b", enabled=True),
        lambda store: store.deactivate(),
        lambda store: store.activate("account-a", enabled=True),
    ],
    ids=["switch", "logout", "relogin"],
)
def test_relogin_before_remote_observation_never_persists_a_stale_response(tmp_path, remote, invalidate):
    class PersistFenceStore(SyncStore):
        def observe_remote(self, account_id, epoch, video_id, observed):
            if getattr(self, "_fence_on_observe", False):
                invalidate(self)
            return super().observe_remote(account_id, epoch, video_id, observed)

    store = PersistFenceStore(tmp_path / "sync.sqlite3")
    epoch = store.activate("account-a", enabled=True)
    baseline = {"status": "valid", "offset": 7, "updated_at": "before"}
    store.observe_remote("account-a", epoch, "video-a", baseline)
    api = API(remote)
    sync = ProgressSync(store, api, "account-a", epoch)
    sync.record_position("video-a", 12, 500, event="pause")
    pending_before = store.pending("account-a", "video-a")
    store._fence_on_observe = True

    assert sync.flush("video-a") == "pending"
    assert api.reads == ["video-a"]
    assert api.writes == []
    assert store.pending("account-a", "video-a") == pending_before
    assert store.observed_remote("account-a", "video-a") == baseline


def test_prepare_remote_absent_is_not_confused_with_malformed_or_failed_read(tmp_path):
    sync, _ = _sync(tmp_path, API(RemoteProgress(status="absent")))
    assert sync.prepare_playback("video-a") == {"state": "absent", "offset": None}

    failing, _ = _sync(tmp_path / "failing", API())
    failing._api.read = lambda video_id: (_ for _ in ()).throw(RuntimeError("network"))
    assert failing.prepare_playback("video-a") == {"state": "unavailable", "offset": None}


def test_prepare_persists_a_successfully_observed_remote_value_separately(tmp_path):
    remote = RemoteProgress(status="valid", offset=42, updated_at="2026-10-11T00:00:00Z")
    sync, store = _sync(tmp_path, API(remote))

    assert sync.prepare_playback("video-a") == {"state": "valid", "offset": 42}

    observed = store.observed_remote("account-a", "video-a")
    assert observed == {
        "status": "valid",
        "offset": 42,
        "updated_at": "2026-10-11T00:00:00Z",
        "marked_as_watched": None,
    }


def test_failed_read_does_not_replace_a_previously_observed_remote_value(tmp_path):
    remote = RemoteProgress(status="valid", offset=42, updated_at="2026-10-11T00:00:00Z")
    sync, store = _sync(tmp_path, API(remote))
    sync.prepare_playback("video-a")
    sync._api.read = lambda video_id: (_ for _ in ()).throw(RuntimeError("offline"))

    assert sync.prepare_playback("video-a") == {"state": "unavailable", "offset": None}
    assert store.observed_remote("account-a", "video-a")["offset"] == 42


def test_explicit_reset_replaces_pending_position_and_confirms_zero(tmp_path):
    api = API()
    sync, store = _sync(tmp_path, api)
    sync.record_position("video-a", 2, 500, event="stop")

    assert sync.reset("video-a") == "synchronized"
    assert api.writes == [("video-a", 0)]
    assert store.pending("account-a", "video-a") is None
    assert store.observed_remote("account-a", "video-a") == {
        "status": "valid",
        "offset": 0,
    }


def test_explicit_reset_defers_zero_while_an_existing_upload_lease_is_active(tmp_path):
    api = API()
    sync, store = _sync(tmp_path, api)
    sync.record_position("video-a", 2, 500, event="stop")
    assert store.claim_upload("account-a", lease_seconds=30) is not None

    assert sync.reset("video-a") == "pending"
    assert api.writes == []
    assert store.pending("account-a", "video-a")["offset"] == 0


def test_reset_fences_a_flush_snapshot_captured_before_its_preflight(tmp_path):
    class ResetDuringReadAPI(API):
        def read(self, video_id):
            assert sync.reset(video_id) == "synchronized"
            return RemoteProgress(status="absent")

    api = ResetDuringReadAPI()
    sync, store = _sync(tmp_path, api)
    sync.record_position("video-a", 2, 300, event="stop")

    assert sync.flush("video-a") == "pending"
    assert api.writes == [("video-a", 0)]
    assert store.pending("account-a", "video-a") is None


def test_stale_reset_epoch_cannot_replace_a_new_same_account_intent(tmp_path):
    api = API()
    old_sync, store = _sync(tmp_path, api)
    new_epoch = store.activate("account-a", enabled=True)
    new_sync = ProgressSync(store, api, "account-a", new_epoch)
    new_sync.record_position("video-a", 77, 300, event="stop")

    assert old_sync.reset("video-a") == "pending"
    assert api.writes == []
    assert store.pending("account-a", "video-a")["offset"] == 77


def test_reset_discards_a_positive_response_from_a_read_started_before_zero(tmp_path):
    store = SyncStore(tmp_path / "sync.sqlite3")
    epoch = store.activate("account-a", enabled=True)

    class ResetDuringReadAPI(API):
        offset = 2

        def read(self, video_id):
            captured = self.offset
            assert sync.reset(video_id) == "synchronized"
            return RemoteProgress(status="valid", offset=captured)

        def write(self, video_id, offset):
            self.offset = offset
            self.writes.append((video_id, offset))

    api = ResetDuringReadAPI()
    sync = ProgressSync(store, api, "account-a", epoch)
    sync.record_position("video-a", 2, 300, event="stop")

    assert sync.prepare_playback("video-a") != {"state": "valid", "offset": 2}
    assert api.writes == [("video-a", 0)]
    assert store.observed_remote("account-a", "video-a")["offset"] == 0


@pytest.mark.parametrize("confirmed", [False, True])
def test_reset_fences_a_positive_read_even_without_a_pending_snapshot(tmp_path, confirmed):
    store = SyncStore(tmp_path / "sync.sqlite3")
    epoch = store.activate("account-a", enabled=True)

    class ResetDuringReadAPI(API):
        offset = 2

        def read(self, video_id):
            captured = self.offset
            assert sync.reset(video_id) == "synchronized"
            return RemoteProgress(status="valid", offset=captured)

        def write(self, video_id, offset):
            self.offset = offset
            self.writes.append((video_id, offset))

    api = ResetDuringReadAPI()
    sync = ProgressSync(store, api, "account-a", epoch)
    if confirmed:
        assert sync.reset("video-a") == "synchronized"
        api.offset = 2
        api.writes.clear()

    assert store.pending("account-a", "video-a") is None
    assert sync.prepare_playback("video-a") != {"state": "valid", "offset": 2}
    assert api.writes == [("video-a", 0)]
    assert store.observed_remote("account-a", "video-a")["offset"] == 0


def test_reset_fences_a_retry_after_its_lease_expires(tmp_path):
    now = [0.0]
    store = SyncStore(tmp_path / "sync.sqlite3", clock=lambda: now[0])
    epoch = store.activate("account-a", enabled=True)

    class ResetDuringRetryAPI(API):
        interrupted = False

        def write(self, video_id, offset):
            if offset == 2 and not self.interrupted:
                self.interrupted = True
                now[0] = 6.0
                assert sync.reset(video_id) == "synchronized"
                raise SessionError("synthetic transient failure")
            self.writes.append((video_id, offset))

    api = ResetDuringRetryAPI()
    sync = ProgressSync(store, api, "account-a", epoch)
    sync.record_position("video-a", 2, 300, event="stop")

    assert sync.flush("video-a") == "pending"
    assert api.writes == [("video-a", 0)]
