"""Focused verification for Task 3 (tier-only billing) logic in main.py.

This is a NEW file. The pre-existing route test files (test_clients.py,
test_payment_flow.py, test_models.py, ...) still construct clients with the
removed `plan=` kwarg / import the deleted `PlanPrice` model, and are the later
test-suite task's responsibility to rewrite — they are intentionally NOT touched
here. These tests cover the isolated, high-risk pieces of this task:

  * the fixed 30-day cadence helpers,
  * tier seeding carrying placeholder names (and its de-duplication),
  * the tier-or-graceful-failure resolver,
  * the guarantee that a client with no/unpriced tier NEVER triggers an STK push,
  * set_group_tier_prices validation, and
  * create_client auto-assigning the lowest tier while leaving
    allowed_ticket_groups untouched (None / unrestricted).
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select
from starlette.responses import RedirectResponse

import main
from models import Client, GroupTierPrice, PaymentSession


def test_next_renewal_is_fixed_30_days():
    assert main._next_renewal() == date.today() + timedelta(days=30)


def test_period_end_is_fixed_30_days():
    assert main._period_end(date(2026, 1, 1)) == date(2026, 1, 31)


@pytest.mark.asyncio
async def test_seed_group_tiers_have_names_and_are_idempotent(db_session):
    tiers = await main._get_or_seed_group_tiers(db_session)
    assert [t.name for t in tiers] == ["Tier 1", "Tier 2", "Tier 3"]
    assert [(t.min_groups, t.max_groups) for t in tiers] == [(1, 5), (6, 10), (11, None)]
    # Second call must not create duplicates (single source of truth).
    again = await main._get_or_seed_group_tiers(db_session)
    assert len(again) == 3


@pytest.mark.asyncio
async def test_resolve_renewal_charge_no_tier(db_session):
    client = Client(name="Legacy", subdomain="legacy", renewal_date=date.today(),
                    created_at=datetime.now(timezone.utc))  # ticket_group_tier_id defaults None
    amount, tier = await main._resolve_renewal_charge(client, db_session)
    assert amount is None
    assert tier is None


@pytest.mark.asyncio
async def test_resolve_renewal_charge_unpriced_tier_is_graceful(db_session):
    tiers = await main._get_or_seed_group_tiers(db_session)  # all seed at amount 0
    client = Client(name="Zero", subdomain="zero", renewal_date=date.today(),
                    created_at=datetime.now(timezone.utc), ticket_group_tier_id=tiers[0].id)
    db_session.add(client)
    await db_session.commit()
    amount, tier = await main._resolve_renewal_charge(client, db_session)
    assert amount is None                 # amount <= 0 => cannot charge
    assert tier is not None               # but the tier itself is returned for context
    assert tier.id == tiers[0].id


@pytest.mark.asyncio
async def test_resolve_renewal_charge_priced_tier(db_session):
    tiers = await main._get_or_seed_group_tiers(db_session)
    tiers[0].amount = Decimal("500")
    await db_session.commit()
    client = Client(name="Paid", subdomain="paid", renewal_date=date.today(),
                    created_at=datetime.now(timezone.utc), ticket_group_tier_id=tiers[0].id)
    db_session.add(client)
    await db_session.commit()
    amount, tier = await main._resolve_renewal_charge(client, db_session)
    assert amount == Decimal("500")
    assert tier.id == tiers[0].id


@pytest.mark.asyncio
async def test_payment_confirm_with_no_tier_never_pushes_stk(db_session, monkeypatch):
    sent: list[str] = []

    async def fake_send(client, msg):
        sent.append(msg)

    async def fake_stk(**kwargs):
        raise AssertionError("initiate_stk_push must NOT be called for a no/unpriced tier")

    monkeypatch.setattr(main, "send_to_group", fake_send)
    monkeypatch.setattr(main, "initiate_stk_push", fake_stk)

    client = Client(name="NoTier", subdomain="notier", renewal_date=date.today(),
                    created_at=datetime.now(timezone.utc))  # no tier assigned
    db_session.add(client)
    await db_session.commit()
    await db_session.refresh(client)

    now = datetime.now(timezone.utc)
    session = PaymentSession(
        client_id=client.id, state="awaiting_confirm", phone="254712345678",
        created_at=now, expires_at=now + timedelta(minutes=5),
    )
    db_session.add(session)
    await db_session.commit()

    # User confirms — must fail gracefully, no STK push, session cleaned up.
    await main._process_client_message(client, {"body": "yes"}, db_session)

    remaining = (await db_session.execute(
        select(PaymentSession).where(PaymentSession.client_id == client.id)
    )).scalars().all()
    assert remaining == []
    assert sent and "tier" in sent[-1].lower()


@pytest.mark.asyncio
async def test_set_group_tier_prices_validation(db_session, monkeypatch):
    await main._get_or_seed_group_tiers(db_session)
    captured: dict = {}

    def fake_tr(request, name, context):
        captured["ctx"] = context
        return context

    monkeypatch.setattr(main.templates, "TemplateResponse", fake_tr)

    async def call(**kw):
        captured.clear()
        await main.set_group_tier_prices(request=None, username="admin", db=db_session, **kw)
        return captured["ctx"]["group_tier_error"]

    assert "blank" in (await call(
        tier1_name="", tier1_amount="1", tier2_name="B", tier2_amount="2",
        tier3_name="C", tier3_amount="3")).lower()

    assert "unique" in (await call(
        tier1_name="A", tier1_amount="1", tier2_name="a", tier2_amount="2",
        tier3_name="C", tier3_amount="3")).lower()

    assert "greater than zero" in (await call(
        tier1_name="A", tier1_amount="0", tier2_name="B", tier2_amount="2",
        tier3_name="C", tier3_amount="3")).lower()

    assert "less than" in (await call(
        tier1_name="A", tier1_amount="5", tier2_name="B", tier2_amount="2",
        tier3_name="C", tier3_amount="3")).lower()


@pytest.mark.asyncio
async def test_set_group_tier_prices_success_updates_names_and_amounts(db_session):
    await main._get_or_seed_group_tiers(db_session)
    resp = await main.set_group_tier_prices(
        request=None, tier1_name="Starter", tier1_amount="100",
        tier2_name="Pro", tier2_amount="200", tier3_name="Max", tier3_amount="300",
        username="admin", db=db_session,
    )
    assert isinstance(resp, RedirectResponse)
    tiers = await main._get_or_seed_group_tiers(db_session)
    assert [t.name for t in tiers] == ["Starter", "Pro", "Max"]
    assert [t.amount for t in tiers] == [Decimal("100"), Decimal("200"), Decimal("300")]


@pytest.mark.asyncio
async def test_create_client_defaults_lowest_tier_and_leaves_groups_unrestricted(db_session):
    resp = await main.create_client(
        request=None, name="New", subdomain="NewCo", ticket_group_tier_id="",
        admin_whatsapp_phone="", backend_port="", username="admin", db=db_session,
    )
    assert isinstance(resp, RedirectResponse)
    client = (await db_session.execute(
        select(Client).where(Client.subdomain == "newco")
    )).scalar_one()
    tiers = await main._get_or_seed_group_tiers(db_session)
    assert client.ticket_group_tier_id == tiers[0].id      # lowest tier auto-assigned
    assert client.allowed_ticket_groups is None            # orthogonal — stays unrestricted
    assert client.renewal_date == date.today() + timedelta(days=30)
