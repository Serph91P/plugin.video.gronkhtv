import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "resources" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from live.twitch import twitch_plugin_entries, twitch_plugin_url  # noqa: E402


def test_twitch_plugin_url_hands_live_channel_to_twitch_addon():
    url = twitch_plugin_url(
        {
            "user_id": "12875057",
            "user_login": "gronkh",
        }
    )

    parsed = urlparse(url)
    assert parsed.scheme == "plugin"
    assert parsed.netloc == "plugin.video.twitch"
    assert parse_qs(parsed.query) == {
        "mode": ["play"],
        "channel_id": ["12875057"],
        "channel_name": ["gronkh"],
    }


def test_twitch_plugin_url_falls_back_to_channel_name():
    url = twitch_plugin_url({"user_login": "gronkh"})

    assert parse_qs(urlparse(url).query) == {
        "mode": ["play"],
        "channel_name": ["gronkh"],
    }


def test_twitch_plugin_url_rejects_missing_channel():
    try:
        twitch_plugin_url({})
    except ValueError as exc:
        assert "Twitch" in str(exc)
    else:
        raise AssertionError("Expected missing Twitch channel to fail")


def test_twitch_plugin_entries_skips_incomplete_streams():
    valid = {
        "user_id": "12875057",
        "user_login": "gronkh",
    }

    assert twitch_plugin_entries([{}, valid]) == [
        (
            valid,
            "plugin://plugin.video.twitch/?mode=play&channel_id=12875057&channel_name=gronkh",
        )
    ]
