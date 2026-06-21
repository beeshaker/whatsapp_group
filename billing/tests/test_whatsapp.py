import pytest
from datetime import date, datetime, timezone
from unittest.mock import AsyncMock, patch
from models import Client


def _mock_client() -> Client:
    c = Client()
    c.openwa_url = "http://localhost:2001"
    c.openwa_session = "acme"
    c.openwa_api_key = "test-key"
    c.whatsapp_group_id = "1234567890@g.us"
    c.renewal_date = date.today()
    c.created_at = datetime.now(timezone.utc)
    return c


@pytest.mark.asyncio
async def test_send_to_group_posts_to_openwa():
    from whatsapp import send_to_group

    sessions_resp = AsyncMock()
    sessions_resp.json = lambda: [{"name": "acme", "id": "sess-uuid-1"}]
    sessions_resp.raise_for_status = lambda: None

    send_resp = AsyncMock()
    send_resp.raise_for_status = lambda: None

    with patch("httpx.AsyncClient") as MockClient:
        instance = AsyncMock()
        instance.__aenter__ = AsyncMock(return_value=instance)
        instance.__aexit__ = AsyncMock(return_value=False)
        instance.get = AsyncMock(return_value=sessions_resp)
        instance.post = AsyncMock(return_value=send_resp)
        MockClient.return_value = instance

        await send_to_group(_mock_client(), "Hello from billing")

    instance.post.assert_called_once()
    url_called = instance.post.call_args[0][0]
    assert "send-text" in url_called
    payload = instance.post.call_args[1]["json"]
    assert payload["text"] == "Hello from billing"
    assert payload["chatId"] == "1234567890@g.us"


@pytest.mark.asyncio
async def test_send_to_group_skips_when_no_openwa_url():
    from whatsapp import send_to_group

    c = _mock_client()
    c.openwa_url = None

    with patch("httpx.AsyncClient") as MockClient:
        await send_to_group(c, "This should not be sent")
    MockClient.assert_not_called()
