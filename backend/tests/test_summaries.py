import zoneinfo
from datetime import date, datetime, timedelta

import pytest

from summaries import window_for_date


KENYA_TZ = zoneinfo.ZoneInfo("Africa/Nairobi")


def test_window_for_weekday_is_single_day():
    # Wednesday June 18 2025
    d = date(2025, 6, 18)
    start, end, label = window_for_date(d)
    assert start == datetime(2025, 6, 18, 0, 0, 0, tzinfo=KENYA_TZ)
    assert end.date() == date(2025, 6, 18)
    assert label == "Wednesday 18 Jun"


def test_window_for_monday_is_preceding_weekend():
    # Monday June 16 2025
    d = date(2025, 6, 16)
    start, end, label = window_for_date(d)
    assert start == datetime(2025, 6, 14, 0, 0, 0, tzinfo=KENYA_TZ)  # Saturday
    assert end.date() == date(2025, 6, 15)                             # Sunday
    assert "Weekend" in label
    assert "14" in label
    assert "15" in label


def test_window_for_saturday_is_single_day():
    d = date(2025, 6, 14)
    start, end, label = window_for_date(d)
    assert start.date() == date(2025, 6, 14)
    assert end.date() == date(2025, 6, 14)


def test_window_for_sunday_is_single_day():
    d = date(2025, 6, 15)
    start, end, label = window_for_date(d)
    assert start.date() == date(2025, 6, 15)
    assert end.date() == date(2025, 6, 15)


from datetime import timezone

import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base
from models import Incident, IncidentStatusHistory
from summaries import build_summary


