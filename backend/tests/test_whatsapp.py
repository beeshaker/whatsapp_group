from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from whatsapp import send_group_message


async def test_send_group_message_returns_message_id():
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"messageId": "wa-msg-123"}
    with patch("whatsapp.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.post = AsyncMock(return_value=mock_resp)
        result = await send_group_message("120363@g.us", "Hello group")
    assert result == "wa-msg-123"


async def test_send_group_message_posts_to_correct_endpoint():
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"messageId": "wa-msg-456"}
    with patch("whatsapp.httpx.AsyncClient") as mock_client:
        mock_post = AsyncMock(return_value=mock_resp)
        mock_client.return_value.__aenter__.return_value.post = mock_post
        await send_group_message("999@g.us", "Test message")
    call_args = mock_post.call_args
    assert "messages/text" in call_args[0][0]
    assert call_args[1]["json"]["chatId"] == "999@g.us"
    assert call_args[1]["json"]["text"] == "Test message"


async def test_send_group_message_raises_on_http_error():
    with patch("whatsapp.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.post = AsyncMock(
            side_effect=Exception("connection refused")
        )
        with pytest.raises(Exception, match="connection refused"):
            await send_group_message("120363@g.us", "Test")
