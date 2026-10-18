"""Real HTTP round-trip through the production adapters, using a synthetic API.

This is transport/integration evidence, not a substitute for two Kodi devices or
for undocumented completion behavior of the real GronkhTV backend.
"""

import json
import sys
import threading
from http.cookiejar import Cookie, CookieJar
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

LIB = Path(__file__).resolve().parents[1] / "resources" / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))

import account.session as session_module
from account.session import GronkhTVSession
from account.progress import ProgressClient
from account.progress_sync import ProgressSync
from account.sync_store import SyncStore

VIDEO = "b0b3d510-1da2-4bcb-9d6c-9d13d23b38b1"


def test_two_clients_server_authority_and_durable_failed_post_over_real_http(tmp_path, monkeypatch):
    state = {"offset": 75, "fail_post": False, "posts": []}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def reply(self, status, data):
            body = json.dumps(data).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == f"/v3/videos/{VIDEO}":
                self.reply(200, {"data": {"watch_progress": {
                    "offset": state["offset"], "marked_as_watched": False,
                }}})
            else:
                self.reply(404, {})

        def do_POST(self):
            assert self.path == f"/v3/watched/progress/{VIDEO}"
            assert self.headers.get("X-XSRF-TOKEN") == "synthetic-csrf"
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            assert set(payload) == {"offset"}
            state["posts"].append(payload)
            if state["fail_post"]:
                self.reply(503, {"message": "synthetic offline failure"})
            else:
                state["offset"] = payload["offset"]
                self.reply(200, {"data": {}})

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(session_module, "API_BASE", f"http://127.0.0.1:{server.server_port}/v3")

    def client(name):
        jar = CookieJar()
        jar.set_cookie(Cookie(0, "XSRF-TOKEN", "synthetic-csrf", None, False,
                              "127.0.0.1", False, False, "/", True, False,
                              None, True, None, None, {}))
        session = GronkhTVSession(tmp_path / f"{name}.cookies", cookie_jar=jar)
        store = SyncStore(tmp_path / f"{name}.sqlite3")
        return ProgressSync(store, ProgressClient(session), "synthetic-account"), store

    try:
        first, store = client("first")
        second, _ = client("second")
        assert first.prepare_playback(VIDEO)["offset"] == 75
        assert first.record_position(VIDEO, 150.125, 300, event="stop") == "synchronized"
        assert second.prepare_playback(VIDEO)["offset"] == 150.125
        state["fail_post"] = True
        assert first.record_position(VIDEO, 2.125, 300, event="stop") == "pending"
        assert store.pending("synthetic-account", VIDEO)["offset"] == 2.125
        # Even the originating client must read the server, not its pending POST.
        assert first.prepare_playback(VIDEO)["offset"] == 150.125
        state["fail_post"] = False
        reopened, reopened_store = client("first")
        assert reopened.flush(VIDEO) == "synchronized"
        assert reopened_store.pending("synthetic-account", VIDEO) is None
        assert second.prepare_playback(VIDEO)["offset"] == 2.125
        assert second.record_position(VIDEO, 0, 300, event="stop") == "synchronized"
        assert first.prepare_playback(VIDEO)["offset"] == 0
        assert state["posts"] == [
            {"offset": 150.125}, {"offset": 2.125}, {"offset": 2.125}, {"offset": 0},
        ]
    finally:
        server.shutdown()
        server.server_close()
        thread.join(2)
