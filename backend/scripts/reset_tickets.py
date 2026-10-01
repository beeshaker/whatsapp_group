"""One-off HARD RESET of a client's ticket data, ahead of rebuilding it from
WhatsApp history with scripts/backfill_history.py --from-start.

Deletes every row in incidents, incident_updates, incident_media,
incident_status_history and audit_log, plus the media files those rows point
to. Keeps configuration: users, group access, admin profiles and
subscriptions, categories, and chat sessions.

There is no undo. Take a database backup first (see docs/vps-architecture.md).

Usage (from inside the backend container, cwd=backend/):
    python scripts/reset_tickets.py                                  # dry run: counts only
    python scripts/reset_tickets.py --apply --confirm <subdomain>    # delete

--confirm must match this container's CLIENT_SUBDOMAIN, so the command can't
be pasted into the wrong client's container by accident.
"""
import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import delete, func, select

from database import AsyncSessionLocal
from media import MEDIA_DIR
from models import AuditLog, Incident, IncidentMedia, IncidentStatusHistory, IncidentUpdate

# Children before parents (foreign keys).
_TABLES = [IncidentMedia, IncidentStatusHistory, IncidentUpdate, AuditLog, Incident]


def _inside_media_dir(file_path: str) -> bool:
    root = os.path.realpath(MEDIA_DIR)
    return os.path.realpath(file_path).startswith(root + os.sep)


async def run(apply: bool, confirm: str | None = None) -> dict:
    subdomain = os.getenv("CLIENT_SUBDOMAIN", "")
    if apply and (not subdomain or confirm != subdomain):
        raise SystemExit(
            f"Refusing to delete: --confirm must equal CLIENT_SUBDOMAIN ({subdomain or 'unset'!s})."
        )

    async with AsyncSessionLocal() as db:
        counts = {m.__tablename__: await db.scalar(select(func.count()).select_from(m)) for m in _TABLES}
        file_paths = (await db.scalars(select(IncidentMedia.file_path))).all()
        print(f"Client: {subdomain or '(CLIENT_SUBDOMAIN unset)'}")
        for table, n in counts.items():
            print(f"  {table}: {n} rows")
        print(f"  media files: {len(file_paths)}")

        if not apply:
            print("Dry run. Nothing was deleted. Re-run with --apply --confirm <subdomain>.")
            return counts

        for model in _TABLES:
            await db.execute(delete(model))
        await db.commit()

    removed = 0
    for file_path in file_paths:
        if not _inside_media_dir(file_path):
            print(f"  skipped file outside {MEDIA_DIR}: {file_path}")
            continue
        try:
            os.remove(file_path)
            removed += 1
        except FileNotFoundError:
            pass
    print(f"Deleted all ticket rows and {removed} media files.")
    return counts


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="actually delete (default is a dry run)")
    parser.add_argument("--confirm", help="this client's subdomain, required with --apply")
    args = parser.parse_args()
    asyncio.run(run(args.apply, args.confirm))
