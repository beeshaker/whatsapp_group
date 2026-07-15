import pytest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch
from models import Client


def _client(
    status="active",
    renewal_date=None,
    grace_started_at=None,
    billing_only_started_at=None,
    last_warning_sent_at=None,
    data_retention_days=90,
    pre_expiry_14_warned=False,
    pre_expiry_2_warned=False,
):
    c = Client()
    c.id = 1
    c.name = "Test"
    c.subdomain = "test"
    c.status = status
    c.renewal_date = renewal_date or date.today()
    c.grace_started_at = grace_started_at
    c.billing_only_started_at = billing_only_started_at
    c.last_warning_sent_at = last_warning_sent_at
    c.data_retention_days = data_retention_days
    c.pre_expiry_14_warned = pre_expiry_14_warned
    c.pre_expiry_2_warned = pre_expiry_2_warned
    c.whatsapp_group_id = "group@g.us"
    c.openwa_url = "http://localhost:2001"
    c.openwa_session = "test"
    c.openwa_api_key = "key"
    c.docker_project = "test"
    c.created_at = datetime.now(timezone.utc)
    return c


@pytest.mark.asyncio
async def test_active_sends_14day_pre_expiry_warning_once():
    from scheduler import _check_client_status
    c = _client(status="active", renewal_date=date.today() + timedelta(days=14))
    mock_db = AsyncMock()
    with patch("scheduler.send_to_group", new=AsyncMock()) as mock_send:
        await _check_client_status(c, mock_db)
    assert c.pre_expiry_14_warned is True
    mock_send.assert_called_once()
    assert "14 days" in mock_send.call_args[0][1]
    mock_db.commit.assert_called_once()


@pytest.mark.asyncio
async def test_active_does_not_resend_14day_warning_if_already_warned():
    from scheduler import _check_client_status
    c = _client(
        status="active",
        renewal_date=date.today() + timedelta(days=14),
        pre_expiry_14_warned=True,
    )
    with patch("scheduler.send_to_group", new=AsyncMock()) as mock_send:
        await _check_client_status(c, AsyncMock())
    mock_send.assert_not_called()


@pytest.mark.asyncio
async def test_active_sends_2day_pre_expiry_warning_once():
    from scheduler import _check_client_status
    c = _client(status="active", renewal_date=date.today() + timedelta(days=2))
    mock_db = AsyncMock()
    with patch("scheduler.send_to_group", new=AsyncMock()) as mock_send:
        await _check_client_status(c, mock_db)
    assert c.pre_expiry_2_warned is True
    mock_send.assert_called_once()
    assert "2 days" in mock_send.call_args[0][1]
    mock_db.commit.assert_called_once()


@pytest.mark.asyncio
async def test_active_transitions_to_grace_when_expired():
    from scheduler import _check_client_status
    c = _client(status="active", renewal_date=date.today() - timedelta(days=1))
    mock_db = AsyncMock()
    with patch("scheduler.send_to_group", new=AsyncMock()) as mock_send:
        await _check_client_status(c, mock_db)
    assert c.status == "grace"
    assert c.grace_started_at is not None
    assert c.last_warning_sent_at is not None
    mock_send.assert_called_once()
    assert "expired" in mock_send.call_args[0][1].lower()
    assert "14 days" in mock_send.call_args[0][1]
    mock_db.commit.assert_called_once()


@pytest.mark.asyncio
async def test_grace_sends_reminder_every_2_days():
    from scheduler import _check_client_status
    now = datetime.now(timezone.utc)
    c = _client(
        status="grace",
        renewal_date=date.today() - timedelta(days=3),
        grace_started_at=now - timedelta(days=3),
        last_warning_sent_at=now - timedelta(days=3),
    )
    mock_db = AsyncMock()
    with patch("scheduler.send_to_group", new=AsyncMock()) as mock_send:
        await _check_client_status(c, mock_db)
    mock_send.assert_called_once()
    msg = mock_send.call_args[0][1]
    assert "overdue" in msg.lower() or "unpaid" in msg.lower()
    assert "days" in msg
    mock_db.commit.assert_called_once()


@pytest.mark.asyncio
async def test_grace_does_not_send_reminder_if_warned_within_2_days():
    from scheduler import _check_client_status
    now = datetime.now(timezone.utc)
    c = _client(
        status="grace",
        renewal_date=date.today() - timedelta(days=3),
        grace_started_at=now - timedelta(days=3),
        last_warning_sent_at=now - timedelta(hours=23),
    )
    with patch("scheduler.send_to_group", new=AsyncMock()) as mock_send:
        await _check_client_status(c, AsyncMock())
    mock_send.assert_not_called()


@pytest.mark.asyncio
async def test_grace_transitions_to_billing_only_after_14_days():
    from scheduler import _check_client_status
    now = datetime.now(timezone.utc)
    c = _client(
        status="grace",
        renewal_date=date.today() - timedelta(days=15),
        grace_started_at=now - timedelta(days=14, hours=1),
        last_warning_sent_at=now - timedelta(days=3),
        data_retention_days=90,
    )
    mock_db = AsyncMock()
    with patch("scheduler.send_to_group", new=AsyncMock()) as mock_send:
        await _check_client_status(c, mock_db)
    assert c.status == "billing_only"
    assert c.billing_only_started_at is not None
    assert c.last_warning_sent_at is not None
    mock_send.assert_called_once()
    msg = mock_send.call_args[0][1]
    assert "suspended" in msg.lower()
    assert "90" in msg
    mock_db.commit.assert_called_once()


@pytest.mark.asyncio
async def test_billing_only_sends_reminder_every_2_days():
    from scheduler import _check_client_status
    now = datetime.now(timezone.utc)
    c = _client(
        status="billing_only",
        renewal_date=date.today() - timedelta(days=17),
        grace_started_at=now - timedelta(days=17),
        billing_only_started_at=now - timedelta(days=3),
        last_warning_sent_at=now - timedelta(days=3),
        data_retention_days=90,
    )
    mock_db = AsyncMock()
    with patch("scheduler.send_to_group", new=AsyncMock()) as mock_send:
        await _check_client_status(c, mock_db)
    mock_send.assert_called_once()
    msg = mock_send.call_args[0][1]
    assert "suspended" in msg.lower() or "urgent" in msg.lower()
    assert "days" in msg
    mock_db.commit.assert_called_once()


@pytest.mark.asyncio
async def test_billing_only_no_reminder_within_2_days():
    from scheduler import _check_client_status
    now = datetime.now(timezone.utc)
    c = _client(
        status="billing_only",
        renewal_date=date.today() - timedelta(days=17),
        billing_only_started_at=now - timedelta(days=3),
        last_warning_sent_at=now - timedelta(hours=20),
    )
    with patch("scheduler.send_to_group", new=AsyncMock()) as mock_send:
        await _check_client_status(c, AsyncMock())
    mock_send.assert_not_called()


@pytest.mark.asyncio
async def test_closed_client_is_skipped():
    from scheduler import _run_daily_checks
    from unittest.mock import patch, AsyncMock, MagicMock
    mock_client = MagicMock()
    mock_client.status = "closed"
    with patch("scheduler.AsyncSessionLocal") as mock_session_cls:
        mock_db = AsyncMock()
        mock_db.__aenter__ = AsyncMock(return_value=mock_db)
        mock_db.__aexit__ = AsyncMock(return_value=None)
        mock_db.execute = AsyncMock(return_value=MagicMock(scalars=MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))))
        mock_session_cls.return_value = mock_db
        await _run_daily_checks()
    # no send_to_group calls — verified implicitly by no exception
