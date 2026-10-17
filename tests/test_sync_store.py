import multiprocessing
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "resources" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from account.sync_store import ContextLostError, SyncStore


def _claim(db_path, account_id, output):
    store = SyncStore(db_path)
    output.put(store.claim_upload(account_id, lease_seconds=30))


def _progress_state(store, account_id, video_id):
    with store._connection() as db:
        row = db.execute(
            "SELECT pending, confirmed_json, observed_json FROM progress WHERE account_id=? AND video_id=?",
            (account_id, video_id),
        ).fetchone()
    return tuple(row)


def test_account_epoch_and_pending_records_are_isolated_and_zero_is_a_real_value(tmp_path):
    store = SyncStore(tmp_path / "progress.sqlite3")
    epoch_a = store.activate("account-a", enabled=True)
    record_a = store.record_local("account-a", "video-a", 0, 120)
    epoch_b = store.activate("account-b", enabled=True)

    assert epoch_b > epoch_a
    assert record_a["offset"] == 0
    assert store.pending("account-a", "video-a")["offset"] == 0
    assert store.pending("account-b", "video-a") is None


def test_stale_ack_cannot_clear_newer_revision_or_another_epoch(tmp_path):
    store = SyncStore(tmp_path / "progress.sqlite3")
    epoch_a = store.activate("account-a", enabled=True)
    first = store.record_local("account-a", "video-a", 10, 100)
    lease = store.claim_upload("account-a", lease_seconds=30)
    second = store.record_local("account-a", "video-a", 2, 100)

    assert not store.confirm_upload("account-a", epoch_a, lease, "video-a", first["revision"])
    assert store.pending("account-a", "video-a")["revision"] == second["revision"]

    store.activate("account-b", enabled=True)
    assert not store.confirm_upload("account-a", epoch_a, lease, "video-a", second["revision"])
    assert store.pending("account-a", "video-a")["revision"] == second["revision"]


def test_two_processes_receive_monotonic_fencing_tokens(tmp_path):
    db_path = str(tmp_path / "progress.sqlite3")
    seed = SyncStore(db_path)
    seed.activate("account-a", enabled=True)
    context = multiprocessing.get_context("spawn")
    output = context.Queue()
    first = context.Process(target=_claim, args=(db_path, "account-a", output))
    second = context.Process(target=_claim, args=(db_path, "account-a", output))
    first.start()
    second.start()
    first.join(10)
    second.join(10)

    values = [output.get(timeout=2), output.get(timeout=2)]
    assert first.exitcode == 0
    assert second.exitcode == 0
    assert len([value for value in values if value is not None]) == 1
    assert next(value for value in values if value is not None)["fence"] >= 1


def test_logout_or_switch_invalidates_old_upload_lease_without_deleting_pending(tmp_path):
    store = SyncStore(tmp_path / "progress.sqlite3")
    epoch_a = store.activate("account-a", enabled=True)
    item = store.record_local("account-a", "video-a", 30, 100)
    lease = store.claim_upload("account-a", lease_seconds=30)

    store.deactivate()
    epoch_b = store.activate("account-b", enabled=True)

    assert epoch_b > epoch_a
    assert not store.confirm_upload("account-a", epoch_a, lease, "video-a", item["revision"])
    assert store.pending("account-a", "video-a")["offset"] == 30
    assert store.pending("account-b", "video-a") is None


def test_observe_remote_rejects_a_same_account_stale_epoch_without_mutation(tmp_path):
    store = SyncStore(tmp_path / "progress.sqlite3")
    old_epoch = store.activate("account-a", enabled=True)
    store.record_local("account-a", "video-a", 12, 100)
    baseline = {"status": "valid", "offset": 7, "updated_at": "before"}
    store.observe_remote("account-a", old_epoch, "video-a", baseline)
    pending_before = store.pending("account-a", "video-a")
    new_epoch = store.activate("account-a", enabled=True)

    try:
        store.observe_remote("account-a", old_epoch, "video-a", {"status": "absent"})
        raise AssertionError("stale epoch unexpectedly persisted a remote observation")
    except ContextLostError:
        pass

    assert new_epoch > old_epoch
    assert store.observed_remote("account-a", "video-a") == baseline
    assert store.pending("account-a", "video-a") == pending_before


def test_reconcile_restores_a_missing_context_without_churning_a_stable_epoch(tmp_path):
    store = SyncStore(tmp_path / "progress.sqlite3")
    missing_epoch = store.deactivate()

    restored_epoch = store.reconcile("account-a", expected_epoch=missing_epoch)
    repeated_epoch = store.reconcile("account-a", expected_epoch=restored_epoch)

    assert restored_epoch > missing_epoch
    assert repeated_epoch == restored_epoch
    assert store.context() == {
        "active_account_id": "account-a",
        "epoch": restored_epoch,
        "sync_enabled": 1,
    }


def test_reconcile_cannot_reactivate_after_a_concurrent_logout(tmp_path):
    store = SyncStore(tmp_path / "progress.sqlite3")
    expected_epoch = store.activate("account-a", enabled=True)
    logout_epoch = store.deactivate()

    assert store.reconcile("account-a", expected_epoch=expected_epoch) is None
    assert store.context() == {
        "active_account_id": None,
        "epoch": logout_epoch,
        "sync_enabled": 0,
    }


