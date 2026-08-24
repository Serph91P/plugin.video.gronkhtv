import importlib.util
import sys
import types
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "resources" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))


class FakeAddon:
    def getAddonInfo(self, key):
        return {"name": "Gronkh.tv", "version": "test"}.get(key, "")

    def getLocalizedString(self, string_id):
        return str(string_id)

    def getSetting(self, setting_id):
        return ""

    def setSetting(self, setting_id, value):
        pass


class FakeDialog:
    notifications = []

    def notification(self, heading, message, icon):
        self.notifications.append((heading, message, icon))


@pytest.fixture
def plugin(monkeypatch, tmp_path):
    FakeDialog.notifications = []
    xbmc = types.ModuleType("xbmc")
    xbmc.LOGINFO = 1
    xbmc.LOGWARNING = 2
    xbmc.LOGERROR = 3
    xbmc.log = lambda *args, **kwargs: None
    xbmc.sleep = lambda milliseconds: None
    xbmc.Player = lambda: pytest.fail("player reached before route validation")
    xbmc.Monitor = lambda: pytest.fail("monitor reached before route validation")

    xbmcaddon = types.ModuleType("xbmcaddon")
    xbmcaddon.Addon = lambda **kwargs: FakeAddon()

    xbmcgui = types.ModuleType("xbmcgui")
    xbmcgui.NOTIFICATION_INFO = 1
    xbmcgui.NOTIFICATION_WARNING = 2
    xbmcgui.NOTIFICATION_ERROR = 3
    xbmcgui.INPUT_ALPHANUM = 0
    xbmcgui.ALPHANUM_HIDE_INPUT = 1
    xbmcgui.Dialog = FakeDialog
    xbmcgui.ListItem = object

    xbmcplugin = types.ModuleType("xbmcplugin")
    xbmcplugin.SORT_METHOD_NONE = 0
    xbmcplugin.SORT_METHOD_DATEADDED = 1

    xbmcvfs = types.ModuleType("xbmcvfs")
    xbmcvfs.translatePath = lambda path: str(tmp_path / path.rsplit("/", 1)[-1])
    xbmcvfs.exists = lambda path: False

    inputstreamhelper = types.ModuleType("inputstreamhelper")
    inputstreamhelper.Helper = object

    for name, module in {
        "xbmc": xbmc,
        "xbmcaddon": xbmcaddon,
        "xbmcgui": xbmcgui,
        "xbmcplugin": xbmcplugin,
        "xbmcvfs": xbmcvfs,
        "inputstreamhelper": inputstreamhelper,
    }.items():
        monkeypatch.setitem(sys.modules, name, module)

    monkeypatch.setattr(sys, "argv", ["plugin://plugin.video.gronkhtv/", "1", ""])
    module_name = "plugin_under_test"
    spec = importlib.util.spec_from_file_location(module_name, LIB / "plugin.py")
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    "handler,parameter",
    [
        ("handle_play", "video"),
        ("handle_play_resume", "episode"),
        ("handle_play_from_start", "episode"),
        ("handle_show_details", "episode"),
        ("handle_add_favorite", "episode"),
        ("handle_remove_favorite", "episode"),
        ("jump_to_chapter", "episode"),
    ],
)
@pytest.mark.parametrize("value", ["", "0", "-1", "1.5", "abc", "1/playlist"])
def test_episode_routes_reject_non_positive_integers(plugin, handler, parameter, value):
    params = {parameter: value}
    if handler == "jump_to_chapter":
        params["offset"] = "10"

    with pytest.raises(ValueError, match="positive integer"):
        getattr(plugin, handler)(params)


@pytest.mark.parametrize(
    ("offset", "expected"),
    [("0", 0.0), ("1.5", 1.5), ("31536000", 31536000.0)],
)
def test_chapter_offset_validator_accepts_inclusive_finite_range(
    plugin, offset, expected
):
    assert plugin._non_negative_finite(offset, "offset") == expected


@pytest.mark.parametrize(
    "offset", ["-1", "nan", "inf", "-inf", "1e308", "1e309", "31536000.1"]
)
def test_chapter_offset_validator_rejects_out_of_range_values(plugin, offset):
    with pytest.raises(ValueError, match="finite non-negative"):
        plugin._non_negative_finite(offset, "offset")


