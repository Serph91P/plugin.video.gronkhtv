import json
import os
import stat
import sys
from http.cookiejar import Cookie, MozillaCookieJar
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "resources" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from account.session import GronkhTVSession  # noqa: E402


class FakeResponse:
    def __init__(self, status, payload=b""):
        self.status = status
        self.payload = payload

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, traceback):
        return False


class FakeOpener:
    def __init__(self, cookie_jar, responses):
        self.cookie_jar = cookie_jar
        self.responses = list(responses)
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append((request, timeout))
        if request.full_url.endswith("/csrf-cookie"):
            self.cookie_jar.set_cookie(_cookie("XSRF-TOKEN", "csrf%3Dvalue"))
            self.cookie_jar.set_cookie(_cookie("copycat-session", "session-value"))
        return self.responses.pop(0)


def _cookie(name, value):
    return Cookie(
        version=0,
        name=name,
        value=value,
        port=None,
        port_specified=False,
        domain=".gronkh.tv",
        domain_specified=True,
        domain_initial_dot=True,
        path="/",
        path_specified=True,
        secure=True,
        expires=None,
        discard=True,
        comment=None,
        comment_url=None,
        rest={"HttpOnly": None},
        rfc2109=False,
    )


def _session(tmp_path, responses):
    cookie_path = tmp_path / "account" / "cookies.txt"
    cookie_jar = MozillaCookieJar(str(cookie_path))
    opener = FakeOpener(cookie_jar, responses)
    session = GronkhTVSession(
        cookie_path=cookie_path,
        cookie_jar=cookie_jar,
        opener=opener,
    )
    return session, opener, cookie_path


def test_login_uses_csrf_cookie_and_persists_only_session_data(tmp_path):
    user = {"login": "tester", "displayname": "Test User"}
    session, opener, cookie_path = _session(
        tmp_path,
        [
            FakeResponse(204),
            FakeResponse(204),
            FakeResponse(200, json.dumps(user).encode("utf-8")),
        ],
    )

    result = session.login("tester@example.com", "very-secret-password")

    assert result.authenticated is True
    assert result.requires_two_factor is False
    assert result.user == user
    auth_request = opener.requests[1][0]
    assert auth_request.full_url == "https://backend.gronkh.tv/v3/auth"
    assert json.loads(auth_request.data) == {
        "login": "tester@example.com",
        "password": "very-secret-password",
    }
    assert auth_request.get_header("X-xsrf-token") == "csrf=value"
    assert auth_request.get_header("Origin") == "https://gronkh.tv"
    assert cookie_path.exists()
    assert stat.S_IMODE(os.stat(cookie_path).st_mode) == 0o600
    saved = cookie_path.read_text(encoding="utf-8")
    assert "copycat-session" in saved
    assert "very-secret-password" not in saved


def test_login_returns_two_factor_challenge_without_requesting_user(tmp_path):
    session, opener, cookie_path = _session(
        tmp_path,
        [FakeResponse(204), FakeResponse(202)],
    )

    result = session.login("tester@example.com", "password")

    assert result.authenticated is False
    assert result.requires_two_factor is True
    assert result.user is None
    assert len(opener.requests) == 2
    assert cookie_path.exists()


def test_two_factor_submission_reuses_existing_challenge_session(tmp_path):
    user = {"login": "tester"}
    session, opener, _ = _session(
        tmp_path,
        [
            FakeResponse(204),
            FakeResponse(202),
            FakeResponse(204),
            FakeResponse(200, json.dumps(user).encode("utf-8")),
        ],
    )

    first = session.login("tester@example.com", "password")
    second = session.login("tester@example.com", "password", two_factor_code="123456")

    assert first.requires_two_factor is True
    assert second.authenticated is True
    assert len(opener.requests) == 4
    assert [request.full_url for request, _ in opener.requests].count(
        "https://backend.gronkh.tv/v3/csrf-cookie"
    ) == 1
    assert json.loads(opener.requests[2][0].data)["2fa_code"] == "123456"


def test_logout_revokes_remote_session_and_removes_local_cookie_file(tmp_path):
    session, opener, cookie_path = _session(tmp_path, [FakeResponse(204)])
    session.cookie_jar.set_cookie(_cookie("XSRF-TOKEN", "csrf-token"))
    session.cookie_jar.set_cookie(_cookie("copycat-session", "session-value"))
    session.save()

    session.logout()

    assert opener.requests[0][0].full_url.endswith("/auth/logout")
    assert not cookie_path.exists()
    assert list(session.cookie_jar) == []


def test_login_rejects_empty_credentials_without_network_request(tmp_path):
    session, opener, _ = _session(tmp_path, [])

    with pytest.raises(ValueError, match="Login und Passwort"):
        session.login("", "")

    assert opener.requests == []


def test_request_headers_respects_cookie_domain(tmp_path):
    session, _, _ = _session(tmp_path, [])
    session.cookie_jar.set_cookie(_cookie("copycat-session", "session-value"))

    backend_headers = session.request_headers(
        "https://backend.gronkh.tv/v3/videos/video-id/playlist"
    )
    cdn_headers = session.request_headers("https://02.cdn.vod.farm/video/chunk.m3u8")

    assert backend_headers["Cookie"] == "copycat-session=session-value"
    assert "Cookie" not in cdn_headers
