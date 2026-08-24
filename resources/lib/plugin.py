import sys
from urllib.parse import urlencode, parse_qsl, quote_plus
import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin
from urllib.request import build_opener, install_opener
import json
import math
from functools import lru_cache
import time
import xbmcvfs
import os
import inputstreamhelper

from account import AuthenticationError, GronkhTVSession, SessionError
from context_menu import (
    chapter_label,
    details_label,
    favorite_label,
    restart_label,
    resume_label,
)
from gronkhtv_api import (
    category_videos,
    configure_account_session,
    discovery,
    live_streams,
    playlist_url_for_episode,
    search_categories,
    search_videos,
    video_by_episode,
)
from live import twitch_plugin_entries, twitch_plugin_url
from playback import configure_authenticated_hls
from resume_store import is_completed, read_record, remove_record, write_record

# Plugin constants
_URL = sys.argv[0]
_HANDLE = int(sys.argv[1])
_addon = xbmcaddon.Addon(id=_URL[9:-1])
_plugin = _addon.getAddonInfo("name")
_version = _addon.getAddonInfo("version")

xbmc.log(f"[PLUGIN] {_plugin}: version {_version} initialized", xbmc.LOGINFO)
xbmc.log(f"[PLUGIN] {_plugin}: addon {_addon}", xbmc.LOGINFO)

# Kategorien - erweitert um neue Features
_CATEGORIES = [
    _addon.getLocalizedString(30001),  # Neuste Streams
    _addon.getLocalizedString(30002),  # Meist gesehen
    _addon.getLocalizedString(30003),  # Alle Streams
    _addon.getLocalizedString(30004),  # Suche
    "Nach Spielen",  # Neu: Spiele-Filter
    "Live-Streams",
    "Favoriten",
    _addon.getLocalizedString(30200),  # Konto
]

_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
_SEARCH_PAGE_SIZE = 20
_MAX_CHAPTER_ROUTE_OFFSET = 31536000

# Addon data paths
_ADDON_DATA = xbmcvfs.translatePath(
    "special://profile/addon_data/plugin.video.gronkhtv/"
)
_RESUME_DIR = os.path.join(_ADDON_DATA, "resume_points")
_FAVORITES_FILE = os.path.join(_ADDON_DATA, "favorites.json")
_ACCOUNT_COOKIE_FILE = os.path.join(_ADDON_DATA, "account", "cookies.txt")
_account_session = GronkhTVSession(_ACCOUNT_COOKIE_FILE)
configure_account_session(
    _account_session if os.path.exists(_ACCOUNT_COOKIE_FILE) else None
)

chapter_cache = {}
video_info_cache = {}


@lru_cache(maxsize=100)
def get_video_info(episode):
    """Holt erweiterte Video-Informationen inkl. next/previous"""
    xbmc.log(f"[Gronkh.tv] Fetching video info for episode {episode}", xbmc.LOGINFO)
    if episode in video_info_cache:
        return video_info_cache[episode]

    try:
        info = video_by_episode(episode)
        video_info_cache[episode] = info
        return info
    except Exception as e:
        xbmc.log(f"[Gronkh.tv] Error fetching video info: {str(e)}", xbmc.LOGERROR)
        return None


@lru_cache(maxsize=100)
def get_chapters(episode):
    xbmc.log(f"[Gronkh.tv] Fetching chapters for episode {episode}", xbmc.LOGINFO)
    info = get_video_info(episode)
    if info and "chapters" in info:
        return info["chapters"]

    if episode in chapter_cache:
        xbmc.log(
            f"[Gronkh.tv] Chapters found in cache for episode {episode}", xbmc.LOGINFO
        )
        return chapter_cache[episode]

    return []


# ============== Favoriten-System ==============
def get_favorites():
    """Lädt die Favoritenliste"""
    try:
        if xbmcvfs.exists(_FAVORITES_FILE):
            with xbmcvfs.File(_FAVORITES_FILE) as f:
                content = f.read()
                return json.loads(content) if content else []
    except Exception as e:
        xbmc.log(f"[Gronkh.tv] Error loading favorites: {str(e)}", xbmc.LOGERROR)
    return []


def save_favorites(favorites):
    """Speichert die Favoritenliste"""
    try:
        if not xbmcvfs.exists(_ADDON_DATA):
            xbmcvfs.mkdirs(_ADDON_DATA)
        with xbmcvfs.File(_FAVORITES_FILE, "w") as f:
            f.write(json.dumps(favorites, indent=2))
    except Exception as e:
        xbmc.log(f"[Gronkh.tv] Error saving favorites: {str(e)}", xbmc.LOGERROR)


def add_to_favorites(episode, video_data):
    """Fügt ein Video zu den Favoriten hinzu"""
    favorites = get_favorites()
    # Prüfen ob schon vorhanden
    if not any(f.get("episode") == episode for f in favorites):
        favorites.append(video_data)
        save_favorites(favorites)
        xbmcgui.Dialog().notification(
            _plugin,
            f"'{video_data.get('title', episode)}' zu Favoriten hinzugefügt",
            xbmcgui.NOTIFICATION_INFO,
        )
    else:
        xbmcgui.Dialog().notification(
            _plugin, "Bereits in Favoriten", xbmcgui.NOTIFICATION_WARNING
        )


def remove_from_favorites(episode):
    """Entfernt ein Video aus den Favoriten"""
    favorites = get_favorites()
    favorites = [f for f in favorites if f.get("episode") != int(episode)]
    save_favorites(favorites)
    xbmcgui.Dialog().notification(
        _plugin, "Aus Favoriten entfernt", xbmcgui.NOTIFICATION_INFO
    )
    xbmc.executebuiltin("Container.Refresh")


def is_favorite(episode):
    """Prüft ob ein Video in den Favoriten ist"""
    favorites = get_favorites()
    return any(f.get("episode") == episode for f in favorites)


