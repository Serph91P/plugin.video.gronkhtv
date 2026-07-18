import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "resources" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from context_menu import (  # noqa: E402
    chapter_label,
    details_label,
    favorite_label,
    restart_label,
    resume_label,
)


def test_context_menu_labels_use_consistent_symbols():
    assert resume_label("01:23") == "▶ Fortsetzen bei 01:23"
    assert restart_label() == "⏮ Von Anfang starten"
    assert favorite_label(False) == "☆ Zu Favoriten hinzufügen"
    assert favorite_label(True) == "★ Aus Favoriten entfernen"
    assert details_label() == "ⓘ Stream-Details anzeigen"


def test_chapter_label_never_displays_none():
    assert chapter_label("00:15:00", "Tabletop RPGs") == "⏩ [00:15:00]: Tabletop RPGs"
    assert chapter_label("00:00:00", None) == "⏩ [00:00:00]: Unbenanntes Kapitel"
