# gronkh.tv plugin for Kodi

`plugin.video.gronkhtv` ist ein Kodi-Addon fuer das GronkhTV-Archiv und die von GronkhTV beworbenen Twitch-Livestreams.

## Funktionen

- Aktuelle, meistgesehene und alle Archiv-Streams
- Suche, Spiele-Filter und Kapitel
- Separate Videothek für Let's Plays: Folgen, Serien und Staffeln durchsuchen
- Favoriten, Wiedergabefortschritt und Auto-Next
- GronkhTV-Anmeldung mit optionaler Zwei-Faktor-Authentifizierung
- Kontositzung fuer API-Anfragen und geschuetzte HLS-Wiedergabe
- Twitch-Livestreams ueber `plugin.video.twitch`

## Konto einrichten

1. Im Addon den Ordner **Konto** oeffnen.
2. **Bei GronkhTV anmelden** waehlen.
3. Login oder E-Mail-Adresse und Passwort eingeben.
4. Falls GronkhTV danach fragt, den Zwei-Faktor-Code eingeben.

Das Passwort wird nicht gespeichert. Das Addon speichert nur die von GronkhTV gesetzten Sitzungscookies im Kodi-Profil und setzt die Datei auf restriktive Zugriffsrechte. Ueber **Von GronkhTV abmelden** wird die lokale Sitzung entfernt.

Die Option **Kontositzung fuer Archiv-Wiedergabe verwenden** uebergibt die Sitzung auch an InputStream Adaptive. Ist keine gueltige Kontositzung vorhanden, bleiben oeffentlich erreichbare Archiv-Inhalte nutzbar.

## Videothek

Öffne **Videothek** im Hauptmenü. Dort kannst du Videos durchsuchen, nach Aufrufen sortieren, nach einem Titel suchen oder über **Nach Tags**, **Nach Serien** und **Nach Spielen** einsteigen. Die Spiele werden den Serien zugeordnet, unabhängig von den Kapiteln im Streamarchiv. Bei einem Tagfilter kann eine Seite leer sein; über **Nächste Seite** geht es weiter, solange der Server weitere Ergebnisse meldet.

Für Details und Wiedergabe verwende deine bestehende GronkhTV-Anmeldung; zur Wiedergabe müssen Kontowiedergabe und InputStream Adaptive verfügbar sein. Folgen werden direkt über ihre Video-ID geöffnet. Die Videothek nutzt keine Stream-Favoriten und liest oder schreibt keine Stream-Fortschrittsdaten.

## Live-Streams

Der Ordner **Live-Streams** zeigt die aktuell von GronkhTV beworbenen Twitch-Kanaele. Die Wiedergabe erfolgt ueber das offizielle Kodi-Addon `plugin.video.twitch`. Twitch-Anmeldung, Abonnements, Turbo, Qualitaetseinstellungen und andere Twitch-Kontovorteile werden deshalb im Twitch-Addon verwaltet.

## Abhaengigkeiten

- Kodi 21 Omega oder neuer
- InputStream Helper
- Twitch Kodi-Addon ab Version 3.0.2

Die Abhaengigkeiten werden ueber `addon.xml` deklariert und von Kodi installiert.

## Entwicklung

```bash
python -m pytest -q
kodi-addon-checker plugin.video.gronkhtv --branch=omega
```

Repository: https://github.com/Serph91P/plugin.video.gronkhtv

_Diese Website und das Addon gehoeren nicht zu GronkhTV und stehen in keiner Beziehung zu GronkhTV._

_Kodi ist eine registrierte Marke der XBMC Foundation. Dieses Addon steht in keiner Beziehung zu Kodi, Team Kodi oder der XBMC Foundation._
