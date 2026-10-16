import importlib.util
import sys
import threading
import time
import types
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "resources" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))


def _visible_account_actions(settings):
    category = ET.parse(ROOT / "resources" / "settings.xml").find("category")
    assert category is not None
    elements = category.findall("setting")
    visible = []
    for index, element in enumerate(elements):
        condition = element.attrib.get("visible")
        if condition == "false":
            continue
        if condition and condition.startswith("eq("):
            offset, expected = condition.removeprefix("eq(").removesuffix(")").split(",")
            referenced = elements[index + int(offset)].attrib["id"]
            if settings.get(referenced, "false") != expected:
                continue
        if element.attrib.get("type") == "action":
            visible.append(element.attrib["id"])
    return visible


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
    # No test may accidentally cross a real private HTTP boundary.
    from urllib.request import OpenerDirector
    monkeypatch.setattr(OpenerDirector, "open", lambda *args, **kwargs: pytest.fail("unexpected real network"))
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


def test_opening_settings_resets_stale_login_state_when_cookie_is_missing(
    plugin, monkeypatch
):
    calls = []

    monkeypatch.setattr(
        plugin._addon,
        "setSetting",
        lambda setting_id, value: calls.append((setting_id, value)),
    )
    monkeypatch.setattr(
        plugin._addon,
        "openSettings",
        lambda: calls.append(("openSettings", None)),
        raising=False,
    )

    plugin.handle_open_settings()

    assert calls == [
        ("account_session_present", "false"),
        ("account_logged_in", "false"),
        ("account_status", "30209"),
        ("openSettings", None),
    ]
    assert _visible_account_actions(
        {"account_session_present": "false", "account_logged_in": "false"}
    ) == ["account_login", "account_logout"]


def test_opening_settings_keeps_stale_session_controls_reachable(plugin, monkeypatch):
    calls = []
    settings = {"account_session_present": "true", "account_logged_in": "true"}

    class ExpiredSession:
        def current_user(self):
            raise plugin.SessionError("session expired")

        def clear(self):
            calls.append(("clear", None))

    monkeypatch.setattr(plugin.os.path, "exists", lambda path: True)
    monkeypatch.setattr(plugin, "_account_session", ExpiredSession())
    monkeypatch.setattr(
        plugin,
        "configure_account_session",
        lambda session: calls.append(("configure", session)),
    )
    monkeypatch.setattr(
        plugin,
        "_deactivate_account_progress_sync",
        lambda: calls.append(("deactivate", None)),
    )

    def set_setting(setting_id, value):
        settings[setting_id] = value
        calls.append((setting_id, value))

    monkeypatch.setattr(plugin._addon, "setSetting", set_setting)
    monkeypatch.setattr(
        plugin._addon,
        "openSettings",
        lambda: calls.append(("openSettings", None)),
        raising=False,
    )

    plugin.handle_open_settings()

    assert calls == [
        ("configure", None),
        ("account_session_present", "true"),
        ("account_logged_in", "false"),
        ("account_status", "30227"),
        ("openSettings", None),
    ]
    assert _visible_account_actions(settings) == ["account_retry", "account_logout"]


def test_account_listing_keeps_retry_and_logout_reachable_for_expired_session(
    plugin, monkeypatch
):
    actions = []

    class ExpiredSession:
        def current_user(self):
            raise plugin.AuthenticationError("expired")

    monkeypatch.setattr(plugin.os.path, "exists", lambda path: True)
    monkeypatch.setattr(plugin, "_account_session", ExpiredSession())
    monkeypatch.setattr(plugin, "configure_account_session", lambda session: None)
    monkeypatch.setattr(plugin, "_deactivate_account_progress_sync", lambda: None)
    monkeypatch.setattr(plugin, "_set_account_status", lambda status: None)
    monkeypatch.setattr(plugin._addon, "setSetting", lambda setting_id, value: None)
    monkeypatch.setattr(plugin._addon, "getSetting", lambda setting_id: "expired")
    monkeypatch.setattr(
        plugin, "_add_account_entry", lambda label, action, icon: actions.append(action)
    )
    monkeypatch.setattr(
        plugin.xbmcplugin, "setPluginCategory", lambda *args: None, raising=False
    )
    monkeypatch.setattr(plugin.xbmcplugin, "setContent", lambda *args: None, raising=False)
    monkeypatch.setattr(
        plugin.xbmcplugin, "addSortMethod", lambda *args: None, raising=False
    )
    monkeypatch.setattr(
        plugin.xbmcplugin, "endOfDirectory", lambda *args: None, raising=False
    )

    plugin.list_account()

    assert actions == [
        "account_status",
        "account_login",
        "account_logout",
        "open_settings",
        "open_twitch_settings",
    ]


def test_account_listing_keeps_login_and_recovery_reachable_without_cookie(
    plugin, monkeypatch
):
    actions = []
    monkeypatch.setattr(plugin.os.path, "exists", lambda path: False)
    monkeypatch.setattr(plugin, "_set_account_status", lambda status: None)
    monkeypatch.setattr(plugin._addon, "setSetting", lambda *args: None)
    monkeypatch.setattr(plugin._addon, "getSetting", lambda setting_id: "")
    monkeypatch.setattr(
        plugin, "_add_account_entry", lambda label, action, icon: actions.append(action)
    )
    monkeypatch.setattr(
        plugin.xbmcplugin, "setPluginCategory", lambda *args: None, raising=False
    )
    monkeypatch.setattr(plugin.xbmcplugin, "setContent", lambda *args: None, raising=False)
    monkeypatch.setattr(
        plugin.xbmcplugin, "addSortMethod", lambda *args: None, raising=False
    )
    monkeypatch.setattr(
        plugin.xbmcplugin, "endOfDirectory", lambda *args: None, raising=False
    )

    plugin.list_account()

    assert actions == [
        "account_status",
        "account_login",
        "account_logout",
        "open_settings",
        "open_twitch_settings",
    ]


