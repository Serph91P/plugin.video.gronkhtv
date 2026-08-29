import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "resources" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from context_menu import (
    chapter_label,
    details_label,
    favorite_label,
    restart_label,
    resume_label,
)


def test_context_menu_labels_do_not_depend_on_icon_glyphs():
    assert resume_label("01:23") == "Fortsetzen bei 01:23"
    assert restart_label() == "Von Anfang starten"
    assert favorite_label(False) == "Zu Favoriten hinzufügen"
    assert favorite_label(True) == "Aus Favoriten entfernen"
    assert details_label() == "Stream-Details anzeigen"


def test_chapter_label_puts_title_before_position_without_none():
    assert chapter_label("00:15:00", "Tabletop RPGs") == "Tabletop RPGs (00:15:00)"
    assert chapter_label("00:00:00", None) == "Unbenanntes Kapitel (00:00:00)"