# ============== Spiele-Sammlung ==============
@lru_cache(maxsize=1)
def get_all_games():
    """Sammelt alle Spiele aus v3 Kategorien"""
    try:
        games = {}
        for category in search_categories():
            slug = category.get("slug")
            if not slug:
                continue
            games[slug] = {
                "id": slug,
                "title": category.get("title", ""),
                "thumbnail": _category_thumbnail(category),
                "videos_count": category.get("videos_count", 0),
            }
        return games
    except Exception as e:
        xbmc.log(f"[Gronkh.tv] Error fetching games: {str(e)}", xbmc.LOGERROR)
        return {}


def _category_thumbnail(category):
    urls = category.get("urls") or {}
    boxarts = urls.get("boxarts") or {}
    if isinstance(boxarts, dict):
        return boxarts.get("url") or boxarts.get("thumbnail") or ""
    if isinstance(boxarts, str):
        return boxarts
    return ""


def get_url(**kwargs):
    return "{}?{}".format(_URL, urlencode(kwargs))


def get_categories():
    return _CATEGORIES


def get_playlist_url(episode):
    return playlist_url_for_episode(episode)


def _configure_authenticated_playback(list_item, playlist_url):
    if not _setting_enabled("use_account_playlists", True):
        return False
    if not os.path.exists(_ACCOUNT_COOKIE_FILE):
        return False

    helper = inputstreamhelper.Helper("hls")
    if not helper.check_inputstream():
        xbmc.log(
            "[Gronkh.tv] InputStream Adaptive is unavailable",
            xbmc.LOGWARNING,
        )
        return False

    headers = _account_session.request_headers(playlist_url)
    configure_authenticated_hls(list_item, headers, helper.inputstream_addon)
    return True


def _play_direct(playlist_url, player=None):
    player = player or xbmc.Player()
    list_item = xbmcgui.ListItem(path=playlist_url)
    list_item.setProperty("IsPlayable", "true")
    _configure_authenticated_playback(list_item, playlist_url)
    player.play(playlist_url, list_item)
    return player


def _setting_enabled(setting_id, default=False):
    value = _addon.getSetting(setting_id)
    if value == "":
        return default
    return value.lower() == "true"


def _account_name(user):
    if not isinstance(user, dict):
        return ""
    return (
        user.get("displayname")
        or user.get("login")
        or user.get("usertag")
        or user.get("email")
        or ""
    )


def _set_account_status(status):
    _addon.setSetting("account_status", status)


