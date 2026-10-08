"""Who sent what: display names for WhatsApp sender phone numbers.

Names are written through to incidents/incident_updates.reporter_name, so
every page, report and export shows them without its own lookup. A manual
name (Contacts page) replaces the name on every row for that phone; a name
learned from WhatsApp only fills rows still marked "Unknown" and never
replaces a manual one.
"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import or_, update
from sqlalchemy.ext.asyncio import AsyncSession

from models import Contact, Incident, IncidentUpdate

UNKNOWN = "Unknown"
MANUAL = "manual"
WHATSAPP = "whatsapp"
_MAX_NAME = 100


def looks_like_lid(phone: Optional[str]) -> bool:
    """WhatsApp's opaque linked-device ids are 14+ digits; real E.164
    numbers are at most 15 but Kenyan ones (the only ones here) are 12."""
    return bool(phone) and phone.isdigit() and len(phone) >= 14


def _clean(name: Optional[str]) -> Optional[str]:
    name = (name or "").strip()[:_MAX_NAME]
    return name or None


async def _rewrite_rows(db: AsyncSession, phone: str, name: str, only_unknown: bool) -> int:
    changed = 0
    for model in (Incident, IncidentUpdate):
        stmt = update(model).where(model.reporter_phone == phone).values(reporter_name=name)
        if only_unknown:
            stmt = stmt.where(or_(model.reporter_name == UNKNOWN, model.reporter_name.is_(None)))
        else:
            stmt = stmt.where(or_(model.reporter_name != name, model.reporter_name.is_(None)))
        changed += (await db.execute(stmt.execution_options(synchronize_session=False))).rowcount or 0
    return changed


async def resolve_reporter_name(db: AsyncSession, phone: Optional[str], notify_name: Optional[str]) -> str:
    """Name to store on a new ticket/update from `phone`."""
    contact = await db.get(Contact, phone) if phone else None
    if contact and contact.source == MANUAL:
        return contact.name
    return _clean(notify_name) or (contact.name if contact else None) or UNKNOWN


async def learn(db: AsyncSession, phone: Optional[str], name: Optional[str]) -> bool:
    """Record a WhatsApp-provided name for `phone` and fill its Unknown rows.
    No-op for manual contacts or when nothing changed. Commits when it writes."""
    name = _clean(name)
    if not phone or not name or name == UNKNOWN:
        return False
    contact = await db.get(Contact, phone)
    if contact and (contact.source == MANUAL or contact.name == name):
        return False
    now = datetime.now(timezone.utc)
    if contact:
        contact.name, contact.updated_at, contact.updated_by = name, now, None
    else:
        db.add(Contact(phone=phone, name=name, source=WHATSAPP, updated_at=now))
    await _rewrite_rows(db, phone, name, only_unknown=True)
    await db.commit()
    return True


async def set_manual_name(db: AsyncSession, phone: str, name: Optional[str], actor: str) -> int:
    """Set (or with an empty name, clear) an admin-chosen name. Returns the
    number of ticket/update rows renamed. The caller commits."""
    name = _clean(name)
    contact = await db.get(Contact, phone)
    if name is None:
        if contact and contact.source == MANUAL:
            await db.delete(contact)
        return 0
    now = datetime.now(timezone.utc)
    if contact:
        contact.name, contact.source, contact.updated_at, contact.updated_by = name, MANUAL, now, actor
    else:
        db.add(Contact(phone=phone, name=name, source=MANUAL, updated_at=now, updated_by=actor))
    return await _rewrite_rows(db, phone, name, only_unknown=False)
