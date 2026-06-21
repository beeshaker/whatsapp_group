from unittest.mock import AsyncMock, MagicMock, patch
from classifier import classify_message, classify_update_or_new


def _make_mock_db():
    """Returns an AsyncMock db session that yields the 8 default categories."""
    mock_db = AsyncMock()
    mock_rows = [
        MagicMock(slug=s)
        for s in ["plumbing", "electrical", "lift", "security", "structural", "cleaning", "access", "other"]
    ]
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = mock_rows
    mock_db.execute = AsyncMock(return_value=mock_result)
    return mock_db


async def test_classifies_incident_correctly():
    mock_db = _make_mock_db()
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "response": '{"is_incident": true, "category": "plumbing", "severity": "high", "confidence": 0.92}'
    }
    with patch("classifier.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.post = AsyncMock(return_value=mock_resp)
        result = await classify_message("The water pump is leaking badly on floor 3", mock_db)

    assert result["is_incident"] is True
    assert result["category"] == "plumbing"
    assert result["severity"] == "high"
    assert result["confidence"] == 0.92


async def test_returns_fallback_on_timeout():
    mock_db = _make_mock_db()
    with patch("classifier.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.post = AsyncMock(
            side_effect=Exception("connection timeout")
        )
        result = await classify_message("hello everyone", mock_db)

    assert result["is_incident"] is False
    assert result["confidence"] == 0.0


async def test_returns_fallback_on_malformed_json():
    mock_db = _make_mock_db()
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"response": "sorry I cannot help with {{{"}
    with patch("classifier.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.post = AsyncMock(return_value=mock_resp)
        result = await classify_message("some message", mock_db)

    assert result["is_incident"] is False
    assert result["confidence"] == 0.0


async def test_classifies_noise_as_non_incident():
    mock_db = _make_mock_db()
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "response": '{"is_incident": false, "category": "other", "severity": "low", "confidence": 0.95}'
    }
    with patch("classifier.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.post = AsyncMock(return_value=mock_resp)
        result = await classify_message("Good morning everyone!", mock_db)

    assert result["is_incident"] is False


async def test_unknown_category_falls_back_to_other():
    mock_db = _make_mock_db()
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "response": '{"is_incident": true, "category": "magic", "severity": "high", "confidence": 0.9}'
    }
    with patch("classifier.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.post = AsyncMock(return_value=mock_resp)
        result = await classify_message("Something weird", mock_db)

    assert result["category"] == "other"


async def test_classify_update_or_new_returns_new_when_no_open_tickets():
    result = await classify_update_or_new("More water leaking", [])
    assert result == {"routing": "new"}


async def test_classify_update_or_new_llm_says_new():
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"response": '{"routing": "new"}'}
    open_tickets = [{"id": 1, "category": "plumbing", "message_body": "Pump leaking floor 3"}]
    with patch("classifier.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.post = AsyncMock(return_value=mock_resp)
        result = await classify_update_or_new("Broken window in lobby", open_tickets)
    assert result == {"routing": "new"}


async def test_classify_update_or_new_llm_says_update():
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"response": '{"routing": "update", "ticket_id": 1}'}
    open_tickets = [{"id": 1, "category": "plumbing", "message_body": "Pump leaking floor 3"}]
    with patch("classifier.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.post = AsyncMock(return_value=mock_resp)
        result = await classify_update_or_new("Still leaking, getting worse", open_tickets)
    assert result == {"routing": "update", "ticket_id": 1}


async def test_classify_update_or_new_rejects_invalid_ticket_id():
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"response": '{"routing": "update", "ticket_id": 999}'}
    open_tickets = [{"id": 1, "category": "plumbing", "message_body": "Pump leaking floor 3"}]
    with patch("classifier.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.post = AsyncMock(return_value=mock_resp)
        result = await classify_update_or_new("Still leaking, getting worse", open_tickets)
    assert result == {"routing": "new"}


async def test_classify_update_or_new_falls_back_on_llm_failure():
    open_tickets = [{"id": 1, "category": "plumbing", "message_body": "Pump leaking floor 3"}]
    with patch("classifier.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.post = AsyncMock(
            side_effect=Exception("timeout")
        )
        result = await classify_update_or_new("Still leaking, getting worse", open_tickets)
    assert result == {"routing": "new"}
