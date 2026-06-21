import asyncio
import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
import database
import models  # noqa: F401 — registers all tables on Base.metadata

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"


@pytest.fixture(scope="session")
def event_loop():
    loop = asyncio.new_event_loop()
    yield loop
    loop.close()


@pytest_asyncio.fixture
async def db_session():
    test_engine = create_async_engine(TEST_DB_URL)
    factory = async_sessionmaker(test_engine, expire_on_commit=False)
    async with test_engine.begin() as conn:
        await conn.run_sync(database.Base.metadata.create_all)
    async with factory() as session:
        yield session
    async with test_engine.begin() as conn:
        await conn.run_sync(database.Base.metadata.drop_all)
    await test_engine.dispose()
