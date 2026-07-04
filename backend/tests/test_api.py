from unittest.mock import AsyncMock, patch

async def _classify_incident(message, db):
    return {"issues": [{"category": "plumbing", "priority": "high", "confidence": 0.92, "message_snippet": message}]}

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
    with patch("main.classify_message", new=_classify_incident):
        with patch("main.push_incident", new=AsyncMock()):
            await client.post("/api/v1/ops/ingest", json=_ORIGINAL, headers={"X-API-Key": "test-secret"})
    incidents = (await client.get("/incidents")).json()
    assert "update_count" in incidents[0]
    assert "media_count" in incidents[0]
    assert incidents[0]["update_count"] == 0
    assert incidents[0]["media_count"] == 0


async def test_list_incidents_update_count_increments(client):
    with patch("main.classify_message", new=_classify_incident):
        with patch("main.push_incident", new=AsyncMock()):
            await client.post("/api/v1/ops/ingest", json=_ORIGINAL, headers={"X-API-Key": "test-secret"})
    incident_id = (await client.get("/incidents")).json()[0]["id"]

    routing = {"routing": "update", "ticket_id": incident_id}
    with patch("main.classify_message", new=_classify_incident):
        with patch("main.classify_update_or_new", new=AsyncMock(return_value=routing)):
            await client.post("/api/v1/ops/ingest", json=_FOLLOWUP, headers={"X-API-Key": "test-secret"})

    incidents = (await client.get("/incidents")).json()
    assert incidents[0]["update_count"] == 1


async def test_get_incident_detail_returns_updates(client):
    with patch("main.classify_message", new=_classify_incident):
        with patch("main.push_incident", new=AsyncMock()):
            await client.post("/api/v1/ops/ingest", json=_ORIGINAL, headers={"X-API-Key": "test-secret"})
    incident_id = (await client.get("/incidents")).json()[0]["id"]

    routing = {"routing": "update", "ticket_id": incident_id}
    with patch("main.classify_message", new=_classify_incident):
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
    with patch("main.classify_message", new=_classify_incident):
        with patch("main.push_incident", new=AsyncMock()):
            await client.post("/api/v1/ops/ingest", json=_ORIGINAL, headers={"X-API-Key": "test-secret"})
    incident_id = (await client.get("/incidents")).json()[0]["id"]

    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = os.path.join(tmpdir, "test.jpg")
        with open(tmp_path, "wb") as f:
            f.write(b"\xff\xd8\xff")

        import datetime as dt
        media = IncidentMedia(
            incident_id=incident_id,
            update_id=None,
            filename="test.jpg",
            mimetype="image/jpeg",
            file_path=tmp_path,
            received_at=dt.datetime.now(dt.timezone.utc),
        )
        db_session.add(media)
        await db_session.commit()
        await db_session.refresh(media)

        # Patch MEDIA_DIR so the path-containment guard accepts tmpdir
        with patch("main.MEDIA_DIR", tmpdir):
            r = await client.get(f"/media/{media.id}", headers={"X-API-Key": "test-secret"})
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("image/jpeg")
        assert r.content == b"\xff\xd8\xff"


async def test_serve_media_requires_auth(client):
    r = await client.get("/media/1")
    assert r.status_code == 401


async def test_serve_media_404_for_missing_record(client):
    r = await client.get("/media/9999", headers={"X-API-Key": "test-secret"})
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
    with patch("main.classify_message", new=_classify_incident):
        with patch("main.push_incident", new=AsyncMock()):
            await client.post("/api/v1/ops/ingest", json=_ORIGINAL, headers={"X-API-Key": "test-secret"})
            await client.post("/api/v1/ops/ingest", json=payload_b, headers={"X-API-Key": "test-secret"})

    incidents = (await client.get("/incidents")).json()
    inc_a_id = next(i["id"] for i in incidents if "Block A" in i["property_name"])
    inc_b_id = next(i["id"] for i in incidents if "Block B" in i["property_name"])

    # Create an update attached to inc_a
    routing = {"routing": "update", "ticket_id": inc_a_id}
    with patch("main.classify_message", new=_classify_incident):
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


