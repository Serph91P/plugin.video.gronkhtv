"""Kodi context-menu labels without Kodi runtime dependencies."""

_DEFAULT_CHAPTER_TITLE = "Unbenanntes Kapitel"


def resume_label(position):
    return f"▶ Fortsetzen bei {position}"


def restart_label():
    return "⏮ Von Anfang starten"


def favorite_label(is_favorite):
    if is_favorite:
        return "★ Aus Favoriten entfernen"
    return "☆ Zu Favoriten hinzufügen"


def chapter_label(position, title):
    return f"⏩ [{position}]: {title or _DEFAULT_CHAPTER_TITLE}"


def details_label():
    return "ⓘ Stream-Details anzeigen"
