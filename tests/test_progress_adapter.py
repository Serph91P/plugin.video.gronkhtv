import math
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "resources" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from account.progress import ProgressClient, ProgressProtocolError
from account.session import AuthenticationError


class Session:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []

    def request_json(self, path, method="GET", payload=None, timeout=None):
        self.calls.append((path, method, payload, timeout))
        result = self.responses.pop(0)
        if isinstance(result, Exception):
            raise result
        return result


def test_read_uses_authenticated_video_endpoint_and_accepts_zero_offset():
    session = Session([{"data": {"watch_progress": {"offset": 0, "updated_at": "t"}}}])

    progress = ProgressClient(session).read("b0b3d510-1da2-4bcb-9d6c-9d13d23b38b1")

    assert progress.offset == 0
    assert progress.status == "valid"
    assert session.calls == [
        ("/videos/b0b3d510-1da2-4bcb-9d6c-9d13d23b38b1", "GET", None, 2)
    ]


def test_read_distinguishes_explicitly_absent_from_malformed_response():
    absent = ProgressClient(Session([{"data": {"watch_progress": None}}])).read("b0b3d510-1da2-4bcb-9d6c-9d13d23b38b1")
    assert absent.status == "absent"

    with pytest.raises(ProgressProtocolError):
        ProgressClient(Session([{"data": {}}])).read("b0b3d510-1da2-4bcb-9d6c-9d13d23b38b1")


def test_write_posts_only_real_finite_nonnegative_offset():
    session = Session([{"data": {}}])

    ProgressClient(session).write("b0b3d510-1da2-4bcb-9d6c-9d13d23b38b1", 12.5)

    assert session.calls == [
        ("/watched/progress/b0b3d510-1da2-4bcb-9d6c-9d13d23b38b1", "POST", {"offset": 12.5}, 2)
    ]


@pytest.mark.parametrize("offset", [True, -1, math.nan, math.inf, "12"])
def test_write_rejects_untrusted_offsets_before_request(offset):
    session = Session()
    with pytest.raises(ValueError):
        ProgressClient(session).write("b0b3d510-1da2-4bcb-9d6c-9d13d23b38b1", offset)
    assert session.calls == []


def test_adapter_never_converts_authentication_failure_to_anonymous_request():
    session = Session([AuthenticationError("expired")])
    with pytest.raises(AuthenticationError):
        ProgressClient(session).read("b0b3d510-1da2-4bcb-9d6c-9d13d23b38b1")
    assert len(session.calls) == 1