def test_confirmed_logout_without_cookie_performs_safe_local_cleanup(plugin, monkeypatch):
    calls = []

    class Session:
        def logout(self):
            calls.append(("logout", None))

    class Dialog:
        def yesno(self, *args, **kwargs):
            return True

        def notification(self, *args):
            calls.append(("notification", args[1]))

    monkeypatch.setattr(plugin.os.path, "exists", lambda path: False)
    monkeypatch.setattr(plugin, "_account_session", Session())
    monkeypatch.setattr(plugin.xbmcgui, "Dialog", Dialog)
    monkeypatch.setattr(
        plugin,
        "configure_account_session",
        lambda session: calls.append(("configure", session)),
    )
    deactivate = plugin._deactivate_account_progress_sync
    monkeypatch.setattr(
        plugin,
        "_deactivate_account_progress_sync",
        lambda: (calls.append(("deactivate", None)), deactivate())[1],
    )
    monkeypatch.setattr(
        plugin._addon,
        "setSetting",
        lambda setting_id, value: calls.append((setting_id, value)),
    )
    monkeypatch.setattr(
        plugin, "_set_account_status", lambda status: calls.append(("status", status))
    )
    monkeypatch.setattr(
        plugin.xbmc,
        "executebuiltin",
        lambda command: calls.append(("builtin", command)),
        raising=False,
    )

    plugin.handle_account_logout()

    assert ("logout", None) in calls
    assert ("configure", None) in calls
    assert ("deactivate", None) in calls
    assert ("account_session_present", "false") in calls
    assert ("account_logged_in", "false") in calls


def test_opening_settings_reconciles_valid_session_before_rendering(plugin, monkeypatch):
    calls = []
    user = {"id": "account-1", "displayname": "Tester"}

    monkeypatch.setattr(plugin.os.path, "exists", lambda path: True)
    monkeypatch.setattr(
        plugin,
        "_account_session",
        types.SimpleNamespace(current_user=lambda: user),
    )
    monkeypatch.setattr(
        plugin._addon,
        "setSetting",
        lambda setting_id, value: calls.append((setting_id, value)),
    )
    monkeypatch.setattr(
        plugin._addon,
        "openSettings",
        lambda: calls.append(("openSettings", None)),
        raising=False,
    )

    plugin.handle_open_settings()

    assert calls == [
        ("account_session_present", "true"),
        ("account_logged_in", "true"),
        ("account_status", "30211: Tester"),
        ("openSettings", None),
    ]


@pytest.mark.parametrize(
    ("cookie_exists", "session_present"), [(False, "false"), (True, "true")]
)
def test_account_indicator_is_initialized_from_persisted_session(
    plugin, monkeypatch, cookie_exists, session_present
):
    calls = []
    monkeypatch.setattr(plugin.os.path, "exists", lambda path: cookie_exists)
    monkeypatch.setattr(
        plugin._addon,
        "setSetting",
        lambda setting_id, value: calls.append((setting_id, value)),
    )

    plugin._sync_account_indicator_from_storage()

    assert calls == [
        ("account_session_present", session_present),
        ("account_logged_in", "false"),
    ]


def test_login_reuses_already_authenticated_session_without_prompting(plugin, monkeypatch):
    calls = []
    user = {"id": "account-1", "displayname": "Tester"}

    monkeypatch.setattr(plugin.os.path, "exists", lambda path: True)
    monkeypatch.setattr(
        plugin,
        "_account_session",
        types.SimpleNamespace(current_user=lambda: user),
    )
    monkeypatch.setattr(
        plugin,
        "_activate_account_progress_sync",
        lambda actual: calls.append(("activate", actual)),
    )
    monkeypatch.setattr(
        plugin._addon,
        "setSetting",
        lambda setting_id, value: calls.append((setting_id, value)),
    )
    monkeypatch.setattr(
        plugin.xbmc,
        "executebuiltin",
        lambda command: calls.append(("builtin", command)),
        raising=False,
    )
    monkeypatch.setattr(
        plugin.xbmcgui,
        "Dialog",
        lambda: pytest.fail("valid session must not prompt for credentials"),
    )

    plugin.handle_account_login()

    assert calls == [
        ("account_session_present", "true"),
        ("account_logged_in", "true"),
        ("account_status", "30211: Tester"),
        ("builtin", "Container.Refresh"),
    ]


def test_login_routes_server_challenge_through_same_session(plugin, monkeypatch):
    calls = []
    user = {"id": "account-1", "displayname": "Tester"}

    class Session:
        def login(self, login, password, two_factor_code=None):
            calls.append(("login", login, password, two_factor_code))
            if two_factor_code is None:
                return types.SimpleNamespace(
                    authenticated=False, requires_two_factor=True, user=None
                )
            return types.SimpleNamespace(
                authenticated=True, requires_two_factor=False, user=user
            )

        def clear(self):
            calls.append(("clear", None))

    answers = iter(["tester@example.com", "password", "123456"])

    class Dialog:
        def input(self, *args, **kwargs):
            return next(answers)

        def notification(self, *args):
            calls.append(("notification", args[1]))

    monkeypatch.setattr(plugin.os.path, "exists", lambda path: False)
    monkeypatch.setattr(plugin, "_account_session", Session())
    monkeypatch.setattr(plugin.xbmcgui, "Dialog", Dialog)
    monkeypatch.setattr(
        plugin,
        "configure_account_session",
        lambda session: calls.append(("configure", session)),
    )
    monkeypatch.setattr(plugin, "_activate_account_progress_sync", lambda actual: None)
    monkeypatch.setattr(plugin._addon, "getSetting", lambda setting_id: "")
    monkeypatch.setattr(
        plugin._addon,
        "setSetting",
        lambda setting_id, value: calls.append((setting_id, value)),
    )
    monkeypatch.setattr(
        plugin.xbmc, "executebuiltin", lambda command: None, raising=False
    )

    plugin.handle_account_login()

    login_calls = [call for call in calls if call[0] == "login"]
    assert login_calls == [
        ("login", "tester@example.com", "password", None),
        ("login", "tester@example.com", "password", "123456"),
    ]
    assert not any(call[0] == "clear" for call in calls)
    assert ("account_logged_in", "true") in calls


