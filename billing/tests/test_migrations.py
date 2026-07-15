"""Tests for the hand-rolled schema migration in main._migrate_db().

These tests build a *legacy-shaped* SQLite database by hand (raw SQL,
independent of the current SQLAlchemy models) to reproduce the real
production schema shape that predates the tier-only-billing refactor:
a `clients.plan` NOT NULL column with no DB-level default, and a
`plan_prices` table. _migrate_db() must destroy both, loudly, and the
app must still be able to create new Client rows afterwards.
"""
from datetime import date, datetime, timezone

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import main
from models import Client

# Mirrors the real, already-migrated production `clients` table shape
# (i.e. every ADD COLUMN in _migrate_db()'s additive list has already run)
# plus the legacy `plan` column this task removes.
LEGACY_CLIENTS_SQL = """
CREATE TABLE clients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    subdomain TEXT NOT NULL UNIQUE,
    plan TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active',
    renewal_date DATE NOT NULL,
    grace_started_at DATETIME,
    billing_only_started_at DATETIME,
    last_warning_sent_at DATETIME,
    data_retention_days INTEGER NOT NULL DEFAULT 90,
    pre_expiry_14_warned BOOLEAN NOT NULL DEFAULT 0,
    pre_expiry_2_warned BOOLEAN NOT NULL DEFAULT 0,
    whatsapp_group_id TEXT,
    openwa_url TEXT,
    openwa_session TEXT,
    openwa_api_key TEXT,
    docker_project TEXT,
    admin_whatsapp_phone TEXT,
    whatsapp_invite_link TEXT,
    backend_port INTEGER,
    allowed_ticket_groups TEXT,
    ticket_group_tier_id INTEGER,
    created_at DATETIME NOT NULL
)
"""

LEGACY_PLAN_PRICES_SQL = """
CREATE TABLE plan_prices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    plan_type TEXT NOT NULL,
    amount NUMERIC(10, 2) NOT NULL,
    currency TEXT NOT NULL DEFAULT 'KES',
    set_at DATETIME NOT NULL,
    set_by TEXT NOT NULL
)
"""

# group_tier_prices/group_upgrade_requests already exist in production
# (introduced by an earlier task) — included here, in their pre-this-task
# shape (no `name` / `mpesa_transaction_id`), purely so the additive and
# backfill parts of _migrate_db() have real tables to operate on, exactly
# as they will on the real legacy DB.
LEGACY_GROUP_TIER_PRICES_SQL = """
CREATE TABLE group_tier_prices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    min_groups INTEGER NOT NULL,
    max_groups INTEGER,
    amount NUMERIC(10, 2) NOT NULL,
    currency TEXT NOT NULL DEFAULT 'KES',
    set_at DATETIME NOT NULL,
    set_by TEXT NOT NULL
)
"""

LEGACY_GROUP_UPGRADE_REQUESTS_SQL = """
CREATE TABLE group_upgrade_requests (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    client_id INTEGER NOT NULL,
    group_id TEXT NOT NULL,
    target_tier_id INTEGER NOT NULL,
    phone TEXT NOT NULL,
    amount NUMERIC(10, 2) NOT NULL,
    checkout_request_id TEXT,
    status TEXT NOT NULL DEFAULT 'pending',
    created_at DATETIME NOT NULL
)
"""


@pytest_asyncio.fixture
async def legacy_engine():
    """A from-scratch SQLite DB built via raw SQL in the pre-migration shape."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    now = datetime.now(timezone.utc).isoformat()
    async with engine.begin() as conn:
        await conn.execute(text(LEGACY_CLIENTS_SQL))
        await conn.execute(text(LEGACY_PLAN_PRICES_SQL))
        await conn.execute(text(LEGACY_GROUP_TIER_PRICES_SQL))
        await conn.execute(text(LEGACY_GROUP_UPGRADE_REQUESTS_SQL))
        for min_groups, max_groups in ((1, 5), (6, 10), (11, None)):
            await conn.execute(
                text(
                    "INSERT INTO group_tier_prices "
                    "(min_groups, max_groups, amount, currency, set_at, set_by) "
                    "VALUES (:min_groups, :max_groups, 0, 'KES', :now, 'system')"
                ),
                {"min_groups": min_groups, "max_groups": max_groups, "now": now},
            )
    yield engine
    await engine.dispose()


async def _clients_columns(engine) -> set[str]:
    async with engine.begin() as conn:
        result = await conn.execute(text("PRAGMA table_info(clients)"))
        return {row[1] for row in result.fetchall()}


async def _table_names(engine) -> set[str]:
    async with engine.begin() as conn:
        result = await conn.execute(text("SELECT name FROM sqlite_master WHERE type='table'"))
        return {row[0] for row in result.fetchall()}


@pytest.mark.asyncio
async def test_migrate_db_drops_legacy_plan_column_and_plan_prices_table(legacy_engine, monkeypatch):
    monkeypatch.setattr(main, "engine", legacy_engine)

    await main._migrate_db()

    assert "plan" not in await _clients_columns(legacy_engine)
    assert "plan_prices" not in await _table_names(legacy_engine)

    # A plan-less Client(...) insert must now succeed against the migrated schema —
    # this is the exact codepath that would raise
    # "NOT NULL constraint failed: clients.plan" if the column were still present.
    factory = async_sessionmaker(legacy_engine, expire_on_commit=False)
    async with factory() as session:
        client = Client(
            name="Acme", subdomain="acme-legacy",
            renewal_date=date.today(), created_at=datetime.now(timezone.utc),
        )
        session.add(client)
        await session.commit()
        await session.refresh(client)
        assert client.id is not None
        assert not hasattr(client, "plan")

    # Running the migration again must be a safe no-op.
    await main._migrate_db()
    assert "plan" not in await _clients_columns(legacy_engine)
    assert "plan_prices" not in await _table_names(legacy_engine)


@pytest.mark.asyncio
async def test_migrate_db_backfills_placeholder_tier_names_idempotently(legacy_engine, monkeypatch):
    monkeypatch.setattr(main, "engine", legacy_engine)

    await main._migrate_db()

    async with legacy_engine.begin() as conn:
        rows = (await conn.execute(
            text("SELECT min_groups, name FROM group_tier_prices ORDER BY min_groups")
        )).fetchall()
    names_by_min_groups = {row[0]: row[1] for row in rows}
    assert names_by_min_groups[1]
    assert names_by_min_groups[6]
    assert names_by_min_groups[11]

    # A second run must not clobber real (non-placeholder) names an admin may
    # have already set — simulate that, then re-run and confirm it's untouched.
    async with legacy_engine.begin() as conn:
        await conn.execute(
            text("UPDATE group_tier_prices SET name = 'Starter' WHERE min_groups = 1")
        )

    await main._migrate_db()

    async with legacy_engine.begin() as conn:
        name = (await conn.execute(
            text("SELECT name FROM group_tier_prices WHERE min_groups = 1")
        )).scalar_one()
    assert name == "Starter"
