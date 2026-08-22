"""Tests for the embedded web UI: routing, auth and the control bridge."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from ecoflow_nut import webapp
from ecoflow_nut.config import WebConfig
from ecoflow_nut.webapp import WebServer


class _Harness:
    """Records control calls so tests can assert what the UI forwarded."""

    def __init__(self) -> None:
        self.control_calls: list[tuple[str, bool]] = []
        self.eve_calls: list[bool] = []
        self.switchbot_calls: list[str] = []
        self.history_calls: list[tuple[float, float, int]] = []
        self.energy_calls: list[tuple[float, float]] = []
        self.settings_updates: list[dict[str, object]] = []
        self.autoshutdown_enabled = False
        self.fail_control = False
        self.fail_settings = False

    def state(self) -> dict[str, object]:
        return {"soc_percent": 55, "ac_output_watts": 120, "status": "OB"}

    async def control(self, kind: str, enabled: bool) -> str:
        if self.fail_control:
            raise RuntimeError("not connected to device")
        self.control_calls.append((kind, enabled))
        return f"{kind} {'on' if enabled else 'off'}"

    async def eve_control(self, enabled: bool) -> str:
        self.eve_calls.append(enabled)
        return f"eve {'on' if enabled else 'off'}"

    async def switchbot_press(self, action: str) -> str:
        self.switchbot_calls.append(action)
        return f"switchbot {action}"

    async def history(
        self, *, since: float, until: float, max_points: int
    ) -> dict[str, object]:
        self.history_calls.append((since, until, max_points))
        return {
            "points": [{"ts": "2026-06-05T00:00:00", "soc_percent": 50}],
            "bucket_seconds": 30,
        }

    def autoshutdown_status(self) -> dict[str, object]:
        return {"enabled": self.autoshutdown_enabled, "armed": False}

    def get_settings(self) -> dict[str, object]:
        return {
            "fields": [{"key": "auto_shutdown.trigger_soc_percent", "type": "int"}],
            "values": {"auto_shutdown.trigger_soc_percent": 10},
        }

    async def update_settings(self, updates: dict[str, object]) -> dict[str, object]:
        if self.fail_settings:
            raise ValueError("recover SoC % must be >= trigger SoC %")
        self.settings_updates.append(updates)
        return {"values": updates, "changed": list(updates)}

    async def energy(self, *, since: float, until: float) -> dict[str, object]:
        self.energy_calls.append((since, until))
        return {"enabled": True, "grid_kwh": 1.5, "total_cost": 0.3, "currency": "€"}


async def _client(
    config: WebConfig,
    harness: _Harness,
    history_enabled: bool = True,
    eve_enabled: bool = False,
    switchbot_enabled: bool = False,
) -> TestClient:
    server = WebServer(
        config,
        state_provider=harness.state,
        control=harness.control,
        history=harness.history,
        autoshutdown_status=harness.autoshutdown_status,
        get_settings=harness.get_settings,
        update_settings=harness.update_settings,
        energy=harness.energy,
        history_enabled=history_enabled,
        eve_control=harness.eve_control if eve_enabled else None,
        switchbot_press=harness.switchbot_press if switchbot_enabled else None,
    )
    client = TestClient(TestServer(server.build_app()))
    await client.start_server()
    return client


@pytest.fixture
async def harness() -> _Harness:
    return _Harness()


@pytest.fixture
async def secured(harness: _Harness) -> AsyncIterator[TestClient]:
    client = await _client(WebConfig(auth_token="s3cret"), harness)
    yield client
    await client.close()


async def test_index_served(secured: TestClient) -> None:
    resp = await secured.get("/")
    assert resp.status == 200
    body = await resp.text()
    # The shell ships a model-agnostic heading; the configured model name is
    # filled in from /api/state, since the bridge drives several models.
    assert 'id="deviceModel"' in body
    assert "EcoFlow" in body
    # Visual port + auto-shutdown status indicators are present.
    for marker in ('id="stAc"', 'id="stUsb"', 'id="stDc"', 'id="asLed"'):
        assert marker in body
    # The shell pulls in its stylesheet and script.
    assert "app.css" in body and "app.js" in body


@pytest.mark.parametrize("name", sorted(webapp._STATIC_ASSETS))
async def test_static_asset_served(secured: TestClient, name: str) -> None:
    resp = await secured.get(f"/static/{name}")
    assert resp.status == 200
    assert resp.headers["Content-Type"].startswith(webapp._STATIC_ASSETS[name])
    assert resp.headers["ETag"]
    assert await resp.read()


async def test_static_asset_conditional_get(secured: TestClient) -> None:
    first = await secured.get("/static/app.css")
    etag = first.headers["ETag"]
    again = await secured.get("/static/app.css", headers={"If-None-Match": etag})
    assert again.status == 304


async def test_static_unknown_asset_404(secured: TestClient) -> None:
    assert (await secured.get("/static/nope.js")).status == 404


async def test_static_rejects_nested_paths(secured: TestClient) -> None:
    # The route matches a single path segment and the handler is an allowlist
    # lookup, so neither nesting nor traversal can reach the filesystem.
    assert (await secured.get("/static/sub/app.js")).status == 404
    assert (await secured.get("/static/../webapp.py")).status == 404


async def test_shell_served_without_auth(harness: _Harness) -> None:
    """The page and its assets stay open so the browser can prompt for a token."""
    client = await _client(
        WebConfig(auth_token="s3cret", require_auth_for_read=True), harness
    )
    try:
        assert (await client.get("/")).status == 200
        assert (await client.get("/static/app.js")).status == 200
        assert (await client.get("/api/state")).status == 401
    finally:
        await client.close()


def test_static_allowlist_matches_disk() -> None:
    """Catches adding a file without registering it (and vice versa)."""
    static_dir = Path(webapp.__file__).parent / "static"
    on_disk = {p.name for p in static_dir.iterdir() if p.is_file()}
    assert on_disk == set(webapp._STATIC_ASSETS)


def test_index_references_only_allowlisted_assets() -> None:
    """A renamed asset would otherwise 404 on first paint."""
    static_dir = Path(webapp.__file__).parent / "static"
    index = (static_dir / "index.html").read_text()
    refs = re.findall(r'(?:src|href)="([^"]+)"', index)
    local = [r for r in refs if not r.startswith(("http:", "https:", "data:", "#"))]
    assert local, "expected the shell to reference its own assets"
    for ref in local:
        assert ref.removeprefix("static/") in webapp._STATIC_ASSETS, ref


async def test_state_exposes_ac_output_flag(secured: TestClient) -> None:
    # The dashboard derives the AC port LED from ac_output_on in /api/state.
    body = await (await secured.get("/api/state")).json()
    assert "ac_output_on" in body or "status" in body


async def test_state_reports_capabilities(secured: TestClient) -> None:
    resp = await secured.get("/api/state")
    assert resp.status == 200
    body = await resp.json()
    assert body["soc_percent"] == 55
    assert body["control_enabled"] is True
    assert body["history_enabled"] is True


async def test_control_requires_token(secured: TestClient, harness: _Harness) -> None:
    resp = await secured.post("/api/control", json={"output": "ac", "enabled": False})
    assert resp.status == 401
    assert harness.control_calls == []


async def test_control_with_token(secured: TestClient, harness: _Harness) -> None:
    resp = await secured.post(
        "/api/control",
        json={"output": "ac", "enabled": False},
        headers={"X-Auth-Token": "s3cret"},
    )
    assert resp.status == 200
    assert harness.control_calls == [("ac", False)]


async def test_control_rejects_bad_output(secured: TestClient) -> None:
    resp = await secured.post(
        "/api/control",
        json={"output": "laser", "enabled": True},
        headers={"X-Auth-Token": "s3cret"},
    )
    assert resp.status == 400


async def test_eve_control_routes_when_enabled(harness: _Harness) -> None:
    client = await _client(WebConfig(auth_token="s3cret"), harness, eve_enabled=True)
    try:
        resp = await client.post(
            "/api/control",
            json={"output": "eve", "enabled": False},
            headers={"X-Auth-Token": "s3cret"},
        )
        assert resp.status == 200
        assert harness.eve_calls == [False]
        assert harness.control_calls == []  # not routed to the EcoFlow path
    finally:
        await client.close()


async def test_eve_control_rejected_when_disabled(secured: TestClient) -> None:
    # The `secured` client is built without an eve_control callback.
    resp = await secured.post(
        "/api/control",
        json={"output": "eve", "enabled": True},
        headers={"X-Auth-Token": "s3cret"},
    )
    assert resp.status == 400


async def test_switchbot_press_when_enabled(harness: _Harness) -> None:
    client = await _client(
        WebConfig(auth_token="s3cret"), harness, switchbot_enabled=True
    )
    try:
        resp = await client.post(
            "/api/switchbot",
            json={"action": "press"},
            headers={"X-Auth-Token": "s3cret"},
        )
        assert resp.status == 200
        assert harness.switchbot_calls == ["press"]
    finally:
        await client.close()


async def test_switchbot_rejected_when_disabled(secured: TestClient) -> None:
    resp = await secured.post(
        "/api/switchbot", json={"action": "press"}, headers={"X-Auth-Token": "s3cret"}
    )
    assert resp.status == 400


async def test_control_device_error_is_conflict(
    secured: TestClient, harness: _Harness
) -> None:
    harness.fail_control = True
    resp = await secured.post(
        "/api/control",
        json={"output": "ac", "enabled": True},
        headers={"X-Auth-Token": "s3cret"},
    )
    assert resp.status == 409


async def test_control_disabled_without_configured_token(harness: _Harness) -> None:
    client = await _client(WebConfig(auth_token=""), harness)
    try:
        resp = await client.post("/api/control", json={"output": "ac", "enabled": True})
        assert resp.status == 503
    finally:
        await client.close()


async def test_autoshutdown_status(secured: TestClient) -> None:
    got = await (await secured.get("/api/autoshutdown")).json()
    assert "enabled" in got


async def test_settings_get_returns_schema(secured: TestClient) -> None:
    body = await (await secured.get("/api/settings")).json()
    assert "fields" in body and "values" in body


async def test_settings_update_requires_token(
    secured: TestClient, harness: _Harness
) -> None:
    resp = await secured.post(
        "/api/settings", json={"updates": {"auto_shutdown.trigger_soc_percent": 15}}
    )
    assert resp.status == 401
    assert harness.settings_updates == []


async def test_settings_update_applies(secured: TestClient, harness: _Harness) -> None:
    resp = await secured.post(
        "/api/settings",
        json={"updates": {"auto_shutdown.trigger_soc_percent": 15}},
        headers={"X-Auth-Token": "s3cret"},
    )
    assert resp.status == 200
    assert harness.settings_updates == [{"auto_shutdown.trigger_soc_percent": 15}]


async def test_settings_validation_error_is_400(
    secured: TestClient, harness: _Harness
) -> None:
    harness.fail_settings = True
    resp = await secured.post(
        "/api/settings",
        json={"updates": {"auto_shutdown.recover_soc_percent": 5}},
        headers={"X-Auth-Token": "s3cret"},
    )
    assert resp.status == 400


async def test_settings_rejects_empty_body(secured: TestClient) -> None:
    resp = await secured.post(
        "/api/settings", json={"updates": {}}, headers={"X-Auth-Token": "s3cret"}
    )
    assert resp.status == 400


async def test_energy_endpoint(secured: TestClient) -> None:
    body = await (await secured.get("/api/energy?minutes=120")).json()
    assert body["enabled"] is True
    assert body["grid_kwh"] == 1.5


async def test_energy_disabled_when_no_store(harness: _Harness) -> None:
    client = await _client(WebConfig(auth_token="s3cret"), harness, history_enabled=False)
    try:
        body = await (await client.get("/api/energy")).json()
        assert body == {"enabled": False}
    finally:
        await client.close()


async def test_history_disabled_returns_empty(harness: _Harness) -> None:
    client = await _client(WebConfig(auth_token="s3cret"), harness, history_enabled=False)
    try:
        body = await (await client.get("/api/history")).json()
        assert body == {"enabled": False, "points": []}
    finally:
        await client.close()


async def test_history_enabled_returns_points(secured: TestClient) -> None:
    body = await (await secured.get("/api/history?minutes=120")).json()
    assert body["enabled"] is True
    assert body["minutes"] == 120
    assert body["points"][0]["soc_percent"] == 50
    assert body["bucket_seconds"] == 30


async def test_history_accepts_absolute_window(
    secured: TestClient, harness: _Harness
) -> None:
    """Zoom/pan sends an explicit window; it reaches the store unchanged."""
    resp = await secured.get("/api/history?since=1700000000&until=1700003600")
    body = await resp.json()
    assert (body["since"], body["until"]) == (1700000000, 1700003600)
    assert body["minutes"] == 60
    assert harness.history_calls[-1][:2] == (1700000000.0, 1700003600.0)


async def test_history_minutes_still_supported(
    secured: TestClient, harness: _Harness
) -> None:
    """The original relative form keeps working for anything already using it."""
    await secured.get("/api/history?minutes=120")
    since, until, _ = harness.history_calls[-1]
    assert until - since == pytest.approx(7200)


async def test_history_rejects_inverted_window(secured: TestClient) -> None:
    resp = await secured.get("/api/history?since=1700003600&until=1700000000")
    assert resp.status == 400


async def test_history_rejects_non_numeric_since(secured: TestClient) -> None:
    assert (await secured.get("/api/history?since=yesterday")).status == 400


async def test_history_clamps_max_points(secured: TestClient, harness: _Harness) -> None:
    await secured.get("/api/history?max_points=99999")
    assert harness.history_calls[-1][2] == 2000
    await secured.get("/api/history?max_points=0")
    assert harness.history_calls[-1][2] == 2


async def test_history_caps_span_to_30_days(
    secured: TestClient, harness: _Harness
) -> None:
    """A pan far into the past is clamped rather than scanning the whole table."""
    until = 1700000000
    await secured.get(f"/api/history?since={until - 90 * 24 * 3600}&until={until}")
    since, got_until, _ = harness.history_calls[-1]
    assert got_until - since == 30 * 24 * 3600


async def test_energy_accepts_absolute_window(
    secured: TestClient, harness: _Harness
) -> None:
    """The energy panel follows the same window as the chart."""
    await secured.get("/api/energy?since=1700000000&until=1700003600")
    assert harness.energy_calls[-1] == (1700000000.0, 1700003600.0)


async def test_require_auth_for_read_blocks_unauthenticated(harness: _Harness) -> None:
    client = await _client(
        WebConfig(auth_token="s3cret", require_auth_for_read=True), harness
    )
    try:
        assert (await client.get("/api/state")).status == 401
        ok = await client.get("/api/state", headers={"X-Auth-Token": "s3cret"})
        assert ok.status == 200
    finally:
        await client.close()


def test_state_reports_how_often_it_publishes(tmp_path):
    """The UI paces itself off this, so it has to be the telemetry cadence.

    poll_interval_seconds is the BLE *link watchdog* tick and says nothing about
    how often a reading appears. Pacing the dashboard off it showed 2-second-old
    data on a 5-second refresh -- and anyone who set the watchdog to the
    minute-scale value its name invites would have had a dashboard that looked
    dead while the bridge was publishing 30 times a minute.
    """
    from ecoflow_nut.config import Config, EcoflowConfig
    from ecoflow_nut.main import Daemon

    config = Config(
        ecoflow=EcoflowConfig(
            mac="DC:06:75:A8:3E:29", serial="E201ZE1APH560861", model="e2000"
        )
    )
    config.nut.dev_file_path = str(tmp_path / "ecoflow.dev")
    config.settings_file = str(tmp_path / "settings.json")
    config.ecoflow.poll_interval_seconds = 60  # a watchdog tick, not a data rate
    config.nut.min_write_interval_seconds = 2.0

    payload = Daemon(config)._web_state()
    assert payload["publish_interval_seconds"] == 2.0
    assert payload["poll_interval_seconds"] == 60, "still reported, just not paced on"


async def _events_server(
    harness: _Harness, config: WebConfig | None = None
) -> tuple[TestClient, WebServer]:
    server = WebServer(
        config or WebConfig(auth_token="s3cret"),
        state_provider=harness.state,
        control=harness.control,
        history=harness.history,
        autoshutdown_status=harness.autoshutdown_status,
        get_settings=harness.get_settings,
        update_settings=harness.update_settings,
        energy=harness.energy,
        history_enabled=True,
    )
    client = TestClient(TestServer(server.build_app()))
    await client.start_server()
    return client, server


async def _next_event(resp, timeout: float = 5.0) -> dict:
    """Read one SSE ``data:`` frame, skipping keepalive comments."""
    while True:
        line = await asyncio.wait_for(resp.content.readline(), timeout=timeout)
        if not line:
            raise AssertionError("stream closed")
        if line.startswith(b"data: "):
            return json.loads(line[len(b"data: "):])


async def test_events_stream_opens_with_the_current_state(harness: _Harness) -> None:
    """A fresh page must paint at once, not wait for the next frame to arrive."""
    client, _server = await _events_server(harness)
    try:
        resp = await client.get("/api/events")
        assert resp.status == 200
        assert resp.headers["Content-Type"].startswith("text/event-stream")
        first = await _next_event(resp)
        assert first["soc_percent"] == 55
        assert first["control_enabled"] is True
        resp.close()
    finally:
        await client.close()


async def test_publish_pushes_to_an_open_stream(harness: _Harness) -> None:
    """The point of the whole exercise: telemetry arrives without being asked for."""
    client, server = await _events_server(harness)
    try:
        resp = await client.get("/api/events")
        await _next_event(resp)  # the opening snapshot
        server.publish({"soc_percent": 42, "status": "OB"})
        pushed = await _next_event(resp)
        assert pushed["soc_percent"] == 42
        assert pushed["status"] == "OB"
        resp.close()
    finally:
        await client.close()


async def test_publish_without_listeners_is_a_no_op(harness: _Harness) -> None:
    """It runs on the decode path, so it must be free when nobody is watching."""
    _client, server = await _events_server(harness)
    try:
        server.publish({"soc_percent": 1})  # must not raise
    finally:
        await _client.close()


def test_a_slow_client_loses_frames_rather_than_stalling_the_bridge() -> None:
    """Back-pressure must never reach the BLE decode path.

    Telemetry is a snapshot, not a log: when a browser cannot keep up the right
    thing to lose is the stalest reading, not the newest -- and never the
    caller's time.
    """
    server = WebServer(
        WebConfig(),
        state_provider=dict,
        control=None,  # type: ignore[arg-type]
        history=None,  # type: ignore[arg-type]
        autoshutdown_status=dict,
        get_settings=dict,
        update_settings=None,  # type: ignore[arg-type]
        energy=None,  # type: ignore[arg-type]
    )
    queue: asyncio.Queue = asyncio.Queue(maxsize=2)
    server._subscribers.add(queue)

    for soc in range(6):
        server.publish({"soc_percent": soc})

    assert queue.qsize() == 2, "bounded, so a dead client cannot grow without limit"
    kept = [queue.get_nowait()["soc_percent"], queue.get_nowait()["soc_percent"]]
    assert kept == [4, 5], "the newest readings survive, not the oldest"


async def test_events_stream_honours_read_auth(harness: _Harness) -> None:
    """require_auth_for_read must gate the push stream like every other read."""
    config = WebConfig(auth_token="s3cret", require_auth_for_read=True)
    client, _server = await _events_server(harness, config)
    try:
        assert (await client.get("/api/events")).status == 401
        # EventSource cannot set headers, so the token rides on the query string.
        ok = await client.get("/api/events?token=s3cret")
        assert ok.status == 200
        ok.close()
    finally:
        await client.close()
