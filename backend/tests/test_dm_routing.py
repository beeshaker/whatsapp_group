from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

from models import AdminProfile, User
from auth import hash_password


_DM_PAYLOAD = {
    "event": "message.received",
    "data": {
        "id": "dm-msg-1",
        "type": "chat",
        "isGroup": False,
        "chatId": "254712345678@c.us",
        "from": "254712345678@c.us",
        "notifyName": "Admin User",
        "author": "254712345678@c.us",
        "body": "How many open tickets?",
        "timestamp": 1782293340,
    },
}


async def test_dm_from_known_admin_gets_reply(client, db_session):
    # Seed admin profile with matching phone
    from sqlalchemy import select
    result = await db_session.execute(
        select(User).where(User.username == "testadmin")
    )
    user = result.scalar_one_or_none()
    db_session.add(AdminProfile(user_id=user.id, whatsapp_phone="254712345678"))
    await db_session.commit()

    with patch("main.answer_query", new=AsyncMock(return_value="2 open tickets")):
        with patch("main.send_group_message", new=AsyncMock(return_value="msg-id")) as mock_send:
            resp = await client.post(
                "/api/v1/ops/ingest", json=_DM_PAYLOAD, headers={"X-API-Key": "test-secret"}
            )

    assert resp.status_code == 202
    assert resp.json()["status"] == "dm_handled"
    mock_send.assert_awaited_once_with("254712345678@c.us", "2 open tickets")


async def test_dm_from_unknown_phone_ignored(client):
    with patch("main.send_group_message", new=AsyncMock()) as mock_send:
        resp = await client.post(
            "/api/v1/ops/ingest", json=_DM_PAYLOAD, headers={"X-API-Key": "test-secret"}
        )
    assert resp.status_code == 202
    assert resp.json()["status"] == "dm_ignored"
    mock_send.assert_not_awaited()


async def test_group_message_still_processed_after_dm_logic(client):
    # Ensure @g.us messages still flow through existing incident creation
    group_payload = {
        "event": "message.received",
        "data": {
            "id": "group-msg-after-dm",
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
    with patch("main.classify_message", new=AsyncMock(return_value={
        "is_incident": True, "category": "plumbing", "severity": "high", "confidence": 0.9
    })):
        with patch("main.push_incident", new=AsyncMock()):
            resp = await client.post(
                "/api/v1/ops/ingest", json=group_payload, headers={"X-API-Key": "test-secret"}
            )
    assert resp.status_code == 202
    assert resp.json()["status"] == "staged"
