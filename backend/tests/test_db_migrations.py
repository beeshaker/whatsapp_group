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


from models import IncidentCategory


async def test_incident_categories_table_created(migrated_engine):
    async with migrated_engine.connect() as conn:
        result = await conn.execute(text(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='incident_categories'"
        ))
        assert result.scalar_one_or_none() == "incident_categories"


async def test_incident_categories_seeded(migrated_engine):
    async with migrated_engine.connect() as conn:
        result = await conn.execute(text("SELECT slug FROM incident_categories ORDER BY slug"))
        slugs = {row[0] for row in result.all()}
    assert slugs == {"plumbing", "electrical", "lift", "security", "structural", "cleaning", "access", "other"}


async def test_other_category_is_protected(migrated_engine):
    async with migrated_engine.connect() as conn:
        result = await conn.execute(text(
            "SELECT is_protected FROM incident_categories WHERE slug='other'"
        ))
        protected = result.scalar_one_or_none()
    assert protected == 1


async def test_non_other_categories_not_protected(migrated_engine):
    async with migrated_engine.connect() as conn:
        result = await conn.execute(text(
            "SELECT is_protected FROM incident_categories WHERE slug='plumbing'"
        ))
        protected = result.scalar_one_or_none()
    assert protected == 0


async def test_incidents_table_has_priority_column(migrated_engine):
    async with migrated_engine.connect() as conn:
        result = await conn.execute(text("PRAGMA table_info(incidents)"))
        columns = [row[1] for row in result.all()]
        assert "priority" in columns
        assert "severity" not in columns


async def test_incidents_table_has_end_date_column(migrated_engine):
    async with migrated_engine.connect() as conn:
        result = await conn.execute(text("PRAGMA table_info(incidents)"))
        columns = [row[1] for row in result.all()]
        assert "end_date" in columns


async def test_incidents_table_has_vehicle_plate_column(migrated_engine):
    async with migrated_engine.connect() as conn:
        result = await conn.execute(text("PRAGMA table_info(incidents)"))
        columns = [row[1] for row in result.all()]
        assert "vehicle_plate" in columns


async def test_incidents_table_has_escalated_column(migrated_engine):
    async with migrated_engine.connect() as conn:
        result = await conn.execute(text("PRAGMA table_info(incidents)"))
        columns = [row[1] for row in result.all()]
        assert "escalated" in columns


async def test_escalated_defaults_to_false(db_session):
    from models import Incident
    now = datetime.now(timezone.utc)
    incident = Incident(
        group_id="g1@g.us",
        property_name="Block A",
        reporter_name="Alice",
        message_body="Pump leaking",
        category="plumbing",
        priority="high",
        confidence=0.9,
        status="review",
        received_at=now,
    )
    db_session.add(incident)
    await db_session.commit()
    await db_session.refresh(incident)
    assert incident.escalated is False
    assert incident.end_date is None


