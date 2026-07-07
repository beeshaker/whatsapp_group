from unittest.mock import AsyncMock, patch
from datetime import datetime, timezone
from sqlalchemy import select
from models import Incident, IncidentUpdate

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

_INCIDENT_CLASSIFICATION = {"issues": [{
    "category": "plumbing", "priority": "high", "confidence": 0.92,
    "message_snippet": "The water pump on floor 3 is leaking heavily",
}]}

_NOISE_CLASSIFICATION = {"issues": []}

_LOW_CONF_CLASSIFICATION = {"issues": [{
    "category": "plumbing", "priority": "low", "confidence": 0.3,
    "message_snippet": "The water pump on floor 3 is leaking heavily",
}]}

_MID_CONF_CLASSIFICATION = {"issues": [{
    "category": "plumbing", "priority": "low", "confidence": 0.75,
    "message_snippet": "The water pump on floor 3 is leaking heavily",
}]}


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


async def test_ingest_handles_image_type_as_media(client):
    # Image messages are now handled by _handle_media_ingest instead of being ignored.
    # This payload has no caption and no open tickets, so it returns staged_media with no incident_id.
    payload = {
        **_VALID_PAYLOAD,
        "data": {**_VALID_PAYLOAD["data"], "type": "image"},
    }
    response = await client.post(
        "/api/v1/ops/ingest", json=payload, headers={"X-API-Key": "test-secret"}
    )
    assert response.status_code == 202
    assert response.json()["status"] == "staged_media"
    assert "incident_id" not in response.json()


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


async def test_ingest_discards_below_new_threshold(client):
    with patch("main.classify_message", new=AsyncMock(return_value=_MID_CONF_CLASSIFICATION)):
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
    assert body["tickets_created"] == 1
    assert body["updates_created"] == 0


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


async def test_multi_issue_split_creates_multiple_incidents_with_issue_index(client, db_session):
    async def _classify_three_issues(message, db):
        return {"issues": [
            {"category": "plumbing", "priority": "high", "confidence": 0.9, "message_snippet": "pump leaking"},
            {"category": "lift", "priority": "urgent", "confidence": 0.95, "message_snippet": "lift stuck on floor 3"},
            {"category": "security", "priority": "medium", "confidence": 0.85, "message_snippet": "broken gate"},
        ]}

    payload = {
        "event": "message.received",
        "data": {
            "id": "msg-split-1", "type": "chat", "isGroup": True,
            "chatId": "120@g.us", "chat": {"name": "Block C"},
            "author": "254700000002@c.us", "notifyName": "Bob",
            "body": "1. pump leaking 2. lift stuck on floor 3 3. broken gate",
            "timestamp": 1782293340,
        },
    }
    with patch("main.classify_message", new=_classify_three_issues):
        with patch("main.push_incident", new=AsyncMock()):
            response = await client.post(
                "/api/v1/ops/ingest", json=payload, headers={"X-API-Key": "test-secret"}
            )
    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "staged"
    assert body["tickets_created"] == 3
    assert body["updates_created"] == 0

    result = await db_session.execute(
        select(Incident).where(Incident.message_id == "msg-split-1").order_by(Incident.issue_index)
    )
    rows = result.scalars().all()
    assert [r.issue_index for r in rows] == [0, 1, 2]
    assert [r.category for r in rows] == ["plumbing", "lift", "security"]


