from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import StaticPool

import main
import whatsapp
from database import Base
from models import AuditLog, Incident
from scripts import backfill_history
from scripts.backfill_history import run

_engine = create_async_engine(
    "sqlite+aiosqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = async_sessionmaker(_engine, expire_on_commit=False)

GROUP = "120363001@g.us"
OTHER_GROUP = "120363999@g.us"
BILLING_GROUP = "120363777@g.us"
T0 = 1790000000  # epoch seconds
SINCE = datetime.fromtimestamp(T0 - 60, tz=timezone.utc)
UNTIL = datetime.fromtimestamp(T0 + 3600, tz=timezone.utc)

_INCIDENT = {"issues": [{
    "category": "plumbing", "priority": "high", "confidence": 0.92,
    "message_snippet": "Pump leaking",
}]}


def _msg(msg_id, ts, body="The water pump is leaking", group=GROUP, **overrides):
    return {
        "id": msg_id, "chatId": group, "from": group, "body": body, "type": "chat",
        "timestamp": ts, "fromMe": False, "isGroup": True,
        "author": "254711223344@c.us", "notifyName": "Jane", **overrides,
    }


@pytest_asyncio.fixture(autouse=True)
async def _setup(monkeypatch):
    monkeypatch.setattr(backfill_history, "AsyncSessionLocal", _Session)
    monkeypatch.setattr(main, "_get_client_billing_status", AsyncMock(return_value="active"))
    monkeypatch.setattr(main, "_get_allowed_ticket_groups", AsyncMock(return_value=[GROUP]))
    monkeypatch.setattr(main, "_get_billing_group_id", AsyncMock(return_value=BILLING_GROUP))
    monkeypatch.setattr(main, "classify_message", AsyncMock(return_value=_INCIDENT))
    monkeypatch.setattr(main, "classify_update_or_new", AsyncMock(return_value={"routing": "new"}))
    monkeypatch.setattr(whatsapp, "list_groups", AsyncMock(return_value=[{"id": GROUP, "name": "Block A"}]))
    # Any outbound WhatsApp send would be a bug in a backfill.
    sends = AsyncMock()
    monkeypatch.setattr(main, "send_group_message", sends)
    monkeypatch.setattr(main, "reply_to_message", sends)
    monkeypatch.setattr(whatsapp, "send_group_message", sends)
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield sends
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


def _history(monkeypatch, messages):
    fetch = AsyncMock(return_value=messages)
    monkeypatch.setattr(backfill_history, "fetch_history", fetch)
    return fetch


async def _incidents():
    async with _Session() as db:
        return (await db.scalars(select(Incident).order_by(Incident.received_at))).all()


async def test_dry_run_writes_nothing(monkeypatch):
    _history(monkeypatch, [_msg("h-1", T0)])

    await run(apply=False, since=SINCE, until=UNTIL)

    assert await _incidents() == []


async def test_apply_creates_tickets_with_original_timestamps_oldest_first(monkeypatch, _setup):
    _history(monkeypatch, [_msg("h-2", T0 + 120), _msg("h-1", T0)])

    results = await run(apply=True, since=SINCE, until=UNTIL)

    incidents = await _incidents()
    assert results == {"staged": 2}
    assert [i.message_id for i in incidents] == ["h-1", "h-2"]
    assert incidents[0].received_at.replace(tzinfo=timezone.utc) == datetime.fromtimestamp(T0, tz=timezone.utc)
    assert incidents[0].property_name == "Block A"
    assert incidents[0].reporter_phone == "254711223344"
    _setup.assert_not_called()


async def test_tags_created_tickets_in_audit_log(monkeypatch):
    _history(monkeypatch, [_msg("h-1", T0)])

    await run(apply=True, since=SINCE, until=UNTIL)

    async with _Session() as db:
        logs = (await db.scalars(select(AuditLog))).all()
    incidents = await _incidents()
    assert [(log.action, log.incident_id) for log in logs] == [("history_backfill", incidents[0].id)]


async def test_rerun_creates_nothing_new(monkeypatch):
    _history(monkeypatch, [_msg("h-1", T0), _msg("h-2", T0 + 60)])
    await run(apply=True, since=SINCE, until=UNTIL)

    results = await run(apply=True, since=SINCE, until=UNTIL)

    assert len(await _incidents()) == 2
    assert results == {"duplicate": 2}


async def test_skips_message_ingested_before_outage_under_another_id(monkeypatch):
    async with _Session() as db:
        db.add(Incident(
            group_id=GROUP, property_name="Block A", message_body="Pump leaking", category="plumbing",
            priority="high", confidence=0.9, status="review", message_id="false_wwebjs_id",
            received_at=datetime.fromtimestamp(T0, tz=timezone.utc),
        ))
        await db.commit()
    _history(monkeypatch, [_msg("baileys-id", T0)])

    results = await run(apply=True, since=SINCE, until=UNTIL)

    assert results == {"duplicate": 1}
    assert len(await _incidents()) == 1


async def test_filters_mirror_live_ingest(monkeypatch):
    _history(monkeypatch, [
        _msg("keep", T0),
        _msg("not-allowed", T0 + 1, group=OTHER_GROUP),
        _msg("billing", T0 + 2, group=BILLING_GROUP),
        _msg("mine", T0 + 3, fromMe=True),
        _msg("cmd", T0 + 4, body="/payment"),
        _msg("empty", T0 + 5, body="   "),
        _msg("dm", T0 + 6, isGroup=False, chatId="254700000000@c.us"),
    ])

    await run(apply=True, since=SINCE, until=UNTIL)

    assert [i.message_id for i in await _incidents()] == ["keep"]


async def test_default_since_is_newest_ticket(monkeypatch):
    newest = datetime.fromtimestamp(T0 - 600, tz=timezone.utc)
    async with _Session() as db:
        db.add(Incident(
            group_id=GROUP, property_name="Block A", message_body="old", category="plumbing",
            priority="low", confidence=0.9, status="review", message_id="old",
            received_at=newest,
        ))
        await db.commit()
    fetch = _history(monkeypatch, [])

    await run(apply=False, until=UNTIL)

    assert fetch.await_args[0][0] == newest


async def test_aborts_when_client_is_billing_only(monkeypatch):
    monkeypatch.setattr(main, "_get_client_billing_status", AsyncMock(return_value="billing_only"))
    fetch = _history(monkeypatch, [_msg("h-1", T0)])

    with pytest.raises(SystemExit):
        await run(apply=True, since=SINCE, until=UNTIL)

    fetch.assert_not_awaited()


async def test_from_start_replays_everything_after_a_reset(monkeypatch):
    fetch = _history(monkeypatch, [_msg("ancient", 1500000000), _msg("h-1", T0)])

    results = await run(apply=True, until=UNTIL, from_start=True)

    assert fetch.await_args[0][0] == datetime.fromtimestamp(0, tz=timezone.utc)
    assert results == {"staged": 2}


async def test_empty_db_without_from_start_asks_for_a_window(monkeypatch):
    _history(monkeypatch, [])

    with pytest.raises(SystemExit):
        await run(apply=False, until=UNTIL)
