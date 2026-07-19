"""Embedded control + telemetry web UI for the EcoFlow NUT bridge.

An async HTTP server (aiohttp) that runs inside the daemon's event loop, so it
shares the single BLE connection: it reads the daemon's live state and routes
control actions over the existing link. ``aiohttp`` is an optional dependency
(``pip install ecoflow-nut-bridge[web]``) imported lazily by the daemon.

Auth model: control actions (toggling outputs, changing auto-shutdown) require a
shared token; the read-only dashboard is open unless ``require_auth_for_read`` is
set. The token is accepted as an ``X-Auth-Token`` header, an
``Authorization: Bearer`` header, or a ``token`` query parameter.

The single-page dashboard (HTML/CSS/JS, no external CDN) is served from ``/`` and
polls ``/api/state`` for live values, ``/api/history`` for charts.
"""

from __future__ import annotations

import hmac
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

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
HistoryFn = Callable[[int], Awaitable[list[dict[str, Any]]]]
AutoStatusFn = Callable[[], dict[str, Any]]
GetSettingsFn = Callable[[], dict[str, Any]]
UpdateSettingsFn = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]
EnergyFn = Callable[[int], Awaitable[dict[str, Any]]]

_VALID_OUTPUTS = ("ac", "usb", "dc")


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

        app = web.Application()
        app.add_routes(
            [
                web.get("/", self._handle_index),
                web.get("/api/state", self._handle_state),
                web.get("/api/history", self._handle_history),
                web.get("/api/energy", self._handle_energy),
                web.get("/api/autoshutdown", self._handle_autoshutdown_get),
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
        from aiohttp import web

        # The page itself is always served; read endpoints enforce auth so the
        # browser can prompt for a token. (When require_auth_for_read is off the
        # dashboard is fully open.)
        return web.Response(text=_INDEX_HTML, content_type="text/html")

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
        try:
            minutes = int(request.query.get("minutes", "60"))
        except ValueError:
            raise web.HTTPBadRequest(reason="minutes must be an integer") from None
        minutes = max(1, min(minutes, 60 * 24 * 30))  # cap at 30 days
        points = await self._history(minutes)
        return web.json_response({"enabled": True, "minutes": minutes, "points": points})

    async def _handle_energy(self, request: web.Request) -> web.Response:
        from aiohttp import web

        self._require_read_auth(request)
        if not self._history_enabled:
            return web.json_response({"enabled": False})
        try:
            minutes = int(request.query.get("minutes", "1440"))
        except ValueError:
            raise web.HTTPBadRequest(reason="minutes must be an integer") from None
        minutes = max(1, min(minutes, 60 * 24 * 30))
        return web.json_response(await self._energy(minutes))

    async def _handle_autoshutdown_get(self, request: web.Request) -> web.Response:
        from aiohttp import web

        self._require_read_auth(request)
        return web.json_response(self._autoshutdown_status())

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


async def _json_body(request: web.Request) -> dict[str, Any]:
    from aiohttp import web

    try:
        data = await request.json()
    except Exception:  # noqa: BLE001
        raise web.HTTPBadRequest(reason="invalid JSON body") from None
    if not isinstance(data, dict):
        raise web.HTTPBadRequest(reason="JSON body must be an object")
    return data


# --------------------------------------------------------------------------- #
# Single-page dashboard. Vanilla JS, no external assets, tiny canvas chart.
# --------------------------------------------------------------------------- #
_INDEX_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>EcoFlow DELTA 3 - Bridge</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; }
  body { margin: 0; font: 15px/1.4 system-ui, sans-serif; background:#0f1115; color:#e6e6e6; }
  header { display:flex; align-items:center; gap:.75rem; padding:1rem 1.25rem;
           border-bottom:1px solid #222631; background:#151823; }
  header h1 { font-size:1.05rem; margin:0; font-weight:600; }
  .status-pill { margin-left:auto; padding:.2rem .6rem; border-radius:999px;
                 font-size:.8rem; font-weight:600; background:#2a2f3c; }
  .status-OL { background:#16432a; color:#7ee2a8; }
  .status-OB { background:#5a4216; color:#f2c969; }
  .status-LB { background:#5a1d1d; color:#f29494; }
  main { padding:1.25rem; max-width:1000px; margin:0 auto; display:grid; gap:1.25rem; }
  .grid { display:grid; gap:1rem; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); }
  .card { background:#151823; border:1px solid #222631; border-radius:12px; padding:1rem 1.1rem; }
  .metric .label { font-size:.72rem; text-transform:uppercase; letter-spacing:.04em; color:#8a93a6; }
  .metric .value { font-size:1.6rem; font-weight:600; margin-top:.2rem; }
  .metric .unit { font-size:.85rem; color:#8a93a6; margin-left:.2rem; font-weight:400; }
  .metric.sm .value { font-size:1.25rem; }
  .soc-bar { height:8px; border-radius:999px; background:#222631; margin-top:.5rem; overflow:hidden; }
  .soc-fill { height:100%; background:linear-gradient(90deg,#3a8f5c,#7ee2a8); transition:width .4s; }
  h2 { font-size:.85rem; text-transform:uppercase; letter-spacing:.04em; color:#8a93a6; margin:0 0 .75rem;
       display:flex; align-items:center; gap:.5rem; }
  .controls { display:flex; flex-wrap:wrap; gap:.75rem; }
  .ctl { flex:1; min-width:140px; display:flex; align-items:center; justify-content:space-between;
         gap:.5rem; padding:.6rem .8rem; background:#1b1f2b; border-radius:10px; }
  .ctl .name { font-weight:600; }
  .led { display:inline-block; width:11px; height:11px; border-radius:50%;
         background:#3a4356; margin-right:.4rem; vertical-align:middle; flex:0 0 auto; }
  .led.on { background:#3ad07a; box-shadow:0 0 7px #3ad07a99; }
  .led.off { background:#6b7280; }
  .led.warn { background:#f2c969; box-shadow:0 0 7px #f2c96999; animation:pulse 1.2s infinite; }
  .led.crit { background:#f06363; box-shadow:0 0 9px #f06363bb; animation:pulse 1s infinite; }
  .led.unknown { background:#3a4356; }
  @keyframes pulse { 0%,100% { opacity:1; } 50% { opacity:.3; } }
  .pstat { display:inline-flex; align-items:center; font-size:.78rem; color:#8a93a6;
           min-width:78px; }
  .pstat.on { color:#7ee2a8; } .pstat.off { color:#c6ccd6; }
  .as-badge { display:inline-flex; align-items:center; font-weight:600; }
  button { font:inherit; border:0; border-radius:8px; padding:.45rem .85rem; cursor:pointer;
           background:#2a2f3c; color:#e6e6e6; font-weight:600; }
  button.on { background:#16432a; color:#7ee2a8; }
  button.off { background:#5a1d1d; color:#f29494; }
  button:disabled { opacity:.5; cursor:not-allowed; }
  .range { display:flex; gap:.4rem; margin-bottom:.75rem; flex-wrap:wrap; }
  .range button { background:#1b1f2b; font-size:.8rem; padding:.3rem .7rem; }
  .range button.active { background:#2d3957; color:#a9c2ff; }
  .chart-wrap { position:relative; }
  canvas { width:100%; height:220px; display:block; cursor:crosshair; }
  #tip { position:absolute; pointer-events:none; display:none; background:#0b0d12;
         border:1px solid #2a2f3c; border-radius:8px; padding:.5rem .6rem; font-size:.78rem;
         white-space:nowrap; box-shadow:0 4px 14px rgba(0,0,0,.4); z-index:5; }
  #tip b { color:#e6e6e6; } #tip .t { color:#8a93a6; margin-bottom:.25rem; }
  #tip i { font-style:normal; display:inline-block; width:9px; height:9px; border-radius:2px;
           margin-right:.35rem; vertical-align:middle; }
  .legend { display:flex; gap:1rem; flex-wrap:wrap; font-size:.78rem; color:#8a93a6; margin-top:.5rem; }
  .legend span::before { content:""; display:inline-block; width:10px; height:10px; border-radius:2px;
                         margin-right:.3rem; vertical-align:middle; background:var(--c); }
  .muted { color:#8a93a6; font-size:.85rem; }
  #toast { position:fixed; bottom:1rem; left:50%; transform:translateX(-50%);
           background:#2a2f3c; padding:.6rem 1rem; border-radius:8px; opacity:0;
           transition:opacity .3s; pointer-events:none; max-width:90vw; z-index:10; }
  #toast.show { opacity:1; }
  .token-row { display:flex; gap:.5rem; align-items:center; margin-top:.5rem; }
  .token-row input { flex:1; background:#0f1115; border:1px solid #2a2f3c; color:#e6e6e6;
                     border-radius:8px; padding:.45rem .6rem; }
  fieldset { border:1px solid #222631; border-radius:10px; margin:0 0 1rem; padding:.75rem 1rem 1rem; }
  legend { color:#a9c2ff; font-size:.78rem; text-transform:uppercase; letter-spacing:.04em; padding:0 .4rem; }
  .frow { display:flex; align-items:center; gap:.6rem; padding:.3rem 0; }
  .frow label { flex:1; min-width:0; }
  .frow .help { display:block; font-size:.74rem; color:#8a93a6; }
  .frow input[type=number], .frow input[type=text], .frow input[type=time] {
    width:130px; background:#0f1115; border:1px solid #2a2f3c; color:#e6e6e6;
    border-radius:8px; padding:.35rem .5rem; }
  .frow input[type=checkbox] { width:18px; height:18px; }
  .save-row { display:flex; gap:.6rem; align-items:center; }
</style>
</head>
<body>
<header>
  <h1>EcoFlow DELTA 3 Bridge</h1>
  <span id="status" class="status-pill">…</span>
</header>
<main>
  <section class="card">
    <div class="grid">
      <div class="metric"><div class="label">State of charge</div>
        <div class="value"><span id="soc">–</span><span class="unit">%</span></div>
        <div class="soc-bar"><div id="socFill" class="soc-fill" style="width:0%"></div></div></div>
      <div class="metric"><div class="label">AC input</div>
        <div class="value"><span id="acIn">–</span><span class="unit">W</span></div></div>
      <div class="metric"><div class="label">AC output</div>
        <div class="value"><span id="acOut">–</span><span class="unit">W</span></div></div>
      <div class="metric"><div class="label">USB / USB-C</div>
        <div class="value"><span id="usb">–</span><span class="unit">W</span></div></div>
      <div class="metric"><div class="label">Runtime est.</div>
        <div class="value"><span id="runtime">–</span></div></div>
      <div class="metric"><div class="label">Charge / discharge</div>
        <div class="value" style="font-size:1.1rem"><span id="remain">–</span></div></div>
    </div>
  </section>

  <section class="card">
    <h2>Port controls</h2>
    <div class="controls">
      <div class="ctl"><span class="name">AC output</span>
        <span class="pstat" id="stAc"><span class="led unknown"></span>…</span>
        <span><button data-out="ac" data-on="1" class="on">On</button>
        <button data-out="ac" data-on="0" class="off">Off</button></span></div>
      <div class="ctl"><span class="name">USB</span>
        <span class="pstat" id="stUsb"><span class="led unknown"></span>…</span>
        <span><button data-out="usb" data-on="1" class="on">On</button>
        <button data-out="usb" data-on="0" class="off">Off</button></span></div>
      <div class="ctl"><span class="name">12V DC</span>
        <span class="pstat" id="stDc"><span class="led unknown"></span>…</span>
        <span><button data-out="dc" data-on="1" class="on">On</button>
        <button data-out="dc" data-on="0" class="off">Off</button></span></div>
      <div class="ctl" id="eveCtl" style="display:none">
        <span class="name">Eve outlet <span class="muted" style="text-transform:none">(downstream)</span></span>
        <span class="pstat" id="stEve"><span class="led unknown"></span>…</span>
        <span><button data-out="eve" data-on="1" class="on">On</button>
        <button data-out="eve" data-on="0" class="off">Off</button></span></div>
      <div class="ctl" id="sbCtl" style="display:none">
        <span class="name">Server power <span class="muted" style="text-transform:none">(SwitchBot)</span></span>
        <span><button id="sbPress" class="on">Press</button></span></div>
    </div>
    <div id="controlNote" class="muted" style="margin-top:.6rem"></div>
    <div class="token-row" id="tokenRow" style="display:none">
      <input id="token" type="password" placeholder="control token" autocomplete="off">
      <button id="saveToken">Save</button>
    </div>
  </section>

  <section class="card" id="energyCard">
    <h2>Energy &amp; cost <span id="energyRange" class="muted" style="text-transform:none"></span></h2>
    <div class="grid">
      <div class="metric"><div class="label">Grid energy</div>
        <div class="value"><span id="eKwh">–</span><span class="unit">kWh</span></div></div>
      <div class="metric"><div class="label">Total cost</div>
        <div class="value"><span id="eCost">–</span></div></div>
      <div class="metric sm"><div class="label">Heures Creuses</div>
        <div class="value"><span id="eHc">–</span></div></div>
      <div class="metric sm"><div class="label">Heures Pleines</div>
        <div class="value"><span id="eHp">–</span></div></div>
      <div class="metric sm"><div class="label">Avg / peak draw</div>
        <div class="value"><span id="eAvg">–</span></div></div>
      <div class="metric sm"><div class="label">Projected</div>
        <div class="value"><span id="eProj">–</span></div></div>
    </div>
    <div id="energyNote" class="muted" style="margin-top:.5rem"></div>
  </section>

  <section class="card" id="historyCard">
    <h2>History</h2>
    <div class="range">
      <button data-min="60">1h</button>
      <button data-min="360">6h</button>
      <button data-min="1440" class="active">24h</button>
      <button data-min="10080">7d</button>
      <button data-min="43200">30d</button>
    </div>
    <div class="chart-wrap">
      <canvas id="chart" width="940" height="220"></canvas>
      <div id="tip"></div>
    </div>
    <div class="legend">
      <span style="--c:#7ee2a8">SoC %</span>
      <span style="--c:#f2c969">AC out W</span>
      <span style="--c:#a9c2ff">AC in W</span>
    </div>
    <div id="historyNote" class="muted"></div>
  </section>

  <section class="card">
    <h2>Auto-shutdown</h2>
    <div class="ctl">
      <span class="as-badge"><span class="led unknown" id="asLed"></span>
        <span id="asState">…</span></span>
      <span><button id="asOn" class="on">Enable</button>
      <button id="asOff" class="off">Disable</button></span>
    </div>
    <div id="asDetail" class="muted" style="margin-top:.6rem"></div>
  </section>

  <section class="card" id="settingsCard">
    <h2>Settings</h2>
    <div id="settingsForm"></div>
    <div class="save-row">
      <button id="saveSettings">Save settings</button>
      <span id="settingsNote" class="muted"></span>
    </div>
  </section>
</main>
<div id="toast"></div>

<script>
const $ = s => document.querySelector(s);
let token = localStorage.getItem("ecoflow_token") || "";
let historyMinutes = 1440;
let controlEnabled = false;
let historyEnabled = false;
let currency = "€";
let lastPoints = [];
let hoverIndex = null;

function authHeaders() { return token ? { "X-Auth-Token": token } : {}; }

function toast(msg) {
  const t = $("#toast"); t.textContent = msg; t.classList.add("show");
  setTimeout(() => t.classList.remove("show"), 2800);
}

function fmtMins(m) {
  if (m == null) return "–";
  if (m >= 6000) return "∞";
  const h = Math.floor(m / 60), mm = m % 60;
  return h ? `${h}h ${mm}m` : `${mm}m`;
}
function fmtRuntime(s) {
  if (s == null || s >= 99999) return "idle";
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h ? `${h}h ${m}m` : `${m}m`;
}
function money(v) { return currency + (v ?? 0).toFixed(2); }

function setPort(id, cls, text, title) {
  const el = $("#" + id); if (!el) return;
  el.className = "pstat " + (cls === "on" ? "on" : cls === "off" ? "off" : "");
  el.innerHTML = `<span class="led ${cls}"></span>${text}`;
  el.title = title || "";
}
function updatePorts(s) {
  // AC has a real on/off flag (flow_info_ac_out); USB/DC do not, so USB is
  // inferred from power draw and DC has no telemetry at all.
  const ac = s.ac_output_on;
  setPort("stAc", ac === true ? "on" : ac === false ? "off" : "unknown",
    ac === true ? "ON" : ac === false ? "OFF" : "?",
    ac == null ? "Awaiting the AC-output flag from the device." : "");
  const usbW = Math.round((s.usb_output_watts ?? 0) + (s.usbc_output_watts ?? 0));
  setPort("stUsb", usbW > 0 ? "on" : "unknown",
    usbW > 0 ? "ON · " + usbW + "W" : "— idle/off",
    usbW > 0 ? "" : "Inferred from power draw; the device reports no USB " +
                    "enable flag, so 0 W means off OR on-but-idle.");
  setPort("stDc", "unknown", "n/a",
    "The DELTA 3 sends no 12V DC telemetry, so its on/off state is unknown.");
}

async function refreshState() {
  try {
    const r = await fetch("api/state", { headers: authHeaders() });
    if (!r.ok) throw new Error(r.status);
    const s = await r.json();
    controlEnabled = s.control_enabled;
    historyEnabled = s.history_enabled;
    $("#soc").textContent = s.soc_percent ?? "–";
    $("#socFill").style.width = (s.soc_percent ?? 0) + "%";
    $("#acIn").textContent = Math.round(s.ac_input_watts ?? 0);
    $("#acOut").textContent = Math.round(s.ac_output_watts ?? 0);
    const usb = (s.usb_output_watts ?? 0) + (s.usbc_output_watts ?? 0);
    $("#usb").textContent = Math.round(usb);
    $("#runtime").textContent = fmtRuntime(s.runtime_seconds);
    const charging = (s.status || "").startsWith("OL");
    $("#remain").textContent = charging
      ? "chg " + fmtMins(s.remain_charge_minutes)
      : "dsg " + fmtMins(s.remain_discharge_minutes);
    const pill = $("#status");
    pill.textContent = s.status ?? "?";
    pill.className = "status-pill " +
      (s.status?.includes("LB") ? "status-LB" : s.status?.startsWith("OL") ? "status-OL" : "status-OB");
    updatePorts(s);
    const eveOn = s.eve_on;
    $("#eveCtl").style.display = s.eve_enabled === true ? "flex" : "none";
    if (s.eve_enabled === true) {
      setPort("stEve",
        eveOn === true ? "on" : eveOn === false ? "off" : "unknown",
        eveOn === true ? "ON" : eveOn === false ? "OFF" : "?",
        eveOn == null ? "Unknown until first command." : "Last commanded state.");
    }
    $("#sbCtl").style.display = s.switchbot_enabled === true ? "flex" : "none";
    applyControlState();
    $("#historyCard").style.display = historyEnabled ? "" : "none";
    $("#energyCard").style.display = historyEnabled ? "" : "none";
  } catch (e) {
    $("#status").textContent = "offline";
    $("#status").className = "status-pill";
  }
}

function applyControlState() {
  const need = controlEnabled && !token;
  const lock = !controlEnabled || need;
  document.querySelectorAll("[data-out]").forEach(b => b.disabled = lock);
  $("#asOn").disabled = $("#asOff").disabled = lock;
  $("#sbPress").disabled = lock;
  $("#saveSettings").disabled = lock;
  $("#tokenRow").style.display = controlEnabled ? "flex" : "none";
  $("#controlNote").textContent = !controlEnabled
    ? "Controls disabled (no auth_token configured on the bridge)."
    : need ? "Enter the control token to enable actions." : "";
}

async function control(output, enabled) {
  // Guard: turning USB off can kill a Pi powered from the DELTA 3's USB port.
  if (output === "usb" && !enabled) {
    if (!confirm(
      "Turn the USB output OFF?\\n\\n" +
      "If this bridge (e.g. a Raspberry Pi) is powered from the DELTA 3's USB " +
      "port, this cuts its OWN power — the dashboard and the bridge will go down.\\n\\n" +
      "Continue only if you are sure nothing critical runs off USB.")) {
      return;
    }
  }
  try {
    const r = await fetch("api/control", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify({ output, enabled }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.reason || r.statusText);
    toast(d.message || "ok");
    setTimeout(refreshState, 800);
  } catch (e) { toast("error: " + e.message); }
}

async function switchbotPress() {
  try {
    const r = await fetch("api/switchbot", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify({ action: "press" }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.reason || r.statusText);
    toast(d.message || "pressed");
  } catch (e) { toast("error: " + e.message); }
}

async function refreshAuto() {
  try {
    const r = await fetch("api/autoshutdown", { headers: authHeaders() });
    if (!r.ok) return;
    const a = await r.json();
    let cls, txt;
    if (!a.enabled) { cls = "off"; txt = "Disabled"; }
    else if (a.triggered) { cls = "crit"; txt = "CUT sent"; }
    else if (a.armed) {
      cls = "warn";
      txt = "ARMED" + (a.seconds_until_cut != null
        ? ` · cutting in ${Math.round(a.seconds_until_cut)}s` : "");
    } else { cls = "on"; txt = "Monitoring"; }
    $("#asLed").className = "led " + cls;
    $("#asState").textContent = txt;
    let d = `trigger ≤ ${a.trigger_soc_percent}%, recover ${a.recover_soc_percent}%, ` +
            `grace ${a.grace_period_seconds}s, cuts: ${(a.cut_outputs || []).join(", ") || "none"}`;
    $("#asDetail").textContent = d;
  } catch (e) {}
}

// Enable/disable auto-shutdown via the settings endpoint (auto_shutdown.enabled).
async function setAuto(enabled) {
  const ok = await saveSettings({ "auto_shutdown.enabled": enabled }, true);
  if (ok) { toast("auto-shutdown " + (enabled ? "enabled" : "disabled")); refreshAuto(); }
}

// ---- chart with hover tooltip ----
const SERIES = [
  { key: "soc_percent", color: "#7ee2a8", max: 100, label: "SoC", unit: "%" },
  { key: "ac_output_watts", color: "#f2c969", max: null, label: "AC out", unit: "W" },
  { key: "ac_input_watts", color: "#a9c2ff", max: null, label: "AC in", unit: "W" },
];
const PAD = 28;
function xAt(i, n, W) { return PAD + (n < 2 ? 0 : (i / (n - 1)) * (W - 2 * PAD)); }
function wattMax(points) {
  let m = 100;
  for (const p of points) m = Math.max(m, p.ac_output_watts || 0, p.ac_input_watts || 0);
  return m;
}
function drawChart() {
  const c = $("#chart"), ctx = c.getContext("2d");
  const W = c.width, H = c.height, points = lastPoints;
  ctx.clearRect(0, 0, W, H);
  if (!points.length) {
    ctx.fillStyle = "#8a93a6"; ctx.font = "13px system-ui";
    ctx.fillText("no data yet", PAD, H / 2); return;
  }
  const wMax = wattMax(points), n = points.length;
  ctx.strokeStyle = "#222631"; ctx.lineWidth = 1;
  ctx.beginPath(); ctx.moveTo(PAD, H - PAD); ctx.lineTo(W - PAD, H - PAD); ctx.stroke();
  for (const s of SERIES) {
    const max = s.max || wMax;
    ctx.strokeStyle = s.color; ctx.lineWidth = 1.8; ctx.beginPath();
    let started = false;
    points.forEach((p, i) => {
      const v = p[s.key]; if (v == null) return;
      const y = H - PAD - (v / max) * (H - 2 * PAD);
      if (!started) { ctx.moveTo(xAt(i, n, W), y); started = true; }
      else ctx.lineTo(xAt(i, n, W), y);
    });
    ctx.stroke();
  }
  if (hoverIndex != null && hoverIndex < n) {
    const x = xAt(hoverIndex, n, W);
    ctx.strokeStyle = "#3a4356"; ctx.lineWidth = 1; ctx.beginPath();
    ctx.moveTo(x, PAD - 8); ctx.lineTo(x, H - PAD); ctx.stroke();
    const p = points[hoverIndex];
    for (const s of SERIES) {
      const v = p[s.key]; if (v == null) continue;
      const max = s.max || wMax, y = H - PAD - (v / max) * (H - 2 * PAD);
      ctx.fillStyle = s.color; ctx.beginPath(); ctx.arc(x, y, 3, 0, 7); ctx.fill();
    }
  }
}
function showTip(p, clientX) {
  const tip = $("#tip"), wrap = $(".chart-wrap").getBoundingClientRect();
  const when = new Date(p.ts).toLocaleString();
  let rows = `<div class="t">${when}</div>`;
  for (const s of SERIES) {
    const v = p[s.key];
    rows += `<div><i style="background:${s.color}"></i>${s.label}: ` +
            `<b>${v == null ? "–" : Math.round(v) + s.unit}</b></div>`;
  }
  tip.innerHTML = rows; tip.style.display = "block";
  let left = clientX - wrap.left + 12;
  if (left + tip.offsetWidth > wrap.width) left = clientX - wrap.left - tip.offsetWidth - 12;
  tip.style.left = Math.max(0, left) + "px"; tip.style.top = "6px";
}
function onMove(e) {
  const c = $("#chart"), rect = c.getBoundingClientRect(), n = lastPoints.length;
  if (!n) return;
  const xCanvas = (e.clientX - rect.left) * (c.width / rect.width);
  let i = Math.round((xCanvas - PAD) / ((c.width - 2 * PAD) || 1) * (n - 1));
  i = Math.max(0, Math.min(n - 1, i));
  hoverIndex = i; drawChart(); showTip(lastPoints[i], e.clientX);
}
function onLeave() { hoverIndex = null; drawChart(); $("#tip").style.display = "none"; }

async function refreshHistory() {
  if (!historyEnabled) return;
  try {
    const r = await fetch("api/history?minutes=" + historyMinutes, { headers: authHeaders() });
    const d = await r.json();
    lastPoints = d.points || []; hoverIndex = null; drawChart();
    $("#historyNote").textContent = lastPoints.length
      ? `${lastPoints.length} points · hover for detail`
      : "Collecting data…";
  } catch (e) { $("#historyNote").textContent = "history unavailable"; }
}

async function refreshEnergy() {
  if (!historyEnabled) return;
  try {
    const r = await fetch("api/energy?minutes=" + historyMinutes, { headers: authHeaders() });
    const d = await r.json();
    if (d.currency) currency = d.currency;
    $("#energyRange").textContent = "· last " + fmtMins(historyMinutes);
    $("#eKwh").textContent = (d.grid_kwh ?? 0).toFixed(2);
    $("#eCost").textContent = d.pricing_enabled ? money(d.total_cost) : "—";
    $("#eHc").innerHTML = `${(d.hc_kwh ?? 0).toFixed(2)} kWh` +
      (d.pricing_enabled ? ` · <b>${money(d.hc_cost)}</b>` : "");
    $("#eHp").innerHTML = `${(d.hp_kwh ?? 0).toFixed(2)} kWh` +
      (d.pricing_enabled ? ` · <b>${money(d.hp_cost)}</b>` : "");
    $("#eAvg").textContent = `${Math.round(d.avg_grid_watts ?? 0)} / ${Math.round(d.peak_grid_watts ?? 0)} W`;
    $("#eProj").innerHTML = d.pricing_enabled
      ? `${money(d.cost_per_day)}/day · <b>${money(d.cost_per_month)}/mo</b>` : "—";
    $("#energyNote").textContent = d.pricing_enabled
      ? `Grid draw priced by HC window ${d.hc_window}. Load delivered: ${(d.load_kwh ?? 0).toFixed(2)} kWh.`
      : "Enable pricing in Settings to see cost. Load delivered: " + (d.load_kwh ?? 0).toFixed(2) + " kWh.";
  } catch (e) { $("#energyNote").textContent = "energy unavailable"; }
}

// ---- settings form ----
const inputId = k => "set_" + k.replace(/[^a-z0-9]/gi, "_");
function fieldInput(f, value) {
  const id = inputId(f.key);
  if (f.type === "bool")
    return `<input type="checkbox" id="${id}" data-key="${f.key}" ${value ? "checked" : ""}>`;
  if (f.type === "time")
    return `<input type="time" id="${id}" data-key="${f.key}" value="${value ?? ""}">`;
  if (f.type === "str")
    return `<input type="text" id="${id}" data-key="${f.key}" value="${value ?? ""}">`;
  const step = f.step != null ? `step="${f.step}"` : "";
  const mn = f.min != null ? `min="${f.min}"` : "", mx = f.max != null ? `max="${f.max}"` : "";
  return `<input type="number" id="${id}" data-key="${f.key}" data-type="${f.type}" ` +
         `${step} ${mn} ${mx} value="${value ?? ""}">`;
}
function renderSettings(schema, values) {
  const groups = {};
  for (const f of schema) (groups[f.group] = groups[f.group] || []).push(f);
  let html = "";
  for (const [group, fields] of Object.entries(groups)) {
    html += `<fieldset><legend>${group}</legend>`;
    for (const f of fields) {
      html += `<div class="frow"><label for="${inputId(f.key)}">${f.label}` +
              (f.help ? `<span class="help">${f.help}</span>` : "") + `</label>` +
              fieldInput(f, values[f.key]) + `</div>`;
    }
    html += `</fieldset>`;
  }
  $("#settingsForm").innerHTML = html;
}
async function loadSettings() {
  try {
    const r = await fetch("api/settings", { headers: authHeaders() });
    if (!r.ok) return;
    const d = await r.json();
    renderSettings(d.fields, d.values);
    applyControlState();
  } catch (e) {}
}
function collectSettings() {
  const out = {};
  document.querySelectorAll("#settingsForm [data-key]").forEach(el => {
    const k = el.dataset.key;
    if (el.type === "checkbox") out[k] = el.checked;
    else if (el.type === "number") {
      if (el.value === "") out[k] = (el.dataset.type === "float_or_null") ? null : 0;
      else out[k] = Number(el.value);
    } else out[k] = el.value;
  });
  return out;
}
async function saveSettings(updates, quiet) {
  try {
    const r = await fetch("api/settings", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...authHeaders() },
      body: JSON.stringify({ updates }),
    });
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.reason || r.statusText);
    if (!quiet) {
      $("#settingsNote").textContent = (d.changed || []).length
        ? `saved ${d.changed.length} change(s)` : "no changes";
      refreshEnergy();
    }
    return true;
  } catch (e) { if (!quiet) $("#settingsNote").textContent = "error: " + e.message;
                else toast("error: " + e.message); return false; }
}

document.querySelectorAll("[data-out]").forEach(b =>
  b.addEventListener("click", () => control(b.dataset.out, b.dataset.on === "1")));
$("#asOn").addEventListener("click", () => setAuto(true));
$("#asOff").addEventListener("click", () => setAuto(false));
$("#sbPress").addEventListener("click", switchbotPress);
$("#saveSettings").addEventListener("click", () => saveSettings(collectSettings(), false));
$("#saveToken").addEventListener("click", () => {
  token = $("#token").value.trim();
  localStorage.setItem("ecoflow_token", token);
  toast("token saved"); applyControlState(); refreshState(); loadSettings();
});
document.querySelectorAll(".range button").forEach(b =>
  b.addEventListener("click", () => {
    document.querySelectorAll(".range button").forEach(x => x.classList.remove("active"));
    b.classList.add("active"); historyMinutes = +b.dataset.min;
    refreshHistory(); refreshEnergy();
  }));
const chart = $("#chart");
chart.addEventListener("mousemove", onMove);
chart.addEventListener("mouseleave", onLeave);

$("#token").value = token;
refreshState(); refreshAuto(); refreshHistory(); refreshEnergy(); loadSettings();
setInterval(refreshState, 4000);
setInterval(refreshAuto, 8000);
setInterval(() => { if (hoverIndex == null) refreshHistory(); }, 30000);
setInterval(refreshEnergy, 60000);
</script>
</body>
</html>
"""