@pytest.mark.parametrize(
    "offset",
    ["", "-1", "nan", "inf", "-inf", "abc", "1e308", "1e309", "31536000.1"],
)
def test_chapter_route_rejects_invalid_offset_before_all_route_sinks(
    plugin, monkeypatch, offset
):
    sinks = []
    player = object()
    monkeypatch.setattr(plugin.xbmc, "Player", lambda: sinks.append("player") or player)
    monkeypatch.setattr(
        plugin,
        "get_playlist_url",
        lambda episode: sinks.append("playlist") or "https://media/playlist",
    )
    monkeypatch.setattr(
        plugin,
        "_play_direct",
        lambda url, actual_player: sinks.append("play") or actual_player,
    )
    monkeypatch.setattr(
        plugin,
        "_seek_to_offset",
        lambda *args, **kwargs: sinks.append("seek") or True,
    )
    monkeypatch.setattr(
        plugin, "monitor_playback", lambda episode: sinks.append("monitor")
    )

    with pytest.raises(ValueError, match="finite non-negative"):
        plugin.jump_to_chapter({"episode": "1105", "offset": offset})

    assert sinks == []


@pytest.mark.parametrize(
    ("offset", "expected"),
    [("0", 0.0), ("1.5", 1.5), ("120", 120.0), ("31536000", 31536000.0)],
)
def test_chapter_route_accepts_offsets_through_inclusive_maximum(
    plugin, monkeypatch, offset, expected
):
    sinks = []
    player = object()
    monkeypatch.setattr(plugin.xbmc, "Player", lambda: sinks.append("player") or player)
    monkeypatch.setattr(
        plugin,
        "get_playlist_url",
        lambda episode: sinks.append(("playlist", episode)) or "https://media/playlist",
    )
    monkeypatch.setattr(
        plugin,
        "_play_direct",
        lambda url, actual_player: sinks.append(("play", url, actual_player))
        or actual_player,
    )
    monkeypatch.setattr(
        plugin,
        "_seek_to_offset",
        lambda actual_player, actual_offset, **kwargs: sinks.append(
            ("seek", actual_player, actual_offset)
        )
        or True,
    )
    monkeypatch.setattr(
        plugin,
        "monitor_playback",
        lambda episode: sinks.append(("monitor", episode)),
    )

    assert plugin.jump_to_chapter({"episode": "1105", "offset": offset}) is True

    assert sinks == [
        "player",
        ("playlist", 1105),
        ("play", "https://media/playlist", player),
        ("seek", player, expected),
        ("monitor", 1105),
    ]


def test_chapter_values_use_title_category_safe_fallback_and_v3_offset(plugin):
    assert plugin._chapter_values(
        {
            "title": None,
            "category": {"title": "Tabletop RPGs"},
            "start_offset": 900,
            "offset": 12,
        }
    ) == ("Tabletop RPGs", 900.0)
    assert plugin._chapter_values(
        {"title": "", "category": {"title": ""}, "offset": "42"}
    ) == ("Unbenanntes Kapitel", 42.0)


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now

    def sleep(self, milliseconds):
        self.now += milliseconds / 1000


class FakeSeekPlayer:
    def __init__(
        self,
        *,
        ready_after=0,
        seek_succeeds=True,
        playing_files=None,
    ):
        self.ready_after = ready_after
        self.seek_succeeds = seek_succeeds
        self.playing_files = list(playing_files or ["https://media/playlist"])
        self.reads = 0
        self.position = 0.0
        self.seek_calls = []
        self.stopped = False

    def isPlayingVideo(self):
        return True

    def getTotalTime(self):
        self.reads += 1
        return 0.0 if self.reads <= self.ready_after else 300.0

    def getTime(self):
        return self.position

    def getPlayingFile(self):
        if len(self.playing_files) > 1:
            return self.playing_files.pop(0)
        return self.playing_files[0]

    def seekTime(self, offset):
        self.seek_calls.append(offset)
        if self.seek_succeeds:
            self.position = offset

    def stop(self):
        self.stopped = True


def _install_clock(plugin, monkeypatch):
    clock = FakeClock()
    monkeypatch.setattr(plugin.time, "monotonic", clock.monotonic)
    monkeypatch.setattr(plugin.xbmc, "sleep", clock.sleep)
    return clock


def test_verified_seek_waits_until_player_is_seekable(plugin, monkeypatch):
    clock = _install_clock(plugin, monkeypatch)
    player = FakeSeekPlayer(ready_after=3)

    assert plugin._seek_to_offset(player, 120, timeout=2, tolerance=2) is True

    assert player.seek_calls == [120.0]
    assert clock.now >= 0.3