async def test_relink_requires_auth():
    """No valid API key and no session cookie → 401."""
    from httpx import AsyncClient, ASGITransport
    from main import app as _app
    async with AsyncClient(transport=ASGITransport(app=_app), base_url="http://test") as ac:
        r = await ac.patch("/incidents/1/relink", json={"incident_id": 2}, headers={"X-API-Key": "wrong"})
    assert r.status_code == 401


async def test_relink_404_for_missing_update(client):
    r = await client.patch(
        "/incidents/9999/relink",
        json={"incident_id": 1},
        headers={"X-API-Key": "test-secret"},
    )
    assert r.status_code == 404


async def test_relink_promote_update_to_standalone_incident(client):
    # Create original incident
    with patch("main.classify_message", new=_classify_incident):
        with patch("main.push_incident", new=AsyncMock()):
            await client.post("/api/v1/ops/ingest", json=_ORIGINAL, headers={"X-API-Key": "test-secret"})
    incident_id = (await client.get("/incidents")).json()[0]["id"]

    # Create an update on it
    routing = {"routing": "update", "ticket_id": incident_id}
    with patch("main.classify_message", new=_classify_incident):
        with patch("main.classify_update_or_new", new=AsyncMock(return_value=routing)):
            await client.post("/api/v1/ops/ingest", json=_FOLLOWUP, headers={"X-API-Key": "test-secret"})

    detail = (await client.get(f"/incidents/{incident_id}")).json()
    update_id = detail["updates"][0]["id"]

    # Promote the update to a standalone incident
    r = await client.patch(
        f"/incidents/{update_id}/relink",
        json={"incident_id": None},
        headers={"X-API-Key": "test-secret"},
    )
    assert r.status_code == 200
    assert r.json()["promoted"] is True
    new_incident_id = r.json()["incident_id"]
    assert new_incident_id != incident_id

    # Original incident should have 0 updates now
    detail_orig = (await client.get(f"/incidents/{incident_id}")).json()
    assert len(detail_orig["updates"]) == 0

    # New incident should exist and have the update's message body
    new_detail = (await client.get(f"/incidents/{new_incident_id}")).json()
    assert new_detail["message_body"] == "Still leaking"
    assert new_detail["status"] == "review"


async def test_ingest_creates_status_history_entry(client):
    with patch("main.classify_message", new=_classify_incident):
        with patch("main.push_incident", new=AsyncMock()):
            await client.post("/api/v1/ops/ingest", json=_ORIGINAL, headers={"X-API-Key": "test-secret"})
    incident_id = (await client.get("/incidents")).json()[0]["id"]
    detail = (await client.get(f"/incidents/{incident_id}")).json()
    assert "status_history" in detail
    assert len(detail["status_history"]) == 1
    assert detail["status_history"][0]["from_status"] is None
    assert detail["status_history"][0]["to_status"] == "review"


async def test_get_detail_includes_relinked_field(client):
    with patch("main.classify_message", new=_classify_incident):
        with patch("main.push_incident", new=AsyncMock()):
            await client.post("/api/v1/ops/ingest", json=_ORIGINAL, headers={"X-API-Key": "test-secret"})
    incident_id = (await client.get("/incidents")).json()[0]["id"]
    routing = {"routing": "update", "ticket_id": incident_id}
    with patch("main.classify_message", new=_classify_incident):
        with patch("main.classify_update_or_new", new=AsyncMock(return_value=routing)):
            await client.post("/api/v1/ops/ingest", json=_FOLLOWUP, headers={"X-API-Key": "test-secret"})
    detail = (await client.get(f"/incidents/{incident_id}")).json()
    assert "relinked" in detail["updates"][0]
    assert detail["updates"][0]["relinked"] is False


async def test_status_change_appends_history(client):
    with patch("main.classify_message", new=_classify_incident):
        with patch("main.push_incident", new=AsyncMock()):
            await client.post("/api/v1/ops/ingest", json=_ORIGINAL, headers={"X-API-Key": "test-secret"})
    incident_id = (await client.get("/incidents")).json()[0]["id"]

    await client.patch(
        f"/incidents/{incident_id}/status",
        json={"status": "acknowledged"},
        headers={"X-API-Key": "test-secret"},
    )
    detail = (await client.get(f"/incidents/{incident_id}")).json()
    assert detail["status"] == "acknowledged"
    assert len(detail["status_history"]) == 2          # creation row + status change
    assert detail["status_history"][1]["from_status"] == "review"
    assert detail["status_history"][1]["to_status"] == "acknowledged"