_engine = create_async_engine(
    "sqlite+aiosqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = async_sessionmaker(_engine, expire_on_commit=False)


@pytest_asyncio.fixture(scope="module", autouse=True)
async def _schema():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@pytest_asyncio.fixture
async def db():
    async with _Session() as session:
        yield session


async def _add_incident(db, group_id, message_body, severity, status, received_at):
    inc = Incident(
        group_id=group_id,
        property_name="Test",
        message_body=message_body,
        category="plumbing",
        severity=severity,
        confidence=0.9,
        status=status,
        received_at=received_at,
    )
    db.add(inc)
    await db.flush()
    return inc


async def test_build_summary_new_count(db):
    gid = "test@g.us"
    window_start = datetime(2025, 6, 18, 0, 0, 0, tzinfo=KENYA_TZ)
    window_end = datetime(2025, 6, 18, 23, 59, 59, tzinfo=KENYA_TZ)

    await _add_incident(db, gid, "Pump leaking badly", "high", "review",
                        datetime(2025, 6, 18, 10, 0, 0, tzinfo=KENYA_TZ))
    await _add_incident(db, gid, "Light out in corridor", "low", "review",
                        datetime(2025, 6, 18, 14, 0, 0, tzinfo=KENYA_TZ))
    # Outside window — should not count
    await _add_incident(db, gid, "Old issue", "medium", "review",
                        datetime(2025, 6, 17, 10, 0, 0, tzinfo=KENYA_TZ))
    await db.commit()

    result = await build_summary(gid, window_start, window_end, "Wednesday 18 Jun", db)

    assert result["group_id"] == gid
    assert result["new_count"] == 2
    assert result["period_label"] == "Wednesday 18 Jun"
    assert len(result["new_incidents"]) == 2


async def test_build_summary_resolved_count(db):
    gid = "resolve@g.us"
    window_start = datetime(2025, 6, 18, 0, 0, 0, tzinfo=KENYA_TZ)
    window_end = datetime(2025, 6, 18, 23, 59, 59, tzinfo=KENYA_TZ)

    inc = await _add_incident(db, gid, "Fixed pump", "low", "resolved",
                              datetime(2025, 6, 17, 9, 0, 0, tzinfo=KENYA_TZ))
    db.add(IncidentStatusHistory(
        incident_id=inc.id,
        from_status="review",
        to_status="resolved",
        changed_at=datetime(2025, 6, 18, 11, 0, 0, tzinfo=KENYA_TZ),
    ))
    await db.commit()

    result = await build_summary(gid, window_start, window_end, "Wednesday 18 Jun", db)
    assert result["resolved_count"] == 1


async def test_build_summary_still_open_backlog(db):
    gid = "backlog@g.us"
    window_start = datetime(2025, 6, 18, 0, 0, 0, tzinfo=KENYA_TZ)
    window_end = datetime(2025, 6, 18, 23, 59, 59, tzinfo=KENYA_TZ)

    await _add_incident(db, gid, "High issue", "high", "review",
                        datetime(2025, 6, 16, 9, 0, 0, tzinfo=KENYA_TZ))
    await _add_incident(db, gid, "Medium issue", "medium", "acknowledged",
                        datetime(2025, 6, 17, 9, 0, 0, tzinfo=KENYA_TZ))
    await db.commit()

    result = await build_summary(gid, window_start, window_end, "Wednesday 18 Jun", db)
    assert result["still_open_count"] == 2
    assert result["open_backlog"]["high"] == 1
    assert result["open_backlog"]["medium"] == 1
    assert result["open_backlog"]["low"] == 0


async def test_build_summary_title_truncated_to_80_chars(db):
    gid = "title@g.us"
    window_start = datetime(2025, 6, 18, 0, 0, 0, tzinfo=KENYA_TZ)
    window_end = datetime(2025, 6, 18, 23, 59, 59, tzinfo=KENYA_TZ)
    long_body = "x" * 120

    await _add_incident(db, gid, long_body, "low", "review",
                        datetime(2025, 6, 18, 10, 0, 0, tzinfo=KENYA_TZ))
    await db.commit()

    result = await build_summary(gid, window_start, window_end, "Wednesday 18 Jun", db)
    assert len(result["new_incidents"][0]["title"]) == 80


from unittest.mock import patch, AsyncMock

from summaries import format_whatsapp_summary


async def test_get_summaries_admin_only(client):
    # client fixture uses a logged-in admin — should succeed
    with patch("main.build_summary", new=AsyncMock(return_value={
        "group_id": "g@g.us", "period_label": "Wednesday 18 Jun",
        "new_count": 1, "resolved_count": 0, "still_open_count": 1,
        "new_incidents": [{"id": 1, "title": "x", "severity": "high", "status": "review"}],
        "open_backlog": {"high": 1, "medium": 0, "low": 0},
    })):
        with patch("main._distinct_group_ids", new=AsyncMock(return_value=["g@g.us"])):
            resp = await client.get("/api/summaries")
    assert resp.status_code == 200


async def test_get_summaries_returns_list(client):
    with patch("main.build_summary", new=AsyncMock(return_value={
        "group_id": "g@g.us", "period_label": "Wednesday 18 Jun",
        "new_count": 0, "resolved_count": 0, "still_open_count": 0,
        "new_incidents": [],
        "open_backlog": {"high": 0, "medium": 0, "low": 0},
    })):
        with patch("main._distinct_group_ids", new=AsyncMock(return_value=["g@g.us"])):
            resp = await client.get("/api/summaries")
    assert isinstance(resp.json(), list)


def test_format_whatsapp_summary_contains_key_fields():
    summary = {
        "group_id": "120363@g.us",
        "period_label": "Tuesday 17 Jun",
        "new_count": 2,
        "resolved_count": 1,
        "still_open_count": 3,
        "new_incidents": [
            {"id": 1, "title": "Pump leaking", "severity": "high", "status": "review"},
        ],
        "open_backlog": {"high": 2, "medium": 1, "low": 0},
    }
    text = format_whatsapp_summary(summary, "http://localhost:8000")

    assert "120363@g.us" in text
    assert "Tuesday 17 Jun" in text
    assert "New issues: 2" in text
    assert "Resolved: 1" in text
    assert "Still unresolved: 3" in text
    assert "http://localhost:8000" in text
    assert "Pump leaking" in text
    assert "high" in text.lower()
