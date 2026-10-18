"""Regression coverage for server authority at the actual Kodi route boundary."""

import types

import pytest

from test_plugin_playback import plugin, RecordingListItem, PlaybackMonitor, PlaybackPlayer


def listing(plugin, monkeypatch, remote):
    items = []
    video = {"id": "backend-id", "episode": 1105, "title": "Synthetic stream",
             "video_length": 300, "tags": []}
    monkeypatch.setattr(plugin, "_account_progress_enabled", lambda: True, raising=False)
    monkeypatch.setattr(plugin, "_listing_account_progress", lambda videos: remote)
    monkeypatch.setattr(plugin, "get_videos", lambda *a, **kw: ([video], ""))
    monkeypatch.setattr(plugin, "get_chapters", lambda ep: [])
    monkeypatch.setattr(plugin, "get_resume_record", lambda *a, **kw: {"position": 2, "duration": 300})
    monkeypatch.setattr(plugin, "is_watched", lambda ep: True)
    monkeypatch.setattr(plugin, "is_favorite", lambda ep: False)
    monkeypatch.setattr(plugin.xbmcgui, "ListItem", RecordingListItem)
    monkeypatch.setattr(plugin.xbmcplugin, "addDirectoryItem", lambda h, u, item, f: items.append(item), raising=False)
    for name in ("setPluginCategory", "setContent", "addSortMethod", "endOfDirectory"):
        monkeypatch.setattr(plugin.xbmcplugin, name, lambda *a: None, raising=False)
    plugin.list_videos("category")
    return items[0]


@pytest.mark.parametrize("watched", [True, False])
def test_account_listing_uses_server_resume_and_watched_not_private_files(plugin, monkeypatch, watched):
    item = listing(plugin, monkeypatch, {1105: {"state": "valid", "offset": 75, "marked_as_watched": watched}})
    assert item.tag.calls["setResumePoint"] == [((75, 300), {})]
    assert item.tag.calls["setPlaycount"] == [((int(watched),), {})]


@pytest.mark.parametrize("remote", [{}, {1105: {"state": "valid", "offset": 0, "marked_as_watched": False}}])
def test_unknown_absent_or_zero_server_value_never_exposes_local_progress(plugin, monkeypatch, remote):
    item = listing(plugin, monkeypatch, remote)
    assert "setResumePoint" not in item.tag.calls
    assert item.tag.calls["setPlaycount"] == [((0,), {})]
    assert "Fortschritt:" not in item.tag.calls["setPlot"][0][0][0]


def test_resume_context_action_ignores_existing_private_offset(plugin, monkeypatch):
    sync = object()
    calls = []
    monkeypatch.setattr(plugin, "get_resume_record", lambda *a, **kw: {"position": 2, "duration": 300})
    monkeypatch.setattr(plugin, "_playback_sync_context", lambda ep: (sync, "backend-id"))
    monkeypatch.setattr(plugin, "_account_resume_offset", lambda *a: 75)
    monkeypatch.setattr(plugin, "get_playlist_url", lambda ep: "https://media/playlist")
    monkeypatch.setattr(plugin, "_play_direct", lambda url: object())
    monkeypatch.setattr(plugin, "_seek_to_offset", lambda player, offset, **kw: calls.append(offset) or True)
    monkeypatch.setattr(plugin, "_monitor_episode", lambda *a: None)
    assert plugin.handle_play_resume({"episode": "1105"}) is True
    assert calls == [75]


def test_resume_get_failure_never_falls_back_to_local_offset(plugin, monkeypatch):
    monkeypatch.setattr(plugin, "_account_progress_enabled", lambda: True, raising=False)
    monkeypatch.setattr(plugin, "get_resume_record", lambda *a, **kw: {"position": 75, "duration": 300})
    monkeypatch.setattr(plugin, "_playback_sync_context", lambda ep: (None, None))
    monkeypatch.setattr(plugin, "_report_seek_failure", lambda message: None)
    monkeypatch.setattr(plugin, "_play_direct", lambda *a: pytest.fail("private fallback started playback"))
    assert plugin.handle_play_resume({"episode": "1105"}) is False


