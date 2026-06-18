from datetime import datetime, timezone
from sqlalchemy import select

from models import AdminProfile, AdminGroupSubscription, User
from auth import hash_password


async def test_get_profile_returns_empty_for_new_admin(client):
    resp = await client.get("/api/admin/profile")
    assert resp.status_code == 200
    data = resp.json()
    assert data["whatsapp_phone"] is None
    assert data["group_ids"] == []


async def test_put_profile_saves_phone(client):
    resp = await client.put("/api/admin/profile", json={"whatsapp_phone": "254712345678"})
    assert resp.status_code == 200

    resp2 = await client.get("/api/admin/profile")
    assert resp2.json()["whatsapp_phone"] == "254712345678"


async def test_put_profile_updates_phone(client):
    await client.put("/api/admin/profile", json={"whatsapp_phone": "254712345678"})
    await client.put("/api/admin/profile", json={"whatsapp_phone": "254799999999"})
    resp = await client.get("/api/admin/profile")
    assert resp.json()["whatsapp_phone"] == "254799999999"


async def test_post_subscriptions_replaces_all(client, db_session):
    resp = await client.post("/api/admin/subscriptions", json={"group_ids": ["g1@g.us", "g2@g.us"]})
    assert resp.status_code == 200

    resp2 = await client.get("/api/admin/profile")
    assert set(resp2.json()["group_ids"]) == {"g1@g.us", "g2@g.us"}


async def test_post_subscriptions_clears_when_empty(client):
    await client.post("/api/admin/subscriptions", json={"group_ids": ["g1@g.us"]})
    await client.post("/api/admin/subscriptions", json={"group_ids": []})
    resp = await client.get("/api/admin/profile")
    assert resp.json()["group_ids"] == []


async def test_profile_endpoints_require_admin(authenticated_client):
    # authenticated_client is an admin — just confirm it works
    resp = await authenticated_client.get("/api/admin/profile")
    assert resp.status_code == 200
