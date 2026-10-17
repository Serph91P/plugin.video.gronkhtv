"""End-to-end reset-controller boundaries using the production router and stores."""
import concurrent.futures
import json
import sqlite3
import threading

from pathlib import Path

import pytest

from test_plugin_playback import plugin
from test_reset_marker_lifecycle import OTHER_VIDEO, VIDEO, local_listing, separate_invoker
from account.session import AuthenticationError, RateLimitError, ValidationError
from account.sync_store import SyncStore


@pytest.fixture
def stopped_player(monkeypatch, plugin):
    class StoppedPlayer:
        def isPlayingVideo(self):
            return False

    monkeypatch.setattr(plugin.xbmc, "Player", StoppedPlayer)


def _progress_rows(store):
    with sqlite3.connect(store.path) as database:
        return database.execute(
            """SELECT account_id, video_id, offset, duration, revision, pending,
                      confirmed_json, observed_json
               FROM progress ORDER BY account_id, video_id"""
        ).fetchall()


def _seed_remote_snapshots(store, account_id, video_id, confirmed_offset, observed_offset):
    with sqlite3.connect(store.path) as database:
        database.execute(
            """UPDATE progress SET confirmed_json=?, observed_json=?
               WHERE account_id=? AND video_id=?""",
            (
                json.dumps({"status": "valid", "offset": confirmed_offset}, sort_keys=True),
                json.dumps({"status": "valid", "offset": observed_offset}, sort_keys=True),
                account_id,
                video_id,
            ),
        )


def _notifications(plugin, monkeypatch):
    calls = []

    class Dialog:
        def yesno(self, *_args, **_kwargs):
            return True

        def notification(self, _heading, message, icon):
            calls.append((message, icon))

    monkeypatch.setattr(plugin.xbmcgui, "Dialog", Dialog)
    return calls


def test_router_reset_reports_primary_delete_failure_and_allows_healthy_retry(
    local_listing, stopped_player, monkeypatch
):
    plugin, remote, _settings, _items, _real_listing = local_listing
    messages = _notifications(plugin, monkeypatch)
    resume_path = plugin._resume_path(1105)
    backup_path = f"{resume_path}.bak"
    plugin.write_record(plugin.xbmcvfs, str(Path(resume_path).parent), backup_path, 2, 300)
    real_delete = plugin.xbmcvfs.delete
    failing = {"value": True}

    def fail_primary_delete(path):
        if path == resume_path and failing["value"]:
            return False
        return real_delete(path)

    monkeypatch.setattr(plugin.xbmcvfs, "delete", fail_primary_delete)

    assert plugin.handle_reset_resume({"episode": "1105"}) is False
    assert remote["writes"] == [0]
    assert Path(resume_path).exists()
    assert not Path(backup_path).exists()
    assert any("lokales Löschen fehlgeschlagen" in message for message, _icon in messages)

    failing["value"] = False
    assert plugin.handle_reset_resume({"episode": "1105"}) is True
    assert remote["writes"] == [0, 0]
    assert not Path(resume_path).exists()
    assert not Path(backup_path).exists()


@pytest.mark.parametrize("state", ["missing", "stale", "opt-out"])
def test_router_reset_refuses_unavailable_account_states_without_zero_write(
    local_listing, stopped_player, monkeypatch, state
):
    plugin, remote, settings, _items, _real_listing = local_listing
    messages = _notifications(plugin, monkeypatch)
    resume_path = plugin._resume_path(1105)

    if state == "missing":
        Path(plugin._ACCOUNT_COOKIE_FILE).unlink()
    elif state == "stale":
        class StaleSession:
            def current_user(self, timeout=None):
                raise AuthenticationError("synthetic stale session")

        plugin._account_session_validated = False
        monkeypatch.setattr(plugin, "_account_session", StaleSession())
    else:
        settings["sync_watch_progress"] = "false"

    assert plugin.handle_reset_resume({"episode": "1105"}) is False
    assert remote["writes"] == []
    assert plugin.get_resume_record(1105, 300)["position"] == 2
    assert any("Anmeldung und Fortschrittssynchronisierung erforderlich" in message for message, _icon in messages)
    assert Path(resume_path).exists()