def test_rejected_credentials_clear_controller_auth_state(plugin, monkeypatch):
    calls = []
    logs = []

    class RejectedSession:
        def current_user(self):
            raise plugin.AuthenticationError("stale")

        def login(self, login, password, two_factor_code=None):
            calls.append(("login", login, password, two_factor_code))
            raise plugin.AuthenticationError(
                "secret-backend-message", phase="auth", status=401
            )

        def clear(self):
            calls.append(("clear", None))

    answers = iter(["tester@example.com", "password"])

    class Dialog:
        def input(self, *args, **kwargs):
            return next(answers)

        def notification(self, *args):
            calls.append(("notification", args[1]))

    monkeypatch.setattr(plugin.os.path, "exists", lambda path: True)
    monkeypatch.setattr(plugin, "_account_session", RejectedSession())
    monkeypatch.setattr(plugin.xbmcgui, "Dialog", Dialog)
    monkeypatch.setattr(plugin.xbmc, "log", lambda message, level: logs.append(message))
    monkeypatch.setattr(plugin._addon, "getSetting", lambda setting_id: "")
    monkeypatch.setattr(
        plugin._addon,
        "setSetting",
        lambda setting_id, value: calls.append((setting_id, value)),
    )
    monkeypatch.setattr(
        plugin,
        "configure_account_session",
        lambda session: calls.append(("configure", session)),
    )
    monkeypatch.setattr(
        plugin,
        "_deactivate_account_progress_sync",
        lambda: calls.append(("deactivate", None)),
    )

    plugin.handle_account_login()

    assert ("login", "tester@example.com", "password", None) in calls
    assert ("clear", None) in calls
    assert ("configure", None) in calls
    assert ("deactivate", None) in calls
    assert ("account_session_present", "false") in calls
    assert ("account_logged_in", "false") in calls
    assert not any(call == ("account_logged_in", "true") for call in calls)
    assert ("notification", "30216 (phase=auth status=401)") in calls
    assert logs == ["[Gronkh.tv] Account login failed: phase=auth status=401"]
    assert "secret" not in " ".join(logs + [str(call) for call in calls])


def test_cancelled_two_factor_clears_challenge_and_controller_auth_state(
    plugin, monkeypatch
):
    calls = []

    class ChallengeSession:
        def login(self, login, password, two_factor_code=None):
            calls.append(("login", login, password, two_factor_code))
            return types.SimpleNamespace(
                authenticated=False, requires_two_factor=True, user=None
            )

        def clear(self):
            calls.append(("clear", None))

    answers = iter(["tester@example.com", "password", ""])

    class Dialog:
        def input(self, *args, **kwargs):
            return next(answers)

        def notification(self, *args):
            calls.append(("notification", args[1]))

    monkeypatch.setattr(plugin.os.path, "exists", lambda path: False)
    monkeypatch.setattr(plugin, "_account_session", ChallengeSession())
    monkeypatch.setattr(plugin.xbmcgui, "Dialog", Dialog)
    monkeypatch.setattr(plugin._addon, "getSetting", lambda setting_id: "")
    monkeypatch.setattr(
        plugin._addon,
        "setSetting",
        lambda setting_id, value: calls.append((setting_id, value)),
    )
    monkeypatch.setattr(
        plugin,
        "configure_account_session",
        lambda session: calls.append(("configure", session)),
    )
    monkeypatch.setattr(
        plugin,
        "_deactivate_account_progress_sync",
        lambda: calls.append(("deactivate", None)),
    )

    plugin.handle_account_login()

    assert [call for call in calls if call[0] == "login"] == [
        ("login", "tester@example.com", "password", None)
    ]
    assert ("clear", None) in calls
    assert ("configure", None) in calls
    assert ("deactivate", None) in calls
    assert ("account_session_present", "false") in calls
    assert ("account_logged_in", "false") in calls
    assert not any(call == ("account_logged_in", "true") for call in calls)


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


def test_account_resume_uses_remote_value_only_for_normal_start(plugin, monkeypatch):
    calls = []

    class Controller:
        def __init__(self, sync, video_id):
            calls.append((sync, video_id))

        def resolve(self, **kwargs):
            calls.append(kwargs)
            return {"state": "valid", "offset": 42}

    monitor = types.SimpleNamespace(abortRequested=lambda: False)
    monkeypatch.setattr(plugin, "ResumeStartController", Controller)
    monkeypatch.setattr(plugin.xbmc, "Monitor", lambda: monitor)

    assert plugin._account_resume_offset(object(), "backend-video", 12) == 42
    assert plugin._account_resume_offset(object(), "backend-video", 12, explicit_offset=0) == 0
    assert calls[0][1] == "backend-video"
    assert calls[1]["local_offset"] == 12
    assert len(calls) == 2


def test_account_resume_prompts_for_conflict_and_can_use_account_progress(plugin, monkeypatch):
    calls = []

    class Controller:
        def __init__(self, sync, video_id):
            pass

        def resolve(self, **kwargs):
            return {"state": "conflict", "offset": 12, "remote_offset": 42}

    class Dialog:
        def yesno(self, *args, **kwargs):
            calls.append((args, kwargs))
            return False

    class Sync:
        def resolve_conflict(self, video_id, choice):
            calls.append((video_id, choice))
            return 42

    monkeypatch.setattr(plugin, "ResumeStartController", Controller)
    monkeypatch.setattr(plugin.xbmcgui, "Dialog", Dialog)
    monitor = types.SimpleNamespace(abortRequested=lambda: False)
    monkeypatch.setattr(plugin.xbmc, "Monitor", lambda: monitor)

    assert plugin._account_resume_offset(Sync(), "backend-video", 12) == 42
    assert calls[-1] == ("backend-video", "account")


def test_account_status_reports_a_durable_progress_conflict(plugin, monkeypatch):
    notices = []

    class Dialog:
        def notification(self, *args):
            notices.append(args)

    class Store:
        def __init__(self, path):
            pass

        def display_status(self):
            return "conflict"

    monkeypatch.setattr(plugin, "_read_account_user", lambda: {"id": "account-a"})
    monkeypatch.setattr(plugin, "_setting_enabled", lambda setting_id, default=False: True)
    monkeypatch.setattr(plugin, "SyncStore", Store)
    monkeypatch.setattr(plugin.xbmcgui, "Dialog", Dialog)

    plugin.handle_account_status()

    assert notices == [(plugin._plugin, "30224", plugin.xbmcgui.NOTIFICATION_INFO)]


