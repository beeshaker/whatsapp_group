from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

import whatsapp
from whatsapp import send_group_message, reply_to_message, list_groups

_SESSIONS_RESP = [{"id": "uuid-abc", "name": "opsgateway", "status": "ready"}]
_GROUPS_RESP = [{"id": "111@g.us", "name": "Support Group"}, {"id": "222@g.us", "name": "Ops Group"}]


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


def _mock_get_client(get_side_effect):
    """Build a mock AsyncClient context manager with a configurable GET side_effect list."""
    inner = MagicMock()
    inner.get = AsyncMock(side_effect=get_side_effect)

    ctx = MagicMock()
    ctx.__aenter__ = AsyncMock(return_value=inner)
    ctx.__aexit__ = AsyncMock(return_value=False)
    return ctx, inner


def _sessions_resp():
    resp = MagicMock()
    resp.raise_for_status = MagicMock()
    resp.json.return_value = _SESSIONS_RESP
    return resp


async def test_list_groups_returns_parsed_list_on_success():
    groups_resp = MagicMock()
    groups_resp.raise_for_status = MagicMock()
    groups_resp.json.return_value = _GROUPS_RESP

    ctx, inner = _mock_get_client([_sessions_resp(), groups_resp])
    with patch("whatsapp.httpx.AsyncClient", return_value=ctx):
        whatsapp._session_uuid = None
        result = await list_groups()

    assert result == _GROUPS_RESP
    groups_call = inner.get.call_args_list[1]
    assert "uuid-abc/groups" in groups_call[0][0]


async def test_list_groups_returns_none_on_http_error():
    ctx, inner = _mock_get_client([_sessions_resp(), Exception("connection refused")])
    with patch("whatsapp.httpx.AsyncClient", return_value=ctx):
        whatsapp._session_uuid = None
        result = await list_groups()
    assert result is None


async def test_list_groups_returns_none_on_timeout():
    ctx, inner = _mock_get_client([_sessions_resp(), httpx.TimeoutException("timed out")])
    with patch("whatsapp.httpx.AsyncClient", return_value=ctx):
        whatsapp._session_uuid = None
        result = await list_groups()
    assert result is None


async def test_list_groups_returns_none_when_session_not_found():
    not_found_resp = MagicMock()
    not_found_resp.raise_for_status = MagicMock()
    not_found_resp.json.return_value = [{"id": "x", "name": "some-other-session"}]

    ctx, inner = _mock_get_client([not_found_resp])
    with patch("whatsapp.httpx.AsyncClient", return_value=ctx):
        whatsapp._session_uuid = None
        result = await list_groups()
    assert result is None