async def test_severity_rename_preserves_existing_data():
    """Simulates upgrading a pre-existing DB that still has the old `severity` column."""
    upgrade_engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with upgrade_engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE incidents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT NOT NULL,
                property_name TEXT NOT NULL,
                reporter_name TEXT,
                reporter_phone TEXT,
                message_body TEXT NOT NULL,
                category VARCHAR(50) NOT NULL,
                severity VARCHAR(20) NOT NULL,
                confidence FLOAT NOT NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'review',
                received_at TIMESTAMP NOT NULL,
                message_id TEXT
            )
        """))
        await conn.execute(text(
            "INSERT INTO incidents (group_id, property_name, message_body, category, "
            "severity, confidence, status, received_at) VALUES "
            "('g1@g.us', 'Block A', 'Pump leaking', 'plumbing', 'high', 0.9, 'review', "
            "'2026-01-01 00:00:00')"
        ))

    import database
    original_engine = database.engine
    database.engine = upgrade_engine
    try:
        await init_db()
    finally:
        database.engine = original_engine

    async with upgrade_engine.connect() as conn:
        result = await conn.execute(text("SELECT priority FROM incidents"))
        assert result.scalar_one() == "high"
        columns = [row[1] for row in (await conn.execute(text("PRAGMA table_info(incidents)"))).all()]
        assert "severity" not in columns
        assert "end_date" in columns
        assert "escalated" in columns
    await upgrade_engine.dispose()


async def test_incidents_table_has_reminder_offset_hours_column(migrated_engine):
    async with migrated_engine.connect() as conn:
        result = await conn.execute(text("PRAGMA table_info(incidents)"))
        columns = [row[1] for row in result.all()]
        assert "reminder_offset_hours" in columns


async def test_incidents_table_has_reminder_sent_at_column(migrated_engine):
    async with migrated_engine.connect() as conn:
        result = await conn.execute(text("PRAGMA table_info(incidents)"))
        columns = [row[1] for row in result.all()]
        assert "reminder_sent_at" in columns


async def test_reminder_fields_default_to_null(db_session):
    from models import Incident
    now = datetime.now(timezone.utc)
    incident = Incident(
        group_id="g1@g.us",
        property_name="Block A",
        reporter_name="Alice",
        message_body="Pump leaking",
        category="plumbing",
        priority="high",
        confidence=0.9,
        status="review",
        received_at=now,
    )
    db_session.add(incident)
    await db_session.commit()
    await db_session.refresh(incident)
    assert incident.reminder_offset_hours is None
    assert incident.reminder_sent_at is None


async def test_incidents_table_has_issue_index_column(migrated_engine):
    async with migrated_engine.connect() as conn:
        result = await conn.execute(text("PRAGMA table_info(incidents)"))
        columns = [row[1] for row in result.all()]
        assert "issue_index" in columns


async def test_incident_updates_table_has_issue_index_column(migrated_engine):
    async with migrated_engine.connect() as conn:
        result = await conn.execute(text("PRAGMA table_info(incident_updates)"))
        columns = [row[1] for row in result.all()]
        assert "issue_index" in columns


async def test_issue_index_defaults_to_zero(db_session):
    from models import Incident
    now = datetime.now(timezone.utc)
    incident = Incident(
        group_id="g1@g.us", property_name="Block A", reporter_name="Alice",
        message_body="Pump leaking", category="plumbing", priority="high",
        confidence=0.9, status="review", received_at=now,
    )
    db_session.add(incident)
    await db_session.commit()
    await db_session.refresh(incident)
    assert incident.issue_index == 0


async def test_compound_unique_constraint_allows_differing_issue_index(db_session):
    from models import Incident
    now = datetime.now(timezone.utc)
    db_session.add(Incident(
        group_id="g1@g.us", property_name="Block A", message_body="Pump leaking",
        category="plumbing", priority="high", confidence=0.9, status="review",
        received_at=now, message_id="msg-split-1", issue_index=0,
    ))
    await db_session.commit()
    db_session.add(Incident(
        group_id="g1@g.us", property_name="Block A", message_body="Lift stuck",
        category="lift", priority="high", confidence=0.9, status="review",
        received_at=now, message_id="msg-split-1", issue_index=1,
    ))
    await db_session.commit()
    result = await db_session.execute(
        select(Incident).where(Incident.message_id == "msg-split-1")
    )
    assert len(result.scalars().all()) == 2


async def test_compound_unique_constraint_rejects_exact_duplicate_pair(db_session):
    from models import Incident
    now = datetime.now(timezone.utc)
    db_session.add(Incident(
        group_id="g1@g.us", property_name="Block A", message_body="Pump leaking",
        category="plumbing", priority="high", confidence=0.9, status="review",
        received_at=now, message_id="msg-dup-1", issue_index=0,
    ))
    await db_session.commit()
    db_session.add(Incident(
        group_id="g1@g.us", property_name="Block A", message_body="Pump leaking again",
        category="plumbing", priority="high", confidence=0.9, status="review",
        received_at=now, message_id="msg-dup-1", issue_index=0,
    ))
    with pytest.raises(Exception):
        await db_session.commit()
    await db_session.rollback()


async def test_legacy_pre_migration_incidents_upgrade_preserves_rows_and_enforces_new_constraint():
    """Simulates upgrading a pre-existing DB that has the old single-column
    uq_incidents_message_id index and no issue_index column."""
    upgrade_engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with upgrade_engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE incidents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT NOT NULL,
                property_name TEXT NOT NULL,
                reporter_name TEXT,
                reporter_phone TEXT,
                message_body TEXT NOT NULL,
                category VARCHAR(50) NOT NULL,
                priority VARCHAR(20) NOT NULL,
                confidence FLOAT NOT NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'review',
                received_at TIMESTAMP NOT NULL,
                message_id TEXT,
                updated_at TIMESTAMP,
                end_date TIMESTAMP,
                escalated BOOLEAN NOT NULL DEFAULT 0
            )
        """))
        await conn.execute(text(
            "CREATE UNIQUE INDEX uq_incidents_message_id ON incidents (message_id)"
        ))
        await conn.execute(text(
            "INSERT INTO incidents (group_id, property_name, message_body, category, "
            "priority, confidence, status, received_at, message_id) VALUES "
            "('g1@g.us', 'Block A', 'Pump leaking', 'plumbing', 'high', 0.9, 'review', "
            "'2026-01-01 00:00:00', 'legacy-msg-1')"
        ))

    import database
    original_engine = database.engine
    database.engine = upgrade_engine
    try:
        await init_db()
    finally:
        database.engine = original_engine

    async with upgrade_engine.connect() as conn:
        result = await conn.execute(text(
            "SELECT issue_index FROM incidents WHERE message_id='legacy-msg-1'"
        ))
        assert result.scalar_one() == 0

        # The new compound constraint allows a second row sharing the same
        # message_id with a different issue_index — exactly what the old
        # single-column unique index on message_id would have rejected.
        await conn.execute(text(
            "INSERT INTO incidents (group_id, property_name, message_body, category, "
            "priority, confidence, status, received_at, message_id, issue_index) VALUES "
            "('g1@g.us', 'Block A', 'Lift stuck', 'lift', 'high', 0.9, 'review', "
            "'2026-01-01 00:00:01', 'legacy-msg-1', 1)"
        ))
        count = (await conn.execute(text(
            "SELECT COUNT(*) FROM incidents WHERE message_id='legacy-msg-1'"
        ))).scalar()
        assert count == 2
    await upgrade_engine.dispose()


