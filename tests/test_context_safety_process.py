"""P1 trust boundaries: real SQLite and deterministic process interleavings."""
import multiprocessing
import sys
import json
import sqlite3
import threading
import time
import gc
from http.cookiejar import Cookie
from io import BytesIO
from pathlib import Path
from contextlib import closing

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "resources/lib"))
from account.sync_store import SyncStore, ContextLostError
from account.progress_sync import ProgressSync
from account.progress import RemoteProgress
from account.session import SessionError
from account.session import GronkhTVSession
from account.progress import ProgressClient
from test_plugin_playback import plugin


VIDEO = "11111111-1111-4111-8111-111111111111"


@pytest.fixture(autouse=True)
def deterministic_spawn_resource_cleanup():
    # CPython 3.13 can run a SemLock finalizer while resource_tracker itself
    # allocates, re-entering its lock. Collect outside that critical section;
    # do not filter warnings or suppress unclosed resources.
    enabled = gc.isenabled()
    gc.disable()
    try:
        yield
        gc.collect()
    finally:
        if enabled:
            gc.enable()


def cookie(name, value):
    return Cookie(0, name, value, None, False, "backend.gronkh.tv", True,
                  False, "/", True, True, None, True, None, None, {}, False)


class Response(BytesIO):
    def __init__(self, status, body):
        super().__init__(json.dumps(body).encode())
        self.status = status


class SyntheticTransport:
    """Stateful synthetic backend behind the real session/ProgressClient."""
    def __init__(self, jar, calls=None):
        self.jar = jar
        self.calls = calls if calls is not None else []
        self.offsets = {}

    def open(self, request, timeout):
        identity = next((c.value for c in self.jar if c.name == "synthetic-session"), None)
        path = request.full_url.split("/v3", 1)[1]
        self.calls.append((identity, request.method, path))
        if path == "/csrf-cookie":
            self.jar.set_cookie(cookie("XSRF-TOKEN", "synthetic-csrf"))
            return Response(204, {})
        if path == "/auth":
            self.jar.set_cookie(cookie("synthetic-session", "b"))
            return Response(204, {})
        if path == "/users/self":
            return Response(200, {"data": {"id": identity}})
        if request.method == "POST":
            self.offsets[(identity, VIDEO)] = json.loads(request.data)["offset"]
            return Response(200, {"data": {}})
        offset = self.offsets.get((identity, VIDEO))
        progress = {"offset": offset} if offset is not None else None
        return Response(200, {"data": {"watch_progress": progress}})


def login_between_snapshot_and_activation(path, db, begin, saved, activate):
    session = GronkhTVSession(path)
    session.opener = SyntheticTransport(session.cookie_jar)
    assert begin.wait(5)
    # The supported login saves cookies before the caller activates sync.
    result = session.login("synthetic-login", "synthetic-password")
    assert result.authenticated and result.user["id"] == "b"
    saved.set()
    assert activate.wait(5)
    SyncStore(db).activate(result.user["id"], True)


