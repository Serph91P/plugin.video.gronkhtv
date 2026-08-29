import xml.etree.ElementTree as ET
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_addon_news_fits_kodi_metadata_limit():
    addon = ET.parse(ROOT / "addon.xml").getroot()
    news = addon.find("./extension[@point='xbmc.addon.metadata']/news").text

    assert len(news) <= 1500
    assert len(news) == 1196


def test_addon_234_documents_chapter_labels_and_keeps_restart_monitoring():
    addon = ET.parse(ROOT / "addon.xml").getroot()
    news = addon.find("./extension[@point='xbmc.addon.metadata']/news").text

    assert addon.get("version") == "2.3.4"
    assert news.startswith(
        "v2.3.4 (29.08.26)\n- Klarere Kapitelmenue-Labels mit Titel vor Zeitposition\n"
    )
    assert (
        "v2.3.3 (24.08.26)\n"
        "- DE: Von Anfang starten setzt die Wiedergabeueberwachung fort und speichert die spaetere Resume-Position\n"
        "- EN: Restarting from the beginning resumes playback monitoring and saves the later resume position\n"
        in news
    )