def test_router_reset_changes_only_selected_account_video_rows_and_private_file(
    local_listing, stopped_player, monkeypatch
):
    plugin, remote, _settings, _items, _real_listing = local_listing
    _notifications(plugin, monkeypatch)
    store = plugin.SyncStore(plugin._PROGRESS_SYNC_FILE)
    other_epoch = store.activate("other-account", enabled=True)
    store.record_local("other-account", OTHER_VIDEO, 66, 300)
    store.observe_remote(other_epoch and "other-account", other_epoch, OTHER_VIDEO, {"status": "valid", "offset": 65})
    store.record_local("other-account", VIDEO, 64, 300)
    store.observe_remote(other_epoch and "other-account", other_epoch, VIDEO, {"status": "valid", "offset": 63})
    selected_epoch = store.activate("synthetic-account", enabled=True)
    store.record_local("synthetic-account", OTHER_VIDEO, 77, 300)
    store.observe_remote("synthetic-account", selected_epoch, OTHER_VIDEO, {"status": "valid", "offset": 76})
    _seed_remote_snapshots(store, "other-account", OTHER_VIDEO, 65, 64)
    _seed_remote_snapshots(store, "other-account", VIDEO, 63, 62)
    _seed_remote_snapshots(store, "synthetic-account", OTHER_VIDEO, 76, 75)
    other_path = plugin._resume_path(1106)
    plugin.write_record(plugin.xbmcvfs, str(Path(other_path).parent), other_path, 75, 300)
    before = _progress_rows(store)

    assert plugin.handle_reset_resume({"episode": "1105"}) is True

    assert remote["writes"] == [0]
    assert plugin.get_resume_record(1106, 300)["position"] == 75
    after_without_selected = [
        row for row in _progress_rows(store) if row[:2] != ("synthetic-account", VIDEO)
    ]
    before_without_selected = [
        row for row in before if row[:2] != ("synthetic-account", VIDEO)
    ]
    assert after_without_selected == before_without_selected
    selected = next(row for row in _progress_rows(store) if row[:2] == ("synthetic-account", VIDEO))
    assert selected[2] == 0
    assert selected[5] == 0
    assert '"offset": 0' in selected[6]
    assert '"offset": 0' in selected[7]


@pytest.mark.parametrize(
    "failure",
    [
        AuthenticationError("synthetic denied"),
        RateLimitError("synthetic rate limit", retry_after=1),
        ValidationError("synthetic validation"),
        RuntimeError("synthetic network"),
    ],
    ids=["denied", "rate-limited", "validation", "network"],
)
def test_router_reset_failure_reports_error_and_preserves_selected_retry_intent(
    local_listing, stopped_player, monkeypatch, failure
):
    plugin, remote, _settings, _items, _real_listing = local_listing
    messages = _notifications(plugin, monkeypatch)
    posts = []

    class FailingSession:
        def request_json(self, path, method="GET", payload=None, timeout=None):
            assert method == "POST" and path == "/watched/progress/" + VIDEO
            posts.append((path, payload))
            raise failure

    monkeypatch.setattr(plugin, "GronkhTVSession", lambda *_args, **_kwargs: FailingSession())

    store = plugin.SyncStore(plugin._PROGRESS_SYNC_FILE)
    other_epoch = store.activate("other-account", enabled=True)
    store.record_local("other-account", VIDEO, 64, 300)
    store.observe_remote("other-account", other_epoch, VIDEO, {"status": "valid", "offset": 63})
    selected_epoch = store.activate("synthetic-account", enabled=True)
    store.record_local("synthetic-account", VIDEO, 2, 300)
    store.record_local("synthetic-account", OTHER_VIDEO, 77, 300)
    store.observe_remote("synthetic-account", selected_epoch, OTHER_VIDEO, {"status": "valid", "offset": 76})
    _seed_remote_snapshots(store, "other-account", VIDEO, 63, 62)
    _seed_remote_snapshots(store, "synthetic-account", OTHER_VIDEO, 76, 75)
    _seed_remote_snapshots(store, "synthetic-account", VIDEO, 2, 2)
    before = _progress_rows(store)
    assert next(row for row in before if row[:2] == ("synthetic-account", VIDEO))[6] == json.dumps(
        {"status": "valid", "offset": 2}, sort_keys=True
    )

    assert plugin.handle_reset_resume({"episode": "1105"}) is False
    assert posts == [("/watched/progress/" + VIDEO, {"offset": 0})]
    assert remote["writes"] == []
    assert plugin.get_resume_record(1105, 300)["position"] == 2
    sync = plugin._account_progress_sync()
    assert sync._store.pending("synthetic-account", VIDEO)["offset"] == 0
    assert sync._store.observed_remote("synthetic-account", VIDEO) == {"status": "valid", "offset": 2}
    selected = next(row for row in _progress_rows(store) if row[:2] == ("synthetic-account", VIDEO))
    assert selected[6] == json.dumps({"status": "valid", "offset": 2}, sort_keys=True)
    assert [row for row in _progress_rows(store) if row[:2] != ("synthetic-account", VIDEO)] == [
        row for row in before if row[:2] != ("synthetic-account", VIDEO)
    ]
    assert any("nicht vollständig zurückgesetzt" in message for message, _icon in messages)


