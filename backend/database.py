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
