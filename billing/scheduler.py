import logging
from datetime import date, datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from database import AsyncSessionLocal
from models import Client
from whatsapp import send_to_group

logger = logging.getLogger(__name__)

_GRACE_DAYS = 14
_WARNING_INTERVAL_HOURS = 48


async def _check_client_status(client: Client, db) -> None:
    today = date.today()
    now = datetime.now(timezone.utc)

    if client.status == "active":
        days_until_renewal = (client.renewal_date - today).days

        if today > client.renewal_date:
            client.status = "grace"
            client.grace_started_at = now
            client.last_warning_sent_at = now
            await db.commit()
            await send_to_group(
                client,
                f"\U0001f512 Your subscription expired on {client.renewal_date}. "
                f"Your dashboard has been locked. Type /payment to restore access. "
                f"You have 14 days before ticketing is also suspended.",
            )
        elif days_until_renewal == 14 and not client.pre_expiry_14_warned:
            client.pre_expiry_14_warned = True
            await db.commit()
            await send_to_group(
                client,
                f"\U0001f514 Reminder: Your subscription renews in 14 days on {client.renewal_date}. "
                f"Type /payment to pay early and avoid any interruption.",
            )
        elif days_until_renewal == 2 and not client.pre_expiry_2_warned:
            client.pre_expiry_2_warned = True
            await db.commit()
            await send_to_group(
                client,
                f"⚠️ Urgent: Your subscription renews in 2 days on {client.renewal_date}. "
                f"Type /payment now to keep your service active.",
            )

    elif client.status == "grace":
        if not client.grace_started_at:
            return
        grace_age = now - client.grace_started_at
        if grace_age >= timedelta(days=_GRACE_DAYS):
            client.status = "billing_only"
            client.billing_only_started_at = now
            client.last_warning_sent_at = now
            await db.commit()
            await send_to_group(
                client,
                f"\U0001f6a8 Your ticketing system has been suspended due to non-payment. "
                f"Your dashboard and ticketing groups are now offline. "
                f"Type /payment to reactivate. "
                f"Your data is retained for {client.data_retention_days} days.",
            )
        elif not client.last_warning_sent_at or (
            now - client.last_warning_sent_at >= timedelta(hours=_WARNING_INTERVAL_HOURS)
        ):
            days_overdue = (today - client.renewal_date).days
            days_left = max(0, _GRACE_DAYS - grace_age.days)
            client.last_warning_sent_at = now
            await db.commit()
            await send_to_group(
                client,
                f"⚠️ Your subscription is unpaid ({days_overdue} days overdue). "
                f"Dashboard locked. Type /payment now — "
                f"ticketing will be suspended in {days_left} days.",
            )

    elif client.status == "billing_only":
        if not client.last_warning_sent_at or (
            now - client.last_warning_sent_at >= timedelta(hours=_WARNING_INTERVAL_HOURS)
        ):
            days_overdue = (today - client.renewal_date).days
            billing_only_start = client.billing_only_started_at or now
            days_elapsed = (now - billing_only_start).days
            days_remaining = max(0, client.data_retention_days - days_elapsed)
            client.last_warning_sent_at = now
            await db.commit()
            await send_to_group(
                client,
                f"\U0001f6a8 Urgent: Your service remains suspended ({days_overdue} days overdue). "
                f"Type /payment now. "
                f"Data will be retained for {days_remaining} more days.",
            )


async def _run_daily_checks() -> None:
    async with AsyncSessionLocal() as db:
        clients = (await db.execute(
            select(Client).where(Client.status != "closed")
        )).scalars().all()
        for client in clients:
            try:
                await _check_client_status(client, db)
            except Exception:
                logger.exception("Error checking client %s", client.subdomain)


def start_scheduler() -> AsyncIOScheduler:
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        _run_daily_checks,
        "cron",
        hour=8,
        minute=0,
        timezone="Africa/Nairobi",
    )
    scheduler.start()
    return scheduler
