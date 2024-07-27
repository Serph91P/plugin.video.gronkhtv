# -*- coding: utf-8 -*-
# Module: main
# Author: Seraph91P
# Based on example by: Roman V. M.
# Created on: 27.07.2024    
# License: GPL v.3 https://www.gnu.org/copyleft/gpl.html

import sys
from urllib.parse import urlencode, parse_qsl, quote_plus
import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin

from urllib.request import urlopen, build_opener, install_opener
import json
from functools import lru_cache

# Get the plugin url in plugin:// notation.
_URL = sys.argv[0]
# Get the plugin handle as an integer number.
_HANDLE = int(sys.argv[1])

# plugin constants
_addon   = xbmcaddon.Addon(id=_URL[9:-1])
_plugin  = _addon.getAddonInfo("name")
_version = _addon.getAddonInfo("version")

xbmc.log(f'[PLUGIN] {_plugin}: version {_version} initialized', xbmc.LOGINFO)
xbmc.log(f'[PLUGIN] {_plugin}: addon {_addon}', xbmc.LOGINFO)

# menu categories
_CATEGORIES = [_addon.getLocalizedString(30001),
                _addon.getLocalizedString(30002),
                _addon.getLocalizedString(30003),
                _addon.getLocalizedString(30004)]

# user agent for requests
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/67.0.3396.62 Safari/537.36"

# Globale Variable für den Kapitel-Cache
chapter_cache = {}

# Custom OSD class
class CustomOSD(xbmcgui.WindowXMLDialog):
    def __init__(self, *args, **kwargs):
        self.chapters = kwargs.get('chapters', [])
        self.player = xbmc.Player()
        super(CustomOSD, self).__init__(*args, **kwargs)

    def onInit(self):
        self.chapter_button = self.getControl(9000)
        self.chapter_button.setLabel("Chapters")

    def onClick(self, controlId):
        if controlId == 9000:
            chapter_offset = show_chapter_menu(self.chapters)
            if chapter_offset is not None:
                self.player.seekTime(chapter_offset)
                self.close()

# Funktion zum Abrufen und Cachen der Kapitel
@lru_cache(maxsize=100)  # Speichert die letzten 100 Aufrufe
def get_chapters(episode):
    xbmc.log(f"[Gronkh.tv] Fetching chapters for episode {episode}", xbmc.LOGINFO)
    if episode in chapter_cache:
        xbmc.log(f"[Gronkh.tv] Chapters found in cache for episode {episode}", xbmc.LOGINFO)
        return chapter_cache[episode]
    
    req = urlopen(f'https://api.gronkh.tv/v1/video/info?episode={episode}')
    content = req.read().decode("utf-8")
    chapters = json.loads(content)["chapters"]
    
    chapter_cache[episode] = chapters
    xbmc.log(f"[Gronkh.tv] Fetched {len(chapters)} chapters for episode {episode}", xbmc.LOGINFO)
    return chapters

def get_url(**kwargs):
    """
    Create a URL for calling the plugin recursively from the given set of keyword arguments.

    :param kwargs: "argument=value" pairs
    :return: plugin call URL
    :rtype: str
    """
    return '{}?{}'.format(_URL, urlencode(kwargs))

def get_categories():
    """
    Get the list of video categories.

    Here you can insert some parsing code that retrieves
    the list of video categories (e.g. 'Movies', 'TV-shows', 'Documentaries' etc.)
    from some site or API.

    .. note:: Consider using `generator functions <https://wiki.python.org/moin/Generators>`_
        instead of returning lists.

    :return: The list of video categories
    :rtype: types.GeneratorType
    """

    return _CATEGORIES

def get_playlist_url(episode):
    """
    Get Playlist-URL from episode number
    Playlist-URL is in .m3u8 format (can be played by Kodi directly)
    """
    pl = urlopen("https://api.gronkh.tv/v1/video/playlist?episode=" + str(episode))
    playlist_url = json.loads(pl.read().decode("utf-8"))["playlist_url"]

    return playlist_url