def test_account_progress_sync_uses_an_immutable_session_snapshot(plugin, monkeypatch, tmp_path):
    """A queued A upload must not inherit cookies from a later B login."""
    import uuid

    writes = []

    class MutableSession:
        identity = "account-a"

        def request_json(self, path, method="GET", payload=None, timeout=None):
            if method == "GET":
                return {"data": {"watch_progress": None}}
            writes.append((self.identity, path, payload, timeout))
            return {"data": {}}

    class SnapshotSession:
        def __init__(self, path):
            self.identity = mutable.identity

        def current_user(self, timeout=None):
            return {"id": self.identity}

        def request_json(self, path, method="GET", payload=None, timeout=None):
            if method == "GET":
                return {"data": {"watch_progress": None}}
            writes.append((self.identity, path, payload, timeout))
            return {"data": {}}

    mutable = MutableSession()
    monkeypatch.setattr(plugin, "_PROGRESS_SYNC_FILE", str(tmp_path / "progress.sqlite3"))
    monkeypatch.setattr(plugin, "_ACCOUNT_COOKIE_FILE", str(tmp_path / "cookies.txt"))
    (tmp_path / "cookies.txt").write_text("placeholder")
    monkeypatch.setattr(plugin, "_account_session", mutable)
    monkeypatch.setattr(plugin, "GronkhTVSession", SnapshotSession)
    monkeypatch.setattr(plugin, "_setting_enabled", lambda setting_id, default=False: True)
    epoch = plugin.SyncStore(plugin._PROGRESS_SYNC_FILE).activate("account-a", True)

    sync = plugin._account_progress_sync()
    video_id = str(uuid.uuid4())
    sync.record_position(video_id, 12, 300, event="pause")
    mutable.identity = "account-b"

    assert sync.flush(video_id) == "synchronized"
    assert writes == [("account-a", f"/watched/progress/{video_id}", {"offset": 12}, 2)]
    assert epoch == sync._epoch


def test_play_video_uses_bounded_account_resume_for_normal_start(plugin, monkeypatch):
    item = RecordingListItem()
    resolved = []
    monitor_calls = []
    monkeypatch.setattr(plugin.xbmcgui, "ListItem", lambda path: item)
    monkeypatch.setattr(
        plugin,
        "get_video_info",
        lambda episode: {"id": "backend-video", "source_length": 300, "next": {}},
    )
    monkeypatch.setattr(plugin, "get_resume_record", lambda *args, **kwargs: {"position": 12, "duration": 300})
    monkeypatch.setattr(plugin, "_account_progress_sync", lambda **kwargs: object())
    monkeypatch.setattr(plugin, "_account_resume_offset", lambda *args, **kwargs: 42)
    monkeypatch.setattr(plugin, "_configure_authenticated_playback", lambda *args: True)
    monkeypatch.setattr(plugin.xbmcplugin, "setResolvedUrl", lambda *args, **kwargs: resolved.append(args), raising=False)
    monkeypatch.setattr(plugin, "monitor_playback", lambda *args, **kwargs: monitor_calls.append((args, kwargs)))

    plugin.play_video("https://media/playlist", 1105)

    assert item.tag.calls["setResumePoint"] == [((42, 300), {})]
    assert resolved
    assert monitor_calls[0][1]["video_id"] == "backend-video"


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
    def __init__(self):
        self.calls = {}

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.setdefault(name, []).append((args, kwargs))

        return record


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
            "Game (0:01:30)",
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


def test_listing_marks_persisted_watched_episode_without_a_resume_point(plugin, monkeypatch):
    items = []
    video = {
        "episode": 1105,
        "title": "Episode",
        "video_length": 300,
        "tags": [],
    }
    monkeypatch.setattr(plugin.xbmcgui, "ListItem", RecordingListItem)
    monkeypatch.setattr(plugin, "get_videos", lambda *args, **kwargs: ([video], ""))
    monkeypatch.setattr(plugin, "get_chapters", lambda episode: [])
    monkeypatch.setattr(plugin, "get_resume_record", lambda *args, **kwargs: None)
    monkeypatch.setattr(plugin, "is_watched", lambda episode: True, raising=False)
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

    assert items[0].tag.calls["setPlaycount"] == [((1,), {})]
    assert "setResumePoint" not in items[0].tag.calls


def test_listing_uses_authenticated_remote_progress_before_playback(plugin, monkeypatch):
    """A fresh profile exposes account resume in the actual list renderer."""
    items = []
    video = {
        "id": "cbe1afd0-c240-4c74-ba0a-0307a093ff9b",
        "episode": 1105,
        "title": "Episode",
        "video_length": 300,
        "tags": [],
    }

    class Sync:
        def __init__(self):
            self.reads = []

        def prepare_playback(self, video_id):
            self.reads.append(video_id)
            return {"state": "valid", "offset": 75}

    sync = Sync()
    monkeypatch.setattr(plugin.xbmcgui, "ListItem", RecordingListItem)
    monkeypatch.setattr(plugin, "get_videos", lambda *args, **kwargs: ([video], ""))
    monkeypatch.setattr(plugin, "get_chapters", lambda episode: [])
    monkeypatch.setattr(plugin, "get_resume_record", lambda *args, **kwargs: None)
    monkeypatch.setattr(plugin, "is_watched", lambda episode: False, raising=False)
    monkeypatch.setattr(plugin, "is_favorite", lambda episode: False)
    monkeypatch.setattr(plugin, "_account_progress_sync", lambda **kwargs: sync)
    monkeypatch.setattr(
        plugin.xbmcplugin,
        "addDirectoryItem",
        lambda handle, url, list_item, is_folder: items.append(list_item),
        raising=False,
    )
    for name in ("setPluginCategory", "setContent", "addSortMethod", "endOfDirectory"):
        monkeypatch.setattr(plugin.xbmcplugin, name, lambda *args: None, raising=False)

    plugin.list_videos("category")

    assert sync.reads == [video["id"]]
    assert items[0].tag.calls["setResumePoint"] == [((75, 300), {})]
    assert items[0].context_menu[0] == (
        "Fortsetzen bei 0:01:15",
        "RunPlugin(plugin://plugin.video.gronkhtv/?action=play_resume&episode=1105)",
    )


