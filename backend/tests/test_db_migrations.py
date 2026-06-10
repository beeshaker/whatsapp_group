import os
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

import pytest
import pytest_asyncio
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from database import init_db, Base


@pytest_asyncio.fixture(scope="module")
async def migrated_engine():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    # Patch the module-level engine used by init_db
    import database
    original_engine = database.engine
    database.engine = engine
    await init_db()
    yield engine
    database.engine = original_engine
    await engine.dispose()


async def test_users_table_created(migrated_engine):
    async with migrated_engine.connect() as conn:
        result = await conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='users'"
        ))
        assert result.scalar_one_or_none() == "users"


async def test_audit_log_table_created(migrated_engine):
    async with migrated_engine.connect() as conn:
        result = await conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='audit_log'"
        ))
        assert result.scalar_one_or_none() == "audit_log"


async def test_incident_status_history_has_changed_by_column(migrated_engine):
    async with migrated_engine.connect() as conn:
        result = await conn.execute(text("PRAGMA table_info(incident_status_history)"))
        columns = [row[1] for row in result.all()]
        assert "changed_by" in columns
