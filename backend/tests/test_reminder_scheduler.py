import os
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("GATEWAY_SECRET_TOKEN", "test-secret")
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-testing-only1")

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import StaticPool

import main
from database import Base
from models import Incident

_engine = create_async_engine(
    "sqlite+aiosqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = async_sessionmaker(_engine, expire_on_commit=False)


@pytest_asyncio.fixture(scope="module", autouse=True)
async def schema():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@pytest_asyncio.fixture(autouse=True)
async def clean_tables():
    yield
    async with _engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            await conn.execute(table.delete())


async def _make_incident(**overrides):
    now = datetime.now(timezone.utc)
    defaults = dict(
        group_id="g1@g.us",
        property_name="Block A",
        reporter_name="Alice",
        message_body="Pump leaking",
        category="plumbing",
        priority="medium",
        confidence=0.9,
        status="review",
        received_at=now,
    )
    defaults.update(overrides)
    async with _Session() as session:
        incident = Incident(**defaults)
        session.add(incident)
        await session.commit()
        await session.refresh(incident)
        return incident.id


async def _get_incident(incident_id):
    async with _Session() as session:
        return await session.get(Incident, incident_id)


async def test_reminder_fires_once_past_offset_time():
    now = datetime.now(timezone.utc)
    incident_id = await _make_incident(
        end_date=now + timedelta(hours=1),
        reminder_offset_hours=1,
    )
    with patch("main.AsyncSessionLocal", _Session):
        with patch("main.send_group_message", new=AsyncMock()) as mock_send:
            await main._check_ticket_reminders()
    mock_send.assert_awaited_once()
    assert mock_send.await_args.args[0] == "g1@g.us"
    ticket = await _get_incident(incident_id)
    assert ticket.reminder_sent_at is not None


async def test_reminder_does_not_fire_before_offset_time():
    now = datetime.now(timezone.utc)
    incident_id = await _make_incident(
        end_date=now + timedelta(hours=5),
        reminder_offset_hours=1,
    )
    with patch("main.AsyncSessionLocal", _Session):
        with patch("main.send_group_message", new=AsyncMock()) as mock_send:
            await main._check_ticket_reminders()
    mock_send.assert_not_awaited()
    ticket = await _get_incident(incident_id)
    assert ticket.reminder_sent_at is None


async def test_reminder_does_not_refire_after_being_sent():
    now = datetime.now(timezone.utc)
    await _make_incident(
        end_date=now + timedelta(hours=1),
        reminder_offset_hours=1,
        reminder_sent_at=now - timedelta(minutes=5),
    )
    with patch("main.AsyncSessionLocal", _Session):
        with patch("main.send_group_message", new=AsyncMock()) as mock_send:
            await main._check_ticket_reminders()
    mock_send.assert_not_awaited()


async def test_escalation_fires_for_overdue_ticket_with_no_reminder_configured():
    now = datetime.now(timezone.utc)
    incident_id = await _make_incident(
        end_date=now - timedelta(hours=1),
        reminder_offset_hours=None,
    )
    with patch("main.AsyncSessionLocal", _Session):
        with patch("main.send_group_message", new=AsyncMock()) as mock_send:
            await main._check_ticket_reminders()
    mock_send.assert_awaited_once()
    ticket = await _get_incident(incident_id)
    assert ticket.escalated is True


async def test_ticket_with_zero_offset_receives_both_messages():
    now = datetime.now(timezone.utc)
    incident_id = await _make_incident(
        end_date=now - timedelta(minutes=1),
        reminder_offset_hours=0,
    )
    with patch("main.AsyncSessionLocal", _Session):
        with patch("main.send_group_message", new=AsyncMock()) as mock_send:
            await main._check_ticket_reminders()
    assert mock_send.await_count == 2
    ticket = await _get_incident(incident_id)
    assert ticket.reminder_sent_at is not None
    assert ticket.escalated is True


async def test_resolved_ticket_past_deadline_is_skipped():
    now = datetime.now(timezone.utc)
    incident_id = await _make_incident(
        end_date=now - timedelta(hours=1),
        reminder_offset_hours=0,
        status="resolved",
    )
    with patch("main.AsyncSessionLocal", _Session):
        with patch("main.send_group_message", new=AsyncMock()) as mock_send:
            await main._check_ticket_reminders()
    mock_send.assert_not_awaited()
    ticket = await _get_incident(incident_id)
    assert ticket.escalated is False
    assert ticket.reminder_sent_at is None


async def test_send_failure_for_one_ticket_does_not_block_others():
    now = datetime.now(timezone.utc)
    failing_id = await _make_incident(
        group_id="fail@g.us",
        end_date=now - timedelta(hours=1),
        reminder_offset_hours=None,
    )
    ok_id = await _make_incident(
        group_id="ok@g.us",
        end_date=now - timedelta(hours=1),
        reminder_offset_hours=None,
    )

    async def _side_effect(chat_id, text):
        if chat_id == "fail@g.us":
            raise RuntimeError("send failed")
        return "msg-id"

    with patch("main.AsyncSessionLocal", _Session):
        with patch("main.send_group_message", new=AsyncMock(side_effect=_side_effect)):
            await main._check_ticket_reminders()

    failing_ticket = await _get_incident(failing_id)
    ok_ticket = await _get_incident(ok_id)
    assert failing_ticket.escalated is False
    assert ok_ticket.escalated is True
