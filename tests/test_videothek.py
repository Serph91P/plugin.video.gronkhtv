import types
from urllib.parse import parse_qs, urlsplit

import pytest

from test_plugin_playback import plugin  # noqa: F401
import gronkhtv_api as api
from account import AuthenticationError

A = '01a1efd4-4702-7096-b76e-1132060b433d'
B = '01a1f3b4-fbbb-7b68-9bed-3bbc6e3b2819'
SERIES = '01a1efd4-47c1-72e2-9245-0429a854fa95'
SEASON = '01a1efd4-4c1f-72f6-86ba-1f4400f748c7'
SERIES_B = '01a1f3b4-fbbb-7b68-9bed-3bbc6e3b2820'
SEASON_B = '01a1f3b4-fbbb-7b68-9bed-3bbc6e3b2821'


def video(video_id, kind='letsplay', tag_id=22):
    series, season = (SERIES_B, SEASON_B) if video_id == B else (SERIES, SEASON)
    return {'id': video_id, 'title': video_id, 'tags': [{'tag_id': tag_id, 'title': 'Demo'}],
            'meta': {'episode': '1', 'type': kind, 'season': {'id': season, 'title': 'Season 1',
                     'meta': {'playlist_id': series}}}}


def page(rows, current, last):
    return {'data': rows, 'meta': {'current_page': current, 'last_page': last}}


def test_tag_filter_verifies_rows_and_continues_through_empty_pages(monkeypatch):
    responses = {
        1: page([video(A, tag_id=1), video(B, kind='stream')], 1, 3),
        2: page([video(A), video(B), None], 2, 3),
        3: page([video(A, tag_id=1)], 3, 3),
    }
    calls = []

    def post(path, payload):
        calls.append(payload)
        assert path == '/videos/search'
        return responses[payload['page']]

    monkeypatch.setattr(api, 'post_json', post)
    assert api.catalog_videos(page=1, tag=22, query='demo', order='views') == ([], 2)
    assert calls == [{'page': 1, 'tags': ['22'], 'query': 'demo', 'order': 'views', 'dir': 'desc'}]
    rows, more = api.catalog_videos(page=2, tag=22)
    assert [row['id'] for row in rows] == [A, B]
    assert more == 3
    assert api.catalog_videos(page=3, tag=22) == ([], None)


def test_catalog_series_game_relation_and_season_paging(monkeypatch):
    def get(path):
        return {
            '/playlists?page=1': page([
                {'id': SERIES, 'title': 'Series', 'category': {'title': 'Game', 'meta': {'slug': '0-game'}}},
                {'id': SERIES, 'title': 'Series duplicate'},
            ], 1, 1),
            f'/playlists/{SERIES}/seasons': {'data': [{'id': SEASON, 'title': 'Season'}]},
            f'/playlists/{SERIES}/seasons/{SEASON}/videos?page=2': page([video(A)], 2, 2),
            '/tags': {'data': [{'tag_id': 22, 'title': 'Demo'}, {'id': A, 'title': 'Invalid'}]},
        }[path]

    monkeypatch.setattr(api, 'get_json', get)
    assert api.catalog_series(1) == ([{'id': SERIES, 'title': 'Series', 'game': 'Game', 'slug': '0-game'}], None)
    assert api.catalog_seasons(SERIES) == [{'id': SEASON, 'title': 'Season'}]
    rows, more = api.catalog_videos(page=2, series=SERIES, season=SEASON)
    assert [v['id'] for v in rows] == [A]
    assert more is None
    assert api.catalog_tags() == [(22, 'Demo')]


@pytest.fixture
def kodi(plugin, monkeypatch):
    class Item:
        def __init__(self, label='', path=''):
            self.label, self.path = label, path
            self.properties = {}
            self.context = []

        def getVideoInfoTag(self):
            return self

        def addContextMenuItems(self, items):
            self.context = items

        def setEpisode(self, episode):
            assert type(episode) is int

        def setGenres(self, genres):
            assert all(isinstance(genre, str) for genre in genres)
            self.genres = genres

        def setProperty(self, key, value):
            self.properties[key] = value

        def __getattr__(self, name):
            return lambda *args: None

    events = types.SimpleNamespace(items=[], playback=[], notifications=[], details=[], ended=[])
    monkeypatch.setattr(plugin.xbmcgui, 'ListItem', Item)
    monkeypatch.setattr(plugin.xbmcgui, 'Dialog', lambda: types.SimpleNamespace(
        notification=lambda *args: events.notifications.append(args),
        textviewer=lambda *args: events.details.append(args),
    ))
    for name, fn in {
        'addDirectoryItem': lambda _, url, item, folder: events.items.append((url, item, folder)),
        'endOfDirectory': lambda *a, **kw: events.ended.append(kw),
        'setResolvedUrl': lambda _, ok, listitem: events.playback.append((ok, listitem)),
        'setPluginCategory': lambda *args: None, 'setContent': lambda *args: None,
        'addSortMethod': lambda *args: None,
    }.items():
        monkeypatch.setattr(plugin.xbmcplugin, name, fn, raising=False)
    return events


