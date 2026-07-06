import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from models import Incident


async def test_incidents_table_exists(db_session):
    result = await db_session.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
    tables = [row[0] for row in result.fetchall()]
    assert "incidents" in tables


async def test_incident_model_columns():
    cols = {c.name for c in Incident.__table__.columns}
    assert cols == {
        "id", "group_id", "property_name", "reporter_name", "reporter_phone",
        "message_body", "category", "priority", "confidence", "status", "received_at",
        "message_id", "updated_at", "end_date", "escalated", "reminder_offset_hours", "reminder_sent_at",
    }


@pytest.mark.asyncio
async def test_init_db_creates_status_history_table():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    import database
    original = database.engine
    database.engine = engine
    try:
        await database.init_db()
        async with engine.connect() as conn:
            result = await conn.execute(text(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='incident_status_history'"
            ))
            assert result.scalar() == "incident_status_history"
    finally:
        database.engine = original
        await engine.dispose()


@pytest.mark.asyncio
async def test_init_db_adds_relinked_column():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    import database
    original = database.engine
    database.engine = engine
    try:
        await database.init_db()
        async with engine.connect() as conn:
            result = await conn.execute(text("PRAGMA table_info(incident_updates)"))
            cols = [row[1] for row in result.fetchall()]
            assert "relinked" in cols
    finally:
        database.engine = original
        await engine.dispose()
