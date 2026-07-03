import logging
import os
import re
import zoneinfo
from datetime import datetime, timezone

import httpx
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from models import ChatSession

logger = logging.getLogger(__name__)

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")

_SALES_SYSTEM_PROMPT = """You are a friendly sales agent for a WhatsApp group management platform. Keep replies short and conversational — this is WhatsApp.

Our platform helps businesses take control of chaotic WhatsApp groups by turning them into an organised ticketing system. Here is what we offer:

• Multi-group ticketing — messages across multiple WhatsApp groups are automatically classified and converted into trackable tickets with status, priority, and full history. No more chasing conversations or losing important issues in the noise.

• AI-powered classification — every message is read, categorised (maintenance, complaint, billing, escalation, etc.), priority-scored, and either linked to an existing open ticket or opened as a new one — automatically, with no manual effort.

• Centralised dashboard — admins manage all their groups from one web dashboard. See every open ticket, filter by group or category, update statuses, and receive daily WhatsApp summaries of what happened overnight.

• WhatsApp sales agents — we can deploy an AI agent inside any of your client's WhatsApp groups that automatically answers customer questions, handles FAQs, and escalates complex issues to a human. Works 24/7 without extra staff.

• M-Pesa billing — built-in subscription reminders and M-Pesa STK push support so payment collection is seamlessly part of your workflow.

We work with property managers, housing estates, customer service teams, and any organisation juggling multiple WhatsApp groups.

Be helpful and honest. If you do not know a specific price or detail, invite the person to reach out directly to the admin for a tailored quote."""
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
OLLAMA_TIMEOUT = float(os.getenv("OLLAMA_TIMEOUT", "60"))
KENYA_TZ = zoneinfo.ZoneInfo("Africa/Nairobi")
_SESSION_TTL_SECONDS = 24 * 3600

_DB_SCHEMA = """
Tables (SQLite, all timestamps UTC):

incidents
  id INTEGER PK, group_id TEXT, message_body TEXT, category TEXT,
  priority TEXT ('low'|'medium'|'high'|'urgent'), status TEXT ('new'|'review'|'acknowledged'|'resolved'|'ignored'),
  -- IMPORTANT: "open" incidents = status NOT IN ('resolved', 'ignored')
  received_at DATETIME, wa_message_id TEXT

incident_status_history
  id INTEGER PK, incident_id INTEGER FK→incidents.id,
  from_status TEXT, to_status TEXT, changed_by TEXT, changed_at DATETIME

incident_updates
  id INTEGER PK, incident_id INTEGER FK→incidents.id,
  message_body TEXT, sent_at DATETIME

users
  id INTEGER PK, username TEXT, role TEXT ('admin'|'user'),
  hashed_password TEXT, created_at DATETIME, created_by TEXT

user_groups
  user_id INTEGER FK→users.id, group_id TEXT
"""

_WRITE_PATTERN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|DROP|CREATE|ALTER|TRUNCATE|REPLACE|ATTACH|DETACH)\b",
    re.IGNORECASE,
)


def _validate_readonly(sql: str) -> None:
    stripped = sql.strip()
    if not stripped.upper().startswith("SELECT"):
        raise ValueError("Only SELECT statements are allowed.")
    if _WRITE_PATTERN.search(stripped):
        raise ValueError("Write operations are not permitted.")


async def _execute_sql(sql: str, db: AsyncSession) -> str:
    _validate_readonly(sql)
    logger.info("Tool SQL: %s", sql)
    try:
        result = await db.execute(text(sql))
        rows = result.fetchall()
        if not rows:
            return "No rows returned."
        cols = list(result.keys())
        lines = [", ".join(cols)]
        for row in rows[:50]:  # cap at 50 rows to keep context manageable
            lines.append(", ".join(str(v) for v in row))
        if len(rows) > 50:
            lines.append(f"... ({len(rows) - 50} more rows truncated)")
        return "\n".join(lines)
    except Exception as exc:
        return f"Query error: {exc}"


async def _chat(messages: list[dict]) -> str:
    async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT) as client:
        resp = await client.post(
            f"{OLLAMA_HOST}/api/chat",
            json={"model": OLLAMA_MODEL, "messages": messages, "stream": False},
        )
        resp.raise_for_status()
        return (resp.json().get("message", {}).get("content") or "").strip()