def get_videos(category, offset=0, search_str=""):
    videos = []

    if category == _CATEGORIES[0]:
        req = urlopen("https://api.gronkh.tv/v1/video/discovery/recent")
        content = req.read().decode("utf-8")
        videos = json.loads(content)["discovery"]
    elif category == _CATEGORIES[1]:
        req = urlopen("https://api.gronkh.tv/v1/video/discovery/views")
        content = req.read().decode("utf-8")
        videos = json.loads(content)["discovery"]
    elif category == _CATEGORIES[2]:
        OFFSET = offset
        NUM = 25
        req = urlopen(f'https://api.gronkh.tv/v1/search?sort=date&offset={OFFSET}&first={NUM}')
        content = req.read().decode("utf-8")
        videos = json.loads(content)["results"]["videos"]
    elif category == _CATEGORIES[3]:
        search_query = search_str if search_str != "" else xbmcgui.Dialog().input("Suche", type=xbmcgui.INPUT_ALPHANUM)
        while len(search_query) < 3:
            if search_query == "":
                return videos, ""
            xbmcgui.Dialog().ok(_plugin, _addon.getLocalizedString(30101))
            search_query = search_str if search_str != "" else xbmcgui.Dialog().input("Suche", type=xbmcgui.INPUT_ALPHANUM)
        req = urlopen(f'https://api.gronkh.tv/v1/search?query={quote_plus(search_query)}')
        content = req.read().decode("utf-8")
        videos = json.loads(content)["results"]["videos"]
    return videos, search_query if category == _CATEGORIES[3] else ""

def list_categories():
    """
    Create the list of video categories in the Kodi interface.
    """
    # Set plugin category. It is displayed in some skins as the name
    # of the current section.
    xbmcplugin.setPluginCategory(_HANDLE, 'Streams und Let\'s Plays (mit Herz)')
    # Set plugin content. It allows Kodi to select appropriate views
    # for this type of content.
    xbmcplugin.setContent(_HANDLE, 'videos')
    # Get video categories
    categories = get_categories()
    # Iterate through categories
    for category in categories:
        # Create a list item with a text label and a thumbnail image.
        list_item = xbmcgui.ListItem(label=category)
        list_item.setInfo('video', {'title': category,
                                    'genre': 'Streams und Let\'s Plays',
                                    'mediatype': 'video'})
        # Create a URL for a plugin recursive call.
        # Example: plugin://plugin.video.example/?action=listing&category=Animals
        url = get_url(action='listing', category=category)
        # is_folder = True means that this item opens a sub-list of lower level items.
        is_folder = True
        # Add our item to the Kodi virtual folder listing.
        xbmcplugin.addDirectoryItem(_HANDLE, url, list_item, is_folder)
    # Add a sort method for the virtual folder items (alphabetically, ignore articles)
    xbmcplugin.addSortMethod(_HANDLE, xbmcplugin.SORT_METHOD_NONE)
    # Finish creating a virtual folder.
    xbmcplugin.endOfDirectory(_HANDLE)

def list_videos(category, offset=0, search_str=""):
    xbmcplugin.setPluginCategory(_HANDLE, category)
    xbmcplugin.setContent(_HANDLE, 'videos')
    videos, query = get_videos(category, offset, search_str)

    for video in videos:
        list_item = xbmcgui.ListItem(label=video['title'])
        ep = video['episode']

        tag = list_item.getVideoInfoTag()
        tag.setMediaType('video')
        tag.setTitle(video['title'])
        tag.setGenres(['Streams und Let\'s Plays'])
        tag.setDuration(video['video_length'])
        tag.setEpisode(ep)
        tag.setDateAdded(video['created_at'])
        tag.setPremiered(video['created_at'])
        tag.setFirstAired(video['created_at'])

        list_item.setArt({'thumb': video['preview_url'], 'icon': video['preview_url'], 'fanart': video['preview_url']})
        list_item.setProperty('IsPlayable', 'true')
        url = get_url(action='play', video=video['episode'])
        xbmcplugin.addDirectoryItem(_HANDLE, url, list_item, False)

    if category == _CATEGORIES[2] and len(videos) == 25 and videos[-1]['episode'] != 1:
        add_more_item(category, offset)
    elif category == _CATEGORIES[3]:
        handle_search_results(videos, query)

    xbmcplugin.addSortMethod(_HANDLE, xbmcplugin.SORT_METHOD_NONE)
    xbmcplugin.endOfDirectory(_HANDLE)