def test_latest_uploads_recovers_missing_sqlite_context_from_one_validated_session(
    plugin, monkeypatch, tmp_path
):
    """A normal second listing repairs context without reopening the account folder."""
    from account.sync_store import SyncStore

    items = []
    reads = []
    session_reads = []
    settings = {"sync_watch_progress": "true"}
    progress_path = tmp_path / "progress-sync.sqlite3"
    cookie_path = tmp_path / "cookies.txt"
    cookie_path.write_text("synthetic-session", encoding="utf-8")
    missing_epoch = SyncStore(progress_path).deactivate()
    video = {
        "id": "synthetic-video-id",
        "episode": 1105,
        "title": "Latest upload",
        "video_length": 300,
        "tags": [],
    }

    class Session:
        def current_user(self, timeout=None):
            session_reads.append(timeout)
            return {"id": "account-1", "displayname": "Tester"}

    class API:
        def read(self, video_id):
            reads.append(video_id)
            return types.SimpleNamespace(
                status="valid", offset=75, updated_at="synthetic", marked_as_watched=False
            )

    monkeypatch.setattr(plugin, "_PROGRESS_SYNC_FILE", str(progress_path))
    monkeypatch.setattr(plugin, "_ACCOUNT_COOKIE_FILE", str(cookie_path))
    monkeypatch.setattr(plugin, "_account_session", Session())
    monkeypatch.setattr(plugin, "GronkhTVSession", lambda path: Session())
    monkeypatch.setattr(plugin, "ProgressClient", lambda session: API())
    monkeypatch.setattr(plugin._addon, "getSetting", lambda key: settings.get(key, ""))
    monkeypatch.setattr(plugin._addon, "setSetting", settings.__setitem__)
    monkeypatch.setattr(plugin.xbmcgui, "ListItem", RecordingListItem)
    monkeypatch.setattr(plugin, "get_videos", lambda *args, **kwargs: ([video], ""))
    monkeypatch.setattr(plugin, "get_chapters", lambda episode: [])
    monkeypatch.setattr(plugin, "get_resume_record", lambda *args, **kwargs: None)
    monkeypatch.setattr(plugin, "is_watched", lambda episode: False, raising=False)
    monkeypatch.setattr(plugin, "is_favorite", lambda episode: False)
    monkeypatch.setattr(
        plugin.xbmcplugin,
        "addDirectoryItem",
        lambda handle, url, list_item, is_folder: items.append(list_item),
        raising=False,
    )
    for name in ("setPluginCategory", "setContent", "addSortMethod", "endOfDirectory"):
        monkeypatch.setattr(plugin.xbmcplugin, name, lambda *args: None, raising=False)

    plugin.list_videos(plugin._CATEGORIES[0])
    restored = SyncStore(progress_path).context()
    plugin.list_videos(plugin._CATEGORIES[0])
    repeated = SyncStore(progress_path).context()

    assert missing_epoch < restored["epoch"]
    assert restored == repeated
    assert restored["active_account_id"] == "account-1"
    assert restored["sync_enabled"] == 1
    assert len(session_reads) == 2  # Each actual snapshot must be validated.
    assert all(0 < timeout <= 2.25 for timeout in session_reads)
    assert reads == [video["id"], video["id"]]
    assert items[0].tag.calls["setResumePoint"] == [((75, 300), {})]


@pytest.mark.parametrize("account_id", [True, False, "", "   ", None, [], {}])
def test_validated_session_with_invalid_flat_id_does_not_restore_context(
    plugin, monkeypatch, tmp_path, account_id
):
    from account.sync_store import SyncStore

    progress_path = tmp_path / "progress-sync.sqlite3"
    cookie_path = tmp_path / "cookies.txt"
    cookie_path.write_text("synthetic-session", encoding="utf-8")
    initial = SyncStore(progress_path).context()
    monkeypatch.setattr(plugin, "_PROGRESS_SYNC_FILE", str(progress_path))
    monkeypatch.setattr(plugin, "_ACCOUNT_COOKIE_FILE", str(cookie_path))
    monkeypatch.setattr(
        plugin,
        "GronkhTVSession",
        lambda path: types.SimpleNamespace(current_user=lambda timeout=None: {"id": account_id}),
    )
    monkeypatch.setattr(
        plugin._addon,
        "getSetting",
        lambda key: "true" if key == "sync_watch_progress" else "",
    )
    monkeypatch.setattr(plugin._addon, "setSetting", lambda *args: None)

    assert plugin._account_progress_sync() is None
    assert SyncStore(progress_path).context() == initial


@pytest.mark.parametrize(
    ("sync_enabled", "cookie_present"), [(False, True), (True, False)]
)
def test_context_recovery_never_validates_a_session_when_sync_or_cookie_gate_is_closed(
    plugin, monkeypatch, tmp_path, sync_enabled, cookie_present
):
    progress_path = tmp_path / "progress-sync.sqlite3"
    cookie_path = tmp_path / "cookies.txt"
    if cookie_present:
        cookie_path.write_text("synthetic-session", encoding="utf-8")
    monkeypatch.setattr(plugin, "_PROGRESS_SYNC_FILE", str(progress_path))
    monkeypatch.setattr(plugin, "_ACCOUNT_COOKIE_FILE", str(cookie_path))
    monkeypatch.setattr(
        plugin,
        "_account_session",
        types.SimpleNamespace(
            current_user=lambda: pytest.fail("closed recovery gate performed a session read")
        ),
    )
    monkeypatch.setattr(
        plugin._addon,
        "getSetting",
        lambda key: "true" if key == "sync_watch_progress" and sync_enabled else "false",
    )

    assert plugin._account_progress_sync() is None


