from unittest.mock import AsyncMock, patch


async def test_incidents_returns_empty_list_initially(client):
    response = await client.get("/incidents")
    assert response.status_code == 200
    assert response.json() == []


async def test_incidents_returns_staged_record(client):
    classification = {"issues": [{
        "category": "electrical", "priority": "medium", "confidence": 0.88,
        "message_snippet": "Main fuse box tripped on ground floor",
    }]}
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
    assert records[0]["priority"] == "medium"
    assert records[0]["status"] == "review"


async def test_dashboard_returns_html(authenticated_client):
    response = await authenticated_client.get("/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


async def test_dashboard_contains_incident_card_markup(authenticated_client):
    classification = {"issues": [{
        "category": "plumbing", "priority": "high", "confidence": 0.90,
        "message_snippet": "Pipe burst in basement",
    }]}
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
            await authenticated_client.post("/api/v1/ops/ingest", json=payload,
                              headers={"X-API-Key": "test-secret"})
    response = await authenticated_client.get("/")
    assert response.status_code == 200
    assert b'class="ticket-row' in response.content
    assert b"Test Property" in response.content


async def test_dashboard_has_filter_controls(authenticated_client):
    response = await authenticated_client.get("/")
    assert response.status_code == 200
    assert b'id="search-input"' in response.content
    assert b'data-group="status"' in response.content


async def test_dashboard_shows_review_badge(authenticated_client):
    from tests.test_ingest import _VALID_PAYLOAD, _INCIDENT_CLASSIFICATION
    with patch("main.classify_message", new=AsyncMock(return_value=_INCIDENT_CLASSIFICATION)):
        with patch("main.push_incident", new=AsyncMock()):
            await authenticated_client.post("/api/v1/ops/ingest", json=_VALID_PAYLOAD, headers={"X-API-Key": "test-secret"})
    response = await authenticated_client.get("/")
    assert b"badge-review" in response.content
    assert b"data-id=" in response.content


async def test_incidents_since_id_returns_only_newer(client):
    classification = {"issues": [{
        "category": "plumbing", "priority": "high", "confidence": 0.92,
        "message_snippet": "Pipe burst in basement",
    }]}
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


async def test_list_incidents_statuses_filter_returns_only_resolved(client):
    from unittest.mock import AsyncMock, patch
    classification = {"issues": [{
        "category": "plumbing", "priority": "high", "confidence": 0.92, "message_snippet": "Issue A",
    }]}
    payload_a = {
        "event": "message.received",
        "data": {"id": "msg-s1", "type": "chat", "isGroup": True, "chatId": "1@g.us",
                 "chat": {"name": "Block A"}, "author": "2541@c.us", "body": "Issue A", "timestamp": 1782293340},
    }
    payload_b = {
        "event": "message.received",
        "data": {"id": "msg-s2", "type": "chat", "isGroup": True, "chatId": "1@g.us",
                 "chat": {"name": "Block A"}, "author": "2541@c.us", "body": "Issue B", "timestamp": 1782293341},
    }
    with patch("main.classify_message", new=AsyncMock(return_value=classification)):
        with patch("main.push_incident", new=AsyncMock()):
            await client.post("/api/v1/ops/ingest", json=payload_a, headers={"X-API-Key": "test-secret"})
            await client.post("/api/v1/ops/ingest", json=payload_b, headers={"X-API-Key": "test-secret"})

    all_ids = [i["id"] for i in (await client.get("/incidents")).json()]
    assert len(all_ids) == 2

    # Resolve only the first
    await client.patch(f"/incidents/{all_ids[0]}/status",
                       json={"status": "resolved"}, headers={"X-API-Key": "test-secret"})

    resolved = (await client.get("/incidents?statuses=resolved")).json()
    assert len(resolved) == 1
    assert resolved[0]["status"] == "resolved"

    review = (await client.get("/incidents?statuses=review")).json()
    assert len(review) == 1
    assert review[0]["status"] == "review"

    all_back = (await client.get("/incidents")).json()
    assert len(all_back) == 2


async def test_list_incidents_statuses_filter_multiple(client):
    from unittest.mock import AsyncMock, patch
    classification = {"issues": [{
        "category": "plumbing", "priority": "high", "confidence": 0.92, "message_snippet": "Issue M",
    }]}
    for msg_id, body_text in [("msg-m1", "Issue M1"), ("msg-m2", "Issue M2")]:
        payload = {
            "event": "message.received",
            "data": {"id": msg_id, "type": "chat", "isGroup": True, "chatId": "2@g.us",
                     "chat": {"name": "Block B"}, "author": "2541@c.us",
                     "body": body_text, "timestamp": 1782293340},
        }
        with patch("main.classify_message", new=AsyncMock(return_value=classification)):
            with patch("main.push_incident", new=AsyncMock()):
                await client.post("/api/v1/ops/ingest", json=payload, headers={"X-API-Key": "test-secret"})

    all_ids = [i["id"] for i in (await client.get("/incidents")).json()]
    await client.patch(f"/incidents/{all_ids[0]}/status",
                       json={"status": "resolved"}, headers={"X-API-Key": "test-secret"})

    both = (await client.get("/incidents?statuses=resolved&statuses=review")).json()
    assert len(both) == 2


async def test_archive_route_returns_html(authenticated_client):
    r = await authenticated_client.get("/archive")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]


