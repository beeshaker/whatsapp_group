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


def test_build_prompt_defaults_to_property_management_context():
    import classifier
    prompt = classifier._build_prompt("test message", ["plumbing", "other"])
    assert "property management company" in prompt


def test_build_prompt_uses_classifier_context_env_override(monkeypatch):
    import importlib
    import classifier
    monkeypatch.setenv("CLASSIFIER_CONTEXT", "You are classifying messages about bike fleet repairs.")
    importlib.reload(classifier)
    try:
        prompt = classifier._build_prompt("test message", ["brakes", "other"])
        assert "bike fleet repairs" in prompt
        assert "property management company" not in prompt
    finally:
        importlib.reload(classifier)


import importlib
import pytest
import classifier as classifier_module


@pytest.fixture(autouse=True)
def _restore_classifier_module_state():
    yield
    importlib.reload(classifier_module)


def _make_mock_lead_db():
    mock_db = AsyncMock()
    mock_rows = [MagicMock(slug=s) for s in ["apartment", "house", "godown", "commercial", "plot", "land", "other"]]
    mock_result = MagicMock()
    mock_result.scalars.return_value.all.return_value = mock_rows
    mock_db.execute = AsyncMock(return_value=mock_result)
    return mock_db


async def test_lead_mode_empty_array_means_noise(monkeypatch):
    monkeypatch.setenv("LEAD_MODE", "true")
    importlib.reload(classifier_module)
    mock_db = _make_mock_lead_db()
    with patch("classifier.httpx.AsyncClient") as mock_client:
        _mock_response(mock_client, "[]")
        result = await classifier_module.classify_message("Good morning team!", mock_db)
    assert result == {"issues": []}


async def test_lead_mode_extracts_all_fields_from_real_sample_message(monkeypatch):
    monkeypatch.setenv("LEAD_MODE", "true")
    importlib.reload(classifier_module)
    mock_db = _make_mock_lead_db()
    body = "@~Jabeen kindly contact Samson 0746823554, looking for a 4br along for rent General mathege , budget 3000usd (Website Enquiry)"
    with patch("classifier.httpx.AsyncClient") as mock_client:
        _mock_response(mock_client, '''
            [{"message_snippet": "looking for a 4br along for rent General mathege, budget 3000usd",
              "category": "apartment", "contact_name": "Samson", "lead_location": "General Mathenge",
              "lead_budget": "3000usd", "transaction_type": "rent", "confidence": 0.91}]
        ''')
        result = await classifier_module.classify_message(body, mock_db)
    assert len(result["issues"]) == 1
    issue = result["issues"][0]
    assert issue["category"] == "apartment"
    assert issue["priority"] == "low"
    assert issue["contact_name"] == "Samson"
    assert issue["lead_location"] == "General Mathenge"
    assert issue["lead_budget"] == "3000usd"
    assert issue["transaction_type"] == "rent"
    assert issue["contact_phone"] == "254746823554"
    assert issue["lead_agent"] == "Jabeen"
    assert issue["lead_source"] == "Website Enquiry"


async def test_lead_mode_unrecognized_category_falls_back_to_other(monkeypatch):
    monkeypatch.setenv("LEAD_MODE", "true")
    importlib.reload(classifier_module)
    mock_db = _make_mock_lead_db()
    body = "@~Victoria kindly contact Mercy on 0784549538 looking for a yacht (Website Enquiry)"
    with patch("classifier.httpx.AsyncClient") as mock_client:
        _mock_response(mock_client, '''
            [{"message_snippet": "looking for a yacht", "category": "marine",
              "contact_name": "Mercy", "lead_location": "", "lead_budget": "",
              "transaction_type": "unknown", "confidence": 0.6}]
        ''')
        result = await classifier_module.classify_message(body, mock_db)
    assert result["issues"][0]["category"] == "other"


