import math
import threading
import time
import types

import pytest

from test_plugin_playback import plugin, RecordingListItem
from account.progress import RemoteProgress
from account.progress_sync import ProgressSync
from account.sync_store import SyncStore

VIDEO = "b0b3d510-1da2-4bcb-9d6c-9d13d23b38b1"


def render(plugin, monkeypatch, offset=0, marked=False, state="valid", account_mode=True):
    items, endings = [], []
    monkeypatch.setattr(plugin, "_account_progress_enabled", lambda: account_mode)
    result = {1105: {"state": state, "offset": offset, "marked_as_watched": marked}} if state else {}
    monkeypatch.setattr(plugin, "_listing_account_progress", lambda videos: result)
    monkeypatch.setattr(plugin, "get_videos", lambda *a, **k: ([{
        "id": VIDEO, "episode": 1105, "title": "Synthetic", "video_length": 300,
    }], ""))
    monkeypatch.setattr(plugin, "get_chapters", lambda ep: [])
    monkeypatch.setattr(plugin, "get_resume_record", lambda *a, **k: None)
    monkeypatch.setattr(plugin, "is_watched", lambda ep: False)
    monkeypatch.setattr(plugin, "is_favorite", lambda ep: False)
    monkeypatch.setattr(plugin.xbmcgui, "ListItem", RecordingListItem)
    for name in ("setPluginCategory", "setContent", "addSortMethod"):
        monkeypatch.setattr(plugin.xbmcplugin, name, lambda *a, **k: None, raising=False)
    monkeypatch.setattr(plugin.xbmcplugin, "addDirectoryItem", lambda h, u, item, f: items.append(item), raising=False)
    monkeypatch.setattr(plugin.xbmcplugin, "endOfDirectory", lambda *a, **k: endings.append((a, k)), raising=False)
    plugin.list_videos("synthetic")
    return items[0], endings


@pytest.mark.parametrize("offset,marked,expected", [
    (0, False, False), (284.99, False, False), (285, False, True),
    (300, None, True), (12, True, True),
])
def test_checkmark_from_server_flag_or_user_approved_95_percent(plugin, monkeypatch, offset, marked, expected):
    item, endings = render(plugin, monkeypatch, offset, marked)
    assert item.tag.calls["setPlaycount"] == [((int(expected),), {})]
    if expected:
        assert "setResumePoint" not in item.tag.calls
        assert not any("action=play_resume" in url for _, url in item.context_menu)
    assert endings == [((plugin._HANDLE,), {"cacheToDisc": False})]


def test_unseen_account_item_offers_explicit_mark_action_without_writing(plugin, monkeypatch):
    item, _ = render(plugin, monkeypatch, 75)
    assert ("30227", "RunPlugin(plugin://plugin.video.gronkhtv/?action=mark_watched&episode=1105)") in item.context_menu


def test_account_read_failure_is_labeled_unknown_not_claimed_unwatched(plugin, monkeypatch):
    item, _ = render(plugin, monkeypatch, state=None)
    assert "30231" in item.tag.calls["setPlot"][0][0][0]


def test_absent_server_record_is_not_a_read_failure(plugin, monkeypatch):
    item, _ = render(plugin, monkeypatch, offset=None, state="absent")
    assert "30231" not in item.tag.calls["setPlot"][0][0][0]
    assert item.tag.calls["setPlaycount"] == [((0,), {})]


def test_guest_does_not_offer_account_mark_action_or_change_cache_contract(plugin, monkeypatch):
    item, endings = render(plugin, monkeypatch, account_mode=False)
    assert not any("action=mark_watched" in url for _, url in item.context_menu)
    assert endings == [((plugin._HANDLE,), {})]


