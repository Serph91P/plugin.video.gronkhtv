import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "resources" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import gronkhtv_api  # noqa: E402
from gronkhtv_api import (  # noqa: E402
    category_videos,
    discovery,
    live_streams,
    normalize_chapter,
    normalize_live_stream,
    normalize_video,
    search_categories,
    search_videos,
    unwrap_data,
)


VIDEO_PAYLOAD = {
    "id": "cbe1afd0-c240-4c74-ba0a-0307a093ff9b",
    "episode": 1105,
    "title": "Test Episode",
    "created_at": "2026-07-05T16:04:45.000000Z",
    "views": "16113",
    "meta": {
        "duration": "42823",
        "width": "1664",
        "height": "936",
        "fps": "60",
    },
    "urls": {
        "thumbnail": "https://cdn.example/preview.jpg",
        "playlist": "https://backend.gronkh.tv/v3/videos/cbe1afd0-c240-4c74-ba0a-0307a093ff9b/playlist",
    },
    "tags": [{"title": "Demo"}],
    "chapters": [
        {
            "title": "Intro",
            "offset": "0",
            "category": {
                "id": "019e-category-id",
                "title": "Just Chatting",
                "slug": "3726-just-chatting",
                "urls": {"boxarts": {"url": "https://cdn.example/boxart.jpg"}},
            },
        }
    ],
    "previous": {"episode": 1104, "title": "Previous"},
    "next": {"episode": 1106, "title": "Next"},
}

CATEGORY_PAYLOAD = {
    "id": "019e-category-id",
    "title": "Minecraft",
    "slug": "2527-minecraft",
    "urls": {"boxarts": {"url": "https://cdn.example/minecraft.jpg"}},
    "videos_count": "42",
    "chapters_count": "84",
}

LIVE_STREAM_PAYLOAD = {
    "user_login": "gronkh",
    "user_name": "Gronkh",
    "game_name": "Just Chatting",
    "title": "Live Test",
    "viewer_count": "1234",
    "started_at": "2026-07-09T18:00:00Z",
    "thumbnail_urls": {"lg": "https://static-cdn.jtvnw.net/live.jpg"},
}


def test_unwrap_data_accepts_wrapped_payloads_and_raw_values():
    wrapped = {"data": [{"id": "video"}]}
    raw = [{"user_login": "gronkh"}]

    assert unwrap_data(wrapped) == [{"id": "video"}]
    assert unwrap_data(raw) == raw


def test_normalize_video_maps_v3_payload_to_legacy_shape():
    video = normalize_video(VIDEO_PAYLOAD)

    assert video["id"] == "cbe1afd0-c240-4c74-ba0a-0307a093ff9b"
    assert video["episode"] == 1105
    assert video["title"] == "Test Episode"
    assert video["created_at"] == "2026-07-05T16:04:45.000000Z"
    assert video["video_length"] == 42823
    assert video["source_length"] == 42823
    assert video["source_width"] == 1664
    assert video["source_height"] == 936
    assert video["source_fps"] == 60
    assert video["views"] == 16113
    assert video["preview_url"] == "https://cdn.example/preview.jpg"
    assert video["playlist_url"].endswith("/playlist")
    assert video["tags"] == [{"title": "Demo"}]
    assert video["previous"]["episode"] == 1104
    assert video["next"]["episode"] == 1106


def test_normalize_chapter_maps_category_to_legacy_game_alias():
    chapter = normalize_chapter(VIDEO_PAYLOAD["chapters"][0])

    assert chapter["title"] == "Intro"
    assert chapter["offset"] == 0
    assert chapter["category"] == {
        "id": "019e-category-id",
        "title": "Just Chatting",
        "slug": "3726-just-chatting",
        "urls": {"boxarts": {"url": "https://cdn.example/boxart.jpg"}},
        "videos_count": 0,
        "chapters_count": 0,
    }
    assert chapter["game"] == {
        "id": "019e-category-id",
        "title": "Just Chatting",
        "slug": "3726-just-chatting",
        "twitch_details": {"thumbnail_url": "https://cdn.example/boxart.jpg"},
    }