def test_login_cookie_save_between_context_read_and_snapshot_never_sends_a_as_b(
    plugin, monkeypatch, tmp_path
):
    path = tmp_path / "cookies.txt"
    session = GronkhTVSession(path)
    session.cookie_jar.set_cookie(cookie("synthetic-session", "a"))
    session.save()
    store = SyncStore(tmp_path / "progress.sqlite3")
    epoch = store.activate("a", True)
    old = ProgressSync(store, ProgressClient(session), "a", epoch)
    old.record_position(VIDEO, 12, 500)
    before = store.pending("a", VIDEO)
    ctx = multiprocessing.get_context("spawn")
    begin, saved, activate = ctx.Event(), ctx.Event(), ctx.Event()
    process = ctx.Process(target=login_between_snapshot_and_activation,
                          args=(str(path), store.path, begin, saved, activate))
    process.start()
    calls = []

    class Snapshot(GronkhTVSession):
        def __init__(self, path):
            super().__init__(path)
            self.opener = SyntheticTransport(self.cookie_jar, calls)

    class BarrierStore(SyncStore):
        def context(self):
            result = super().context()
            begin.set()
            assert saved.wait(5)
            return result

    monkeypatch.setattr(plugin, "SyncStore", BarrierStore)
    monkeypatch.setattr(plugin, "GronkhTVSession", Snapshot)
    monkeypatch.setattr(plugin, "_PROGRESS_SYNC_FILE", store.path)
    monkeypatch.setattr(plugin, "_ACCOUNT_COOKIE_FILE", str(path))
    monkeypatch.setattr(plugin, "_setting_enabled", lambda *args, **kwargs: True)
    try:
        sync = plugin._account_progress_sync()
        assert saved.is_set()
        assert sync is not None
        # Do not create a new record: specifically try replaying A's old queue.
        sync.flush(VIDEO)
        assert ("b", "POST", "/watched/progress/" + VIDEO) not in calls
        assert sync._account_id == "b"
        assert ("b", "GET", "/users/self") in calls
        assert store.pending("a", VIDEO) == before
        assert old.prepare_playback(VIDEO)["state"] == "pending"
    finally:
        activate.set()
        process.join(5)
        if process.is_alive():
            process.terminate()
        process.join(5)
        assert process.exitcode == 0


class API:
    def read(self, video):
        return RemoteProgress(status="absent")

    def write(self, video, offset):
        pass


def invalidate(path, ready, resume, event):
    store = SyncStore(path)
    ready.set()
    assert resume.wait(5)
    if event == "logout":
        store.deactivate()
    elif event == "optout":
        store.disable()
    elif event == "switch":
        store.activate("b", True)
    elif event == "aba":
        store.activate("b", True)
        store.activate("a", True)
    else:
        store.activate("a", True)


@pytest.mark.parametrize("event", ["logout", "optout", "switch", "aba", "relogin"])
def test_two_process_old_epoch_cannot_mutate_or_ack(tmp_path, event):
    store = SyncStore(tmp_path / "progress.sqlite3")
    epoch = store.activate("a", True)
    sync = ProgressSync(store, API(), "a", epoch)
    sync.record_position("video", 12, 500)
    store.observe_remote("a", epoch, "video", {"status": "valid", "offset": 42})
    before = store.pending("a", "video")
    lease = store.claim_upload("a", 30)
    ctx = multiprocessing.get_context("spawn")
    ready, resume = ctx.Event(), ctx.Event()
    worker = ctx.Process(target=invalidate, args=(store.path, ready, resume, event))
    worker.start()
    try:
        assert ready.wait(5)
        resume.set()
        worker.join(5)
        assert worker.exitcode == 0
        with pytest.raises(ContextLostError):
            sync.record_position("video", 0, 500)
        for choice in ("kodi", "account"):
            with pytest.raises(RuntimeError):
                sync.resolve_conflict("video", choice)
        assert not store.confirm_upload("a", epoch, lease, "video", before["revision"])
        assert store.pending("a", "video") == before
        assert sync.flush("video") == "pending"
    finally:
        if worker.is_alive():
            worker.terminate()
        worker.join(5)


def test_expired_lease_without_successor_preserves_pending(tmp_path):
    now = [100.0]
    store = SyncStore(tmp_path / "progress.sqlite3", clock=lambda: now[0])
    epoch = store.activate("a", True)
    sync = ProgressSync(store, API(), "a", epoch)
    sync.record_position("video", 12, 500)
    row = store.pending("a", "video")
    lease = store.claim_upload("a", 5)
    now[0] = 105.0
    assert not store.confirm_upload("a", epoch, lease, "video", row["revision"])
    assert store.pending("a", "video") == row