@pytest.mark.parametrize("attempt", range(6))
def test_parallel_openers_upgrade_legacy_profile_without_losing_pending_rows(tmp_path, attempt):
    """Actual two-opener migration keeps the legacy profile usable for reset."""
    database_path = tmp_path / "legacy-progress.sqlite3"
    with sqlite3.connect(database_path) as database:
        database.executescript(
            """
            CREATE TABLE control (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                active_account_id TEXT, epoch INTEGER NOT NULL DEFAULT 0,
                sync_enabled INTEGER NOT NULL DEFAULT 0, fence INTEGER NOT NULL DEFAULT 0,
                lease_account_id TEXT, lease_fence INTEGER, lease_expires REAL
            );
            INSERT INTO control(singleton, active_account_id, epoch, sync_enabled)
            VALUES (1, 'synthetic-account', 1, 1);
            CREATE TABLE progress (
                account_id TEXT NOT NULL, video_id TEXT NOT NULL, offset REAL NOT NULL,
                duration REAL NOT NULL, revision INTEGER NOT NULL, pending INTEGER NOT NULL,
                confirmed_json TEXT, observed_json TEXT,
                PRIMARY KEY(account_id, video_id)
            );
            INSERT INTO progress(account_id, video_id, offset, duration, revision, pending)
            VALUES ('synthetic-account', 'synthetic-video', 2, 300, 1, 1);
            """
        )

    start = threading.Barrier(2)

    def open_store():
        start.wait(timeout=5)
        return SyncStore(database_path)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(open_store) for _ in range(2)]
        results = [future.result(timeout=10) for future in futures]

    assert len(results) == 2
    pending = results[0].pending("synthetic-account", "synthetic-video")
    assert {key: pending[key] for key in ("account_id", "video_id", "offset", "duration", "revision", "pending")} == {
        "account_id": "synthetic-account", "video_id": "synthetic-video",
        "offset": 2.0, "duration": 300.0, "revision": 1, "pending": 1,
    }
    with sqlite3.connect(database_path) as database:
        columns = {row[1] for row in database.execute("PRAGMA table_info(control)")}
    assert {"writer_account_id", "writer_video_id", "writer_fence", "writer_expires"} <= columns


@pytest.mark.parametrize("attempt", range(6))
def test_parallel_first_openers_initialize_one_new_profile(tmp_path, attempt):
    """Two real first opens tolerate the one-time WAL transition."""
    database_path = tmp_path / "new-progress.sqlite3"
    start = threading.Barrier(2)

    def open_store():
        start.wait(timeout=5)
        return SyncStore(database_path)

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(open_store) for _ in range(2)]
        stores = [future.result(timeout=10) for future in futures]

    assert len(stores) == 2
    assert stores[0].context() == stores[1].context()


