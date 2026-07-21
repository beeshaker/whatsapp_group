import importlib
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy import select

import main as backend_main
from database import get_db
from models import Incident, IncidentStatusHistory, AuditLog

GATEWAY_TOKEN = "reaction-test-gateway-secret"
GROUP_ID = "dunhill-sales@g.us"

_LEAD_CLASS = {"issues": [{
    "category": "apartment", "priority": "low", "confidence": 0.9,
    "message_snippet": "Looking for a 2br",
    "contact_name": "Test Contact", "contact_phone": "254700111222",
    "lead_location": "Kilimani", "lead_budget": "50000", "transaction_type": "rent",
    "lead_agent": "Agent A", "lead_source": "WhatsApp",
}]}


@pytest.fixture(autouse=True)
def _restore_main_module_state():
    yield
    importlib.reload(backend_main)


@pytest_asyncio.fixture
async def client(monkeypatch):
    monkeypatch.setenv("LEAD_MODE", "true")
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


@pytest_asyncio.fixture
async def non_lead_client(monkeypatch):
    """Same app, LEAD_MODE left off — proves reactions are ignored entirely."""
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


def _lead_payload(message_id, author="254711223344@c.us", timestamp=1782300000):
    return {
        "event": "message.received",
        "data": {
            "id": message_id,
            "type": "chat",
            "isGroup": True,
            "chatId": GROUP_ID,
            "chat": {"name": "Dunhill Sales"},
            "author": author,
            "notifyName": "Enquirer",
            "body": "Looking for a 2br",
            "timestamp": timestamp,
        },
    }


def _reaction_payload(emoji="👍", chat_id=GROUP_ID, sender="254799888777@c.us",
                       target_message_id=None, target_author=None, target_timestamp=None):
    data = {"chatId": chat_id, "emoji": emoji, "senderId": sender}
    if target_message_id is not None:
        data["targetMessageId"] = target_message_id
    if target_author is not None:
        data["targetAuthor"] = target_author
    if target_timestamp is not None:
        data["targetTimestamp"] = target_timestamp
    return {"event": "message.reaction", "data": data}


async def _create_lead_incident(client, message_id, author="254711223344@c.us", timestamp=1782300000):
    with patch("main.classify_message", new=AsyncMock(return_value=_LEAD_CLASS)):
        with patch("main.push_incident", new=AsyncMock()):
            r = await client.post(
                "/api/v1/ops/ingest",
                json=_lead_payload(message_id, author=author, timestamp=timestamp),
                headers={"X-API-Key": GATEWAY_TOKEN},
            )
    assert r.status_code == 202
    from tests.conftest import _TestSession
    async with _TestSession() as session:
        result = await session.execute(select(Incident).where(Incident.message_id == message_id))
        incident = result.scalar_one()
    return incident.id


async def _set_status(incident_id, status):
    from tests.conftest import _TestSession
    async with _TestSession() as session:
        result = await session.execute(select(Incident).where(Incident.id == incident_id))
        incident = result.scalar_one()
        incident.status = status
        await session.commit()


async def test_reaction_exact_message_id_match_transitions_new_to_contacted(client):
    incident_id = await _create_lead_incident(client, "lead-r1")
    r = await client.post(
        "/api/v1/ops/ingest",
        json=_reaction_payload(target_message_id="lead-r1"),
        headers={"X-API-Key": GATEWAY_TOKEN},
    )
    assert r.status_code == 202
    assert r.json()["status"] == "status_updated"

    from tests.conftest import _TestSession
    async with _TestSession() as session:
        incident = (await session.execute(select(Incident).where(Incident.id == incident_id))).scalar_one()
        assert incident.status == "contacted"


async def test_reaction_matches_via_author_and_timestamp_when_no_message_id(client):
    incident_id = await _create_lead_incident(
        client, "lead-r2", author="254722334455@c.us", timestamp=1782301000,
    )
    r = await client.post(
        "/api/v1/ops/ingest",
        json=_reaction_payload(target_author="254722334455@c.us", target_timestamp=1782301000),
        headers={"X-API-Key": GATEWAY_TOKEN},
    )
    assert r.status_code == 202
    assert r.json()["status"] == "status_updated"
    assert r.json()["incident_id"] == incident_id


async def test_reaction_single_candidate_fallback_when_only_author_present(client):
    incident_id = await _create_lead_incident(
        client, "lead-r3", author="254733445566@c.us", timestamp=1782302000,
    )
    r = await client.post(
        "/api/v1/ops/ingest",
        json=_reaction_payload(target_author="254733445566@c.us"),
        headers={"X-API-Key": GATEWAY_TOKEN},
    )
    assert r.status_code == 202
    assert r.json()["status"] == "status_updated"
    assert r.json()["incident_id"] == incident_id