def test_real_progress_client_preserves_zero_reverse_and_read_only_browse(plugin, monkeypatch, tmp_path):
    path = tmp_path / "cookies.txt"
    session = GronkhTVSession(path)
    session.cookie_jar.set_cookie(cookie("synthetic-session", "a"))
    session.save()
    store = SyncStore(tmp_path / "progress.sqlite3")
    store.activate("a", True)
    calls = []

    class Snapshot(GronkhTVSession):
        def __init__(self, path):
            super().__init__(path)
            self.opener = SyntheticTransport(self.cookie_jar, calls)

    monkeypatch.setattr(plugin, "GronkhTVSession", Snapshot)
    monkeypatch.setattr(plugin, "_PROGRESS_SYNC_FILE", store.path)
    monkeypatch.setattr(plugin, "_ACCOUNT_COOKIE_FILE", str(path))
    monkeypatch.setattr(plugin, "_setting_enabled", lambda *args, **kwargs: True)
    sync = plugin._account_progress_sync()
    assert sync is not None
    assert sync.prepare_playback(VIDEO)["state"] == "absent"
    assert not any(method == "POST" for _, method, _ in calls)
    for offset in (100, 20, 0):
        sync.record_position(VIDEO, offset, 500)
        assert sync.flush(VIDEO) == "synchronized"
        assert sync.prepare_playback(VIDEO)["offset"] == offset
    from dataclasses import FrozenInstanceError
    with pytest.raises(FrozenInstanceError):
        sync.context.account_id = "b"
    with pytest.raises(AttributeError):
        sync.context = None


def test_resume_uses_remaining_context_budget_and_ignores_late_result(plugin, monkeypatch):
    from types import SimpleNamespace
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    class Sync:
        def prepare_playback(self, video):
            started.set()
            try:
                assert release.wait(5)
                return {"state": "valid", "offset": 99}
            finally:
                finished.set()

    monkeypatch.setattr(plugin.xbmc, "Monitor", lambda: SimpleNamespace(abortRequested=lambda: False))
    start = time.monotonic()
    try:
        assert plugin._account_resume_offset(Sync(), VIDEO, 7, deadline=start + 0.05) == 7
        assert time.monotonic() - start < 0.25
        assert started.is_set()
    finally:
        release.set()
        assert finished.wait(5)
    # Exhausted setup budget cannot even dispatch a new progress request.
    assert plugin._account_resume_offset(Sync(), VIDEO, 7, deadline=start) == 7


def test_retry_checks_expiry_and_does_not_send_again(tmp_path):
    now = [100.0]
    store = SyncStore(tmp_path / "progress.sqlite3", clock=lambda: now[0])
    epoch = store.activate("a", True)
    calls = []

    class ExpiringAPI(API):
        def write(self, video, offset):
            calls.append(offset)
            now[0] = 106.0
            raise SessionError("synthetic timeout")

    sync = ProgressSync(store, ExpiringAPI(), "a", epoch)
    sync.record_position("video", 12, 500)
    assert sync.flush("video") == "pending"
    assert calls == [12]
    assert store.pending("a", "video") is not None


def mutate_after_writer_lock(path, epoch, action, ready, go, begin, output):
    store = SyncStore(path)
    connect = store._connection

    def connection():
        db = connect()
        db.set_trace_callback(lambda sql: begin.set() if sql == "BEGIN IMMEDIATE" else None)
        return db

    store._connection = connection
    ready.set()
    assert go.wait(5)
    try:
        if action == "record":
            result = store.record_local("a", epoch, "video", 0, 500)
        elif action == "kodi":
            result = store.rebase_pending_on_observed_remote("a", epoch, "video")
        else:
            result = store.accept_observed_remote("a", epoch, "video")
        output.put(result)
    except ContextLostError:
        output.put(None)


