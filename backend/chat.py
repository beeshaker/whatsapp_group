import logging
import os
import zoneinfo
from datetime import datetime, timedelta, timezone

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import ChatSession, Incident, IncidentStatusHistory

logger = logging.getLogger(__name__)

OLLAMA_HOST = os.getenv("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2")
OLLAMA_TIMEOUT = float(os.getenv("OLLAMA_TIMEOUT", "10"))
KENYA_TZ = zoneinfo.ZoneInfo("Africa/Nairobi")
_SESSION_TTL_SECONDS = 24 * 3600


async def _call_ollama(prompt: str) -> str:
    async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT) as client:
        response = await client.post(
            f"{OLLAMA_HOST}/api/generate",
            json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
        )
        response.raise_for_status()
        return response.json().get("response", "").strip()


async def _build_context(db: AsyncSession) -> str:
    now = datetime.now(timezone.utc)
    today_ke = now.astimezone(KENYA_TZ).replace(hour=0, minute=0, second=0, microsecond=0)

    open_result = await db.execute(
        select(Incident).where(~Incident.status.in_(["resolved", "ignored"]))
    )
    open_incidents = open_result.scalars().all()

    resolved_today_result = await db.execute(
        select(IncidentStatusHistory.incident_id, Incident.group_id)
        .join(Incident, IncidentStatusHistory.incident_id == Incident.id)
        .where(IncidentStatusHistory.to_status == "resolved")
        .where(IncidentStatusHistory.changed_at >= today_ke)
    )
    resolved_today_by_group: dict[str, int] = {}
    for _, gid in resolved_today_result.all():
        resolved_today_by_group[gid] = resolved_today_by_group.get(gid, 0) + 1

    by_group: dict[str, list] = {}
    for inc in open_incidents:
        by_group.setdefault(inc.group_id, []).append(inc)

    all_groups = sorted(set(by_group.keys()) | set(resolved_today_by_group.keys()))
    lines = ["Groups:"]
    for gid in all_groups:
        incs = by_group.get(gid, [])
        lines.append(
            f"- {gid}: {len(incs)} open | "
            f"{resolved_today_by_group.get(gid, 0)} resolved today | "
            f"{sum(1 for i in incs if i.severity == 'high')} high / "
            f"{sum(1 for i in incs if i.severity == 'medium')} medium / "
            f"{sum(1 for i in incs if i.severity == 'low')} low"
        )

    high_result = await db.execute(
        select(Incident)
        .where(Incident.severity == "high")
        .where(~Incident.status.in_(["resolved", "ignored"]))
        .order_by(Incident.received_at.desc())
        .limit(5)
    )
    high_incidents = high_result.scalars().all()
    lines.append("\nHigh-severity open incidents (newest 5):")
    for inc in high_incidents:
        age_days = (now - inc.received_at).days
        lines.append(f"- [#{inc.id}] {inc.message_body[:80]} ({inc.group_id}, {age_days}d old)")

    return "\n".join(lines)


async def answer_query(question: str, session_key: str, db: AsyncSession) -> str:
    now = datetime.now(timezone.utc)

    result = await db.execute(
        select(ChatSession).where(ChatSession.session_key == session_key)
    )
    session = result.scalar_one_or_none()

    if session is None:
        session = ChatSession(session_key=session_key, messages=[], updated_at=now)
        db.add(session)
        await db.flush()
    elif (now - session.updated_at.replace(tzinfo=session.updated_at.tzinfo or timezone.utc)).total_seconds() > _SESSION_TTL_SECONDS:
        session.messages = []
        session.updated_at = now

    context = await _build_context(db)
    now_ke = now.astimezone(KENYA_TZ)
    history_text = "\n".join(
        f"{m['role'].capitalize()}: {m['content']}" for m in session.messages
    )

    prompt = (
        f"System: You are a read-only incident management assistant for a property operations team.\n"
        f"Today is {now_ke.strftime('%Y-%m-%d')} ({now_ke.strftime('%A')}), timezone Africa/Nairobi.\n\n"
        f"Current incident data:\n{context}\n\n"
        f"Answer questions concisely. Never suggest actions or pretend you can change data.\n"
        f"If asked about something not in the data above, say so clearly.\n\n"
        f"Conversation so far:\n{history_text}\n\n"
        f"User: {question}"
    )

    reply = await _call_ollama(prompt)

    session.messages = (session.messages + [
        {"role": "user", "content": question},
        {"role": "assistant", "content": reply},
    ])[-20:]
    session.updated_at = now
    await db.commit()

    return reply
