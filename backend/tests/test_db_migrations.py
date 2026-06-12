import os
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")

from datetime import datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

from database import init_db, Base
from models import User, UserGroup


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


async def test_user_role_defaults_to_user(db_session):
    user = User(
        username="roletest",
        hashed_password="x",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(user)
    await db_session.commit()
    await db_session.refresh(user)
    assert user.role == "user"


async def test_user_group_stores_group_id(db_session):
    user = User(
        username="grouptest",
        hashed_password="x",
        created_at=datetime.now(timezone.utc),
        role="user",
    )
    db_session.add(user)
    await db_session.flush()
    ug = UserGroup(user_id=user.id, group_id="111@g.us")
    db_session.add(ug)
    await db_session.commit()
    result = await db_session.execute(
        select(UserGroup).where(UserGroup.user_id == user.id)
    )
    assert result.scalar_one().group_id == "111@g.us"


async def test_user_group_unique_constraint(db_session):
    user = User(
        username="dupgroup",
        hashed_password="x",
        created_at=datetime.now(timezone.utc),
        role="user",
    )
    db_session.add(user)
    await db_session.flush()
    db_session.add(UserGroup(user_id=user.id, group_id="dup@g.us"))
    await db_session.commit()
    db_session.add(UserGroup(user_id=user.id, group_id="dup@g.us"))
    with pytest.raises(Exception):
        await db_session.commit()
    await db_session.rollback()


async def test_user_groups_table_created(migrated_engine):
    async with migrated_engine.connect() as conn:
        result = await conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='user_groups'"
        ))
        assert result.scalar_one_or_none() == "user_groups"


async def test_users_table_has_role_column(migrated_engine):
    async with migrated_engine.connect() as conn:
        result = await conn.execute(text("PRAGMA table_info(users)"))
        columns = [row[1] for row in result.all()]
        assert "role" in columns
