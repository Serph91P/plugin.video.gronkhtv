"""Regression coverage for reset guards across separate playback invokers."""
import importlib.util
import os
import sys
import threading
from pathlib import Path

import pytest

from test_plugin_playback import RecordingListItem, plugin as plugin_fixture

plugin = plugin_fixture


VIDEO = "00000000-0000-4000-8000-000000000001"
OTHER_VIDEO = "00000000-0000-4000-8000-000000000002"


@pytest.fixture
def local_listing(plugin, monkeypatch):
    class VFS:
        @staticmethod
        def exists(path):
            return Path(path).exists()

        @staticmethod
        def mkdirs(path):
            Path(path).mkdir(parents=True, exist_ok=True)
            return True

        @staticmethod
        def File(path, mode="r"):
            return open(path, mode, encoding="utf-8")

        @staticmethod
        def delete(path):
            Path(path).unlink()
            return True

        @staticmethod
        def rename(source, destination):
            os.replace(source, destination)
            return True

    for attribute in ("exists", "mkdirs", "File", "delete", "rename"):
        monkeypatch.setattr(plugin.xbmcvfs, attribute, getattr(VFS, attribute), raising=False)
    resume_path = plugin._resume_path(1105)
    plugin.write_record(plugin.xbmcvfs, str(Path(resume_path).parent), resume_path, 2, 300)
    video = {"id": VIDEO, "episode": 1105, "title": "Synthetic", "video_length": 300, "tags": []}
    remote = {"offset": 2.0, "writes": []}

    class Session:
        def current_user(self, timeout=None):
            return {"id": "synthetic-account", "displayname": "Synthetic"}

        def request_json(self, path, method="GET", payload=None, timeout=None):
            if method == "GET" and path == "/videos/" + VIDEO:
                return {"data": {"watch_progress": {"offset": remote["offset"], "updated_at": "synthetic", "marked_as_watched": False}}}
            if method == "POST" and path == "/watched/progress/" + VIDEO:
                remote["writes"].append(payload["offset"])
                remote["offset"] = payload["offset"]
                return {}
            pytest.fail("unexpected synthetic API operation")

    settings = {"sync_watch_progress": "true"}
    cookie = Path(plugin._ACCOUNT_COOKIE_FILE)
    cookie.parent.mkdir(parents=True, exist_ok=True)
    cookie.write_text("synthetic-only", encoding="utf-8")
    monkeypatch.setattr(plugin, "_account_session", Session())
    monkeypatch.setattr(plugin, "GronkhTVSession", lambda *args, **kwargs: Session())
    monkeypatch.setattr(plugin._addon, "getSetting", lambda key: settings.get(key, ""))
    monkeypatch.setattr(plugin._addon, "setSetting", settings.__setitem__)
    monkeypatch.setattr(plugin.xbmcgui.Dialog, "yesno", lambda *args, **kwargs: True, raising=False)
    monkeypatch.setattr(plugin.xbmc, "getCondVisibility", lambda *args: False, raising=False)
    monkeypatch.setattr(plugin, "get_video_info", lambda *args, **kwargs: video)
    monkeypatch.setattr(plugin, "get_videos", lambda *args, **kwargs: ([video], ""))
    monkeypatch.setattr(plugin, "get_chapters", lambda *args: [])
    monkeypatch.setattr(plugin, "is_favorite", lambda *args: False)
    monkeypatch.setattr(plugin, "is_watched", lambda *args: False)
    monkeypatch.setattr(plugin, "_listing_account_progress", lambda *args, **kwargs: {})
    monkeypatch.setattr(plugin.xbmcgui, "ListItem", RecordingListItem)
    for attribute in ("setPluginCategory", "setContent", "addSortMethod", "endOfDirectory"):
        monkeypatch.setattr(plugin.xbmcplugin, attribute, lambda *args: None, raising=False)
    monkeypatch.setattr(plugin.xbmcplugin, "addDirectoryItem", lambda handle, url, item, folder: None, raising=False)
    monkeypatch.setattr(plugin.xbmc, "executebuiltin", lambda *args, **kwargs: None, raising=False)
    return plugin, remote, settings


def separate_invoker(plugin, monkeypatch, name):
    source = Path(plugin.__file__)
    spec = importlib.util.spec_from_file_location(name, source)
    other = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, other)
    spec.loader.exec_module(other)
    for binding in ("_addon", "_account_session", "GronkhTVSession", "get_video_info"):
        monkeypatch.setattr(other, binding, getattr(plugin, binding))
    assert other._resume_path(1105) == plugin._resume_path(1105)
    return other


