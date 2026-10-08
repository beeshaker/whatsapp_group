"""Fleet reports (FLEET_PLATE_MODE tenants, e.g. Pixiilive).

All functions take `allowed` from main._get_allowed_groups: None means no
group filter (admins), a list restricts to those group_ids. Bucketing by day
is done in Python in the report timezone so it behaves the same on SQLite
(tests) and Postgres (prod), and fleet ticket volumes are small enough that
pulling the scoped rows is cheap.
"""
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone, tzinfo
from decimal import Decimal
from typing import Optional

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from models import Incident, IncidentCategory, IncidentCost, IncidentStatusHistory, IncidentUpdate

OPEN_EXCLUDED = ("resolved", "ignored")
REPEAT_THRESHOLD = 3
REPEAT_WINDOW_DAYS = 30
_PRIORITIES = ("urgent", "high", "medium", "low")


def _aware(dt: datetime) -> datetime:
    # SQLite round-trips timestamps as naive; they are stored as UTC.
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _scope(q, allowed: Optional[list[str]]):
    return q.where(Incident.group_id.in_(allowed)) if allowed is not None else q


RANGE_CHOICES = ("7", "30", "90", "all")


def normalize_days(days: Optional[str]) -> str:
    return days if days in RANGE_CHOICES else "30"


async def earliest_day(db: AsyncSession, allowed: Optional[list[str]], tz: tzinfo) -> Optional[date]:
    first = (await db.execute(_scope(select(func.min(Incident.received_at)), allowed))).scalar_one()
    return _aware(first).astimezone(tz).date() if first else None


def resolve_range(
    tz: tzinfo,
    days: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    earliest: Optional[date] = None,
) -> tuple[datetime, datetime, date, date]:
    """Returns (start, end, first_day, last_day): start/end are the UTC
    instants of local midnight on first_day and the day after last_day.
    Custom from/to wins when both parse; otherwise `days` ("7", "30", "90",
    default "30") ending today, or "all" from `earliest` (the first ticket)."""
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
        days = normalize_days(days)
        last = today
        if days == "all":
            first = min(earliest or today, today)
        else:
            first = today - timedelta(days=int(days) - 1)
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

    spend = (await db.execute(_scope(
        select(func.coalesce(func.sum(IncidentCost.amount), 0))
        .join(Incident, Incident.id == IncidentCost.incident_id)
        .where(IncidentCost.incurred_on >= first_day)
        .where(IncidentCost.incurred_on <= last_day),
        allowed,
    ))).scalar_one()

    priority_counts = Counter(p for p, _, _ in open_rows)
    category_counts = Counter(c for _, c, _ in open_rows)

    return {
        "open_count": len(open_rows),
        "vehicles_open": len({plate for _, _, plate in open_rows if plate}),
        "open_unassigned": sum(1 for _, _, plate in open_rows if not plate),
        "new_in_range": len(received),
        "resolved_in_range": len(resolved),
        "repair_spend": float(spend or 0),
        "backlog_by_priority": [(p, priority_counts.get(p, 0)) for p in _PRIORITIES],
        "backlog_by_category": category_counts.most_common(),
        "trend": _trend(received, resolved, first_day, last_day, tz),
    }


def _trend(received, resolved, first_day: date, last_day: date, tz: tzinfo) -> dict:
    """Daily buckets up to 31 days, weekly up to ~6 months, 4-weekly beyond;
    buckets start at first_day."""
    span = (last_day - first_day).days + 1
    step = 1 if span <= 31 else 7 if span <= 182 else 28
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
    granularity = {1: "day", 7: "week", 28: "4 weeks"}[step]
    return {"granularity": granularity, "buckets": buckets}