async def test_lead_mode_regex_phone_overrides_llm_proposed_phone(monkeypatch):
    monkeypatch.setenv("LEAD_MODE", "true")
    importlib.reload(classifier_module)
    mock_db = _make_mock_lead_db()
    body = "@~Harsha kindly contact Sarah at 0718449483 for a plot (Board Enquiry)"
    with patch("classifier.httpx.AsyncClient") as mock_client:
        _mock_response(mock_client, '''
            [{"message_snippet": "for a plot", "category": "plot", "contact_name": "Sarah",
              "lead_location": "", "lead_budget": "", "transaction_type": "sale",
              "confidence": 0.8, "contact_phone": "0700000000"}]
        ''')
        result = await classifier_module.classify_message(body, mock_db)
    assert result["issues"][0]["contact_phone"] == "254718449483"


async def test_lead_mode_regex_contact_name_overrides_llm_proposed_name(monkeypatch):
    monkeypatch.setenv("LEAD_MODE", "true")
    importlib.reload(classifier_module)
    mock_db = _make_mock_lead_db()
    body = "@~Peter kindly contact Victoria 0700111222 for a house (Website)"
    with patch("classifier.httpx.AsyncClient") as mock_client:
        _mock_response(mock_client, '''
            [{"message_snippet": "for a house", "category": "house", "contact_name": "WrongName",
              "lead_location": "", "lead_budget": "", "transaction_type": "unknown", "confidence": 0.8}]
        ''')
        result = await classifier_module.classify_message(body, mock_db)
    assert result["issues"][0]["contact_name"] == "Victoria"


async def test_lead_mode_llm_contact_name_used_when_regex_finds_none(monkeypatch):
    monkeypatch.setenv("LEAD_MODE", "true")
    importlib.reload(classifier_module)
    mock_db = _make_mock_lead_db()
    body = "@~Peter budget 5M for a house near Kilimani (Website)"
    with patch("classifier.httpx.AsyncClient") as mock_client:
        _mock_response(mock_client, '''
            [{"message_snippet": "budget 5M for a house near Kilimani", "category": "house",
              "contact_name": "Grace", "lead_location": "Kilimani", "lead_budget": "5M",
              "transaction_type": "unknown", "confidence": 0.75}]
        ''')
        result = await classifier_module.classify_message(body, mock_db)
    assert result["issues"][0]["contact_name"] == "Grace"


async def test_lead_mode_multi_issue_message_does_not_leak_contact_name_across_issues(monkeypatch):
    monkeypatch.setenv("LEAD_MODE", "true")
    importlib.reload(classifier_module)
    mock_db = _make_mock_lead_db()
    body = "@~Alice kindly contact Sam 0746823554 for a 2br rent. @~Bob has a plot buyer 0722516801 for a plot sale (Website Enquiry)"
    with patch("classifier.httpx.AsyncClient") as mock_client:
        _mock_response(mock_client, '''
            [{"message_snippet": "@~Alice kindly contact Sam 0746823554 for a 2br rent", "category": "apartment",
              "contact_name": "Sam", "lead_location": "", "lead_budget": "", "transaction_type": "rent", "confidence": 0.85},
             {"message_snippet": "@~Bob has a plot buyer 0722516801 for a plot sale", "category": "plot",
              "contact_name": "Jo", "lead_location": "", "lead_budget": "", "transaction_type": "sale", "confidence": 0.85}]
        ''')
        result = await classifier_module.classify_message(body, mock_db)
    assert result["issues"][0]["contact_name"] == "Sam"
    assert result["issues"][1]["contact_name"] == "Jo"


