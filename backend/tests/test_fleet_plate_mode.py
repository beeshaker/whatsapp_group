import importlib
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

import main as backend_main
from database import get_db

GATEWAY_TOKEN = "fleet-mode-gateway-secret"
GROUP_ID = "fleet-riders-group@g.us"


@pytest.fixture(autouse=True)
def _restore_main_module_state():
    """See identical fixture in test_billing_forward.py / test_route_issue.py:
    reloading `main` after monkeypatching FLEET_PLATE_MODE mutates the shared
    module's globals, so it must be reloaded again once monkeypatch reverts
    the env var, or later tests see a stale FLEET_PLATE_MODE value."""
    yield
    importlib.reload(backend_main)


@pytest_asyncio.fixture
async def client(monkeypatch):
    monkeypatch.setenv("FLEET_PLATE_MODE", "true")
    monkeypatch.setenv("GATEWAY_SECRET_TOKEN", GATEWAY_TOKEN)
    from tests.conftest import _TestSession
    importlib.reload(backend_main)

    async def _override_get_db():
        async with _TestSession() as session:
            yield session

    backend_main.app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=backend_main.app), base_url="http://test") as c:
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
            "chat": {"name": "Pixiilive Riders"},
            "author": "254700111222@c.us",
            "notifyName": "Rider Joe",
            "body": body,
            "timestamp": timestamp,
        },
    }


def _single_issue(snippet, category="brakes", confidence=0.9):
    return {"issues": [{"category": category, "priority": "high", "confidence": confidence, "message_snippet": snippet}]}


async def test_plated_message_creates_incident_with_plate(client):
    body = "brakes are grinding on KMGQ 947Z"
    with patch("main.classify_message", new=AsyncMock(return_value=_single_issue(body))):
        with patch("main.push_incident", new=AsyncMock()):
            r = await client.post(
                "/api/v1/ops/ingest", headers={"X-API-Key": GATEWAY_TOKEN}, json=_payload("m1", body)
            )
    assert r.status_code == 202
    assert r.json()["tickets_created"] == 1

    from sqlalchemy import select
    from models import Incident
    from tests.conftest import _TestSession
    async with _TestSession() as session:
        result = await session.execute(select(Incident).where(Incident.message_id == "m1"))
        incident = result.scalar_one()
    assert incident.vehicle_plate == "KMGQ947Z"


async def test_repeat_plate_threads_into_existing_open_ticket(client):
    first_body = "brakes are grinding on KMGQ 947Z"
    with patch("main.classify_message", new=AsyncMock(return_value=_single_issue(first_body))):
        with patch("main.push_incident", new=AsyncMock()):
            r1 = await client.post(
                "/api/v1/ops/ingest", headers={"X-API-Key": GATEWAY_TOKEN}, json=_payload("m2", first_body)
            )
    assert r1.json()["tickets_created"] == 1

    second_body = "still grinding, KMGQ 947Z brakes worse now"
    with patch("main.classify_message", new=AsyncMock(return_value=_single_issue(second_body))):
        with patch("main.push_incident", new=AsyncMock()):
            r2 = await client.post(
                "/api/v1/ops/ingest", headers={"X-API-Key": GATEWAY_TOKEN}, json=_payload("m3", second_body)
            )
    assert r2.json() == {"status": "staged", "tickets_created": 0, "updates_created": 1}


async def test_plateless_message_creates_unassigned_incident(client):
    body = "the bike needs a service soon"
    with patch("main.classify_message", new=AsyncMock(return_value=_single_issue(body))):
        with patch("main.push_incident", new=AsyncMock()):
            r = await client.post(
                "/api/v1/ops/ingest", headers={"X-API-Key": GATEWAY_TOKEN}, json=_payload("m4", body)
            )
    assert r.json()["tickets_created"] == 1

    from sqlalchemy import select
    from models import Incident
    from tests.conftest import _TestSession
    async with _TestSession() as session:
        result = await session.execute(select(Incident).where(Incident.message_id == "m4"))
        incident = result.scalar_one()
    assert incident.vehicle_plate is None


async def test_two_issue_message_tags_each_with_its_own_plate(client):
    body = "1. KMGQ 947Z brakes grinding 2. KZZT 501M flat tyre"
    classification = {
        "issues": [
            {"category": "brakes", "priority": "high", "confidence": 0.9, "message_snippet": "KMGQ 947Z brakes grinding"},
            {"category": "tyres", "priority": "medium", "confidence": 0.85, "message_snippet": "KZZT 501M flat tyre"},
        ]
    }
    with patch("main.classify_message", new=AsyncMock(return_value=classification)):
        with patch("main.push_incident", new=AsyncMock()):
            r = await client.post(
                "/api/v1/ops/ingest", headers={"X-API-Key": GATEWAY_TOKEN}, json=_payload("m5", body)
            )
    assert r.json()["tickets_created"] == 2

    from sqlalchemy import select
    from models import Incident
    from tests.conftest import _TestSession
    async with _TestSession() as session:
        result = await session.execute(
            select(Incident).where(Incident.message_id == "m5").order_by(Incident.issue_index)
        )
        incidents = result.scalars().all()
    assert [i.vehicle_plate for i in incidents] == ["KMGQ947Z", "KZZT501M"]