@pytest.mark.parametrize("action", ["record", "kodi", "account"])
def test_mutation_rechecks_epoch_inside_begin_immediate(tmp_path, action):
    store = SyncStore(tmp_path / "progress.sqlite3")
    epoch = store.activate("a", True)
    store.record_local("a", epoch, "video", 12, 500)
    store.observe_remote("a", epoch, "video", {"status": "valid", "offset": 42})
    before = store.pending("a", "video")
    ctx = multiprocessing.get_context("spawn")
    ready, go, begin, output = ctx.Event(), ctx.Event(), ctx.Event(), ctx.Queue()
    process = ctx.Process(target=mutate_after_writer_lock,
                          args=(store.path, epoch, action, ready, go, begin, output))
    process.start()
    try:
        assert ready.wait(5)
        with closing(sqlite3.connect(store.path)) as db, db:
            db.execute("BEGIN IMMEDIATE")
            go.set()
            assert begin.wait(5)
            # Same-account new login commits while the mutation waits for us.
            db.execute("UPDATE control SET epoch=epoch+1 WHERE singleton=1")
        process.join(5)
        assert process.exitcode == 0
        assert output.get(timeout=2) is None
        assert store.pending("a", "video") == before
    finally:
        if process.is_alive():
            process.terminate()
        process.join(5)


@pytest.mark.parametrize("blocked", ["session", "sqlite"])
def test_entire_factory_and_listing_are_bounded_and_do_not_reactivate_late(
    plugin, monkeypatch, tmp_path, blocked
):
    path = tmp_path / "cookies.txt"
    path.write_text("synthetic-marker")
    store = SyncStore(tmp_path / "progress.sqlite3")
    before = store.context()
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    calls = []

    class Session:
        def current_user(self, timeout=None):
            calls.append(timeout)
            started.set()
            assert release.wait(5)
            return {"id": "a"}

    original_build = plugin._build_account_progress_sync

    def build(deadline):
        try:
            return original_build(deadline)
        finally:
            finished.set()

    monkeypatch.setattr(plugin, "_build_account_progress_sync", build)
    monkeypatch.setattr(plugin, "GronkhTVSession", lambda path: Session())
    monkeypatch.setattr(plugin, "_PROGRESS_SYNC_FILE", store.path)
    monkeypatch.setattr(plugin, "_ACCOUNT_COOKIE_FILE", str(path))
    monkeypatch.setattr(plugin, "_setting_enabled", lambda *args, **kwargs: True)
    db = sqlite3.connect(store.path)
    if blocked == "sqlite":
        db.execute("BEGIN IMMEDIATE")
    start = time.monotonic()
    try:
        assert plugin._listing_account_progress([], timeout_seconds=0.05) == {}
        assert time.monotonic() - start < 0.25
        if blocked == "session":
            assert started.is_set()
        # A still blocked initializer cannot accumulate new threads/requests.
        for _ in range(10):
            assert plugin._account_progress_sync(timeout_seconds=0.05) is None
    finally:
        db.rollback()
        db.close()
        release.set()
        assert finished.wait(5)
    assert store.context() == before
    assert len(calls) <= 1