def test_bounded_resume_failure_does_not_restore_private_offset(plugin, monkeypatch):
    monkeypatch.setattr(plugin.xbmc, "Monitor", lambda: types.SimpleNamespace(abortRequested=lambda: False))
    sync = types.SimpleNamespace(prepare_playback=lambda video_id: {"state": "unavailable", "offset": None})
    assert plugin._account_resume_offset(sync, "backend-id", 75) == 0


def test_session_is_validated_without_sqlite_auth_context(plugin, monkeypatch, tmp_path):
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("synthetic-cookie")
    monkeypatch.setattr(plugin, "_ACCOUNT_COOKIE_FILE", str(cookies))
    monkeypatch.setattr(plugin, "_PROGRESS_SYNC_FILE", str(tmp_path / "sync.sqlite3"))
    monkeypatch.setattr(plugin, "_setting_enabled", lambda *a: True)
    calls = []
    class Session:
        def __init__(self, path):
            pass
        def current_user(self, timeout=None):
            calls.append(timeout)
            return {"id": "account-a"}
    monkeypatch.setattr(plugin, "GronkhTVSession", Session)
    sync = plugin._account_progress_sync()
    assert sync is not None
    assert sync._account_id == "account-a"
    assert calls == [2]
    cookies.unlink()
    assert sync.prepare_playback("backend-id")["state"] == "authentication_required"


def test_session_change_during_identity_validation_cannot_bind_wrong_account(plugin, monkeypatch, tmp_path):
    cookies = tmp_path / "cookies.txt"
    cookies.write_text("synthetic-cookie")
    monkeypatch.setattr(plugin, "_ACCOUNT_COOKIE_FILE", str(cookies))
    monkeypatch.setattr(plugin, "_PROGRESS_SYNC_FILE", str(tmp_path / "sync.sqlite3"))
    monkeypatch.setattr(plugin, "_setting_enabled", lambda *a: True)
    class Session:
        def __init__(self, path):
            pass
        def current_user(self, timeout=None):
            cookies.unlink()
            return {"id": "account-a"}
    monkeypatch.setattr(plugin, "GronkhTVSession", Session)
    assert plugin._account_progress_sync() is None


def test_account_restart_is_playback_only_and_does_not_reset_private_or_server_state(plugin, monkeypatch):
    calls = []
    sync = object()
    monkeypatch.setattr(plugin, "_account_progress_enabled", lambda: True)
    monkeypatch.setattr(plugin, "_playback_sync_context", lambda ep: (sync, "backend-id"))
    monkeypatch.setattr(plugin, "remove_record", lambda *a: pytest.fail("account restart removed private state"))
    monkeypatch.setattr(plugin, "clear_watched", lambda *a: pytest.fail("account restart reset local watched"))
    monkeypatch.setattr(plugin, "get_playlist_url", lambda ep: "https://media/playlist")
    monkeypatch.setattr(plugin, "_play_direct", lambda url: calls.append(("play", url)))
    monkeypatch.setattr(plugin, "_monitor_episode", lambda *a: calls.append(("monitor", a)))
    plugin.handle_play_from_start({"episode": "1105"})
    assert calls == [("play", "https://media/playlist"), ("monitor", (1105, sync, "backend-id"))]


def test_pause_posts_once_and_stop_waits_for_latest_position_without_private_save(plugin, monkeypatch):
    monitor = PlaybackMonitor(5)
    player = PlaybackPlayer(monitor, [(10, 300), (11, 300), (11, 300), (11, 300), (12, 300)])
    player.getSpeed = lambda: 0 if monitor.index in (1, 2, 3) else 1
    posts = []
    class Sync:
        def record_position(self, video_id, offset, duration, event):
            posts.append((video_id, offset, event))
            return "synchronized"
        def flush_pending(self, exclude_video_id=None):
            posts.append(("retry", exclude_video_id))
    monkeypatch.setattr(plugin.xbmc, "Player", lambda: player)
    monkeypatch.setattr(plugin.xbmc, "Monitor", lambda: monitor)
    monkeypatch.setattr(plugin.time, "monotonic", lambda: monitor.now)
    monkeypatch.setattr(plugin, "save_resume_point", lambda *a: pytest.fail("account data persisted as private resume"))
    plugin.monitor_playback(1105, progress_sync=Sync(), video_id="backend-id")
    assert posts == [("backend-id", 11, "pause"), ("backend-id", 12, "stop"), ("retry", "backend-id")]
