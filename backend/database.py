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
        # Migrate: add message_id column if not present (no-op for fresh schemas)
        try:
            await conn.execute(text("ALTER TABLE incidents ADD COLUMN message_id TEXT"))
        except Exception:
            pass
        # Ensure unique index exists (idempotent — IF NOT EXISTS is safe on PG and SQLite)
        await conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_incidents_message_id "
            "ON incidents (message_id)"
        ))
