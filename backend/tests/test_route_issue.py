import importlib
from unittest.mock import AsyncMock, patch

import pytest

import main as main_module


@pytest.fixture(autouse=True)
def _restore_main_module_state():
    """See identical fixture in test_billing_forward.py: reloading `main` after
    monkeypatching FLEET_PLATE_MODE mutates the shared module's globals, so it
    must be reloaded again after monkeypatch reverts the env var, or later
    tests (in this file or others) see a stale FLEET_PLATE_MODE value."""
    yield
    importlib.reload(main_module)


async def test_route_issue_delegates_to_llm_router_when_fleet_mode_off(monkeypatch):
    monkeypatch.setenv("FLEET_PLATE_MODE", "false")
    importlib.reload(main_module)
    issue = {"message_snippet": "brakes grinding on KMGQ 947Z"}
    open_tickets = [{"id": 1, "category": "brakes", "message_body": "old", "vehicle_plate": "KMGQ947Z"}]
    with patch("main.classify_update_or_new", new=AsyncMock(return_value={"routing": "update", "ticket_id": 1})):
        result = await main_module._route_issue(issue, issue["message_snippet"], open_tickets)
    assert result == {"routing": "update", "ticket_id": 1, "vehicle_plate": None}


async def test_route_issue_skips_llm_call_when_no_open_tickets_and_fleet_mode_off(monkeypatch):
    monkeypatch.setenv("FLEET_PLATE_MODE", "false")
    importlib.reload(main_module)
    issue = {"message_snippet": "brakes grinding"}
    with patch("main.classify_update_or_new", new=AsyncMock()) as mock_router:
        result = await main_module._route_issue(issue, issue["message_snippet"], [])
    mock_router.assert_not_called()
    assert result == {"routing": "new", "vehicle_plate": None}


async def test_route_issue_threads_matching_plate_to_open_ticket(monkeypatch):
    monkeypatch.setenv("FLEET_PLATE_MODE", "true")
    importlib.reload(main_module)
    issue = {"message_snippet": "brakes grinding on KMGQ 947Z"}
    open_tickets = [{"id": 5, "category": "brakes", "message_body": "old", "vehicle_plate": "KMGQ947Z"}]
    with patch("main.classify_update_or_new", new=AsyncMock()) as mock_router:
        result = await main_module._route_issue(issue, issue["message_snippet"], open_tickets)
    mock_router.assert_not_called()
    assert result == {"routing": "update", "ticket_id": 5, "vehicle_plate": "KMGQ947Z"}


async def test_route_issue_creates_new_when_plate_resolves_but_no_match(monkeypatch):
    monkeypatch.setenv("FLEET_PLATE_MODE", "true")
    importlib.reload(main_module)
    issue = {"message_snippet": "brakes grinding on KMGQ 947Z"}
    open_tickets = [{"id": 5, "category": "brakes", "message_body": "old", "vehicle_plate": "KZZT501M"}]
    result = await main_module._route_issue(issue, issue["message_snippet"], open_tickets)
    assert result == {"routing": "new", "vehicle_plate": "KMGQ947Z"}


async def test_route_issue_always_new_when_no_plate_resolved(monkeypatch):
    monkeypatch.setenv("FLEET_PLATE_MODE", "true")
    importlib.reload(main_module)
    issue = {"message_snippet": "brakes grinding, no plate mentioned"}
    open_tickets = [{"id": 5, "category": "brakes", "message_body": "old", "vehicle_plate": "KZZT501M"}]
    with patch("main.classify_update_or_new", new=AsyncMock()) as mock_router:
        result = await main_module._route_issue(issue, issue["message_snippet"], open_tickets)
    mock_router.assert_not_called()
    assert result == {"routing": "new", "vehicle_plate": None}


async def test_route_issue_always_new_when_lead_mode_on(monkeypatch):
    monkeypatch.setenv("LEAD_MODE", "true")
    importlib.reload(main_module)
    issue = {"message_snippet": "looking for a 2br apartment for rent"}
    open_tickets = [{"id": 1, "category": "apartment", "message_body": "old lead", "vehicle_plate": None}]
    with patch("main.classify_update_or_new", new=AsyncMock()) as mock_router:
        result = await main_module._route_issue(issue, issue["message_snippet"], open_tickets)
    mock_router.assert_not_called()
    assert result == {"routing": "new", "vehicle_plate": None}


async def test_route_issue_lead_mode_ignores_fleet_plate_mode(monkeypatch):
    monkeypatch.setenv("LEAD_MODE", "true")
    monkeypatch.setenv("FLEET_PLATE_MODE", "true")
    importlib.reload(main_module)
    issue = {"message_snippet": "looking for a plot, ref KMGQ947Z"}
    with patch("main.classify_update_or_new", new=AsyncMock()) as mock_router:
        result = await main_module._route_issue(issue, issue["message_snippet"], [])
    mock_router.assert_not_called()
    assert result == {"routing": "new", "vehicle_plate": None}
