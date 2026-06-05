from unittest.mock import AsyncMock, patch

_INCIDENT_CLASS = {"is_incident": True, "category": "plumbing", "severity": "high", "confidence": 0.92}

_ORIGINAL = {
    "event": "message.received",
    "data": {
        "id": "msg-a",
        "type": "chat",
        "isGroup": True,
        "chatId": "123@g.us",
        "chat": {"name": "Block A"},
        "author": "2541@c.us",
        "notifyName": "Alice",
        "body": "Pump leaking",
        "timestamp": 1782293340,
    },
}

_FOLLOWUP = {
    "event": "message.received",
    "data": {
        "id": "msg-b",
        "type": "chat",
        "isGroup": True,
        "chatId": "123@g.us",
        "chat": {"name": "Block A"},
        "author": "2541@c.us",
        "notifyName": "Alice",
        "body": "Still leaking",
        "timestamp": 1782293400,
    },
}


async def test_list_incidents_includes_counts(client):
    with patch("main.classify_message", new=AsyncMock(return_value=_INCIDENT_CLASS)):
        with patch("main.push_incident", new=AsyncMock()):
            await client.post("/api/v1/ops/ingest", json=_ORIGINAL, headers={"X-API-Key": "test-secret"})
    incidents = (await client.get("/incidents")).json()
    assert "update_count" in incidents[0]
    assert "media_count" in incidents[0]
    assert incidents[0]["update_count"] == 0
    assert incidents[0]["media_count"] == 0


async def test_list_incidents_update_count_increments(client):
    with patch("main.classify_message", new=AsyncMock(return_value=_INCIDENT_CLASS)):
        with patch("main.push_incident", new=AsyncMock()):
            await client.post("/api/v1/ops/ingest", json=_ORIGINAL, headers={"X-API-Key": "test-secret"})
    incident_id = (await client.get("/incidents")).json()[0]["id"]

    routing = {"routing": "update", "ticket_id": incident_id}
    with patch("main.classify_message", new=AsyncMock(return_value=_INCIDENT_CLASS)):
        with patch("main.classify_update_or_new", new=AsyncMock(return_value=routing)):
            await client.post("/api/v1/ops/ingest", json=_FOLLOWUP, headers={"X-API-Key": "test-secret"})

    incidents = (await client.get("/incidents")).json()
    assert incidents[0]["update_count"] == 1


async def test_get_incident_detail_returns_updates(client):
    with patch("main.classify_message", new=AsyncMock(return_value=_INCIDENT_CLASS)):
        with patch("main.push_incident", new=AsyncMock()):
            await client.post("/api/v1/ops/ingest", json=_ORIGINAL, headers={"X-API-Key": "test-secret"})
    incident_id = (await client.get("/incidents")).json()[0]["id"]

    routing = {"routing": "update", "ticket_id": incident_id}
    with patch("main.classify_message", new=AsyncMock(return_value=_INCIDENT_CLASS)):
        with patch("main.classify_update_or_new", new=AsyncMock(return_value=routing)):
            await client.post("/api/v1/ops/ingest", json=_FOLLOWUP, headers={"X-API-Key": "test-secret"})

    detail = (await client.get(f"/incidents/{incident_id}")).json()
    assert detail["id"] == incident_id
    assert len(detail["updates"]) == 1
    assert detail["updates"][0]["message_body"] == "Still leaking"
    assert detail["updates"][0]["ai_linked"] is True
    assert detail["updates"][0]["media_count"] == 0
    assert detail["media"] == []


async def test_get_incident_detail_404(client):
    r = await client.get("/incidents/9999")
    assert r.status_code == 404
