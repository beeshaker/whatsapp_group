from unittest.mock import AsyncMock, patch


async def test_post_chat_returns_reply(client):
    with patch("main.answer_query", new=AsyncMock(return_value="2 open incidents")):
        resp = await client.post("/api/chat", json={"message": "How many open?"})
    assert resp.status_code == 200
    assert resp.json()["reply"] == "2 open incidents"


async def test_post_chat_requires_auth():
    # Use a raw httpx client without session
    from httpx import AsyncClient, ASGITransport
    from main import app
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        resp = await ac.post("/api/chat", json={"message": "test"})
    assert resp.status_code in (302, 401, 403)
