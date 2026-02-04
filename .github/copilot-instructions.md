# Copilot Instructions für plugin.video.gronkhtv

## Projekt-Übersicht
Dies ist ein Kodi Video-Addon für gronkh.tv, das Zugang zum Stream-Archiv von Gronkh bietet.

## Versionierung - WICHTIG!

Bei **jeder Änderung** am Code müssen folgende Schritte durchgeführt werden:

### 1. Version erhöhen in `addon.xml`
Die Version folgt dem Schema `MAJOR.MINOR.PATCH`:
```xml
<addon id="plugin.video.gronkhtv" name="Gronkh.tv" version="X.Y.Z" provider-name="Seraph91P">
```

- **PATCH** erhöhen (z.B. 2.0.2 → 2.0.3): Bugfixes, kleine Änderungen
- **MINOR** erhöhen (z.B. 2.0.3 → 2.1.0): Neue Features
- **MAJOR** erhöhen (z.B. 2.1.0 → 3.0.0): Breaking Changes

### 2. Changelog in `addon.xml` aktualisieren
Im `<news>` Tag muss ein neuer Eintrag am **Anfang** hinzugefügt werden:
```xml
<news>vX.Y.Z (TT.MM.JJ)
- Beschreibung der Änderung 1
- Beschreibung der Änderung 2
[... vorherige Einträge ...]</news>
```

**Format:**
- Datum im Format `TT.MM.JJ` (z.B. `04.02.26`)
- Jede Änderung als eigene Zeile mit `- ` Prefix
- Keine Umlaute in der news-Sektion (Kodi-Kompatibilität)
  - ä → ae, ö → oe, ü → ue, ß → ss

### 3. Commit und Push
Nach dem Push auf `main` erstellt die GitHub Actions Pipeline automatisch:
- Git-Tag basierend auf der Version in addon.xml
- ZIP-Datei für Kodi
- GitHub Release
- Update auf GitHub Pages

## Code-Richtlinien

### Kodi 21 (Omega) Kompatibilität
- **Nicht verwenden:** `listitem.setInfo()` (deprecated)
- **Stattdessen:** `listitem.getVideoInfoTag()` mit Setter-Methoden
```python
tag = listitem.getVideoInfoTag()
tag.setTitle("Titel")
tag.setDuration(3600)
tag.setPlot("Beschreibung")
```

### Keine Emojis in UI-Texten
Kodi kann Unicode-Emojis nicht darstellen. Verwende ASCII-Alternativen:
- Statt ⭐ → `[+]`
- Statt ❌ → `[X]`
- Statt ℹ → `[i]`
- Statt ▶ → `[>]`

### API-Limits beachten
- gronkh.tv API akzeptiert maximal **25 Ergebnisse** pro Anfrage
- Bei mehr Daten: Paginierung verwenden

### Dateipfade
- Immer `xbmcvfs.translatePath()` für Kodi-Pfade verwenden
- Addon-Daten in: `special://profile/addon_data/plugin.video.gronkhtv/`

## Projektstruktur

```
plugin.video.gronkhtv/
├── addon.py              # Hauptlogik des Addons
├── addon.xml             # Addon-Metadaten und Version
├── LICENSE
├── README.md
├── resources/
│   ├── icon.png
│   └── language/         # Lokalisierungen
│       ├── resource.language.de_de/
│       ├── resource.language.en_gb/
│       └── resource.language.en_us/
└── .github/
    └── workflows/
        └── build-repo.yml  # CI/CD Pipeline
```

## Features des Addons

- Thumbnails für alle Videos (preview_url)
- Resume-System mit Fortschrittsbalken
- Favoriten/Watchlist (lokal gespeichert)
- Filter nach Spielen
- Kapitel-Navigation via Kontextmenü
- Automatisch nächste Episode
- Detail-Dialog mit erweiterten Infos

## Bekannte Limitierungen

- **EDL/Bookmarks:** Funktionieren nicht für Streaming-URLs (nur lokale Dateien)
- **Live-Stream:** Kein API-Endpoint gefunden
- **Emojis:** Werden in Kodi als Boxen dargestellt