async def test_multi_issue_split_mixed_new_and_update(client, db_session):
    existing_payload = {
        "event": "message.received",
        "data": {
            "id": "msg-existing-1", "type": "chat", "isGroup": True,
            "chatId": "121@g.us", "chat": {"name": "Block D"},
            "author": "254700000003@c.us", "notifyName": "Carol",
            "body": "The water pump on floor 3 is leaking heavily",
            "timestamp": 1782293300,
        },
    }
    with patch("main.classify_message", new=AsyncMock(return_value=_INCIDENT_CLASSIFICATION)):
        with patch("main.push_incident", new=AsyncMock()):
            await client.post(
                "/api/v1/ops/ingest", json=existing_payload, headers={"X-API-Key": "test-secret"}
            )
    existing_incident_id = (await client.get("/incidents")).json()[0]["id"]

    async def _classify_mixed(message, db):
        return {"issues": [
            {"category": "plumbing", "priority": "high", "confidence": 0.9, "message_snippet": "still leaking"},
            {"category": "lift", "priority": "urgent", "confidence": 0.95, "message_snippet": "lift stuck"},
        ]}

    async def _routing_update_then_new(message, open_tickets):
        if message == "still leaking":
            return {"routing": "update", "ticket_id": existing_incident_id}
        return {"routing": "new"}

    payload = {
        "event": "message.received",
        "data": {
            "id": "msg-mixed-1", "type": "chat", "isGroup": True,
            "chatId": "121@g.us", "chat": {"name": "Block D"},
            "author": "254700000003@c.us", "notifyName": "Carol",
            "body": "1. still leaking 2. lift stuck",
            "timestamp": 1782293400,
        },
    }
    with patch("main.classify_message", new=_classify_mixed):
        with patch("main.classify_update_or_new", new=_routing_update_then_new):
            with patch("main.push_incident", new=AsyncMock()):
                response = await client.post(
                    "/api/v1/ops/ingest", json=payload, headers={"X-API-Key": "test-secret"}
                )
    body = response.json()
    assert body["status"] == "staged"
    assert body["tickets_created"] == 1
    assert body["updates_created"] == 1

    upd_result = await db_session.execute(
        select(IncidentUpdate).where(IncidentUpdate.message_id == "msg-mixed-1")
    )
    updates = upd_result.scalars().all()
    assert len(updates) == 1
    assert updates[0].issue_index == 0
    assert updates[0].incident_id == existing_incident_id

    inc_result = await db_session.execute(
        select(Incident).where(Incident.message_id == "msg-mixed-1")
    )
    new_incidents = inc_result.scalars().all()
    assert len(new_incidents) == 1
    assert new_incidents[0].issue_index == 1
    assert new_incidents[0].category == "lift"


async def test_multi_issue_split_confidence_filtering_drops_only_low_issue(client, db_session):
    async def _classify_two_confidences(message, db):
        return {"issues": [
            {"category": "plumbing", "priority": "high", "confidence": 0.9, "message_snippet": "pump leaking"},
            {"category": "cleaning", "priority": "low", "confidence": 0.4, "message_snippet": "bin overflowing"},
        ]}

    payload = {
        "event": "message.received",
        "data": {
            "id": "msg-partial-conf-1", "type": "chat", "isGroup": True,
            "chatId": "122@g.us", "chat": {"name": "Block E"},
            "author": "254700000004@c.us", "notifyName": "Dan",
            "body": "1. pump leaking 2. bin overflowing",
            "timestamp": 1782293340,
        },
    }
    with patch("main.classify_message", new=_classify_two_confidences):
        with patch("main.push_incident", new=AsyncMock()):
            response = await client.post(
                "/api/v1/ops/ingest", json=payload, headers={"X-API-Key": "test-secret"}
            )
    body = response.json()
    assert body["status"] == "staged"
    assert body["tickets_created"] == 1

    result = await db_session.execute(
        select(Incident).where(Incident.message_id == "msg-partial-conf-1")
    )
    rows = result.scalars().all()
    assert len(rows) == 1
    assert rows[0].issue_index == 0
    assert rows[0].category == "plumbing"


