import json
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API_BASE = "https://backend.gronkh.tv/v3"
_TIMEOUT = 10
_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
DEFAULT_HEADERS = {
    "User-Agent": _UA,
    "Accept": "application/json",
}


def get_json(path, method="GET", payload=None):
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

    meta = video.get("meta") or {}
    urls = video.get("urls") or {}
    chapters = video.get("chapters") or []

    return {
        "id": video.get("id"),
        "episode": video.get("episode"),
        "title": video.get("title", ""),
        "created_at": video.get("created_at", ""),
        "video_length": _as_int(meta.get("duration"), 0),
        "source_length": _as_int(meta.get("duration"), 0),
        "source_width": _as_int(meta.get("width"), 0),
        "source_height": _as_int(meta.get("height"), 0),
        "source_fps": _as_int(meta.get("fps"), 0),
        "views": _as_int(video.get("views"), 0),
        "preview_url": urls.get("thumbnail", ""),
        "playlist_url": urls.get("playlist", ""),
        "tags": video.get("tags") or [],
        "chapters": [normalize_chapter(chapter) for chapter in chapters],
        "previous": _normalize_related_video(video.get("previous")),
        "next": _normalize_related_video(video.get("next")),
    }


def normalize_chapter(chapter):
    if not isinstance(chapter, dict):
        chapter = {}

    category = _normalize_category(chapter.get("category") or chapter.get("game"))
    return {
        "title": chapter.get("title", ""),
        "offset": _as_int(chapter.get("offset"), 0),
        "category": category,
        "game": _category_to_legacy_game(category),
    }


def video_by_episode(episode):
    return normalize_video(unwrap_data(get_json(f"/videos/episode/{episode}")))


def playlist_url_for_video(video):
    normalized = normalize_video(video)
    if normalized["playlist_url"]:
        return normalized["playlist_url"]
    video_id = normalized.get("id")
    if not video_id:
        return ""
    return f"{API_BASE}/videos/{video_id}/playlist"


def playlist_url_for_episode(episode):
    return playlist_url_for_video(video_by_episode(episode))


def discovery(kind):
    return [
        normalize_video(video)
        for video in unwrap_data(get_json(f"/videos/discovery/{kind}")) or []
    ]


def search_videos(query=None, page=None):
    payload = _search_payload(query=query, page=page)
    return [
        normalize_video(video)
        for video in unwrap_data(post_json("/videos/search", payload)) or []
    ]


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
    return [normalize_video(video) for video in unwrap_data(get_json(path)) or []]


def live_streams():
    return [
        normalize_live_stream(stream)
        for stream in unwrap_data(get_json("/promoted/streams")) or []
    ]


def normalize_live_stream(stream):
    if not isinstance(stream, dict):
        stream = {}

    user_login = stream.get("user_login", "")
    return {
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
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return f"{API_BASE}{path if path.startswith('/') else '/' + path}"


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

    meta = video.get("meta") or {}
    urls = video.get("urls") or {}
    return {
        "id": video.get("id"),
        "episode": video.get("episode"),
        "title": video.get("title", ""),
        "created_at": video.get("created_at", ""),
        "video_length": _as_int(meta.get("duration"), 0),
        "source_length": _as_int(meta.get("duration"), 0),
        "source_width": _as_int(meta.get("width"), 0),
        "source_height": _as_int(meta.get("height"), 0),
        "source_fps": _as_int(meta.get("fps"), 0),
        "views": _as_int(video.get("views"), 0),
        "preview_url": urls.get("thumbnail", ""),
        "playlist_url": urls.get("playlist", ""),
        "tags": video.get("tags") or [],
        "chapters": [],
        "previous": None,
        "next": None,
    }


def _normalize_category(category):
    if not isinstance(category, dict):
        category = {}

    urls = category.get("urls") or {}
    return {
        "id": category.get("id"),
        "title": category.get("title", ""),
        "slug": category.get("slug"),
        "urls": urls,
        "videos_count": _as_int(category.get("videos_count"), 0),
        "chapters_count": _as_int(category.get("chapters_count"), 0),
    }


def _category_to_legacy_game(category):
    thumbnail = ""
    urls = category.get("urls") or {}
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


def _as_int(value, default):
    try:
        return int(value)
    except (TypeError, ValueError):
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return default
