import ast
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_manifest_declares_twitch_playback_dependency():
    root = ET.parse(ROOT / "addon.xml").getroot()
    imports = {
        entry.attrib["addon"]: entry.attrib.get("version")
        for entry in root.findall("./requires/import")
    }

    assert imports["plugin.video.twitch"] == "3.0.2"


def test_settings_expose_account_and_playback_actions_without_password_storage():
    settings_path = ROOT / "resources" / "settings.xml"
    root = ET.parse(settings_path).getroot()
    settings = {entry.attrib.get("id"): entry for entry in root.findall(".//setting")}

    assert settings["account_email"].attrib["type"] == "text"
    assert settings["account_login"].attrib["action"].endswith("?action=account_login)")
    assert (
        settings["account_logout"].attrib["action"].endswith("?action=account_logout)")
    )
    assert settings["use_account_playlists"].attrib["default"] == "true"
    assert settings["twitch_settings"].attrib["action"] == (
        "RunPlugin(plugin://$ID/?action=open_twitch_settings)"
    )
    assert all(
        "password" not in setting_id.lower() for setting_id in settings if setting_id
    )


def test_plugin_routes_account_actions_and_uses_separate_feature_packages():
    source = (ROOT / "resources" / "lib" / "plugin.py").read_text(encoding="utf-8")

    assert '"account_login": handle_account_login' in source
    assert '"account_logout": handle_account_logout' in source
    assert '"account_status": handle_account_status' in source
    assert "from account import" in source
    assert "from live import twitch_plugin_url" in source
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
