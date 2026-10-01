from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from models import Client
from whatsapp import get_session_state, reconnect_session


def _client_row(**overrides):
    fields = dict(
        name="Acme", subdomain="acme",
        openwa_url="http://acme-openwa-1:2785", openwa_session="acme",
        openwa_api_key="key-123", renewal_date=date.today(),
        created_at=datetime.now(timezone.utc),
    )
    fields.update(overrides)
    return Client(**fields)


def _resp(status_code=200, json_body=None, text=""):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_body
    r.text = text
    r.raise_for_status = MagicMock()
    if status_code >= 400:
        r.raise_for_status.side_effect = httpx.HTTPStatusError("err", request=MagicMock(), response=r)
    return r


def _http(get=None, post=None):
    inner = MagicMock()
    inner.get = AsyncMock(side_effect=get or [])
    inner.post = AsyncMock(side_effect=post or [])
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=inner)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx, inner


def _sessions(**fields):
    return _resp(json_body=[{"id": "uuid-1", "name": "acme", **fields}])


@pytest.mark.asyncio
async def test_state_not_configured_makes_no_request():
    with patch("whatsapp.httpx.AsyncClient") as mock_http:
        state = await get_session_state(_client_row(openwa_url=None))
    assert state["status"] == "NOT_CONFIGURED"
    mock_http.assert_not_called()


@pytest.mark.asyncio
async def test_state_ready_includes_phone():
    ctx, inner = _http(get=[_sessions(status="ready", phone="254712345678")])
    with patch("whatsapp.httpx.AsyncClient", return_value=ctx):
        state = await get_session_state(_client_row())
    assert state["status"] == "READY"
    assert state["phone"] == "254712345678"
    assert inner.get.call_count == 1


@pytest.mark.asyncio
async def test_state_qr_ready_fetches_qr_and_relink_flag():
    ctx, _ = _http(get=[
        _sessions(status="qr_ready", needsRelink=True),
        _resp(json_body={"qrCode": "data:image/png;base64,xyz"}),
    ])
    with patch("whatsapp.httpx.AsyncClient", return_value=ctx):
        state = await get_session_state(_client_row())
    assert state["qrCode"] == "data:image/png;base64,xyz"
    assert state["needsRelink"] is True


@pytest.mark.asyncio
async def test_state_unreachable_never_raises():
    ctx, _ = _http(get=[httpx.ConnectError("Temporary failure in name resolution")])
    with patch("whatsapp.httpx.AsyncClient", return_value=ctx):
        state = await get_session_state(_client_row())
    assert state["status"] == "UNREACHABLE"
    assert "name resolution" in state["lastError"]


@pytest.mark.asyncio
async def test_relink_posts_to_relink_endpoint():
    ctx, inner = _http(get=[_sessions(status="ready")], post=[_resp(json_body={})])
    with patch("whatsapp.httpx.AsyncClient", return_value=ctx):
        result = await reconnect_session(_client_row(), "relink")
    assert result == {"ok": True, "detail": None}
    assert inner.post.call_args_list[0][0][0] == "http://acme-openwa-1:2785/api/sessions/uuid-1/relink"


@pytest.mark.asyncio
async def test_reconnect_creates_session_when_missing():
    ctx, inner = _http(
        get=[_resp(json_body=[])],
        post=[_resp(status_code=201, json_body={"id": "new-uuid", "name": "acme"}), _resp(json_body={})],
    )
    with patch("whatsapp.httpx.AsyncClient", return_value=ctx):
        result = await reconnect_session(_client_row(), "restart")
    assert result["ok"] is True
    assert inner.post.call_args_list[0][0][0].endswith("/api/sessions")
    assert inner.post.call_args_list[1][0][0].endswith("/new-uuid/restart")


@pytest.mark.asyncio
async def test_restart_falls_back_to_stop_start_on_old_openwa():
    ctx, inner = _http(
        get=[_sessions(status="disconnected")],
        post=[_resp(status_code=404), _resp(json_body={}), _resp(json_body={})],
    )
    with patch("whatsapp.httpx.AsyncClient", return_value=ctx):
        result = await reconnect_session(_client_row(), "restart")
    assert result["ok"] is True
    urls = [c[0][0] for c in inner.post.call_args_list]
    assert urls[1].endswith("/uuid-1/stop")
    assert urls[2].endswith("/uuid-1/start")


@pytest.mark.asyncio
async def test_relink_on_old_openwa_explains_deploy_needed():
    ctx, _ = _http(get=[_sessions(status="ready")], post=[_resp(status_code=404)])
    with patch("whatsapp.httpx.AsyncClient", return_value=ctx):
        result = await reconnect_session(_client_row(), "relink")
    assert result["ok"] is False
    assert "predates /relink" in result["detail"]


@pytest.mark.asyncio
async def test_reconnect_surfaces_openwa_errors():
    ctx, _ = _http(get=[_sessions(status="ready")], post=[_resp(status_code=500, text="boom")])
    with patch("whatsapp.httpx.AsyncClient", return_value=ctx):
        result = await reconnect_session(_client_row(), "restart")
    assert result["ok"] is False
    assert "500" in result["detail"]


@pytest.mark.asyncio
async def test_reconnect_not_configured_makes_no_request():
    with patch("whatsapp.httpx.AsyncClient") as mock_http:
        result = await reconnect_session(_client_row(openwa_session=None), "relink")
    assert result["ok"] is False
    mock_http.assert_not_called()
