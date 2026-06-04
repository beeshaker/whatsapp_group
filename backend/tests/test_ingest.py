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
