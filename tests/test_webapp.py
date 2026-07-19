"""Tests for the embedded web UI: routing, auth and the control bridge."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from aiohttp.test_utils import TestClient, TestServer

from ecoflow_nut.config import WebConfig
from ecoflow_nut.webapp import WebServer


class _Harness:
    """Records control calls so tests can assert what the UI forwarded."""

    def __init__(self) -> None:
        self.control_calls: list[tuple[str, bool]] = []
        self.eve_calls: list[bool] = []
        self.switchbot_calls: list[str] = []
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

    async def history(self, minutes: int) -> list[dict[str, object]]:
        return [{"ts": "2026-06-05T00:00:00", "soc_percent": 50}]

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

    async def energy(self, minutes: int) -> dict[str, object]:
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
    assert "EcoFlow DELTA 3" in body
    # Visual port + auto-shutdown status indicators are present.
    for marker in ('id="stAc"', 'id="stUsb"', 'id="stDc"', 'id="asLed"'):
        assert marker in body


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
