"""Selective logout race fix from #44, without progress-store changes."""
import threading
from http.cookiejar import Cookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import HTTPCookieProcessor

import pytest

from test_account_session import FakeResponse, _cookie, _session
import account.session as session_module
from account.session import GronkhTVSession, SessionError


@pytest.mark.parametrize("failed", [False, True])
def test_slow_logout_clears_old_cookie_before_dispatch_and_preserves_new_login(tmp_path, failed):
    session, opener, path = _session(tmp_path, [])
    session.cookie_jar.set_cookie(_cookie("XSRF-TOKEN", "synthetic-csrf"))
    session.cookie_jar.set_cookie(_cookie("copycat-session", "synthetic-old"))
    session.save()
    def remote(request, timeout=None):
        assert not path.exists()
        assert "synthetic-old" in request.get_header("Cookie")
        assert request.get_header("X-xsrf-token") == "synthetic-csrf"
        assert HTTPCookieProcessor(session.cookie_jar).http_request(request).get_header("Cookie") == request.get_header("Cookie")
        replacement = GronkhTVSession(path)
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
    assert GronkhTVSession(path)._cookie_value("copycat-session") == "synthetic-new"
    assert list(session.cookie_jar) == []


@pytest.mark.parametrize("status", [204, 503])
def test_real_http_late_logout_does_not_remove_new_saved_session(tmp_path, monkeypatch, status):
    started, finish = threading.Event(), threading.Event()
    requests, errors = [], []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_POST(self):
            requests.append((self.path, self.headers.get("Cookie"), self.headers.get("X-XSRF-TOKEN")))
            started.set()
            assert finish.wait(3)
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.end_headers()
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    serving = threading.Thread(target=server.serve_forever, daemon=True)
    serving.start()
    monkeypatch.setattr(session_module, "API_BASE", f"http://127.0.0.1:{server.server_port}/v3")
    path = tmp_path / "cookies.txt"
    def cookie(name, value):
        return Cookie(0, name, value, None, False, "127.0.0.1", False, False,
                      "/", True, False, None, True, None, None, {})
    old = GronkhTVSession(path)
    old.cookie_jar.set_cookie(cookie("XSRF-TOKEN", "synthetic-csrf"))
    old.cookie_jar.set_cookie(cookie("session", "synthetic-old"))
    old.save()
    def logout():
        try:
            old.logout()
        except SessionError as exc:
            errors.append(exc)
    worker = threading.Thread(target=logout)
    worker.start()
    try:
        assert started.wait(2)
        assert not path.exists()
        new = GronkhTVSession(path)
        new.cookie_jar.set_cookie(cookie("session", "synthetic-new"))
        new.save()
        finish.set()
        worker.join(3)
        assert not worker.is_alive()
        assert GronkhTVSession(path)._cookie_value("session") == "synthetic-new"
        assert requests[0][0] == "/v3/auth/logout"
        assert "synthetic-old" in requests[0][1] and requests[0][2] == "synthetic-csrf"
        assert len(errors) == (0 if status == 204 else 1)
    finally:
        finish.set()
        worker.join(3)
        server.shutdown()
        server.server_close()
        serving.join(2)
