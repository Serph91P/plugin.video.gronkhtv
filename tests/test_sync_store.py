import multiprocessing
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "resources" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from account.sync_store import ContextLostError, SyncStore


def _claim(db_path, account_id, output):
    store = SyncStore(db_path)
    output.put(store.claim_upload(account_id, lease_seconds=30))


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