@pytest.mark.parametrize("boundary", ["entry", "read", "retry", "ack"])
def test_observed_optout_fences_each_private_boundary_and_reenable_validates(
    plugin, monkeypatch, tmp_path, boundary
):
    settings = {"sync_watch_progress": "true"}
    store = SyncStore(tmp_path / "progress.sqlite3")
    epoch = store.activate("a", True)
    path = tmp_path / "cookies.txt"
    session = GronkhTVSession(path)
    session.cookie_jar.set_cookie(cookie("synthetic-session", "a"))
    session.save()
    calls = []
    armed = [False]

    class Transport(SyntheticTransport):
        def open(self, request, timeout):
            response = super().open(request, timeout)
            is_read = request.method == "GET" and "/videos/" in request.full_url
            if armed[0] and ((boundary == "read" and is_read) or
                             (boundary in ("retry", "ack") and request.method == "POST")):
                settings["sync_watch_progress"] = "false"
                if boundary == "retry":
                    raise SessionError("synthetic transient write failure")
            return response

    class Snapshot(GronkhTVSession):
        def __init__(self, path):
            super().__init__(path)
            self.opener = Transport(self.cookie_jar, calls)

    monkeypatch.setattr(plugin, "GronkhTVSession", Snapshot)
    monkeypatch.setattr(plugin, "_PROGRESS_SYNC_FILE", store.path)
    monkeypatch.setattr(plugin, "_ACCOUNT_COOKIE_FILE", str(path))
    monkeypatch.setattr(plugin._addon, "getSetting", lambda key: settings.get(key, ""))
    sync = plugin._account_progress_sync()
    assert sync is not None
    sync.record_position(VIDEO, 12, 500)
    pending = store.pending("a", VIDEO)
    armed[0] = True
    if boundary == "entry":
        settings["sync_watch_progress"] = "false"
        monkeypatch.setattr(plugin, "router", lambda params: None)
        plugin.run()
    else:
        assert sync.flush(VIDEO) == "pending"
    stopped = store.context()
    assert stopped["sync_enabled"] == 0
    assert stopped["epoch"] > epoch
    requests_at_stop = list(calls)
    with pytest.raises(ContextLostError):
        sync.record_position(VIDEO, 0, 500)
    assert sync.flush(VIDEO) == "pending"
    assert calls == requests_at_stop
    assert store.pending("a", VIDEO)["revision"] == pending["revision"]
    assert len([c for c in calls if c[1] == "POST"]) == (1 if boundary in ("ack", "retry") else 0)
    armed[0] = False
    settings["sync_watch_progress"] = "true"
    fresh = plugin._account_progress_sync()
    assert fresh is not None and fresh._epoch > sync._epoch
    assert len([c for c in calls if c[2] == "/users/self"]) == 2
    with pytest.raises(ContextLostError):
        sync.record_position(VIDEO, 0, 500)
    assert store.pending("a", VIDEO)["revision"] == pending["revision"]


@pytest.mark.parametrize("outcome", ["success", "authentication", "transport"])
@pytest.mark.parametrize("event", ["logout", "switch", "aba", "relogin", "optout"])
def test_late_reconciliation_cannot_change_newer_context_or_ui(
    plugin, monkeypatch, tmp_path, outcome, event
):
    from account.session import AuthenticationError

    store = SyncStore(tmp_path / "progress.sqlite3")
    epoch = store.activate("a", True)
    cookie_path = tmp_path / "cookies.txt"
    cookie_path.write_text("synthetic")
    calls = []
    ctx = multiprocessing.get_context("spawn")
    ready, resume = ctx.Event(), ctx.Event()
    process = ctx.Process(target=invalidate, args=(store.path, ready, resume, event))
    process.start()

    class Session:
        def current_user(self):
            assert ready.wait(5)
            resume.set()
            process.join(5)
            assert process.exitcode == 0
            if outcome == "authentication":
                raise AuthenticationError("synthetic")
            if outcome == "transport":
                raise SessionError("synthetic")
            return {"id": "a", "login": "synthetic"}

    monkeypatch.setattr(plugin, "_account_session", Session())
    monkeypatch.setattr(plugin, "_PROGRESS_SYNC_FILE", store.path)
    monkeypatch.setattr(plugin, "_ACCOUNT_COOKIE_FILE", str(cookie_path))
    monkeypatch.setattr(plugin, "_setting_enabled", lambda *args, **kwargs: True)
    monkeypatch.setattr(plugin._addon, "setSetting", lambda *args: calls.append(args))
    monkeypatch.setattr(plugin, "configure_account_session", lambda session: calls.append(session))
    try:
        assert plugin._read_account_user(expected_sync_epoch=epoch) is None
        assert store.context()["epoch"] > epoch
        assert calls == []
        assert cookie_path.read_text() == "synthetic"
    finally:
        if process.is_alive():
            process.terminate()
        process.join(5)


