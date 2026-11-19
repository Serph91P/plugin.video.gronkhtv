"""Saved-cookie identity must never be treated as confirmed sign-in."""
import json
import types

import pytest

from test_plugin_playback import plugin
from test_account_session import FakeResponse, _session
from account.session import SessionError


@pytest.mark.parametrize("user", [None, [], {}, {"displayname": "Old Name"}, {"id": True}, {"id": "  "}, {"login": ""}])
def test_self_endpoint_requires_a_confirmed_identity(tmp_path, user):
    session, _, _ = _session(tmp_path, [FakeResponse(200, json.dumps({"data": user}).encode())])
    with pytest.raises(SessionError):
        session.current_user()


@pytest.mark.parametrize("user", [{"id": 17}, {"id": "synthetic-id"}, {"login": "tester"}, {"usertag": "tester"}, {"email": "test@example.invalid"}])
def test_self_endpoint_preserves_valid_identity_objects(tmp_path, user):
    session, _, _ = _session(tmp_path, [FakeResponse(200, json.dumps({"data": user}).encode())])
    assert session.current_user() == user


@pytest.mark.parametrize("user", [{}, {"displayname": "Unverified Old Name"}, {"id": False}])
def test_settings_controller_never_publishes_unverified_name(plugin, monkeypatch, user):
    settings = {"account_status": "30211: Unverified Old Name"}
    monkeypatch.setattr(plugin.os.path, "exists", lambda path: True)
    monkeypatch.setattr(plugin, "_account_session", types.SimpleNamespace(current_user=lambda: user))
    monkeypatch.setattr(plugin._addon, "setSetting", lambda key, value: settings.__setitem__(key, value))
    assert plugin._read_account_user() is None
    assert settings["account_logged_in"] == "false"
    assert "Unverified Old Name" not in settings["account_status"]
    assert settings["account_session_present"] == "true"


@pytest.mark.parametrize("present,label", [(True, "30228"), (False, "30209")])
def test_startup_clears_previously_persisted_name_without_network(plugin, monkeypatch, present, label):
    settings = {"account_status": "30211: Old Name"}
    monkeypatch.setattr(plugin.os.path, "exists", lambda path: present)
    monkeypatch.setattr(plugin._addon, "setSetting", lambda key, value: settings.__setitem__(key, value))
    monkeypatch.setattr(plugin, "_account_session", types.SimpleNamespace(current_user=lambda: pytest.fail("network during import")))
    plugin._sync_account_indicator_from_storage()
    assert settings["account_logged_in"] == "false"
    assert settings["account_status"] == label


def test_settings_controller_verifies_saved_session_over_real_http(plugin, monkeypatch, tmp_path):
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    import account.session as session_module
    state = {"user": {"id": "synthetic-account", "displayname": "Synthetic User"}, "gets": 0}
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass
        def do_GET(self):
            assert self.path == "/v3/users/self"
            state["gets"] += 1
            body = json.dumps({"data": state["user"]}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        monkeypatch.setattr(session_module, "API_BASE", f"http://127.0.0.1:{server.server_port}/v3")
        cookie = tmp_path / "synthetic-cookies.txt"
        session = session_module.GronkhTVSession(cookie)
        session.save()
        monkeypatch.setattr(plugin, "_ACCOUNT_COOKIE_FILE", str(cookie))
        monkeypatch.setattr(plugin, "_account_session", session)
        settings = {}
        opened = []
        monkeypatch.setattr(plugin._addon, "setSetting", lambda key, value: settings.__setitem__(key, value))
        monkeypatch.setattr(plugin._addon, "openSettings", lambda: opened.append(True), raising=False)
        plugin.handle_open_settings()
        assert settings["account_status"] == "30211: Synthetic User"
        assert settings["account_logged_in"] == "true"
        state["user"] = {"displayname": "Unverified User"}
        plugin.handle_open_settings()
        assert settings["account_logged_in"] == "false"
        assert settings["account_status"] == "30210"
        assert cookie.exists() and settings["account_session_present"] == "true"
        assert state["gets"] == 2 and len(opened) == 2
    finally:
        server.shutdown()
        server.server_close()
        worker.join(2)
