import json
import math
from uuid import UUID
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlencode, urlsplit
from urllib.request import Request, urlopen

from account import AuthenticationError

API_BASE = "https://backend.gronkh.tv/v3"
_TIMEOUT = 10
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
_MAX_URL_DECODE_ROUNDS = 4
_MAX_CHAPTER_ROUTE_OFFSET = 31536000
DEFAULT_HEADERS = {
    "User-Agent": _UA,
    "Accept": "application/json",
}
_account_session = None


def configure_account_session(session):
    global _account_session
    _account_session = session


def get_json(path, method="GET", payload=None):
    path = _relative_api_path(path)
    if _account_session is not None:
        try:
            return _account_session.request_json(path, method=method, payload=payload)
        except AuthenticationError:
            pass

    url = _make_url(path)
    data = None
    headers = dict(DEFAULT_HEADERS)

    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    request = Request(url, data=data, headers=headers, method=method)
    try:
        with urlopen(request, timeout=_TIMEOUT) as response:
            content = response.read().decode("utf-8")
    except (HTTPError, URLError, TimeoutError) as exc:
        raise RuntimeError(f"GronkhTV API request failed for {url}: {exc}") from exc

    try:
        return json.loads(content)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"GronkhTV API returned invalid JSON for {url}") from exc


def post_json(path, payload):
    return get_json(path, method="POST", payload=payload)


def unwrap_data(payload):
    if isinstance(payload, dict) and "data" in payload:
        return payload["data"]
    return payload


def normalize_video(video):
    if not isinstance(video, dict):
        video = {}

    meta = _mapping(video.get("meta"))
    urls = _mapping(video.get("urls"))

    normalized = {
        "id": video.get("id"),
        "episode": _episode_number(video, meta),
        "title": _text(video.get("title")),
        "created_at": _text(video.get("created_at")),
        "video_length": _as_int(meta.get("duration"), 0),
        "source_length": _as_int(meta.get("duration"), 0),
        "source_width": _as_int(meta.get("width"), 0),
        "source_height": _as_int(meta.get("height"), 0),
        "source_fps": _as_int(meta.get("fps"), 0),
        "views": _as_int(video.get("views"), 0),
        "preview_url": _text(urls.get("thumbnail")),
        "playlist_url": _text(urls.get("playlist")),
        "tags": _rows(video.get("tags")),
        "chapters": [],
        "previous": _normalize_related_video(video.get("previous")),
        "next": _normalize_related_video(video.get("next")),
    }
    for row in _rows(video.get("chapters")):
        chapter = normalize_chapter(row)
        if chapter is not None:
            normalized["chapters"].append(chapter)
    progress = _normalize_watch_progress(video.get("watch_progress"))
    if progress is not None:
        normalized["watch_progress"] = progress
    return normalized


def non_negative_seconds(value, name):
    if isinstance(value, bool):
        raise ValueError(f"{name} must be finite non-negative seconds")
    try:
        parsed = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be finite non-negative seconds") from exc
    if not math.isfinite(parsed) or not 0 <= parsed <= _MAX_CHAPTER_ROUTE_OFFSET:
        raise ValueError(f"{name} must be finite non-negative seconds")
    return parsed


def normalize_chapter(chapter):
    if not isinstance(chapter, dict):
        chapter = {}

    category = _normalize_category(chapter.get("category") or chapter.get("game"))
    title = _text(chapter.get("title")) or category["title"] or "Unbenanntes Kapitel"
    try:
        offset = non_negative_seconds(chapter.get("start_offset", chapter.get("offset", 0)), "chapter offset")
    except ValueError:
        return None
    return {
        "title": title,
        "offset": int(offset),
        "category": category,
        "game": _category_to_legacy_game(category),
    }


def video_by_episode(episode):
    episode = _positive_int(episode, "episode")
    video = normalize_video(unwrap_data(get_json(f"/videos/episode/{episode}")))
    if video["episode"] != episode:
        raise ValueError("GronkhTV returned an invalid stream episode")
    return video