def test_monitor_observes_optout_before_next_poll_and_final_snapshot(plugin, monkeypatch, tmp_path):
    from test_plugin_playback import PlaybackMonitor, PlaybackPlayer
    settings = {"sync_watch_progress": "true"}
    store = SyncStore(tmp_path / "progress.sqlite3")
    epoch = store.activate("a", True)
    store.record_local("a", epoch, VIDEO, 12, 500)
    pending = store.pending("a", VIDEO)

    class ForbiddenAPI:
        def read(self, video):
            pytest.fail("private read after observed opt-out")

        def write(self, video, offset):
            pytest.fail("private write after observed opt-out")

    sync = ProgressSync(store, ForbiddenAPI(), "a", epoch,
                        enabled=lambda: settings["sync_watch_progress"] == "true")

    class Monitor(PlaybackMonitor):
        def waitForAbort(self, seconds):
            settings["sync_watch_progress"] = "false"
            return super().waitForAbort(seconds)

    monitor = Monitor(3)
    player = PlaybackPlayer(monitor, [(100, 500), (90, 500), (0, 500)])
    saves = []
    monkeypatch.setattr(plugin._addon, "getSetting", lambda key: settings.get(key, ""))
    monkeypatch.setattr(plugin.xbmc, "Player", lambda: player)
    monkeypatch.setattr(plugin.xbmc, "Monitor", lambda: monitor)
    monkeypatch.setattr(plugin.time, "monotonic", lambda: monitor.now)
    monkeypatch.setattr(plugin, "save_resume_point", lambda *args: saves.append(args))
    plugin.monitor_playback(1, progress_sync=sync, video_id=VIDEO)
    assert store.context()["sync_enabled"] == 0
    assert store.pending("a", VIDEO) == pending
    assert saves[-1] == (1, 0, 500)


def test_blocked_flush_cannot_accumulate_threads(plugin, monkeypatch):
    started, release = threading.Event(), threading.Event()
    threads = []
    real_thread = threading.Thread

    def thread(*args, **kwargs):
        worker = real_thread(*args, **kwargs)
        threads.append(worker)
        return worker

    class Sync:
        def flush(self, video):
            started.set()
            assert release.wait(5)

    monkeypatch.setattr(plugin.threading, "Thread", thread)
    try:
        plugin._flush_progress_async(Sync(), VIDEO)
        assert started.wait(5)
        for _ in range(20):
            plugin._flush_progress_async(Sync(), VIDEO)
        assert len(threads) == 1
    finally:
        release.set()
        for worker in threads:
            worker.join(5)
    plugin._flush_progress_async(Sync(), VIDEO)
    for worker in threads:
        worker.join(5)
    assert len(threads) == 2


@pytest.mark.parametrize("failed", [False, True])
def test_logout_fences_before_remote_and_does_not_clear_newer_ui(plugin, monkeypatch, tmp_path, failed):
    from types import SimpleNamespace
    store = SyncStore(tmp_path / "progress.sqlite3")
    old_epoch = store.activate("a", True)
    calls = []

    class Session:
        def logout(self):
            assert store.context()["epoch"] > old_epoch
            assert store.context()["sync_enabled"] == 0
            calls.append("remote")
            store.activate("b", True)
            if failed:
                raise SessionError("synthetic unavailable")

        def clear(self):
            calls.append("clear")

    monkeypatch.setattr(plugin, "_PROGRESS_SYNC_FILE", store.path)
    monkeypatch.setattr(plugin, "_account_session", Session())
    monkeypatch.setattr(plugin.xbmcgui, "Dialog", lambda: SimpleNamespace(yesno=lambda *args: True, notification=lambda *args: calls.append("notification")))
    monkeypatch.setattr(plugin, "configure_account_session", lambda session: calls.append("configure"))
    monkeypatch.setattr(plugin._addon, "setSetting", lambda *args: calls.append("setting"))
    monkeypatch.setattr(plugin.xbmc, "executebuiltin", lambda *args: calls.append("refresh"), raising=False)
    plugin.handle_account_logout()
    assert calls == ["remote"]
    assert store.context()["active_account_id"] == "b"


