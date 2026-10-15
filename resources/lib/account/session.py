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
    """A safe public error with optional allowlisted request diagnostics."""

    _AUTH_PHASES = frozenset(("csrf", "auth", "2fa", "current_user"))

    def __init__(self, message="GronkhTV request failed", phase=None, status=None):
        super().__init__(message)
        self.phase = phase if phase in self._AUTH_PHASES else None
        self.status = (
            status
            if isinstance(status, int)
            and not isinstance(status, bool)
            and 100 <= status <= 599
            else None
        )

    def auth_diagnostic(self):
        fields = []
        if self.phase is not None:
            fields.append(f"phase={self.phase}")
        if self.status is not None:
            fields.append(f"status={self.status}")
        return " ".join(fields) or "unavailable"


class AuthenticationError(SessionError):
    pass


class ValidationError(SessionError):
    """The authenticated request was understood but rejected (HTTP 422)."""


class RateLimitError(SessionError):
    """The server requested a bounded pause before another attempt."""

    def __init__(self, message, retry_after=None, phase=None, status=None):
        super().__init__(message, phase=phase, status=status)
        self.retry_after = retry_after


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
            raise AuthenticationError("GronkhTV authentication failed", phase="2fa")

        payload = {"login": login, "password": password}
        if two_factor_code:
            payload["2fa_code"] = two_factor_code

        status, _ = self._request(
            "/auth",
            method="POST",
            payload=payload,
            accepted_statuses=(202, 204),
            csrf=True,
            phase="2fa" if two_factor_code is not None else "auth",
        )
        self.save()
        if status == 202:
            return LoginResult(requires_two_factor=True)

        user = self.current_user()
        return LoginResult(authenticated=True, user=user)

    def current_user(self, timeout=None):
        user = self.request_json("/users/self", timeout=timeout, phase="current_user")
        if isinstance(user, dict) and "data" in user:
            return user["data"]
        return user

    def request_json(self, path, method="GET", payload=None, timeout=None, phase=None):
        csrf = method.upper() not in ("GET", "HEAD") and bool(
            self._cookie_value("XSRF-TOKEN")
        )
        _, content = self._request(
            path,
            method=method,
            payload=payload,
            accepted_statuses=(200,),
            csrf=csrf,
            timeout=timeout,
            phase=phase,
        )
        try:
            return json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            message = (
                "GronkhTV response could not be decoded"
                if phase in SessionError._AUTH_PHASES
                else "GronkhTV hat ungueltiges JSON geliefert"
            )
            raise SessionError(message, phase=phase) from exc

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
                    clear_before_dispatch=True,
                )
            except AuthenticationError:
                pass
        else:
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
        self._request("/csrf-cookie", accepted_statuses=(204,), phase="csrf")
        if not self._cookie_value("XSRF-TOKEN"):
            raise AuthenticationError("GronkhTV authentication failed", phase="csrf")
        self.save()

    def _request(
        self,
        path,
        method="GET",
        payload=None,
        accepted_statuses=(200,),
        csrf=False,
        timeout=None,
        phase=None,
        clear_before_dispatch=False,
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
                message = (
                    "GronkhTV authentication failed"
                    if phase in SessionError._AUTH_PHASES
                    else "Die GronkhTV-Sitzung ist nicht vorbereitet"
                )
                raise AuthenticationError(message, phase=phase)
            headers["X-XSRF-TOKEN"] = unquote(csrf_token)

        request = Request(url, data=data, headers=headers, method=method)
        if clear_before_dispatch:
            # Freeze the outgoing logout credentials before clearing local
            # state. A slow response must never unlink a later login's file.
            # HTTPCookieProcessor preserves an already attached Cookie header.
            self.cookie_jar.add_cookie_header(request)
            self.clear()
        try:
            with self.opener.open(request, timeout=timeout or _TIMEOUT) as response:
                status = getattr(response, "status", None)
                if status is None:
                    status = response.getcode()
                content = response.read()
        except HTTPError as exc:
            status = self._safe_status(exc.code)
            message = (
                "GronkhTV request failed"
                if phase in SessionError._AUTH_PHASES
                else self._error_message(exc)
            )
            if exc.code in (401, 403, 419):
                raise AuthenticationError(message, phase=phase, status=status) from exc
            if exc.code == 422:
                raise ValidationError(message, phase=phase, status=status) from exc
            if exc.code == 429:
                raise RateLimitError(
                    message, _retry_after(exc), phase=phase, status=status
                ) from exc
            raise SessionError(message, phase=phase, status=status) from exc
        except (URLError, TimeoutError, OSError) as exc:
            message = (
                "GronkhTV request failed"
                if phase in SessionError._AUTH_PHASES
                else f"GronkhTV ist nicht erreichbar: {exc}"
            )
            raise SessionError(message, phase=phase) from exc

        if status not in accepted_statuses:
            message = (
                "GronkhTV request failed"
                if phase in SessionError._AUTH_PHASES
                else f"Unerwartete GronkhTV-Antwort: HTTP {status}"
            )
            raise SessionError(
                message,
                phase=phase,
                status=self._safe_status(status),
            )
        return status, content

    def _cookie_value(self, name):
        for cookie in self.cookie_jar:
            if cookie.name == name:
                return cookie.value
        return None

    @staticmethod
    def _safe_status(status):
        if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599:
            return status
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


def _retry_after(error):
    headers = getattr(error, "headers", None)
    value = headers.get("Retry-After") if headers is not None else None
    if value is None:
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    return seconds if seconds >= 0 else None
