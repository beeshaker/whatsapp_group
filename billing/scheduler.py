import logging
from datetime import date, datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from database import AsyncSessionLocal
from docker_manager import stop_client
from models import Client
from whatsapp import send_to_group

logger = logging.getLogger(__name__)

_GRACE_DAYS = 3
_WARNING_HOURS = 24


async def _check_client_status(client: Client, db) -> None:
    today = date.today()
    now = datetime.now(timezone.utc)

    if client.status == "active":
        if today > client.renewal_date:
            client.status = "grace"
            client.grace_started_at = now
            await db.commit()
            await send_to_group(
                client,
                f"⚠️ Your subscription expired on {client.renewal_date}. "
                f"Your dashboard has been locked. Type /payment in this group to renew.",
            )
        elif client.renewal_date - today == timedelta(days=3):
            await send_to_group(
                client,
                f"\U0001f514 Reminder: Your subscription renews on {client.renewal_date}. "
                f"Type /payment to pay early and avoid any interruption.",
            )

    elif client.status == "grace":
        grace_age = now - client.grace_started_at
        if grace_age >= timedelta(days=_GRACE_DAYS):
            client.status = "warning"
            client.warning_sent_at = now
            await db.commit()
            await send_to_group(
                client,
                f"\U0001f6a8 Your subscription has been expired for {_GRACE_DAYS} days. "
                f"You have 24 hours before your WhatsApp bot is disconnected. "
                f"Type /payment NOW to keep your service active.",
            )
        else:
            await send_to_group(
                client,
                f"⚠️ Your subscription is still unpaid (expired {client.renewal_date}). "
                f"Dashboard is locked. Type /payment to restore access.",
            )

    elif client.status == "warning":
        warning_age = now - client.warning_sent_at
        if warning_age >= timedelta(hours=_WARNING_HOURS):
            client.status = "suspended"
            await db.commit()
            await stop_client(client)
            await send_to_group(
                client,
                "\U0001f534 Your WhatsApp bot has been suspended due to non-payment. "
                "Type /payment in this group to reactivate your service.",
            )


async def _run_daily_checks() -> None:
    async with AsyncSessionLocal() as db:
        clients = (await db.execute(
            select(Client).where(Client.status != "suspended")
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