def _page_from_offset(offset):
    try:
        offset = int(offset)
    except (TypeError, ValueError):
        offset = 0
    return (offset // _SEARCH_PAGE_SIZE) + 1


def get_videos(category, offset=0, search_str="", game_id=None):
    videos = []
    if game_id:  # Videos für ein bestimmtes Spiel
        videos = category_videos(game_id, page=_page_from_offset(offset))
    elif category == _CATEGORIES[0]:  # Neuste Streams
        videos = discovery("newest")
    elif category == _CATEGORIES[1]:  # Meist gesehen
        videos = discovery("most-viewed")
    elif category == _CATEGORIES[2]:  # Alle Streams
        videos = search_videos(page=_page_from_offset(offset))
    elif category == _CATEGORIES[3]:  # Suche
        search_query = (
            search_str
            if search_str != ""
            else xbmcgui.Dialog().input("Suche", type=xbmcgui.INPUT_ALPHANUM)
        )
        while len(search_query) < 3:
            if search_query == "":
                return videos, ""
            xbmcgui.Dialog().ok(_plugin, _addon.getLocalizedString(30101))
            search_query = (
                search_str
                if search_str != ""
                else xbmcgui.Dialog().input("Suche", type=xbmcgui.INPUT_ALPHANUM)
            )
        videos = search_videos(query=search_query)
    elif category == _CATEGORIES[6]:  # Favoriten
        videos = get_favorites()
    return videos, search_query if category == _CATEGORIES[3] else ""


def list_categories():
    xbmcplugin.setPluginCategory(_HANDLE, "Streams und Let's Plays (mit Herz)")
    xbmcplugin.setContent(_HANDLE, "videos")
    categories = get_categories()

    # Icons für Kategorien
    category_icons = {
        0: "DefaultRecentlyAddedEpisodes.png",
        1: "DefaultTVShows.png",
        2: "DefaultMovies.png",
        3: "DefaultAddonsSearch.png",
        4: "DefaultAddonGame.png",  # Spiele
        5: "DefaultLiveTV.png",
        6: "DefaultFavourites.png",  # Favoriten
        7: "DefaultUser.png",  # Konto
    }

    for i, category in enumerate(categories):
        list_item = xbmcgui.ListItem(label=category)

        # Kodi 21: VideoInfoTag verwenden statt setInfo
        tag = list_item.getVideoInfoTag()
        tag.setTitle(category)
        tag.setGenres(["Streams und Let's Plays"])
        tag.setMediaType("video")

        # Icon setzen
        list_item.setArt({"icon": category_icons.get(i, "DefaultFolder.png")})

        url = get_url(action="listing", category=category)
        is_folder = True
        xbmcplugin.addDirectoryItem(_HANDLE, url, list_item, is_folder)

    xbmcplugin.addSortMethod(_HANDLE, xbmcplugin.SORT_METHOD_NONE)
    xbmcplugin.endOfDirectory(_HANDLE)


def list_games():
    """Zeigt alle verfügbaren Spiele an"""
    xbmcplugin.setPluginCategory(_HANDLE, "Nach Spielen")
    xbmcplugin.setContent(_HANDLE, "videos")

    games = get_all_games()

    # Nach Anzahl der Videos sortieren
    sorted_games = sorted(
        games.values(), key=lambda x: x.get("videos_count", 0), reverse=True
    )

    for game in sorted_games:
        video_count = game.get("videos_count", 0)
        list_item = xbmcgui.ListItem(label=f"{game['title']} ({video_count} Streams)")

        if game.get("thumbnail"):
            list_item.setArt(
                {
                    "thumb": game["thumbnail"],
                    "poster": game["thumbnail"],
                    "icon": game["thumbnail"],
                }
            )

        tag = list_item.getVideoInfoTag()
        tag.setTitle(game["title"])
        tag.setPlot(f"{video_count} Streams mit diesem Spiel")
        tag.setMediaType("video")

        url = get_url(
            action="list_game_videos", game_id=game["id"], game_title=game["title"]
        )
        xbmcplugin.addDirectoryItem(_HANDLE, url, list_item, True)

    xbmcplugin.addSortMethod(_HANDLE, xbmcplugin.SORT_METHOD_NONE)
    xbmcplugin.endOfDirectory(_HANDLE)


def list_live_streams():
    """Zeigt aktuell beworbene Live-Streams an."""
    xbmcplugin.setPluginCategory(_HANDLE, "Live-Streams")
    xbmcplugin.setContent(_HANDLE, "videos")

    try:
        streams = live_streams()
    except Exception as e:
        xbmc.log(f"[Gronkh.tv] Error fetching live streams: {str(e)}", xbmc.LOGERROR)
        xbmcgui.Dialog().notification(
            _plugin, "Live-Streams nicht verfügbar", xbmcgui.NOTIFICATION_ERROR
        )
        streams = []

    stream_entries = twitch_plugin_entries(streams)
    skipped_streams = len(streams) - len(stream_entries)
    if skipped_streams:
        xbmc.log(
            f"[Gronkh.tv] Skipped {skipped_streams} live streams without a Twitch channel",
            xbmc.LOGWARNING,
        )

    if not stream_entries:
        list_item = xbmcgui.ListItem(label="Aktuell keine Live-Streams")
        tag = list_item.getVideoInfoTag()
        tag.setTitle("Aktuell keine Live-Streams")
        tag.setPlot("Derzeit meldet GronkhTV keine beworbenen Twitch-Livestreams.")
        tag.setMediaType("video")
        xbmcplugin.addDirectoryItem(_HANDLE, "", list_item, False)
    else:
        for stream, url in stream_entries:
            user_name = stream.get("user_name") or stream.get("user_login") or "Twitch"
            title = stream.get("title") or user_name
            game_name = stream.get("game_name") or "Unbekannt"
            viewer_count = stream.get("viewer_count", 0)
            started_at = stream.get("started_at", "")

            list_item = xbmcgui.ListItem(label=f"{user_name}: {title}")
            thumbnail_url = stream.get("thumbnail_url", "")
            if thumbnail_url:
                list_item.setArt(
                    {
                        "thumb": thumbnail_url,
                        "poster": thumbnail_url,
                        "fanart": thumbnail_url,
                        "icon": thumbnail_url,
                    }
                )

            plot_parts = [
                f"Kanal: {user_name}",
                f"Kategorie: {game_name}",
                f"Zuschauer: {viewer_count:,}",
            ]
            if started_at:
                plot_parts.append(f"Live seit: {started_at}")
            plot_parts.extend(
                [
                    "",
                    "Wiedergabe und Twitch-Kontovorteile laufen ueber das Twitch-Addon.",
                ]
            )

            tag = list_item.getVideoInfoTag()
            tag.setTitle(title)
            tag.setPlot("\n".join(plot_parts))
            tag.setGenres(["Live-Streams", game_name])
            tag.setMediaType("video")
            list_item.setProperty("IsPlayable", "true")

            xbmcplugin.addDirectoryItem(_HANDLE, url, list_item, False)

    xbmcplugin.addSortMethod(_HANDLE, xbmcplugin.SORT_METHOD_NONE)
    xbmcplugin.endOfDirectory(_HANDLE)


def _read_account_user():
    if not os.path.exists(_ACCOUNT_COOKIE_FILE):
        _set_account_status(_addon.getLocalizedString(30209))
        return None
    try:
        user = _account_session.current_user()
    except (AuthenticationError, SessionError):
        _account_session.clear()
        configure_account_session(None)
        _set_account_status(_addon.getLocalizedString(30210))
        return None

    name = _account_name(user)
    status = (
        f"{_addon.getLocalizedString(30211)}: {name}"
        if name
        else _addon.getLocalizedString(30211)
    )
    _set_account_status(status)
    return user


def _add_account_entry(label, action, icon):
    list_item = xbmcgui.ListItem(label=label)
    list_item.setArt({"icon": icon})
    tag = list_item.getVideoInfoTag()
    tag.setTitle(label)
    tag.setMediaType("video")
    xbmcplugin.addDirectoryItem(
        _HANDLE,
        get_url(action=action),
        list_item,
        False,
    )


def list_account():
    xbmcplugin.setPluginCategory(_HANDLE, _addon.getLocalizedString(30200))
    xbmcplugin.setContent(_HANDLE, "videos")
    user = _read_account_user()
    status = _addon.getSetting("account_status") or _addon.getLocalizedString(30209)

    _add_account_entry(
        f"{_addon.getLocalizedString(30202)}: {status}",
        "account_status",
        "DefaultUser.png",
    )
    if user:
        _add_account_entry(
            _addon.getLocalizedString(30205),
            "account_logout",
            "DefaultAddonService.png",
        )
    else:
        _add_account_entry(
            _addon.getLocalizedString(30203),
            "account_login",
            "DefaultAddonService.png",
        )
    _add_account_entry(
        _addon.getLocalizedString(30212),
        "open_settings",
        "DefaultAddonHelper.png",
    )
    _add_account_entry(
        _addon.getLocalizedString(30208),
        "open_twitch_settings",
        "DefaultAddonService.png",
    )

    xbmcplugin.addSortMethod(_HANDLE, xbmcplugin.SORT_METHOD_NONE)
    xbmcplugin.endOfDirectory(_HANDLE)


def list_videos(category, offset=0, search_str="", game_id=None):
    xbmcplugin.setPluginCategory(_HANDLE, category)
    xbmcplugin.setContent(_HANDLE, "videos")

    if category == _CATEGORIES[7]:  # Konto
        list_account()
        return

    # Spezialfall: Spiele-Kategorie zeigt Spiele-Liste
    if category == _CATEGORIES[4]:  # "Nach Spielen"
        list_games()
        return

    if category == _CATEGORIES[5]:  # Live-Streams
        list_live_streams()
        return

    videos, query = get_videos(category, offset, search_str, game_id)

    for video in videos:
        list_item = xbmcgui.ListItem(label=video["title"])
        ep = video["episode"]
        video_duration = video.get("video_length", 0)

        # Thumbnails/Artwork setzen
        preview_url = video.get("preview_url", "")
        if preview_url:
            list_item.setArt(
                {
                    "thumb": preview_url,
                    "poster": preview_url,
                    "fanart": preview_url,
                    "icon": preview_url,
                }
            )

        cm = []
        chapters = get_chapters(ep)
        chapters_content = []
        games_in_stream = []

        for c in chapters:
            title, chapter_offset = _chapter_values(c)
            position = seconds_to_time(chapter_offset)
            cm.append(
                (
                    chapter_label(position, title),
                    f"RunPlugin(plugin://plugin.video.gronkhtv/?action=jump_to_chapter&episode={ep}&offset={chapter_offset:g})",
                )
            )
            chapters_content.append(f"[{position}]: {title}")
            # Spiele sammeln
            game = c.get("game")
            if game and game.get("title") and game.get("title") not in games_in_stream:
                games_in_stream.append(game.get("title"))

        # Kontextmenü für Resume hinzufügen
        resume_record = get_resume_record(ep, legacy_duration=video_duration)
        resume_point = resume_record["position"] if resume_record else 0
        resume_duration = resume_record["duration"] if resume_record else video_duration
        if resume_point > 60:  # Nur anzeigen wenn mehr als 1 Minute geschaut
            cm.insert(
                0,
                (
                    resume_label(seconds_to_time(int(resume_point))),
                    f"RunPlugin(plugin://plugin.video.gronkhtv/?action=play_resume&episode={ep})",
                ),
            )
            cm.insert(
                1,
                (
                    restart_label(),
                    f"RunPlugin(plugin://plugin.video.gronkhtv/?action=play_from_start&episode={ep})",
                ),
            )

        # Favoriten-Menü
        if is_favorite(ep):
            cm.append(
                (
                    favorite_label(True),
                    f"RunPlugin(plugin://plugin.video.gronkhtv/?action=remove_favorite&episode={ep})",
                )
            )
        else:
            # Video-Daten für Favoriten serialisieren
            video_json = quote_plus(
                json.dumps(
                    {
                        "episode": ep,
                        "title": video["title"],
                        "preview_url": preview_url,
                        "video_length": video.get("video_length", 0),
                        "views": video.get("views", 0),
                        "created_at": video.get("created_at", ""),
                        "tags": video.get("tags", []),
                    }
                )
            )
            cm.append(
                (
                    favorite_label(False),
                    f"RunPlugin(plugin://plugin.video.gronkhtv/?action=add_favorite&episode={ep}&video_data={video_json})",
                )
            )

        # Video-Details anzeigen
        cm.append(
            (
                details_label(),
                f"RunPlugin(plugin://plugin.video.gronkhtv/?action=show_details&episode={ep})",
            )
        )

        list_item.addContextMenuItems(cm)

        # Tags als Genres nutzen
        tags = video.get("tags", [])
        genres = ["Streams und Let's Plays"] + [
            t.get("title", "") for t in tags if t.get("title")
        ]

        # Erweiterter Plot mit mehr Infos (ohne Emojis für Kodi-Kompatibilität)
        views = video.get("views", 0)
        duration_str = seconds_to_time(video.get("video_length", 0))

        plot_parts = []
        if views:
            plot_parts.append(f"Aufrufe: {views:,}")
        plot_parts.append(f"Dauer: {duration_str}")
        if games_in_stream:
            plot_parts.append(f"Spiele: {', '.join(games_in_stream)}")
        if resume_point > 60:
            progress = (resume_point / resume_duration) * 100 if resume_duration else 0
            plot_parts.append(
                f"Fortschritt: {progress:.1f}% ({seconds_to_time(int(resume_point))})"
            )
        plot_parts.append("")  # Leerzeile
        plot_parts.append("Kapitel:")
        plot_parts.extend(chapters_content)

        plot = "\n".join(plot_parts)

        tag = list_item.getVideoInfoTag()
        tag.setMediaType("video")
        tag.setTitle(video["title"])
        tag.setGenres(genres)
        tag.setDuration(video.get("video_length", 0))
        tag.setEpisode(ep)
        tag.setDateAdded(video.get("created_at", ""))
        tag.setPremiered(video.get("created_at", ""))
        tag.setFirstAired(video.get("created_at", ""))
        tag.setPlot(plot)
        tag.setPlaycount(1 if resume_point > 0 else 0)

        # Resume-Fortschritt setzen für Fortschrittsbalken
        if resume_point > 0:
            tag.setResumePoint(resume_point, resume_duration)

        list_item.setProperty("IsPlayable", "true")
        url = get_url(action="play", video=video["episode"])
        xbmcplugin.addDirectoryItem(_HANDLE, url, list_item, False)

    if (
        category == _CATEGORIES[2]
        and len(videos) == _SEARCH_PAGE_SIZE
        and videos[-1]["episode"] != 1
    ):
        add_more_item(category, offset)
    elif category == _CATEGORIES[3]:
        handle_search_results(videos, query)
    elif game_id and len(videos) == _SEARCH_PAGE_SIZE:
        add_more_game_item(game_id, category, offset)
    elif category == _CATEGORIES[6] and not videos:
        # Leere Favoriten-Liste
        list_item = xbmcgui.ListItem(label="Noch keine Favoriten")
        tag = list_item.getVideoInfoTag()
        tag.setTitle("Noch keine Favoriten")
        tag.setPlot("Füge Videos über das Kontextmenü zu deinen Favoriten hinzu.")
        tag.setMediaType("video")
        xbmcplugin.addDirectoryItem(_HANDLE, "", list_item, False)

    xbmcplugin.addSortMethod(_HANDLE, xbmcplugin.SORT_METHOD_NONE)
    xbmcplugin.endOfDirectory(_HANDLE)


def add_more_item(category, offset):
    list_item = xbmcgui.ListItem(label="... mehr")
    tag = list_item.getVideoInfoTag()
    tag.setTitle("... mehr")
    tag.setGenres(["Streams und Let's Plays"])
    tag.setMediaType("video")
    url = get_url(
        action="listing", category=category, offset=int(offset) + _SEARCH_PAGE_SIZE
    )
    xbmcplugin.addDirectoryItem(_HANDLE, url, list_item, True)


def add_more_game_item(game_id, game_title, offset):
    list_item = xbmcgui.ListItem(label="... mehr")
    tag = list_item.getVideoInfoTag()
    tag.setTitle("... mehr")
    tag.setGenres(["Streams und Let's Plays"])
    tag.setMediaType("video")
    url = get_url(
        action="list_game_videos",
        game_id=game_id,
        game_title=game_title,
        offset=int(offset) + _SEARCH_PAGE_SIZE,
    )
    xbmcplugin.addDirectoryItem(_HANDLE, url, list_item, True)


def handle_search_results(videos, query):
    if not videos:
        xbmc.log(
            f'[gronkh.tv] Kein Titel bei der Suche nach "{query}" gefunden',
            xbmc.LOGINFO,
        )
        list_item = xbmcgui.ListItem(label=f'Kein Titel unter "{query}" gefunden')
        tag = list_item.getVideoInfoTag()
        tag.setTitle(f'Kein Titel bei der Suche nach "{query}" gefunden')
        tag.setGenres(["Streams und Let's Plays"])
        tag.setMediaType("video")
        url = get_url(action="listing", category=_CATEGORIES[3])
        xbmcplugin.addDirectoryItem(_HANDLE, url, list_item, True)
    else:
        xbmcplugin.addSortMethod(_HANDLE, xbmcplugin.SORT_METHOD_DATEADDED)


def show_video_details(episode):
    """Zeigt erweiterte Video-Details in einem Dialog"""
    info = get_video_info(episode)
    if not info:
        xbmcgui.Dialog().notification(
            _plugin, "Details nicht verfügbar", xbmcgui.NOTIFICATION_ERROR
        )
        return

    # Informationen sammeln
    title = info.get("title", f"Episode {episode}")
    duration = seconds_to_time(info.get("source_length", 0))
    views = info.get("views", 0)
    created = info.get("created_at", "")[:10]  # Nur Datum
    resolution = f"{info.get('source_width', '?')}x{info.get('source_height', '?')}"
    fps = info.get("source_fps", "?")

    # Spiele aus Kapiteln
    games = []
    chapters = info.get("chapters", [])
    for ch in chapters:
        game = ch.get("game", {})
        if game.get("title") and game.get("title") not in games:
            games.append(game.get("title"))

    # Previous/Next
    prev_ep = info.get("previous", {})
    next_ep = info.get("next", {})

    # Dialog-Text erstellen
    details = [
        f"[B]Titel:[/B] {title}",
        f"[B]Episode:[/B] {episode}",
        f"[B]Datum:[/B] {created}",
        f"[B]Dauer:[/B] {duration}",
        f"[B]Aufrufe:[/B] {views:,}",
        f"[B]Auflösung:[/B] {resolution} @ {fps}fps",
        "",
        f"[B]Gespielte Spiele ({len(games)}):[/B]",
        ", ".join(games) if games else "Keine Spiele",
        "",
        f"[B]Kapitel ({len(chapters)}):[/B]",
    ]
    for ch in chapters:
        title, chapter_offset = _chapter_values(ch)
        offset = seconds_to_time(chapter_offset)
        details.append(f"  [{offset}] {title}")

    if prev_ep:
        details.append(f"\n[B]Vorherige Episode:[/B] {prev_ep.get('title', '?')}")
    if next_ep:
        details.append(f"[B]Nächste Episode:[/B] {next_ep.get('title', '?')}")

    xbmcgui.Dialog().textviewer(
        f"Stream-Details: Episode {episode}", "\n".join(details)
    )


def play_video(path, episode, start_offset=0):
    """Spielt ein Video ab mit Auto-Next Unterstützung"""
    start_offset = _non_negative_finite(start_offset, "offset")
    xbmc.log(f"[Gronkh.tv] Playing video: {path}, episode: {episode}", xbmc.LOGINFO)

    # Video-Info holen für nächste Episode
    info = get_video_info(episode)
    video_length = info.get("source_length", 0) if info else 0
    next_episode = info.get("next", {}) if info else {}

    play_item = xbmcgui.ListItem(path=path)
    play_item.setProperty("IsPlayable", "true")
    _configure_authenticated_playback(play_item, path)

    # Thumbnail setzen
    preview_url = info.get("preview_url", "") if info else ""
    if preview_url:
        play_item.setArt(
            {"thumb": preview_url, "poster": preview_url, "fanart": preview_url}
        )

    # Video-Info Tag setzen (Kodi 21 Style)
    tag = play_item.getVideoInfoTag()
    tag.setTitle(
        info.get("title", f"Episode {episode}") if info else f"Episode {episode}"
    )
    tag.setDuration(video_length)
    tag.setEpisode(int(episode))
    tag.setMediaType("video")

    resume_record = get_resume_record(episode, legacy_duration=video_length)
    resume_point = resume_record["position"] if resume_record else 0
    xbmc.log(f"[Gronkh.tv] Resume point: {resume_point}", xbmc.LOGINFO)

    # Start-Offset (für Kapitel-Sprung oder Resume)
    if start_offset > 0:
        play_item.setProperty("StartOffset", str(start_offset))
    elif resume_point > 0:
        tag.setResumePoint(resume_point, video_length)

    xbmcplugin.setResolvedUrl(_HANDLE, True, listitem=play_item)

    # Playback überwachen und ggf. nächste Episode starten
    monitor_playback(episode, next_episode)


def monitor_playback(episode, next_episode=None):
    """Überwacht die Wiedergabe, speichert Fortschritt und startet ggf. nächste Episode"""
    player = xbmc.Player()
    monitor = xbmc.Monitor()

    for _ in range(100):
        if player.isPlayingVideo():
            break
        if monitor.waitForAbort(0.1):
            return

    if not player.isPlayingVideo():
        xbmc.log("[Gronkh.tv] Playback did not start", xbmc.LOGWARNING)
        return

    latest = None
    next_save = time.monotonic() + 5

    try:
        while player.isPlayingVideo() and not monitor.abortRequested():
            try:
                current_time = _non_negative_finite(player.getTime(), "player time")
                total_time = _non_negative_finite(
                    player.getTotalTime(), "player duration"
                )
                if latest is None or current_time > 0 or latest["position"] == 0:
                    if latest and total_time == 0 and latest["duration"] > 0:
                        total_time = latest["duration"]
                    latest = {"position": current_time, "duration": total_time}

                now = time.monotonic()
                if latest and now >= next_save:
                    save_resume_point(
                        episode, latest["position"], latest["duration"]
                    )
                    next_save = now + 5
            except Exception as exc:
                xbmc.log(
                    f"[Gronkh.tv] Error in playback monitor: {exc}",
                    xbmc.LOGWARNING,
                )
            if monitor.waitForAbort(1):
                break
    finally:
        if latest:
            save_resume_point(episode, latest["position"], latest["duration"])
            xbmc.log(
                "[Gronkh.tv] Final resume point saved: "
                f"{latest['position']}/{latest['duration']}",
                xbmc.LOGINFO,
            )

    # Prüfen ob Video zu Ende geschaut wurde (95%+)
    if latest and latest["duration"] > 0 and latest["position"] > 0:
        progress = (latest["position"] / latest["duration"]) * 100
        if progress >= 95 and next_episode and next_episode.get("episode"):
            # Automatisch nächste Episode fragen
            next_title = next_episode.get(
                "title", f"Episode {next_episode.get('episode')}"
            )
            if xbmcgui.Dialog().yesno(
                "Nächste Episode",
                f"Möchtest du die nächste Episode starten?\n\n{next_title}",
                yeslabel="Ja, weiter!",
                nolabel="Nein, danke",
            ):
                next_ep = next_episode.get("episode")
                xbmc.log(
                    f"[Gronkh.tv] Auto-playing next episode: {next_ep}", xbmc.LOGINFO
                )
                next_url = get_playlist_url(next_ep)
                _play_direct(next_url, player)

                # Warten bis nächste Episode startet
                xbmc.sleep(2000)
                monitor_playback(next_ep)


def _resume_path(episode):
    episode = _positive_int(episode, "episode")
    return os.path.join(_RESUME_DIR, f"{episode}.txt")


def get_resume_record(episode, legacy_duration=0):
    path = _resume_path(episode)
    try:
        record = read_record(xbmcvfs, path, legacy_duration=legacy_duration)
        if record and is_completed(record):
            if not remove_record(xbmcvfs, path):
                xbmc.log(
                    f"[Gronkh.tv] Could not remove completed resume record: {path}",
                    xbmc.LOGERROR,
                )
            return None
        return record
    except Exception as e:
        xbmc.log(
            f"[Gronkh.tv] Error getting resume record for episode {episode}: {str(e)}",
            xbmc.LOGERROR,
        )
        return None


def get_resume_point(episode, legacy_duration=0):
    record = get_resume_record(episode, legacy_duration=legacy_duration)
    return record["position"] if record else 0


def save_resume_point(episode, current_time, total_time):
    path = _resume_path(episode)
    try:
        record = {
            "position": _non_negative_finite(current_time, "resume position"),
            "duration": _non_negative_finite(total_time, "resume duration"),
        }
        existing = get_resume_record(episode, legacy_duration=record["duration"])
        if existing and existing["position"] > 0 and record["position"] == 0:
            return False
        if is_completed(record):
            return remove_record(xbmcvfs, path)
        write_record(
            xbmcvfs,
            _RESUME_DIR,
            path,
            record["position"],
            record["duration"],
        )
        return True
    except Exception as e:
        xbmc.log(
            f"[Gronkh.tv] Error saving resume point for episode {episode}: {str(e)}",
            xbmc.LOGERROR,
        )
        return False


def get_total_time(episode):
    try:
        with xbmcvfs.File(
            f"special://profile/addon_data/plugin.video.gronkhtv/total_times/{episode}.txt"
        ) as f:
            return float(f.read() or "0")
    except Exception as e:
        xbmc.log(
            f"[Gronkh.tv] Error getting total time for episode {episode}: {str(e)}",
            xbmc.LOGERROR,
        )
        return 0


def jump_to_chapter(params):
    episode = _positive_int(params.get("episode"), "episode")
    offset = _non_negative_finite(params.get("offset"), "offset")
    xbmc.log(
        f"[Gronkh.tv] Jumping to chapter in episode {episode} at offset {offset}",
        xbmc.LOGINFO,
    )
    player = xbmc.Player()
    url = get_playlist_url(episode)
    xbmc.log(f"[Gronkh.tv] Starting playback of {url}", xbmc.LOGINFO)
    _play_direct(url, player)

    if _seek_to_offset(player, offset, expected_path=url):
        monitor_playback(episode)
        return True
    _stop_failed_playback(player)
    _report_seek_failure("Kapitelsprung fehlgeschlagen")
    return False


def router(paramstring):
    xbmc.log(f"[Gronkh.tv] Router called with params: {paramstring}", xbmc.LOGINFO)
    params = dict(parse_qsl(paramstring))

    action_handlers = {
        "listing": handle_listing,
        "play": handle_play,
        "play_resume": handle_play_resume,
        "play_from_start": handle_play_from_start,
        "jump_to_chapter": jump_to_chapter,
        "add_favorite": handle_add_favorite,
        "remove_favorite": handle_remove_favorite,
        "show_details": handle_show_details,
        "show_live_stream": handle_show_live_stream,
        "list_game_videos": handle_list_game_videos,
        "account_login": handle_account_login,
        "account_logout": handle_account_logout,
        "account_status": handle_account_status,
        "open_settings": handle_open_settings,
        "open_twitch_settings": handle_open_twitch_settings,
    }

    try:
        if params:
            action = params.get("action")
            if action in action_handlers:
                action_handlers[action](params)
            else:
                raise ValueError(f"Invalid action: {action}")
        else:
            list_categories()
    except Exception as e:
        xbmc.log(f"[Gronkh.tv] Error in router: {str(e)}", xbmc.LOGERROR)
        import traceback

        xbmc.log(f"[Gronkh.tv] Traceback: {traceback.format_exc()}", xbmc.LOGERROR)

    xbmc.log("[Gronkh.tv] Router finished", xbmc.LOGINFO)


def handle_listing(params):
    category = params.get("category", "")
    # Spezialfall für Spiele-Kategorie
    if category == _CATEGORIES[4]:  # "Nach Spielen"
        list_games()
    else:
        list_videos(category, params.get("offset", "0"), params.get("search_str", ""))


def handle_play(params):
    episode = _positive_int(params.get("video"), "episode")
    play_video(get_playlist_url(episode), episode)


def handle_add_favorite(params):
    """Fügt ein Video zu den Favoriten hinzu"""
    episode = _positive_int(params.get("episode"), "episode")
    video_data_str = params.get("video_data", "")
    if video_data_str:
        from urllib.parse import unquote

        video_data = json.loads(unquote(video_data_str))
    else:
        # Fallback: Video-Info holen
        info = get_video_info(episode)
        video_data = {
            "episode": episode,
            "title": info.get("title", f"Episode {episode}")
            if info
            else f"Episode {episode}",
            "preview_url": info.get("preview_url", "") if info else "",
            "video_length": info.get("source_length", 0) if info else 0,
            "views": info.get("views", 0) if info else 0,
            "created_at": info.get("created_at", "") if info else "",
            "tags": info.get("tags", []) if info else [],
        }
    add_to_favorites(episode, video_data)


def handle_remove_favorite(params):
    """Entfernt ein Video aus den Favoriten"""
    episode = _positive_int(params.get("episode"), "episode")
    remove_from_favorites(episode)


def handle_show_details(params):
    """Zeigt Video-Details an"""
    episode = _positive_int(params.get("episode"), "episode")
    show_video_details(episode)


def handle_show_live_stream(params):
    """Starts a promoted channel through plugin.video.twitch."""
    stream_url = twitch_plugin_url(params)
    xbmc.executebuiltin(f"PlayMedia({stream_url})")


def handle_account_login(params=None):
    dialog = xbmcgui.Dialog()
    saved_login = _addon.getSetting("account_email")
    login = dialog.input(
        _addon.getLocalizedString(30201),
        defaultt=saved_login,
        type=xbmcgui.INPUT_ALPHANUM,
    ).strip()
    if not login:
        return

    password = dialog.input(
        _addon.getLocalizedString(30213),
        type=xbmcgui.INPUT_ALPHANUM,
        option=xbmcgui.ALPHANUM_HIDE_INPUT,
    )
    if not password:
        return

    try:
        result = _account_session.login(login, password)
        if result.requires_two_factor:
            code = dialog.input(
                _addon.getLocalizedString(30214),
                type=xbmcgui.INPUT_ALPHANUM,
            ).strip()
            if not code:
                _account_session.clear()
                configure_account_session(None)
                dialog.notification(
                    _plugin,
                    _addon.getLocalizedString(30215),
                    xbmcgui.NOTIFICATION_WARNING,
                )
                return
            result = _account_session.login(
                login,
                password,
                two_factor_code=code,
            )

        if not result.authenticated:
            raise AuthenticationError(_addon.getLocalizedString(30216))
        configure_account_session(_account_session)
        _addon.setSetting("account_email", login)
        name = _account_name(result.user)
        status = (
            f"{_addon.getLocalizedString(30211)}: {name}"
            if name
            else _addon.getLocalizedString(30211)
        )
        _set_account_status(status)
        dialog.notification(
            _plugin,
            status,
            xbmcgui.NOTIFICATION_INFO,
        )
        xbmc.executebuiltin("Container.Refresh")
    except (AuthenticationError, SessionError, ValueError) as exc:
        _account_session.clear()
        configure_account_session(None)
        _set_account_status(_addon.getLocalizedString(30209))
        xbmc.log(f"[Gronkh.tv] Account login failed: {exc}", xbmc.LOGWARNING)
        dialog.notification(
            _plugin,
            _addon.getLocalizedString(30216),
            xbmcgui.NOTIFICATION_ERROR,
        )


def handle_account_logout(params=None):
    dialog = xbmcgui.Dialog()
    if not dialog.yesno(_plugin, _addon.getLocalizedString(30217)):
        return
    try:
        _account_session.logout()
    except SessionError as exc:
        xbmc.log(f"[Gronkh.tv] Remote logout failed: {exc}", xbmc.LOGWARNING)
        _account_session.clear()
    configure_account_session(None)
    _set_account_status(_addon.getLocalizedString(30209))
    dialog.notification(
        _plugin,
        _addon.getLocalizedString(30218),
        xbmcgui.NOTIFICATION_INFO,
    )
    xbmc.executebuiltin("Container.Refresh")


def handle_account_status(params=None):
    user = _read_account_user()
    status = _addon.getSetting("account_status") or _addon.getLocalizedString(30209)
    notification = xbmcgui.NOTIFICATION_INFO if user else xbmcgui.NOTIFICATION_WARNING
    xbmcgui.Dialog().notification(_plugin, status, notification)


def handle_open_settings(params=None):
    _addon.openSettings()


def handle_open_twitch_settings(params=None):
    xbmc.executebuiltin("Addon.OpenSettings(plugin.video.twitch)")


def handle_list_game_videos(params):
    """Zeigt alle Videos für ein bestimmtes Spiel"""
    game_id = params["game_id"]
    game_title = params.get("game_title", "Spiel")

    list_videos(game_title, params.get("offset", "0"), game_id=game_id)


def handle_play_resume(params):
    """Video an gespeicherter Position fortsetzen"""
    episode = _positive_int(params.get("episode"), "episode")
    resume_record = get_resume_record(episode)
    resume_point = resume_record["position"] if resume_record else 0
    url = get_playlist_url(episode)
    player = _play_direct(url)

    if resume_point > 0 and _seek_to_offset(
        player, resume_point, expected_path=url
    ):
        monitor_playback(episode)
        return True
    _stop_failed_playback(player)
    _report_seek_failure("Fortsetzen fehlgeschlagen")
    return False


def handle_play_from_start(params):
    """Video von Anfang starten und Resume-Point löschen"""
    episode = _positive_int(params.get("episode"), "episode")
    try:
        remove_record(xbmcvfs, _resume_path(episode))
    except Exception as e:
        xbmc.log(f"[Gronkh.tv] Error deleting resume point: {str(e)}", xbmc.LOGERROR)

    url = get_playlist_url(episode)
    _play_direct(url)


def seconds_to_time(s):
    h = int(s / 60 / 60)
    m = int((s / 60) % 60)
    s = int(s % 60)
    return f"{h}:{m:02d}:{s:02d}"


def _positive_int(value, name):
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if parsed <= 0 or str(value).strip() not in {str(parsed), f"+{parsed}"}:
        raise ValueError(f"{name} must be a positive integer")
    return parsed


def _non_negative_finite(value, name):
    if isinstance(value, bool):
        raise ValueError(f"{name} must be finite non-negative seconds")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be finite non-negative seconds") from exc
    if not math.isfinite(parsed) or not 0 <= parsed <= _MAX_CHAPTER_ROUTE_OFFSET:
        raise ValueError(f"{name} must be finite non-negative seconds")
    return parsed


def _chapter_values(chapter):
    category = chapter.get("category") or chapter.get("game") or {}
    title = chapter.get("title") or category.get("title") or "Unbenanntes Kapitel"
    offset = chapter.get("start_offset", chapter.get("offset", 0))
    return title, _non_negative_finite(offset, "chapter offset")


def _seek_to_offset(player, offset, timeout=10, tolerance=3, expected_path=None):
    offset = _non_negative_finite(offset, "offset")
    deadline = time.monotonic() + timeout
    attempts = 0
    next_attempt = 0

    while time.monotonic() < deadline:
        try:
            if player.isPlayingVideo():
                if expected_path and player.getPlayingFile() != expected_path:
                    xbmc.sleep(100)
                    continue
                duration = float(player.getTotalTime())
                if math.isfinite(duration) and duration > 0 and offset > duration:
                    xbmc.log(
                        f"[Gronkh.tv] Offset {offset} exceeds duration {duration}",
                        xbmc.LOGWARNING,
                    )
                    return False
                position = float(player.getTime())
                seekable = (
                    math.isfinite(duration)
                    and duration > 0
                    and math.isfinite(position)
                    and position >= 0
                )
                if seekable and abs(position - offset) <= tolerance:
                    return True
                if seekable and attempts < 2 and time.monotonic() >= next_attempt:
                    xbmc.log(
                        f"[Gronkh.tv] Seeking to offset {offset} (attempt {attempts + 1})",
                        xbmc.LOGINFO,
                    )
                    player.seekTime(offset)
                    attempts += 1
                    next_attempt = time.monotonic() + 0.5
        except Exception as exc:
            xbmc.log(
                f"[Gronkh.tv] Player not seekable yet: {exc}", xbmc.LOGWARNING
            )
        xbmc.sleep(100)

    xbmc.log(
        f"[Gronkh.tv] Could not verify seek to offset {offset}", xbmc.LOGERROR
    )
    return False


def _stop_failed_playback(player):
    try:
        player.stop()
    except Exception as exc:
        xbmc.log(f"[Gronkh.tv] Could not stop failed playback: {exc}", xbmc.LOGERROR)


def _report_seek_failure(message):
    xbmc.log(f"[Gronkh.tv] {message}", xbmc.LOGERROR)
    xbmcgui.Dialog().notification(
        _plugin, message, xbmcgui.NOTIFICATION_ERROR
    )


def run():
    """Main entry point, sets up opener and runs router."""
    opener = build_opener()
    opener.addheaders = [
        ("User-Agent", _UA),
        ("Accept-Encoding", "identity"),
        ("Accept-Charset", "utf-8"),
    ]
    install_opener(opener)

    router(sys.argv[2][1:])
