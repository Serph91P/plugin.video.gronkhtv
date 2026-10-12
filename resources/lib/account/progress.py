"""Authenticated Gronkh TV watch-progress API adapter."""

import math
from dataclasses import dataclass
from typing import Optional, Union
from uuid import UUID


class ProgressProtocolError(RuntimeError):
    """The server returned a syntactically valid but unsupported progress shape."""


@dataclass(frozen=True)
class RemoteProgress:
    status: str
    offset: Optional[Union[float, int]] = None
    updated_at: Optional[str] = None
    marked_as_watched: Optional[bool] = None


class ProgressClient:
    """Private progress calls through one already-authenticated session only."""

    def __init__(self, session, timeout=2):
        if session is None:
            raise ValueError("An authenticated session is required")
        self._session = session
        self._timeout = timeout

    def read(self, video_id):
        video_id = _video_id(video_id)
        payload = self._session.request_json(
            f"/videos/{video_id}", timeout=self._timeout
        )
        if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
            raise ProgressProtocolError("Video response has no data object")
        data = payload["data"]
        if "watch_progress" not in data:
            raise ProgressProtocolError("Video response has no watch_progress field")
        progress = data["watch_progress"]
        if progress is None:
            return RemoteProgress(status="absent")
        if not isinstance(progress, dict):
            raise ProgressProtocolError("watch_progress is not an object")
        offset = _offset(progress.get("offset"))
        return RemoteProgress(
            status="valid",
            offset=offset,
            updated_at=_optional_string(progress.get("updated_at")),
            marked_as_watched=_optional_bool(progress.get("marked_as_watched")),
        )

    def write(self, video_id, offset):
        video_id = _video_id(video_id)
        offset = _offset(offset)
        return self._session.request_json(
            f"/watched/progress/{video_id}",
            method="POST",
            payload={"offset": offset},
            timeout=self._timeout,
        )


def _video_id(value):
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError("video id must be a backend UUID") from exc


def _offset(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("offset must be finite non-negative seconds")
    if not math.isfinite(value) or value < 0:
        raise ValueError("offset must be finite non-negative seconds")
    return value


def _optional_string(value):
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProgressProtocolError("updated_at is not a string")
    return value


def _optional_bool(value):
    if value is None:
        return None
    if not isinstance(value, bool):
        raise ProgressProtocolError("marked_as_watched is not a boolean")
    return value
