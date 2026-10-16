"""Regression coverage for observed opt-out under busy context/SQLite boundaries."""
import multiprocessing
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from test_plugin_playback import plugin, PlaybackPlayer, PlaybackMonitor
from test_context_safety_process import SyntheticTransport, cookie, VIDEO
from account.session import GronkhTVSession
from account.sync_store import SyncStore, ContextLostError


@pytest.fixture
def bound(plugin, monkeypatch, tmp_path):
    settings = {'sync_watch_progress': 'true'}
    path = tmp_path / 'cookies.txt'
    session = GronkhTVSession(path)
    session.cookie_jar.set_cookie(cookie('synthetic-session', 'a'))
    session.save()
    store = SyncStore(tmp_path / 'sync.sqlite3')
    calls = []
    block = [False]
    started, release, finished = threading.Event(), threading.Event(), threading.Event()

    class Snapshot(GronkhTVSession):
        def __init__(self, path):
            super().__init__(path)
            self.opener = SyntheticTransport(self.cookie_jar, calls)

        def current_user(self, timeout=None):
            if block[0]:
                started.set()
                assert release.wait(5)
            return super().current_user(timeout=timeout)

    monkeypatch.setattr(plugin, 'GronkhTVSession', Snapshot)
    monkeypatch.setattr(plugin, '_PROGRESS_SYNC_FILE', store.path)
    monkeypatch.setattr(plugin, '_ACCOUNT_COOKIE_FILE', str(path))
    monkeypatch.setattr(plugin._addon, 'getSetting', lambda key: settings.get(key, ''))
    monkeypatch.setattr(plugin, 'router', lambda params: None)
    old = plugin._account_progress_sync()
    assert old is not None
    old.record_position(VIDEO, 12, 500)
    original = plugin._build_account_progress_sync

    def build(deadline):
        try:
            return original(deadline)
        finally:
            finished.set()

    monkeypatch.setattr(plugin, '_build_account_progress_sync', build)
    yield plugin, settings, store, calls, old, block, started, release, finished
    release.set()


def assert_fenced(store, before, old, calls):
    after = store.context()
    assert after['sync_enabled'] == 0 and after['epoch'] > before['epoch']
    with pytest.raises(ContextLostError):
        old.record_position(VIDEO, 0, 500)
    count = len(calls)
    old.prepare_playback(VIDEO)
    assert len(calls) == count
    assert store.pending('a', VIDEO)['offset'] == 12


def test_entry_optout_survives_busy_context_permit(bound):
    plugin, settings, store, calls, old, block, started, release, finished = bound
    before = store.context()
    block[0] = True
    try:
        assert plugin._account_progress_sync(timeout_seconds=0.02) is None
        assert started.is_set()
        settings['sync_watch_progress'] = 'false'
        plugin.run()
        settings['sync_watch_progress'] = 'true'
        assert_fenced(store, before, old, calls)
    finally:
        release.set()
        assert finished.wait(5)


def test_entry_optout_survives_sqlite_initialization_lock(bound):
    plugin, settings, store, calls, old, _, _, _, finished = bound
    before = store.context()
    db = sqlite3.connect(store.path)
    db.execute('BEGIN IMMEDIATE')
    try:
        settings['sync_watch_progress'] = 'false'
        plugin.run()
        assert not finished.is_set()
        settings['sync_watch_progress'] = 'true'
        # Fence is already effective BEFORE the writer releases the database.
        assert old.prepare_playback(VIDEO)['state'] == 'pending'
    finally:
        db.rollback()
        db.close()
    assert finished.wait(5)
    assert_fenced(store, before, old, calls)
    assert sum(call[2] == '/users/self' for call in calls) == 1
    new = plugin._account_progress_sync()
    assert new is not None and new.context.epoch > before['epoch']
    assert sum(call[2] == '/users/self' for call in calls) == 2
    with pytest.raises(ContextLostError):
        old.record_position(VIDEO, 0, 500)


