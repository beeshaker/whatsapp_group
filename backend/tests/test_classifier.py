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


def _mock_response(mock_client, response_text):
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"response": response_text}
    mock_client.return_value.__aenter__.return_value.post = AsyncMock(return_value=mock_resp)


async def test_empty_array_means_noise():
    mock_db = _make_mock_db()
    with patch("classifier.httpx.AsyncClient") as mock_client:
        _mock_response(mock_client, "[]")
        result = await classify_message("Good morning everyone!", mock_db)
    assert result == {"issues": []}


async def test_single_issue_parsed():
    mock_db = _make_mock_db()
    with patch("classifier.httpx.AsyncClient") as mock_client:
        _mock_response(mock_client, '''
            [{"message_snippet": "The water pump is leaking badly on floor 3",
              "category": "plumbing", "priority": "high", "confidence": 0.92}]
        ''')
        result = await classify_message("The water pump is leaking badly on floor 3", mock_db)
    assert len(result["issues"]) == 1
    issue = result["issues"][0]
    assert issue["category"] == "plumbing"
    assert issue["priority"] == "high"
    assert issue["confidence"] == 0.92
    assert issue["message_snippet"] == "The water pump is leaking badly on floor 3"


async def test_multi_issue_parsed_in_order():
    mock_db = _make_mock_db()
    with patch("classifier.httpx.AsyncClient") as mock_client:
        _mock_response(mock_client, '''
            [{"message_snippet": "pump leaking", "category": "plumbing", "priority": "high", "confidence": 0.9},
             {"message_snippet": "lift stuck on floor 3", "category": "lift", "priority": "urgent", "confidence": 0.95},
             {"message_snippet": "broken gate", "category": "security", "priority": "medium", "confidence": 0.7}]
        ''')
        result = await classify_message("1. pump leaking 2. lift stuck on floor 3 3. broken gate", mock_db)
    assert len(result["issues"]) == 3
    assert [i["category"] for i in result["issues"]] == ["plumbing", "lift", "security"]


async def test_caps_to_top_5_by_confidence_preserving_order():
    mock_db = _make_mock_db()
    confidences = [0.5, 0.9, 0.3, 0.95, 0.6, 0.99, 0.2]
    items = [
        {"message_snippet": f"issue {n}", "category": "plumbing", "priority": "low", "confidence": c}
        for n, c in enumerate(confidences)
    ]
    import json as _json
    with patch("classifier.httpx.AsyncClient") as mock_client:
        _mock_response(mock_client, _json.dumps(items))
        result = await classify_message("seven issues in one message", mock_db)
    assert len(result["issues"]) == 5
    # The two lowest confidences (0.3 at index 2, 0.2 at index 6) are dropped;
    # the remaining five keep their original relative order (not re-sorted).
    assert [i["confidence"] for i in result["issues"]] == [0.5, 0.9, 0.95, 0.6, 0.99]


async def test_per_issue_category_fallback_to_other():
    mock_db = _make_mock_db()
    with patch("classifier.httpx.AsyncClient") as mock_client:
        _mock_response(mock_client, '[{"message_snippet": "weird thing", "category": "magic", "priority": "high", "confidence": 0.9}]')
        result = await classify_message("Something weird", mock_db)
    assert result["issues"][0]["category"] == "other"


async def test_per_issue_priority_fallback_to_medium():
    mock_db = _make_mock_db()
    with patch("classifier.httpx.AsyncClient") as mock_client:
        _mock_response(mock_client, '[{"message_snippet": "urgent-ish", "category": "plumbing", "priority": "critical", "confidence": 0.9}]')
        result = await classify_message("Something urgent-ish", mock_db)
    assert result["issues"][0]["priority"] == "medium"


async def test_urgent_priority_is_accepted():
    mock_db = _make_mock_db()
    with patch("classifier.httpx.AsyncClient") as mock_client:
        _mock_response(mock_client, '[{"message_snippet": "flooding", "category": "plumbing", "priority": "urgent", "confidence": 0.95}]')
        result = await classify_message("Pipe burst, flooding the lobby", mock_db)
    assert result["issues"][0]["priority"] == "urgent"


async def test_returns_fallback_on_timeout():
    mock_db = _make_mock_db()
    with patch("classifier.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.post = AsyncMock(
            side_effect=Exception("connection timeout")
        )
        result = await classify_message("hello everyone", mock_db)
    assert result == {"issues": []}


async def test_returns_fallback_on_malformed_json():
    mock_db = _make_mock_db()
    with patch("classifier.httpx.AsyncClient") as mock_client:
        _mock_response(mock_client, "sorry I cannot help with {{{")
        result = await classify_message("some message", mock_db)
    assert result == {"issues": []}


async def test_returns_fallback_when_response_is_not_a_json_array():
    mock_db = _make_mock_db()
    with patch("classifier.httpx.AsyncClient") as mock_client:
        _mock_response(mock_client, '{"message_snippet": "x", "category": "plumbing", "priority": "high", "confidence": 0.9}')
        result = await classify_message("some message", mock_db)
    assert result == {"issues": []}


async def test_returns_fallback_on_db_error():
    mock_db = AsyncMock()
    mock_db.execute = AsyncMock(side_effect=Exception("DB connection lost"))
    result = await classify_message("Pump leaking", mock_db)
    assert result == {"issues": []}


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
