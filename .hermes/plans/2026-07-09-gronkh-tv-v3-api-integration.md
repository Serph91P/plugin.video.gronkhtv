# GronkhTV v3 API Integration Plan

> For Hermes: Use `subagent-driven-development` or manual implementation task by task. Keep the change surgical and verify each task locally.

**Goal:** Replace the broken `api.gronkh.tv/v1` integration with the live `backend.gronkh.tv/v3` API while preserving the current Kodi UI features.

**Architecture:** Add a small API adapter module that normalizes v3 responses into the current internal video shape first. Then migrate existing list, detail, chapter, category, and playback code to the adapter without changing the Kodi routing surface in one large step.

**Tech Stack:** Kodi Python addon, Python stdlib `urllib`, Kodi `xbmcgui` InfoTag API, HLS playlist URLs from GronkhTV backend.

---

## Current findings

### Repo and code structure

- Local repo: `/opt/data/workspace/github_repos/plugin.video.gronkhtv-v3-api`
- Current branch: `feat/gronkhtv-v3-api`, based on `origin/develop`
- Addon entry: `addon.py` delegates to `resources/lib/plugin.py` via `run()`.
- All core behavior is currently in `resources/lib/plugin.py`.
- Graphify query confirmed the main graph nodes and routing functions in `resources/lib/plugin.py`.
- Baseline scaffold mirrors the v2.1.0 entrypoint refactor without metadata, icon, or API behavior changes.
- `python3 -m py_compile addon.py resources/lib/plugin.py` currently passes.

### Current v1 usage and breakage

The addon still uses `https://api.gronkh.tv/v1/...` directly in these seams:

| Existing function | Lines | Current endpoint | Current expected shape |
|---|---:|---|---|
| `get_video_info` | `plugin.py:43` | `/v1/video/info?episode=N` | JSON object with `title`, `source_length`, `views`, `preview_url`, `chapters`, `previous`, `next` |
| `get_chapters` | `plugin.py:60` | `/v1/video/info?episode=N` | `chapters` field on same object |
| `get_playlist_url` | `plugin.py:180` | `/v1/video/playlist?episode=N` | JSON object with `playlist_url` |
| `get_videos` recent | `plugin.py:185` | `/v1/video/discovery/recent` | `{ discovery: [...] }` |
| `get_videos` views | `plugin.py:185` | `/v1/video/discovery/views` | `{ discovery: [...] }` |
| `get_videos` all/search | `plugin.py:185` | `/v1/search?...` | `{ results: { videos: [...] } }` |
| `get_all_games` | `plugin.py:130` | `/v1/search?...` plus per video `/info` | Video list plus chapter game data |

Live check result: all tested `api.gronkh.tv/v1` requests return Cloudflare HTTP 526 invalid SSL certificate. The current integration is not reliable as a runtime dependency.

### Discovered v3 API behavior

Source: live website JS environment and direct HTTP checks against `https://backend.gronkh.tv/v3`.

| Feature | v3 endpoint | Method | Verified |
|---|---|---|---|
| Newest videos | `/videos/discovery/newest` | GET | 200, returns `{ data: [...] }` |
| Hot videos | `/videos/discovery/hot` | GET | 200, returns `{ data: [...] }` |
| Random videos | `/videos/discovery/random` | GET | 200, returns `{ data: [...] }` |
| Most viewed videos | `/videos/discovery/most-viewed` | GET | 200, returns `{ data: [...] }` |
| Video by episode | `/videos/episode/1105` | GET | 200, returns `{ data: { ... } }` |
| Playlist | `/videos/{uuid}/playlist` | GET | 200, returns `application/x-mpegURL` directly |
| Video search | `/videos/search` | POST | 200, accepts JSON body such as `{ "query": "demo" }` |
| Categories search | `/categories/search` | POST | 200, accepts JSON body such as `{ "query": "minecraft" }` |
| Category by slug | `/categories/3726-just-chatting` | GET | 200, returns `{ data: { ... } }` |
| Category videos | `/categories/3726-just-chatting/videos` | GET | 200, returns `{ data: [...] }` |
| Live streams | `/promoted/streams` | GET | 200, returns Twitch stream objects |

Important v3 shape differences:

- Most response payloads wrap content in `data`.
- Video ID is a UUID string. Episode remains a numeric field.
- Playlist URL is supplied as `video["urls"]["playlist"]`, and that URL returns the M3U8 body directly.
- Duration and resolution live in `video["meta"]`, for example `duration`, `width`, `height`, `fps`.
- Thumbnail is `video["urls"]["thumbnail"]`.
- Chapters use `category` instead of the old `game` shape.
- Category objects have `title`, `slug`, `urls.boxarts`, `videos_count`, and `chapters_count`.

---

## Normalization target

Create one internal shape so most existing UI code can stay stable:

```python
{
    "id": "cbe1afd0-c240-4c74-ba0a-0307a093ff9b",
    "episode": 1105,
    "title": "...",
    "created_at": "2026-07-05T16:04:45.000000Z",
    "video_length": 42823,
    "source_length": 42823,
    "source_width": 1664,
    "source_height": 936,
    "source_fps": 60,
    "views": 16113,
    "preview_url": "https://02.cdn.vod.farm/preview/...jpg",
    "playlist_url": "https://backend.gronkh.tv/v3/videos/{uuid}/playlist",
    "tags": [{"title": "..."}],
    "chapters": [
        {
            "title": "...",
            "offset": 0,
            "category": {"title": "Just Chatting", "slug": "3726-just-chatting"},
            "game": {"title": "Just Chatting", "id": "019e...", "slug": "3726-just-chatting"}
        }
    ],
    "previous": {...} or None,
    "next": {...} or None
}
```

The duplicate `game` alias is intentional for the first migration. It keeps existing `list_videos`, `show_video_details`, and game filtering code working while we later rename the UI from games to categories if desired.

---

## Task 1: Add an API adapter module

**Objective:** Put all v3 HTTP, JSON parsing, and response normalization in one file.

**Files:**

- Create: `resources/lib/gronkhtv_api.py`
- Modify later: `resources/lib/plugin.py`
- Test later: `tests/test_gronkhtv_api.py`

**Implementation notes:**

- Use stdlib `urllib.request`, not `requests`, to avoid adding a Kodi dependency.
- Central constants:

```python
API_BASE = "https://backend.gronkh.tv/v3"
DEFAULT_HEADERS = {
    "User-Agent": _UA,
    "Accept": "application/json",
}
```

- Functions to add:
  - `get_json(path, method="GET", payload=None)`
  - `post_json(path, payload)`
  - `unwrap_data(payload)`
  - `normalize_video(video)`
  - `normalize_chapter(chapter)`
  - `video_by_episode(episode)`
  - `playlist_url_for_video(video)`
  - `playlist_url_for_episode(episode)`
  - `discovery(kind)`
  - `search_videos(query=None, page=None)`
  - `search_categories(query=None, page=None)`
  - `category_videos(slug, page=None)`
  - `live_streams()`

**Verification:**

```bash
python3 -m py_compile resources/lib/gronkhtv_api.py
```

Expected: exits 0.

---

## Task 2: Add unit tests for normalization

**Objective:** Lock down v3 to legacy internal mapping before touching plugin behavior.

**Files:**

- Create: `tests/test_gronkhtv_api.py`
- Optional create: `tests/fixtures/v3_video_episode_1105.json`

**Test cases:**

1. `normalize_video` maps:
   - `meta.duration` to `video_length` and `source_length`
   - `urls.thumbnail` to `preview_url`
   - `urls.playlist` to `playlist_url`
   - `meta.width`, `meta.height`, `meta.fps` to source fields
2. `normalize_chapter` maps:
   - v3 `category` to both `category` and temporary legacy `game`
3. `unwrap_data` accepts both `{ "data": ... }` and raw arrays for endpoints like `/promoted/streams`.
4. Missing `meta`, `urls`, `tags`, or `chapters` returns safe defaults.

**Verification:**

```bash
python3 -m pytest tests/test_gronkhtv_api.py -q
```

Expected after implementation: all tests pass.

---

## Task 3: Migrate video details and chapters

**Objective:** Make `get_video_info` and `get_chapters` use the adapter.

**Files:**

- Modify: `resources/lib/plugin.py:43-80`

**Changes:**

- Import the adapter:

```python
from gronkhtv_api import video_by_episode
```

- Replace direct v1 `urlopen` calls in `get_video_info` with `video_by_episode(episode)`.
- Keep existing `lru_cache` and `video_info_cache` for now.
- Make `get_chapters` read `info.get("chapters", [])` only.

**Verification:**

```bash
python3 -m py_compile addon.py resources/lib/plugin.py resources/lib/gronkhtv_api.py
```

Expected: exits 0.

---

## Task 4: Migrate playlist playback

**Objective:** Make playback use the v3 playlist URL.

**Files:**

- Modify: `resources/lib/plugin.py:180-183`
- Verify call sites: `handle_play`, `handle_play_resume`, `handle_play_from_start`, `jump_to_chapter`, `monitor_playback`

**Changes:**

- Replace `get_playlist_url(episode)` body with adapter `playlist_url_for_episode(episode)`.
- Do not parse JSON for playlist. The v3 playlist endpoint returns M3U8 directly and Kodi can play the URL.
- Keep `get_playlist_url` function name to minimize diff.

**Verification:**

Manual endpoint check:

```bash
python3 - <<'PY'
from resources.lib.gronkhtv_api import playlist_url_for_episode
url = playlist_url_for_episode(1105)
print(url)
assert url.startswith("https://backend.gronkh.tv/v3/videos/")
assert url.endswith("/playlist")
PY
```

Expected: URL printed and assertions pass.

---

## Task 5: Migrate list categories backed by discovery and search

**Objective:** Replace v1 list endpoints in `get_videos` while preserving existing category names.

**Files:**

- Modify: `resources/lib/plugin.py:185-232`

**Mapping:**