async def plate_table(
    db: AsyncSession,
    allowed: Optional[list[str]],
    start: datetime,
    end: datetime,
    now: datetime,
    first_day: Optional[date] = None,
    last_day: Optional[date] = None,
) -> list[dict]:
    """One row per plate reported in [start, end) (pass the all-time range
    for every plate). `tickets`, `reports` and `top_category` cover that
    range; `open`, `last_reported` and the repeat flag do not.

    In fleet mode a new message about a plate that already has an open ticket
    becomes a follow-up on that ticket, not a new ticket, so a bike's ticket
    count understates how often it is reported. `reports` counts tickets plus
    follow-up messages, and the repeat flag uses reports in the last
    REPEAT_WINDOW_DAYS.

    `cost` is repair spend with incurred_on in [first_day, last_day]; a
    plate with spend in range but no reports is still listed."""
    rows = (await db.execute(_scope(
        select(Incident.vehicle_plate, Incident.category, Incident.status, Incident.received_at)
        .where(Incident.vehicle_plate.isnot(None)),
        allowed,
    ))).all()
    update_rows = (await db.execute(_scope(
        select(Incident.vehicle_plate, IncidentUpdate.received_at)
        .join(Incident, Incident.id == IncidentUpdate.incident_id)
        .where(Incident.vehicle_plate.isnot(None)),
        allowed,
    ))).all()

    repeat_since = _aware(now) - timedelta(days=REPEAT_WINDOW_DAYS)
    plates: dict[str, dict] = {}

    def entry(plate: str) -> dict:
        return plates.setdefault(plate, {
            "plate": plate, "tickets": 0, "reports": 0, "open": 0, "recent": 0,
            "last_reported": None, "categories": Counter(), "all_categories": Counter(),
        })

    def report(p: dict, at: datetime) -> bool:
        in_range = start <= at < end
        if in_range:
            p["reports"] += 1
        if at >= repeat_since:
            p["recent"] += 1
        if p["last_reported"] is None or at > p["last_reported"]:
            p["last_reported"] = at
        return in_range

    for plate, category, status, received_at in rows:
        p = entry(plate)
        p["all_categories"][category] += 1
        if status not in OPEN_EXCLUDED:
            p["open"] += 1
        if report(p, _aware(received_at)):
            p["tickets"] += 1
            p["categories"][category] += 1
    for plate, received_at in update_rows:
        report(entry(plate), _aware(received_at))

    costs: dict[str, Decimal] = {}
    if first_day is not None and last_day is not None:
        cost_rows = (await db.execute(_scope(
            select(Incident.vehicle_plate, func.sum(IncidentCost.amount))
            .join(Incident, Incident.id == IncidentCost.incident_id)
            .where(Incident.vehicle_plate.isnot(None))
            .where(IncidentCost.incurred_on >= first_day)
            .where(IncidentCost.incurred_on <= last_day)
            .group_by(Incident.vehicle_plate),
            allowed,
        ))).all()
        costs = {plate: Decimal(total or 0) for plate, total in cost_rows}

    result = []
    for p in plates.values():
        cost = costs.get(p["plate"], Decimal(0))
        if not p["reports"] and not cost:
            continue
        # A bike reported in range only via follow-ups has no in-range
        # ticket; fall back to its ticket's category.
        top = p["categories"].most_common(1) or p["all_categories"].most_common(1)
        result.append({
            "plate": p["plate"],
            "tickets": p["tickets"],
            "reports": p["reports"],
            "open": p["open"],
            "recent": p["recent"],
            "repeat": p["recent"] >= REPEAT_THRESHOLD,
            "last_reported": p["last_reported"],
            "top_category": top[0][0],
            "cost": float(cost),
        })
    result.sort(key=lambda r: (-r["recent"], -r["reports"], r["plate"]))
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
    cost_items = (await db.execute(
        select(IncidentCost)
        .where(IncidentCost.incident_id.in_(ids))
        .order_by(IncidentCost.incurred_on.asc(), IncidentCost.id.asc())
    )).scalars().all()
    costs_by_incident: dict[int, list] = defaultdict(list)
    for c in cost_items:
        costs_by_incident[c.incident_id].append(c)

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
            "costs": costs_by_incident[i.id],
            "cost_total": float(sum((c.amount for c in costs_by_incident[i.id]), Decimal(0))),
        }
        for i in incidents
    ]


