from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base
from models import (
    AuditLog, Incident, IncidentCategory, IncidentMedia, IncidentStatusHistory, IncidentUpdate, User,
)
from scripts import reset_tickets
from scripts.reset_tickets import run

_engine = create_async_engine(
    "sqlite+aiosqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = async_sessionmaker(_engine, expire_on_commit=False)


@pytest_asyncio.fixture(autouse=True)
async def _setup(monkeypatch, tmp_path):
    monkeypatch.setattr(reset_tickets, "AsyncSessionLocal", _Session)
    monkeypatch.setattr(reset_tickets, "MEDIA_DIR", str(tmp_path / "media"))
    monkeypatch.setenv("CLIENT_SUBDOMAIN", "pixiilive")
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)


async def _seed(tmp_path):
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    inside = media_dir / "photo.jpg"
    inside.write_bytes(b"x")
    outside = tmp_path / "keep.jpg"
    outside.write_bytes(b"x")
    now = datetime.now(timezone.utc)
    async with _Session() as db:
        db.add(User(username="admin", hashed_password="x", role="admin", created_at=now))
        db.add(IncidentCategory(slug="plumbing", label="Plumbing", created_at=now))
        incident = Incident(
            group_id="g@g.us", property_name="A", message_body="leak", category="plumbing",
            priority="high", confidence=0.9, status="resolved", received_at=now,
        )
        db.add(incident)
        await db.flush()
        update = IncidentUpdate(incident_id=incident.id, message_body="still leaking", received_at=now)
        db.add(update)
        await db.flush()
        for path in (inside, outside):
            db.add(IncidentMedia(incident_id=incident.id, update_id=update.id, filename=path.name,
                                 mimetype="image/jpeg", file_path=str(path), received_at=now))
        db.add(IncidentStatusHistory(incident_id=incident.id, from_status="review", to_status="resolved", changed_at=now))
        db.add(AuditLog(username="admin", action="status_change", incident_id=incident.id, created_at=now))
        await db.commit()
    return inside, outside


async def _count(model):
    async with _Session() as db:
        return await db.scalar(select(func.count()).select_from(model))


async def test_dry_run_deletes_nothing(tmp_path):
    inside, _ = await _seed(tmp_path)

    counts = await run(apply=False)

    assert counts["incidents"] == 1 and counts["incident_media"] == 2
    assert await _count(Incident) == 1
    assert inside.exists()


async def test_refuses_without_matching_confirm(tmp_path):
    await _seed(tmp_path)

    for wrong in (None, "dunhill"):
        with pytest.raises(SystemExit):
            await run(apply=True, confirm=wrong)

    assert await _count(Incident) == 1


async def test_refuses_when_client_subdomain_unset(tmp_path, monkeypatch):
    await _seed(tmp_path)
    monkeypatch.delenv("CLIENT_SUBDOMAIN")

    with pytest.raises(SystemExit):
        await run(apply=True, confirm="")

    assert await _count(Incident) == 1


async def test_apply_wipes_ticket_data_and_keeps_configuration(tmp_path):
    inside, outside = await _seed(tmp_path)

    await run(apply=True, confirm="pixiilive")

    for model in (Incident, IncidentUpdate, IncidentMedia, IncidentStatusHistory, AuditLog):
        assert await _count(model) == 0, model.__tablename__
    assert await _count(User) == 1
    assert await _count(IncidentCategory) == 1
    assert not inside.exists()
    assert outside.exists()  # never deletes outside MEDIA_DIR
