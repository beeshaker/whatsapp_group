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
        "event": "message",
        "data": {
            "type": "chat",
            "isGroup": True,
            "chatId": "999@g.us",
            "chat": {"name": "Riverside Towers"},
            "sender": {"name": "Ali (Caretaker)", "pushname": "Ali"},
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
    assert records[0]["status"] == "new"


async def test_dashboard_returns_html(client):
    response = await client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


async def test_dashboard_contains_incident_table_markup(client):
    response = await client.get("/")
    assert response.status_code == 200
    assert b"<table" in response.content