@pytest.mark.parametrize("authoritative", [False, True])
def test_reconciliation_failure_keeps_cookies_pending_and_distinguishes_transport(
    plugin, monkeypatch, tmp_path, authoritative
):
    from account.session import AuthenticationError
    store = SyncStore(tmp_path / "progress.sqlite3")
    epoch = store.activate("a", True)
    store.record_local("a", epoch, VIDEO, 12, 500)
    pending = store.pending("a", VIDEO)
    path = tmp_path / "cookies.txt"
    path.write_text("synthetic")
    settings = {}

    class Session:
        def current_user(self):
            error = AuthenticationError if authoritative else SessionError
            raise error("synthetic")

    monkeypatch.setattr(plugin, "_account_session", Session())
    monkeypatch.setattr(plugin, "_PROGRESS_SYNC_FILE", store.path)
    monkeypatch.setattr(plugin, "_ACCOUNT_COOKIE_FILE", str(path))
    monkeypatch.setattr(plugin._addon, "setSetting", settings.__setitem__)
    assert plugin._read_account_user() is None
    assert store.pending("a", VIDEO) == pending
    assert path.read_text() == "synthetic"
    assert settings["account_status"] == ("30210" if authoritative else "30227")
    assert store.context()["sync_enabled"] == (0 if authoritative else 1)
    assert store.context()["epoch"] == epoch + (1 if authoritative else 0)


def test_logout_does_not_dispatch_when_durable_fence_is_unavailable(plugin, monkeypatch):
    from types import SimpleNamespace
    notifications = []

    class Store:
        def __init__(self, path):
            pass

        def deactivate(self):
            raise sqlite3.OperationalError("synthetic busy")

    monkeypatch.setattr(plugin, "SyncStore", Store)
    monkeypatch.setattr(plugin, "_account_session", SimpleNamespace(logout=lambda: pytest.fail("unfenced logout")))
    monkeypatch.setattr(plugin.xbmcgui, "Dialog", lambda: SimpleNamespace(yesno=lambda *args: True, notification=lambda *args: notifications.append(args)))
    monkeypatch.setattr(plugin._addon, "setSetting", lambda *args: pytest.fail("false logout state"))
    plugin.handle_account_logout()
    assert notifications[0][1] == "30227"


def delayed_upload(path, cookies, epoch, now, dispatched, release, output):
    store = SyncStore(path, clock=lambda: now.value)
    session = GronkhTVSession(cookies)

    class Transport(SyntheticTransport):
        def open(self, request, timeout):
            response = super().open(request, timeout)
            if request.method == "POST":
                dispatched.set()
                assert release.wait(5)
            return response

    session.opener = Transport(session.cookie_jar)
    sync = ProgressSync(store, ProgressClient(session), "a", epoch)
    output.put(sync.flush(VIDEO))


@pytest.mark.parametrize("successor", [False, True])
def test_two_process_successful_post_after_lease_expiry_cannot_ack(tmp_path, successor):
    ctx = multiprocessing.get_context("spawn")
    now = ctx.Value("d", 100.0)
    store = SyncStore(tmp_path / "progress.sqlite3", clock=lambda: now.value)
    epoch = store.activate("a", True)
    store.record_local("a", epoch, VIDEO, 12, 500)
    cookies = tmp_path / "cookies.txt"
    session = GronkhTVSession(cookies)
    session.cookie_jar.set_cookie(cookie("synthetic-session", "a"))
    session.save()
    dispatched, release, output = ctx.Event(), ctx.Event(), ctx.Queue()
    process = ctx.Process(target=delayed_upload,
                          args=(store.path, str(cookies), epoch, now, dispatched, release, output))
    process.start()
    try:
        assert dispatched.wait(5)
        now.value = 106.0
        if successor:
            assert store.claim_upload("a", 30, epoch=epoch) is not None
        release.set()
        process.join(5)
        assert process.exitcode == 0
        assert output.get(timeout=2) == "pending"
        assert store.pending("a", VIDEO)["offset"] == 12
    finally:
        release.set()
        if process.is_alive():
            process.terminate()
        process.join(5)
