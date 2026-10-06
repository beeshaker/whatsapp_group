"""Fleet reports (FLEET_PLATE_MODE tenants, e.g. Pixiilive).

All functions take `allowed` from main._get_allowed_groups: None means no
group filter (admins), a list restricts to those group_ids. Bucketing by day
is done in Python in the report timezone so it behaves the same on SQLite
(tests) and Postgres (prod), and fleet ticket volumes are small enough that
pulling the scoped rows is cheap.
"""
from collections import Counter
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import Incident, IncidentCategory, IncidentStatusHistory, IncidentUpdate

OPEN_EXCLUDED = ("resolved", "ignored")
REPEAT_THRESHOLD = 3
REPEAT_WINDOW_DAYS = 30
_PRIORITIES = ("urgent", "high", "medium", "low")


def _aware(dt: datetime) -> datetime:
    # SQLite round-trips timestamps as naive; they are stored as UTC.
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _scope(q, allowed: Optional[list[str]]):
    return q.where(Incident.group_id.in_(allowed)) if allowed is not None else q


def resolve_range(
    tz: tzinfo,
    days: Optional[int] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
) -> tuple[datetime, datetime, date, date]:
    """Returns (start, end, first_day, last_day): start/end are the UTC
    instants of local midnight on first_day and the day after last_day.
    Custom from/to wins when both parse; otherwise the last `days` days
    (default 30) ending today."""
    today = datetime.now(tz).date()
    first = last = None
    if date_from and date_to:
        try:
            first, last = date.fromisoformat(date_from), date.fromisoformat(date_to)
        except ValueError:
            first = last = None
        if first and last and first > last:
            first, last = last, first
    if first is None:
        span = days if days in (7, 30, 90) else 30
        last = today
        first = today - timedelta(days=span - 1)
    # Bounds go to the DB as UTC: SQLite drops the offset of an aware
    # parameter rather than converting it, and rows are stored as UTC.
    start = datetime.combine(first, datetime.min.time(), tzinfo=tz).astimezone(timezone.utc)
    end = datetime.combine(last + timedelta(days=1), datetime.min.time(), tzinfo=tz).astimezone(timezone.utc)
    return start, end, first, last


async def category_labels(db: AsyncSession) -> dict[str, str]:
    rows = (await db.execute(select(IncidentCategory.slug, IncidentCategory.label))).all()
    return {slug: label for slug, label in rows}


async def fleet_overview(
    db: AsyncSession,
    allowed: Optional[list[str]],
    start: datetime,
    end: datetime,
    first_day: date,
    last_day: date,
    tz: tzinfo,
) -> dict:
    open_rows = (await db.execute(_scope(
        select(Incident.priority, Incident.category, Incident.vehicle_plate)
        .where(~Incident.status.in_(OPEN_EXCLUDED)),
        allowed,
    ))).all()

    received = (await db.execute(_scope(
        select(Incident.received_at)
        .where(Incident.received_at >= start)
        .where(Incident.received_at < end),
        allowed,
    ))).scalars().all()

    resolved_q = (
        select(IncidentStatusHistory.changed_at)
        .join(Incident, Incident.id == IncidentStatusHistory.incident_id)
        .where(IncidentStatusHistory.to_status == "resolved")
        .where(IncidentStatusHistory.changed_at >= start)
        .where(IncidentStatusHistory.changed_at < end)
    )
    resolved = (await db.execute(_scope(resolved_q, allowed))).scalars().all()

    priority_counts = Counter(p for p, _, _ in open_rows)
    category_counts = Counter(c for _, c, _ in open_rows)

    return {
        "open_count": len(open_rows),
        "vehicles_open": len({plate for _, _, plate in open_rows if plate}),
        "open_unassigned": sum(1 for _, _, plate in open_rows if not plate),
        "new_in_range": len(received),
        "resolved_in_range": len(resolved),
        "backlog_by_priority": [(p, priority_counts.get(p, 0)) for p in _PRIORITIES],
        "backlog_by_category": category_counts.most_common(),
        "trend": _trend(received, resolved, first_day, last_day, tz),
    }


def _trend(received, resolved, first_day: date, last_day: date, tz: tzinfo) -> dict:
    """Daily buckets up to 31 days, weekly (7-day, starting at first_day) beyond."""
    span = (last_day - first_day).days + 1
    step = 1 if span <= 31 else 7
    n = (span + step - 1) // step

    def bucket(dt: datetime) -> int:
        return ((_aware(dt).astimezone(tz).date() - first_day).days) // step

    new_counts, resolved_counts = [0] * n, [0] * n
    for dt in received:
        i = bucket(dt)
        if 0 <= i < n:
            new_counts[i] += 1
    for dt in resolved:
        i = bucket(dt)
        if 0 <= i < n:
            resolved_counts[i] += 1

    buckets = []
    for i in range(n):
        b_start = first_day + timedelta(days=i * step)
        b_end = min(b_start + timedelta(days=step - 1), last_day)
        buckets.append({
            "start": b_start.isoformat(),
            "end": b_end.isoformat(),
            "new": new_counts[i],
            "resolved": resolved_counts[i],
        })
    return {"granularity": "day" if step == 1 else "week", "buckets": buckets}


