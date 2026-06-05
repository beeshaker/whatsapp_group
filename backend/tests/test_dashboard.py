from unittest.mock import AsyncMock, patch


async def test_incidents_returns_empty_list_initially(client):
    response = await client.get("/incidents")
    assert response.status_code == 200
    assert response.json() == []


async def test_incidents_returns_staged_record(client):
    classification = {
        "is_incident": True,
        "category": "electrical",
        "severity": "medium",
        "confidence": 0.88,
    }
    payload = {
        "event": "message.received",
        "data": {
            "type": "chat",
            "isGroup": True,
            "chatId": "999@g.us",
            "chat": {"name": "Riverside Towers"},
            "author": "254700000001@c.us",
            "body": "Main fuse box tripped on ground floor",
            "timestamp": 1782293340,
        },
    }
    with patch("main.classify_message", new=AsyncMock(return_value=classification)):
        with patch("main.push_incident", new=AsyncMock()):
            await client.post(
                "/api/v1/ops/ingest", json=payload, headers={"X-API-Key": "test-secret"}
            )

    response = await client.get("/incidents")
    assert response.status_code == 200
    records = response.json()
    assert len(records) == 1
    assert records[0]["property_name"] == "Riverside Towers"
    assert records[0]["category"] == "electrical"
    assert records[0]["severity"] == "medium"
    assert records[0]["status"] == "review"


async def test_dashboard_returns_html(client):
    response = await client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


async def test_dashboard_contains_incident_card_markup(client):
    classification = {
        "is_incident": True, "category": "plumbing",
        "severity": "high", "confidence": 0.90,
    }
    payload = {
        "event": "message.received",
        "data": {
            "type": "chat", "isGroup": True,
            "chatId": "111@g.us", "chat": {"name": "Test Property"},
            "author": "25400000000@c.us",
            "body": "Pipe burst in basement", "timestamp": 1782293340,
        },
    }
    with patch("main.classify_message", new=AsyncMock(return_value=classification)):
        with patch("main.push_incident", new=AsyncMock()):
            await client.post("/api/v1/ops/ingest", json=payload,
                              headers={"X-API-Key": "test-secret"})
    response = await client.get("/")
    assert response.status_code == 200
    assert b'class="card' in response.content
    assert b"Test Property" in response.content


async def test_dashboard_has_filter_controls(client):
    response = await client.get("/")
    assert response.status_code == 200
    assert b'id="search-input"' in response.content
    assert b'id="sidebar"' in response.content


async def test_dashboard_shows_review_badge(client):
    from tests.test_ingest import _VALID_PAYLOAD, _INCIDENT_CLASSIFICATION
    with patch("main.classify_message", new=AsyncMock(return_value=_INCIDENT_CLASSIFICATION)):
        with patch("main.push_incident", new=AsyncMock()):
            await client.post("/api/v1/ops/ingest", json=_VALID_PAYLOAD, headers={"X-API-Key": "test-secret"})
    response = await client.get("/")
    assert b"badge-review" in response.content
    assert b"data-id=" in response.content


async def test_incidents_since_id_returns_only_newer(client):
    classification = {
        "is_incident": True, "category": "plumbing",
        "severity": "high", "confidence": 0.92,
    }
    base_payload = {
        "event": "message.received",
        "data": {
            "type": "chat", "isGroup": True,
            "chatId": "111@g.us", "chat": {"name": "Block A"},
            "author": "254700000001@c.us",
            "body": "Pipe burst in basement", "timestamp": 1782293340,
        },
    }
    with patch("main.classify_message", new=AsyncMock(return_value=classification)):
        with patch("main.push_incident", new=AsyncMock()):
            await client.post("/api/v1/ops/ingest", json=base_payload,
                              headers={"X-API-Key": "test-secret"})
            second = {**base_payload, "data": {**base_payload["data"],
                "id": "msg-second", "body": "Second incident"}}
            await client.post("/api/v1/ops/ingest", json=second,
                              headers={"X-API-Key": "test-secret"})

    all_incidents = (await client.get("/incidents")).json()
    assert len(all_incidents) == 2
    first_id = min(i["id"] for i in all_incidents)

    newer = (await client.get(f"/incidents?since_id={first_id}")).json()
    assert len(newer) == 1
    assert newer[0]["id"] > first_id