async def _status_and_history(message_id):
    from sqlalchemy import select
    from models import Incident, IncidentStatusHistory
    from tests.conftest import _TestSession
    async with _TestSession() as session:
        incident = (await session.execute(select(Incident).where(Incident.message_id == message_id))).scalar_one()
        history = (await session.execute(
            select(IncidentStatusHistory.to_status).where(IncidentStatusHistory.incident_id == incident.id)
        )).scalars().all()
    return incident, history


async def test_repair_report_without_open_ticket_goes_to_review(client):
    body = "KXDB 202B\nFootrest replacement done"
    with patch("main.classify_message", new=AsyncMock(return_value=_single_issue(body, category="other"))):
        with patch("main.push_incident", new=AsyncMock()):
            r = await client.post("/api/v1/ops/ingest", headers={"X-API-Key": GATEWAY_TOKEN},
                                  json=_payload("done-1", body))
    assert r.json()["tickets_created"] == 1
    incident, history = await _status_and_history("done-1")
    assert (incident.vehicle_plate, incident.status, history) == ("KXDB202B", "review", ["review"])


async def test_repair_report_on_open_ticket_adds_update_and_leaves_it_open(client):
    fault = "KXDA 101A brake cable snapped"
    with patch("main.classify_message", new=AsyncMock(return_value=_single_issue(fault))):
        with patch("main.push_incident", new=AsyncMock()):
            await client.post("/api/v1/ops/ingest", headers={"X-API-Key": GATEWAY_TOKEN},
                              json=_payload("done-2", fault))

    done = "KXDA 101A\nBrake cable replacement done"
    with patch("main.classify_message", new=AsyncMock(return_value=_single_issue(done))):
        with patch("main.push_incident", new=AsyncMock()):
            r = await client.post("/api/v1/ops/ingest", headers={"X-API-Key": GATEWAY_TOKEN},
                                  json=_payload("done-3", done))
    assert r.json() == {"status": "staged", "tickets_created": 0, "updates_created": 1}
    incident, history = await _status_and_history("done-2")
    assert (incident.status, history) == ("review", ["review"])


async def test_one_vehicle_two_job_kinds_logs_one_ticket_per_category(client):
    body = "KXDF 501F\nBrake pad replacement done\nFootrest bar replacement done"
    classification = {"issues": [
        {"category": "brakes", "priority": "low", "confidence": 0.95,
         "message_snippet": "KXDF 501F\nBrake pad replacement done"},
        {"category": "structural", "priority": "low", "confidence": 0.95,
         "message_snippet": "KXDF 501F\nFootrest bar replacement done"},
    ]}
    with patch("main.classify_message", new=AsyncMock(return_value=classification)):
        with patch("main.push_incident", new=AsyncMock()):
            r = await client.post("/api/v1/ops/ingest", headers={"X-API-Key": GATEWAY_TOKEN},
                                  json=_payload("done-5", body))
    assert r.json() == {"status": "staged", "tickets_created": 2, "updates_created": 0}

    later = "KXDF 501F brakes squeaking again"
    with patch("main.classify_message", new=AsyncMock(return_value=_single_issue(later, category="brakes"))):
        with patch("main.push_incident", new=AsyncMock()):
            r = await client.post("/api/v1/ops/ingest", headers={"X-API-Key": GATEWAY_TOKEN},
                                  json=_payload("done-6", later))
    assert r.json() == {"status": "staged", "tickets_created": 0, "updates_created": 1}

    motor = "KXDF 501F motor down"
    with patch("main.classify_message", new=AsyncMock(return_value=_single_issue(motor, category="motor"))):
        with patch("main.push_incident", new=AsyncMock()):
            r = await client.post("/api/v1/ops/ingest", headers={"X-API-Key": GATEWAY_TOKEN},
                                  json=_payload("done-7", motor))
    assert r.json() == {"status": "staged", "tickets_created": 1, "updates_created": 0}


async def test_multi_vehicle_repair_report_logs_one_ticket_per_plate(client):
    body = "NEW MOTOR INSTALLATION\n\nKXDD 401D\nMotor No.C1\n\nKXDE 402E\nMotor No.C2"
    classification = {"issues": [
        {"category": "motor", "priority": "low", "confidence": 0.95, "message_snippet": "KXDD 401D\nMotor No.C1"},
        {"category": "motor", "priority": "low", "confidence": 0.95, "message_snippet": "KXDE 402E\nMotor No.C2"},
    ]}
    with patch("main.classify_message", new=AsyncMock(return_value=classification)):
        with patch("main.push_incident", new=AsyncMock()):
            r = await client.post("/api/v1/ops/ingest", headers={"X-API-Key": GATEWAY_TOKEN},
                                  json=_payload("done-4", body))
    assert r.json()["tickets_created"] == 2

    from sqlalchemy import select
    from models import Incident
    from tests.conftest import _TestSession
    async with _TestSession() as session:
        incidents = (await session.execute(
            select(Incident).where(Incident.message_id == "done-4").order_by(Incident.issue_index)
        )).scalars().all()
    assert [(i.vehicle_plate, i.status) for i in incidents] == [("KXDD401D", "review"), ("KXDE402E", "review")]