def test_listing_loads_thirteenth_item_without_fixed_cutoff_and_bounds_concurrency(plugin, monkeypatch):
    reads, active, maximum = [], [0], [0]
    lock = threading.Lock()
    class Sync:
        def prepare_playback(self, video_id):
            with lock:
                active[0] += 1
                maximum[0] = max(maximum[0], active[0])
            time.sleep(0.005)
            with lock:
                active[0] -= 1
                reads.append(video_id)
            return {"state": "valid", "offset": 300, "marked_as_watched": False}
    monkeypatch.setattr(plugin, "_account_progress_sync", lambda: Sync())
    videos = [{"id": f"synthetic-{index}", "episode": index + 1} for index in range(21)]
    result = plugin._listing_account_progress(videos)
    assert sorted(result) == list(range(1, 22))
    assert sorted(reads) == sorted(video["id"] for video in videos)
    assert maximum[0] <= 4


def test_listing_keeps_explicit_absent_separate_from_unavailable(plugin, monkeypatch):
    sync = types.SimpleNamespace(prepare_playback=lambda video_id: {"state": "absent", "offset": None})
    monkeypatch.setattr(plugin, "_account_progress_sync", lambda: sync)
    assert plugin._listing_account_progress([{"id": VIDEO, "episode": 1105}]) == {
        1105: {"state": "absent", "offset": None},
    }


@pytest.fixture
def action(plugin, monkeypatch, tmp_path):
    class API:
        error = None
        offset = 75
        def __init__(self):
            self.writes, self.reads = [], []
        def write(self, video_id, offset):
            self.writes.append((video_id, offset))
            if self.error:
                raise self.error
            self.offset = offset
        def read(self, video_id):
            self.reads.append(video_id)
            return RemoteProgress("valid", self.offset, marked_as_watched=False)
    api = API()
    store = SyncStore(tmp_path / "uploads.sqlite3")
    sync = ProgressSync(store, api, "synthetic-account")
    refreshed = []
    monkeypatch.setattr(plugin, "_account_progress_enabled", lambda: True)
    monkeypatch.setattr(plugin, "_account_progress_sync", lambda: sync)
    monkeypatch.setattr(plugin, "get_video_info", lambda ep: {"id": VIDEO, "source_length": 300})
    monkeypatch.setattr(plugin, "mark_watched", lambda *a: pytest.fail("private watched state mutated"))
    monkeypatch.setattr(plugin, "remove_record", lambda *a: pytest.fail("private resume state mutated"))
    monkeypatch.setattr(plugin.xbmc, "executebuiltin", lambda command: refreshed.append(command), raising=False)
    return plugin, api, store, sync, refreshed


def test_manual_mark_posts_end_then_reads_back_and_refreshes_without_private_state(action):
    plugin, api, store, sync, refreshed = action
    store.enqueue("synthetic-account", VIDEO, 2)
    assert plugin.handle_mark_watched({"episode": "1105"}) is True
    assert api.writes == [(VIDEO, 300)]
    assert api.reads == [VIDEO]
    assert store.pending("synthetic-account", VIDEO) is None
    assert refreshed == ["Container.Refresh"]


def test_failed_manual_mark_is_queued_but_cannot_claim_watched(action):
    plugin, api, store, _, refreshed = action
    api.error = RuntimeError("synthetic offline")
    assert plugin.handle_mark_watched({"episode": "1105"}) is False
    assert store.pending("synthetic-account", VIDEO)["offset"] == 300
    assert refreshed == []
    assert api.reads == []


def test_successful_post_without_confirmed_readback_does_not_claim_success(action):
    plugin, api, store, _, refreshed = action
    api.read = lambda video_id: RemoteProgress("valid", 75)
    assert plugin.handle_mark_watched({"episode": "1105"}) is False
    assert api.writes == [(VIDEO, 300)]
    assert store.pending("synthetic-account", VIDEO) is None
    assert refreshed == ["Container.Refresh"]


@pytest.mark.parametrize("duration", [0, -1, True, math.nan, math.inf])
def test_manual_mark_rejects_unknown_invalid_duration_without_post_or_queue(action, monkeypatch, duration):
    plugin, api, store, _, refreshed = action
    monkeypatch.setattr(plugin, "get_video_info", lambda ep: {"id": VIDEO, "source_length": duration})
    assert plugin.handle_mark_watched({"episode": "1105"}) is False
    assert api.writes == []
    assert store.pending_uploads("synthetic-account") == []
    assert refreshed == []


