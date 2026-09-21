import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_addon_news_fits_kodi_metadata_limit():
    addon = ET.parse(ROOT / "addon.xml").getroot()
    news = addon.find("./extension[@point='xbmc.addon.metadata']/news").text

    assert len(news) <= 1500


def test_addon_235_documents_account_menu_state_and_keeps_chapter_history():
    addon = ET.parse(ROOT / "addon.xml").getroot()
    news = addon.find("./extension[@point='xbmc.addon.metadata']/news").text

    assert addon.get("version") == "2.3.5"
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