@pytest.mark.parametrize('failure', ['writer', 'database_error', 'finally_database_error'])
def test_monitor_optout_preserves_prompt_samples_and_final_resume(bound, monkeypatch, failure):
    plugin, settings, store, _, sync, _, _, _, _ = bound
    monitor = PlaybackMonitor(1)
    player = PlaybackPlayer(monitor, [(100, 500)])
    monkeypatch.setattr(plugin.xbmc, 'Player', lambda: player)
    monkeypatch.setattr(plugin.xbmc, 'Monitor', lambda: monitor)
    saved = []
    monkeypatch.setattr(plugin, 'save_resume_point', lambda *args: saved.append(args))
    completed, sampled = threading.Event(), threading.Event()
    original_time = player.getTime

    def get_time():
        sampled.set()
        if failure == 'finally_database_error':
            settings['sync_watch_progress'] = 'false'
        return original_time()

    monkeypatch.setattr(player, 'getTime', get_time)
    if failure != 'finally_database_error':
        settings['sync_watch_progress'] = 'false'
    if failure != 'writer':
        def error(self):
            raise sqlite3.OperationalError('synthetic database failure')
        monkeypatch.setattr(SyncStore, 'observe_opt_out', error)
    errors = []

    def run():
        try:
            plugin.monitor_playback(1, progress_sync=sync, video_id=VIDEO)
        except Exception as exc:
            errors.append(type(exc).__name__)
        finally:
            completed.set()

    db = sqlite3.connect(store.path)
    db.execute('BEGIN IMMEDIATE')
    worker = threading.Thread(target=run)
    worker.start()
    try:
        assert sampled.wait(0.25), 'optional sync must not block local sampling'
        assert completed.wait(0.25), 'optional sync must not block final local persistence'
    finally:
        db.rollback()
        db.close()
        worker.join(5)
    assert not errors
    assert saved == [(1, 100, 500)]
    assert store.pending('a', VIDEO)['offset'] == 12
    settings['sync_watch_progress'] = 'true'
    assert sync.prepare_playback(VIDEO)['state'] == 'pending'


def _observe_in_process(path, observed):
    store = SyncStore(path, timeout=0, initialize=False)
    store.observe_opt_out()
    observed.set()


def test_durable_optout_survives_observer_exit_under_another_process_writer(tmp_path):
    # No live daemon is needed to remember the event after the observing process exits.
    store = SyncStore(tmp_path / 'sync.sqlite3')
    epoch = store.activate('a', True)
    pending = store.record_local('a', epoch, VIDEO, 12, 500)
    store.observe_remote('a', epoch, VIDEO, {'status': 'valid', 'offset': 50})
    lease = store.claim_upload('a', 60, epoch=epoch)
    ctx = multiprocessing.get_context('spawn')
    observed = ctx.Event()
    db = sqlite3.connect(store.path)
    db.execute('BEGIN IMMEDIATE')
    worker = ctx.Process(target=_observe_in_process, args=(store.path, observed))
    worker.start()
    try:
        assert observed.wait(5)
        worker.join(5)
        assert worker.exitcode == 0
        assert not store.is_active_context('a', epoch)
        assert not store.owns_lease('a', epoch, lease)
    finally:
        db.rollback()
        db.close()
        worker.join(5)
    with pytest.raises(ContextLostError):
        store.record_local('a', epoch, VIDEO, 0, 500)
    assert store.rebase_pending_on_observed_remote('a', epoch, VIDEO) is None
    assert store.accept_observed_remote('a', epoch, VIDEO) is None
    assert not store.confirm_upload('a', epoch, lease, VIDEO, pending['revision'])
    restarted = SyncStore(store.path)
    assert restarted.context()['sync_enabled'] == 0
    assert restarted.context()['epoch'] > epoch
    assert restarted.pending('a', VIDEO)['offset'] == 12
    assert restarted.reconcile('a', restarted.context()['epoch']) > epoch
    assert not store.is_active_context('a', epoch)


