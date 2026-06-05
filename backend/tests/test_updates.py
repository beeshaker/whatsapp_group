from unittest.mock import AsyncMock, patch

_GROUP_PAYLOAD = {
    "event": "message.received",
    "data": {
        "id": "msg-original",
        "type": "chat",
        "isGroup": True,
        "chatId": "120363@g.us",
        "chat": {"name": "Oakridge Block A"},
        "author": "254711111111@c.us",
        "notifyName": "Caretaker A",
        "body": "The water pump on floor 3 is leaking",
        "timestamp": 1782293340,
    },
}

_FOLLOWUP_PAYLOAD = {
    "event": "message.received",
    "data": {
        "id": "msg-followup",
        "type": "chat",
        "isGroup": True,
        "chatId": "120363@g.us",
        "chat": {"name": "Oakridge Block A"},
        "author": "254711111111@c.us",
        "notifyName": "Caretaker A",
        "body": "Still leaking badly, now flooding",
        "timestamp": 1782293400,
    },
}

_INCIDENT_CLASS = {"is_incident": True, "category": "plumbing", "severity": "high", "confidence": 0.92}
_NOISE_CLASS = {"is_incident": False, "category": "other", "severity": "low", "confidence": 0.95}


async def test_followup_creates_update_when_llm_says_update(client):
    with patch("main.classify_message", new=AsyncMock(return_value=_INCIDENT_CLASS)):
        with patch("main.push_incident", new=AsyncMock()):
            r1 = await client.post("/api/v1/ops/ingest", json=_GROUP_PAYLOAD, headers={"X-API-Key": "test-secret"})
    assert r1.json()["status"] == "staged"
    incident_id = (await client.get("/incidents")).json()[0]["id"]

    routing = {"routing": "update", "ticket_id": incident_id}
    with patch("main.classify_message", new=AsyncMock(return_value=_INCIDENT_CLASS)):
        with patch("main.classify_update_or_new", new=AsyncMock(return_value=routing)):
            r2 = await client.post("/api/v1/ops/ingest", json=_FOLLOWUP_PAYLOAD, headers={"X-API-Key": "test-secret"})
    assert r2.json()["status"] == "staged_update"
    assert r2.json()["incident_id"] == incident_id


async def test_followup_creates_new_incident_when_llm_says_new(client):
    with patch("main.classify_message", new=AsyncMock(return_value=_INCIDENT_CLASS)):
        with patch("main.push_incident", new=AsyncMock()):
            await client.post("/api/v1/ops/ingest", json=_GROUP_PAYLOAD, headers={"X-API-Key": "test-secret"})

    with patch("main.classify_message", new=AsyncMock(return_value=_INCIDENT_CLASS)):
        with patch("main.classify_update_or_new", new=AsyncMock(return_value={"routing": "new"})):
            with patch("main.push_incident", new=AsyncMock()):
                r2 = await client.post("/api/v1/ops/ingest", json=_FOLLOWUP_PAYLOAD, headers={"X-API-Key": "test-secret"})
    assert r2.json()["status"] == "staged"

    incidents = (await client.get("/incidents")).json()
    assert len(incidents) == 2


async def test_no_open_tickets_skips_stage2(client):
    """When no open tickets exist, Stage 2 is not called."""
    with patch("main.classify_message", new=AsyncMock(return_value=_INCIDENT_CLASS)):
        with patch("main.classify_update_or_new", new=AsyncMock()) as mock_stage2:
            with patch("main.push_incident", new=AsyncMock()):
                r = await client.post("/api/v1/ops/ingest", json=_GROUP_PAYLOAD, headers={"X-API-Key": "test-secret"})
    assert r.json()["status"] == "staged"
    mock_stage2.assert_not_called()


async def test_update_deduplication(client):
    with patch("main.classify_message", new=AsyncMock(return_value=_INCIDENT_CLASS)):
        with patch("main.push_incident", new=AsyncMock()):
            await client.post("/api/v1/ops/ingest", json=_GROUP_PAYLOAD, headers={"X-API-Key": "test-secret"})
    incident_id = (await client.get("/incidents")).json()[0]["id"]

    routing = {"routing": "update", "ticket_id": incident_id}
    with patch("main.classify_message", new=AsyncMock(return_value=_INCIDENT_CLASS)):
        with patch("main.classify_update_or_new", new=AsyncMock(return_value=routing)):
            r1 = await client.post("/api/v1/ops/ingest", json=_FOLLOWUP_PAYLOAD, headers={"X-API-Key": "test-secret"})
            r2 = await client.post("/api/v1/ops/ingest", json=_FOLLOWUP_PAYLOAD, headers={"X-API-Key": "test-secret"})
    assert r1.json()["status"] == "staged_update"
    assert r2.json()["status"] == "duplicate"
