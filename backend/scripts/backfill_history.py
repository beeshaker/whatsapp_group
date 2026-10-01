"""One-off backfill: turn group messages missed during a WhatsApp outage into
tickets, using the history the phone syncs to OpenWA when the bot is re-linked
on the Baileys engine (GET /api/sessions/:id/history).

Messages go through the same text pipeline as live ingest (_handle_text_ingest:
classifier, multi-issue split, update-vs-new routing) with their ORIGINAL
timestamps, oldest first. Unlike the live webhook it never sends anything to
WhatsApp: no DM/sales replies, no billing forwards, and /commands are skipped.

Safe to re-run: messages already ingested are skipped by message id, and by
(group, exact received_at) for ones captured before the outage under the old
engine's different message ids. Media files aren't fetched; a media message's
caption is ingested as text.

Usage (from inside the backend container, cwd=backend/):
    python scripts/backfill_history.py                        # dry run, no writes
    python scripts/backfill_history.py --apply                # write tickets
    python scripts/backfill_history.py --since 2026-09-20T08:00 --until 2026-10-02T12:00
    python scripts/backfill_history.py --group 120363XXXX@g.us --apply

--since defaults to the newest ticket's received_at (the last message captured
before the drop); --until defaults to now. Naive times are UTC.
"""
import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone
from typing import Optional

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import httpx
from sqlalchemy import func, select

import main
import whatsapp
from database import AsyncSessionLocal
from models import AuditLog, Incident, IncidentUpdate


def _as_utc(dt: datetime) -> datetime:
    # SQLite drops tzinfo on read-back even for DateTime(timezone=True).
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt.astimezone(timezone.utc)


async def _default_since() -> Optional[datetime]:
    async with AsyncSessionLocal() as db:
        latest = await db.scalar(select(func.max(Incident.received_at)))
    return _as_utc(latest) if latest else None


async def fetch_history(since: datetime, until: datetime, group: Optional[str]) -> list[dict]:
    params = {"since": int(since.timestamp()), "until": int(until.timestamp())}
    if group:
        params["chatId"] = group
    async with httpx.AsyncClient(timeout=60.0) as client:
        session_id = await whatsapp._resolve_session_uuid(client)
        r = await client.get(
            f"{whatsapp.OPENWA_URL}/api/sessions/{session_id}/history",
            headers={"X-API-Key": whatsapp.OPENWA_API_KEY},
            params=params,
        )
        r.raise_for_status()
        return r.json()


async def _already_ingested(db, group_id: str, received_at: datetime) -> bool:
    """Same group + same second as an existing ticket or update: almost certainly
    the same message, ingested before the outage under a different message id."""
    incident = await db.scalar(
        select(Incident.id).where(Incident.group_id == group_id, Incident.received_at == received_at).limit(1)
    )
    if incident is not None:
        return True
    update = await db.scalar(
        select(IncidentUpdate.id)
        .join(Incident, IncidentUpdate.incident_id == Incident.id)
        .where(Incident.group_id == group_id, IncidentUpdate.received_at == received_at)
        .limit(1)
    )
    return update is not None


def _select(messages: list[dict], allowed: Optional[list[str]], billing_group: Optional[str]) -> list[dict]:
    """Mirror the live ingest filters for group text (and captioned media)."""
    picked = []
    for m in messages:
        group_id = m.get("chatId") or m.get("from") or ""
        body = (m.get("body") or "").strip()
        if not m.get("isGroup") or m.get("fromMe") or not body or body.startswith("/"):
            continue
        if billing_group and group_id == billing_group:
            continue
        if allowed is not None and group_id not in allowed:
            continue
        picked.append(m)
    return sorted(picked, key=lambda m: m.get("timestamp") or 0)


async def run(
    apply: bool,
    since: Optional[datetime] = None,
    until: Optional[datetime] = None,
    group: Optional[str] = None,
) -> dict:
    since = since or await _default_since()
    if since is None:
        raise SystemExit("No tickets exist yet to infer the outage start from; pass --since.")
    until = until or datetime.now(timezone.utc)
    print(f"Window (UTC): {since.isoformat()} -> {until.isoformat()}" + (f"  group={group}" if group else ""))

    if await main._get_client_billing_status() in ("billing_only", "closed"):
        raise SystemExit("Client is billing_only/closed; live ingest would drop these too. Aborting.")

    history = await fetch_history(since, until, group)
    allowed = await main._get_allowed_ticket_groups()
    candidates = _select(history, allowed, await main._get_billing_group_id())
    group_names = {g["id"]: g["name"] for g in (await whatsapp.list_groups() or [])}
    print(f"{len(history)} messages in window, {len(candidates)} eligible ticket-group messages")

    async with AsyncSessionLocal() as db:
        id_floor = await db.scalar(select(func.max(Incident.id))) or 0
    ingested_ids: list[str] = []

    results: dict[str, int] = {}
    for n, m in enumerate(candidates, 1):
        group_id = m.get("chatId") or m["from"]
        received_at = datetime.fromtimestamp(m["timestamp"], tz=timezone.utc)
        body = m["body"].strip()[:4000]
        reporter_name = (m.get("notifyName") or "").strip() or "Unknown"
        reporter_phone = (m.get("author") or "").split("@")[0].strip() or None
        group_name = group_names.get(group_id) or group_id.split("@")[0]
        print(f"[{n}/{len(candidates)}] {received_at:%Y-%m-%d %H:%M} | {group_name} | {reporter_name} | {body[:70]!r}")
        if not apply:
            continue

        async with AsyncSessionLocal() as db:
            if await _already_ingested(db, group_id, received_at):
                status = "duplicate"
            else:
                outcome = await main._handle_text_ingest(
                    db, group_id, group_name, reporter_name, reporter_phone,
                    body, received_at, m.get("id") or None,
                )
                status = outcome.get("status", "unknown")
                if m.get("id"):
                    ingested_ids.append(m["id"])
        results[status] = results.get(status, 0) + 1
        print(f"    -> {status}")

    if apply:
        # Tag every ticket this run created so they can be found (or undone) later.
        async with AsyncSessionLocal() as db:
            created = (await db.scalars(
                select(Incident.id).where(Incident.id > id_floor, Incident.message_id.in_(ingested_ids))
            )).all() if ingested_ids else []
            now = datetime.now(timezone.utc)
            for incident_id in created:
                db.add(AuditLog(
                    username="system:backfill_history",
                    action="history_backfill",
                    incident_id=incident_id,
                    detail=f"backfilled from history window {since.isoformat()} -> {until.isoformat()}",
                    created_at=now,
                ))
            await db.commit()
        print(f"Done: {results}; {len(created)} new tickets tagged action='history_backfill' in audit_log")
    else:
        print("Dry run. Nothing was written. Re-run with --apply.")
    return results


def _parse_time(value: str) -> datetime:
    return _as_utc(datetime.fromisoformat(value))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write tickets (default is a dry run)")
    parser.add_argument("--since", type=_parse_time, help="window start, ISO time (default: newest ticket)")
    parser.add_argument("--until", type=_parse_time, help="window end, ISO time (default: now)")
    parser.add_argument("--group", help="only this group JID")
    args = parser.parse_args()
    asyncio.run(run(args.apply, args.since, args.until, args.group))