def test_main_menu_and_videothek_keep_uuid_links_and_more_pages(plugin, kodi, monkeypatch):
    plugin.router('')
    assert any('action=videothek' in url for url, _, _ in kodi.items)
    kodi.items.clear()
    monkeypatch.setattr(api, 'post_json', lambda path, payload: page([video(A), video(B)], 1, 2))
    plugin.router('action=videothek_videos&tag=22&query=demo&order=views')
    playable = [(parse_qs(urlsplit(url).query), item) for url, item, folder in kodi.items if not folder]
    assert [params['id'][0] for params, _ in playable] == [A, B]
    assert all(f'id={params["id"][0]}' in item.context[0][1] for params, item in playable)
    assert any('page=2' in url and 'tag=22' in url and 'query=demo' in url and 'order=views' in url
               for url, _, folder in kodi.items if folder)
    assert kodi.ended[-1]['cacheToDisc'] is False


def test_game_picker_opens_its_series_even_without_category_videos(plugin, kodi, monkeypatch):
    def get(path):
        return {
            '/playlists?page=1': page([
                {'id': SERIES, 'title': 'Series', 'category': {'title': 'Game', 'meta': {'slug': '0-game'}}},
                {'id': B, 'title': 'Other', 'category': {'title': 'Else', 'meta': {'slug': 'else'}}},
            ], 1, 1),
            f'/playlists/{SERIES}/seasons': {'data': [{'id': SEASON, 'title': 'Season'}]},
            f'/playlists/{SERIES}/seasons/{SEASON}/videos?page=1': page([video(A)], 1, 1),
        }[path]

    monkeypatch.setattr(api, 'get_json', get)
    plugin.router('action=videothek_games')
    selected = next(url for url, item, _ in kodi.items if item.label == 'Game')
    for label in ('Series', 'Season', A):
        kodi.items.clear()
        plugin.router(urlsplit(selected).query)
        assert [item.label for _, item, _ in kodi.items] == [label]
        selected = kodi.items[0][0]
    assert 'action=videothek_play' in selected


def test_game_picker_reaches_series_before_and_after_the_page_where_selected(plugin, kodi, monkeypatch):
    game = {'title': 'Game', 'meta': {'slug': '0-game'}}
    other = {'id': B, 'title': 'Other', 'category': {'title': 'Else', 'meta': {'slug': 'else'}}}
    pages = {
        '/playlists?page=1': page([{'id': SERIES, 'title': 'Early', 'category': game}, other], 1, 3),
        '/playlists?page=2': page([other], 2, 3),
        '/playlists?page=3': page([{'id': SERIES_B, 'title': 'Late', 'category': game}], 3, 3),
    }
    requests = []

    def get(path):
        requests.append(path)
        return pages[path]

    def open_link(url):
        kodi.items.clear()
        plugin.router(urlsplit(url).query)
        assert kodi.ended[-1]['succeeded']

    monkeypatch.setattr(api, 'get_json', get)
    plugin.router('action=videothek_games&page=3')
    open_link(next(url for url, item, _ in kodi.items if item.label == 'Game'))
    assert [item.label for url, item, _ in kodi.items if 'action=videothek_seasons' in url] == ['Early']
    assert requests == ['/playlists?page=3', '/playlists?page=1']
    open_link(next(url for url, _, _ in kodi.items if 'page=2' in url))
    assert len(kodi.items) == 1  # Empty filtered page still has a Next link.
    open_link(kodi.items[0][0])
    assert [item.label for _, item, _ in kodi.items] == ['Late']
    assert requests[2:] == ['/playlists?page=2', '/playlists?page=3']


@pytest.mark.parametrize('bad_offset', [-1, 'nan', 'inf', 31536001, True, 'not seconds', None])
def test_latest_uploads_omits_bad_chapter_and_still_renders_neighbor(plugin, kodi, monkeypatch, bad_offset):
    first = {'title': 'Current', 'meta': {'type': 'stream', 'episode': '1135'}}
    second = {'title': 'Neighbor', 'meta': {'type': 'stream', 'episode': '1136'}}
    responses = {
        '/videos/discovery/newest': {'data': [first, second]},
        '/videos/episode/1135': {'data': dict(first, chapters=[
            {'title': 'Bad chapter', 'start_offset': bad_offset},
            {'title': 'Good chapter', 'offset': '42', 'category': {'title': 17}},
        ])},
        '/videos/episode/1136': {'data': second},
    }
    monkeypatch.setattr(api, 'get_json', responses.__getitem__)
    plugin.router(f'action=listing&category={plugin._CATEGORIES[0]}')
    assert [item.label for _, item, _ in kodi.items] == ['Current', 'Neighbor']
    chapter_links = [url for _, url in kodi.items[0][1].context if 'action=jump_to_chapter' in url]
    assert chapter_links == ['RunPlugin(plugin://plugin.video.gronkhtv/?action=jump_to_chapter&episode=1135&offset=42)']
    assert len(kodi.ended) == 1


