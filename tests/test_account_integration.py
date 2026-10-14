import ast
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _visible_account_setting_ids(persisted_settings):
    """Evaluate Kodi's relative visibility rules from persisted/default values."""
    root = ET.parse(ROOT / "resources" / "settings.xml").getroot()
    account_settings = root.findall("./category[@label='30200']/setting")
    values = {
        setting.attrib["id"]: persisted_settings.get(
            setting.attrib["id"], setting.attrib.get("default", "")
        )
        for setting in account_settings
    }
    visible = []
    for index, setting in enumerate(account_settings):
        condition = setting.attrib["visible"]
        if condition == "false":
            continue
        if condition.startswith("eq("):
            relative_index, expected = condition[3:-1].split(",")
            target = account_settings[index + int(relative_index)]
            if values[target.attrib["id"]] != expected:
                continue
        visible.append(setting.attrib["id"])
    return visible


def test_direct_settings_upgrade_without_new_marker_keeps_retry_and_logout_reachable():
    """Kodi can open add-on settings before importing the plugin controller."""
    assert _visible_account_setting_ids({"account_logged_in": "false"}) == [
        "account_status",
        "account_retry",
        "account_logout",
    ]
    assert _visible_account_setting_ids({"account_logged_in": "true"}) == [
        "account_status",
        "account_retry",
        "account_logout",
    ]


def test_manifest_declares_twitch_playback_dependency():
    root = ET.parse(ROOT / "addon.xml").getroot()
    imports = {
        entry.attrib["addon"]: entry.attrib.get("version")
        for entry in root.findall("./requires/import")
    }

    assert imports["plugin.video.twitch"] == "3.0.2"


def test_settings_expose_only_state_appropriate_account_actions_without_secrets():
    settings_path = ROOT / "resources" / "settings.xml"
    root = ET.parse(settings_path).getroot()
    settings = {entry.attrib.get("id"): entry for entry in root.findall(".//setting")}

    assert "account_email" not in settings
    assert settings["account_logged_in"].attrib == {
        "id": "account_logged_in",
        "type": "bool",
        "default": "false",
        "visible": "false",
    }
    assert settings["account_session_present"].attrib["default"] == "true"
    account_category = root.find("./category[@label='30200']")
    assert account_category is not None
    account_settings = account_category.findall("setting")
    assert [setting.attrib["id"] for setting in account_settings] == [
        "account_session_present",
        "account_logged_in",
        "account_status",
        "account_login",
        "account_retry",
        "account_logout",
    ]
    assert settings["account_status"].attrib["visible"] == "eq(-2,true)"
    assert settings["account_login"].attrib["visible"] == "eq(-3,false)"
    assert settings["account_retry"].attrib["visible"] == "eq(-4,true)"
    assert settings["account_logout"].attrib["visible"] == "eq(-5,true)"

    def visible_setting_ids(session_present, logged_in):
        values = {
            "account_session_present": session_present,
            "account_logged_in": logged_in,
        }
        visible = []
        for index, setting in enumerate(account_settings):
            condition = setting.attrib["visible"]
            if condition == "false":
                continue
            if condition.startswith("eq("):
                relative_index, expected = condition[3:-1].split(",")
                target = account_settings[index + int(relative_index)]
                if values[target.attrib["id"]] != (expected == "true"):
                    continue
            visible.append(setting.attrib["id"])
        return visible

    assert visible_setting_ids(False, False) == ["account_login"]
    assert visible_setting_ids(True, False) == [
        "account_status",
        "account_retry",
        "account_logout",
    ]
    assert visible_setting_ids(True, True) == [
        "account_status",
        "account_retry",
        "account_logout",
    ]
    assert settings["account_login"].attrib["action"].endswith("?action=account_login)")
    assert settings["account_retry"].attrib["action"].endswith("?action=account_login)")
    assert (
        settings["account_logout"].attrib["action"].endswith("?action=account_logout)")
    )
    assert settings["use_account_playlists"].attrib["default"] == "true"
    assert settings["sync_watch_progress"].attrib == {
        "id": "sync_watch_progress",
        "type": "bool",
        "label": "30219",
        "default": "false",
    }
    assert settings["twitch_settings"].attrib["action"] == (
        "RunPlugin(plugin://$ID/?action=open_twitch_settings)"
    )
    assert all(
        secret not in setting_id.lower()
        for secret in ("password", "token", "cookie", "2fa")
        for setting_id in settings
        if setting_id
    )


def test_account_status_logic_updates_hidden_login_state_from_session_validation():
    source = (ROOT / "resources" / "lib" / "plugin.py").read_text(encoding="utf-8")

    assert '_addon.setSetting("account_logged_in", "true")' in source
    assert source.count('_addon.setSetting("account_logged_in", "false")') >= 3
    assert 'saved_login = _addon.getSetting("account_email")' in source
    assert '_addon.setSetting("account_email", login)' in source


def test_plugin_routes_account_actions_and_uses_separate_feature_packages():
    source = (ROOT / "resources" / "lib" / "plugin.py").read_text(encoding="utf-8")

    assert '"account_login": handle_account_login' in source
    assert '"account_logout": handle_account_logout' in source
    assert '"account_status": handle_account_status' in source
    assert "from account import" in source
    assert "from live import" in source
    assert "from playback import" in source
    assert "configure_authenticated_hls(list_item, headers" in source
    assert 'setProperty("inputstream.adaptive.stream_headers"' not in source


def test_all_direct_player_calls_use_authenticated_list_item_helper():
    source = (ROOT / "resources" / "lib" / "plugin.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    direct_play_functions = []

    for function in (
        node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef)
    ):
        for call in (node for node in ast.walk(function) if isinstance(node, ast.Call)):
            if (
                isinstance(call.func, ast.Attribute)
                and call.func.attr == "play"
                and isinstance(call.func.value, ast.Name)
                and call.func.value.id == "player"
            ):
                direct_play_functions.append(function.name)

    assert direct_play_functions == ["_play_direct"]


def test_account_login_discards_challenge_session_on_cancel_and_error():
    source = (ROOT / "resources" / "lib" / "plugin.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    function = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "handle_account_login"
    )

    cancelled_two_factor = next(
        node
        for node in ast.walk(function)
        if isinstance(node, ast.If)
        and isinstance(node.test, ast.UnaryOp)
        and isinstance(node.test.op, ast.Not)
        and isinstance(node.test.operand, ast.Name)
        and node.test.operand.id == "code"
    )
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "clear"
        for node in ast.walk(cancelled_two_factor)
    )

    login_error = next(node for node in function.body if isinstance(node, ast.Try))
    assert any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "clear"
        for handler in login_error.handlers
        for node in ast.walk(handler)
    )