def add_more_item(category, offset):
    list_item = xbmcgui.ListItem(label="... mehr")
    list_item.setInfo('video', {'title': "... mehr", 'genre': 'Streams und Let\'s Plays', 'mediatype': 'video'})
    url = get_url(action='listing', category=category, offset=int(offset)+25)
    xbmcplugin.addDirectoryItem(_HANDLE, url, list_item, True)

def handle_search_results(videos, query):
    if not videos:
        xbmc.log(f'[gronkh.tv] Kein Titel bei der Suche nach "{query}" gefunden', xbmc.LOGINFO)
        list_item = xbmcgui.ListItem(label=f'Kein Titel unter "{query}" gefunden')
        list_item.setInfo('video', {'title': f'Kein Titel bei der Suche nach "{query}" gefunden',
                                    'genre': 'Streams und Let\'s Plays',
                                    'mediatype': 'video'})
        url = get_url(action='listing', category=_CATEGORIES[3])
        xbmcplugin.addDirectoryItem(_HANDLE, url, list_item, True)
    else:
        xbmcplugin.addSortMethod(_HANDLE, xbmcplugin.SORT_METHOD_DATEADDED)

def show_chapter_menu(chapters):
    chapter_names = [f"{c['title']} ({seconds_to_time(c['offset'])})" for c in chapters]
    dialog = xbmcgui.Dialog()
    index = dialog.select("Select Chapter", chapter_names)
    if index >= 0:
        return chapters[index]['offset']
    return None

def play_video(path, episode):
    xbmc.log(f"[Gronkh.tv] Playing video: {path}, episode: {episode}", xbmc.LOGINFO)
    play_item = xbmcgui.ListItem(path=path)
    
    try:
        chapters = get_chapters(episode)
        xbmc.log(f"[Gronkh.tv] Fetched {len(chapters)} chapters for episode {episode}", xbmc.LOGINFO)
        
        video_tag = play_item.getVideoInfoTag()
        video_tag.setTitle(f'Episode {episode}')
        video_tag.setEpisode(int(episode))
        video_tag.setMediaType('video')
        
        total_duration = chapters[-1]['offset'] if chapters else 0
        video_tag.setDuration(total_duration)
        
        xbmcplugin.setResolvedUrl(_HANDLE, True, listitem=play_item)
        
        player = xbmc.Player()
        while not player.isPlayingVideo():
            xbmc.sleep(100)
        
        custom_osd = CustomOSD('CustomOSD.xml', _addon.getAddonInfo('path'), 'default', '1080i', chapters=chapters)
        custom_osd.doModal()
        
    except Exception as e:
        xbmc.log(f"[Gronkh.tv] Error processing chapters: {str(e)}", xbmc.LOGERROR)
    
    xbmc.log("[Gronkh.tv] Video playback ended", xbmc.LOGINFO)

def seconds_to_time(s):
    h = int(s / 60 / 60)
    m = int((s / 60) % 60)
    s = int(s % 60)
    return f'{h}:{m:02d}:{s:02d}'

def router(paramstring):
    xbmc.log(f"[Gronkh.tv] Router called with params: {paramstring}", xbmc.LOGINFO)
    params = dict(parse_qsl(paramstring))

    action_handlers = {
        'listing': handle_listing,
        'play': handle_play
    }

    if params:
        action = params.get('action')
        if action in action_handlers:
            action_handlers[action](params)
        else:
            raise ValueError(f'Invalid action: {action}')
    else:
        list_categories()

    xbmc.log("[Gronkh.tv] Router finished", xbmc.LOGINFO)

def handle_listing(params):
    category = params['category']
    offset = params.get('offset', '0')
    search_str = params.get('search_str', '')
    list_videos(category, offset, search_str)

def handle_play(params):
    video = params['video']
    play_video(get_playlist_url(video), video)

if __name__ == "__main__":
    #set up headers for https requests
    opener = build_opener()
    opener.addheaders = [("User-Agent",      _UA),
                         ("Accept-Encoding", "identity"),
                         ("Accept-Charset",  "utf-8")]
    install_opener(opener)

    # Call the router function and pass the plugin call parameters to it.
    # We use string slicing to trim the leading '?' from the plugin call paramstring
    router(sys.argv[2][1:])
