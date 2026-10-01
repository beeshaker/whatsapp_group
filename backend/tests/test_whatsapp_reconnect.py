from unittest.mock import AsyncMock, MagicMock, patch

import httpx

import whatsapp
from whatsapp import get_session_state, reconnect_session


def _resp(status_code=200, json_body=None, text=""):
    r = MagicMock()
    r.status_code = status_code
    r.json.return_value = json_body
    r.text = text
    r.raise_for_status = MagicMock()
    if status_code >= 400:
        r.raise_for_status.side_effect = httpx.HTTPStatusError("err", request=MagicMock(), response=r)
    return r


def _client(get=None, post=None):
    inner = MagicMock()
    inner.get = AsyncMock(side_effect=get or [])
    inner.post = AsyncMock(side_effect=post or [])
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=inner)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx, inner


def _sessions(**fields):
    return _resp(json_body=[{"id": "uuid-abc", "name": "opsgateway", **fields}])


# ── get_session_state ─────────────────────────────────────────────────

async def test_state_ready_reports_phone_and_skips_qr_fetch():
    ctx, inner = _client(get=[_sessions(status="ready", phone="254700000000")])
    with patch("whatsapp.httpx.AsyncClient", return_value=ctx):
        state = await get_session_state()
    assert state["status"] == "READY"
    assert state["phone"] == "254700000000"
    assert state["qrCode"] is None
    assert inner.get.call_count == 1


async def test_state_qr_ready_includes_qr_and_relink_flag():
    ctx, inner = _client(get=[
        _sessions(status="qr_ready", needsRelink=True, lastDisconnectReason="LOGOUT"),
        _resp(json_body={"qrCode": "data:image/png;base64,xyz", "status": "qr_ready"}),
    ])
    with patch("whatsapp.httpx.AsyncClient", return_value=ctx):
        state = await get_session_state()
    assert state["status"] == "QR_READY"
    assert state["qrCode"] == "data:image/png;base64,xyz"
    assert state["needsRelink"] is True
    assert state["lastDisconnectReason"] == "LOGOUT"
    assert "uuid-abc/qr" in inner.get.call_args_list[1][0][0]


async def test_state_tolerates_old_gateway_400_on_qr():
    ctx, _ = _client(get=[_sessions(status="qr_ready"), _resp(status_code=400)])
    with patch("whatsapp.httpx.AsyncClient", return_value=ctx):
        state = await get_session_state()
    assert state["status"] == "QR_READY"
    assert state["qrCode"] is None


async def test_state_failed_passes_through_last_error():
    ctx, _ = _client(get=[_sessions(status="failed", lastError="Chrome crashed")])
    with patch("whatsapp.httpx.AsyncClient", return_value=ctx):
        state = await get_session_state()
    assert state["status"] == "FAILED"
    assert state["lastError"] == "Chrome crashed"


async def test_state_not_found():
    ctx, _ = _client(get=[_resp(json_body=[{"id": "x", "name": "other"}])])
    with patch("whatsapp.httpx.AsyncClient", return_value=ctx):
        state = await get_session_state()
    assert state["status"] == "NOT_FOUND"


async def test_state_unreachable_never_raises():
    ctx, _ = _client(get=[httpx.ConnectError("connection refused")])
    with patch("whatsapp.httpx.AsyncClient", return_value=ctx):
        state = await get_session_state()
    assert state["status"] == "UNREACHABLE"
    assert "connection refused" in state["lastError"]


# ── reconnect_session ─────────────────────────────────────────────────

async def test_restart_posts_to_restart_endpoint():
    ctx, inner = _client(get=[_sessions(status="failed")], post=[_resp(json_body={})])
    with patch("whatsapp.httpx.AsyncClient", return_value=ctx):
        result = await reconnect_session("restart")
    assert result == {"ok": True, "detail": None}
    assert inner.post.call_args_list[0][0][0].endswith("/api/sessions/uuid-abc/restart")


async def test_relink_posts_to_relink_endpoint():
    ctx, inner = _client(get=[_sessions(status="ready")], post=[_resp(json_body={})])
    with patch("whatsapp.httpx.AsyncClient", return_value=ctx):
        result = await reconnect_session("relink")
    assert result["ok"] is True
    assert inner.post.call_args_list[0][0][0].endswith("/api/sessions/uuid-abc/relink")


async def test_reconnect_creates_session_when_missing():
    ctx, inner = _client(
        get=[_resp(json_body=[])],
        post=[_resp(status_code=201, json_body={"id": "new-uuid", "name": "opsgateway"}), _resp(json_body={})],
    )
    with patch("whatsapp.httpx.AsyncClient", return_value=ctx):
        result = await reconnect_session("relink")
    assert result["ok"] is True
    assert inner.post.call_args_list[0][0][0].endswith("/api/sessions")
    assert inner.post.call_args_list[1][0][0].endswith("/new-uuid/relink")


async def test_reconnect_resets_cached_session_uuid():
    whatsapp._session_uuid = "stale-uuid"
    ctx, _ = _client(get=[_sessions(status="ready")], post=[_resp(json_body={})])
    with patch("whatsapp.httpx.AsyncClient", return_value=ctx):
        await reconnect_session("restart")
    assert whatsapp._session_uuid is None


async def test_restart_falls_back_to_stop_start_on_old_gateway():
    ctx, inner = _client(
        get=[_sessions(status="disconnected")],
        post=[_resp(status_code=404), _resp(json_body={}), _resp(json_body={})],
    )
    with patch("whatsapp.httpx.AsyncClient", return_value=ctx):
        result = await reconnect_session("restart")
    assert result["ok"] is True
    urls = [c[0][0] for c in inner.post.call_args_list]
    assert urls[1].endswith("/uuid-abc/stop")
    assert urls[2].endswith("/uuid-abc/start")


async def test_relink_on_old_gateway_reports_clearly():
    ctx, _ = _client(get=[_sessions(status="ready")], post=[_resp(status_code=404)])
    with patch("whatsapp.httpx.AsyncClient", return_value=ctx):
        result = await reconnect_session("relink")
    assert result["ok"] is False
    assert "out of date" in result["detail"]


async def test_reconnect_surfaces_gateway_errors_instead_of_swallowing():
    ctx, _ = _client(get=[_sessions(status="ready")], post=[_resp(status_code=500, text="boom")])
    with patch("whatsapp.httpx.AsyncClient", return_value=ctx):
        result = await reconnect_session("restart")
    assert result["ok"] is False
    assert "500" in result["detail"]


async def test_reconnect_unreachable_never_raises():
    ctx, _ = _client(get=[httpx.ConnectError("connection refused")])
    with patch("whatsapp.httpx.AsyncClient", return_value=ctx):
        result = await reconnect_session("restart")
    assert result["ok"] is False
    assert "connection refused" in result["detail"]
