"""One-off backfill: fix "Unknown" senders and hidden (@lid) sender numbers on
tickets and updates already in the database.

Run after deploying the OpenWA contact directory, once it has had a few minutes
connected to learn the groups' members. Two passes:

1. History: each ticket/update whose message id is in OpenWA's saved history
   gets the sender's real phone number (in place of an opaque @lid id) and,
   if still "Unknown", the sender's WhatsApp name. OpenWA resolves both from
   its contact directory when the history is read.
2. Contacts: every name the WhatsApp session knows fills the remaining
   "Unknown" rows for that phone (the same as the Contacts page's
   "Sync names from WhatsApp" button).

Names set by an admin on the Contacts page are never overwritten.

Usage (from inside the backend container, cwd=backend/):
    python scripts/backfill_reporter_identity.py            # dry run, no writes
    python scripts/backfill_reporter_identity.py --apply    # write changes
"""
import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import func, select

import contacts
import whatsapp
from database import AsyncSessionLocal
from models import AuditLog, Contact, Incident, IncidentUpdate
from scripts.backfill_history import fetch_history


def _sender_by_message_id(history: list[dict]) -> dict[str, tuple[str, str]]:
    senders = {}
    for m in history:
        phone = (m.get("author") or "").split("@")[0].strip()
        if m.get("id") and m.get("isGroup") and phone:
            senders[m["id"]] = (phone, (m.get("notifyName") or "").strip())
    return senders


async def _fix_from_history(db, senders: dict[str, tuple[str, str]], apply: bool) -> int:
    manual = {c.phone: c.name for c in (await db.execute(
        select(Contact).where(Contact.source == contacts.MANUAL)
    )).scalars().all()}
    now = datetime.now(timezone.utc)
    changed = 0
    for model in (Incident, IncidentUpdate):
        rows = (await db.execute(
            select(model).where(model.message_id.in_(list(senders)))
        )).scalars().all() if senders else []
        for row in rows:
            phone, name = senders[row.message_id]
            changes = []
            if (
                row.reporter_phone != phone
                and not contacts.looks_like_lid(phone)
                and (row.reporter_phone is None or contacts.looks_like_lid(row.reporter_phone))
            ):
                changes.append(f"reporter_phone: {row.reporter_phone} → {phone}")
                row.reporter_phone = phone
            new_name = manual.get(row.reporter_phone) or (
                name if row.reporter_name in (None, contacts.UNKNOWN) else None
            )
            if new_name and new_name != row.reporter_name:
                changes.append(f"reporter_name: {row.reporter_name} → {new_name}")
                row.reporter_name = new_name
            if not changes:
                continue
            changed += 1
            label = f"incident #{row.id}" if model is Incident else f"update #{row.id} (incident #{row.incident_id})"
            print(f"{label}: {'; '.join(changes)}")
            if apply:
                db.add(AuditLog(
                    username="system:backfill_reporter_identity",
                    action="reporter_backfill",
                    incident_id=row.id if model is Incident else row.incident_id,
                    detail="; ".join(changes),
                    created_at=now,
                ))
    if apply:
        await db.commit()
    else:
        await db.rollback()
    return changed


async def _unknown_count(db) -> int:
    total = 0
    for model in (Incident, IncidentUpdate):
        total += await db.scalar(
            select(func.count()).select_from(model)
            .where((model.reporter_name == contacts.UNKNOWN) | model.reporter_name.is_(None))
        )
    return total


async def run(apply: bool) -> dict:
    history = await fetch_history(datetime.fromtimestamp(0, tz=timezone.utc), datetime.now(timezone.utc), None)
    senders = _sender_by_message_id(history)
    print(f"{len(history)} history messages, {len(senders)} with a resolvable group sender")

    async with AsyncSessionLocal() as db:
        unknown_before = await _unknown_count(db)
        from_history = await _fix_from_history(db, senders, apply)

    wa_contacts = await whatsapp.list_contacts() or []
    from_contacts = 0
    if apply:
        async with AsyncSessionLocal() as db:
            for c in wa_contacts:
                phone = (c.get("number") or (c.get("id") or "").split("@")[0]).strip()
                if await contacts.learn(db, phone or None, c.get("name") or c.get("pushName")):
                    from_contacts += 1
            unknown_after = await _unknown_count(db)
    else:
        named = sum(1 for c in wa_contacts if c.get("name") or c.get("pushName"))
        print(f"WhatsApp knows {len(wa_contacts)} contacts ({named} with a name); "
              "their names would fill remaining Unknown rows.")
        unknown_after = None

    summary = {
        "rows_fixed_from_history": from_history,
        "contacts_learned": from_contacts,
        "unknown_before": unknown_before,
        "unknown_after": unknown_after,
    }
    print(f"{'Applied' if apply else 'Dry run'}: {summary}")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="Write changes (default is dry-run)")
    args = parser.parse_args()
    asyncio.run(run(args.apply))


if __name__ == "__main__":
    main()
