import os
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("GATEWAY_SECRET_TOKEN", "test-secret")
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-testing-only1")

from datetime import datetime, timezone

import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base, get_db
from main import app
from models import User, Incident, IncidentCategory
from auth import hash_password

_engine = create_async_engine(
    "sqlite+aiosqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = async_sessionmaker(_engine, expire_on_commit=False)
_HASHED = hash_password("pass1234")


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


@pytest_asyncio.fixture
async def tdu_client():
    async def _override_get_db():
        async with _Session() as session:
            yield session

    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


async def _seed_incident(role="admin", username="ticketadmin"):
    now = datetime.now(timezone.utc)
    async with _Session() as session:
        session.add(User(username=username, hashed_password=_HASHED, created_at=now, role=role))
        session.add(IncidentCategory(slug="plumbing", label="Plumbing", is_protected=False, created_at=now))
        session.add(IncidentCategory(slug="electrical", label="Electrical", is_protected=False, created_at=now))
        incident = Incident(
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
        session.add(incident)
        await session.commit()
        await session.refresh(incident)
        return incident.id


async def test_patch_requires_login(tdu_client):
    incident_id = await _seed_incident()
    resp = await tdu_client.patch(f"/incidents/{incident_id}", json={"priority": "high"})
    assert resp.status_code == 302


async def test_patch_rejects_user_role(tdu_client):
    incident_id = await _seed_incident(role="user", username="planeuser")
    await tdu_client.post("/login", data={"username": "planeuser", "password": "pass1234"})
    resp = await tdu_client.patch(f"/incidents/{incident_id}", json={"priority": "high"})
    assert resp.status_code == 403


async def test_patch_updates_priority(tdu_client):
    incident_id = await _seed_incident()
    await tdu_client.post("/login", data={"username": "ticketadmin", "password": "pass1234"})
    resp = await tdu_client.patch(f"/incidents/{incident_id}", json={"priority": "urgent"})
    assert resp.status_code == 200
    assert resp.json()["priority"] == "urgent"


async def test_patch_rejects_invalid_priority(tdu_client):
    incident_id = await _seed_incident()
    await tdu_client.post("/login", data={"username": "ticketadmin", "password": "pass1234"})
    resp = await tdu_client.patch(f"/incidents/{incident_id}", json={"priority": "critical"})
    assert resp.status_code == 422


async def test_patch_updates_category(tdu_client):
    incident_id = await _seed_incident()
    await tdu_client.post("/login", data={"username": "ticketadmin", "password": "pass1234"})
    resp = await tdu_client.patch(f"/incidents/{incident_id}", json={"category": "electrical"})
    assert resp.status_code == 200
    assert resp.json()["category"] == "electrical"


async def test_patch_rejects_unknown_category(tdu_client):
    incident_id = await _seed_incident()
    await tdu_client.post("/login", data={"username": "ticketadmin", "password": "pass1234"})
    resp = await tdu_client.patch(f"/incidents/{incident_id}", json={"category": "nonexistent"})
    assert resp.status_code == 422


async def test_patch_sets_end_date(tdu_client):
    incident_id = await _seed_incident()
    await tdu_client.post("/login", data={"username": "ticketadmin", "password": "pass1234"})
    resp = await tdu_client.patch(f"/incidents/{incident_id}", json={"end_date": "2026-08-01T00:00:00"})
    assert resp.status_code == 200
    assert resp.json()["end_date"].startswith("2026-08-01")


async def test_patch_clears_end_date(tdu_client):
    incident_id = await _seed_incident()
    await tdu_client.post("/login", data={"username": "ticketadmin", "password": "pass1234"})
    await tdu_client.patch(f"/incidents/{incident_id}", json={"end_date": "2026-08-01T00:00:00"})
    resp = await tdu_client.patch(f"/incidents/{incident_id}", json={"end_date": None})
    assert resp.status_code == 200
    assert resp.json()["end_date"] is None


async def test_patch_toggles_escalated(tdu_client):
    incident_id = await _seed_incident()
    await tdu_client.post("/login", data={"username": "ticketadmin", "password": "pass1234"})
    resp = await tdu_client.patch(f"/incidents/{incident_id}", json={"escalated": True})
    assert resp.status_code == 200
    assert resp.json()["escalated"] is True


async def test_patch_rejects_empty_body(tdu_client):
    incident_id = await _seed_incident()
    await tdu_client.post("/login", data={"username": "ticketadmin", "password": "pass1234"})
    resp = await tdu_client.patch(f"/incidents/{incident_id}", json={})
    assert resp.status_code == 422


async def test_patch_404_for_missing_incident(tdu_client):
    await _seed_incident()
    await tdu_client.post("/login", data={"username": "ticketadmin", "password": "pass1234"})
    resp = await tdu_client.patch("/incidents/999999", json={"priority": "high"})
    assert resp.status_code == 404


async def test_patch_writes_audit_log_per_field(tdu_client):
    from sqlalchemy import select
    from models import AuditLog
    incident_id = await _seed_incident()
    await tdu_client.post("/login", data={"username": "ticketadmin", "password": "pass1234"})
    resp = await tdu_client.patch(
        f"/incidents/{incident_id}",
        json={"priority": "urgent", "escalated": True},
    )
    assert resp.status_code == 200
    async with _Session() as session:
        result = await session.execute(
            select(AuditLog).where(AuditLog.incident_id == incident_id)
        )
        rows = result.scalars().all()
    actions = [r.action for r in rows]
    assert actions.count("ticket_detail_update") == 2


async def test_get_incident_detail_includes_new_fields(tdu_client):
    incident_id = await _seed_incident()
    await tdu_client.post("/login", data={"username": "ticketadmin", "password": "pass1234"})
    await tdu_client.patch(f"/incidents/{incident_id}", json={"end_date": "2026-08-01T00:00:00", "escalated": True})
    resp = await tdu_client.get(f"/incidents/{incident_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["end_date"].startswith("2026-08-01")
    assert data["escalated"] is True


async def _set_incident_fields(incident_id, **fields):
    async with _Session() as session:
        incident = await session.get(Incident, incident_id)
        for k, v in fields.items():
            setattr(incident, k, v)
        session.add(incident)
        await session.commit()


async def test_patch_sets_reminder_offset_hours(tdu_client):
    incident_id = await _seed_incident()
    await tdu_client.post("/login", data={"username": "ticketadmin", "password": "pass1234"})
    resp = await tdu_client.patch(f"/incidents/{incident_id}", json={"reminder_offset_hours": 6})
    assert resp.status_code == 200
    assert resp.json()["reminder_offset_hours"] == 6


async def test_patch_clears_reminder_offset_hours(tdu_client):
    incident_id = await _seed_incident()
    await tdu_client.post("/login", data={"username": "ticketadmin", "password": "pass1234"})
    await tdu_client.patch(f"/incidents/{incident_id}", json={"reminder_offset_hours": 6})
    resp = await tdu_client.patch(f"/incidents/{incident_id}", json={"reminder_offset_hours": None})
    assert resp.status_code == 200
    assert resp.json()["reminder_offset_hours"] is None


async def test_patch_rejects_invalid_reminder_offset_hours(tdu_client):
    incident_id = await _seed_incident()
    await tdu_client.post("/login", data={"username": "ticketadmin", "password": "pass1234"})
    resp = await tdu_client.patch(f"/incidents/{incident_id}", json={"reminder_offset_hours": 12})
    assert resp.status_code == 422


async def test_patch_end_date_change_resets_reminder_sent_at(tdu_client):
    incident_id = await _seed_incident()
    await tdu_client.post("/login", data={"username": "ticketadmin", "password": "pass1234"})
    await tdu_client.patch(f"/incidents/{incident_id}", json={"end_date": "2026-08-01T00:00:00"})
    await _set_incident_fields(incident_id, reminder_sent_at=datetime(2026, 7, 1, tzinfo=timezone.utc))
    resp = await tdu_client.patch(f"/incidents/{incident_id}", json={"end_date": "2026-09-01T00:00:00"})
    assert resp.status_code == 200
    assert resp.json()["reminder_sent_at"] is None


async def test_patch_future_end_date_resets_escalated(tdu_client):
    incident_id = await _seed_incident()
    await tdu_client.post("/login", data={"username": "ticketadmin", "password": "pass1234"})
    await tdu_client.patch(f"/incidents/{incident_id}", json={"end_date": "2020-01-01T00:00:00"})
    await tdu_client.patch(f"/incidents/{incident_id}", json={"escalated": True})
    resp = await tdu_client.patch(f"/incidents/{incident_id}", json={"end_date": "2099-01-01T00:00:00"})
    assert resp.status_code == 200
    assert resp.json()["escalated"] is False


async def test_patch_past_end_date_leaves_escalated_untouched(tdu_client):
    incident_id = await _seed_incident()
    await tdu_client.post("/login", data={"username": "ticketadmin", "password": "pass1234"})
    await tdu_client.patch(f"/incidents/{incident_id}", json={"end_date": "2020-01-01T00:00:00"})
    await tdu_client.patch(f"/incidents/{incident_id}", json={"escalated": True})
    resp = await tdu_client.patch(f"/incidents/{incident_id}", json={"end_date": "2020-06-01T00:00:00"})
    assert resp.status_code == 200
    assert resp.json()["escalated"] is True


async def test_get_incident_detail_includes_reminder_fields(tdu_client):
    incident_id = await _seed_incident()
    await tdu_client.post("/login", data={"username": "ticketadmin", "password": "pass1234"})
    await tdu_client.patch(f"/incidents/{incident_id}", json={"reminder_offset_hours": 1})
    resp = await tdu_client.get(f"/incidents/{incident_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["reminder_offset_hours"] == 1
    assert data["reminder_sent_at"] is None


async def test_patch_sets_vehicle_plate(tdu_client):
    incident_id = await _seed_incident()
    await tdu_client.post("/login", data={"username": "ticketadmin", "password": "pass1234"})
    resp = await tdu_client.patch(f"/incidents/{incident_id}", json={"vehicle_plate": "kmgq 947z"})
    assert resp.status_code == 200
    assert resp.json()["vehicle_plate"] == "KMGQ947Z"


async def test_patch_clears_vehicle_plate(tdu_client):
    incident_id = await _seed_incident()
    await tdu_client.post("/login", data={"username": "ticketadmin", "password": "pass1234"})
    await tdu_client.patch(f"/incidents/{incident_id}", json={"vehicle_plate": "KMGQ947Z"})
    resp = await tdu_client.patch(f"/incidents/{incident_id}", json={"vehicle_plate": None})
    assert resp.status_code == 200
    assert resp.json()["vehicle_plate"] is None


async def test_patch_rejects_malformed_vehicle_plate(tdu_client):
    incident_id = await _seed_incident()
    await tdu_client.post("/login", data={"username": "ticketadmin", "password": "pass1234"})
    resp = await tdu_client.patch(f"/incidents/{incident_id}", json={"vehicle_plate": "not a plate"})
    assert resp.status_code == 422


async def test_get_incident_detail_includes_vehicle_plate(tdu_client):
    incident_id = await _seed_incident()
    await tdu_client.post("/login", data={"username": "ticketadmin", "password": "pass1234"})
    await tdu_client.patch(f"/incidents/{incident_id}", json={"vehicle_plate": "KMGQ947Z"})
    resp = await tdu_client.get(f"/incidents/{incident_id}")
    assert resp.status_code == 200
    assert resp.json()["vehicle_plate"] == "KMGQ947Z"
