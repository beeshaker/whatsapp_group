import zoneinfo
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import StaticPool
from sqlalchemy import select

from database import Base
from models import ChatSession
from chat import answer_query

KENYA_TZ = zoneinfo.ZoneInfo("Africa/Nairobi")

_engine = create_async_engine(
    "sqlite+aiosqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = async_sessionmaker(_engine, expire_on_commit=False)

_MOCK_REPLY = "There are 2 open incidents."

# _chat returns an Ollama /api/chat response dict with no tool calls
_MOCK_CHAT_RESPONSE = {
    "message": {"role": "assistant", "content": _MOCK_REPLY, "tool_calls": []}
}


@pytest_asyncio.fixture(scope="module", autouse=True)
async def _schema():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@pytest_asyncio.fixture
async def db():
    async with _Session() as session:
        yield session


async def test_answer_query_creates_session(db):
    with patch("chat._chat", new=AsyncMock(return_value=_MOCK_CHAT_RESPONSE)):
        reply = await answer_query("How many open?", "web:1", db)

    assert reply == _MOCK_REPLY
    result = await db.execute(select(ChatSession).where(ChatSession.session_key == "web:1"))
    session = result.scalar_one_or_none()
    assert session is not None
    assert len(session.messages) == 2
    assert session.messages[0]["role"] == "user"
    assert session.messages[1]["role"] == "assistant"


async def test_answer_query_appends_history(db):
    with patch("chat._chat", new=AsyncMock(return_value=_MOCK_CHAT_RESPONSE)):
        await answer_query("First question", "web:2", db)
        await answer_query("Second question", "web:2", db)

    result = await db.execute(select(ChatSession).where(ChatSession.session_key == "web:2"))
    session = result.scalar_one_or_none()
    assert len(session.messages) == 4


async def test_answer_query_trims_to_20_messages(db):
    with patch("chat._chat", new=AsyncMock(return_value=_MOCK_CHAT_RESPONSE)):
        for i in range(12):
            await answer_query(f"Q{i}", "web:3", db)

    result = await db.execute(select(ChatSession).where(ChatSession.session_key == "web:3"))
    session = result.scalar_one_or_none()
    assert len(session.messages) == 20


async def test_answer_query_resets_stale_session(db):
    stale_time = datetime.now(timezone.utc) - timedelta(hours=25)
    old_session = ChatSession(
        session_key="web:4",
        messages=[{"role": "user", "content": "old"}, {"role": "assistant", "content": "old reply"}],
        updated_at=stale_time,
    )
    db.add(old_session)
    await db.commit()

    with patch("chat._chat", new=AsyncMock(return_value=_MOCK_CHAT_RESPONSE)):
        await answer_query("Fresh question", "web:4", db)

    result = await db.execute(select(ChatSession).where(ChatSession.session_key == "web:4"))
    session = result.scalar_one_or_none()
    # Only the new exchange, old messages discarded
    assert len(session.messages) == 2
    assert session.messages[0]["content"] == "Fresh question"