| Current category | Current behavior | New adapter call |
|---|---|---|
| Neuste Streams | Latest VODs | `discovery("newest")` |
| Meist gesehen | All time top views | `discovery("most-viewed")` |
| Alle Streams | Paginated VOD list | `search_videos(page=...)` or `discovery("newest")` until pagination payload is confirmed |
| Suche | Full text search | `search_videos(query=search_query)` |
| Favoriten | Local file | unchanged |
| Game/category videos | Episodes by category | `category_videos(slug)` after Task 6 |

**Open point:** `/videos/search` supports POST and pagination metadata. Browser confirmed the site calls this endpoint, but the exact sort payload should be checked in DevTools or by observing request body before finalizing `Alle Streams` paging. Safe first version can use `discovery("newest")` and keep the existing `... mehr` item disabled until pagination is verified.

**Verification:**

- Mock adapter calls in unit tests for `get_videos`.
- Compile plugin.
- If running in Kodi, open all top-level categories and verify that each directory fills without router errors.

---

## Task 6: Replace expensive game scraping with v3 categories

**Objective:** Stop crawling 100 videos and calling per episode detail just to build the game list.

**Files:**

- Modify: `resources/lib/plugin.py:128-172`
- Modify: `resources/lib/plugin.py:268-298`
- Modify: `resources/lib/plugin.py:726-777`

**New behavior:**

- Rename internal concept from games to categories in code if the diff stays small. If not, keep public `list_games` function name for now and feed it normalized category data.
- Use `search_categories()` for the top level category list.
- Use `category_videos(slug)` for selecting a category.
- Store category slug instead of numeric `game_id` in Kodi URLs:

```python
url = get_url(action="list_category_videos", category_slug=category["slug"], category_title=category["title"])
```

**Compatibility note:** Kodi URLs currently use `game_id`. This can be changed because it is only internal plugin routing.

**Verification:**

- Unit test URL generation for category item includes `category_slug`.
- Unit test selected category calls `category_videos(slug)`.
- Compile plugin.

---

## Task 7: Add live streams category

**Objective:** Implement the live streams feature shown in the Kodinerds post and supported by v3 `/promoted/streams`.

**Files:**

- Modify: `resources/lib/plugin.py:24-30`
- Modify: `resources/lib/plugin.py:652-666`
- Add helper functions near existing listing helpers

**Behavior:**

- Add category label `Live-Streams`.
- Fetch `live_streams()`.
- Render entries with Twitch thumbnail, viewer count, game name, and title.
- Playback target can initially be `https://www.twitch.tv/{user_login}` if Kodi supports external resolution in this context, or use a plugin URL with a clear notification if Twitch playback requires a separate addon.

**Verification:**

- Compile plugin.
- In Kodi, open `Live-Streams`; verify entries appear for `gronkh` and `gronkhtv` when live.

---

## Task 8: Add smoke tests for live API contracts

**Objective:** Detect future API drift quickly without needing Kodi.

**Files:**

- Create: `tests/test_gronkhtv_api_live.py`

**Tests:**

- Mark as live or skip unless `GRONKHTV_LIVE_TESTS=1` is set.
- Check:
  - `video_by_episode(1105)` returns episode `1105` with a `playlist_url`.
  - `discovery("newest")` returns at least one video with `episode` and `title`.
  - `playlist_url_for_episode(1105)` returns a backend playlist URL.

**Verification:**

```bash
GRONKHTV_LIVE_TESTS=1 python3 -m pytest tests/test_gronkhtv_api_live.py -q
```

Expected: all live smoke tests pass when network is available.

---

## Task 9: Final validation

**Objective:** Verify the addon remains syntactically valid and the migrated API works.

**Commands:**

```bash
python3 -m py_compile addon.py resources/lib/*.py
python3 -m pytest -q
GRONKHTV_LIVE_TESTS=1 python3 -m pytest tests/test_gronkhtv_api_live.py -q
```

If no pytest setup exists yet, add the tests and minimal Kodi mocks first, or run the adapter tests that do not import Kodi modules.

**Kodi manual smoke:**

1. Open addon root.
2. Open newest videos.
3. Open most viewed videos.
4. Search for `demo`.
5. Open category search/list.
6. Start episode 1105 and verify HLS playback starts.
7. Use chapter jump context menu.
8. Add and remove a favorite.

---

## Risks and decisions

- `api.gronkh.tv/v1` is currently broken with HTTP 526, so v3 migration should not preserve v1 as primary.
- v3 appears undocumented. Keep adapter isolated because endpoint names or payloads can drift.
- `/videos/search` sort and pagination need one more request-body capture before restoring exact `Alle Streams` paging.
- Playlist endpoint returns M3U8 directly. Do not expect JSON from v3 playback.
- Chapters are category based now. Preserve a temporary `game` alias to reduce the first diff.
- The current addon uses Kodi 20 plus typed InfoTag APIs. Do not add `setInfo` back.

## Suggested implementation order

1. Adapter plus pure normalization tests.
2. Details, chapters, and playlist.
3. Discovery and search video listings.
4. Category listing and category videos.
5. Live streams.
6. Manual Kodi smoke.