def test_verified_seek_waits_for_requested_file_to_replace_previous_video(
    plugin, monkeypatch
):
    clock = _install_clock(plugin, monkeypatch)
    player = FakeSeekPlayer(
        playing_files=[
            "https://media/old-playlist",
            "https://media/old-playlist",
            "https://media/new-playlist",
        ]
    )

    assert plugin._seek_to_offset(
        player,
        120,
        expected_path="https://media/new-playlist",
        timeout=2,
    ) is True
    assert player.seek_calls == [120.0]
    assert clock.now >= 0.2


def test_verified_seek_performs_only_one_bounded_retry(plugin, monkeypatch):
    _install_clock(plugin, monkeypatch)
    player = FakeSeekPlayer(seek_succeeds=False)

    assert plugin._seek_to_offset(player, 120, timeout=1, tolerance=2) is False

    assert player.seek_calls == [120.0, 120.0]


def test_verified_seek_rejects_offset_beyond_known_duration_before_seek(plugin, monkeypatch):
    _install_clock(plugin, monkeypatch)
    player = FakeSeekPlayer()

    assert plugin._seek_to_offset(player, 301, timeout=1, tolerance=2) is False

    assert player.seek_calls == []


def test_jump_to_chapter_reports_error_when_seek_cannot_be_verified(plugin, monkeypatch):
    _install_clock(plugin, monkeypatch)
    player = FakeSeekPlayer(seek_succeeds=False)
    monkeypatch.setattr(plugin.xbmc, "Player", lambda: player)
    monkeypatch.setattr(
        plugin, "get_playlist_url", lambda episode: "https://media/playlist"
    )
    monkeypatch.setattr(plugin, "_play_direct", lambda url, actual_player: actual_player)

    assert plugin.jump_to_chapter({"episode": "1105", "offset": "120"}) is False

    assert player.seek_calls == [120.0, 120.0]
    assert FakeDialog.notifications[-1][1] == "Kapitelsprung fehlgeschlagen"
    assert FakeDialog.notifications[-1][2] == plugin.xbmcgui.NOTIFICATION_ERROR
    assert player.stopped is True


def test_resume_playback_uses_same_verified_seek_lifecycle(plugin, monkeypatch):
    player = FakeSeekPlayer()
    calls = []
    monitors = []
    monkeypatch.setattr(
        plugin,
        "get_resume_record",
        lambda episode: {"position": 75.0, "duration": 300.0},
        raising=False,
    )
    monkeypatch.setattr(plugin, "get_playlist_url", lambda episode: "https://media/playlist")
    monkeypatch.setattr(plugin, "_play_direct", lambda url: player)
    monkeypatch.setattr(
        plugin,
        "_seek_to_offset",
        lambda actual_player, offset, **kwargs: calls.append((actual_player, offset)) or True,
    )
    monkeypatch.setattr(
        plugin, "monitor_playback", lambda episode: monitors.append(episode)
    )

    assert plugin.handle_play_resume({"episode": "1105"}) is True

    assert calls == [(player, 75.0)]
    assert monitors == [1105]


def test_chapter_jump_starts_selected_episode_and_monitors_after_seek(
    plugin, monkeypatch
):
    player = FakeSeekPlayer()
    plays = []
    monitors = []
    monkeypatch.setattr(plugin.xbmc, "Player", lambda: player)
    monkeypatch.setattr(
        plugin, "get_playlist_url", lambda episode: f"https://media/{episode}/playlist"
    )
    monkeypatch.setattr(
        plugin,
        "_play_direct",
        lambda url, actual_player: plays.append((url, actual_player)) or actual_player,
    )
    monkeypatch.setattr(plugin, "_seek_to_offset", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        plugin, "monitor_playback", lambda episode: monitors.append(episode)
    )

    assert plugin.jump_to_chapter({"episode": "1105", "offset": "120"}) is True
    assert plays == [("https://media/1105/playlist", player)]
    assert monitors == [1105]


class RecordingTag:
    def __getattr__(self, name):
        return lambda *args, **kwargs: None


class RecordingListItem:
    def __init__(self, label=None, path=None):
        self.context_menu = []
        self.tag = RecordingTag()

    def setArt(self, art):
        pass

    def addContextMenuItems(self, items):
        self.context_menu.extend(items)

    def getVideoInfoTag(self):
        return self.tag

    def setProperty(self, key, value):
        pass