async def test_lead_mode_invalid_transaction_type_falls_back_to_unknown(monkeypatch):
    monkeypatch.setenv("LEAD_MODE", "true")
    importlib.reload(classifier_module)
    mock_db = _make_mock_lead_db()
    body = "@~Peter kindly contact Joy on 0722516801 for a house (Website)"
    with patch("classifier.httpx.AsyncClient") as mock_client:
        _mock_response(mock_client, '''
            [{"message_snippet": "for a house", "category": "house", "contact_name": "Joy",
              "lead_location": "", "lead_budget": "", "transaction_type": "swap",
              "confidence": 0.8}]
        ''')
        result = await classifier_module.classify_message(body, mock_db)
    assert result["issues"][0]["transaction_type"] == "unknown"


async def test_lead_mode_null_json_fields_become_none_not_string(monkeypatch):
    monkeypatch.setenv("LEAD_MODE", "true")
    importlib.reload(classifier_module)
    mock_db = _make_mock_lead_db()
    body = "@~Peter kindly contact someone for a house, no other details (Website)"
    with patch("classifier.httpx.AsyncClient") as mock_client:
        _mock_response(mock_client, '''
            [{"message_snippet": "for a house", "category": "house", "contact_name": null,
              "lead_location": null, "lead_budget": null, "transaction_type": "unknown",
              "confidence": 0.7}]
        ''')
        result = await classifier_module.classify_message(body, mock_db)
    issue = result["issues"][0]
    assert issue["contact_name"] is None
    assert issue["lead_location"] is None
    assert issue["lead_budget"] is None


async def test_lead_mode_null_confidence_defaults_to_zero_not_dropped(monkeypatch):
    monkeypatch.setenv("LEAD_MODE", "true")
    importlib.reload(classifier_module)
    mock_db = _make_mock_lead_db()
    body = "@~Peter kindly contact Joy on 0722516801 for a house (Website)"
    with patch("classifier.httpx.AsyncClient") as mock_client:
        _mock_response(mock_client, '''
            [{"message_snippet": "for a house", "category": "house", "contact_name": "Joy",
              "lead_location": "", "lead_budget": "", "transaction_type": "unknown",
              "confidence": null}]
        ''')
        result = await classifier_module.classify_message(body, mock_db)
    assert len(result["issues"]) == 1
    assert result["issues"][0]["confidence"] == 0.0


async def test_lead_mode_multi_agent_message_attributes_correctly_when_snippet_has_tag(monkeypatch):
    monkeypatch.setenv("LEAD_MODE", "true")
    importlib.reload(classifier_module)
    mock_db = _make_mock_lead_db()
    body = "@~Alice kindly contact Sam 0746823554 for a 2br rent. @~Bob kindly contact Jo 0722516801 for a plot sale (Website Enquiry)"
    with patch("classifier.httpx.AsyncClient") as mock_client:
        _mock_response(mock_client, '''
            [{"message_snippet": "@~Alice kindly contact Sam 0746823554 for a 2br rent", "category": "apartment",
              "contact_name": "Sam", "lead_location": "", "lead_budget": "", "transaction_type": "rent", "confidence": 0.85},
             {"message_snippet": "@~Bob kindly contact Jo 0722516801 for a plot sale", "category": "plot",
              "contact_name": "Jo", "lead_location": "", "lead_budget": "", "transaction_type": "sale", "confidence": 0.85}]
        ''')
        result = await classifier_module.classify_message(body, mock_db)
    assert result["issues"][0]["lead_agent"] == "Alice"
    assert result["issues"][1]["lead_agent"] == "Bob"


async def test_lead_mode_off_uses_original_maintenance_classifier(monkeypatch):
    monkeypatch.setenv("LEAD_MODE", "false")
    importlib.reload(classifier_module)
    mock_db = _make_mock_db()
    with patch("classifier.httpx.AsyncClient") as mock_client:
        _mock_response(mock_client, '''
            [{"message_snippet": "pump leaking", "category": "plumbing", "priority": "high", "confidence": 0.9}]
        ''')
        result = await classifier_module.classify_message("pump leaking", mock_db)
    assert result["issues"][0]["category"] == "plumbing"
    assert "contact_phone" not in result["issues"][0]
