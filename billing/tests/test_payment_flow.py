import pytest
import pytest_asyncio
from decimal import Decimal
from datetime import date, datetime, timedelta, timezone
from httpx import AsyncClient, ASGITransport
from unittest.mock import AsyncMock, patch
import database, main
from models import Client, Payment, PaymentSession, PlanPrice


@pytest.fixture(autouse=True)
def patch_external(monkeypatch):
    monkeypatch.setattr("main.send_to_group", AsyncMock())
    monkeypatch.setattr("main.initiate_stk_push", AsyncMock(return_value={
        "CheckoutRequestID": "ws_CO_TEST_123",
        "ResponseCode": "0",
    }))
    monkeypatch.setattr("main.start_client", AsyncMock())


@pytest_asyncio.fixture
async def http(db_session, monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    monkeypatch.setattr(database, "AsyncSessionLocal", factory)
    import os
    os.environ["BILLING_WEBHOOK_SECRET"] = "test-secret"
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as c:
        yield c


@pytest_asyncio.fixture
async def grace_client(db_session):
    c = Client(
        name="Acme", subdomain="acme", plan="monthly", status="grace",
        renewal_date=date.today() - timedelta(days=1),
        grace_started_at=datetime.now(timezone.utc) - timedelta(hours=2),
        whatsapp_group_id="group@g.us",
        openwa_url="http://localhost:2001",
        openwa_session="acme",
        openwa_api_key="key",
        docker_project="acme",
        created_at=datetime.now(timezone.utc),
    )
    price = PlanPrice(
        plan_type="monthly", amount=Decimal("1500"), currency="KES",
        set_at=datetime.now(timezone.utc), set_by="admin",
    )
    db_session.add(c)
    db_session.add(price)
    await db_session.commit()
    return c


def _signed_body(body: bytes, secret: str = "test-secret") -> dict:
    import hmac, hashlib
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {"Content-Type": "application/json", "X-Webhook-Signature": sig}


@pytest.mark.asyncio
async def test_payment_command_creates_session_and_asks_for_number(http, grace_client):
    body = b'{"event":"message","body":"/payment","fromMe":false,"chatId":"group@g.us"}'
    r = await http.post("/webhook/client/acme", content=body, headers=_signed_body(body))
    assert r.status_code == 200
    main.send_to_group.assert_called_once()
    msg = main.send_to_group.call_args[0][1]
    assert "number" in msg.lower() or "phone" in msg.lower()


@pytest.mark.asyncio
async def test_bot_messages_ignored(http, grace_client):
    body = b'{"event":"message","body":"/payment","fromMe":true,"chatId":"group@g.us"}'
    r = await http.post("/webhook/client/acme", content=body, headers=_signed_body(body))
    assert r.status_code == 200
    main.send_to_group.assert_not_called()


@pytest.mark.asyncio
async def test_phone_reply_triggers_stk_push(http, grace_client, db_session):
    ps = PaymentSession(
        client_id=grace_client.id, state="awaiting_phone",
        created_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    db_session.add(ps)
    await db_session.commit()

    body = b'{"event":"message","body":"0712345678","fromMe":false,"chatId":"group@g.us"}'
    r = await http.post("/webhook/client/acme", content=body, headers=_signed_body(body))
    assert r.status_code == 200
    main.initiate_stk_push.assert_called_once()
    call = main.initiate_stk_push.call_args
    assert "254712345678" in str(call)


@pytest.mark.asyncio
async def test_invalid_phone_asks_again(http, grace_client, db_session):
    ps = PaymentSession(
        client_id=grace_client.id, state="awaiting_phone",
        created_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    db_session.add(ps)
    await db_session.commit()

    body = b'{"event":"message","body":"not-a-phone","fromMe":false,"chatId":"group@g.us"}'
    r = await http.post("/webhook/client/acme", content=body, headers=_signed_body(body))
    assert r.status_code == 200
    main.initiate_stk_push.assert_not_called()
    msg = main.send_to_group.call_args[0][1]
    assert "invalid" in msg.lower() or "number" in msg.lower()


@pytest.mark.asyncio
async def test_mpesa_callback_success_reactivates_client(http, grace_client, db_session):
    payment = Payment(
        client_id=grace_client.id, phone="254712345678", amount=Decimal("1500"),
        status="pending", initiated_at=datetime.now(timezone.utc),
        period_start=date.today(), period_end=date.today() + timedelta(days=30),
    )
    db_session.add(payment)
    await db_session.flush()
    ps = PaymentSession(
        client_id=grace_client.id, state="awaiting_stk_confirm",
        phone="254712345678", checkout_request_id="ws_CO_TEST_123",
        payment_id=payment.id,
        created_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    db_session.add(ps)
    await db_session.commit()

    r = await http.post("/webhook/mpesa", json={
        "Body": {"stkCallback": {
            "CheckoutRequestID": "ws_CO_TEST_123",
            "ResultCode": 0,
            "CallbackMetadata": {
                "Item": [
                    {"Name": "MpesaReceiptNumber", "Value": "QGH7XXXXX"},
                    {"Name": "Amount", "Value": 1500},
                ]
            }
        }}
    })
    assert r.status_code == 200
    await db_session.refresh(grace_client)
    assert grace_client.status == "active"
    main.start_client.assert_called_once()
    msg = main.send_to_group.call_args[0][1]
    assert "confirmed" in msg.lower() or "active" in msg.lower()


@pytest.mark.asyncio
async def test_mpesa_callback_failure_sends_error_message(http, grace_client, db_session):
    payment = Payment(
        client_id=grace_client.id, phone="254712345678", amount=Decimal("1500"),
        status="pending", initiated_at=datetime.now(timezone.utc),
        period_start=date.today(), period_end=date.today() + timedelta(days=30),
    )
    db_session.add(payment)
    await db_session.flush()
    ps = PaymentSession(
        client_id=grace_client.id, state="awaiting_stk_confirm",
        phone="254712345678", checkout_request_id="ws_CO_FAIL_123",
        payment_id=payment.id,
        created_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=10),
    )
    db_session.add(ps)
    await db_session.commit()

    r = await http.post("/webhook/mpesa", json={
        "Body": {"stkCallback": {
            "CheckoutRequestID": "ws_CO_FAIL_123",
            "ResultCode": 1032,
        }}
    })
    assert r.status_code == 200
    await db_session.refresh(payment)
    assert payment.status == "failed"
    msg = main.send_to_group.call_args[0][1]
    assert "failed" in msg.lower() or "cancelled" in msg.lower()


@pytest.mark.asyncio
async def test_auth_check_active_returns_200(http, grace_client, db_session):
    grace_client.status = "active"
    await db_session.commit()
    r = await http.get("/internal/auth-check", headers={"X-Client-Subdomain": "acme"})
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_auth_check_grace_returns_403(http, grace_client):
    r = await http.get("/internal/auth-check", headers={"X-Client-Subdomain": "acme"})
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_auth_check_billing_only_returns_403(http, grace_client, db_session):
    grace_client.status = "billing_only"
    await db_session.commit()
    r = await http.get("/internal/auth-check", headers={"X-Client-Subdomain": "acme"})
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_auth_check_unknown_subdomain_returns_200(http):
    r = await http.get("/internal/auth-check", headers={"X-Client-Subdomain": "unknown"})
    assert r.status_code == 200


@pytest.mark.asyncio
async def test_close_command_sets_status_closed(db_session, monkeypatch):
    """POST /close via by-group webhook closes the client and calls stop_client."""
    from sqlalchemy import select
    import database, main

    stop_mock = AsyncMock()
    send_mock = AsyncMock()
    monkeypatch.setattr("main.stop_client", stop_mock)
    monkeypatch.setattr("main.send_to_group", send_mock)
    monkeypatch.setattr("main.start_client", AsyncMock())

    from sqlalchemy.ext.asyncio import async_sessionmaker
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    monkeypatch.setattr(database, "AsyncSessionLocal", factory)

    from models import Client
    import os
    os.environ["BILLING_WEBHOOK_SECRET"] = ""

    client = Client(
        name="CloseCo", subdomain="closeco", plan="monthly", status="active",
        renewal_date=date.today() + timedelta(days=30),
        whatsapp_group_id="closeco-group@g.us",
        openwa_url="http://localhost:2001",
        openwa_session="closeco",
        openwa_api_key="key",
        docker_project="closeco",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(client)
    await db_session.commit()

    body = b'{"body":"/close","fromMe":false,"type":"chat","chatId":"closeco-group@g.us"}'
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as http_client:
        r = await http_client.post(
            f"/webhook/by-group/closeco-group@g.us",
            content=body,
            headers={"Content-Type": "application/json"},
        )
    assert r.status_code == 200

    await db_session.refresh(client)
    assert client.status == "closed"
    stop_mock.assert_called_once()
