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

_INCIDENT_CLASS = {"issues": [{
    "category": "plumbing", "priority": "high", "confidence": 0.92,
    "message_snippet": "The water pump on floor 3 is leaking",
}]}
_NOISE_CLASS = {"issues": []}


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
    assert r2.json()["status"] == "staged"
    assert r2.json()["updates_created"] == 1
    assert r2.json()["tickets_created"] == 0


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
    assert r1.json()["status"] == "staged"
    assert r1.json()["updates_created"] == 1
    assert r2.json()["status"] == "duplicate"


_IMAGE_PAYLOAD = {
    "event": "message.received",
    "data": {
        "id": "msg-image-1",
        "type": "image",
        "isGroup": True,
        "chatId": "120363@g.us",
        "chat": {"name": "Oakridge Block A"},
        "author": "254711111111@c.us",
        "notifyName": "Caretaker A",
        "caption": "Burst pipe flooding the corridor",
        "mediaUrl": "http://openwa.local/media/abc.jpg",
        "timestamp": 1782293500,
    },
}

_IMAGE_NO_CAPTION = {
    "event": "message.received",
    "data": {
        "id": "msg-image-2",
        "type": "image",
        "isGroup": True,
        "chatId": "120363@g.us",
        "chat": {"name": "Oakridge Block A"},
        "author": "254711111111@c.us",
        "mediaUrl": "http://openwa.local/media/def.jpg",
        "timestamp": 1782293600,
    },
}


async def test_media_with_caption_creates_incident_and_media_row(client):
    fake_media = ("abc123.jpg", "image/jpeg", "/app/media/abc123.jpg")
    with patch("main.classify_message", new=AsyncMock(return_value=_INCIDENT_CLASS)):
        with patch("main.classify_update_or_new", new=AsyncMock(return_value={"routing": "new"})):
            with patch("main.push_incident", new=AsyncMock()):
                with patch("main.download_media", new=AsyncMock(return_value=fake_media)):
                    r = await client.post(
                        "/api/v1/ops/ingest", json=_IMAGE_PAYLOAD, headers={"X-API-Key": "test-secret"}
                    )
    assert r.json()["status"] == "staged_media"
    assert "incident_id" in r.json()


async def test_media_no_caption_attaches_to_open_ticket(client):
    # Create an open ticket first
    with patch("main.classify_message", new=AsyncMock(return_value=_INCIDENT_CLASS)):
        with patch("main.push_incident", new=AsyncMock()):
            await client.post("/api/v1/ops/ingest", json=_GROUP_PAYLOAD, headers={"X-API-Key": "test-secret"})
    incident_id = (await client.get("/incidents")).json()[0]["id"]

    fake_media = ("def456.jpg", "image/jpeg", "/app/media/def456.jpg")
    with patch("main.download_media", new=AsyncMock(return_value=fake_media)):
        r = await client.post(
            "/api/v1/ops/ingest", json=_IMAGE_NO_CAPTION, headers={"X-API-Key": "test-secret"}
        )
    assert r.json()["status"] == "staged_media"
    assert r.json()["incident_id"] == incident_id


async def test_media_no_caption_no_open_ticket_returns_staged_media(client):
    r = await client.post(
        "/api/v1/ops/ingest", json=_IMAGE_NO_CAPTION, headers={"X-API-Key": "test-secret"}
    )
    assert r.json()["status"] == "staged_media"
    assert "incident_id" not in r.json()


async def test_media_download_failure_still_creates_incident_from_caption(client):
    with patch("main.classify_message", new=AsyncMock(return_value=_INCIDENT_CLASS)):
        with patch("main.classify_update_or_new", new=AsyncMock(return_value={"routing": "new"})):
            with patch("main.push_incident", new=AsyncMock()):
                with patch("main.download_media", new=AsyncMock(side_effect=Exception("network error"))):
                    r = await client.post(
                        "/api/v1/ops/ingest", json=_IMAGE_PAYLOAD, headers={"X-API-Key": "test-secret"}
                    )
    assert r.json()["status"] == "staged_media"
    incidents = (await client.get("/incidents")).json()
    assert len(incidents) == 1


async def test_media_caption_split_attaches_media_only_to_highest_confidence_issue(client, db_session):
    from sqlalchemy import select
    from models import Incident, IncidentMedia

    async def _classify_two_issues(caption, db):
        return {"issues": [
            {"category": "plumbing", "priority": "high", "confidence": 0.8, "message_snippet": "pump leaking"},
            {"category": "lift", "priority": "urgent", "confidence": 0.95, "message_snippet": "lift stuck"},
        ]}

    fake_media = ("split.jpg", "image/jpeg", "/app/media/split.jpg")
    payload = {
        "event": "message.received",
        "data": {
            "id": "msg-media-split-1", "type": "image", "isGroup": True,
            "chatId": "126@g.us", "chat": {"name": "Block I"},
            "author": "254700000008@c.us", "notifyName": "Hana",
            "caption": "1. pump leaking 2. lift stuck",
            "mediaUrl": "http://fake/media/split",
            "timestamp": 1782293340,
        },
    }
    with patch("main.classify_message", new=_classify_two_issues):
        with patch("main.classify_update_or_new", new=AsyncMock(return_value={"routing": "new"})):
            with patch("main.push_incident", new=AsyncMock()):
                with patch("main.download_media", new=AsyncMock(return_value=fake_media)):
                    r = await client.post(
                        "/api/v1/ops/ingest", json=payload, headers={"X-API-Key": "test-secret"}
                    )
    assert r.json()["status"] == "staged_media"

    inc_result = await db_session.execute(
        select(Incident).where(Incident.message_id == "msg-media-split-1").order_by(Incident.issue_index)
    )
    incidents = inc_result.scalars().all()
    assert len(incidents) == 2

    media_result = await db_session.execute(select(IncidentMedia))
    media_rows = media_result.scalars().all()
    assert len(media_rows) == 1
    lift_incident = next(i for i in incidents if i.category == "lift")
    assert media_rows[0].incident_id == lift_incident.id
