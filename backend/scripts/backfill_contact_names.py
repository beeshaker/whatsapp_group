"""One-off backfill: extract contact_name from message_body for incidents
where it's still null, using the same deterministic regex the live
classifier applies to new leads.

Skips any incident whose message_id is shared with another incident row
(a multi-issue WhatsApp message produces multiple sibling incidents that
all store the SAME full message_body) — extracting from the shared text
risks assigning one sibling's name to another, so those are left for
manual review instead of guessing.

Usage (from inside the backend container, cwd=backend/):
    python scripts/backfill_contact_names.py            # dry run, no writes
    python scripts/backfill_contact_names.py --apply    # write changes
"""
import argparse
import asyncio
import os
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import func, select

from database import AsyncSessionLocal
from lead_fields import extract_contact_name
from models import AuditLog, Incident


async def run(apply: bool) -> int:
    changed = 0
    async with AsyncSessionLocal() as db:
        sibling_result = await db.execute(
            select(Incident.message_id)
            .where(Incident.message_id.is_not(None))
            .group_by(Incident.message_id)
            .having(func.count(Incident.id) > 1)
        )
        ambiguous_message_ids = {row[0] for row in sibling_result.all()}

        result = await db.execute(select(Incident).where(Incident.contact_name.is_(None)))
        incidents = result.scalars().all()
        now = datetime.now(timezone.utc)
        skipped_ambiguous = 0
        for incident in incidents:
            if incident.message_id is not None and incident.message_id in ambiguous_message_ids:
                skipped_ambiguous += 1
                continue
            name = extract_contact_name(incident.message_body)
            if name is None:
                continue
            print(f"incident #{incident.id}: None -> {name!r}")
            changed += 1
            if apply:
                incident.contact_name = name
                db.add(incident)
                db.add(AuditLog(
                    username="system:backfill_contact_names",
                    action="contact_name_backfill",
                    incident_id=incident.id,
                    detail=f"contact_name: None → {name}",
                    created_at=now,
                ))
        if apply and changed:
            await db.commit()
        print(
            f"{'Applied' if apply else 'Would apply'} {changed} change(s), "
            f"skipped {skipped_ambiguous} multi-issue-sibling incident(s), "
            f"out of {len(incidents)} null-contact_name incident(s)."
        )
    return changed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Write changes (default is dry-run)")
    args = parser.parse_args()
    asyncio.run(run(args.apply))


if __name__ == "__main__":
    main()
