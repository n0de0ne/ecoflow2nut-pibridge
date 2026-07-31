"""Embedded control + telemetry web UI for the EcoFlow NUT bridge.

An async HTTP server (aiohttp) that runs inside the daemon's event loop, so it
shares the single BLE connection: it reads the daemon's live state and routes
control actions over the existing link. ``aiohttp`` is an optional dependency
(``pip install ecoflow-nut-bridge[web]``) imported lazily by the daemon.

Auth model: control actions (toggling outputs, changing auto-shutdown) require a
shared token; the read-only dashboard is open unless ``require_auth_for_read`` is
set. The token is accepted as an ``X-Auth-Token`` header, an
``Authorization: Bearer`` header, or a ``token`` query parameter.

The single-page dashboard (HTML/CSS/JS, no external CDN) lives in the ``static/``
directory next to this module: ``/`` serves ``index.html`` and ``/static/{name}``
serves the stylesheet and scripts. The page polls ``/api/state`` for live values
and ``/api/history`` for charts.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Awaitable, Callable
from importlib.resources import files
from typing import TYPE_CHECKING, Any, Protocol

import structlog

from .config import WebConfig

if TYPE_CHECKING:  # pragma: no cover - typing only
    from aiohttp import web

log = structlog.get_logger(__name__)

# Callback signatures the daemon wires up.
StateProvider = Callable[[], dict[str, Any]]
ControlFn = Callable[[str, bool], Awaitable[str]]
EveControlFn = Callable[[bool], Awaitable[str]]
SwitchBotFn = Callable[[str], Awaitable[str]]
AutoStatusFn = Callable[[], dict[str, Any]]
GetSettingsFn = Callable[[], dict[str, Any]]
UpdateSettingsFn = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class HistoryFn(Protocol):
    """Down-sampled history over an absolute window."""

    async def __call__(
        self, *, since: float, until: float, max_points: int
    ) -> dict[str, Any]: ...


class EnergyFn(Protocol):
    """Energy + cost summary over an absolute window."""

    async def __call__(self, *, since: float, until: float) -> dict[str, Any]: ...


_VALID_OUTPUTS = ("ac", "usb", "dc")

# Window guards. The span cap bounds the worst-case query on a Pi's SD card; the
# point cap bounds the JSON we serialise for a browser that asks for too much.
_MAX_SPAN_SECONDS = 30 * 24 * 3600
_MAX_POINTS = 2000

# The browser-facing assets, as an explicit allowlist: the ``/static/{name}``
# handler is a dict lookup, so no request can escape this set (and aiohttp's
# ``{name}`` matches a single path segment, so nested paths never match either).
# Anything added here must also be added to ``package-data`` in pyproject.toml,
# or it will be missing from the installed wheel and the Docker image.
_STATIC_ASSETS: dict[str, str] = {
    "index.html": "text/html",
    "app.css": "text/css",
    "app.js": "text/javascript",
    "core.js": "text/javascript",
    "chart.js": "text/javascript",
    "views.js": "text/javascript",
}

# name -> (body, etag), populated lazily and cached for the process lifetime.
_ASSET_CACHE: dict[str, tuple[bytes, str]] = {}


def _asset(name: str) -> tuple[bytes, str]:
    """Return ``(body, etag)`` for a static asset, reading it at most once."""
    cached = _ASSET_CACHE.get(name)
    if cached is None:
        body = files(__package__).joinpath("static", name).read_bytes()
        etag = '"' + hashlib.sha256(body).hexdigest()[:16] + '"'
        cached = _ASSET_CACHE[name] = (body, etag)
    return cached


class WebServer:
    """Owns the aiohttp app lifecycle (runner + site)."""

    def __init__(
        self,
        config: WebConfig,
        *,
        state_provider: StateProvider,
        control: ControlFn,
        history: HistoryFn,
        autoshutdown_status: AutoStatusFn,
        get_settings: GetSettingsFn,
        update_settings: UpdateSettingsFn,
        energy: EnergyFn,
        history_enabled: bool = False,
        eve_control: EveControlFn | None = None,
        switchbot_press: SwitchBotFn | None = None,
    ) -> None:
        self._config = config
        self._state_provider = state_provider
        self._control = control
        self._eve_control = eve_control
        self._switchbot_press = switchbot_press
        self._history = history
        self._autoshutdown_status = autoshutdown_status
        self._get_settings = get_settings
        self._update_settings = update_settings
        self._energy = energy
        self._history_enabled = history_enabled
        self._runner: web.AppRunner | None = None

    # -- lifecycle ---------------------------------------------------------- #
    def build_app(self) -> web.Application:
        """Construct the aiohttp application (also used directly by tests)."""
        from aiohttp import web  # lazy: optional dependency

        # Read the shell once up front: a packaging mistake then fails loudly at
        # startup (caught and logged by the daemon) instead of silently serving
        # 404s to the browser.
        _asset("index.html")

        app = web.Application()
        app.add_routes(
            [
                web.get("/", self._handle_index),
                web.get("/static/{name}", self._handle_static),
                web.get("/api/state", self._handle_state),
                web.get("/api/history", self._handle_history),
                web.get("/api/energy", self._handle_energy),
                web.get("/api/autoshutdown", self._handle_autoshutdown_get),
                web.get("/api/auth/check", self._handle_auth_check),
                web.get("/api/settings", self._handle_settings_get),
                web.post("/api/settings", self._handle_settings_set),
                web.post("/api/control", self._handle_control),
                web.post("/api/switchbot", self._handle_switchbot),
            ]
        )
        return app

    async def start(self) -> None:
        from aiohttp import web  # lazy: optional dependency

        self._runner = web.AppRunner(self.build_app())
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._config.host, self._config.port)
        await site.start()
        log.info(
            "web.listening",
            host=self._config.host,
            port=self._config.port,
            auth=bool(self._config.auth_token),
        )

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
            self._runner = None

    # -- auth --------------------------------------------------------------- #
    def _token_ok(self, request: web.Request) -> bool:
        """Constant-time comparison of the presented token against the config."""
        expected = self._config.auth_token
        if not expected:
            return False
        presented = request.headers.get("X-Auth-Token", "")
        if not presented:
            auth = request.headers.get("Authorization", "")
            if auth.startswith("Bearer "):
                presented = auth[len("Bearer ") :]
        if not presented:
            presented = request.query.get("token", "")
        return bool(presented) and hmac.compare_digest(presented, expected)

    def _require_control_auth(self, request: web.Request) -> None:
        from aiohttp import web

        if not self._config.auth_token:
            raise web.HTTPServiceUnavailable(
                reason="control disabled: no auth_token configured"
            )
        if not self._token_ok(request):
            raise web.HTTPUnauthorized(reason="invalid or missing auth token")

    def _require_read_auth(self, request: web.Request) -> None:
        from aiohttp import web

        if self._config.require_auth_for_read and not self._token_ok(request):
            raise web.HTTPUnauthorized(reason="invalid or missing auth token")

    # -- handlers ----------------------------------------------------------- #
    async def _handle_index(self, request: web.Request) -> web.Response:
        # The page itself is always served; read endpoints enforce auth so the
        # browser can prompt for a token. (When require_auth_for_read is off the
        # dashboard is fully open.)
        return self._static_response(request, "index.html")

    async def _handle_static(self, request: web.Request) -> web.Response:
        from aiohttp import web

        name = request.match_info["name"]
        if name not in _STATIC_ASSETS:
            raise web.HTTPNotFound()
        # Served unauthenticated for the same reason as the page itself: the
        # stylesheet and script carry no telemetry, and gating them would leave
        # an unauthenticated visitor staring at an unstyled blank page instead
        # of the token prompt.
        return self._static_response(request, name)

    def _static_response(self, request: web.Request, name: str) -> web.Response:
        from aiohttp import web

        body, etag = _asset(name)
        # Revalidate on every load (cheap on a LAN) so a bridge upgrade can
        # never leave a stale shell cached in the browser.
        headers = {"ETag": etag, "Cache-Control": "no-cache, must-revalidate"}
        if request.headers.get("If-None-Match") == etag:
            raise web.HTTPNotModified(headers=headers)
        return web.Response(body=body, content_type=_STATIC_ASSETS[name], headers=headers)

    async def _handle_state(self, request: web.Request) -> web.Response:
        from aiohttp import web

        self._require_read_auth(request)
        payload = self._state_provider()
        payload["history_enabled"] = self._history_enabled
        payload["control_enabled"] = bool(self._config.auth_token)
        return web.json_response(payload)

    async def _handle_history(self, request: web.Request) -> web.Response:
        from aiohttp import web

        self._require_read_auth(request)
        if not self._history_enabled:
            return web.json_response({"enabled": False, "points": []})
        since, until = _window_from_query(request, default_minutes=60)
        max_points = _int_query(request, "max_points", 240, 2, _MAX_POINTS)
        result = await self._history(since=since, until=until, max_points=max_points)
        return web.json_response(
            {
                "enabled": True,
                "since": since,
                "until": until,
                "minutes": round((until - since) / 60),
                "max_points": max_points,
                "bucket_seconds": result.get("bucket_seconds", 0),
                "points": result.get("points", []),
            }
        )

    async def _handle_energy(self, request: web.Request) -> web.Response:
        from aiohttp import web

        self._require_read_auth(request)
        if not self._history_enabled:
            return web.json_response({"enabled": False})
        since, until = _window_from_query(request, default_minutes=1440)
        return web.json_response(await self._energy(since=since, until=until))

    async def _handle_autoshutdown_get(self, request: web.Request) -> web.Response:
        from aiohttp import web

        self._require_read_auth(request)
        return web.json_response(self._autoshutdown_status())

    async def _handle_auth_check(self, request: web.Request) -> web.Response:
        """Validate a token without performing an action.

        Lets the browser's unlock dialog say "that token was not accepted" up
        front, rather than storing a typo and failing on the next control action.
        """
        from aiohttp import web

        self._require_control_auth(request)
        return web.json_response({"ok": True})

    async def _handle_settings_get(self, request: web.Request) -> web.Response:
        from aiohttp import web

        self._require_read_auth(request)
        return web.json_response(self._get_settings())

    async def _handle_settings_set(self, request: web.Request) -> web.Response:
        from aiohttp import web

        self._require_control_auth(request)
        body = await _json_body(request)
        updates = body.get("updates", body)
        if not isinstance(updates, dict) or not updates:
            raise web.HTTPBadRequest(reason="body must be a non-empty object of edits")
        try:
            result = await self._update_settings(updates)
        except ValueError as exc:
            raise web.HTTPBadRequest(reason=str(exc)) from exc
        return web.json_response(result)

    async def _handle_control(self, request: web.Request) -> web.Response:
        from aiohttp import web

        self._require_control_auth(request)
        body = await _json_body(request)
        output = body.get("output")
        enabled = body.get("enabled")
        if not isinstance(enabled, bool) or (
            output != "eve" and output not in _VALID_OUTPUTS
        ):
            raise web.HTTPBadRequest(
                reason='body must be {"output": "ac|usb|dc|eve", "enabled": bool}'
            )
        if output == "eve" and self._eve_control is None:
            raise web.HTTPBadRequest(reason="eve outlet control is not enabled")
        try:
            if output == "eve":
                assert self._eve_control is not None
                message = await self._eve_control(enabled)
            else:
                message = await self._control(output, enabled)
        except Exception as exc:  # noqa: BLE001 - surface as a 4xx/5xx to the client
            raise web.HTTPConflict(reason=str(exc)) from exc
        log.info("web.control", output=output, enabled=enabled)
        return web.json_response({"ok": True, "message": message})

    async def _handle_switchbot(self, request: web.Request) -> web.Response:
        from aiohttp import web

        self._require_control_auth(request)
        if self._switchbot_press is None:
            raise web.HTTPBadRequest(reason="switchbot is not enabled")
        body = await _json_body(request)
        action = body.get("action", "press")
        if action not in ("press", "on", "off"):
            raise web.HTTPBadRequest(reason='action must be "press", "on" or "off"')
        try:
            message = await self._switchbot_press(action)
        except Exception as exc:  # noqa: BLE001 - surface as a 4xx/5xx to the client
            raise web.HTTPConflict(reason=str(exc)) from exc
        log.info("web.switchbot", action=action)
        return web.json_response({"ok": True, "message": message})


def _float_query(request: web.Request, name: str) -> float | None:
    """Parse an optional epoch-seconds query parameter."""
    from aiohttp import web

    raw = request.query.get(name)
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except ValueError:
        raise web.HTTPBadRequest(reason=f"{name} must be epoch seconds") from None


def _int_query(request: web.Request, name: str, default: int, lo: int, hi: int) -> int:
    """Parse an optional integer query parameter, clamped to ``[lo, hi]``."""
    from aiohttp import web

    raw = request.query.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        raise web.HTTPBadRequest(reason=f"{name} must be an integer") from None
    return max(lo, min(value, hi))


def _window_from_query(
    request: web.Request, *, default_minutes: int
) -> tuple[float, float]:
    """Resolve the requested time window to absolute epoch seconds.

    Accepts either an explicit ``since``/``until`` pair (what the chart sends as
    you zoom and pan) or the original ``minutes=N``, which stays supported. This
    is the single place relative windows become absolute -- everything below it
    only ever speaks absolute time.

    A window that lies in the future is not an error: live mode keeps the right
    edge slightly ahead of now, and an empty result is the correct answer.
    """
    from aiohttp import web

    until = _float_query(request, "until")
    since = _float_query(request, "since")
    if until is None:
        until = time.time()
    if since is None:
        minutes = _int_query(
            request, "minutes", default_minutes, 1, _MAX_SPAN_SECONDS // 60
        )
        since = until - minutes * 60
    if until <= since:
        raise web.HTTPBadRequest(reason="until must be greater than since")
    if until - since > _MAX_SPAN_SECONDS:
        since = until - _MAX_SPAN_SECONDS
    return since, until


async def _json_body(request: web.Request) -> dict[str, Any]:
    from aiohttp import web

    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        raise web.HTTPBadRequest(reason="invalid JSON body") from None
    if not isinstance(data, dict):
        raise web.HTTPBadRequest(reason="JSON body must be an object")
    return data