async def test_multi_issue_split_later_issue_updates_ticket_created_by_same_split(client, db_session):
    async def _classify_two_issues(message, db):
        return {"issues": [
            {"category": "plumbing", "priority": "high", "confidence": 0.9, "message_snippet": "pump leaking on floor 3"},
            {"category": "plumbing", "priority": "high", "confidence": 0.88, "message_snippet": "same pump still leaking"},
        ]}

    async def _routing_second_is_update(message, open_tickets):
        if message == "same pump still leaking":
            assert len(open_tickets) == 1
            return {"routing": "update", "ticket_id": open_tickets[0]["id"]}
        return {"routing": "new"}

    payload = {
        "event": "message.received",
        "data": {
            "id": "msg-self-follow-1", "type": "chat", "isGroup": True,
            "chatId": "123@g.us", "chat": {"name": "Block F"},
            "author": "254700000005@c.us", "notifyName": "Eve",
            "body": "1. pump leaking on floor 3 2. same pump still leaking",
            "timestamp": 1782293340,
        },
    }
    with patch("main.classify_message", new=_classify_two_issues):
        with patch("main.classify_update_or_new", new=_routing_second_is_update):
            with patch("main.push_incident", new=AsyncMock()):
                response = await client.post(
                    "/api/v1/ops/ingest", json=payload, headers={"X-API-Key": "test-secret"}
                )
    body = response.json()
    assert body["status"] == "staged"
    assert body["tickets_created"] == 1
    assert body["updates_created"] == 1

    inc_result = await db_session.execute(select(Incident).where(Incident.message_id == "msg-self-follow-1"))
    incidents = inc_result.scalars().all()
    assert len(incidents) == 1
    assert incidents[0].issue_index == 0

    upd_result = await db_session.execute(select(IncidentUpdate).where(IncidentUpdate.message_id == "msg-self-follow-1"))
    updates = upd_result.scalars().all()
    assert len(updates) == 1
    assert updates[0].issue_index == 1
    assert updates[0].incident_id == incidents[0].id


async def test_retried_delivery_of_fully_processed_message_is_duplicate(client, db_session):
    payload = {
        "event": "message.received",
        "data": {
            "id": "msg-retry-full-1", "type": "chat", "isGroup": True,
            "chatId": "124@g.us", "chat": {"name": "Block G"},
            "author": "254700000006@c.us", "notifyName": "Fay",
            "body": "pump leaking",
            "timestamp": 1782293340,
        },
    }
    with patch("main.classify_message", new=AsyncMock(return_value=_INCIDENT_CLASSIFICATION)):
        with patch("main.push_incident", new=AsyncMock()):
            r1 = await client.post("/api/v1/ops/ingest", json=payload, headers={"X-API-Key": "test-secret"})
            r2 = await client.post("/api/v1/ops/ingest", json=payload, headers={"X-API-Key": "test-secret"})
    assert r1.json()["status"] == "staged"
    assert r2.json()["status"] == "duplicate"


async def test_retried_delivery_of_partially_processed_message_completes_missing_issues(client, db_session):
    async def _classify_two_issues(message, db):
        return {"issues": [
            {"category": "plumbing", "priority": "high", "confidence": 0.9, "message_snippet": "pump leaking"},
            {"category": "lift", "priority": "urgent", "confidence": 0.95, "message_snippet": "lift stuck"},
        ]}

    payload = {
        "event": "message.received",
        "data": {
            "id": "msg-retry-partial-1", "type": "chat", "isGroup": True,
            "chatId": "125@g.us", "chat": {"name": "Block H"},
            "author": "254700000007@c.us", "notifyName": "Gus",
            "body": "1. pump leaking 2. lift stuck",
            "timestamp": 1782293340,
        },
    }

    # Simulate a crash after issue 0 committed but before issue 1: insert the
    # issue_index=0 row directly, bypassing the ingest endpoint entirely.
    db_session.add(Incident(
        group_id="125@g.us", property_name="Block H", reporter_name="Gus",
        reporter_phone="254700000007", message_body="pump leaking",
        category="plumbing", priority="high", confidence=0.9, status="review",
        received_at=datetime.now(timezone.utc), message_id="msg-retry-partial-1", issue_index=0,
    ))
    await db_session.commit()

    with patch("main.classify_message", new=_classify_two_issues):
        with patch("main.push_incident", new=AsyncMock()):
            response = await client.post(
                "/api/v1/ops/ingest", json=payload, headers={"X-API-Key": "test-secret"}
            )
    body = response.json()
    assert body["status"] == "staged"
    assert body["tickets_created"] == 1
    assert body["updates_created"] == 0

    result = await db_session.execute(
        select(Incident).where(Incident.message_id == "msg-retry-partial-1").order_by(Incident.issue_index)
    )
    rows = result.scalars().all()
    assert len(rows) == 2
    assert [r.issue_index for r in rows] == [0, 1]
    assert rows[1].category == "lift"