def test_expired_real_monitor_defers_reset_until_its_finalizer_has_stopped(
    local_listing, monkeypatch
):
    """An expired lease never authorizes a concurrent reset over a live monitor."""
    plugin, remote, _settings, _items, _real_listing = local_listing
    clock = {"now": 100.0}
    real_store = plugin.SyncStore
    monkeypatch.setattr(plugin, "SyncStore", lambda path: real_store(path, clock=lambda: clock["now"]))
    state = {"playing": True, "reset_result": None}

    class Player:
        def isPlayingVideo(self): return state["playing"]
        def isPlaying(self): return state["playing"]
        def getTime(self): return 2
        def getTotalTime(self): return 300
        def getPlaySpeed(self): return 1
        def getSpeed(self): return 1
        def play(self, *_args, **_kwargs): pytest.fail("unexpected playback control")
        def stop(self, *_args, **_kwargs): pytest.fail("unexpected playback control")
        def seekTime(self, *_args, **_kwargs): pytest.fail("unexpected playback control")

    class Monitor:
        def abortRequested(self): return False
        def waitForAbort(self, _timeout):
            state["playing"] = False
            clock["now"] += 6
            state["reset_result"] = plugin.handle_reset_resume({"episode": "1105"})
            return True

    monkeypatch.setattr(plugin.xbmc, "Player", Player)
    monkeypatch.setattr(plugin.xbmc, "Monitor", Monitor)
    monkeypatch.setattr(plugin, "_flush_progress_async", lambda coordinator, video: coordinator.flush(video))
    sync = plugin._account_progress_sync()
    assert sync is not None

    plugin.monitor_playback(1105, progress_sync=sync, video_id=VIDEO)

    assert state["reset_result"] is False
    assert 0 not in remote["writes"]


def test_writer_registration_between_directory_snapshot_and_reset_is_fenced(
    local_listing, monkeypatch
):
    """A genuine late monitor guard prevents a reset from publishing zero."""
    plugin, remote, settings, _items, _real_listing = local_listing
    old = separate_invoker(plugin, monkeypatch, "late_monitor_reset_invoker")
    playing = {"old": True}
    sampled = threading.Event()
    release = threading.Event()
    started = threading.Event()
    errors = []

    class Player:
        def isPlayingVideo(self):
            return playing["old"] if threading.current_thread().name == "late-monitor" else False
        def getTime(self): return 2
        def getTotalTime(self): return 300
        def getPlaySpeed(self): return 1
        def getSpeed(self): return 1
        def play(self, *_args, **_kwargs): pytest.fail("unexpected playback control")
        def stop(self, *_args, **_kwargs): pytest.fail("unexpected playback control")
        def seekTime(self, *_args, **_kwargs): pytest.fail("unexpected playback control")

    class Monitor:
        def abortRequested(self): return False
        def waitForAbort(self, _timeout):
            playing["old"] = False
            sampled.set()
            assert release.wait(5)
            return True

    def run_old_monitor():
        try:
            old.monitor_playback(1105, progress_sync=None, video_id=VIDEO)
        except BaseException as error:
            errors.append(error)

    monitor_thread = threading.Thread(target=run_old_monitor, name="late-monitor")

    def snapshot(directory):
        entries = list(Path(directory).iterdir())
        if not started.is_set():
            settings["sync_watch_progress"] = "false"
            started.set()
            monitor_thread.start()
            assert sampled.wait(5)
            settings["sync_watch_progress"] = "true"
        return [], [entry.name for entry in entries if entry.is_file()]

    monkeypatch.setattr(plugin.xbmc, "Player", Player)
    monkeypatch.setattr(plugin.xbmc, "Monitor", Monitor)
    monkeypatch.setattr(plugin.xbmcvfs, "listdir", snapshot, raising=False)
    try:
        result = plugin.handle_reset_resume({"episode": "1105"})
    finally:
        release.set()
        monitor_thread.join(5)

    assert started.is_set() and sampled.is_set()
    assert not monitor_thread.is_alive() and not errors
    record = plugin.get_resume_record(1105, 300)
    if result:
        assert remote["writes"] == [0]
        assert record is None or record["position"] == 0
    else:
        assert 0 not in remote["writes"]
