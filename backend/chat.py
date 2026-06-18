import json
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
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "qwen2.5:7b")
OLLAMA_TIMEOUT = float(os.getenv("OLLAMA_TIMEOUT", "60"))
KENYA_TZ = zoneinfo.ZoneInfo("Africa/Nairobi")
_SESSION_TTL_SECONDS = 24 * 3600
_MAX_TOOL_ROUNDS = 4

_DB_SCHEMA = """
Tables (SQLite, all timestamps UTC):

incidents
  id INTEGER PK, group_id TEXT, message_body TEXT, category TEXT,
  severity TEXT ('high'|'medium'|'low'), status TEXT ('new'|'review'|'acknowledged'|'resolved'|'ignored'),
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

_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "query_database",
            "description": (
                "Run a read-only SQL SELECT query against the incidents database. "
                "Use this to answer any question about incidents, groups, trends, "
                "history, counts, or timelines. Always query rather than guessing."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {
                        "type": "string",
                        "description": "A SQL SELECT statement. Read-only only.",
                    }
                },
                "required": ["sql"],
            },
        },
    }
]

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


async def _chat(messages: list[dict], use_tools: bool = True) -> dict:
    payload: dict = {"model": OLLAMA_MODEL, "messages": messages, "stream": False}
    if use_tools:
        payload["tools"] = _TOOLS
    async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT) as client:
        resp = await client.post(f"{OLLAMA_HOST}/api/chat", json=payload)
        resp.raise_for_status()
        return resp.json()


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

    system_msg = {
        "role": "system",
        "content": (
            f"You are a concise incident dashboard assistant. "
            f"Today is {now_ke.strftime('%A %d %b %Y, %H:%M')} Kenya time.\n"
            f"Always call query_database to look up facts — never guess or make up data.\n"
            f"Reply in 1–3 short sentences after getting query results. "
            f"Be direct and human. No bullet points unless listing multiple items.\n\n"
            f"Database schema:\n{_DB_SCHEMA}"
        ),
    }

    messages: list[dict] = [system_msg] + list(session.messages) + [
        {"role": "user", "content": question}
    ]

    # Agentic tool-call loop
    for _ in range(_MAX_TOOL_ROUNDS):
        data = await _chat(messages)
        msg = data.get("message", {})
        tool_calls = msg.get("tool_calls") or []

        if not tool_calls:
            # Final text answer
            reply = (msg.get("content") or "").strip()
            break

        # Execute each tool call and append results
        messages.append({"role": "assistant", "content": msg.get("content", ""), "tool_calls": tool_calls})
        for call in tool_calls:
            fn = call.get("function", {})
            sql = ""
            args = fn.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except Exception:
                    args = {}
            sql = args.get("sql", "")
            tool_result = await _execute_sql(sql, db)
            messages.append({
                "role": "tool",
                "content": tool_result,
            })
    else:
        # Hit round limit — ask for a plain-text summary without further tool calls
        messages.append({"role": "user", "content": "Summarise what you found in one sentence."})
        data = await _chat(messages, use_tools=False)
        reply = (data.get("message", {}).get("content") or "").strip()

    # Persist conversation (keep last 20 messages, skip system and tool messages)
    new_turns = [
        {"role": "user", "content": question},
        {"role": "assistant", "content": reply},
    ]
    session.messages = (list(session.messages) + new_turns)[-20:]
    session.updated_at = now
    await db.commit()

    return reply or "No answer generated."
