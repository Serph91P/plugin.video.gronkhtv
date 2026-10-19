"""Explicit reset contract; synthetic account and Kodi boundaries only."""
import types

import pytest

from test_plugin_playback import plugin
from test_account_watched import VIDEO, render
from account.progress import RemoteProgress
from account.progress_sync import ProgressSync
from account.sync_store import SyncStore


@pytest.fixture
def reset_action(plugin, monkeypatch, tmp_path):
    class API:
        offset = 2
        error = None
        def __init__(self):
            self.writes = []
        def write(self, video_id, offset):
            self.writes.append((video_id, offset))
            if self.error:
                raise self.error
            self.offset = offset
        def read(self, video_id):
            return RemoteProgress("valid", self.offset, marked_as_watched=False)
    api = API()
    store = SyncStore(tmp_path / "uploads.sqlite3")
    sync = ProgressSync(store, api, "synthetic-account")
    monkeypatch.setattr(plugin, "_PROGRESS_SYNC_FILE", store.path)
    changes = []
    monkeypatch.setattr(plugin, "_account_progress_enabled", lambda: True)
    monkeypatch.setattr(plugin, "_account_progress_sync", lambda: sync)
    monkeypatch.setattr(plugin, "get_video_info", lambda ep: {"id": VIDEO, "source_length": 300})
    monkeypatch.setattr(plugin.xbmcgui.Dialog, "yesno", lambda *a, **k: True, raising=False)
    monkeypatch.setattr(plugin.xbmc, "Player", lambda: types.SimpleNamespace(isPlayingVideo=lambda: False))
    monkeypatch.setattr(plugin, "remove_record", lambda vfs, path: changes.append(("resume", path)) or True)
    monkeypatch.setattr(plugin, "clear_watched", lambda ep: changes.append(("watched", ep)) or True)
    monkeypatch.setattr(plugin.xbmc, "executebuiltin", lambda cmd: changes.append(("command", cmd)))
    return plugin, api, store, sync, changes


def test_confirmed_reset_posts_zero_and_clears_only_selected_private_and_failed_upload(reset_action):
    plugin, api, store, sync, changes = reset_action
    store.enqueue("synthetic-account", VIDEO, 75)
    store.enqueue("other-account", VIDEO, 99)
    store.enqueue("synthetic-account", "other-video", 88)
    assert plugin.handle_reset_resume({"episode": "1105"}) is True
    assert api.writes == [(VIDEO, 0)]
    assert store.pending("synthetic-account", VIDEO) is None
    assert store.pending("other-account", VIDEO)["offset"] == 99
    assert store.pending("synthetic-account", "other-video")["offset"] == 88
    assert changes == [("resume", plugin._resume_path(1105)), ("watched", 1105), ("command", "Container.Refresh")]
    reopened = ProgressSync(SyncStore(store.path), api, "synthetic-account")
    assert reopened.flush(VIDEO) == "synchronized"
    assert api.writes == [(VIDEO, 0)]
    assert sync.prepare_playback(VIDEO)["offset"] == 0


def test_cancelled_reset_does_not_touch_playback_account_or_private_state(reset_action, monkeypatch):
    plugin, api, store, _, changes = reset_action
    monkeypatch.setattr(plugin.xbmcgui.Dialog, "yesno", lambda *a, **k: False)
    monkeypatch.setattr(plugin.xbmc, "Player", lambda: pytest.fail("playback reached after cancellation"))
    assert plugin.handle_reset_resume({"episode": "1105"}) is False
    assert api.writes == [] and changes == []
    assert store.pending_uploads("synthetic-account") == []


def test_reset_defers_during_playback_without_stopping_it(reset_action, monkeypatch):
    plugin, api, _, _, changes = reset_action
    monkeypatch.setattr(plugin.xbmc, "Player", lambda: types.SimpleNamespace(isPlayingVideo=lambda: True))
    assert plugin.handle_reset_resume({"episode": "1105"}) is False
    assert api.writes == changes == []


@pytest.mark.parametrize("enabled", [False, True])
def test_reset_requires_optin_and_validated_session(reset_action, monkeypatch, enabled):
    plugin, api, _, _, changes = reset_action
    monkeypatch.setattr(plugin, "_account_progress_enabled", lambda: enabled)
    monkeypatch.setattr(plugin, "_account_progress_sync", lambda: None)
    assert plugin.handle_reset_resume({"episode": "1105"}) is False
    assert api.writes == changes == []


def test_offline_reset_queues_zero_without_clearing_private_state_or_claiming_success(reset_action):
    plugin, api, store, _, changes = reset_action
    store.enqueue("synthetic-account", VIDEO, 75)
    api.error = RuntimeError("synthetic offline")
    assert plugin.handle_reset_resume({"episode": "1105"}) is False
    assert changes == []
    assert store.pending("synthetic-account", VIDEO)["offset"] == 0


def test_reset_requires_zero_and_unwatched_readback_before_clearing_private_state(reset_action):
    plugin, api, _, _, changes = reset_action
    api.read = lambda video_id: RemoteProgress("valid", 0, marked_as_watched=True)
    assert plugin.handle_reset_resume({"episode": "1105"}) is False
    assert changes == []


def test_private_delete_failure_does_not_claim_complete_reset(reset_action, monkeypatch):
    plugin, api, _, _, changes = reset_action
    monkeypatch.setattr(plugin, "remove_record", lambda *args: False)
    assert plugin.handle_reset_resume({"episode": "1105"}) is False
    assert api.writes == [(VIDEO, 0)]
    assert changes == [("command", "Container.Refresh")]


@pytest.mark.parametrize("offset,marked", [(2, False), (75, False), (300, True)])
def test_reset_context_action_covers_short_progress_and_watched_items(plugin, monkeypatch, offset, marked):
    item, _ = render(plugin, monkeypatch, offset=offset, marked=marked)
    assert any("action=reset_resume&episode=1105" in url for _, url in item.context_menu)


def test_reset_route_is_nonplaying(plugin, monkeypatch):
    calls = []
    monkeypatch.setattr(plugin, "handle_reset_resume", lambda params: calls.append(params), raising=False)
    plugin.router("action=reset_resume&episode=1105")
    assert calls == [{"action": "reset_resume", "episode": "1105"}]