def test_logout_during_session_reconciliation_cannot_reactivate_sqlite_context(
    plugin, monkeypatch, tmp_path
):
    from account.sync_store import SyncStore

    progress_path = tmp_path / "progress-sync.sqlite3"
    cookie_path = tmp_path / "cookies.txt"
    cookie_path.write_text("synthetic-session", encoding="utf-8")
    store = SyncStore(progress_path)
    store.activate("account-1", enabled=True)
    store.deactivate()
    expected_epoch = store.context()["epoch"]
    read_started = threading.Event()
    read_finished = threading.Event()
    result = []

    class Session:
        def current_user(self):
            read_started.set()
            assert read_finished.wait(2)
            return {"id": "account-1"}

    monkeypatch.setattr(plugin, "_PROGRESS_SYNC_FILE", str(progress_path))
    monkeypatch.setattr(plugin, "_ACCOUNT_COOKIE_FILE", str(cookie_path))
    monkeypatch.setattr(plugin, "_account_session", Session())
    monkeypatch.setattr(
        plugin._addon,
        "getSetting",
        lambda key: "true" if key == "sync_watch_progress" else "",
    )
    monkeypatch.setattr(plugin._addon, "setSetting", lambda *args: None)

    worker = threading.Thread(
        target=lambda: result.append(plugin._read_account_user(expected_epoch)), daemon=True
    )
    worker.start()
    assert read_started.wait(2)
    logout_epoch = SyncStore(progress_path).deactivate()
    cookie_path.unlink()
    read_finished.set()
    worker.join(2)

    assert result == [None]
    assert SyncStore(progress_path).context() == {
        "active_account_id": None,
        "epoch": logout_epoch,
        "sync_enabled": 0,
    }


def test_remote_only_list_resume_context_seeks_the_account_offset(plugin, monkeypatch):
    """The list's resume action must work when no local record exists."""
    items = []
    video = {
        "id": "synthetic-backend-id",
        "episode": 1105,
        "title": "Episode",
        "video_length": 300,
        "tags": [],
    }

    class Sync:
        def prepare_playback(self, video_id):
            assert video_id == "synthetic-backend-id"
            return {"state": "valid", "offset": 75}

    player = FakeSeekPlayer()
    seeks = []
    sync = Sync()
    monkeypatch.setattr(
        plugin.xbmc, "Monitor", lambda: types.SimpleNamespace(abortRequested=lambda: False)
    )
    monkeypatch.setattr(plugin.xbmcgui, "ListItem", RecordingListItem)
    monkeypatch.setattr(plugin, "get_videos", lambda *args, **kwargs: ([video], ""))
    monkeypatch.setattr(plugin, "get_chapters", lambda episode: [])
    monkeypatch.setattr(plugin, "get_resume_record", lambda *args, **kwargs: None)
    monkeypatch.setattr(plugin, "is_watched", lambda episode: False, raising=False)
    monkeypatch.setattr(plugin, "is_favorite", lambda episode: False)
    monkeypatch.setattr(plugin, "_account_progress_sync", lambda **kwargs: sync)
    monkeypatch.setattr(plugin, "get_video_info", lambda episode: {"id": "synthetic-backend-id"})
    monkeypatch.setattr(plugin, "get_playlist_url", lambda episode: "https://media/playlist")
    monkeypatch.setattr(plugin, "_play_direct", lambda url: player)
    monkeypatch.setattr(
        plugin,
        "_seek_to_offset",
        lambda actual_player, offset, **kwargs: seeks.append(offset) or True,
    )
    monkeypatch.setattr(plugin, "monitor_playback", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        plugin.xbmcplugin,
        "addDirectoryItem",
        lambda handle, url, list_item, is_folder: items.append(list_item),
        raising=False,
    )
    for name in ("setPluginCategory", "setContent", "addSortMethod", "endOfDirectory"):
        monkeypatch.setattr(plugin.xbmcplugin, name, lambda *args: None, raising=False)

    plugin.list_videos("category")

    assert items[0].context_menu[0][1].endswith("action=play_resume&episode=1105)")
    assert plugin.handle_play_resume({"episode": "1105"}) is True
    assert seeks == [75]


def test_favorite_without_backend_id_uses_episode_lookup_for_account_progress(
    plugin, monkeypatch
):
    """Legacy favorite metadata must not be mistaken for a backend video ID."""
    items = []
    favorite = {
        "episode": 1105,
        "title": "Legacy favorite",
        "video_length": 300,
        "tags": [],
    }

    class Sync:
        def __init__(self):
            self.reads = []

        def prepare_playback(self, video_id):
            self.reads.append(video_id)
            return {"state": "valid", "offset": 75}

    sync = Sync()
    monkeypatch.setattr(plugin.xbmcgui, "ListItem", RecordingListItem)
    monkeypatch.setattr(plugin, "get_favorites", lambda: [favorite])
    monkeypatch.setattr(plugin, "get_video_info", lambda episode: {"id": "synthetic-backend-id"})
    monkeypatch.setattr(plugin, "get_chapters", lambda episode: [])
    monkeypatch.setattr(plugin, "get_resume_record", lambda *args, **kwargs: None)
    monkeypatch.setattr(plugin, "is_watched", lambda episode: False, raising=False)
    monkeypatch.setattr(plugin, "is_favorite", lambda episode: True)
    monkeypatch.setattr(plugin, "_account_progress_sync", lambda **kwargs: sync)
    monkeypatch.setattr(
        plugin.xbmcplugin,
        "addDirectoryItem",
        lambda handle, url, list_item, is_folder: items.append(list_item),
        raising=False,
    )
    for name in ("setPluginCategory", "setContent", "addSortMethod", "endOfDirectory"):
        monkeypatch.setattr(plugin.xbmcplugin, name, lambda *args: None, raising=False)

    plugin.list_videos(plugin._CATEGORIES[6])

    assert sync.reads == ["synthetic-backend-id"]
    assert items[0].tag.calls["setResumePoint"] == [((75, 300), {})]


def test_listing_limits_unresolved_legacy_favorite_lookups(plugin, monkeypatch):
    """A legacy page cannot turn failed ID resolution into unbounded requests."""
    videos = [
        {"episode": episode, "title": "Legacy favorite"}
        for episode in range(1, 15)
    ]
    lookups = []

    monkeypatch.setattr(plugin, "_account_progress_sync", lambda **kwargs: object())
    monkeypatch.setattr(
        plugin,
        "get_video_info",
        lambda episode: lookups.append(episode) or None,
    )

    assert plugin._listing_account_progress(videos, maximum_reads=12) == {}
    assert lookups == list(range(1, 13))


def test_listing_stops_legacy_id_resolution_after_shared_deadline(plugin, monkeypatch):
    """The list deadline covers legacy ID lookups, not only progress reads."""
    videos = [
        {"episode": episode, "title": "Legacy favorite"}
        for episode in range(1, 15)
    ]
    lookups = []
    clock = [0.0]

    def resolve(episode):
        lookups.append(episode)
        clock[0] += 3
        return None

    monkeypatch.setattr(plugin, "_account_progress_sync", lambda **kwargs: object())
    monkeypatch.setattr(plugin, "get_video_info", resolve)
    monkeypatch.setattr(plugin.time, "monotonic", lambda: clock[0])

    assert plugin._listing_account_progress(videos, timeout_seconds=2.25) == {}
    assert lookups == [1]


