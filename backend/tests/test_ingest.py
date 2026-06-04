from unittest.mock import AsyncMock, patch

_VALID_PAYLOAD = {
    "event": "message.received",
    "data": {
        "id": "msg-unique-xyz",
        "type": "chat",
        "isGroup": True,
        "chatId": "120363218945612345@g.us",
        "from": "120363218945612345@g.us",
        "chat": {"name": "Oakridge Heights - Block B"},
        "author": "254711223344@c.us",
        "notifyName": "John (Caretaker)",
        "body": "The water pump on floor 3 is leaking heavily",
        "timestamp": 1782293340,
    },
}

_INCIDENT_CLASSIFICATION = {
    "is_incident": True,
    "category": "plumbing",
    "severity": "high",
    "confidence": 0.92,
}

_NOISE_CLASSIFICATION = {
    "is_incident": False,
    "category": "other",
    "severity": "low",
    "confidence": 0.95,
}

_LOW_CONF_CLASSIFICATION = {
    "is_incident": True,
    "category": "plumbing",
    "severity": "low",
    "confidence": 0.3,
}


async def test_health(client):
    response = await client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_ingest_rejects_wrong_api_key(client):
    response = await client.post(
        "/api/v1/ops/ingest", json=_VALID_PAYLOAD, headers={"X-API-Key": "wrong-key"}
    )
    assert response.status_code == 401


async def test_ingest_ignores_non_group(client):
    payload = {
        **_VALID_PAYLOAD,
        "data": {**_VALID_PAYLOAD["data"], "isGroup": False},
    }
    response = await client.post(
        "/api/v1/ops/ingest", json=payload, headers={"X-API-Key": "test-secret"}
    )
    assert response.status_code == 202
    assert response.json()["status"] == "ignored"


async def test_ingest_ignores_non_chat_type(client):
    payload = {
        **_VALID_PAYLOAD,
        "data": {**_VALID_PAYLOAD["data"], "type": "image"},
    }
    response = await client.post(
        "/api/v1/ops/ingest", json=payload, headers={"X-API-Key": "test-secret"}
    )
    assert response.status_code == 202
    assert response.json()["status"] == "ignored"


async def test_ingest_discards_noise(client):
    with patch("main.classify_message", new=AsyncMock(return_value=_NOISE_CLASSIFICATION)):
        response = await client.post(
            "/api/v1/ops/ingest", json=_VALID_PAYLOAD, headers={"X-API-Key": "test-secret"}
        )
    assert response.status_code == 202
    assert response.json()["status"] == "noise"


async def test_ingest_discards_low_confidence(client):
    with patch("main.classify_message", new=AsyncMock(return_value=_LOW_CONF_CLASSIFICATION)):
        response = await client.post(
            "/api/v1/ops/ingest", json=_VALID_PAYLOAD, headers={"X-API-Key": "test-secret"}
        )
    assert response.status_code == 202
    assert response.json()["status"] == "noise"


async def test_ingest_stages_confirmed_incident(client):
    with patch("main.classify_message", new=AsyncMock(return_value=_INCIDENT_CLASSIFICATION)):
        with patch("main.push_incident", new=AsyncMock()):
            response = await client.post(
                "/api/v1/ops/ingest",
                json=_VALID_PAYLOAD,
                headers={"X-API-Key": "test-secret"},
            )
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "staged"
    assert body["property"] == "Oakridge Heights - Block B"
    assert body["category"] == "plumbing"
    assert body["severity"] == "high"


async def test_ingest_captures_reporter_identity(client):
    with patch("main.classify_message", new=AsyncMock(return_value=_INCIDENT_CLASSIFICATION)):
        with patch("main.push_incident", new=AsyncMock()):
            response = await client.post(
                "/api/v1/ops/ingest", json=_VALID_PAYLOAD, headers={"X-API-Key": "test-secret"}
            )
    assert response.json()["status"] == "staged"
    incidents_resp = await client.get("/incidents")
    incident = incidents_resp.json()[0]
    assert incident["reporter_phone"] == "254711223344"
    assert incident["reporter_name"] == "John (Caretaker)"


async def test_ingest_reporter_defaults_when_fields_absent(client):
    payload = {
        "event": "message.received",
        "data": {
            "id": "msg-no-reporter",
            "type": "chat",
            "isGroup": True,
            "chatId": "120363218945612345@g.us",
            "body": "The water pump on floor 3 is leaking heavily",
            "timestamp": 1782293340,
        },
    }
    with patch("main.classify_message", new=AsyncMock(return_value=_INCIDENT_CLASSIFICATION)):
        with patch("main.push_incident", new=AsyncMock()):
            resp = await client.post("/api/v1/ops/ingest", json=payload, headers={"X-API-Key": "test-secret"})
    assert resp.json()["status"] == "staged"
    incidents = (await client.get("/incidents")).json()
    incident = incidents[0]
    assert incident["reporter_name"] == "Unknown"
    assert incident["reporter_phone"] is None


async def test_patch_incident_status(client):
    # Create an incident first
    with patch("main.classify_message", new=AsyncMock(return_value=_INCIDENT_CLASSIFICATION)):
        with patch("main.push_incident", new=AsyncMock()):
            await client.post("/api/v1/ops/ingest", json=_VALID_PAYLOAD, headers={"X-API-Key": "test-secret"})

    incidents = (await client.get("/incidents")).json()
    incident_id = incidents[0]["id"]

    response = await client.patch(
        f"/incidents/{incident_id}/status",
        json={"status": "acknowledged"},
        headers={"X-API-Key": "test-secret"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "acknowledged"


async def test_patch_incident_status_rejects_invalid(client):
    response = await client.patch(
        "/incidents/1/status",
        json={"status": "banana"},
        headers={"X-API-Key": "test-secret"},
    )
    assert response.status_code == 422


async def test_ingest_deduplicates_same_message_id(client):
    payload = {
        "event": "message.received",
        "data": {
            "id": "msg-abc-123",
            "type": "chat",
            "isGroup": True,
            "chatId": "120363218945612345@g.us",
            "body": "The water pump on floor 3 is leaking heavily",
            "timestamp": 1782293340,
        },
    }
    with patch("main.classify_message", new=AsyncMock(return_value=_INCIDENT_CLASSIFICATION)):
        with patch("main.push_incident", new=AsyncMock()):
            r1 = await client.post("/api/v1/ops/ingest", json=payload, headers={"X-API-Key": "test-secret"})
            r2 = await client.post("/api/v1/ops/ingest", json=payload, headers={"X-API-Key": "test-secret"})
    assert r1.json()["status"] == "staged"
    assert r2.json()["status"] == "duplicate"
