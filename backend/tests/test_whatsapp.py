from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import whatsapp
from whatsapp import send_group_message, reply_to_message

_SESSIONS_RESP = [{"id": "uuid-abc", "name": "opsgateway", "status": "ready"}]


def _mock_client(post_return=None, post_side_effect=None):
    """Build a mock AsyncClient context manager with GET sessions + POST configured."""
    mock_get_resp = MagicMock()
    mock_get_resp.raise_for_status = MagicMock()
    mock_get_resp.json.return_value = _SESSIONS_RESP

    mock_post_resp = MagicMock()
    mock_post_resp.raise_for_status = MagicMock()
    if post_return:
        mock_post_resp.json.return_value = post_return
        mock_post_resp.status_code = 201

    inner = MagicMock()
    inner.get = AsyncMock(return_value=mock_get_resp)
    if post_side_effect:
        inner.post = AsyncMock(side_effect=post_side_effect)
    else:
        inner.post = AsyncMock(return_value=mock_post_resp)

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=inner)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx, inner


async def test_send_group_message_returns_message_id():
    ctx, inner = _mock_client(post_return={"messageId": "wa-msg-123"})
    with patch("whatsapp.httpx.AsyncClient", return_value=ctx):
        whatsapp._session_uuid = None
        result = await send_group_message("120363@g.us", "Hello group")
    assert result == "wa-msg-123"


async def test_send_group_message_posts_to_correct_endpoint():
    ctx, inner = _mock_client(post_return={"messageId": "wa-msg-456"})
    with patch("whatsapp.httpx.AsyncClient", return_value=ctx):
        whatsapp._session_uuid = None
        await send_group_message("999@g.us", "Test message")
    call_args = inner.post.call_args
    assert "messages/send-text" in call_args[0][0]
    assert call_args[1]["json"]["chatId"] == "999@g.us"
    assert call_args[1]["json"]["text"] == "Test message"


async def test_send_group_message_raises_on_http_error():
    ctx, inner = _mock_client(post_side_effect=Exception("connection refused"))
    with patch("whatsapp.httpx.AsyncClient", return_value=ctx):
        whatsapp._session_uuid = None
        with pytest.raises(Exception, match="connection refused"):
            await send_group_message("120363@g.us", "Test")
