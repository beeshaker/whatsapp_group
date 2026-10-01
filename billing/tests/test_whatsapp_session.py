from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select

import database, main
from models import Client


@pytest_asyncio.fixture
async def auth_http(db_session, monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    monkeypatch.setattr(database, "AsyncSessionLocal", factory)
    from models import AdminUser
    from auth import hash_password
    db_session.add(AdminUser(
        username="admin", hashed_password=hash_password("pw"),
        created_at=datetime.now(timezone.utc),
    ))
    await db_session.commit()
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as c:
        await c.post("/login", data={"username": "admin", "password": "pw"})
        yield c


async def _make_configured_client(auth_http, db_session, subdomain):
    await auth_http.post("/clients", data={"name": "Acme", "subdomain": subdomain})
    client = await db_session.scalar(select(Client).where(Client.subdomain == subdomain))
    await auth_http.post(f"/clients/{client.id}", data={
        "openwa_url": "http://acme-openwa-1:2785",
        "openwa_session": subdomain,
        "openwa_api_key": "key-123",
        "docker_project": subdomain,
    })
    await db_session.refresh(client)
    return client


def _state(phone, status="READY"):
    return {"status": status, "phone": phone, "qrCode": None, "lastError": None,
            "lastDisconnectReason": None, "needsRelink": False}


def _mock_post_client(post_side_effect):
    inner = MagicMock()
    inner.post = AsyncMock(side_effect=post_side_effect)
    inner.delete = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=inner)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx, inner


# ---------------------------------------------------------------------------
# restart / relink endpoints (thin wrappers over whatsapp.reconnect_session)
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_restart_endpoint_calls_restart(auth_http, db_session):
    client = await _make_configured_client(auth_http, db_session, "recon-restart")
    with patch("main.reconnect_session", new=AsyncMock(return_value={"ok": True, "detail": None})) as m:
        r = await auth_http.post(f"/clients/{client.id}/restart-whatsapp")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert m.await_args[0][1] == "restart"


@pytest.mark.asyncio
async def test_relink_endpoint_surfaces_failure_as_502(auth_http, db_session):
    client = await _make_configured_client(auth_http, db_session, "recon-relink")
    with patch("main.reconnect_session", new=AsyncMock(return_value={"ok": False, "detail": "boom"})) as m:
        r = await auth_http.post(f"/clients/{client.id}/relink-whatsapp")
    assert r.status_code == 502
    assert r.json()["detail"] == "boom"
    assert m.await_args[0][1] == "relink"


@pytest.mark.asyncio
async def test_reconnect_form_post_relinks_and_redirects_to_qr_page(auth_http, db_session):
    client = await _make_configured_client(auth_http, db_session, "recon-form")
    with patch("main.reconnect_session", new=AsyncMock(return_value={"ok": True, "detail": None})) as m:
        r = await auth_http.post(f"/clients/{client.id}/reconnect-whatsapp")
    assert r.status_code == 303
    assert r.headers["location"].endswith(f"/clients/{client.id}/reconnect")
    assert m.await_args[0][1] == "relink"


@pytest.mark.asyncio
async def test_restart_endpoint_404_for_missing_client(auth_http):
    r = await auth_http.post("/clients/999999/restart-whatsapp")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_whatsapp_qr_returns_session_state(auth_http, db_session):
    client = await _make_configured_client(auth_http, db_session, "recon-qr")
    state = {**_state(None, status="QR_READY"), "qrCode": "data:x", "needsRelink": True}
    with patch("main.get_session_state", new=AsyncMock(return_value=state)):
        r = await auth_http.get(f"/clients/{client.id}/whatsapp-qr")
    assert r.json() == state


# ---------------------------------------------------------------------------
# disconnect_whatsapp
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_disconnect_deletes_session_when_found(auth_http, db_session):
    client = await _make_configured_client(auth_http, db_session, "disc-found")

    delete_resp = MagicMock()
    delete_resp.status_code = 204
    inner = MagicMock()
    inner.delete = AsyncMock(return_value=delete_resp)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=inner)
    ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("main._get_session_id", new=AsyncMock(return_value="sess-to-kill")), \
         patch("main.httpx.AsyncClient", return_value=ctx):
        r = await auth_http.post(f"/clients/{client.id}/disconnect-whatsapp")

    assert r.status_code == 303
    assert "disconnected=1" in r.headers["location"]
    delete_call = inner.delete.call_args_list[0]
    assert "sess-to-kill" in delete_call[0][0]


@pytest.mark.asyncio
async def test_disconnect_noop_when_no_session(auth_http, db_session):
    client = await _make_configured_client(auth_http, db_session, "disc-none")

    with patch("main._get_session_id", new=AsyncMock(return_value=None)), \
         patch("main.httpx.AsyncClient") as MockClient:
        r = await auth_http.post(f"/clients/{client.id}/disconnect-whatsapp")

    assert r.status_code == 303
    MockClient.assert_not_called()


@pytest.mark.asyncio
async def test_disconnect_404_for_missing_client(auth_http):
    r = await auth_http.post("/clients/999999/disconnect-whatsapp")
    assert r.status_code == 404


# ---------------------------------------------------------------------------
# whatsapp-status surfaces session state + phone mismatch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_whatsapp_status_endpoint_passes_through_failure_details(auth_http, db_session):
    client = await _make_configured_client(auth_http, db_session, "status-failed")
    state = {**_state(None, status="FAILED"), "lastError": "Chrome crashed", "lastDisconnectReason": "LOGOUT"}

    with patch("main.get_session_state", new=AsyncMock(return_value=state)):
        r = await auth_http.get(f"/clients/{client.id}/whatsapp-status")

    body = r.json()
    assert body["status"] == "FAILED"
    assert body["lastError"] == "Chrome crashed"
    assert body["lastDisconnectReason"] == "LOGOUT"
    assert body["phone_mismatch"] is False


@pytest.mark.asyncio
async def test_whatsapp_status_endpoint_flags_phone_mismatch(auth_http, db_session):
    client = await _make_configured_client(auth_http, db_session, "mismatch-1")
    client.admin_whatsapp_phone = "254700000000"
    await db_session.commit()

    with patch("main.get_session_state", new=AsyncMock(return_value=_state("254711111111"))):
        r = await auth_http.get(f"/clients/{client.id}/whatsapp-status")

    assert r.status_code == 200
    body = r.json()
    assert body["phone"] == "254711111111"
    assert body["admin_phone"] == "254700000000"
    assert body["phone_mismatch"] is True


@pytest.mark.asyncio
async def test_whatsapp_status_endpoint_no_mismatch_when_formats_differ(auth_http, db_session):
    client = await _make_configured_client(auth_http, db_session, "mismatch-2")
    client.admin_whatsapp_phone = "0712345678"
    await db_session.commit()

    with patch("main.get_session_state", new=AsyncMock(return_value=_state("254712345678"))):
        r = await auth_http.get(f"/clients/{client.id}/whatsapp-status")

    assert r.json()["phone_mismatch"] is False


@pytest.mark.asyncio
async def test_whatsapp_status_endpoint_no_mismatch_when_admin_phone_unset(auth_http, db_session):
    client = await _make_configured_client(auth_http, db_session, "mismatch-3")

    with patch("main.get_session_state", new=AsyncMock(return_value=_state("254712345678"))):
        r = await auth_http.get(f"/clients/{client.id}/whatsapp-status")

    assert r.json()["phone_mismatch"] is False