def playlist_url_for_video(video):
    normalized = normalize_video(video)
    if normalized["playlist_url"]:
        return _v3_url(normalized["playlist_url"])
    video_id = normalized.get("id")
    if not video_id:
        return ""
    try:
        video_id = str(UUID(str(video_id)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("Playlist URL must use the GronkhTV v3 origin") from exc
    return f"{API_BASE}/videos/{video_id}/playlist"


def playlist_url_for_episode(episode):
    return playlist_url_for_video(video_by_episode(episode))


def discovery(kind):
    return _stream_rows(get_json(f"/videos/discovery/{kind}"))


def search_videos(query=None, page=None):
    payload = _search_payload(query=query, page=page)
    return _stream_rows(post_json("/videos/search", payload), page)


def search_categories(query=None, page=None):
    payload = _search_payload(query=query, page=page)
    return [
        _normalize_category(category)
        for category in unwrap_data(post_json("/categories/search", payload)) or []
    ]


def category_videos(slug, page=None):
    path = f"/categories/{slug}/videos"
    if page is not None:
        path = f"{path}?{urlencode({'page': page})}"
    return _stream_rows(get_json(path), page)


class _StreamRows(list):
    """Keep server paging independent of the number of usable stream rows."""

    def __init__(self, videos, has_more):
        super().__init__(videos)
        self.has_more = has_more


def _stream_rows(payload, page=None):
    videos = (normalize_video(row) for row in _rows(unwrap_data(payload)))
    meta = _mapping(_mapping(payload).get("meta"))
    has_more = _next_page(payload, page) is not None if page and "last_page" in meta else None
    return _StreamRows((video for video in videos if video["episode"] is not None), has_more)


def _rows(value):
    return [row for row in value if isinstance(row, dict)] if isinstance(value, list) else []


def _mapping(value):
    return value if isinstance(value, dict) else {}


def _text(value):
    return value if isinstance(value, str) else ""


def _episode_number(video, meta):
    if meta.get("type", "stream") != "stream":
        return None
    for value in (meta.get("episode"), video.get("episode")):
        try:
            return _positive_int(value, "episode")
        except ValueError:
            continue
    return None


def live_streams():
    return [
        normalize_live_stream(stream)
        for stream in unwrap_data(get_json("/promoted/streams")) or []
    ]


def _uuid(value):
    canonical = str(UUID(str(value)))
    if not isinstance(value, str) or value.lower() != canonical:
        raise ValueError("GronkhTV requires a video or series UUID")
    return canonical


def _optional_uuid(value):
    try:
        return _uuid(value)
    except ValueError:
        return None


def _catalog_video(row):
    meta = _mapping(row.get("meta"))
    video_id = _optional_uuid(row.get("id"))
    if not video_id or meta.get("type") != "letsplay":
        return None
    video = normalize_video(row)
    video["id"] = video_id
    season = _mapping(meta.get("season"))
    video["series_id"] = _optional_uuid(_mapping(season.get("meta")).get("playlist_id"))
    video["series_title"] = _text(season.get("title"))
    video["description"] = _text(row.get("description"))
    return video


def _next_page(response, page):
    meta = _mapping(_mapping(response).get("meta"))
    current = _as_int(meta.get("current_page"), 0)
    last = _as_int(meta.get("last_page"), 0)
    return current + 1 if current == page and current < last else None


def catalog_videos(page=1, query=None, tag=None, order="relevancy", series=None, season=None):
    page = _positive_int(page, "page")
    if tag is not None:
        tag = _positive_int(tag, "tag")
    if order not in ("relevancy", "views"):
        raise ValueError("Unsupported catalog order")
    if series or season:
        series, season = _uuid(series), _uuid(season)
        response = get_json(f"/playlists/{series}/seasons/{season}/videos?page={page}")
    else:
        payload = _search_payload(query=query, page=page)
        payload.update(order=order, dir="desc")
        if tag is not None:
            payload["tags"] = [str(tag)]
        response = post_json("/videos/search", payload)
    videos = []
    for row in _rows(unwrap_data(response)):
        # The search API also returns unrelated rows when tags are supplied.
        if tag is not None and not any(type(t.get("tag_id")) is int and t["tag_id"] == tag
                                       for t in _rows(row.get("tags"))):
            continue
        video = _catalog_video(row)
        if video:
            videos.append(video)
    return videos, _next_page(response, page)


def catalog_series(page):
    page = _positive_int(page, "page")
    response = get_json(f"/playlists?page={page}")
    series = {}
    for row in _rows(unwrap_data(response)):
        series_id = _optional_uuid(row.get("id"))
        title = _text(row.get("title"))
        if not series_id or not title or row.get("is_watch_later"):
            continue
        category = _mapping(row.get("category"))
        series.setdefault(series_id, {
            "id": series_id, "title": title, "game": _text(category.get("title")),
            "slug": _text(_mapping(category.get("meta")).get("slug")),
        })
    return list(series.values()), _next_page(response, page)


def catalog_seasons(series):
    return [{"id": season, "title": _text(row.get("title"))}
            for row in _rows(unwrap_data(get_json(f"/playlists/{_uuid(series)}/seasons")))
            if (season := _optional_uuid(row.get("id")))]


def catalog_tags():
    return [(row["tag_id"], row["title"]) for row in _rows(unwrap_data(get_json("/tags")))
            if type(row.get("tag_id")) is int and row["tag_id"] > 0 and _text(row.get("title"))]


def catalog_video_by_id(video_id, session=None):
    video_id = _uuid(video_id)
    path = f"/videos/{video_id}"
    get = session.request_json if session is not None else get_json
    video = _catalog_video(_mapping(unwrap_data(get(path))))
    if not video or video["id"] != video_id:
        raise ValueError("GronkhTV returned a different video")
    return video


def catalog_playlist_path(video_id):
    return f"/videos/{_uuid(video_id)}/playlist"


def normalize_live_stream(stream):
    if not isinstance(stream, dict):
        stream = {}

    user_login = stream.get("user_login", "")
    return {
        "user_id": str(stream.get("user_id") or ""),
        "user_login": user_login,
        "user_name": stream.get("user_name", ""),
        "game_name": stream.get("game_name", ""),
        "title": stream.get("title", ""),
        "viewer_count": _as_int(stream.get("viewer_count"), 0),
        "started_at": stream.get("started_at", ""),
        "thumbnail_url": _thumbnail_from_stream(stream),
        "url": f"https://www.twitch.tv/{user_login}" if user_login else "",
    }


def _make_url(path):
    path = _relative_api_path(path)
    return f"{API_BASE}{path}"


def _relative_api_path(path):
    if not isinstance(path, str) or not path:
        raise ValueError("GronkhTV requests require a relative API path")
    parsed = urlsplit(path)
    decoded_path = _repeated_unquote(parsed.path)
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.fragment
        or "#" in path
        or "\\" in decoded_path
        or "//" in decoded_path
        or decoded_path.count("/") != parsed.path.count("/")
        or any(part == ".." for part in decoded_path.split("/"))
    ):
        raise ValueError("GronkhTV requests require a relative API path")
    normalized_path = parsed.path if parsed.path.startswith("/") else f"/{parsed.path}"
    return normalized_path + (f"?{parsed.query}" if parsed.query else "")


def _v3_url(url):
    if not isinstance(url, str):
        raise ValueError("Playlist URL must use the GronkhTV v3 origin")
    parsed = urlsplit(url)
    decoded_path = _repeated_unquote(parsed.path)
    if (
        parsed.scheme != "https"
        or parsed.netloc != "backend.gronkh.tv"
        or "?" in url
        or "#" in url
        or parsed.fragment
        or "\\" in decoded_path
        or "//" in decoded_path
        or decoded_path.count("/") != parsed.path.count("/")
        or any(part == ".." for part in decoded_path.split("/"))
    ):
        raise ValueError("Playlist URL must use the GronkhTV v3 origin")
    parts = parsed.path.split("/")
    if (
        len(parts) != 5
        or parts[:3] != ["", "v3", "videos"]
        or parts[4] != "playlist"
    ):
        raise ValueError("Playlist URL must use the GronkhTV v3 origin")
    try:
        video_id = str(UUID(parts[3]))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("Playlist URL must use the GronkhTV v3 origin") from exc
    return f"{API_BASE}/videos/{video_id}/playlist"


def _repeated_unquote(value):
    for _ in range(_MAX_URL_DECODE_ROUNDS):
        decoded = unquote(value)
        if decoded == value:
            break
        value = decoded
    return value


def _positive_int(value, name):
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a positive integer")
    try:
        parsed = int(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ValueError(f"{name} must be a positive integer") from exc
    if parsed <= 0 or str(value).strip() not in {str(parsed), f"+{parsed}"}:
        raise ValueError(f"{name} must be a positive integer")
    return parsed


def _search_payload(query=None, page=None):
    payload = {}
    if query:
        payload["query"] = query
    if page is not None:
        payload["page"] = page
    return payload


def _normalize_related_video(video):
    if not isinstance(video, dict):
        return None

    meta = _mapping(video.get("meta"))
    urls = _mapping(video.get("urls"))
    return {
        "id": video.get("id"),
        "episode": _episode_number(video, meta),
        "title": _text(video.get("title")),
        "created_at": _text(video.get("created_at")),
        "video_length": _as_int(meta.get("duration"), 0),
        "source_length": _as_int(meta.get("duration"), 0),
        "source_width": _as_int(meta.get("width"), 0),
        "source_height": _as_int(meta.get("height"), 0),
        "source_fps": _as_int(meta.get("fps"), 0),
        "views": _as_int(video.get("views"), 0),
        "preview_url": urls.get("thumbnail", ""),
        "playlist_url": urls.get("playlist", ""),
        "tags": _rows(video.get("tags")),
        "chapters": [],
        "previous": None,
        "next": None,
    }


def _normalize_category(category):
    if not isinstance(category, dict):
        category = {}

    urls = _mapping(category.get("urls"))
    return {
        "id": category.get("id"),
        "title": _text(category.get("title")),
        "slug": category.get("slug"),
        "urls": urls,
        "videos_count": _as_int(category.get("videos_count"), 0),
        "chapters_count": _as_int(category.get("chapters_count"), 0),
    }


def _category_to_legacy_game(category):
    thumbnail = ""
    urls = _mapping(category.get("urls"))
    boxarts = urls.get("boxarts") or {}
    if isinstance(boxarts, dict):
        thumbnail = boxarts.get("url") or boxarts.get("thumbnail") or ""
    elif isinstance(boxarts, str):
        thumbnail = boxarts

    return {
        "id": category.get("id") or category.get("slug"),
        "title": category.get("title", ""),
        "slug": category.get("slug"),
        "twitch_details": {"thumbnail_url": thumbnail},
    }


def _thumbnail_from_stream(stream):
    thumbnail_url = stream.get("thumbnail_url") or stream.get("thumbnail")
    if thumbnail_url:
        return thumbnail_url

    thumbnail_urls = stream.get("thumbnail_urls") or {}
    if isinstance(thumbnail_urls, dict):
        for key in ("lg", "md", "sm", "xs", "url", "thumbnail", "base"):
            thumbnail_url = thumbnail_urls.get(key)
            if thumbnail_url:
                return thumbnail_url.replace("{width}", "1280").replace(
                    "{height}", "720"
                )
        return ""
    if isinstance(thumbnail_urls, list):
        return next((url for url in thumbnail_urls if url), "")
    if isinstance(thumbnail_urls, str):
        return thumbnail_urls
    return ""


def _normalize_watch_progress(progress):
    if not isinstance(progress, dict):
        return None
    offset = progress.get("offset")
    if isinstance(offset, bool) or not isinstance(offset, (int, float)):
        return None
    if not math.isfinite(offset) or offset < 0:
        return None
    normalized = {"offset": offset}
    for field in ("updated_at", "marked_as_watched", "percentage"):
        if field in progress:
            normalized[field] = progress[field]
    return normalized


def _as_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        try:
            return int(float(value))
        except (TypeError, ValueError, OverflowError):
            return default
