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


def _mock_post_client(post_side_effect):
    inner = MagicMock()
    inner.post = AsyncMock(side_effect=post_side_effect)
    inner.delete = AsyncMock()
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=inner)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx, inner


# ---------------------------------------------------------------------------
# reconnect_whatsapp create-if-missing
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_reconnect_creates_session_when_missing(auth_http, db_session):
    client = await _make_configured_client(auth_http, db_session, "recon-missing")

    create_resp = MagicMock()
    create_resp.status_code = 201
    create_resp.raise_for_status = MagicMock()
    create_resp.json.return_value = {"id": "new-uuid", "name": "recon-missing"}

    start_resp = MagicMock()
    start_resp.status_code = 200

    ctx, inner = _mock_post_client([create_resp, start_resp])

    with patch("main._get_session_id", new=AsyncMock(return_value=None)), \
         patch("main.httpx.AsyncClient", return_value=ctx):
        r = await auth_http.post(f"/clients/{client.id}/reconnect-whatsapp")

    assert r.status_code == 303
    create_call = inner.post.call_args_list[0]
    assert create_call[0][0].endswith("/api/sessions")
    start_call = inner.post.call_args_list[1]
    assert "new-uuid/start" in start_call[0][0]


@pytest.mark.asyncio
async def test_reconnect_stops_and_starts_existing_session(auth_http, db_session):
    client = await _make_configured_client(auth_http, db_session, "recon-existing")

    start_resp = MagicMock()
    start_resp.status_code = 200
    ctx, inner = _mock_post_client(start_resp)
    inner.post = AsyncMock(return_value=start_resp)

    with patch("main._get_session_id", new=AsyncMock(return_value="existing-uuid")), \
         patch("main.httpx.AsyncClient", return_value=ctx):
        r = await auth_http.post(f"/clients/{client.id}/reconnect-whatsapp")

    assert r.status_code == 303
    stop_call = inner.post.call_args_list[0]
    assert "existing-uuid/stop" in stop_call[0][0]
    start_call = inner.post.call_args_list[1]
    assert "existing-uuid/start" in start_call[0][0]


@pytest.mark.asyncio
async def test_reconnect_noop_when_not_configured(auth_http, db_session):
    await auth_http.post("/clients", data={"name": "Bare", "subdomain": "recon-bare"})
    client = await db_session.scalar(select(Client).where(Client.subdomain == "recon-bare"))

    with patch("main._get_session_id", new=AsyncMock(return_value=None)), \
         patch("main.httpx.AsyncClient") as MockClient:
        r = await auth_http.post(f"/clients/{client.id}/reconnect-whatsapp")

    assert r.status_code == 303
    MockClient.assert_not_called()


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
# _get_session_status returns (status, phone); whatsapp-status surfaces mismatch
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_get_session_status_returns_status_and_phone():
    from main import _get_session_status

    client = Client(
        name="Acme", subdomain="status-phone",
        openwa_url="http://acme-openwa-1:2785", openwa_session="status-phone",
        openwa_api_key="key-123", renewal_date=date.today(),
        created_at=datetime.now(timezone.utc),
    )
    sess_resp = MagicMock()
    sess_resp.raise_for_status = MagicMock()
    sess_resp.json.return_value = [
        {"id": "id1", "name": "status-phone", "status": "ready", "phone": "254712345678"}
    ]
    inner = MagicMock()
    inner.get = AsyncMock(return_value=sess_resp)
    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=inner)
    ctx.__aexit__ = AsyncMock(return_value=False)

    with patch("main.httpx.AsyncClient", return_value=ctx):
        status, phone = await _get_session_status(client)

    assert status == "ready"
    assert phone == "254712345678"


@pytest.mark.asyncio
async def test_whatsapp_status_endpoint_flags_phone_mismatch(auth_http, db_session):
    client = await _make_configured_client(auth_http, db_session, "mismatch-1")
    client.admin_whatsapp_phone = "254700000000"
    await db_session.commit()

    with patch("main._get_session_status", new=AsyncMock(return_value=("ready", "254711111111"))):
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

    with patch("main._get_session_status", new=AsyncMock(return_value=("ready", "254712345678"))):
        r = await auth_http.get(f"/clients/{client.id}/whatsapp-status")

    assert r.json()["phone_mismatch"] is False


@pytest.mark.asyncio
async def test_whatsapp_status_endpoint_no_mismatch_when_admin_phone_unset(auth_http, db_session):
    client = await _make_configured_client(auth_http, db_session, "mismatch-3")

    with patch("main._get_session_status", new=AsyncMock(return_value=("ready", "254712345678"))):
        r = await auth_http.get(f"/clients/{client.id}/whatsapp-status")

    assert r.json()["phone_mismatch"] is False
