import pytest
import pytest_asyncio
from decimal import Decimal
from datetime import date, datetime, timedelta, timezone
from httpx import AsyncClient, ASGITransport
from unittest.mock import AsyncMock, patch
import database, main
from models import Client, GroupTierPrice, Payment, PaymentSession


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
    # ticket_group_tier_id deliberately left unset (None) here: several tests
    # (e.g. test_upgrade_endpoint_rejected_for_unrestricted_client) depend on a
    # freshly-built grace_client being an "unrestricted, no tier" client. Tests
    # that need a priced tier should use the `priced_tier` fixture below and
    # assign it explicitly.
    c = Client(
        name="Acme", subdomain="acme", status="grace",
        renewal_date=date.today() - timedelta(days=1),
        grace_started_at=datetime.now(timezone.utc) - timedelta(hours=2),
        whatsapp_group_id="group@g.us",
        openwa_url="http://localhost:2001",
        openwa_session="acme",
        openwa_api_key="key",
        docker_project="acme",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(c)
    await db_session.commit()
    return c


@pytest_asyncio.fixture
async def priced_tier(db_session):
    tier = GroupTierPrice(
        name="Tier 1", min_groups=1, max_groups=5, amount=Decimal("1500"),
        set_at=datetime.now(timezone.utc), set_by="admin",
    )
    db_session.add(tier)
    await db_session.commit()
    await db_session.refresh(tier)
    return tier


def _signed_body(body: bytes, secret: str = "test-secret") -> dict:
    import hmac, hashlib
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {"Content-Type": "application/json", "X-Webhook-Signature": sig}


def _openwa_signed_body(body: bytes, secret: str = "test-secret") -> dict:
    # OpenWA (webhook.service.js) always prefixes its signature with
    # "sha256=" — this is the real header format production deliveries use.
    import hmac, hashlib
    sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return {"Content-Type": "application/json", "X-Webhook-Signature": f"sha256={sig}"}


def test_verify_sig_accepts_bare_hex_and_sha256_prefixed():
    body = b'{"event":"message.received"}'
    secret = "test-secret"
    import hmac, hashlib
    bare = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert main._verify_sig(secret, body, bare)
    assert main._verify_sig(secret, body, f"sha256={bare}")
    assert not main._verify_sig(secret, body, "sha256=wrongdigest")
    assert not main._verify_sig(secret, body, "wrongdigest")


@pytest.mark.asyncio
async def test_payment_command_creates_session_and_asks_for_number(http, grace_client):
    body = b'{"event":"message","body":"/payment","fromMe":false,"chatId":"group@g.us"}'
    r = await http.post("/webhook/client/acme", content=body, headers=_signed_body(body))
    assert r.status_code == 200
    main.send_to_group.assert_called_once()
    msg = main.send_to_group.call_args[0][1]
    assert "number" in msg.lower() or "phone" in msg.lower()


@pytest.mark.asyncio
async def test_payment_command_via_group_webhook_with_openwa_signature_format(http, grace_client):
    # Regression test: OpenWA's real webhook deliveries sign with a "sha256="
    # prefix (webhook.service.js), which _verify_sig previously rejected
    # outright — silently breaking /payment for every client's by-group
    # webhook in production.
    body = b'{"event":"message","body":"/payment","fromMe":false,"chatId":"group@g.us"}'
    r = await http.post(
        "/webhook/by-group/group@g.us", content=body, headers=_openwa_signed_body(body)
    )
    assert r.status_code == 200
    main.send_to_group.assert_called_once()


@pytest.mark.asyncio
async def test_bot_messages_ignored(http, grace_client):
    body = b'{"event":"message","body":"/payment","fromMe":true,"chatId":"group@g.us"}'
    r = await http.post("/webhook/client/acme", content=body, headers=_signed_body(body))
    assert r.status_code == 200
    main.send_to_group.assert_not_called()


@pytest.mark.asyncio
async def test_phone_reply_triggers_stk_push(http, grace_client, db_session, priced_tier):
    # The renewal charge is resolved from the client's assigned group tier, so
    # this test (the one that actually exercises that tier-amount lookup) needs
    # a priced tier assigned — grace_client itself deliberately has none.
    grace_client.ticket_group_tier_id = priced_tier.id
    await db_session.commit()

    ps = PaymentSession(
        client_id=grace_client.id, state="awaiting_phone",
        created_at=datetime.now(timezone.utc),
        expires_at=datetime.now(timezone.utc) + timedelta(minutes=5),
    )
    db_session.add(ps)
    await db_session.commit()

    # Step 1: reply with a phone number. Current flow asks for confirmation
    # before charging — it does NOT push STK yet.
    body = b'{"event":"message","body":"0712345678","fromMe":false,"chatId":"group@g.us"}'
    r = await http.post("/webhook/client/acme", content=body, headers=_signed_body(body))
    assert r.status_code == 200
    main.initiate_stk_push.assert_not_called()

    # Step 2: confirm with YES — now the STK push fires.
    body2 = b'{"event":"message","body":"YES","fromMe":false,"chatId":"group@g.us"}'
    r2 = await http.post("/webhook/client/acme", content=body2, headers=_signed_body(body2))
    assert r2.status_code == 200
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
        name="CloseCo", subdomain="closeco", status="active",
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


@pytest.mark.asyncio
async def test_upgrade_endpoint_triggers_stk_push(http, grace_client, db_session, monkeypatch):
    # BILLING_WEBHOOK_SECRET is read into a module-level constant at import time,
    # so monkeypatch.setenv (which only changes os.environ) has no effect on code
    # that already captured the old value — patch the constant on `main` directly.
    monkeypatch.setattr(main, "BILLING_WEBHOOK_SECRET", "test-secret")
    from models import GroupTierPrice
    from sqlalchemy import select
    from datetime import datetime, timezone
    from decimal import Decimal
    tier1 = GroupTierPrice(name="Tier 1", min_groups=1, max_groups=5, amount=Decimal("500"), set_at=datetime.now(timezone.utc), set_by="admin")
    tier2 = GroupTierPrice(name="Tier 2", min_groups=6, max_groups=10, amount=Decimal("1200"), set_at=datetime.now(timezone.utc), set_by="admin")
    db_session.add_all([tier1, tier2])
    await db_session.flush()
    grace_client.allowed_ticket_groups = "[]"
    grace_client.ticket_group_tier_id = tier1.id
    await db_session.commit()

    r = await http.post(
        "/api/clients/acme/ticket-groups/upgrade",
        json={"group_id": "g-new@g.us", "phone": "0712345678"},
        headers={"X-Billing-Secret": "test-secret"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "stk_sent"
    main.initiate_stk_push.assert_called_once()
    from models import GroupUpgradeRequest
    reqs = (await db_session.execute(select(GroupUpgradeRequest))).scalars().all()
    assert len(reqs) == 1
    assert reqs[0].status == "pending"
    assert reqs[0].checkout_request_id == "ws_CO_TEST_123"


@pytest.mark.asyncio
async def test_upgrade_endpoint_reuses_pending_request(http, grace_client, db_session, monkeypatch):
    monkeypatch.setattr(main, "BILLING_WEBHOOK_SECRET", "test-secret")
    from models import GroupTierPrice, GroupUpgradeRequest
    from datetime import datetime, timezone
    from decimal import Decimal
    tier1 = GroupTierPrice(name="Tier 1", min_groups=1, max_groups=5, amount=Decimal("500"), set_at=datetime.now(timezone.utc), set_by="admin")
    db_session.add(tier1)
    await db_session.flush()
    grace_client.allowed_ticket_groups = "[]"
    grace_client.ticket_group_tier_id = tier1.id
    existing = GroupUpgradeRequest(
        client_id=grace_client.id, group_id="g-new@g.us", target_tier_id=tier1.id,
        phone="0712345678", amount=Decimal("500"), checkout_request_id="ws_CO_EXISTING",
        status="pending", created_at=datetime.now(timezone.utc),
    )
    db_session.add(existing)
    await db_session.commit()

    r = await http.post(
        "/api/clients/acme/ticket-groups/upgrade",
        json={"group_id": "g-new@g.us", "phone": "0712345678"},
        headers={"X-Billing-Secret": "test-secret"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "pending_exists"
    main.initiate_stk_push.assert_not_called()


@pytest.mark.asyncio
async def test_upgrade_endpoint_refuses_unpriced_next_tier(http, grace_client, db_session, monkeypatch):
    """Same hazard class already guarded against on the renewal /payment flow
    (see test_tier_billing.py::test_payment_confirm_with_no_tier_never_pushes_stk):
    tiers seed at amount=0 until an admin sets real prices via /prices, so a
    client whose NEXT tier hasn't been priced yet must not trigger an M-Pesa
    STK push for KES 0."""
    monkeypatch.setattr(main, "BILLING_WEBHOOK_SECRET", "test-secret")
    from models import GroupTierPrice, GroupUpgradeRequest
    from sqlalchemy import select
    from datetime import datetime, timezone
    from decimal import Decimal
    tier1 = GroupTierPrice(name="Tier 1", min_groups=1, max_groups=5, amount=Decimal("500"), set_at=datetime.now(timezone.utc), set_by="admin")
    tier2 = GroupTierPrice(name="Tier 2", min_groups=6, max_groups=10, amount=Decimal("0"), set_at=datetime.now(timezone.utc), set_by="admin")
    db_session.add_all([tier1, tier2])
    await db_session.flush()
    grace_client.allowed_ticket_groups = "[]"
    grace_client.ticket_group_tier_id = tier1.id
    await db_session.commit()

    r = await http.post(
        "/api/clients/acme/ticket-groups/upgrade",
        json={"group_id": "g-new@g.us", "phone": "0712345678"},
        headers={"X-Billing-Secret": "test-secret"},
    )
    assert r.status_code == 400
    main.initiate_stk_push.assert_not_called()
    reqs = (await db_session.execute(select(GroupUpgradeRequest))).scalars().all()
    assert reqs == []


@pytest.mark.asyncio
async def test_upgrade_endpoint_rejected_for_unrestricted_client(http, grace_client, db_session, monkeypatch):
    """Regression (Finding A, re-review): the same None-vs-empty-list bug fixed
    in the self-service add endpoint also existed here — an unrestricted client
    (allowed_ticket_groups is None) must not be able to kick off a tier-upgrade
    M-Pesa charge, since that would silently convert them to restricted once
    the callback lands."""
    monkeypatch.setattr(main, "BILLING_WEBHOOK_SECRET", "test-secret")
    from models import GroupUpgradeRequest
    from sqlalchemy import select
    assert grace_client.allowed_ticket_groups is None

    r = await http.post(
        "/api/clients/acme/ticket-groups/upgrade",
        json={"group_id": "g-new@g.us", "phone": "0712345678"},
        headers={"X-Billing-Secret": "test-secret"},
    )
    assert r.status_code == 403
    main.initiate_stk_push.assert_not_called()
    reqs = (await db_session.execute(select(GroupUpgradeRequest))).scalars().all()
    assert reqs == []
    await db_session.refresh(grace_client)
    assert grace_client.allowed_ticket_groups is None
    assert grace_client.ticket_group_tier_id is None


@pytest.mark.asyncio
async def test_mpesa_callback_confirms_group_upgrade(http, grace_client, db_session, monkeypatch):
    monkeypatch.setattr(main, "BILLING_WEBHOOK_SECRET", "test-secret")
    from models import GroupTierPrice, GroupUpgradeRequest
    from datetime import datetime, timezone
    from decimal import Decimal
    tier2 = GroupTierPrice(name="Tier 2", min_groups=6, max_groups=10, amount=Decimal("1200"), set_at=datetime.now(timezone.utc), set_by="admin")
    db_session.add(tier2)
    await db_session.flush()
    grace_client.allowed_ticket_groups = "[]"
    req = GroupUpgradeRequest(
        client_id=grace_client.id, group_id="g-new@g.us", target_tier_id=tier2.id,
        phone="254712345678", amount=Decimal("1200"), checkout_request_id="ws_CO_UPGRADE_1",
        status="pending", created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    await db_session.commit()

    r = await http.post("/webhook/mpesa", json={
        "Body": {"stkCallback": {
            "CheckoutRequestID": "ws_CO_UPGRADE_1",
            "ResultCode": 0,
            "CallbackMetadata": {"Item": [{"Name": "MpesaReceiptNumber", "Value": "QGH8XXXXX"}]},
        }}
    })
    assert r.status_code == 200
    await db_session.refresh(grace_client)
    await db_session.refresh(req)
    import json as _json
    assert _json.loads(grace_client.allowed_ticket_groups) == ["g-new@g.us"]
    assert grace_client.ticket_group_tier_id == tier2.id
    assert req.status == "confirmed"
    # Renewal state machine must be untouched
    assert grace_client.status == "grace"


@pytest.mark.asyncio
async def test_mpesa_callback_marks_group_upgrade_failed(http, grace_client, db_session, monkeypatch):
    monkeypatch.setattr(main, "BILLING_WEBHOOK_SECRET", "test-secret")
    from models import GroupTierPrice, GroupUpgradeRequest
    from datetime import datetime, timezone
    from decimal import Decimal
    tier2 = GroupTierPrice(name="Tier 2", min_groups=6, max_groups=10, amount=Decimal("1200"), set_at=datetime.now(timezone.utc), set_by="admin")
    db_session.add(tier2)
    await db_session.flush()
    grace_client.allowed_ticket_groups = "[]"
    req = GroupUpgradeRequest(
        client_id=grace_client.id, group_id="g-new@g.us", target_tier_id=tier2.id,
        phone="254712345678", amount=Decimal("1200"), checkout_request_id="ws_CO_UPGRADE_FAIL",
        status="pending", created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    await db_session.commit()

    r = await http.post("/webhook/mpesa", json={
        "Body": {"stkCallback": {"CheckoutRequestID": "ws_CO_UPGRADE_FAIL", "ResultCode": 1032}}
    })
    assert r.status_code == 200
    await db_session.refresh(req)
    await db_session.refresh(grace_client)
    assert req.status == "failed"
    assert grace_client.allowed_ticket_groups == "[]"


@pytest.mark.asyncio
async def test_mpesa_callback_group_upgrade_handles_deleted_client(http, db_session, monkeypatch):
    monkeypatch.setattr(main, "BILLING_WEBHOOK_SECRET", "test-secret")
    from models import GroupTierPrice, GroupUpgradeRequest
    from datetime import datetime, timezone
    from decimal import Decimal
    tier2 = GroupTierPrice(name="Tier 2", min_groups=6, max_groups=10, amount=Decimal("1200"), set_at=datetime.now(timezone.utc), set_by="admin")
    db_session.add(tier2)
    await db_session.flush()
    req = GroupUpgradeRequest(
        client_id=999999, group_id="g-new@g.us", target_tier_id=tier2.id,
        phone="254712345678", amount=Decimal("1200"), checkout_request_id="ws_CO_UPGRADE_NOCLIENT",
        status="pending", created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    await db_session.commit()

    r = await http.post("/webhook/mpesa", json={
        "Body": {"stkCallback": {
            "CheckoutRequestID": "ws_CO_UPGRADE_NOCLIENT",
            "ResultCode": 0,
            "CallbackMetadata": {"Item": [{"Name": "MpesaReceiptNumber", "Value": "QGH8XXXXX"}]},
        }}
    })
    assert r.status_code == 200
