import json
import os
from http.cookiejar import LoadError, MozillaCookieJar
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import unquote
from urllib.request import HTTPCookieProcessor, Request, build_opener

API_BASE = "https://backend.gronkh.tv/v3"
FRONTEND_ORIGIN = "https://gronkh.tv"
_TIMEOUT = 15
_USER_AGENT = (
    "Mozilla/5.0 (Kodi; plugin.video.gronkhtv) "
    "AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36"
)


class SessionError(RuntimeError):
    pass


class AuthenticationError(SessionError):
    pass


class LoginResult:
    def __init__(self, authenticated=False, requires_two_factor=False, user=None):
        self.authenticated = authenticated
        self.requires_two_factor = requires_two_factor
        self.user = user


class GronkhTVSession:
    def __init__(self, cookie_path, cookie_jar=None, opener=None):
        self.cookie_path = Path(cookie_path)
        self.cookie_jar = (
            cookie_jar
            if cookie_jar is not None
            else MozillaCookieJar(str(self.cookie_path))
        )
        self._load()
        self.opener = opener or build_opener(HTTPCookieProcessor(self.cookie_jar))

    def login(self, login, password, two_factor_code=None):
        if not login or not password:
            raise ValueError("Login und Passwort duerfen nicht leer sein")

        if two_factor_code is None:
            self._prepare_csrf()
        elif not self._cookie_value("XSRF-TOKEN"):
            raise AuthenticationError("Die Zwei-Faktor-Sitzung ist abgelaufen")

        payload = {"login": login, "password": password}
        if two_factor_code:
            payload["2fa_code"] = two_factor_code

        status, _ = self._request(
            "/auth",
            method="POST",
            payload=payload,
            accepted_statuses=(202, 204),
            csrf=True,
        )
        self.save()
        if status == 202:
            return LoginResult(requires_two_factor=True)

        user = self.current_user()
        return LoginResult(authenticated=True, user=user)

    def current_user(self):
        user = self.request_json("/users/self")
        if isinstance(user, dict) and "data" in user:
            return user["data"]
        return user

    def request_json(self, path, method="GET", payload=None):
        csrf = method.upper() not in ("GET", "HEAD") and bool(
            self._cookie_value("XSRF-TOKEN")
        )
        _, content = self._request(
            path,
            method=method,
            payload=payload,
            accepted_statuses=(200,),
            csrf=csrf,
        )
        try:
            return json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SessionError("GronkhTV hat ungueltiges JSON geliefert") from exc

    def request_bytes(self, path):
        _, content = self._request(path, accepted_statuses=(200,))
        return content

    def request_headers(self, url):
        request = Request(url)
        self.cookie_jar.add_cookie_header(request)
        headers = {"User-Agent": _USER_AGENT}
        cookie = request.get_header("Cookie")
        if cookie:
            headers["Cookie"] = cookie
        return headers

    def logout(self):
        if self._cookie_value("XSRF-TOKEN"):
            try:
                self._request(
                    "/auth/logout",
                    method="POST",
                    accepted_statuses=(204,),
                    csrf=True,
                )
            except AuthenticationError:
                pass
        self.clear()

    def save(self):
        self.cookie_path.parent.mkdir(parents=True, exist_ok=True)
        self.cookie_jar.save(ignore_discard=True, ignore_expires=True)
        os.chmod(self.cookie_path, 0o600)

    def clear(self):
        self.cookie_jar.clear()
        if self.cookie_path.exists():
            self.cookie_path.unlink()

    def _load(self):
        if not self.cookie_path.exists():
            return
        try:
            self.cookie_jar.load(ignore_discard=True, ignore_expires=True)
        except (LoadError, OSError):
            self.cookie_jar.clear()

    def _prepare_csrf(self):
        self._request("/csrf-cookie", accepted_statuses=(204,))
        if not self._cookie_value("XSRF-TOKEN"):
            raise AuthenticationError("GronkhTV hat kein CSRF-Cookie geliefert")
        self.save()

    def _request(
        self,
        path,
        method="GET",
        payload=None,
        accepted_statuses=(200,),
        csrf=False,
    ):
        url = path if path.startswith("http") else f"{API_BASE}/{path.lstrip('/')}"
        headers = {
            "Accept": "application/json",
            "Origin": FRONTEND_ORIGIN,
            "Referer": f"{FRONTEND_ORIGIN}/",
            "User-Agent": _USER_AGENT,
        }
        data = None
        if payload is not None:
            data = json.dumps(payload).encode("utf-8")
            headers["Content-Type"] = "application/json"
        if csrf:
            csrf_token = self._cookie_value("XSRF-TOKEN")
            if not csrf_token:
                raise AuthenticationError("Die GronkhTV-Sitzung ist nicht vorbereitet")
            headers["X-XSRF-TOKEN"] = unquote(csrf_token)

        request = Request(url, data=data, headers=headers, method=method)
        try:
            with self.opener.open(request, timeout=_TIMEOUT) as response:
                status = getattr(response, "status", None)
                if status is None:
                    status = response.getcode()
                content = response.read()
        except HTTPError as exc:
            message = self._error_message(exc)
            if exc.code in (401, 403, 419, 422):
                raise AuthenticationError(message) from exc
            raise SessionError(message) from exc
        except (URLError, TimeoutError, OSError) as exc:
            raise SessionError(f"GronkhTV ist nicht erreichbar: {exc}") from exc

        if status not in accepted_statuses:
            raise SessionError(f"Unerwartete GronkhTV-Antwort: HTTP {status}")
        return status, content

    def _cookie_value(self, name):
        for cookie in self.cookie_jar:
            if cookie.name == name:
                return cookie.value
        return None

    @staticmethod
    def _error_message(error):
        fallback = f"GronkhTV-Anfrage fehlgeschlagen: HTTP {error.code}"
        try:
            payload = json.loads(error.read().decode("utf-8"))
        except (AttributeError, UnicodeDecodeError, json.JSONDecodeError):
            return fallback
        if isinstance(payload, dict):
            return payload.get("message") or fallback
        return fallback
