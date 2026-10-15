import json
import os
import stat
import sys
from io import BytesIO
from http.cookiejar import Cookie, MozillaCookieJar
from pathlib import Path
from urllib.error import HTTPError, URLError

import pytest

ROOT = Path(__file__).resolve().parents[1]
LIB = ROOT / "resources" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

from account.session import (  # noqa: E402
    AuthenticationError,
    GronkhTVSession,
    RateLimitError,
    SessionError,
    ValidationError,
)


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
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


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


def _http_error(status, body=b'{"message":"secret-response-marker"}', headers=None):
    return HTTPError(
        "https://backend.gronkh.tv/v3/auth",
        status,
        "secret-reason-marker",
        headers or {},
        BytesIO(body),
    )


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


@pytest.mark.parametrize("failed", [False, True])
def test_logout_clears_before_remote_dispatch_and_keeps_a_new_session(tmp_path, failed):
    session, opener, cookie_path = _session(tmp_path, [])
    session.cookie_jar.set_cookie(_cookie("XSRF-TOKEN", "synthetic-csrf"))
    session.cookie_jar.set_cookie(_cookie("copycat-session", "synthetic-old"))
    session.save()

    def remote(request, timeout=None):
        assert not cookie_path.exists(), "local logout must precede a slow response"
        assert "synthetic-old" in request.get_header("Cookie")
        assert request.get_header("X-xsrf-token") == "synthetic-csrf"
        from urllib.request import HTTPCookieProcessor
        assert HTTPCookieProcessor(session.cookie_jar).http_request(request).get_header("Cookie") == request.get_header("Cookie")
        replacement = GronkhTVSession(cookie_path)
        replacement.cookie_jar.set_cookie(_cookie("copycat-session", "synthetic-new"))
        replacement.save()
        if failed:
            raise SessionError("synthetic transport unavailable")
        return FakeResponse(204)

    opener.open = remote
    if failed:
        with pytest.raises(SessionError):
            session.logout()
    else:
        session.logout()
    assert cookie_path.exists()
    assert GronkhTVSession(cookie_path)._cookie_value("copycat-session") == "synthetic-new"
    assert list(session.cookie_jar) == []


def test_logout_revokes_remote_session_and_removes_local_cookie_file(tmp_path):
    session, opener, cookie_path = _session(tmp_path, [FakeResponse(204)])
    session.cookie_jar.set_cookie(_cookie("XSRF-TOKEN", "csrf-token"))
    session.cookie_jar.set_cookie(_cookie("copycat-session", "session-value"))
    session.save()

    session.logout()

    assert opener.requests[0][0].full_url.endswith("/auth/logout")
    assert not cookie_path.exists()
    assert list(session.cookie_jar) == []


def test_logout_without_cookie_is_local_noop_cleanup(tmp_path):
    session, opener, cookie_path = _session(tmp_path, [])

    session.logout()

    assert opener.requests == []
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


def test_request_json_accepts_a_bounded_per_request_timeout(tmp_path):
    session, opener, _ = _session(tmp_path, [FakeResponse(200, b'{"data": {}}')])

    assert session.request_json("/videos/example", timeout=2) == {"data": {}}

    assert opener.requests[0][1] == 2


@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (401, AuthenticationError),
        (403, AuthenticationError),
        (419, AuthenticationError),
        (422, ValidationError),
        (429, RateLimitError),
        (500, SessionError),
    ],
)
def test_auth_http_errors_expose_only_allowlisted_phase_and_status(
    tmp_path, status, error_type
):
    headers = {"Retry-After": "7"} if status == 429 else {}
    session, _, _ = _session(tmp_path, [FakeResponse(204), _http_error(status, headers=headers)])

    with pytest.raises(error_type) as caught:
        session.login("secret-login-marker", "secret-password-marker")

    error = caught.value
    assert error.auth_diagnostic() == f"phase=auth status={status}"
    assert "secret" not in f"{error} {error.auth_diagnostic()}"
    if status == 429:
        assert error.retry_after == 7.0


def test_csrf_http_error_reports_csrf_phase(tmp_path):
    session, _, _ = _session(tmp_path, [_http_error(500)])

    with pytest.raises(SessionError) as caught:
        session.login("login", "password")

    assert caught.value.auth_diagnostic() == "phase=csrf status=500"


def test_auth_diagnostics_use_actual_2fa_and_current_user_callsites(tmp_path):
    session, opener, _ = _session(
        tmp_path,
        [FakeResponse(204), FakeResponse(202), _http_error(403)],
    )
    session.login("login", "password")

    with pytest.raises(AuthenticationError) as two_factor:
        session.login("login", "password", two_factor_code="123456")
    assert two_factor.value.auth_diagnostic() == "phase=2fa status=403"

    opener.responses.append(_http_error(401))
    with pytest.raises(AuthenticationError) as current_user:
        session.current_user()
    assert current_user.value.auth_diagnostic() == "phase=current_user status=401"


def test_transport_and_decode_failures_do_not_expose_exception_or_payload(tmp_path):
    session, opener, _ = _session(
        tmp_path,
        [FakeResponse(204), URLError("secret-transport-marker")],
    )
    with pytest.raises(SessionError) as transport:
        session.login("login", "password")
    assert transport.value.auth_diagnostic() == "phase=auth"
    assert "secret" not in str(transport.value)

    opener.responses.append(FakeResponse(200, b"secret-invalid-json-marker"))
    with pytest.raises(SessionError) as decode:
        session.current_user()
    assert decode.value.auth_diagnostic() == "phase=current_user"
    assert "secret" not in str(decode.value)


def test_diagnostic_rejects_unknown_phase_and_invalid_status():
    error = SessionError("safe", phase="password", status=True)
    assert error.auth_diagnostic() == "unavailable"
