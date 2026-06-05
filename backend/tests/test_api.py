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


import tempfile
import os
from models import IncidentMedia


async def test_serve_media_returns_file(client, db_session):
    # Create incident
    with patch("main.classify_message", new=AsyncMock(return_value=_INCIDENT_CLASS)):
        with patch("main.push_incident", new=AsyncMock()):
            await client.post("/api/v1/ops/ingest", json=_ORIGINAL, headers={"X-API-Key": "test-secret"})
    incident_id = (await client.get("/incidents")).json()[0]["id"]

    # Write a real temp file
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
        f.write(b"\xff\xd8\xff")
        tmp_path = f.name

    try:
        import datetime as dt
        media = IncidentMedia(
            incident_id=incident_id,
            update_id=None,
            filename=os.path.basename(tmp_path),
            mimetype="image/jpeg",
            file_path=tmp_path,
            received_at=dt.datetime.now(dt.timezone.utc),
        )
        db_session.add(media)
        await db_session.commit()
        await db_session.refresh(media)

        r = await client.get(f"/media/{media.id}")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("image/jpeg")
        assert r.content == b"\xff\xd8\xff"
    finally:
        os.unlink(tmp_path)


async def test_serve_media_404_for_missing_record(client):
    r = await client.get("/media/9999")
    assert r.status_code == 404


async def test_relink_update_to_different_incident(client):
    # Create two incidents in different groups
    payload_b = {
        "event": "message.received",
        "data": {
            "id": "msg-c",
            "type": "chat",
            "isGroup": True,
            "chatId": "999@g.us",
            "chat": {"name": "Block B"},
            "author": "2541@c.us",
            "notifyName": "Alice",
            "body": "Another issue",
            "timestamp": 1782293350,
        },
    }
    with patch("main.classify_message", new=AsyncMock(return_value=_INCIDENT_CLASS)):
        with patch("main.push_incident", new=AsyncMock()):
            await client.post("/api/v1/ops/ingest", json=_ORIGINAL, headers={"X-API-Key": "test-secret"})
            await client.post("/api/v1/ops/ingest", json=payload_b, headers={"X-API-Key": "test-secret"})

    incidents = (await client.get("/incidents")).json()
    inc_a_id = next(i["id"] for i in incidents if "Block A" in i["property_name"])
    inc_b_id = next(i["id"] for i in incidents if "Block B" in i["property_name"])

    # Create an update attached to inc_a
    routing = {"routing": "update", "ticket_id": inc_a_id}
    with patch("main.classify_message", new=AsyncMock(return_value=_INCIDENT_CLASS)):
        with patch("main.classify_update_or_new", new=AsyncMock(return_value=routing)):
            await client.post("/api/v1/ops/ingest", json=_FOLLOWUP, headers={"X-API-Key": "test-secret"})

    detail_a = (await client.get(f"/incidents/{inc_a_id}")).json()
    update_id = detail_a["updates"][0]["id"]

    # Relink it to inc_b
    r = await client.patch(
        f"/incidents/{update_id}/relink",
        json={"incident_id": inc_b_id},
        headers={"X-API-Key": "test-secret"},
    )
    assert r.status_code == 200
    assert r.json()["incident_id"] == inc_b_id

    # Verify it moved
    detail_a2 = (await client.get(f"/incidents/{inc_a_id}")).json()
    detail_b2 = (await client.get(f"/incidents/{inc_b_id}")).json()
    assert len(detail_a2["updates"]) == 0
    assert len(detail_b2["updates"]) == 1


async def test_relink_requires_auth(client):
    r = await client.patch("/incidents/1/relink", json={"incident_id": 2}, headers={"X-API-Key": "wrong"})
    assert r.status_code == 401


async def test_relink_404_for_missing_update(client):
    r = await client.patch(
        "/incidents/9999/relink",
        json={"incident_id": 1},
        headers={"X-API-Key": "test-secret"},
    )
    assert r.status_code == 404