async def test_incidents_table_has_lead_columns(migrated_engine):
    async with migrated_engine.connect() as conn:
        result = await conn.execute(text("PRAGMA table_info(incidents)"))
        columns = [row[1] for row in result.all()]
    for col in ("lead_agent", "contact_name", "contact_phone", "lead_location",
                "lead_budget", "transaction_type", "lead_source"):
        assert col in columns


async def test_lead_columns_default_to_null(db_session):
    from models import Incident
    now = datetime.now(timezone.utc)
    incident = Incident(
        group_id="g1@g.us", property_name="Block A", reporter_name="Alice",
        message_body="Pump leaking", category="plumbing", priority="high",
        confidence=0.9, status="review", received_at=now,
    )
    db_session.add(incident)
    await db_session.commit()
    await db_session.refresh(incident)
    assert incident.lead_agent is None
    assert incident.contact_name is None
    assert incident.contact_phone is None
    assert incident.lead_location is None
    assert incident.lead_budget is None
    assert incident.transaction_type is None
    assert incident.lead_source is None


async def test_lead_mode_seeds_real_estate_categories():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    import database
    original_engine = database.engine
    original_lead_mode = database.LEAD_MODE
    database.engine = engine
    database.LEAD_MODE = True
    try:
        await init_db()
        async with engine.connect() as conn:
            result = await conn.execute(text("SELECT slug FROM incident_categories ORDER BY slug"))
            slugs = {row[0] for row in result.all()}
    finally:
        database.engine = original_engine
        database.LEAD_MODE = original_lead_mode
        await engine.dispose()
    assert slugs == {"apartment", "house", "godown", "commercial", "plot", "land", "other"}