def test_normalize_video_uses_safe_defaults_for_missing_optional_fields():
    video = normalize_video({"id": "video-id", "episode": 1})

    assert video == {
        "id": "video-id",
        "episode": 1,
        "title": "",
        "created_at": "",
        "video_length": 0,
        "source_length": 0,
        "source_width": 0,
        "source_height": 0,
        "source_fps": 0,
        "views": 0,
        "preview_url": "",
        "playlist_url": "",
        "tags": [],
        "chapters": [],
        "previous": None,
        "next": None,
    }


def test_discovery_fetches_v3_kind_and_normalizes(monkeypatch):
    calls = []

    def fake_get_json(path):
        calls.append(path)
        return {"data": [VIDEO_PAYLOAD]}

    monkeypatch.setattr(gronkhtv_api, "get_json", fake_get_json)

    videos = discovery("most-viewed")

    assert calls == ["/videos/discovery/most-viewed"]
    assert videos[0]["episode"] == 1105
    assert videos[0]["preview_url"] == "https://cdn.example/preview.jpg"


def test_search_videos_posts_query_and_page_payload(monkeypatch):
    calls = []

    def fake_post_json(path, payload):
        calls.append((path, payload))
        return {"data": [VIDEO_PAYLOAD]}

    monkeypatch.setattr(gronkhtv_api, "post_json", fake_post_json)

    videos = search_videos(query="demo", page=2)

    assert calls == [("/videos/search", {"query": "demo", "page": 2})]
    assert videos[0]["episode"] == 1105


def test_search_categories_posts_query_and_normalizes_categories(monkeypatch):
    calls = []

    def fake_post_json(path, payload):
        calls.append((path, payload))
        return {"data": [CATEGORY_PAYLOAD]}

    monkeypatch.setattr(gronkhtv_api, "post_json", fake_post_json)

    categories = search_categories(query="minecraft", page=2)

    assert calls == [("/categories/search", {"query": "minecraft", "page": 2})]
    assert categories == [
        {
            "id": "019e-category-id",
            "title": "Minecraft",
            "slug": "2527-minecraft",
            "urls": {"boxarts": {"url": "https://cdn.example/minecraft.jpg"}},
            "videos_count": 42,
            "chapters_count": 84,
        }
    ]


def test_category_videos_fetches_slug_page_and_normalizes_videos(monkeypatch):
    calls = []

    def fake_get_json(path):
        calls.append(path)
        return {"data": [VIDEO_PAYLOAD]}

    monkeypatch.setattr(gronkhtv_api, "get_json", fake_get_json)

    videos = category_videos("2527-minecraft", page=3)

    assert calls == ["/categories/2527-minecraft/videos?page=3"]
    assert videos[0]["episode"] == 1105


def test_normalize_live_stream_maps_promoted_stream_shape():
    stream = normalize_live_stream(LIVE_STREAM_PAYLOAD)

    assert stream == {
        "user_login": "gronkh",
        "user_name": "Gronkh",
        "game_name": "Just Chatting",
        "title": "Live Test",
        "viewer_count": 1234,
        "started_at": "2026-07-09T18:00:00Z",
        "thumbnail_url": "https://static-cdn.jtvnw.net/live.jpg",
        "url": "https://www.twitch.tv/gronkh",
    }


def test_live_streams_fetches_promoted_streams_and_normalizes(monkeypatch):
    calls = []

    def fake_get_json(path):
        calls.append(path)
        return {"data": [LIVE_STREAM_PAYLOAD]}

    monkeypatch.setattr(gronkhtv_api, "get_json", fake_get_json)

    streams = live_streams()

    assert calls == ["/promoted/streams"]
    assert streams[0]["user_login"] == "gronkh"
    assert streams[0]["viewer_count"] == 1234
