from unittest.mock import AsyncMock, MagicMock, patch
from classifier import classify_message


async def test_classifies_incident_correctly():
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "response": '{"is_incident": true, "category": "plumbing", "severity": "high", "confidence": 0.92}'
    }
    with patch("classifier.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.post = AsyncMock(return_value=mock_resp)
        result = await classify_message("The water pump is leaking badly on floor 3")

    assert result["is_incident"] is True
    assert result["category"] == "plumbing"
    assert result["severity"] == "high"
    assert result["confidence"] == 0.92


async def test_returns_fallback_on_timeout():
    with patch("classifier.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.post = AsyncMock(
            side_effect=Exception("connection timeout")
        )
        result = await classify_message("hello everyone")

    assert result["is_incident"] is False
    assert result["confidence"] == 0.0


async def test_returns_fallback_on_malformed_json():
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"response": "sorry I cannot help with {{{"}
    with patch("classifier.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.post = AsyncMock(return_value=mock_resp)
        result = await classify_message("some message")

    assert result["is_incident"] is False
    assert result["confidence"] == 0.0


async def test_classifies_noise_as_non_incident():
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "response": '{"is_incident": false, "category": "other", "severity": "low", "confidence": 0.95}'
    }
    with patch("classifier.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.post = AsyncMock(return_value=mock_resp)
        result = await classify_message("Good morning everyone!")

    assert result["is_incident"] is False