async def test_archive_route_shows_only_resolved_incidents(authenticated_client):
    from unittest.mock import AsyncMock, patch
    classification = {"issues": [{
        "category": "plumbing", "priority": "high", "confidence": 0.92, "message_snippet": "Archive issue",
    }]}
    payload_live = {
        "event": "message.received",
        "data": {"id": "msg-arc1", "type": "chat", "isGroup": True, "chatId": "3@g.us",
                 "chat": {"name": "Live Property"}, "author": "2541@c.us",
                 "body": "Live issue", "timestamp": 1782293340},
    }
    payload_resolved = {
        "event": "message.received",
        "data": {"id": "msg-arc2", "type": "chat", "isGroup": True, "chatId": "3@g.us",
                 "chat": {"name": "Resolved Property"}, "author": "2541@c.us",
                 "body": "Resolved issue", "timestamp": 1782293341},
    }
    with patch("main.classify_message", new=AsyncMock(return_value=classification)):
        with patch("main.push_incident", new=AsyncMock()):
            await authenticated_client.post("/api/v1/ops/ingest", json=payload_live, headers={"X-API-Key": "test-secret"})
            await authenticated_client.post("/api/v1/ops/ingest", json=payload_resolved, headers={"X-API-Key": "test-secret"})

    all_ids = sorted([i["id"] for i in (await authenticated_client.get("/incidents")).json()])
    await authenticated_client.patch(f"/incidents/{all_ids[-1]}/status",
                       json={"status": "resolved"}, headers={"X-API-Key": "test-secret"})

    r = await authenticated_client.get("/archive")
    assert r.status_code == 200
    assert b"Resolved Property" in r.content
    assert b"Live Property" not in r.content


async def test_live_dashboard_excludes_resolved_incidents(authenticated_client):
    from unittest.mock import AsyncMock, patch
    classification = {"issues": [{
        "category": "plumbing", "priority": "high", "confidence": 0.92, "message_snippet": "To be resolved",
    }]}
    payload = {
        "event": "message.received",
        "data": {"id": "msg-exc1", "type": "chat", "isGroup": True, "chatId": "4@g.us",
                 "chat": {"name": "Exclude Me"}, "author": "2541@c.us",
                 "body": "To be resolved", "timestamp": 1782293340},
    }
    with patch("main.classify_message", new=AsyncMock(return_value=classification)):
        with patch("main.push_incident", new=AsyncMock()):
            await authenticated_client.post("/api/v1/ops/ingest", json=payload, headers={"X-API-Key": "test-secret"})

    incident_id = (await authenticated_client.get("/incidents")).json()[0]["id"]
    await authenticated_client.patch(f"/incidents/{incident_id}/status",
                       json={"status": "resolved"}, headers={"X-API-Key": "test-secret"})

    r = await authenticated_client.get("/")
    assert r.status_code == 200
    assert b"Exclude Me" not in r.content


from datetime import datetime, timezone
from models import User, UserGroup
from auth import hash_password as _hash_pw

_HASHED_TESTPASS_VISIBLE = _hash_pw("testpass")  # computed once at module load


async def test_incidents_filtered_by_group_for_user_role(client, db_session):
    """A user with role='user' only sees incidents from their assigned groups."""
    from unittest.mock import AsyncMock, patch

    classification = {"issues": [{
        "category": "maintenance", "priority": "low", "confidence": 0.9, "message_snippet": "Group filter incident",
    }]}
    for chatId, chatName, msg_id, body_text in [
        ("visible@g.us", "Visible Property", "msg-vis1", "Visible incident"),
        ("hidden@g.us",  "Hidden Property",  "msg-hid1", "Hidden incident"),
    ]:
        payload = {
            "event": "message.received",
            "data": {"id": msg_id, "type": "chat", "isGroup": True,
                     "chatId": chatId, "chat": {"name": chatName},
                     "author": "254700000001@c.us", "body": body_text, "timestamp": 1782293340},
        }
        with patch("main.classify_message", new=AsyncMock(return_value=classification)):
            with patch("main.push_incident", new=AsyncMock()):
                await client.post("/api/v1/ops/ingest", json=payload, headers={"X-API-Key": "test-secret"})

    limited = User(
        username="limiteduser",
        hashed_password=_HASHED_TESTPASS_VISIBLE,
        created_at=datetime.now(timezone.utc),
        role="user",
    )
    db_session.add(limited)
    await db_session.flush()
    db_session.add(UserGroup(user_id=limited.id, group_id="visible@g.us"))
    await db_session.commit()

    await client.post("/login", data={"username": "limiteduser", "password": "testpass"})
    resp = await client.get("/incidents")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 1
    assert data[0]["property_name"] == "Visible Property"
