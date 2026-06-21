import pytest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch, MagicMock
from models import Client


def _client(status="active", renewal_date=None, grace_started_at=None, warning_sent_at=None):
    c = Client()
    c.id = 1
    c.name = "Test"
    c.subdomain = "test"
    c.plan = "monthly"
    c.status = status
    c.renewal_date = renewal_date or date.today()
    c.grace_started_at = grace_started_at
    c.warning_sent_at = warning_sent_at
    c.whatsapp_group_id = "group@g.us"
    c.openwa_url = "http://localhost:2001"
    c.openwa_session = "test"
    c.openwa_api_key = "key"
    c.docker_project = "test"
    c.created_at = datetime.now(timezone.utc)
    return c


@pytest.mark.asyncio
async def test_3day_pre_renewal_reminder():
    from scheduler import _check_client_status
    c = _client(status="active", renewal_date=date.today() + timedelta(days=3))
    with patch("scheduler.send_to_group", new=AsyncMock()) as mock_send:
        await _check_client_status(c, AsyncMock())
    assert c.status == "active"
    mock_send.assert_called_once()
    assert "renew" in mock_send.call_args[0][1].lower() or "renewal" in mock_send.call_args[0][1].lower()


@pytest.mark.asyncio
async def test_active_transitions_to_grace_when_expired():
    from scheduler import _check_client_status
    c = _client(status="active", renewal_date=date.today() - timedelta(days=1))
    mock_db = AsyncMock()
    with patch("scheduler.send_to_group", new=AsyncMock()) as mock_send:
        await _check_client_status(c, mock_db)
    assert c.status == "grace"
    assert c.grace_started_at is not None
    mock_send.assert_called_once()
    assert "expired" in mock_send.call_args[0][1].lower()
    mock_db.commit.assert_called_once()


@pytest.mark.asyncio
async def test_grace_sends_daily_reminder_before_3_days():
    from scheduler import _check_client_status
    now = datetime.now(timezone.utc)
    c = _client(
        status="grace",
        renewal_date=date.today() - timedelta(days=2),
        grace_started_at=now - timedelta(hours=25),
    )
    with patch("scheduler.send_to_group", new=AsyncMock()) as mock_send:
        await _check_client_status(c, AsyncMock())
    assert c.status == "grace"
    mock_send.assert_called_once()


@pytest.mark.asyncio
async def test_grace_transitions_to_warning_after_3_days():
    from scheduler import _check_client_status
    now = datetime.now(timezone.utc)
    c = _client(
        status="grace",
        renewal_date=date.today() - timedelta(days=4),
        grace_started_at=now - timedelta(days=4),
    )
    mock_db = AsyncMock()
    with patch("scheduler.send_to_group", new=AsyncMock()) as mock_send:
        await _check_client_status(c, mock_db)
    assert c.status == "warning"
    assert c.warning_sent_at is not None
    assert "24 hours" in mock_send.call_args[0][1]
    mock_db.commit.assert_called_once()


@pytest.mark.asyncio
async def test_warning_suspends_after_24h():
    from scheduler import _check_client_status
    now = datetime.now(timezone.utc)
    c = _client(
        status="warning",
        renewal_date=date.today() - timedelta(days=5),
        grace_started_at=now - timedelta(days=5),
        warning_sent_at=now - timedelta(hours=25),
    )
    mock_db = AsyncMock()
    with patch("scheduler.send_to_group", new=AsyncMock()):
        with patch("scheduler.stop_client", new=AsyncMock()) as mock_stop:
            await _check_client_status(c, mock_db)
    assert c.status == "suspended"
    mock_stop.assert_called_once_with(c)
    mock_db.commit.assert_called_once()
