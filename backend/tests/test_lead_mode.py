import importlib
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

import main as backend_main
from database import get_db
from models import User

GATEWAY_TOKEN = "lead-mode-gateway-secret"
GROUP_ID = "dunhill-sales@g.us"


@pytest.fixture(autouse=True)
def _restore_main_module_state():
    yield
    importlib.reload(backend_main)


@pytest_asyncio.fixture
async def client(monkeypatch):
    monkeypatch.setenv("LEAD_MODE", "true")
    monkeypatch.setenv("GATEWAY_SECRET_TOKEN", GATEWAY_TOKEN)
    from tests.conftest import _TestSession, _HASHED_TESTPASS
    importlib.reload(backend_main)

    async def _override_get_db():
        async with _TestSession() as session:
            yield session

    backend_main.app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=backend_main.app), base_url="http://test") as c:
        # Seed a test admin and log in so session-gated routes (e.g. GET
        # /incidents) work alongside the X-API-Key-gated ingest/status routes.
        async with _TestSession() as session:
            session.add(User(
                username="leadtestadmin",
                hashed_password=_HASHED_TESTPASS,
                created_at=datetime.now(timezone.utc),
                created_by=None,
                role="admin",
            ))
            await session.commit()
        await c.post("/login", data={"username": "leadtestadmin", "password": "testpass"})
        yield c
    backend_main.app.dependency_overrides.clear()


def _payload(message_id, body, timestamp=1782300000):
    return {
        "event": "message.received",
        "data": {
            "id": message_id,
            "type": "chat",
            "isGroup": True,
            "chatId": GROUP_ID,
            "from": GROUP_ID,
            "chat": {"name": "Dunhill Sales Enquiries"},
            "author": "254790458670@c.us",
            "notifyName": "Nyambu",
            "body": body,
            "timestamp": timestamp,
        },
    }


def _lead_issue(snippet, category="apartment", confidence=0.9, **overrides):
    issue = {
        "category": category,
        "priority": "low",
        "confidence": confidence,
        "message_snippet": snippet,
        "contact_name": "Samson",
        "contact_phone": "254746823554",
        "lead_location": "General Mathenge",
        "lead_budget": "3000usd",
        "transaction_type": "rent",
        "lead_agent": "Jabeen",
        "lead_source": "Website Enquiry",
    }
    issue.update(overrides)
    return issue


async def test_lead_message_creates_lead_with_all_fields_and_full_raw_body(client):
    body = "@~Jabeen kindly contact Samson 0746823554, looking for a 4br along for rent General mathege , budget 3000usd (Website Enquiry)"
    classification = {"issues": [_lead_issue("looking for a 4br along for rent")]}
    with patch("main.classify_message", new=AsyncMock(return_value=classification)):
        with patch("main.push_incident", new=AsyncMock()):
            r = await client.post(
                "/api/v1/ops/ingest", headers={"X-API-Key": GATEWAY_TOKEN}, json=_payload("lead-m1", body)
            )
    assert r.status_code == 202
    assert r.json()["tickets_created"] == 1

    from sqlalchemy import select
    from models import Incident
    from tests.conftest import _TestSession
    async with _TestSession() as session:
        result = await session.execute(select(Incident).where(Incident.message_id == "lead-m1"))
        incident = result.scalar_one()
    assert incident.message_body == body
    assert incident.status == "new"
    assert incident.priority == "low"
    assert incident.category == "apartment"
    assert incident.lead_agent == "Jabeen"
    assert incident.contact_name == "Samson"
    assert incident.contact_phone == "254746823554"
    assert incident.lead_location == "General Mathenge"
    assert incident.lead_budget == "3000usd"
    assert incident.transaction_type == "rent"
    assert incident.lead_source == "Website Enquiry"