def test_context_menu_preserves_resume_chapter_action_order_and_urls(
    plugin, monkeypatch
):
    items = []
    video = {
        "episode": 1105,
        "title": "Episode",
        "video_length": 300,
        "tags": [],
    }
    monkeypatch.setattr(plugin.xbmcgui, "ListItem", RecordingListItem)
    monkeypatch.setattr(plugin, "get_videos", lambda *args, **kwargs: ([video], ""))
    monkeypatch.setattr(
        plugin,
        "get_chapters",
        lambda episode: [
            {"title": None, "category": {"title": "Game"}, "start_offset": 90}
        ],
    )
    monkeypatch.setattr(
        plugin,
        "get_resume_record",
        lambda episode, legacy_duration=0: {"position": 75.0, "duration": 300.0},
    )
    monkeypatch.setattr(plugin, "is_favorite", lambda episode: False)
    monkeypatch.setattr(
        plugin.xbmcplugin,
        "addDirectoryItem",
        lambda handle, url, list_item, is_folder: items.append(list_item),
        raising=False,
    )
    for name in ("setPluginCategory", "setContent", "addSortMethod", "endOfDirectory"):
        monkeypatch.setattr(plugin.xbmcplugin, name, lambda *args: None, raising=False)

    plugin.list_videos("category")

    assert items[0].context_menu == [
        (
            "Fortsetzen bei 0:01:15",
            "RunPlugin(plugin://plugin.video.gronkhtv/?action=play_resume&episode=1105)",
        ),
        (
            "Von Anfang starten",
            "RunPlugin(plugin://plugin.video.gronkhtv/?action=play_from_start&episode=1105)",
        ),
        (
            "Kapitel [0:01:30]: Game",
            "RunPlugin(plugin://plugin.video.gronkhtv/?action=jump_to_chapter&episode=1105&offset=90)",
        ),
        (
            "Zu Favoriten hinzufügen",
            items[0].context_menu[3][1],
        ),
        (
            "Stream-Details anzeigen",
            "RunPlugin(plugin://plugin.video.gronkhtv/?action=show_details&episode=1105)",
        ),
    ]
    assert "action=add_favorite&episode=1105&video_data=" in items[0].context_menu[3][1]


class PlaybackMonitor:
    def __init__(self, sample_count, abort_after=None):
        self.index = 0
        self.sample_count = sample_count
        self.abort_after = abort_after
        self.now = 0.0

    def abortRequested(self):
        return self.abort_after is not None and self.index >= self.abort_after

    def waitForAbort(self, seconds):
        self.now += seconds
        self.index += 1
        return self.abortRequested()


class PlaybackPlayer:
    def __init__(self, monitor, samples):
        self.monitor = monitor
        self.samples = samples

    def isPlayingVideo(self):
        return self.monitor.index < len(self.samples)

    def getTime(self):
        position = self.samples[self.monitor.index][0]
        if isinstance(position, Exception):
            raise position
        return position

    def getTotalTime(self):
        duration = self.samples[self.monitor.index][1]
        if isinstance(duration, Exception):
            raise duration
        return duration


def _run_monitor(plugin, monkeypatch, samples, abort_after=None):
    monitor = PlaybackMonitor(len(samples), abort_after=abort_after)
    player = PlaybackPlayer(monitor, samples)
    saves = []
    monkeypatch.setattr(plugin.xbmc, "Player", lambda: player)
    monkeypatch.setattr(plugin.xbmc, "Monitor", lambda: monitor)
    monkeypatch.setattr(
        plugin.xbmc,
        "sleep",
        lambda milliseconds: monitor.waitForAbort(milliseconds / 1000),
    )
    monkeypatch.setattr(plugin.time, "monotonic", lambda: monitor.now)
    monkeypatch.setattr(
        plugin,
        "save_resume_point",
        lambda episode, position, duration: saves.append(
            (episode, position, duration)
        ),
    )

    plugin.monitor_playback(1105)
    return saves


def test_short_playback_saves_last_valid_sample_in_finally(plugin, monkeypatch):
    assert _run_monitor(plugin, monkeypatch, [(12.5, 300)]) == [
        (1105, 12.5, 300.0)
    ]


def test_normal_stop_saves_latest_valid_sample(plugin, monkeypatch):
    assert _run_monitor(plugin, monkeypatch, [(10, 300), (22, 300)]) == [
        (1105, 22.0, 300.0)
    ]