def test_latest_uploads_omits_bad_tag_title_and_still_renders_neighbor(plugin, kodi, monkeypatch):
    first = {'title': 'Current', 'meta': {'type': 'stream', 'episode': '1135'},
             'tags': [{'title': 17}, {'title': 'Valid genre'}]}
    second = {'title': 'Neighbor', 'meta': {'type': 'stream', 'episode': '1136'}}
    responses = {
        '/videos/discovery/newest': {'data': [first, second]},
        '/videos/episode/1135': {'data': first},
        '/videos/episode/1136': {'data': second},
    }
    monkeypatch.setattr(api, 'get_json', responses.__getitem__)
    plugin.router(f'action=listing&category={plugin._CATEGORIES[0]}')
    assert [item.label for _, item, _ in kodi.items] == ['Current', 'Neighbor']
    assert kodi.items[0][1].genres == ["Streams und Let's Plays", 'Valid genre']
    assert len(kodi.ended) == 1


def test_mixed_stream_search_keeps_next_page_after_filtering(plugin, kodi, monkeypatch):
    monkeypatch.setattr(api, 'post_json', lambda path, payload: page([video(A)], 1, 2))
    plugin.router(f'action=listing&category={plugin._CATEGORIES[3]}&search_str=demo')
    assert any('offset=20' in url and 'search_str=demo' in url for url, _, folder in kodi.items if folder)


def test_empty_filtered_page_is_not_end_of_results(plugin, kodi, monkeypatch):
    monkeypatch.setattr(api, 'post_json', lambda path, payload: page([video(A, tag_id=1)], 1, 2))
    plugin.router('action=videothek_videos&tag=22')
    assert len(kodi.items) == 1
    assert 'page=2' in kodi.items[0][0] and kodi.items[0][2]


@pytest.mark.parametrize('failure', [None, 'expired', 'denied', 'wrong-id', 'not-hls', 'missing-cookie'])
def test_videothek_uuid_details_and_protected_playback(plugin, kodi, monkeypatch, failure):
    requests = []

    class Session:
        def current_user(self):
            if failure == 'expired':
                raise AuthenticationError('session secret', status=401)
            return {'id': 'test-user'}

        def request_json(self, path):
            requests.append(path)
            assert path in (f'/videos/{A}', f'/videos/{B}')
            return {'data': video(A if failure == 'wrong-id' else path.rsplit('/', 1)[-1])}

        def request_bytes(self, path):
            requests.append(path)
            assert path == f'/videos/{B}/playlist'
            if failure == 'denied':
                raise AuthenticationError('playlist secret', status=403)
            return b'not hls' if failure == 'not-hls' else b'#EXTM3U\n'

        def request_headers(self, url):
            assert url == f'{api.API_BASE}/videos/{B}/playlist'
            if failure == 'missing-cookie':
                return {'User-Agent': 'test'}
            return {'Cookie': 'synthetic_session=1'}

    monkeypatch.setattr(plugin, '_account_session', Session())
    monkeypatch.setattr(plugin.os.path, 'exists', lambda path: True)
    monkeypatch.setattr(plugin.inputstreamhelper, 'Helper', lambda _: types.SimpleNamespace(
        check_inputstream=lambda: True, inputstream_addon='inputstream.adaptive'))
    monkeypatch.setattr(api, 'urlopen', lambda *a, **kw: pytest.fail('guest fallback'))
    if failure is None:
        plugin.router(f'action=videothek_details&id={A}')
        assert kodi.details[0][0] == A
    plugin.router(f'action=videothek_play&id={B}')
    success, item = kodi.playback[-1]
    assert success is (failure is None)
    if failure is None:
        assert requests == [f'/videos/{A}', f'/videos/{B}', f'/videos/{B}/playlist']
        assert item.path == f'{api.API_BASE}/videos/{B}/playlist'
        assert 'synthetic_session' in item.properties['inputstream.adaptive.manifest_headers']
        assert 'inputstream.adaptive.stream_headers' not in item.properties
    else:
        assert not item.path
        assert kodi.notifications and 'secret' not in repr(kodi.notifications)
