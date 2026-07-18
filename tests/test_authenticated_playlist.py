import sys
from pathlib import Path
from urllib.parse import parse_qs

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "resources" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from playback.playlist import configure_authenticated_hls  # noqa: E402


class FakeListItem:
    def __init__(self):
        self.content_lookup = None
        self.mime_type = None
        self.properties = {}

    def setContentLookup(self, enabled):
        self.content_lookup = enabled

    def setMimeType(self, mime_type):
        self.mime_type = mime_type

    def setProperty(self, key, value):
        self.properties[key] = value


def test_configure_authenticated_hls_passes_cookie_only_to_manifests():
    list_item = FakeListItem()

    configure_authenticated_hls(
        list_item,
        {
            "Cookie": "copycat-session=session-value; XSRF-TOKEN=csrf=value",
            "User-Agent": "Kodi test",
        },
        inputstream_addon="inputstream.adaptive",
    )

    assert list_item.content_lookup is False
    assert list_item.mime_type == "application/vnd.apple.mpegurl"
    assert list_item.properties["inputstream"] == "inputstream.adaptive"
    assert list_item.properties["inputstream.adaptive.manifest_type"] == "hls"
    headers = parse_qs(
        list_item.properties["inputstream.adaptive.manifest_headers"],
        keep_blank_values=True,
    )
    assert headers == {
        "Cookie": ["copycat-session=session-value; XSRF-TOKEN=csrf=value"],
        "User-Agent": ["Kodi test"],
    }
    assert "inputstream.adaptive.stream_headers" not in list_item.properties
    assert "inputstream.adaptive.common_headers" not in list_item.properties


def test_configure_authenticated_hls_rejects_empty_headers():
    list_item = FakeListItem()

    try:
        configure_authenticated_hls(list_item, {}, "inputstream.adaptive")
    except ValueError as exc:
        assert "Header" in str(exc)
    else:
        raise AssertionError("empty headers must be rejected")