@pytest.mark.parametrize('observe_again', [False, True, 'process'])
def test_disabled_validation_is_fenced_by_next_optout(bound, monkeypatch, observe_again):
    plugin, settings, store, calls, old, block, started, release, finished = bound
    settings['sync_watch_progress'] = 'false'
    assert not plugin._observe_progress_opt_in(store)
    disabled = store.context()
    assert disabled['sync_enabled'] == 0
    settings['sync_watch_progress'] = 'true'
    original = plugin.GronkhTVSession.current_user

    def delayed_response(self, timeout=None):
        user = original(self, timeout=timeout)
        started.set()
        assert release.wait(3)
        return user

    monkeypatch.setattr(plugin.GronkhTVSession, 'current_user', delayed_response)
    results = []
    worker = threading.Thread(target=lambda: results.append(plugin._account_progress_sync(timeout_seconds=2)))
    observer = None
    worker.start()
    try:
        assert started.wait(1)
        if observe_again:
            settings['sync_watch_progress'] = 'false'
            if observe_again == 'process':
                ctx = multiprocessing.get_context('spawn')
                observed = ctx.Event()
                observer = ctx.Process(target=_observe_in_process, args=(store.path, observed))
                observer.start()
                assert observed.wait(3)
                observer.join(1)
                assert observer.exitcode == 0
            else:
                plugin.run()
        settings['sync_watch_progress'] = 'true'
        release.set()
        worker.join(3)
        assert not worker.is_alive()
        sync = results[0]
        count = len(calls)
        if sync is not None:
            sync.prepare_playback(VIDEO)
        if observe_again:
            assert sync is None
            assert store.context()['sync_enabled'] == 0
            assert len(calls) == count
            # Replaying the SAME journal entry is idempotent. Only a fresh
            # snapshot validation after the event may restore the binding.
            context = store.context()
            store.apply_observed_opt_out()
            store.apply_observed_opt_out()
            assert store.context() == context
            fresh = plugin._account_progress_sync()
            assert fresh is not None
            assert fresh.context.epoch > disabled['epoch']
            assert sum(call[2] == '/users/self' for call in calls) == 3
        else:
            assert sync is not None
            assert store.context()['sync_enabled'] == 1
            assert len(calls) == count + 1
        assert store.pending('a', VIDEO)['offset'] == 12
    finally:
        release.set()
        worker.join(3)
        if observer is not None and observer.is_alive():
            observer.terminate()
        if observer is not None:
            observer.join(3)


def test_disabled_cutoff_rejects_validation_before_sqlite_materialization(tmp_path):
    store = SyncStore(tmp_path / 'sync.sqlite3')
    store.activate('a', True)
    store.observe_opt_out()
    epoch = store.context()['epoch']
    db = sqlite3.connect(store.path)
    db.execute('BEGIN IMMEDIATE')
    try:
        store.observe_opt_out()
        assert store.context()['epoch'] == epoch
    finally:
        db.rollback()
        db.close()
    assert store.reconcile('a', epoch) is None
    store.apply_observed_opt_out()
    current = store.context()
    assert current['epoch'] > epoch and current['sync_enabled'] == 0
    store.apply_observed_opt_out()
    assert store.context() == current
    assert store.reconcile('a', current['epoch']) > current['epoch']


def _overlap_cutoff_observer(path, ready, begin, captured, publish):
    """Publish an opt-out only after it has captured the pre-bind epoch."""
    original_context = SyncStore.context

    def context(self):
        result = original_context(self)
        captured.set()
        assert publish.wait(5)
        return result

    SyncStore.context = context
    ready.set()
    assert begin.wait(5)
    SyncStore(path, timeout=0, initialize=False).observe_opt_out()