def test_listing_returns_by_deadline_while_legacy_lookup_is_still_blocked(
    plugin, monkeypatch
):
    """A blocked legacy lookup cannot keep Kodi's renderer past its budget."""
    started = threading.Event()
    release = threading.Event()

    def resolve(episode):
        started.set()
        release.wait(1)
        return None

    monkeypatch.setattr(plugin, "_account_progress_sync", lambda **kwargs: object())
    monkeypatch.setattr(plugin, "get_video_info", resolve)

    started_at = time.monotonic()
    try:
        assert plugin._listing_account_progress(
            [{"episode": 1105}], timeout_seconds=0.05
        ) == {}
    finally:
        release.set()

    assert started.wait(0.05)
    assert time.monotonic() - started_at <= 0.25


def test_listing_does_not_start_progress_read_after_late_legacy_lookup(
    plugin, monkeypatch
):
    """A backend ID arriving after the budget must not trigger another request."""
    lookup_started = threading.Event()
    reads = []

    class Sync:
        def prepare_playback(self, video_id):
            reads.append(video_id)
            return {"state": "valid", "offset": 75}

    def resolve(episode):
        lookup_started.set()
        time.sleep(0.10)
        return {"id": "late-backend-id"}

    monkeypatch.setattr(plugin, "_account_progress_sync", lambda **kwargs: Sync())
    monkeypatch.setattr(plugin, "get_video_info", resolve)

    assert plugin._listing_account_progress(
        [{"episode": 1105}], timeout_seconds=0.05
    ) == {}
    assert lookup_started.wait(0.05)
    time.sleep(0.12)
    assert reads == []


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
        self.play_calls = []

    def play(self, playlist_url, list_item):
        self.play_calls.append((playlist_url, list_item))

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


def test_play_from_start_removes_prior_resume_and_monitors_new_playback(
    plugin, monkeypatch
):
    monitor = PlaybackMonitor(1)
    player = PlaybackPlayer(monitor, [(12.5, 300)])
    removed = []
    saves = []
    monkeypatch.setattr(plugin.xbmc, "Player", lambda: player)
    monkeypatch.setattr(plugin.xbmc, "Monitor", lambda: monitor)
    monkeypatch.setattr(plugin.xbmcgui, "ListItem", RecordingListItem)
    monkeypatch.setattr(
        plugin, "get_playlist_url", lambda episode: f"https://media/{episode}/playlist"
    )
    monkeypatch.setattr(plugin, "_configure_authenticated_playback", lambda *args: True)
    monkeypatch.setattr(
        plugin, "remove_record", lambda vfs, path: removed.append(path) or True
    )
    monkeypatch.setattr(plugin.time, "monotonic", lambda: monitor.now)
    monkeypatch.setattr(
        plugin,
        "save_resume_point",
        lambda episode, position, duration: saves.append((episode, position, duration)),
    )

    plugin.handle_play_from_start({"episode": "1105"})

    assert removed == [plugin._resume_path(1105)]
    assert [url for url, _ in player.play_calls] == ["https://media/1105/playlist"]
    assert saves == [(1105, 12.5, 300.0)]


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


def test_monitor_records_account_progress_every_fifteen_seconds_and_on_stop(plugin, monkeypatch):
    monitor = PlaybackMonitor(16)
    player = PlaybackPlayer(monitor, [(float(index), 300) for index in range(16)])
    records = []
    flushes = []

    class Sync:
        def record_position(self, *args, **kwargs):
            records.append((args, kwargs))

    monkeypatch.setattr(plugin.xbmc, "Player", lambda: player)
    monkeypatch.setattr(plugin.xbmc, "Monitor", lambda: monitor)
    monkeypatch.setattr(plugin.time, "monotonic", lambda: monitor.now)
    monkeypatch.setattr(plugin, "save_resume_point", lambda *args: True)
    monkeypatch.setattr(plugin, "_flush_progress_async", lambda sync, video_id: flushes.append((sync, video_id)))

    sync = Sync()
    plugin.monitor_playback(1105, progress_sync=sync, video_id="backend-video")

    assert [args[1] for args, _ in records] == [15.0, 15.0]
    assert [kwargs["event"] for _, kwargs in records] == ["tick", "stop"]
    assert flushes == [(sync, "backend-video"), (sync, "backend-video")]


def test_monitor_preserves_chronologically_latest_zero_seek(plugin, monkeypatch):
    assert _run_monitor(plugin, monkeypatch, [(50, 300), (0, 300)]) == [
        (1105, 0.0, 300.0)
    ]


def test_monitor_keeps_chronologically_latest_backward_seek(plugin, monkeypatch):
    assert _run_monitor(plugin, monkeypatch, [(200, 300), (100, 300)]) == [
        (1105, 100.0, 300.0)
    ]