PERIOD_KINDS = ("new", "resolved")


async def period_tickets(
    db: AsyncSession,
    allowed: Optional[list[str]],
    start: datetime,
    end: datetime,
    kind: str,
) -> list[dict]:
    """The tickets behind the "New" / "Resolved" headline numbers, newest
    first. `at` is when the ticket arrived, or for "resolved" when it was
    resolved. Like the headline count, "resolved" lists each resolution, so a
    ticket resolved, reopened and resolved again in range appears twice."""
    if kind == "new":
        q = (
            select(Incident, Incident.received_at)
            .where(Incident.received_at >= start)
            .where(Incident.received_at < end)
            .order_by(Incident.received_at.desc())
        )
    else:
        q = (
            select(Incident, IncidentStatusHistory.changed_at)
            .join(IncidentStatusHistory, IncidentStatusHistory.incident_id == Incident.id)
            .where(IncidentStatusHistory.to_status == "resolved")
            .where(IncidentStatusHistory.changed_at >= start)
            .where(IncidentStatusHistory.changed_at < end)
            .order_by(IncidentStatusHistory.changed_at.desc())
        )
    rows = (await db.execute(_scope(q, allowed))).all()
    return [{"incident": i, "at": _aware(at)} for i, at in rows]


EXPORT_COLUMNS = [
    "id", "received_at", "vehicle_plate", "category", "priority", "status",
    "reporter_name", "reporter_phone", "group", "resolved_at", "repair_cost", "message",
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
    cost_sq = (
        select(func.sum(IncidentCost.amount))
        .where(IncidentCost.incident_id == Incident.id)
        .correlate(Incident)
        .scalar_subquery()
    )
    q = (
        select(Incident, resolved_at_sq.label("resolved_at"), cost_sq.label("repair_cost"))
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
            fmt(resolved_at) if i.status == "resolved" else "",
            f"{Decimal(repair_cost):.2f}" if repair_cost is not None else "", i.message_body,
        ]
        for i, resolved_at, repair_cost in rows
    ]


def cost_breakdown(timeline: list[dict]) -> list[tuple[str, float]]:
    """Total spend per cost_type across a plate_timeline, largest first."""
    totals: Counter = Counter()
    for t in timeline:
        for c in t["costs"]:
            totals[c.cost_type] += float(c.amount)
    return totals.most_common()


COST_EXPORT_COLUMNS = [
    "cost_id", "incurred_on", "vehicle_plate", "ticket_id", "category", "cost_type",
    "description", "vendor", "amount", "currency", "receipt", "recorded_by",
]


async def cost_export_rows(
    db: AsyncSession,
    allowed: Optional[list[str]],
    first_day: date,
    last_day: date,
    plate: Optional[str] = None,
) -> list[list]:
    """One row per cost item with incurred_on in [first_day, last_day]."""
    q = (
        select(IncidentCost, Incident)
        .join(Incident, Incident.id == IncidentCost.incident_id)
        .where(IncidentCost.incurred_on >= first_day)
        .where(IncidentCost.incurred_on <= last_day)
        .order_by(IncidentCost.incurred_on.asc(), IncidentCost.id.asc())
    )
    if plate:
        q = q.where(Incident.vehicle_plate == plate)
    rows = (await db.execute(_scope(q, allowed))).all()
    return [
        [
            c.id, c.incurred_on.isoformat(), i.vehicle_plate or "", i.id, i.category, c.cost_type,
            c.description or "", c.vendor or "", f"{c.amount:.2f}", c.currency,
            "yes" if c.receipt_path else "no", c.created_by,
        ]
        for c, i in rows
    ]