@pytest.mark.parametrize('observer_kind', ['thread', 'process'])
@pytest.mark.parametrize('overlap', [False, True])
@pytest.mark.parametrize('sqlite_busy', [False, True])
def test_completed_optout_fences_validation_overlapping_journal_publication(
    bound, monkeypatch, overlap, observer_kind, sqlite_busy
):
    plugin, settings, store, calls, _, _, started, release, _ = bound
    settings['sync_watch_progress'] = 'false'
    assert not plugin._observe_progress_opt_in(store)
    settings['sync_watch_progress'] = 'true'
    original_user = plugin.GronkhTVSession.current_user

    def delayed_user(self, timeout=None):
        user = original_user(self, timeout=timeout)
        started.set()
        assert release.wait(3)
        return user

    monkeypatch.setattr(plugin.GronkhTVSession, 'current_user', delayed_user)
    ctx = multiprocessing.get_context('spawn')
    captured, publish, ready, begin = (ctx.Event() for _ in range(4))
    original_context = SyncStore.context

    def context(self):
        result = original_context(self)
        if threading.current_thread().name == 'overlap-cutoff-observer':
            captured.set()
            assert publish.wait(3)
        return result

    monkeypatch.setattr(SyncStore, 'context', context)
    results, observer_results = [], []
    builder = threading.Thread(
        target=lambda: results.append(plugin._account_progress_sync(timeout_seconds=2))
    )
    if observer_kind == 'process':
        observer = ctx.Process(
            target=_overlap_cutoff_observer,
            args=(store.path, ready, begin, captured, publish),
        )
        observer.start()
        assert ready.wait(3)
    else:
        observer = threading.Thread(
            name='overlap-cutoff-observer',
            target=lambda: observer_results.append(plugin._observe_progress_opt_in(store)),
        )
    writer = None
    builder.start()
    try:
        assert started.wait(1)
        settings['sync_watch_progress'] = 'false'
        if observer_kind == 'process':
            begin.set()
        else:
            observer.start()
        assert captured.wait(1)
        if not overlap:
            publish.set()
            observer.join(1)
            assert not observer.is_alive()
        settings['sync_watch_progress'] = 'true'
        release.set()
        builder.join(2)
        assert not builder.is_alive()
        if overlap:
            if sqlite_busy:
                writer = sqlite3.connect(store.path, timeout=0)
                writer.execute('BEGIN IMMEDIATE')
            publish.set()
            observer.join(1)
        assert not observer.is_alive()
        if observer_kind == 'process':
            assert observer.exitcode == 0
        else:
            assert observer_results == [False]
        after = store.context()
        sync = results[0]
        if overlap and sqlite_busy:
            assert sync is not None
            assert not store.is_active_context('a', sync.context.epoch)
        if writer is not None:
            writer.rollback()
            writer.close()
            writer = None
        count = len(calls)
        if sync is not None:
            sync.prepare_playback(VIDEO)
        assert store.pending('a', VIDEO)['offset'] == 12
        assert after['sync_enabled'] == int(overlap and sqlite_busy)
        assert len(calls) == count
    finally:
        publish.set()
        release.set()
        if writer is not None:
            writer.rollback()
            writer.close()
        builder.join(3)
        if observer.ident is not None:
            observer.join(3)
            if observer_kind == 'process':
                if observer.is_alive():
                    observer.terminate()
                    observer.join(3)
                observer.close()


def _auth_overlap_observer_process(path, ready, begin, captured, publish):
    """Hold an observer after its read, before durable journal publication."""
    original = SyncStore.context

    def context(self):
        result = original(self)
        captured.set()
        assert publish.wait(5)
        return result

    SyncStore.context = context
    ready.set()
    assert begin.wait(5)
    SyncStore(path, timeout=0, initialize=False).observe_opt_out()


