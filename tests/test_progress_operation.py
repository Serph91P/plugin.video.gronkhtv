"""Kernel-owned operation locks, not durable progress state or timed leases."""
import threading

from test_plugin_playback import plugin
from test_account_watched import VIDEO
from account.progress_sync import ProgressSync
from account.sync_store import SyncStore
from test_progress_sync import API


def test_reset_defers_while_another_coordinator_is_uploading(reset_action):
    plugin, api, store, _, changes = reset_action
    store.enqueue("synthetic-account", VIDEO, 100)
    started, finish = threading.Event(), threading.Event()
    old_api = API()
    def late_write(video_id, offset):
        started.set()
        assert finish.wait(3)
    old_api.write = late_write
    old = ProgressSync(SyncStore(store.path), old_api, "synthetic-account")
    worker = threading.Thread(target=lambda: old.flush(VIDEO))
    worker.start()
    try:
        assert started.wait(2)
        assert plugin.handle_reset_resume({"episode": "1105"}) is False
        assert api.writes == changes == []
    finally:
        finish.set()
        worker.join(3)
    assert not worker.is_alive()


def test_reset_defers_until_stopped_monitor_finishes_its_final_upload(reset_action, monkeypatch):
    plugin, api, _, sync, changes = reset_action
    started, finish = threading.Event(), threading.Event()
    playing = [True]
    class Player:
        def isPlayingVideo(self): return playing[0]
        def getTime(self): return 100
        def getTotalTime(self): return 300
    class Monitor:
        def abortRequested(self): return False
        def waitForAbort(self, timeout):
            playing[0] = False
            started.set()
            assert finish.wait(3)
            return False
    monkeypatch.setattr(plugin.xbmc, "Player", Player)
    monkeypatch.setattr(plugin.xbmc, "Monitor", Monitor)
    worker = threading.Thread(target=lambda: plugin.monitor_playback(1105, progress_sync=sync, video_id=VIDEO))
    worker.start()
    try:
        assert started.wait(2)
        assert plugin.handle_reset_resume({"episode": "1105"}) is False
        assert api.writes == changes == []
    finally:
        finish.set()
        worker.join(3)
    assert not worker.is_alive()


# Import the shared fixture explicitly; no live account or Kodi is involved.
from test_account_reset import reset_action


def test_monitor_hands_off_on_media_change_without_sampling_new_episode(reset_action, monkeypatch):
    plugin, api, _, sync, _ = reset_action
    path = ["synthetic-old.m3u8"]
    class Player:
        def isPlayingVideo(self): return True
        def getPlayingFile(self): return path[0]
        def getTime(self): return 100 if path[0] == "synthetic-old.m3u8" else 200
        def getTotalTime(self): return 300
    class Monitor:
        checks = 0
        def abortRequested(self):
            self.checks += 1
            return self.checks > 2
        def waitForAbort(self, timeout):
            path[0] = "synthetic-new.m3u8"
            return False
    monkeypatch.setattr(plugin.xbmc, "Player", Player)
    monkeypatch.setattr(plugin.xbmc, "Monitor", Monitor)
    plugin.monitor_playback(1105, progress_sync=sync, video_id=VIDEO)
    assert api.writes == [(VIDEO, 100)]


def test_kernel_lock_reentry_cross_process_exclusion_and_crash_release(tmp_path):
    import os
    import subprocess
    import sys
    from account.operation_lock import progress_operation
    database = str(tmp_path / "queue.sqlite3")
    code = "from account.operation_lock import progress_operation; import os,sys; "
    code += "exec('with progress_operation(sys.argv[1]) as ok:\\n print(ok, flush=True)\\n os._exit(0)')"
    env = dict(os.environ, PYTHONPATH=str(__import__("pathlib").Path(__file__).resolve().parents[1] / "resources/lib"))
    with progress_operation(database) as acquired:
        assert acquired
        with progress_operation(database) as nested:
            assert nested
        child = subprocess.run([sys.executable, "-c", code, database], capture_output=True, text=True, env=env, timeout=5)
        assert child.returncode == 0, child.stderr
        assert child.stdout.strip() == "False"
    # The child exits without context-manager cleanup; the OS releases the lock.
    child = subprocess.run([sys.executable, "-c", code, database], capture_output=True, text=True, env=env, timeout=5)
    assert child.returncode == 0, child.stderr
    assert child.stdout.strip() == "True"
    with progress_operation(database) as acquired:
        assert acquired
