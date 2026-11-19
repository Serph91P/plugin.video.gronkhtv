import sqlite3
import sys
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

LIB = Path(__file__).resolve().parents[1] / "resources" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from account.sync_store import SyncStore


def test_queue_is_durable_account_isolated_and_accepts_zero(tmp_path):
    path = tmp_path / "sync.sqlite3"
    store = SyncStore(path)
    store.enqueue("account-a", "video-a", 0)
    store.enqueue("account-b", "video-a", 99)
    reopened = SyncStore(path)
    assert reopened.pending("account-a", "video-a")["offset"] == 0
    assert reopened.pending("account-b", "video-a")["offset"] == 99
    assert reopened.pending("account-a", "other") is None
    assert reopened.display_status("account-a") == "pending"
    assert reopened.display_status("account-c") == "synchronized"


def test_late_ack_cannot_remove_newer_failure_even_for_same_offset(tmp_path):
    store = SyncStore(tmp_path / "sync.sqlite3")
    first = store.enqueue("account-a", "video-a", 12)
    second = store.enqueue("account-a", "video-a", 12)
    assert not store.remove(first)
    assert store.pending("account-a", "video-a") == second
    assert store.remove(second)
    assert store.pending("account-a", "video-a") is None


@pytest.mark.parametrize("value", [True, -1, float("nan"), float("inf"), "2", None])
def test_invalid_offset_does_not_create_an_upload(tmp_path, value):
    store = SyncStore(tmp_path / "sync.sqlite3")
    with pytest.raises(ValueError):
        store.enqueue("account-a", "video-a", value)
    assert store.pending_uploads("account-a") == []


def test_old_local_progress_is_neither_imported_nor_deleted(tmp_path):
    path = tmp_path / "sync.sqlite3"
    with sqlite3.connect(path) as db:
        # Previous v2.3.9 model; never turn this private state into new uploads.
        db.execute("CREATE TABLE progress(account_id TEXT, video_id TEXT, offset REAL, pending INTEGER)")
        db.execute("INSERT INTO progress VALUES ('account-a', 'video-a', 999, 1)")
        db.execute("CREATE TABLE control(epoch INTEGER, active_account_id TEXT)")
        db.execute("INSERT INTO control VALUES (12, 'account-a')")
    for _ in range(2):
        store = SyncStore(path)
        assert store.pending_uploads("account-a") == []
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT offset FROM progress").fetchone()[0] == 999
        assert db.execute("SELECT epoch FROM control").fetchone()[0] == 12
    assert store.enqueue("account-a", "video-a", 2)["offset"] == 2


def test_concurrent_queue_open_and_account_writes(tmp_path):
    path = tmp_path / "sync.sqlite3"
    def enqueue(index):
        return SyncStore(path).enqueue(f"account-{index}", "video-a", index)
    with ThreadPoolExecutor(max_workers=8) as pool:
        records = list(pool.map(enqueue, range(16)))
    store = SyncStore(path)
    assert all(store.pending(record["account_id"], "video-a") == record for record in records)
