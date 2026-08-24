import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_addon_232_documents_playback_reliability_in_german_and_english():
    addon = ET.parse(ROOT / "addon.xml").getroot()
    news = addon.find("./extension[@point='xbmc.addon.metadata']/news").text

    assert addon.get("version") == "2.3.2"
    assert news.startswith("v2.3.2 (24.08.26)\n")
    assert "- DE: Resiliente Kapitelspruenge und zuverlaessige Resume-Speicherung" in news
    assert "- EN: Resilient chapter seeks and reliable resume persistence" in news
