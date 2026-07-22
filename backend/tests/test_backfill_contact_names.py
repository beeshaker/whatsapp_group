from datetime import datetime, timezone

import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base
from models import AuditLog, Incident
from scripts.backfill_contact_names import run

_engine = create_async_engine(
    "sqlite+aiosqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = async_sessionmaker(_engine, expire_on_commit=False)


@pytest_asyncio.fixture(autouse=True)
async def _setup_db(monkeypatch):
    monkeypatch.setattr("scripts.backfill_contact_names.AsyncSessionLocal", _Session)
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _seed(message_body, contact_name=None, message_id=None, issue_index=0):
    now = datetime.now(timezone.utc)
    async with _Session() as session:
        incident = Incident(
            group_id="dunhill@g.us",
            property_name="lead",
            message_body=message_body,
            category="house",
            priority="low",
            confidence=0.8,
            status="new",
            contact_name=contact_name,
            message_id=message_id,
            issue_index=issue_index,
            received_at=now,
        )
        session.add(incident)
        await session.commit()
        await session.refresh(incident)
        return incident.id


async def test_dry_run_reports_without_writing():
    incident_id = await _seed("kindly contact Victoria 0700111222 for a house")
    changed = await run(apply=False)
    assert changed == 1
    async with _Session() as session:
        incident = await session.get(Incident, incident_id)
        assert incident.contact_name is None


async def test_apply_writes_contact_name_and_audit_log():
    incident_id = await _seed("kindly contact Victoria 0700111222 for a house")
    changed = await run(apply=True)
    assert changed == 1
    async with _Session() as session:
        incident = await session.get(Incident, incident_id)
        assert incident.contact_name == "Victoria"
        result = await session.execute(select(AuditLog).where(AuditLog.incident_id == incident_id))
        audit = result.scalars().one()
        assert audit.username == "system:backfill_contact_names"
        assert audit.action == "contact_name_backfill"
        assert audit.detail == "contact_name: None → Victoria"


async def test_skips_rows_with_no_extractable_name():
    await _seed("looking for a house near Kilimani, no contact given")
    changed = await run(apply=True)
    assert changed == 0


async def test_skips_rows_that_already_have_contact_name():
    await _seed("kindly contact Victoria 0700111222 for a house", contact_name="Existing")
    changed = await run(apply=True)
    assert changed == 0


async def test_skips_incidents_sharing_message_id_with_a_sibling():
    shared_body = (
        "@~Alice kindly contact Sam 0746823554 for a 2br rent. "
        "@~Bob has a plot buyer 0722516801 for a plot sale (Website Enquiry)"
    )
    id0 = await _seed(shared_body, message_id="msg-1", issue_index=0)
    id1 = await _seed(shared_body, message_id="msg-1", issue_index=1)
    changed = await run(apply=True)
    assert changed == 0
    async with _Session() as session:
        incident0 = await session.get(Incident, id0)
        incident1 = await session.get(Incident, id1)
        assert incident0.contact_name is None
        assert incident1.contact_name is None