def test_stale_successful_ack_advances_its_unchanged_observation_without_clearing_pending(tmp_path):
    store = SyncStore(tmp_path / "progress.sqlite3")
    epoch = store.activate("account-a", enabled=True)
    first = store.record_local("account-a", "video-a", 120.058, 500)
    observed_before_write = {"status": "valid", "offset": 0}
    store.observe_remote("account-a", epoch, "video-a", observed_before_write)
    lease = store.claim_upload("account-a", lease_seconds=30)
    second = store.record_local("account-a", "video-a", 130.125, 500)

    assert not store.confirm_upload(
        "account-a", epoch, lease, "video-a", first["revision"],
        confirmed={"status": "valid", "offset": 120},
        observed_before_write=observed_before_write,
    )
    assert store.pending("account-a", "video-a")["revision"] == second["revision"]
    assert store.observed_remote("account-a", "video-a") == {"status": "valid", "offset": 120}
    assert store.display_status() == "pending"


def test_successful_current_ack_leaves_a_following_local_record_pending_without_conflict(tmp_path):
    store = SyncStore(tmp_path / "progress.sqlite3")
    epoch = store.activate("account-a", enabled=True)
    item = store.record_local("account-a", "video-a", 120.058, 500)
    observed_before_write = {"status": "valid", "offset": 0}
    store.observe_remote("account-a", epoch, "video-a", observed_before_write)
    lease = store.claim_upload("account-a", lease_seconds=30)

    assert store.confirm_upload(
        "account-a", epoch, lease, "video-a", item["revision"],
        confirmed={"status": "valid", "offset": 120},
        observed_before_write=observed_before_write,
    )
    store.record_local("account-a", "video-a", 130.125, 500)

    assert store.display_status() == "pending"


@pytest.mark.parametrize("add_newer_local", [False, True], ids=["current-ack", "stale-ack"])
def test_concurrent_observation_is_not_replaced_by_a_successful_ack(tmp_path, add_newer_local):
    store = SyncStore(tmp_path / "progress.sqlite3")
    epoch = store.activate("account-a", enabled=True)
    first = store.record_local("account-a", "video-a", 120.058, 500)
    observed_before_write = {"status": "valid", "offset": 0}
    store.observe_remote("account-a", epoch, "video-a", observed_before_write)
    lease = store.claim_upload("account-a", lease_seconds=30)
    if add_newer_local:
        store.record_local("account-a", "video-a", 130.125, 500)
    concurrent_remote = {"status": "valid", "offset": 77.5}
    store.observe_remote("account-a", epoch, "video-a", concurrent_remote)

    assert store.confirm_upload(
        "account-a", epoch, lease, "video-a", first["revision"],
        confirmed={"status": "valid", "offset": 120},
        observed_before_write=observed_before_write,
    ) is not add_newer_local
    assert store.observed_remote("account-a", "video-a") == concurrent_remote
    store.record_local("account-a", "video-a", 140.125, 500)
    assert store.display_status() == "conflict"


def test_expired_or_replaced_lease_cannot_confirm_or_replace_observation(tmp_path):
    now = [100]
    store = SyncStore(tmp_path / "progress.sqlite3", clock=lambda: now[0])
    epoch = store.activate("account-a", enabled=True)
    item = store.record_local("account-a", "video-a", 120.058, 500)
    observed = {"status": "valid", "offset": 0}
    store.observe_remote("account-a", epoch, "video-a", observed)
    expired_lease = store.claim_upload("account-a", lease_seconds=1)
    now[0] += 2
    before_expired_confirmation = _progress_state(store, "account-a", "video-a")

    assert not store.confirm_upload(
        "account-a", epoch, expired_lease, "video-a", item["revision"],
        confirmed={"status": "valid", "offset": 120},
        observed_before_write=observed,
    )
    assert _progress_state(store, "account-a", "video-a") == before_expired_confirmation

    replacement_lease = store.claim_upload("account-a", lease_seconds=30)

    assert not store.confirm_upload(
        "account-a", epoch, expired_lease, "video-a", item["revision"],
        confirmed={"status": "valid", "offset": 120},
        observed_before_write=observed,
    )
    assert _progress_state(store, "account-a", "video-a") == before_expired_confirmation
    assert replacement_lease is not None


def test_active_playback_writer_defers_reset_without_replacing_its_pending_sample(tmp_path):
    """A second plugin invoker must observe the durable selected-writer lease."""
    store = SyncStore(tmp_path / "progress.sqlite3")
    epoch = store.activate("account-a", enabled=True)
    original = store.record_local("account-a", "video-a", 2, 300)

    writer = store.claim_playback_writer("account-a", epoch, "video-a", lease_seconds=5)

    assert writer is not None
    assert store.record_reset_if_idle("account-a", epoch, "video-a") is None
    assert store.pending("account-a", "video-a")["revision"] == original["revision"]
    assert store.pending("account-a", "video-a")["offset"] == 2