async def test_relink_sets_relinked_flag(client):
    # Create two incidents
    with patch("main.classify_message", new=_classify_incident):
        with patch("main.push_incident", new=AsyncMock()):
            await client.post("/api/v1/ops/ingest", json=_ORIGINAL, headers={"X-API-Key": "test-secret"})
    incident_id = (await client.get("/incidents")).json()[0]["id"]

    # Add an update to it
    routing = {"routing": "update", "ticket_id": incident_id}
    with patch("main.classify_message", new=_classify_incident):
        with patch("main.classify_update_or_new", new=AsyncMock(return_value=routing)):
            await client.post("/api/v1/ops/ingest", json=_FOLLOWUP, headers={"X-API-Key": "test-secret"})

    # Create a second incident to relink to
    second_payload = {
        "event": "message.received",
        "data": {
            "id": "msg-c", "type": "chat", "isGroup": True,
            "chatId": "123@g.us", "chat": {"name": "Block A"},
            "author": "2541@c.us", "notifyName": "Alice",
            "body": "Different issue", "timestamp": 1782293500,
        },
    }
    with patch("main.classify_message", new=_classify_incident):
        with patch("main.push_incident", new=AsyncMock()):
            await client.post("/api/v1/ops/ingest", json=second_payload, headers={"X-API-Key": "test-secret"})
    incidents = (await client.get("/incidents")).json()
    second_id = next(i["id"] for i in incidents if i["id"] != incident_id)

    # Get the update id
    detail = (await client.get(f"/incidents/{incident_id}")).json()
    update_id = detail["updates"][0]["id"]

    # Relink the update to second incident
    resp = await client.patch(
        f"/incidents/{update_id}/relink",
        json={"incident_id": second_id},
        headers={"X-API-Key": "test-secret"},
    )
    assert resp.status_code == 200
    assert resp.json()["incident_id"] == second_id
    second_detail = (await client.get(f"/incidents/{second_id}")).json()
    assert second_detail["updates"][0]["relinked"] is True


async def test_sibling_tickets_empty_for_non_split_ticket(client):
    with patch("main.classify_message", new=_classify_incident):
        with patch("main.push_incident", new=AsyncMock()):
            await client.post("/api/v1/ops/ingest", json=_ORIGINAL, headers={"X-API-Key": "test-secret"})
    incident_id = (await client.get("/incidents")).json()[0]["id"]
    detail = (await client.get(f"/incidents/{incident_id}")).json()
    assert detail["sibling_tickets"] == []


async def test_sibling_tickets_returns_other_split_rows_in_order(client):
    async def _classify_three_issues(message, db):
        return {"issues": [
            {"category": "plumbing", "priority": "high", "confidence": 0.9, "message_snippet": "pump leaking"},
            {"category": "lift", "priority": "urgent", "confidence": 0.95, "message_snippet": "lift stuck"},
            {"category": "security", "priority": "medium", "confidence": 0.85, "message_snippet": "broken gate"},
        ]}
    payload = {
        "event": "message.received",
        "data": {
            "id": "msg-siblings-1", "type": "chat", "isGroup": True,
            "chatId": "127@g.us", "chat": {"name": "Block J"},
            "author": "254700000009@c.us", "notifyName": "Ivy",
            "body": "1. pump leaking 2. lift stuck 3. broken gate",
            "timestamp": 1782293340,
        },
    }
    with patch("main.classify_message", new=_classify_three_issues):
        with patch("main.push_incident", new=AsyncMock()):
            await client.post("/api/v1/ops/ingest", json=payload, headers={"X-API-Key": "test-secret"})

    incidents = (await client.get("/incidents")).json()
    split_incidents = [i for i in incidents if i["property_name"] == "Block J"]
    assert len(split_incidents) == 3
    by_category = {i["category"]: i["id"] for i in split_incidents}

    detail = (await client.get(f"/incidents/{by_category['plumbing']}")).json()
    assert [s["id"] for s in detail["sibling_tickets"]] == [by_category["lift"], by_category["security"]]
    assert detail["sibling_tickets"][0]["category"] == "lift"
    assert detail["sibling_tickets"][1]["category"] == "security"
    assert all(s["id"] != by_category["plumbing"] for s in detail["sibling_tickets"])