async def plate_table(
    db: AsyncSession,
    allowed: Optional[list[str]],
    start: datetime,
    end: datetime,
    now: datetime,
) -> list[dict]:
    """One row per plate that has any ticket. `tickets` and `top_category`
    use the selected range; `open`, `last_reported` and the repeat flag
    (REPEAT_THRESHOLD+ tickets in the last REPEAT_WINDOW_DAYS) do not."""
    rows = (await db.execute(_scope(
        select(Incident.vehicle_plate, Incident.category, Incident.status, Incident.received_at)
        .where(Incident.vehicle_plate.isnot(None)),
        allowed,
    ))).all()

    repeat_since = _aware(now) - timedelta(days=REPEAT_WINDOW_DAYS)
    plates: dict[str, dict] = {}
    for plate, category, status, received_at in rows:
        received_at = _aware(received_at)
        p = plates.setdefault(plate, {
            "plate": plate, "tickets": 0, "open": 0, "recent": 0,
            "last_reported": None, "categories": Counter(),
        })
        if status not in OPEN_EXCLUDED:
            p["open"] += 1
        if received_at >= repeat_since:
            p["recent"] += 1
        if p["last_reported"] is None or received_at > p["last_reported"]:
            p["last_reported"] = received_at
        if start <= received_at < end:
            p["tickets"] += 1
            p["categories"][category] += 1

    result = []
    for p in plates.values():
        top = p["categories"].most_common(1)
        result.append({
            "plate": p["plate"],
            "tickets": p["tickets"],
            "open": p["open"],
            "recent": p["recent"],
            "repeat": p["recent"] >= REPEAT_THRESHOLD,
            "last_reported": p["last_reported"],
            "top_category": top[0][0] if top else None,
        })
    result.sort(key=lambda r: (-r["open"], -r["tickets"], r["plate"]))
    return result


async def plate_timeline(
    db: AsyncSession, allowed: Optional[list[str]], plate: str
) -> list[dict]:
    """Every ticket for the plate, newest first, each with its follow-up
    messages and status changes merged into one chronological event list."""
    incidents = (await db.execute(_scope(
        select(Incident)
        .where(Incident.vehicle_plate == plate)
        .order_by(Incident.received_at.desc()),
        allowed,
    ))).scalars().all()
    if not incidents:
        return []
    ids = [i.id for i in incidents]

    updates = (await db.execute(
        select(IncidentUpdate).where(IncidentUpdate.incident_id.in_(ids))
    )).scalars().all()
    changes = (await db.execute(
        select(IncidentStatusHistory).where(IncidentStatusHistory.incident_id.in_(ids))
    )).scalars().all()

    events: dict[int, list[dict]] = {i: [] for i in ids}
    for u in updates:
        events[u.incident_id].append({
            "at": _aware(u.received_at), "kind": "update",
            "who": u.reporter_name, "text": u.message_body,
        })
    for c in changes:
        events[c.incident_id].append({
            "at": _aware(c.changed_at), "kind": "status",
            "who": c.changed_by, "text": f"{c.from_status or '—'} → {c.to_status}",
        })

    return [
        {
            "incident": i,
            "received_at": _aware(i.received_at),
            "events": sorted(events[i.id], key=lambda e: e["at"]),
        }
        for i in incidents
    ]


EXPORT_COLUMNS = [
    "id", "received_at", "vehicle_plate", "category", "priority", "status",
    "reporter_name", "reporter_phone", "group", "resolved_at", "message",
]


async def export_rows(
    db: AsyncSession,
    allowed: Optional[list[str]],
    start: datetime,
    end: datetime,
    tz: tzinfo,
    plate: Optional[str] = None,
    category: Optional[str] = None,
    status: Optional[str] = None,
) -> list[list]:
    resolved_at_sq = (
        select(func.max(IncidentStatusHistory.changed_at))
        .where(IncidentStatusHistory.incident_id == Incident.id)
        .where(IncidentStatusHistory.to_status == "resolved")
        .correlate(Incident)
        .scalar_subquery()
    )
    q = (
        select(Incident, resolved_at_sq.label("resolved_at"))
        .where(Incident.received_at >= start)
        .where(Incident.received_at < end)
        .order_by(Incident.received_at.asc())
    )
    if plate:
        q = q.where(Incident.vehicle_plate == plate)
    if category:
        q = q.where(Incident.category == category)
    if status:
        q = q.where(Incident.status == status)
    rows = (await db.execute(_scope(q, allowed))).all()

    def fmt(dt: Optional[datetime]) -> str:
        return _aware(dt).astimezone(tz).strftime("%Y-%m-%d %H:%M") if dt else ""

    # Only the ticket's resolved_at when it is currently resolved: a ticket
    # resolved then reopened should not show a stale resolution time.
    return [
        [
            i.id, fmt(i.received_at), i.vehicle_plate or "", i.category, i.priority,
            i.status, i.reporter_name or "", i.reporter_phone or "", i.property_name,
            fmt(resolved_at) if i.status == "resolved" else "", i.message_body,
        ]
        for i, resolved_at in rows
    ]