async def test_reaction_ambiguous_multiple_candidates_is_ignored(client):
    await _create_lead_incident(client, "lead-r4a", author="254744556677@c.us", timestamp=1782303000)
    await _create_lead_incident(client, "lead-r4b", author="254744556677@c.us", timestamp=1782303100)
    r = await client.post(
        "/api/v1/ops/ingest",
        json=_reaction_payload(target_author="254744556677@c.us"),
        headers={"X-API-Key": GATEWAY_TOKEN},
    )
    assert r.status_code == 202
    assert r.json()["status"] == "ignored"


async def test_reaction_noop_when_incident_already_contacted(client):
    incident_id = await _create_lead_incident(client, "lead-r5")
    await _set_status(incident_id, "contacted")
    r = await client.post(
        "/api/v1/ops/ingest",
        json=_reaction_payload(target_message_id="lead-r5"),
        headers={"X-API-Key": GATEWAY_TOKEN},
    )
    assert r.status_code == 202
    assert r.json()["status"] == "ignored"

    from tests.conftest import _TestSession
    async with _TestSession() as session:
        incident = (await session.execute(select(Incident).where(Incident.id == incident_id))).scalar_one()
        assert incident.status == "contacted"


async def test_reaction_noop_when_incident_closed_won(client):
    incident_id = await _create_lead_incident(client, "lead-r6")
    await _set_status(incident_id, "closed_won")
    r = await client.post(
        "/api/v1/ops/ingest",
        json=_reaction_payload(target_message_id="lead-r6"),
        headers={"X-API-Key": GATEWAY_TOKEN},
    )
    assert r.json()["status"] == "ignored"


async def test_reaction_ignores_non_thumbsup_emoji(client):
    incident_id = await _create_lead_incident(client, "lead-r7")
    r = await client.post(
        "/api/v1/ops/ingest",
        json=_reaction_payload(emoji="❤️", target_message_id="lead-r7"),
        headers={"X-API-Key": GATEWAY_TOKEN},
    )
    assert r.json()["status"] == "ignored"

    from tests.conftest import _TestSession
    async with _TestSession() as session:
        incident = (await session.execute(select(Incident).where(Incident.id == incident_id))).scalar_one()
        assert incident.status == "new"


async def test_reaction_writes_status_history_and_audit_log(client):
    incident_id = await _create_lead_incident(client, "lead-r8")
    await client.post(
        "/api/v1/ops/ingest",
        json=_reaction_payload(target_message_id="lead-r8", sender="254788990011@c.us"),
        headers={"X-API-Key": GATEWAY_TOKEN},
    )

    from tests.conftest import _TestSession
    async with _TestSession() as session:
        # Incident creation itself already writes a from_status=None history
        # row (see _handle_text_ingest), so filter to the transition this
        # test is actually about.
        history = (await session.execute(
            select(IncidentStatusHistory).where(
                IncidentStatusHistory.incident_id == incident_id,
                IncidentStatusHistory.to_status == "contacted",
            )
        )).scalar_one()
        assert history.from_status == "new"
        assert history.to_status == "contacted"
        assert history.changed_by == "whatsapp:254788990011"

        audit = (await session.execute(
            select(AuditLog).where(AuditLog.incident_id == incident_id)
        )).scalar_one()
        assert audit.action == "auto_status_reaction"
        assert audit.username == "whatsapp:254788990011"


async def test_reaction_no_message_sent_back_to_group(client):
    incident_id = await _create_lead_incident(client, "lead-r9")
    with patch("main.send_group_message", new=AsyncMock()) as mock_send:
        with patch("main.reply_to_message", new=AsyncMock()) as mock_reply:
            await client.post(
                "/api/v1/ops/ingest",
                json=_reaction_payload(target_message_id="lead-r9"),
                headers={"X-API-Key": GATEWAY_TOKEN},
            )
    mock_send.assert_not_called()
    mock_reply.assert_not_called()


async def test_reaction_ignored_when_not_lead_mode(non_lead_client):
    r = await non_lead_client.post(
        "/api/v1/ops/ingest",
        json=_reaction_payload(target_message_id="anything"),
        headers={"X-API-Key": GATEWAY_TOKEN},
    )
    assert r.status_code == 202
    assert r.json()["status"] == "ignored"