def test_monitor_abort_saves_latest_valid_sample(plugin, monkeypatch):
    assert _run_monitor(
        plugin, monkeypatch, [(10, 300), (20, 300), (30, 300)], abort_after=1
    ) == [(1105, 10.0, 300.0)]


@pytest.mark.parametrize(
    "bad_sample",
    [(RuntimeError("getTime failed"), 300), (10, RuntimeError("duration failed"))],
)
def test_transient_player_read_error_preserves_later_valid_state(
    plugin, monkeypatch, bad_sample
):
    assert _run_monitor(plugin, monkeypatch, [bad_sample, (35, 300)]) == [
        (1105, 35.0, 300.0)
    ]


def test_monitor_uses_elapsed_deadline_instead_of_playback_modulo(plugin, monkeypatch):
    samples = [(value, 300) for value in (11.2, 12.2, 13.2, 14.2, 15.2, 16.2, 17.2)]

    assert _run_monitor(plugin, monkeypatch, samples) == [
        (1105, 16.2, 300.0),
        (1105, 17.2, 300.0),
    ]


def test_monitor_never_replaces_newer_sample_with_zero(plugin, monkeypatch):
    assert _run_monitor(plugin, monkeypatch, [(50, 300), (0, 300)]) == [
        (1105, 50.0, 300.0)
    ]


def test_monitor_keeps_chronologically_latest_backward_seek(plugin, monkeypatch):
    assert _run_monitor(plugin, monkeypatch, [(200, 300), (100, 300)]) == [
        (1105, 100.0, 300.0)
    ]


def test_monitor_preserves_known_duration_during_transient_zero(plugin, monkeypatch):
    assert _run_monitor(plugin, monkeypatch, [(50, 300), (60, 0)]) == [
        (1105, 60.0, 300.0)
    ]


def test_completed_resume_record_is_removed_when_read(plugin, monkeypatch):
    removed = []
    monkeypatch.setattr(
        plugin,
        "read_record",
        lambda vfs, path, legacy_duration=0: {"position": 285.0, "duration": 300.0},
        raising=False,
    )
    monkeypatch.setattr(plugin, "is_completed", lambda record: True, raising=False)
    monkeypatch.setattr(
        plugin,
        "remove_record",
        lambda vfs, path: removed.append(path) or True,
        raising=False,
    )

    assert plugin.get_resume_record(1105, legacy_duration=300) is None
    assert removed == [plugin._resume_path(1105)]


def test_save_resume_point_does_not_overwrite_newer_position(plugin, monkeypatch):
    monkeypatch.setattr(
        plugin,
        "get_resume_record",
        lambda episode, legacy_duration=0: {"position": 100.0, "duration": 300.0},
        raising=False,
    )
    monkeypatch.setattr(
        plugin,
        "write_record",
        lambda *args: pytest.fail("older sample must not be written"),
        raising=False,
    )

    assert plugin.save_resume_point(1105, 0, 300) is False


def test_save_resume_point_allows_newer_nonzero_backward_seek(plugin, monkeypatch):
    writes = []
    monkeypatch.setattr(
        plugin,
        "get_resume_record",
        lambda episode, legacy_duration=0: {"position": 200.0, "duration": 300.0},
    )
    monkeypatch.setattr(
        plugin, "write_record", lambda *args: writes.append(args)
    )

    assert plugin.save_resume_point(1105, 100, 300) is True
    assert writes[-1][-2:] == (100.0, 300.0)


def test_save_resume_point_removes_completed_state(plugin, monkeypatch):
    removed = []
    monkeypatch.setattr(
        plugin, "get_resume_record", lambda *args, **kwargs: None, raising=False
    )
    monkeypatch.setattr(plugin, "is_completed", lambda record: True, raising=False)
    monkeypatch.setattr(
        plugin,
        "remove_record",
        lambda vfs, path: removed.append(path) or True,
        raising=False,
    )
    monkeypatch.setattr(
        plugin,
        "write_record",
        lambda *args: pytest.fail("completed sample must not be written"),
        raising=False,
    )

    assert plugin.save_resume_point(1105, 285, 300) is True
    assert removed == [plugin._resume_path(1105)]


def test_save_resume_point_reports_failed_completed_cleanup(plugin, monkeypatch):
    monkeypatch.setattr(
        plugin, "get_resume_record", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(plugin, "remove_record", lambda vfs, path: False)

    assert plugin.save_resume_point(1105, 285, 300) is False
