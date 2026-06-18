import os
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./incidents.db")

engine = create_async_engine(DATABASE_URL, pool_pre_ping=True)
AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


async def get_db():
    async with AsyncSessionLocal() as session:
        yield session


async def init_db():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    # Each migration runs in its own transaction so an expected failure (column/index
    # already exists) doesn't abort the transaction that follows.
    try:
        async with engine.begin() as conn:
            await conn.execute(text("ALTER TABLE incidents ADD COLUMN message_id TEXT"))
    except Exception:
        pass

    try:
        async with engine.begin() as conn:
            await conn.execute(text(
                "CREATE UNIQUE INDEX IF NOT EXISTS uq_incidents_message_id "
                "ON incidents (message_id)"
            ))
    except Exception:
        pass

    try:
        async with engine.begin() as conn:
            await conn.execute(text("ALTER TABLE incidents ADD COLUMN updated_at TIMESTAMP"))
    except Exception:
        pass

    try:
        async with engine.begin() as conn:
            await conn.execute(text(
                "ALTER TABLE incident_updates ADD COLUMN relinked BOOLEAN NOT NULL DEFAULT FALSE"
            ))
    except Exception:
        pass

    try:
        async with engine.begin() as conn:
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS incident_status_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    incident_id INTEGER NOT NULL REFERENCES incidents(id),
                    from_status VARCHAR(20),
                    to_status VARCHAR(20) NOT NULL,
                    changed_at TIMESTAMP NOT NULL
                )
            """))
    except Exception:
        pass

    try:
        async with engine.begin() as conn:
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_incident_status_history_incident_id "
                "ON incident_status_history (incident_id)"
            ))
    except Exception:
        pass

    try:
        async with engine.begin() as conn:
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    hashed_password TEXT NOT NULL,
                    created_at TIMESTAMP NOT NULL,
                    created_by TEXT
                )
            """))
    except Exception:
        pass

    try:
        async with engine.begin() as conn:
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL,
                    action VARCHAR(30) NOT NULL,
                    incident_id INTEGER NOT NULL,
                    detail TEXT,
                    created_at TIMESTAMP NOT NULL
                )
            """))
    except Exception:
        pass

    try:
        async with engine.begin() as conn:
            await conn.execute(text(
                "ALTER TABLE incident_status_history ADD COLUMN changed_by TEXT"
            ))
    except Exception:
        pass

    try:
        async with engine.begin() as conn:
            await conn.execute(
                text("ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'user'")
            )
    except Exception:
        pass

    try:
        async with engine.begin() as conn:
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS user_groups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    group_id TEXT NOT NULL,
                    UNIQUE (user_id, group_id)
                )
            """))
    except Exception:
        pass

    try:
        async with engine.begin() as conn:
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_user_groups_user_id ON user_groups (user_id)"
            ))
    except Exception:
        pass

    try:
        async with engine.begin() as conn:
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS admin_profiles (
                    user_id INTEGER PRIMARY KEY REFERENCES users(id),
                    whatsapp_phone TEXT
                )
            """))
    except Exception:
        pass

    try:
        async with engine.begin() as conn:
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS admin_group_subscriptions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER NOT NULL REFERENCES users(id),
                    group_id TEXT NOT NULL,
                    UNIQUE (user_id, group_id)
                )
            """))
    except Exception:
        pass

    try:
        async with engine.begin() as conn:
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_admin_group_subscriptions_user_id "
                "ON admin_group_subscriptions (user_id)"
            ))
    except Exception:
        pass

    try:
        async with engine.begin() as conn:
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS chat_sessions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_key TEXT UNIQUE NOT NULL,
                    messages JSON NOT NULL DEFAULT '[]',
                    updated_at TIMESTAMP NOT NULL
                )
            """))
    except Exception:
        pass
