import re
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _addon_version(addon):
    version = addon.get("version")
    if not re.fullmatch(r"\d+\.\d+\.\d+", version or ""):
        raise ValueError("invalid Kodi add-on version")
    return tuple(map(int, version.split(".")))


def test_addon_news_fits_kodi_metadata_limit():
    addon = ET.parse(ROOT / "addon.xml").getroot()
    news = addon.find("./extension[@point='xbmc.addon.metadata']/news").text

    assert len(news) <= 1500


def test_addon_version_is_valid_and_news_keeps_account_and_chapter_history():
    addon = ET.parse(ROOT / "addon.xml").getroot()
    news = addon.find("./extension[@point='xbmc.addon.metadata']/news").text

    assert _addon_version(addon) >= (2, 3, 5)
    assert news.startswith(
        "v2.3.5 (21.09.26)\n"
        "- DE: Konto-Einstellungen zeigen nur die passende Anmeldeaktion und den sichtbaren Kontonamen\n"
        "- EN: Account settings show only the matching sign-in action and visible account name\n"
    )
    assert (
        "v2.3.3 (24.08.26)\n"
        "- DE: Von Anfang starten setzt die Wiedergabeueberwachung fort und speichert die spaetere Resume-Position\n"
        "- EN: Restarting from the beginning resumes playback monitoring and saves the later resume position\n"
        in news
    )


@pytest.mark.parametrize("version", ["2.3", "v2.3.6", "2.3.6rc1", "2.-1.0", ""])
def test_addon_version_rejects_invalid_metadata(version, tmp_path):
    addon = tmp_path / "addon.xml"
    addon.write_text(f'<addon id="plugin.video.gronkhtv" version="{version}"/>\n', encoding="utf-8")
    root = ET.parse(addon).getroot()

    with pytest.raises(ValueError, match="invalid Kodi add-on version"):
        _addon_version(root)
