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


async def test_reconnect_creates_session_when_missing(authenticated_client):
    create_resp = MagicMock()
    create_resp.status_code = 201
    create_resp.json.return_value = {"id": "new-uuid", "name": "opsgateway"}
    create_resp.raise_for_status = MagicMock()

    start_resp = MagicMock()
    start_resp.status_code = 200

    inner = MagicMock()
    inner.post = AsyncMock(side_effect=[create_resp, start_resp])
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=inner)
    ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("main._openwa_find_session", new=AsyncMock(return_value=(None, None))), \
         patch("main.httpx.AsyncClient", return_value=ctx):
        r = await authenticated_client.post("/api/settings/whatsapp-reconnect")

    assert r.status_code == 200
    assert r.json()["ok"] is True

    create_call = inner.post.call_args_list[0]
    assert create_call[0][0].endswith("/api/sessions")
    start_call = inner.post.call_args_list[1]
    assert "new-uuid/start" in start_call[0][0]


async def test_reconnect_stops_and_starts_existing_session(authenticated_client):
    start_resp = MagicMock()
    start_resp.status_code = 200

    inner = MagicMock()
    inner.post = AsyncMock(return_value=start_resp)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=inner)
    ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("main._openwa_find_session", new=AsyncMock(return_value=("existing-uuid", "disconnected"))), \
         patch("main.httpx.AsyncClient", return_value=ctx):
        r = await authenticated_client.post("/api/settings/whatsapp-reconnect")

    assert r.status_code == 200
    assert r.json()["ok"] is True

    stop_call = inner.post.call_args_list[0]
    assert "existing-uuid/stop" in stop_call[0][0]
    start_call = inner.post.call_args_list[1]
    assert "existing-uuid/start" in start_call[0][0]


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
