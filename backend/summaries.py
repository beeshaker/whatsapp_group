import zoneinfo
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from models import Incident, IncidentStatusHistory

KENYA_TZ = zoneinfo.ZoneInfo("Africa/Nairobi")


def window_for_date(d: date) -> tuple[datetime, datetime, str]:
    """Returns (start, end, period_label) for the summary window of the given Kenya date.

    Monday → preceding Saturday 00:00 to Sunday 23:59 Kenya time.
    Any other day → midnight to midnight of that day.
    """
    if d.weekday() == 0:  # Monday
        sat = d - timedelta(days=2)
        sun = d - timedelta(days=1)
        start = datetime(sat.year, sat.month, sat.day, 0, 0, 0, tzinfo=KENYA_TZ)
        end = datetime(sun.year, sun.month, sun.day, 23, 59, 59, 999999, tzinfo=KENYA_TZ)
        label = f"Weekend {sat.strftime('%d')}–{sun.strftime('%d %b')}"
    else:
        start = datetime(d.year, d.month, d.day, 0, 0, 0, tzinfo=KENYA_TZ)
        end = datetime(d.year, d.month, d.day, 23, 59, 59, 999999, tzinfo=KENYA_TZ)
        label = start.strftime("%A %d %b")
    return start, end, label


async def build_summary(
    group_id: str,
    date_from: datetime,
    date_to: datetime,
    period_label: str,
    db: AsyncSession,
) -> dict:
    new_result = await db.execute(
        select(Incident)
        .where(Incident.group_id == group_id)
        .where(Incident.received_at >= date_from)
        .where(Incident.received_at <= date_to)
        .order_by(Incident.received_at.asc())
    )
    new_incidents = new_result.scalars().all()

    resolved_result = await db.execute(
        select(IncidentStatusHistory.incident_id)
        .join(Incident, IncidentStatusHistory.incident_id == Incident.id)
        .where(Incident.group_id == group_id)
        .where(IncidentStatusHistory.to_status == "resolved")
        .where(IncidentStatusHistory.changed_at >= date_from)
        .where(IncidentStatusHistory.changed_at <= date_to)
    )
    resolved_count = len(resolved_result.all())

    open_result = await db.execute(
        select(Incident)
        .where(Incident.group_id == group_id)
        .where(~Incident.status.in_(["resolved", "ignored"]))
    )
    open_incidents = open_result.scalars().all()

    return {
        "group_id": group_id,
        "period_label": period_label,
        "new_count": len(new_incidents),
        "resolved_count": resolved_count,
        "still_open_count": len(open_incidents),
        "new_incidents": [
            {
                "id": i.id,
                "title": i.message_body[:80],
                "priority": i.priority,
                "status": i.status,
            }
            for i in new_incidents
        ],
        "open_backlog": {
            "urgent": sum(1 for i in open_incidents if i.priority == "urgent"),
            "high": sum(1 for i in open_incidents if i.priority == "high"),
            "medium": sum(1 for i in open_incidents if i.priority == "medium"),
            "low": sum(1 for i in open_incidents if i.priority == "low"),
        },
    }


def format_whatsapp_summary(summary: dict, dashboard_url: str) -> str:
    lines = [
        f"📊 Daily Summary — {summary['group_id']}",
        summary["period_label"],
        "",
        f"New issues: {summary['new_count']}",
    ]
    for inc in summary["new_incidents"]:
        sev_emoji = {"urgent": "🟣", "high": "🔴", "medium": "🟡", "low": "⚪"}.get(inc["priority"], "⚪")
        lines.append(f"  {sev_emoji} {inc['title']} ({inc['status']})")

    backlog = summary["open_backlog"]
    lines += [
        "",
        f"Still unresolved: {summary['still_open_count']}",
        f"  {backlog.get('urgent', 0)} urgent · {backlog['high']} high · {backlog['medium']} medium · {backlog['low']} low",
        "",
        f"Resolved: {summary['resolved_count']}",
        "",
        f"🔗 {dashboard_url}",
    ]
    return "\n".join(lines)