async def _generate_sql(question: str) -> str:
    messages = [
        {
            "role": "system",
            "content": (
                "You write SQLite SELECT queries. Output ONLY the SQL — no explanation, "
                "no markdown, no backticks, no semicolon.\n\n"
                f"Schema:\n{_DB_SCHEMA}"
            ),
        },
        {"role": "user", "content": f"Write a query to answer: {question}"},
    ]
    raw = await _chat(messages)
    # Strip markdown fences if model adds them anyway
    sql = re.sub(r"^```[a-zA-Z]*\n?", "", raw.strip(), flags=re.IGNORECASE)
    sql = re.sub(r"\n?```$", "", sql).strip().rstrip(";")
    logger.info("Generated SQL: %s", sql)
    return sql


async def answer_sales_query(question: str, session_key: str, db: AsyncSession) -> str:
    """Answer a question using the sales agent persona. No SQL — pure conversation."""
    from sqlalchemy import select as sa_select
    now = datetime.now(timezone.utc)

    result = await db.execute(sa_select(ChatSession).where(ChatSession.session_key == session_key))
    session = result.scalar_one_or_none()

    if session is None:
        session = ChatSession(session_key=session_key, messages=[], updated_at=now)
        db.add(session)
        await db.flush()
    elif (now - session.updated_at.replace(tzinfo=session.updated_at.tzinfo or timezone.utc)).total_seconds() > _SESSION_TTL_SECONDS:
        session.messages = []
        session.updated_at = now

    messages = [{"role": "system", "content": _SALES_SYSTEM_PROMPT}]
    messages.extend(list(session.messages))
    messages.append({"role": "user", "content": question})

    reply = await _chat(messages)

    new_turns = [
        {"role": "user", "content": question},
        {"role": "assistant", "content": reply},
    ]
    session.messages = (list(session.messages) + new_turns)[-20:]
    session.updated_at = now
    await db.commit()

    return reply or "Sorry, I could not generate a reply. Please contact the admin directly."


async def answer_query(question: str, session_key: str, db: AsyncSession) -> str:
    now = datetime.now(timezone.utc)
    now_ke = now.astimezone(KENYA_TZ)

    # Load or create session
    from sqlalchemy import select as sa_select
    result = await db.execute(
        sa_select(ChatSession).where(ChatSession.session_key == session_key)
    )
    session = result.scalar_one_or_none()

    if session is None:
        session = ChatSession(session_key=session_key, messages=[], updated_at=now)
        db.add(session)
        await db.flush()
    elif (now - session.updated_at.replace(tzinfo=session.updated_at.tzinfo or timezone.utc)).total_seconds() > _SESSION_TTL_SECONDS:
        session.messages = []
        session.updated_at = now

    # Phase 1: generate SQL for this question
    sql = await _generate_sql(question)
    query_results = await _execute_sql(sql, db)

    # Phase 2: answer using the real data + conversation history
    history_text = "\n".join(
        f"{m['role'].capitalize()}: {m['content']}" for m in session.messages
    )
    messages = [
        {
            "role": "system",
            "content": (
                f"You are a concise incident dashboard assistant. "
                f"Today is {now_ke.strftime('%A %d %b %Y, %H:%M')} Kenya time.\n"
                f"Answer in 1–3 short sentences. Be direct and human.\n"
                f"Base your answer ONLY on the query results provided — do not guess."
            ),
        },
        {
            "role": "user",
            "content": (
                f"Question: {question}\n\n"
                f"Query run: {sql}\n"
                f"Results:\n{query_results}\n\n"
                f"{'Previous conversation:\\n' + history_text if history_text else ''}"
                f"\nAnswer the question using only these results."
            ),
        },
    ]

    reply = await _chat(messages)

    # Persist conversation
    new_turns = [
        {"role": "user", "content": question},
        {"role": "assistant", "content": reply},
    ]
    session.messages = (list(session.messages) + new_turns)[-20:]
    session.updated_at = now
    await db.commit()

    return reply or "No answer generated."