async def test_two_enquiry_message_creates_two_independent_leads(client):
    body = "@~Jabeen kindly contact Samson 0746823554 for a 4br. @~Victoria kindly contact Mercy 0784549538 for commercial space."
    classification = {"issues": [
        _lead_issue("Samson 4br", category="apartment", contact_name="Samson", lead_agent="Jabeen"),
        _lead_issue("Mercy commercial space", category="commercial", contact_name="Mercy", lead_agent="Victoria"),
    ]}
    with patch("main.classify_message", new=AsyncMock(return_value=classification)):
        with patch("main.push_incident", new=AsyncMock()):
            r = await client.post(
                "/api/v1/ops/ingest", headers={"X-API-Key": GATEWAY_TOKEN}, json=_payload("lead-m2", body)
            )
    assert r.json()["tickets_created"] == 2
    assert r.json()["updates_created"] == 0

    from sqlalchemy import select
    from models import Incident
    from tests.conftest import _TestSession
    async with _TestSession() as session:
        result = await session.execute(
            select(Incident).where(Incident.message_id == "lead-m2").order_by(Incident.issue_index)
        )
        incidents = result.scalars().all()
    assert [i.contact_name for i in incidents] == ["Samson", "Mercy"]
    assert [i.lead_agent for i in incidents] == ["Jabeen", "Victoria"]


async def test_noise_message_creates_no_leads(client):
    body = "Good morning team, hope everyone is well!"
    with patch("main.classify_message", new=AsyncMock(return_value={"issues": []})):
        with patch("main.push_incident", new=AsyncMock()):
            r = await client.post(
                "/api/v1/ops/ingest", headers={"X-API-Key": GATEWAY_TOKEN}, json=_payload("lead-m3", body)
            )
    assert r.json() == {"status": "noise", "message": "Message classified as non-incident"}


async def test_lead_status_transition_new_to_contacted_to_closed_won(client):
    body = "@~Jabeen kindly contact Samson 0746823554 for a 4br (Website Enquiry)"
    classification = {"issues": [_lead_issue("4br for Samson")]}
    with patch("main.classify_message", new=AsyncMock(return_value=classification)):
        with patch("main.push_incident", new=AsyncMock()):
            r = await client.post(
                "/api/v1/ops/ingest", headers={"X-API-Key": GATEWAY_TOKEN}, json=_payload("lead-status-1", body)
            )
    from sqlalchemy import select
    from models import Incident
    from tests.conftest import _TestSession
    async with _TestSession() as session:
        result = await session.execute(select(Incident).where(Incident.message_id == "lead-status-1"))
        incident_id = result.scalar_one().id

    r1 = await client.patch(f"/incidents/{incident_id}/status", json={"status": "contacted"})
    assert r1.status_code == 200
    assert r1.json()["status"] == "contacted"

    r2 = await client.patch(f"/incidents/{incident_id}/status", json={"status": "closed_won"})
    assert r2.status_code == 200
    assert r2.json()["status"] == "closed_won"


async def test_lead_status_rejects_maintenance_status_values(client):
    body = "@~Jabeen kindly contact Samson 0746823554 for a 4br (Website Enquiry)"
    classification = {"issues": [_lead_issue("4br for Samson")]}
    with patch("main.classify_message", new=AsyncMock(return_value=classification)):
        with patch("main.push_incident", new=AsyncMock()):
            await client.post(
                "/api/v1/ops/ingest", headers={"X-API-Key": GATEWAY_TOKEN}, json=_payload("lead-status-2", body)
            )
    from sqlalchemy import select
    from models import Incident
    from tests.conftest import _TestSession
    async with _TestSession() as session:
        result = await session.execute(select(Incident).where(Incident.message_id == "lead-status-2"))
        incident_id = result.scalar_one().id

    r = await client.patch(f"/incidents/{incident_id}/status", json={"status": "resolved"})
    assert r.status_code == 422


async def test_closed_lead_appears_in_archive_not_live(client):
    body = "@~Jabeen kindly contact Samson 0746823554 for a 4br (Website Enquiry)"
    classification = {"issues": [_lead_issue("4br for Samson")]}
    with patch("main.classify_message", new=AsyncMock(return_value=classification)):
        with patch("main.push_incident", new=AsyncMock()):
            await client.post(
                "/api/v1/ops/ingest", headers={"X-API-Key": GATEWAY_TOKEN}, json=_payload("lead-status-3", body)
            )
    from sqlalchemy import select
    from models import Incident
    from tests.conftest import _TestSession
    async with _TestSession() as session:
        result = await session.execute(select(Incident).where(Incident.message_id == "lead-status-3"))
        incident_id = result.scalar_one().id

    await client.patch(f"/incidents/{incident_id}/status", json={"status": "closed_lost"})

    live = await client.get("/")
    assert f'data-id="{incident_id}"' not in live.text
    archived = await client.get("/archive")
    assert f'data-id="{incident_id}"' in archived.text