def test_failed_guard_creation_prevents_outgoing_monitor_from_restoring_reset(local_listing, monkeypatch):
    plugin, remote, settings = local_listing
    reset = separate_invoker(plugin, monkeypatch, "reset_after_guard_failure")
    state = {"playing": True}
    real_file = plugin.xbmcvfs.File

    def fail_writer_file(path, mode="r"):
        if ".writer" in str(path) and mode == "w":
            raise OSError("synthetic guard failure")
        return real_file(path, mode)

    class Player:
        def isPlayingVideo(self): return state["playing"]
        def isPlaying(self): return state["playing"]
        def getTime(self): return 2
        def getTotalTime(self): return 300
        def getPlaySpeed(self): return 1
        def play(self, *args, **kwargs): pytest.fail("unexpected playback control")
        def stop(self, *args, **kwargs): pytest.fail("unexpected playback control")
        def seekTime(self, *args, **kwargs): pytest.fail("unexpected playback control")

    class Monitor:
        def abortRequested(self): return False
        def waitForAbort(self, timeout):
            state["playing"] = False
            settings["sync_watch_progress"] = "true"
            assert reset.handle_reset_resume({"episode": "1105"}) is True
            return True

    settings["sync_watch_progress"] = "false"
    monkeypatch.setattr(plugin.xbmcvfs, "File", fail_writer_file)
    monkeypatch.setattr(plugin.xbmc, "Player", Player)
    monkeypatch.setattr(plugin.xbmc, "Monitor", Monitor)
    plugin.monitor_playback(1105, progress_sync=None, video_id=VIDEO)

    record = plugin.get_resume_record(1105, 300)
    assert record is None or record["position"] == 0, (record, remote)


def test_refused_claim_releases_guard_and_allows_later_reset(local_listing, monkeypatch):
    plugin, remote, _settings = local_listing
    sync = plugin._account_progress_sync()
    blocker = sync.begin_playback_writer(OTHER_VIDEO)

    class Player:
        def isPlayingVideo(self): return True

    monkeypatch.setattr(plugin.xbmc, "Player", Player)
    monkeypatch.setattr(plugin.xbmc, "Monitor", lambda: object())
    plugin.monitor_playback(1105, progress_sync=sync, video_id=VIDEO)
    assert sync.finish_playback_writer(OTHER_VIDEO, blocker)

    class StoppedPlayer:
        def isPlayingVideo(self): return False

    monkeypatch.setattr(plugin.xbmc, "Player", StoppedPlayer)
    assert plugin.handle_reset_resume({"episode": "1105"}) is True
    assert remote["writes"] == [0]


def test_finishing_one_monitor_keeps_another_monitors_guard(local_listing, monkeypatch):
    plugin, remote, settings = local_listing
    other = separate_invoker(plugin, monkeypatch, "overlapping_monitor")
    reset = separate_invoker(plugin, monkeypatch, "overlapping_reset")
    settings["sync_watch_progress"] = "false"
    playing = {"main": True, "other": True}
    sampled = threading.Event()
    release_other = threading.Event()

    def identity():
        return "other" if threading.current_thread().name == "other-monitor" else "main"

    class Player:
        def isPlayingVideo(self): return playing[identity()]
        def getTime(self): return 2
        def getTotalTime(self): return 300
        def getPlaySpeed(self): return 1
        def getSpeed(self): return 1
        def play(self, *args, **kwargs): pytest.fail("unexpected playback control")
        def stop(self, *args, **kwargs): pytest.fail("unexpected playback control")
        def seekTime(self, *args, **kwargs): pytest.fail("unexpected playback control")

    errors = []
    def run_other():
        try:
            other.monitor_playback(1105, progress_sync=None, video_id=VIDEO)
        except BaseException as error:
            errors.append(error)

    thread = threading.Thread(target=run_other, name="other-monitor")

    class Monitor:
        def abortRequested(self): return False
        def waitForAbort(self, timeout):
            if identity() == "other":
                playing["other"] = False
                sampled.set()
                assert release_other.wait(5)
            else:
                thread.start()
                assert sampled.wait(5)
                playing["main"] = False
            return True

    monkeypatch.setattr(plugin.xbmc, "Player", Player)
    monkeypatch.setattr(plugin.xbmc, "Monitor", Monitor)
    try:
        plugin.monitor_playback(1105, progress_sync=None, video_id=VIDEO)
        settings["sync_watch_progress"] = "true"
        assert reset.handle_reset_resume({"episode": "1105"}) is False
    finally:
        release_other.set()
        thread.join(5)
    assert not errors
    assert remote["writes"] == []
