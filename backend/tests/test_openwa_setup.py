from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest


def _mock_request_client(request_side_effect):
    """Build a mock AsyncClient context manager with a configurable .request() side_effect."""
    inner = MagicMock()
    inner.request = AsyncMock(side_effect=request_side_effect)

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=inner)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx, inner


async def test_openwa_proxy_returns_json_error_when_upstream_unreachable(client):
    ctx, inner = _mock_request_client(httpx.ConnectError("connection refused"))
    with patch("main.httpx.AsyncClient", return_value=ctx):
        r = await client.get("/api/openwa/sessions")

    assert r.status_code == 502
    body = r.json()
    assert "error" in body


async def test_setup_session_name_returns_configured_session(client):
    import whatsapp as _wa
    with patch.object(_wa, "OPENWA_SESSION", "dunhill"):
        r = await client.get("/api/setup/session-name")
    assert r.status_code == 200
    assert r.json() == {"sessionName": "dunhill"}


async def test_settings_restart_returns_ok(authenticated_client):
    with patch("whatsapp.reconnect_session", new=AsyncMock(return_value={"ok": True, "detail": None})) as m:
        r = await authenticated_client.post("/api/settings/whatsapp-restart")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    m.assert_awaited_once_with("restart")


async def test_settings_relink_surfaces_failure_as_502(authenticated_client):
    with patch("whatsapp.reconnect_session", new=AsyncMock(return_value={"ok": False, "detail": "nope"})) as m:
        r = await authenticated_client.post("/api/settings/whatsapp-relink")
    assert r.status_code == 502
    assert r.json()["detail"] == "nope"
    m.assert_awaited_once_with("relink")


async def test_settings_status_returns_session_state(authenticated_client):
    state = {"status": "QR_READY", "qrCode": "data:x", "phone": None,
             "lastError": None, "lastDisconnectReason": "LOGOUT", "needsRelink": True}
    with patch("whatsapp.get_session_state", new=AsyncMock(return_value=state)):
        r = await authenticated_client.get("/api/settings/whatsapp-status")
    assert r.status_code == 200
    assert r.json() == state


async def test_openwa_proxy_passes_through_json_on_success(client):
    upstream_resp = MagicMock()
    upstream_resp.status_code = 200
    upstream_resp.headers = {"content-type": "application/json"}
    upstream_resp.json.return_value = [{"id": "uuid-1", "name": "opsgateway"}]

    ctx, inner = _mock_request_client(None)
    inner.request = AsyncMock(return_value=upstream_resp)
    with patch("main.httpx.AsyncClient", return_value=ctx):
        r = await client.get("/api/openwa/sessions")

    assert r.status_code == 200
    assert r.json() == [{"id": "uuid-1", "name": "opsgateway"}]