@pytest.mark.parametrize("authenticated", [False, True])
def test_manual_mark_requires_optin_and_fresh_validated_account(action, monkeypatch, authenticated):
    plugin, api, store, _, refreshed = action
    monkeypatch.setattr(plugin, "_account_progress_enabled", lambda: authenticated)
    if authenticated:
        monkeypatch.setattr(plugin, "_account_progress_sync", lambda: None)
    assert plugin.handle_mark_watched({"episode": "1105"}) is False
    assert api.writes == []
    assert store.pending_uploads("synthetic-account") == []


def test_manual_mark_is_routed_as_an_explicit_action(plugin, monkeypatch):
    calls = []
    monkeypatch.setattr(plugin, "handle_mark_watched", lambda params: calls.append(params), raising=False)
    plugin.router("action=mark_watched&episode=1105")
    assert calls == [{"action": "mark_watched", "episode": "1105"}]


@pytest.mark.parametrize("offset,marked", [(300, False), (75, True)])
def test_watched_account_stream_starts_without_resuming_at_the_end(plugin, monkeypatch, offset, marked):
    item = RecordingListItem()
    sync = types.SimpleNamespace(prepare_playback=lambda video_id: {
        "state": "valid", "offset": offset, "marked_as_watched": marked,
    })
    monkeypatch.setattr(plugin, "_account_progress_sync", lambda: sync)
    monkeypatch.setattr(plugin, "_account_progress_enabled", lambda: True)
    monkeypatch.setattr(plugin, "get_video_info", lambda ep: {"id": VIDEO, "source_length": 300})
    monkeypatch.setattr(plugin.xbmcgui, "ListItem", lambda **kwargs: item)
    monkeypatch.setattr(plugin.xbmc, "Monitor", lambda: types.SimpleNamespace(abortRequested=lambda: False))
    monkeypatch.setattr(plugin.xbmcplugin, "setResolvedUrl", lambda *a, **kw: None, raising=False)
    monkeypatch.setattr(plugin, "_configure_authenticated_playback", lambda *a: True)
    monkeypatch.setattr(plugin, "monitor_playback", lambda *a, **kw: None)
    plugin.play_video("https://media/synthetic", 1105)
    assert "setResumePoint" not in item.tag.calls


def test_explicit_resume_rechecks_completion_when_server_advanced_after_listing(plugin, monkeypatch):
    sync = types.SimpleNamespace(prepare_playback=lambda video_id: {
        "state": "valid", "offset": 300, "marked_as_watched": False,
    })
    monitor_calls = []
    monkeypatch.setattr(plugin, "_playback_sync_context", lambda ep: (sync, VIDEO))
    monkeypatch.setattr(plugin, "get_video_info", lambda ep: {"id": VIDEO, "source_length": 300})
    monkeypatch.setattr(plugin.xbmc, "Monitor", lambda: types.SimpleNamespace(abortRequested=lambda: False))
    monkeypatch.setattr(plugin, "get_playlist_url", lambda ep: "https://media/synthetic")
    monkeypatch.setattr(plugin, "_play_direct", lambda url: object())
    monkeypatch.setattr(plugin, "_seek_to_offset", lambda *a, **kw: pytest.fail("completed account stream resumed at its end"))
    monkeypatch.setattr(plugin, "_monitor_episode", lambda *a: monitor_calls.append(a))
    assert plugin.handle_play_resume({"episode": "1105"}) is True
    assert monitor_calls == [(1105, sync, VIDEO)]


def test_automatic_playback_end_does_not_invent_stream_end(action):
    _, api, _, sync, _ = action
    assert sync.record_position(VIDEO, 286, 300, event="end") == "synchronized"
    assert api.writes == [(VIDEO, 286)]
