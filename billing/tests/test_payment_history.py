"""Tests for billing/payment_history.py — the unified Payment +
GroupUpgradeRequest merge helper introduced by Task 4 (tier-only billing
refactor). Independent of FastAPI wiring; exercises the helper directly
against an in-memory DB via the shared db_session fixture (see conftest.py).
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

import pytest

from models import Client, GroupTierPrice, GroupUpgradeRequest, Payment
from payment_history import unified_payment_history


async def _make_client(db_session, name="Acme", subdomain="acme"):
    client = Client(
        name=name, subdomain=subdomain, status="active",
        renewal_date=date.today(), created_at=datetime.now(timezone.utc),
    )
    db_session.add(client)
    await db_session.commit()
    await db_session.refresh(client)
    return client


async def _make_tier(db_session, name="Growth", amount="500"):
    tier = GroupTierPrice(
        name=name, min_groups=1, max_groups=5, amount=Decimal(amount),
        set_at=datetime.now(timezone.utc), set_by="admin",
    )
    db_session.add(tier)
    await db_session.commit()
    await db_session.refresh(tier)
    return tier


@pytest.mark.asyncio
async def test_merges_confirmed_payment_and_confirmed_upgrade_sorted_desc(db_session):
    client = await _make_client(db_session)
    tier = await _make_tier(db_session)
    now = datetime.now(timezone.utc)

    older_payment = Payment(
        client_id=client.id, phone="254700000001", amount=Decimal("300"),
        status="confirmed", initiated_at=now - timedelta(days=10),
        confirmed_at=now - timedelta(days=10),
        period_start=date.today() - timedelta(days=40),
        period_end=date.today() - timedelta(days=10),
        mpesa_transaction_id="RCPT001",
    )
    newer_upgrade = GroupUpgradeRequest(
        client_id=client.id, group_id="120363@g.us", target_tier_id=tier.id,
        phone="254700000002", amount=Decimal("150"), status="confirmed",
        mpesa_transaction_id="RCPT002", created_at=now - timedelta(days=1),
    )
    db_session.add_all([older_payment, newer_upgrade])
    await db_session.commit()

    history = await unified_payment_history(db_session, client.id, confirmed_only=True)

    assert len(history) == 2
    # date-descending: the more recent tier upgrade comes first
    assert history[0]["kind"] == "tier_upgrade"
    assert history[1]["kind"] == "renewal"
    assert history[0]["amount"] == "150"
    assert history[1]["amount"] == "300"
    # tier-upgrade rows have no subscription period — rendered as "-", matching
    # pdf.py's existing missing-value convention (see payment_history._NA)
    assert history[0]["period_start"] == "-"
    assert history[0]["period_end"] == "-"
    # renewal row keeps its real period
    assert history[1]["period_start"] == str(older_payment.period_start)
    assert history[1]["period_end"] == str(older_payment.period_end)


@pytest.mark.asyncio
async def test_confirmed_only_excludes_pending_and_failed_of_both_kinds(db_session):
    client = await _make_client(db_session)
    tier = await _make_tier(db_session)
    now = datetime.now(timezone.utc)

    db_session.add_all([
        Payment(
            client_id=client.id, phone="254700000003", amount=Decimal("300"),
            status="pending", initiated_at=now, period_start=date.today(),
            period_end=date.today() + timedelta(days=30),
        ),
        Payment(
            client_id=client.id, phone="254700000004", amount=Decimal("300"),
            status="failed", initiated_at=now, period_start=date.today(),
            period_end=date.today() + timedelta(days=30),
        ),
        GroupUpgradeRequest(
            client_id=client.id, group_id="g@g.us", target_tier_id=tier.id,
            phone="254700000005", amount=Decimal("150"), status="pending",
            created_at=now,
        ),
        GroupUpgradeRequest(
            client_id=client.id, group_id="g2@g.us", target_tier_id=tier.id,
            phone="254700000006", amount=Decimal("150"), status="failed",
            created_at=now,
        ),
    ])
    await db_session.commit()

    confirmed_only = await unified_payment_history(db_session, client.id, confirmed_only=True)
    assert confirmed_only == []

    all_statuses = await unified_payment_history(db_session, client.id, confirmed_only=False)
    assert len(all_statuses) == 4
    assert {h["status"] for h in all_statuses} == {"pending", "failed"}


@pytest.mark.asyncio
async def test_upgrade_request_description_includes_tier_name(db_session):
    client = await _make_client(db_session)
    tier = await _make_tier(db_session, name="Scale")
    now = datetime.now(timezone.utc)

    db_session.add(GroupUpgradeRequest(
        client_id=client.id, group_id="g@g.us", target_tier_id=tier.id,
        phone="254700000007", amount=Decimal("999"), status="confirmed",
        created_at=now,
    ))
    await db_session.commit()

    history = await unified_payment_history(db_session, client.id, confirmed_only=True)
    assert len(history) == 1
    assert history[0]["description"] == "Group tier upgrade → Scale"


@pytest.mark.asyncio
async def test_upgrade_request_dangling_tier_fk_does_not_crash(db_session):
    client = await _make_client(db_session)
    now = datetime.now(timezone.utc)

    # target_tier_id points at a tier id that doesn't (or no longer) exists.
    db_session.add(GroupUpgradeRequest(
        client_id=client.id, group_id="g@g.us", target_tier_id=999999,
        phone="254700000008", amount=Decimal("50"), status="confirmed",
        created_at=now,
    ))
    await db_session.commit()

    history = await unified_payment_history(db_session, client.id, confirmed_only=True)
    assert len(history) == 1
    assert history[0]["description"] == "Group tier upgrade"


@pytest.mark.asyncio
async def test_empty_history_for_client_with_no_records(db_session):
    client = await _make_client(db_session)
    history = await unified_payment_history(db_session, client.id, confirmed_only=False)
    assert history == []