@pytest.mark.parametrize('observer_kind', ['thread', 'process'])
@pytest.mark.parametrize('busy', [False, True])
@pytest.mark.parametrize('publish_before_auth', [False, True])
@pytest.mark.parametrize('identity', ['a', 'b'])
def test_completed_optout_fences_auth_rebound_before_journal_publication(
    bound, monkeypatch, observer_kind, busy, publish_before_auth, identity
):
    """An off event fences logout/login bindings begun before its publication."""
    plugin, settings, store, calls, _, _, _, _, _ = bound
    settings['sync_watch_progress'] = 'false'
    assert not plugin._observe_progress_opt_in(store)
    disabled = store.context()
    ctx = multiprocessing.get_context('spawn')
    captured, publish, ready, begin = (ctx.Event() for _ in range(4))
    original_context = SyncStore.context

    def context(self):
        result = original_context(self)
        if threading.current_thread().name == 'auth-overlap-observer':
            captured.set()
            assert publish.wait(5)
        return result

    monkeypatch.setattr(SyncStore, 'context', context)
    observations = []
    if observer_kind == 'process':
        observer = ctx.Process(
            target=_auth_overlap_observer_process,
            args=(store.path, ready, begin, captured, publish),
        )
        observer.start()
        assert ready.wait(3)
        begin.set()
    else:
        observer = threading.Thread(
            name='auth-overlap-observer',
            target=lambda: observations.append(plugin._observe_progress_opt_in(store)),
        )
        observer.start()
    writer = None
    try:
        assert captured.wait(2)
        if publish_before_auth:
            publish.set()
            observer.join(2)
            assert not observer.is_alive()
        settings['sync_watch_progress'] = 'true'
        session = GronkhTVSession(plugin._ACCOUNT_COOKIE_FILE)

        class LoginTransport(SyntheticTransport):
            def open(self, request, timeout):
                response = super().open(request, timeout)
                if request.full_url.endswith('/auth'):
                    self.jar.set_cookie(cookie('synthetic-session', identity))
                return response

        session.opener = LoginTransport(session.cookie_jar, calls)
        monkeypatch.setattr(plugin, '_account_session', session)
        inputs = iter(['synthetic-login', 'synthetic-password'])
        monkeypatch.setattr(plugin.xbmcgui, 'Dialog', lambda: type('Dialog', (), {
            'yesno': lambda *args: True,
            'input': lambda *args, **kwargs: next(inputs),
            'notification': lambda *args: None,
        })())
        monkeypatch.setattr(plugin.xbmc, 'executebuiltin', lambda *args: None, raising=False)
        monkeypatch.setattr(plugin, 'configure_account_session', lambda *args: None)
        plugin.handle_account_logout()
        assert not Path(plugin._ACCOUNT_COOKIE_FILE).exists()
        plugin.handle_account_login()
        sync = plugin._account_progress_sync()
        assert sync is not None
        assert sync.context.account_id == identity
        assert sync.context.epoch > disabled['epoch'] + 1
        if not publish_before_auth:
            if busy:
                writer = sqlite3.connect(store.path, timeout=0)
                writer.execute('BEGIN IMMEDIATE')
            publish.set()
            observer.join(2)
        assert not observer.is_alive()
        if observer_kind == 'process':
            assert getattr(observer, 'exitcode') == 0
        else:
            assert observations == [False]
        active = store.is_active_context(identity, sync.context.epoch)
        if writer is not None:
            writer.rollback()
            writer.close()
            writer = None
        count = len(calls)
        validations = sum(call[2] == '/users/self' for call in calls)
        sync.prepare_playback(VIDEO)
        new_gets = len(calls) - count
        assert sum(call[2] == '/users/self' for call in calls) == validations
        assert store.pending('a', VIDEO)['offset'] == 12
        assert new_gets == int(publish_before_auth)
        assert active == publish_before_auth
    finally:
        publish.set()
        if writer is not None:
            writer.rollback()
            writer.close()
        observer.join(3)
        if observer_kind == 'process':
            if observer.is_alive():
                getattr(observer, 'terminate')()
                observer.join(3)
            getattr(observer, 'close')()