def test_monitor_records_the_latest_account_snapshot_without_network_work(plugin, monkeypatch):
    monitor = PlaybackMonitor(2)
    player = PlaybackPlayer(monitor, [(200, 300), (100, 300)])
    calls = []

    class Sync:
        def record_position(self, video_id, offset, duration, event):
            calls.append((video_id, offset, duration, event))

    monkeypatch.setattr(plugin, "_flush_progress_async", lambda *args: None)
    monkeypatch.setattr(plugin.xbmc, "Player", lambda: player)
    monkeypatch.setattr(plugin.xbmc, "Monitor", lambda: monitor)
    monkeypatch.setattr(plugin.time, "monotonic", lambda: monitor.now)
    monkeypatch.setattr(plugin, "save_resume_point", lambda *args: True)

    plugin.monitor_playback(1105, progress_sync=Sync(), video_id="backend-video")

    assert calls == [
        ("backend-video", 100.0, 300.0, "seek"),
        ("backend-video", 100.0, 300.0, "stop"),
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
    monkeypatch.setattr(plugin, "mark_watched", lambda episode: True, raising=False)
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
    monkeypatch.setattr(plugin, "mark_watched", lambda episode: True, raising=False)
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


def test_completed_playback_marks_episode_watched_before_removing_resume(plugin, monkeypatch):
    operations = []
    monkeypatch.setattr(plugin, "get_resume_record", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        plugin, "mark_watched", lambda episode: operations.append(("watched", episode)) or True
    )
    monkeypatch.setattr(
        plugin,
        "remove_record",
        lambda vfs, path: operations.append(("resume", path)) or True,
    )

    assert plugin.save_resume_point(1105, 285, 300) is True

    assert operations == [("watched", 1105), ("resume", plugin._resume_path(1105))]


def test_play_from_start_clears_watched_state(plugin, monkeypatch):
    cleared = []
    monkeypatch.setattr(plugin, "remove_record", lambda *args: True)
    monkeypatch.setattr(plugin, "clear_watched", lambda episode: cleared.append(episode) or True)
    monkeypatch.setattr(plugin, "get_playlist_url", lambda episode: "https://media/playlist")
    monkeypatch.setattr(plugin, "_play_direct", lambda url: None)
    monkeypatch.setattr(plugin, "monitor_playback", lambda episode: None)

    plugin.handle_play_from_start({"episode": "1105"})

    assert cleared == [1105]


@pytest.mark.parametrize(
    "handler,params",
    [
        ("handle_play_resume", {"episode": "1105"}),
        ("handle_play_from_start", {"episode": "1105"}),
        ("jump_to_chapter", {"episode": "1105", "offset": "0"}),
    ],
)
def test_explicit_playback_paths_bind_account_sync_and_backend_video_id(
    plugin, monkeypatch, handler, params
):
    """Explicit resume/start/chapter paths cannot silently drop account sync."""
    player = FakeSeekPlayer()
    monitor_calls = []
    sync = object()
    monkeypatch.setattr(plugin.xbmc, "Player", lambda: player)
    monkeypatch.setattr(plugin, "get_playlist_url", lambda episode: "https://media/playlist")
    monkeypatch.setattr(plugin, "get_video_info", lambda episode: {"id": "backend-video"})
    monkeypatch.setattr(plugin, "_account_progress_sync", lambda **kwargs: sync)
    monkeypatch.setattr(plugin, "_play_direct", lambda *args: player)
    monkeypatch.setattr(plugin, "_seek_to_offset", lambda *args, **kwargs: True)
    monkeypatch.setattr(plugin, "get_resume_record", lambda *args, **kwargs: {"position": 10, "duration": 300})
    monkeypatch.setattr(plugin, "remove_record", lambda *args, **kwargs: True)
    monkeypatch.setattr(plugin, "clear_watched", lambda *args, **kwargs: True)
    monkeypatch.setattr(
        plugin,
        "monitor_playback",
        lambda *args, **kwargs: monitor_calls.append((args, kwargs)),
    )

    getattr(plugin, handler)(params)

    assert monitor_calls == [
        ((1105,), {"progress_sync": sync, "video_id": "backend-video"})
    ]


@pytest.mark.parametrize(
    "previous,current,duration,speed,expected",
    [
        (None, 12, 300, 1, "tick"),
        (12, 12, 300, 0, "pause"),
        (120, 0, 300, 1, "seek"),
        (294, 299, 300, 1, "end"),
    ],
)
def test_account_playback_event_classifies_pause_seek_and_end(
    plugin, previous, current, duration, speed, expected
):
    assert plugin._account_playback_event(previous, current, duration, speed) == expected


def test_auto_next_binds_same_account_sync_next_backend_id_and_new_generation(plugin, monkeypatch):
    monitor = PlaybackMonitor(1)
    player = PlaybackPlayer(monitor, [(299.0, 300.0)])
    sync = object()
    next_calls = []

    class Dialog:
        def yesno(self, *args, **kwargs):
            return True

    monkeypatch.setattr(plugin.xbmc, "Player", lambda: player)
    monkeypatch.setattr(plugin.xbmc, "Monitor", lambda: monitor)
    monkeypatch.setattr(plugin.time, "monotonic", lambda: monitor.now)
    monkeypatch.setattr(plugin.xbmcgui, "Dialog", Dialog)
    monkeypatch.setattr(plugin, "save_resume_point", lambda *args: True)
    monkeypatch.setattr(plugin, "get_playlist_url", lambda episode: f"https://media/{episode}")
    monkeypatch.setattr(
        plugin,
        "get_video_info",
        lambda episode: {"id": "backend-next", "next": {}},
    )
    monkeypatch.setattr(plugin, "_play_direct", lambda *args: player)
    original_monitor = plugin.monitor_playback

    def capture_next(*args, **kwargs):
        if args[0] == 1106:
            next_calls.append((args, kwargs))
            return None
        return original_monitor(*args, **kwargs)

    monkeypatch.setattr(plugin, "monitor_playback", capture_next)

    plugin.monitor_playback(
        1105,
        next_episode={"episode": 1106, "title": "Next"},
        progress_sync=sync,
        video_id="backend-current",
        generation=3,
    )

    assert next_calls == [
        (
            (1106, {}),
            {"progress_sync": sync, "video_id": "backend-next", "generation": 4},
        )
    ]


def test_stale_playback_generation_drops_old_monitor_events(plugin, monkeypatch):
    monitor = PlaybackMonitor(1)
    player = PlaybackPlayer(monitor, [(12.0, 300.0)])
    records = []

    class Sync:
        def record_position(self, *args, **kwargs):
            records.append((args, kwargs))

    monkeypatch.setattr(plugin, "_PLAYBACK_GENERATION", 2)
    monkeypatch.setattr(plugin.xbmc, "Player", lambda: player)
    monkeypatch.setattr(plugin.xbmc, "Monitor", lambda: monitor)
    monkeypatch.setattr(plugin, "save_resume_point", lambda *args: True)

    plugin.monitor_playback(
        1105, progress_sync=Sync(), video_id="backend-old", generation=1
    )

    assert records == []


def test_save_resume_point_reports_failed_completed_cleanup(plugin, monkeypatch):
    monkeypatch.setattr(
        plugin, "get_resume_record", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(plugin, "remove_record", lambda vfs, path: False)

    assert plugin.save_resume_point(1105, 285, 300) is False
