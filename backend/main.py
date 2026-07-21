import asyncio
import hmac
import logging
import os
import re
import sys
import zoneinfo

import httpx
from contextlib import asynccontextmanager
from datetime import date as _date, datetime, timedelta, timezone
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from fastapi import Body, Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, field_validator
from sqlalchemy import func, select, update as sa_update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.sessions import SessionMiddleware

from auth import require_login, require_admin, require_super_admin, hash_password, verify_password, check_incident_group_access
from chat import answer_query, answer_sales_query
from classifier import classify_message, classify_update_or_new, _VALID_TRANSACTION_TYPES
from database import get_db, init_db, AsyncSessionLocal
from media import MEDIA_DIR, download_media
from models import Incident, IncidentCategory, IncidentMedia, IncidentStatusHistory, IncidentUpdate, User, UserGroup, AuditLog, AdminProfile, AdminGroupSubscription
from odoo_stub import push_incident
from summaries import build_summary, format_whatsapp_summary, window_for_date
from lead_fields import is_valid_phone, normalize_phone
from vehicle_plate import is_valid_plate, normalize_plate, resolve_plate_for_issue
from whatsapp import reply_to_message, send_group_message, list_groups as list_whatsapp_groups

_VALID_STATUSES = {"new", "review", "acknowledged", "resolved", "ignored"}
_VALID_PRIORITIES = {"low", "medium", "high", "urgent"}
_VALID_REMINDER_OFFSETS = {0, 1, 6, 24}
_MEDIA_TYPES = {"image", "video", "document", "audio"}


class StatusUpdate(BaseModel):
    status: str


class RelinkBody(BaseModel):
    incident_id: Optional[int]


class ReplyBody(BaseModel):
    text: str


class CreateUserBody(BaseModel):
    username: str
    password: str
    role: str = "user"
    group_ids: list[str] = []


class GroupAssignBody(BaseModel):
    group_ids: list[str]


class TicketGroupAddBody(BaseModel):
    group_id: str


class TicketGroupUpgradeBody(BaseModel):
    group_id: str
    phone: str


class AdminProfileBody(BaseModel):
    whatsapp_phone: Optional[str] = None

    @field_validator("whatsapp_phone")
    @classmethod
    def validate_phone(cls, v: Optional[str]) -> Optional[str]:
        if not v:
            return None
        if not re.fullmatch(r"\d{7,15}", v):
            raise ValueError("whatsapp_phone must be 7–15 digits")
        return v


class AdminSubscriptionsBody(BaseModel):
    group_ids: list[str]


class ChatBody(BaseModel):
    message: str


class CreateCategoryBody(BaseModel):
    slug: str
    label: str

    @field_validator("slug")
    @classmethod
    def validate_slug(cls, v: str) -> str:
        if not v or len(v) > 50:
            raise ValueError("slug must be 1–50 characters")
        if not re.fullmatch(r"[a-z0-9_]+", v):
            raise ValueError("slug must match ^[a-z0-9_]+$")
        return v

    @field_validator("label")
    @classmethod
    def validate_label(cls, v: str) -> str:
        if not v or not v.strip():
            raise ValueError("label must not be empty")
        if len(v) > 100:
            raise ValueError("label must be max 100 characters")
        return v


class DeleteCategoryBody(BaseModel):
    remap_to: Optional[str] = None


class TicketDetailUpdateBody(BaseModel):
    priority: Optional[str] = None
    category: Optional[str] = None
    end_date: Optional[datetime] = None
    escalated: Optional[bool] = None
    reminder_offset_hours: Optional[int] = None
    vehicle_plate: Optional[str] = None
    lead_agent: Optional[str] = None
    contact_name: Optional[str] = None
    contact_phone: Optional[str] = None
    lead_location: Optional[str] = None
    lead_budget: Optional[str] = None
    transaction_type: Optional[str] = None
    lead_source: Optional[str] = None

    @field_validator("end_date")
    @classmethod
    def normalize_end_date(cls, v: Optional[datetime]) -> Optional[datetime]:
        if v is not None and v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v

    @field_validator("vehicle_plate")
    @classmethod
    def normalize_vehicle_plate(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        if not is_valid_plate(v):
            raise ValueError("vehicle_plate must match Kenyan plate format, e.g. KMGQ947Z")
        return normalize_plate(v)

    @field_validator("contact_phone")
    @classmethod
    def normalize_contact_phone(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        if not is_valid_phone(v):
            raise ValueError("contact_phone must be a valid Kenyan phone number, e.g. 0712345678")
        return normalize_phone(v)

    @field_validator("transaction_type")
    @classmethod
    def normalize_transaction_type(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        v = v.strip()
        if not v:
            return None
        v = v.lower()
        if v not in _VALID_TRANSACTION_TYPES:
            raise ValueError("transaction_type must be one of: sale, rent, unknown")
        return v


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

GATEWAY_SECRET_TOKEN = os.getenv("GATEWAY_SECRET_TOKEN", "change-me")
SUMMARY_TIMEZONE = os.getenv("SUMMARY_TIMEZONE", "Africa/Nairobi")
BILLING_SERVICE_URL = os.getenv("BILLING_SERVICE_URL", "")
BILLING_WEBHOOK_SECRET = os.getenv("BILLING_WEBHOOK_SECRET", "")
CLIENT_SUBDOMAIN = os.getenv("CLIENT_SUBDOMAIN", "")
FLEET_PLATE_MODE = os.getenv("FLEET_PLATE_MODE", "false").lower() == "true"
LEAD_MODE = os.getenv("LEAD_MODE", "false").lower() == "true"
_LEAD_INITIAL_STATUS = "new"
_LEAD_STATUSES = {"new", "contacted", "closed_won", "closed_lost"}
_LEAD_TERMINAL_STATUSES = {"closed_won", "closed_lost"}


def _valid_statuses() -> set[str]:
    return _LEAD_STATUSES if LEAD_MODE else _VALID_STATUSES


def _archived_statuses() -> set[str]:
    return _LEAD_TERMINAL_STATUSES if LEAD_MODE else {"resolved"}


try:
    SUMMARY_SCHEDULE_HOUR = int(os.getenv("SUMMARY_SCHEDULE_HOUR", "8"))
except ValueError:
    logger.warning("Invalid SUMMARY_SCHEDULE_HOUR env var, using default of 8")
    SUMMARY_SCHEDULE_HOUR = 8
DASHBOARD_URL = os.getenv("DASHBOARD_URL", "http://localhost:8000")

_billing_status_cache: dict | None = None
_ticket_groups_cache: dict | None = None
_CACHE_TTL_SECONDS = 60

SECRET_KEY = os.getenv("SECRET_KEY", "")
if not SECRET_KEY or SECRET_KEY == "change-me":
    logger.error("SECRET_KEY env var is not set or is the default value. Refusing to start.")
    sys.exit(1)


async def _fetch_billing_client_info() -> dict:
    """Fetches {status, whatsapp_group_id} for this client from billing, 60s
    cache, fails open to {"status": "active", "whatsapp_group_id": None}."""
    global _billing_status_cache
    if not BILLING_SERVICE_URL or not CLIENT_SUBDOMAIN:
        return {"status": "active", "whatsapp_group_id": None}
    now = datetime.now(timezone.utc)
    if (
        _billing_status_cache is not None
        and (now - _billing_status_cache["fetched_at"]).total_seconds() < _CACHE_TTL_SECONDS
    ):
        return _billing_status_cache
    try:
        async with httpx.AsyncClient(timeout=5.0) as http:
            r = await http.get(
                f"{BILLING_SERVICE_URL}/api/clients/{CLIENT_SUBDOMAIN}/status",
                headers={"X-Billing-Secret": BILLING_WEBHOOK_SECRET},
            )
            r.raise_for_status()
            body = r.json()
            status = body.get("status", "active")
            whatsapp_group_id = body.get("whatsapp_group_id")
    except Exception:
        logger.warning("Billing status check failed — defaulting to active")
        return {"status": "active", "whatsapp_group_id": None}
    _billing_status_cache = {
        "status": status, "whatsapp_group_id": whatsapp_group_id, "fetched_at": now,
    }
    return _billing_status_cache


async def _get_client_billing_status() -> str:
    info = await _fetch_billing_client_info()
    return info["status"]


async def _get_billing_group_id() -> Optional[str]:
    """Returns the client's configured billing/superusers WhatsApp group JID,
    fetched live from billing (same cache as _get_client_billing_status) so it
    stays in sync with whatever is set in the billing dashboard — no separate
    per-client env var to keep in sync by hand."""
    info = await _fetch_billing_client_info()
    return info["whatsapp_group_id"]


async def _get_allowed_ticket_groups() -> Optional[list[str]]:
    """Returns the client's allowed ticket-raising group IDs, or None if unrestricted
    (today's default) or the billing service is unreachable/unconfigured (fail open)."""
    global _ticket_groups_cache
    if not BILLING_SERVICE_URL or not CLIENT_SUBDOMAIN:
        return None
    now = datetime.now(timezone.utc)
    if (
        _ticket_groups_cache is not None
        and (now - _ticket_groups_cache["fetched_at"]).total_seconds() < _CACHE_TTL_SECONDS
    ):
        return _ticket_groups_cache["allowed_groups"]
    try:
        async with httpx.AsyncClient(timeout=5.0) as http:
            r = await http.get(
                f"{BILLING_SERVICE_URL}/api/clients/{CLIENT_SUBDOMAIN}/ticket-groups",
                headers={"X-Billing-Secret": BILLING_WEBHOOK_SECRET},
            )
            r.raise_for_status()
            allowed_groups = r.json().get("allowed_groups")
    except Exception:
        logger.warning("Ticket-groups check failed — defaulting to unrestricted")
        return None
    if allowed_groups is not None and not isinstance(allowed_groups, list):
        logger.warning(
            "Ticket-groups response had unexpected shape (%r) — defaulting to unrestricted",
            allowed_groups,
        )
        return None
    _ticket_groups_cache = {"allowed_groups": allowed_groups, "fetched_at": now}
    return allowed_groups


async def require_write_auth(
    request: Request,
    x_api_key: str = Header(None, alias="X-API-Key"),
) -> Optional[str]:
    """Returns session username (str) or None (X-API-Key auth). Raises 401 if neither."""
    if hmac.compare_digest(x_api_key or "", GATEWAY_SECRET_TOKEN):
        return None
    username = request.session.get("username")
    if username:
        return username
    raise HTTPException(status_code=401, detail="Unauthorized")


try:
    MIN_CONFIDENCE = float(os.getenv("MIN_CONFIDENCE", "0.65"))
except ValueError:
    logger.warning("Invalid MIN_CONFIDENCE env var, using default of 0.65")
    MIN_CONFIDENCE = 0.65

templates = Jinja2Templates(
    directory=os.path.join(os.path.dirname(__file__), "templates")
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    async with AsyncSessionLocal() as session:
        result = await session.execute(select(User))
        if not result.scalars().first():
            admin_user = os.getenv("ADMIN_USERNAME", "admin")
            admin_pass = os.getenv("ADMIN_PASSWORD", "changeme")
            if admin_user == "admin" and admin_pass == "changeme":
                logger.warning(
                    "Using default admin credentials (admin/changeme). "
                    "Set ADMIN_USERNAME and ADMIN_PASSWORD env vars."
                )
            session.add(User(
                username=admin_user,
                hashed_password=hash_password(admin_pass),
                created_at=datetime.now(timezone.utc),
                created_by=None,
                role="admin",
            ))
            await session.commit()
            logger.info("Bootstrap admin user '%s' created.", admin_user)

        # Upgrade existing admin account to role='admin' if migrating an existing DB
        admin_user = os.getenv("ADMIN_USERNAME", "admin")
        result2 = await session.execute(select(User).where(User.username == admin_user))
        existing_admin = result2.scalar_one_or_none()
        if existing_admin and existing_admin.role != "admin":
            existing_admin.role = "admin"
            await session.commit()

        # Bootstrap super_admin user
        super_admin_user = os.getenv("SUPER_ADMIN_USERNAME", "")
        super_admin_pass = os.getenv("SUPER_ADMIN_PASSWORD", "")
        if super_admin_user and super_admin_pass:
            result3 = await session.execute(select(User).where(User.username == super_admin_user))
            existing_super = result3.scalar_one_or_none()
            if not existing_super:
                session.add(User(
                    username=super_admin_user,
                    hashed_password=hash_password(super_admin_pass),
                    created_at=datetime.now(timezone.utc),
                    created_by=None,
                    role="super_admin",
                ))
                await session.commit()
                logger.info("Bootstrap super_admin user '%s' created.", super_admin_user)

    scheduler = None
    if not os.getenv("TESTING"):
        scheduler = AsyncIOScheduler()
        scheduler.add_job(
            _push_summaries,
            CronTrigger(
                hour=SUMMARY_SCHEDULE_HOUR,
                day_of_week="mon-fri",
                timezone=SUMMARY_TIMEZONE,
            ),
        )
        scheduler.add_job(_check_ticket_reminders, IntervalTrigger(minutes=15))
        scheduler.start()
        logger.info("Summary scheduler started (hour=%s, tz=%s)", SUMMARY_SCHEDULE_HOUR, SUMMARY_TIMEZONE)

    yield

    if scheduler:
        scheduler.shutdown(wait=False)


app = FastAPI(
    title="Ops Snapshot Gateway",
    description="Ingests WhatsApp property group messages into structured incident records",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    https_only=False,
    same_site="lax",
)

app.mount("/static", StaticFiles(directory=os.path.join(os.path.dirname(__file__), "static")), name="static")


_SETUP_HTML = os.path.join(os.path.dirname(__file__), "setup.html")
_OPENWA_INTERNAL = os.getenv("OPENWA_URL", "http://openwa:2785")


@app.get("/setup", response_class=HTMLResponse)
async def setup_page():
    if not os.path.exists(_SETUP_HTML):
        raise HTTPException(status_code=404, detail="setup.html not found — mount it into the container")
    return FileResponse(_SETUP_HTML, media_type="text/html")


@app.api_route("/api/openwa/{path:path}", methods=["GET", "POST", "PUT", "DELETE", "PATCH"])
async def openwa_proxy(path: str, request: Request):
    """Reverse-proxy to the OpenWA container so the setup page works through a single ngrok tunnel."""
    url = f"{_OPENWA_INTERNAL}/api/{path}"
    forward_headers = {
        k: v for k, v in request.headers.items()
        if k.lower() in ("x-api-key", "content-type")
    }
    body = await request.body()
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.request(
                method=request.method,
                url=url,
                headers=forward_headers,
                content=body,
                params=dict(request.query_params),
            )
    except httpx.RequestError as exc:
        logger.warning("openwa proxy: could not reach %s: %s", url, exc)
        return JSONResponse({"error": f"Could not reach WhatsApp service: {exc}"}, status_code=502)
    try:
        return JSONResponse(content=resp.json(), status_code=resp.status_code)
    except Exception:
        return Response(content=resp.content, status_code=resp.status_code,
                        media_type=resp.headers.get("content-type", "application/octet-stream"))


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/setup/session-name")
async def setup_session_name():
    import whatsapp as _wa
    return {"sessionName": _wa.OPENWA_SESSION}


@app.get("/api/webhook-url")
async def webhook_url():
    # Use the stable "backend.internal" network alias (see docker-compose.yml)
    # rather than this container's own IP: the IP changes every time the
    # backend container is recreated, which silently breaks the
    # already-registered webhook on openwa. openwa's webhook URL validator
    # also rejects bare, non-dotted hostnames like "backend", which is why
    # this alias has a dot in it.
    host = os.getenv("BACKEND_HOSTNAME", "backend.internal")
    return {"url": f"http://{host}:8000/api/v1/ops/ingest"}


async def _get_open_tickets(db: AsyncSession, group_id: str, limit: int = 5) -> list[dict]:
    result = await db.execute(
        select(Incident)
        .where(Incident.group_id == group_id)
        .where(~Incident.status.in_(["resolved", "ignored"]))
        .order_by(Incident.received_at.desc())
        .limit(limit)
    )
    return [
        {"id": i.id, "category": i.category, "message_body": i.message_body, "vehicle_plate": i.vehicle_plate}
        for i in result.scalars().all()
    ]


async def _route_issue(issue: dict, full_text: str, open_tickets: list[dict]) -> dict:
    """Decide whether `issue` belongs to an existing open ticket or starts a new one.

    When FLEET_PLATE_MODE is on, this fully replaces the LLM-based
    classify_update_or_new call for this issue with a deterministic exact-plate
    match — mixing a reliable identity signal with a fuzzy LLM judgment risks
    threading two different bikes' issues together.

    When LEAD_MODE is on, every enquiry is always a new lead — there's no
    natural "update to an existing lead" concept the way a recurring
    maintenance fault has one.
    """
    if LEAD_MODE:
        return {"routing": "new", "vehicle_plate": None}

    if FLEET_PLATE_MODE:
        plate = resolve_plate_for_issue(issue["message_snippet"], full_text)
        if plate is not None:
            for ticket in open_tickets:
                if ticket.get("vehicle_plate") == plate:
                    return {"routing": "update", "ticket_id": ticket["id"], "vehicle_plate": plate}
        return {"routing": "new", "vehicle_plate": plate}

    if open_tickets:
        routing = await classify_update_or_new(issue["message_snippet"], open_tickets)
    else:
        routing = {"routing": "new"}
    routing["vehicle_plate"] = None
    return routing


async def _distinct_group_ids(db: AsyncSession) -> list[str]:
    result = await db.execute(select(Incident.group_id).distinct())
    return [row[0] for row in result.all()]


async def _push_summaries():
    kenya_tz = zoneinfo.ZoneInfo(SUMMARY_TIMEZONE)
    today = datetime.now(kenya_tz).date()
    # window_for_date(today) handles Monday → weekend window automatically
    date_from, date_to, period_label = window_for_date(today)

    async with AsyncSessionLocal() as db:
        groups = set(await _distinct_group_ids(db))
        profiles_result = await db.execute(
            select(AdminProfile).where(AdminProfile.whatsapp_phone.isnot(None))
        )
        for profile in profiles_result.scalars().all():
            subs_result = await db.execute(
                select(AdminGroupSubscription.group_id).where(
                    AdminGroupSubscription.user_id == profile.user_id
                )
            )
            subscribed = [row[0] for row in subs_result.all()]
            for gid in subscribed:
                if gid not in groups:
                    continue
                try:
                    summary = await build_summary(gid, date_from, date_to, period_label, db)
                    if summary["new_count"] == 0:
                        continue
                    text = format_whatsapp_summary(summary, DASHBOARD_URL)
                    await send_group_message(f"{profile.whatsapp_phone}@c.us", text)
                    logger.info("Summary sent to %s for group %s", profile.whatsapp_phone, gid)
                except Exception as exc:
                    logger.error("Summary push failed for %s group %s: %s", profile.whatsapp_phone, gid, exc)


async def _check_ticket_reminders():
    now = datetime.now(timezone.utc)
    async with AsyncSessionLocal() as db:
        result = await db.execute(
            select(Incident).where(
                ~Incident.status.in_(["resolved", "ignored"]),
                Incident.end_date.isnot(None),
            )
        )
        tickets = result.scalars().all()
        # Snapshot everything the checks below need from one read-only pass.
        # Each ticket's writes then happen on their own short-lived session
        # (below): a session-level rollback expires every object still
        # attached to that session, so writing on a session shared across
        # the whole batch would let one ticket's failed commit break the
        # tickets processed after it in the same run.
        ticket_snapshots = []
        for ticket in tickets:
            # SQLite (used in tests / lightweight deployments) drops tzinfo on
            # read-back even for DateTime(timezone=True) columns; normalize to
            # UTC-aware here so the comparisons below are always apples-to-apples.
            end_date = ticket.end_date
            if end_date.tzinfo is None:
                end_date = end_date.replace(tzinfo=timezone.utc)
            ticket_snapshots.append({
                "id": ticket.id,
                "group_id": ticket.group_id,
                "property_name": ticket.property_name,
                "message_body": ticket.message_body,
                "end_date": end_date,
                "reminder_offset_hours": ticket.reminder_offset_hours,
                "reminder_sent_at": ticket.reminder_sent_at,
                "escalated": ticket.escalated,
            })

    for snap in ticket_snapshots:
        try:
            if (
                snap["reminder_offset_hours"] is not None
                and snap["reminder_sent_at"] is None
                and now >= snap["end_date"] - timedelta(hours=snap["reminder_offset_hours"])
            ):
                await send_group_message(
                    snap["group_id"],
                    f"⏰ Reminder: Ticket #{snap['id']} ({snap['property_name']}) is "
                    f"approaching its deadline.\n{snap['message_body'][:200]}",
                )
                async with AsyncSessionLocal() as db:
                    ticket = await db.get(Incident, snap["id"])
                    ticket.reminder_sent_at = now
                    await db.commit()
        except Exception as exc:
            logger.error("Reminder check failed for incident %s: %s", snap["id"], exc)

        try:
            if not snap["escalated"] and now >= snap["end_date"]:
                await send_group_message(
                    snap["group_id"],
                    f"🚨 Ticket #{snap['id']} ({snap['property_name']}) has passed its "
                    f"deadline and has been escalated.\n{snap['message_body'][:200]}",
                )
                async with AsyncSessionLocal() as db:
                    ticket = await db.get(Incident, snap["id"])
                    ticket.escalated = True
                    await db.commit()
        except Exception as exc:
            logger.error("Escalation check failed for incident %s: %s", snap["id"], exc)


async def _get_allowed_groups(username: str, db: AsyncSession) -> Optional[list[str]]:
    """Returns list of allowed group_ids for a user-role user, or None for admins (no filter)."""
    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()
    if not user:
        return []  # fail-closed: unknown user sees nothing
    if user.role in ("admin", "super_admin"):
        return None  # None means no filter (see all)
    groups_result = await db.execute(
        select(UserGroup.group_id).where(UserGroup.user_id == user.id)
    )
    return [row[0] for row in groups_result.all()]


async def _handle_text_ingest(
    db: AsyncSession,
    group_id: str,
    group_name: str,
    reporter_name: str,
    reporter_phone: Optional[str],
    message_body: str,
    received_at: datetime,
    message_id: Optional[str],
) -> dict:
    classification = await classify_message(message_body, db)
    all_issues = classification["issues"]
    survivor_count = sum(1 for i in all_issues if i["confidence"] >= MIN_CONFIDENCE)
    if survivor_count == 0:
        return {"status": "noise", "message": "Message classified as non-incident"}

    open_tickets = await _get_open_tickets(db, group_id, limit=(50 if FLEET_PLATE_MODE else 5))
    tickets_created = 0
    updates_created = 0
    duplicates_skipped = 0

    for issue_index, issue in enumerate(all_issues):
        if issue["confidence"] < MIN_CONFIDENCE:
            continue

        dup_incident = await db.execute(
            select(Incident.id).where(
                Incident.message_id == message_id, Incident.issue_index == issue_index
            )
        )
        dup_update = await db.execute(
            select(IncidentUpdate.id).where(
                IncidentUpdate.message_id == message_id, IncidentUpdate.issue_index == issue_index
            )
        )
        if dup_incident.scalar_one_or_none() is not None or dup_update.scalar_one_or_none() is not None:
            duplicates_skipped += 1
            continue

        routing = await _route_issue(issue, message_body, open_tickets)

        if routing["routing"] == "update":
            incident_id = routing["ticket_id"]
            update = IncidentUpdate(
                incident_id=incident_id,
                message_id=message_id,
                issue_index=issue_index,
                reporter_name=reporter_name,
                reporter_phone=reporter_phone,
                message_body=issue["message_snippet"],
                received_at=received_at,
                ai_linked=True,
            )
            try:
                db.add(update)
                parent = await db.get(Incident, incident_id)
                if parent:
                    parent.updated_at = received_at
                await db.commit()
                updates_created += 1
            except IntegrityError:
                await db.rollback()
                duplicates_skipped += 1
            except Exception as exc:
                await db.rollback()
                logger.error("DB commit failed for update: %s", exc)
                return {"status": "error", "message": "Update could not be persisted"}
            continue

        incident = Incident(
            group_id=group_id,
            property_name=group_name,
            reporter_name=reporter_name,
            reporter_phone=reporter_phone,
            message_body=message_body if LEAD_MODE else issue["message_snippet"],
            category=issue["category"],
            vehicle_plate=routing["vehicle_plate"],
            priority=issue["priority"],
            confidence=issue["confidence"],
            status=_LEAD_INITIAL_STATUS if LEAD_MODE else "review",
            received_at=received_at,
            message_id=message_id,
            issue_index=issue_index,
            lead_agent=issue.get("lead_agent"),
            contact_name=issue.get("contact_name"),
            contact_phone=issue.get("contact_phone"),
            lead_location=issue.get("lead_location"),
            lead_budget=issue.get("lead_budget"),
            transaction_type=issue.get("transaction_type"),
            lead_source=issue.get("lead_source"),
        )
        try:
            db.add(incident)
            await db.flush()
            db.add(IncidentStatusHistory(
                incident_id=incident.id,
                from_status=None,
                to_status=_LEAD_INITIAL_STATUS if LEAD_MODE else "review",
                changed_at=received_at,
            ))
            await db.commit()
            await db.refresh(incident)
            tickets_created += 1
        except IntegrityError:
            await db.rollback()
            duplicates_skipped += 1
            continue
        except Exception as exc:
            await db.rollback()
            logger.error("DB commit failed: %s", exc)
            return {"status": "error", "message": "Incident could not be persisted"}

        open_tickets.append({
            "id": incident.id, "category": incident.category, "message_body": incident.message_body,
            "vehicle_plate": incident.vehicle_plate,
        })

        try:
            await push_incident(incident)
        except Exception as exc:
            logger.error("push_incident failed: %s", exc)

        logger.info(
            "[INCIDENT] property=%s category=%s priority=%s confidence=%.2f issue_index=%d",
            group_name, issue["category"], issue["priority"], issue["confidence"], issue_index,
        )

    if tickets_created == 0 and updates_created == 0 and duplicates_skipped > 0:
        return {"status": "duplicate", "message": "Message already processed"}

    return {"status": "staged", "tickets_created": tickets_created, "updates_created": updates_created}


async def _handle_media_ingest(
    db: AsyncSession,
    group_id: str,
    group_name: str,
    reporter_name: str,
    reporter_phone: Optional[str],
    caption: str,
    media_url: Optional[str],
    received_at: datetime,
    message_id: Optional[str],
) -> dict:
    incident_id: Optional[int] = None
    update_id: Optional[int] = None
    _created_incidents: list[Incident] = []
    caption_had_survivors = False

    if caption:
        classification = await classify_message(caption, db)
        survivors = [
            (idx, issue) for idx, issue in enumerate(classification["issues"])
            if issue["confidence"] >= MIN_CONFIDENCE
        ]
        if survivors:
            caption_had_survivors = True
            open_tickets = await _get_open_tickets(db, group_id, limit=(50 if FLEET_PLATE_MODE else 5))
            highest_idx = max(survivors, key=lambda pair: pair[1]["confidence"])[0]
            tickets_created = 0
            updates_created = 0
            duplicates_skipped = 0
            media_target_incident_id: Optional[int] = None
            media_target_update_id: Optional[int] = None

            for issue_index, issue in survivors:
                dup_incident = await db.execute(
                    select(Incident.id).where(
                        Incident.message_id == message_id, Incident.issue_index == issue_index
                    )
                )
                dup_update = await db.execute(
                    select(IncidentUpdate.id).where(
                        IncidentUpdate.message_id == message_id, IncidentUpdate.issue_index == issue_index
                    )
                )
                if dup_incident.scalar_one_or_none() is not None or dup_update.scalar_one_or_none() is not None:
                    duplicates_skipped += 1
                    continue

                routing = await _route_issue(issue, caption, open_tickets)

                if routing["routing"] == "update":
                    parent_id = routing["ticket_id"]
                    upd = IncidentUpdate(
                        incident_id=parent_id,
                        message_id=message_id,
                        issue_index=issue_index,
                        reporter_name=reporter_name,
                        reporter_phone=reporter_phone,
                        message_body=issue["message_snippet"],
                        received_at=received_at,
                        ai_linked=True,
                    )
                    try:
                        async with db.begin_nested():
                            db.add(upd)
                            parent = await db.get(Incident, parent_id)
                            if parent:
                                parent.updated_at = received_at
                            await db.flush()
                        updates_created += 1
                        if issue_index == highest_idx:
                            media_target_incident_id = parent_id
                            media_target_update_id = upd.id
                    except IntegrityError:
                        duplicates_skipped += 1
                else:
                    new_inc = Incident(
                        group_id=group_id,
                        property_name=group_name,
                        reporter_name=reporter_name,
                        reporter_phone=reporter_phone,
                        message_body=caption if LEAD_MODE else issue["message_snippet"],
                        category=issue["category"],
                        vehicle_plate=routing["vehicle_plate"],
                        priority=issue["priority"],
                        confidence=issue["confidence"],
                        status=_LEAD_INITIAL_STATUS if LEAD_MODE else "review",
                        received_at=received_at,
                        message_id=message_id,
                        issue_index=issue_index,
                        lead_agent=issue.get("lead_agent"),
                        contact_name=issue.get("contact_name"),
                        contact_phone=issue.get("contact_phone"),
                        lead_location=issue.get("lead_location"),
                        lead_budget=issue.get("lead_budget"),
                        transaction_type=issue.get("transaction_type"),
                        lead_source=issue.get("lead_source"),
                    )
                    try:
                        async with db.begin_nested():
                            db.add(new_inc)
                            await db.flush()
                        tickets_created += 1
                        open_tickets.append({
                            "id": new_inc.id, "category": new_inc.category, "message_body": new_inc.message_body,
                            "vehicle_plate": new_inc.vehicle_plate,
                        })
                        _created_incidents.append(new_inc)
                        if issue_index == highest_idx:
                            media_target_incident_id = new_inc.id
                    except IntegrityError:
                        duplicates_skipped += 1

            if tickets_created == 0 and updates_created == 0 and duplicates_skipped > 0:
                return {"status": "duplicate", "message": "Message already processed"}

            incident_id = media_target_incident_id
            update_id = media_target_update_id

    if incident_id is None and media_url and not caption_had_survivors:
        open_tickets = await _get_open_tickets(db, group_id)
        if open_tickets:
            incident_id = open_tickets[0]["id"]

    if media_url and incident_id is not None:
        try:
            filename, mimetype, file_path = await download_media(media_url, MEDIA_DIR)
            media_rec = IncidentMedia(
                incident_id=incident_id,
                update_id=update_id,
                filename=filename,
                mimetype=mimetype,
                file_path=file_path,
                received_at=received_at,
            )
            db.add(media_rec)
            parent = await db.get(Incident, incident_id)
            if parent:
                parent.updated_at = received_at
        except Exception as exc:
            logger.error("Media download failed: %s", exc)
    elif media_url is None:
        logger.warning("Media message has no mediaUrl — skipping download")

    try:
        await db.commit()
    except Exception as exc:
        await db.rollback()
        logger.error("DB commit failed for media ingest: %s", exc)
        return {"status": "error", "message": "Media could not be persisted"}

    for inc in _created_incidents:
        try:
            await push_incident(inc)
        except Exception as exc:
            logger.error("push_incident failed for media incident: %s", exc)

    if incident_id is None:
        logger.warning("Orphaned media: no open ticket in group %s and no classified caption", group_id)
        return {"status": "staged_media", "message": "Media saved but no open ticket found"}

    return {"status": "staged_media", "incident_id": incident_id}


async def _forward_to_billing(subdomain: str, event_body: dict) -> None:
    if not BILLING_SERVICE_URL:
        return
    import hashlib, hmac as _hmac_mod, json as _json
    payload = _json.dumps(event_body).encode()
    sig = _hmac_mod.new(BILLING_WEBHOOK_SECRET.encode(), payload, hashlib.sha256).hexdigest()
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"{BILLING_SERVICE_URL}/webhook/client/{subdomain}",
                content=payload,
                headers={"Content-Type": "application/json", "X-Webhook-Signature": sig},
            )
    except Exception:
        logger.exception("Failed to forward message to billing service")


async def _forward_to_billing_by_group(group_id: str, event_body: dict) -> None:
    if not BILLING_SERVICE_URL:
        return
    import hashlib, hmac as _hmac_mod, json as _json
    payload = _json.dumps(event_body).encode()
    sig = _hmac_mod.new(BILLING_WEBHOOK_SECRET.encode(), payload, hashlib.sha256).hexdigest()
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            await client.post(
                f"{BILLING_SERVICE_URL}/webhook/by-group/{group_id}",
                content=payload,
                headers={"Content-Type": "application/json", "X-Webhook-Signature": sig},
            )
    except Exception:
        logger.exception("Failed to forward command to billing service for group %s", group_id)


async def _handle_reaction(data: dict, db: AsyncSession) -> dict:
    """Match a WhatsApp 👍 reaction to a lead ticket and auto-transition new -> contacted.

    Gated entirely behind LEAD_MODE. See docs/superpowers/specs/
    2026-07-21-dunhill-reaction-status-and-reply-quoting-design.md §3 for the
    3-tier match strategy (exact message_id, then author+exact timestamp,
    then a single-candidate fallback when the message wasn't in OpenWA's
    local cache).
    """
    if not LEAD_MODE:
        return {"status": "ignored", "message": "Reactions only handled in LEAD_MODE"}

    if data.get("emoji") != "👍":
        return {"status": "ignored", "message": "Unsupported reaction emoji"}

    group_id = data.get("chatId") or ""
    allowed_groups = await _get_allowed_ticket_groups()
    if allowed_groups is not None and group_id not in allowed_groups:
        return {"status": "group_not_licensed"}

    target_message_id = data.get("targetMessageId")
    target_author = data.get("targetAuthor")
    target_timestamp = data.get("targetTimestamp")

    incidents: list[Incident] = []

    if target_message_id:
        result = await db.execute(
            select(Incident).where(
                Incident.group_id == group_id,
                Incident.message_id == target_message_id,
            )
        )
        incidents = list(result.scalars().all())

    if not incidents and target_author and target_timestamp is not None:
        author_phone = target_author.split("@")[0]
        received_at = datetime.fromtimestamp(target_timestamp, tz=timezone.utc)
        result = await db.execute(
            select(Incident).where(
                Incident.group_id == group_id,
                Incident.reporter_phone == author_phone,
                Incident.received_at == received_at,
            )
        )
        incidents = list(result.scalars().all())

    if not incidents and target_author and target_timestamp is None:
        author_phone = target_author.split("@")[0]
        result = await db.execute(
            select(Incident).where(
                Incident.group_id == group_id,
                Incident.reporter_phone == author_phone,
                Incident.status == "new",
            )
        )
        candidates = result.scalars().all()
        if len(candidates) == 1:
            incidents = [candidates[0]]

    if not incidents:
        logger.debug("Reaction matched no incident for group %s", group_id)
        return {"status": "ignored", "message": "No matching incident found for reaction"}

    # Tier-1/tier-2 matches can legitimately return multiple rows: one
    # WhatsApp message can split into multiple tickets sharing the same
    # message_id (or the same reporter_phone+received_at), one per
    # issue_index (see multi-ticket-message-split design). Transition every
    # matched incident still at "new"; leave incidents already past "new"
    # untouched rather than treating that as an error.
    new_incidents = [i for i in incidents if i.status == "new"]
    if not new_incidents:
        return {"status": "ignored", "message": "Incident already past new status"}

    sender_phone = (data.get("senderId") or "").split("@")[0]
    now = datetime.now(timezone.utc)
    for incident in new_incidents:
        old_status = incident.status
        incident.status = "contacted"
        db.add(incident)
        db.add(IncidentStatusHistory(
            incident_id=incident.id,
            from_status=old_status,
            to_status="contacted",
            changed_at=now,
            changed_by=f"whatsapp:{sender_phone}",
        ))
        db.add(AuditLog(
            username=f"whatsapp:{sender_phone}",
            action="auto_status_reaction",
            incident_id=incident.id,
            detail=f"👍 reaction from {sender_phone}",
            created_at=now,
        ))
    await db.commit()
    return {
        "status": "status_updated",
        "incident_ids": [i.id for i in new_incidents],
        "to_status": "contacted",
    }


@app.post("/api/v1/ops/ingest", status_code=status.HTTP_202_ACCEPTED)
async def ingest(
    request: Request,
    x_api_key: str = Header(None, alias="X-API-Key"),
    db: AsyncSession = Depends(get_db),
):
    if not hmac.compare_digest(x_api_key or "", GATEWAY_SECRET_TOKEN):
        raise HTTPException(status_code=401, detail="Unauthorized")

    try:
        payload = await request.json()
    except Exception:
        return {"status": "error", "message": "Invalid JSON body"}

    event_type = payload.get("event")
    data = payload.get("data", {})

    if event_type == "message.reaction":
        return await _handle_reaction(data, db)

    if event_type != "message.received":
        return {"status": "ignored", "message": "Non-group or non-chat event skipped"}

    chat_id = data.get("chatId") or data.get("from", "")
    msg_type = data.get("type", "")

    # DM routing — handle before the isGroup check so @c.us messages are caught
    if chat_id.endswith("@c.us") and not data.get("isGroup", False):
        if msg_type == "chat":
            phone = chat_id[: -len("@c.us")]
            profile_result = await db.execute(
                select(AdminProfile).where(AdminProfile.whatsapp_phone == phone)
            )
            profile = profile_result.scalar_one_or_none()
            if profile:
                dm_body = data.get("body", "").strip()
                if dm_body:
                    try:
                        reply = await answer_query(dm_body, f"wa:{phone}", db)
                        await send_group_message(chat_id, reply)
                    except Exception as exc:
                        logger.error("DM reply failed for %s: %s", phone, exc)
                        return {"status": "dm_error", "message": "Failed to process DM"}
                return {"status": "dm_handled"}
        return {"status": "dm_ignored", "message": "DM from unknown phone or non-chat type"}

    if not data.get("isGroup", False):
        return {"status": "ignored", "message": "Non-group or non-chat event skipped"}

    if msg_type != "chat" and msg_type not in _MEDIA_TYPES:
        return {"status": "ignored", "message": "Non-group or non-chat event skipped"}

    group_id = chat_id
    billing_group_id = await _get_billing_group_id()
    # Sales agent — handle the billing/superusers group as a sales/support channel
    if billing_group_id and group_id == billing_group_id:
        if msg_type == "chat" and BILLING_SERVICE_URL:
            asyncio.create_task(_forward_to_billing_by_group(group_id, data))
        if not data.get("fromMe", False):
            msg_body = (data.get("body") or "").strip()
            if msg_body and not msg_body.startswith("/"):
                try:
                    reply = await answer_sales_query(msg_body, f"sales:{group_id}", db)
                    await send_group_message(group_id, reply)
                except Exception as exc:
                    logger.error("Sales agent reply failed: %s", exc)
        return JSONResponse({"status": "sales_handled"}, status_code=status.HTTP_202_ACCEPTED)

    allowed_groups = await _get_allowed_ticket_groups()
    if allowed_groups is not None and group_id not in allowed_groups:
        return {"status": "group_not_licensed"}

    group_name = (
        data.get("chatName")
        or (data.get("chat") or {}).get("name")
        or (group_id.split("@")[0] if group_id else "Unmapped Property Group")
    )
    # data.id isn't populated by every OpenWA delivery (observed: messages from
    # senders using WhatsApp's "@lid" linked-identity scheme arrive with no
    # data.id at all) — payload.deliveryId is OpenWA's own per-delivery UUID
    # and is always present, so fall back to it rather than leaving every such
    # message with message_id=None, which collapses them all into duplicates
    # of whichever message claimed that null slot first.
    message_id: Optional[str] = data.get("id") or payload.get("deliveryId") or None

    reporter_name = (data.get("notifyName") or "").strip() or "Unknown"
    reporter_phone = (data.get("author") or "").split("@")[0].strip() or None
    epoch = data.get("timestamp") or datetime.now(timezone.utc).timestamp()
    received_at = datetime.fromtimestamp(epoch, tz=timezone.utc)

    if msg_type == "chat":
        message_body = data.get("body", "").strip()[:4000]
        if not message_body:
            return {"status": "ignored", "message": "Empty message body"}
        if BILLING_SERVICE_URL:
            asyncio.create_task(_forward_to_billing_by_group(group_id, data))

        # billing gate — silent drop for billing_only and closed
        billing_status = await _get_client_billing_status()
        if billing_status in ("billing_only", "closed"):
            return {"status": "billing_only_drop"}

        if message_body.startswith("/"):
            return {"status": "forwarded_to_billing"}
        return await _handle_text_ingest(
            db, group_id, group_name, reporter_name, reporter_phone,
            message_body, received_at, message_id,
        )

    # Media message
    billing_status = await _get_client_billing_status()
    if billing_status in ("billing_only", "closed"):
        return {"status": "billing_only_drop"}
    caption = (data.get("caption") or "").strip()[:4000]
    media_url: Optional[str] = data.get("mediaUrl") or None
    return await _handle_media_ingest(
        db, group_id, group_name, reporter_name, reporter_phone,
        caption, media_url, received_at, message_id,
    )


@app.get("/incidents")
async def list_incidents(
    username: str = Depends(require_login),
    since_id: Optional[int] = None,
    statuses: Optional[list[str]] = Query(default=None),
    db: AsyncSession = Depends(get_db),
):
    update_count_sq = (
        select(func.count(IncidentUpdate.id))
        .where(IncidentUpdate.incident_id == Incident.id)
        .correlate(Incident)
        .scalar_subquery()
    )
    media_count_sq = (
        select(func.count(IncidentMedia.id))
        .where(IncidentMedia.incident_id == Incident.id)
        .correlate(Incident)
        .scalar_subquery()
    )
    query = (
        select(Incident, update_count_sq.label("uc"), media_count_sq.label("mc"))
        .order_by(Incident.received_at.desc())
    )
    if since_id is not None:
        query = query.where(Incident.id > since_id)
    if statuses is not None:
        query = query.where(Incident.status.in_(statuses))
    allowed = await _get_allowed_groups(username, db)
    if allowed is not None:
        query = query.where(Incident.group_id.in_(allowed))
    result = await db.execute(query)
    return [
        {
            "id": i.id,
            "property_name": i.property_name,
            "reporter_name": i.reporter_name,
            "reporter_phone": i.reporter_phone,
            "lead_agent": i.lead_agent,
            "contact_name": i.contact_name,
            "contact_phone": i.contact_phone,
            "lead_location": i.lead_location,
            "lead_budget": i.lead_budget,
            "transaction_type": i.transaction_type,
            "lead_source": i.lead_source,
            "category": i.category,
            "vehicle_plate": i.vehicle_plate,
            "priority": i.priority,
            "confidence": round(i.confidence, 2),
            "status": i.status,
            "message_body": i.message_body,
            "received_at": i.received_at.isoformat(),
            "update_count": uc,
            "media_count": mc,
        }
        for i, uc, mc in result.all()
    ]


@app.get("/incidents/{incident_id}")
async def get_incident_detail(
    incident_id: int,
    username: str = Depends(require_login),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Incident).where(Incident.id == incident_id))
    incident = result.scalar_one_or_none()
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    await check_incident_group_access(username, incident_id, db)

    updates_result = await db.execute(
        select(IncidentUpdate)
        .where(IncidentUpdate.incident_id == incident_id)
        .order_by(IncidentUpdate.received_at.asc())
    )
    updates = updates_result.scalars().all()

    update_rows = []
    for u in updates:
        mc_result = await db.execute(
            select(func.count(IncidentMedia.id)).where(IncidentMedia.update_id == u.id)
        )
        mc = mc_result.scalar() or 0
        update_rows.append({
            "id": u.id,
            "reporter_name": u.reporter_name,
            "reporter_phone": u.reporter_phone,
            "message_body": u.message_body,
            "received_at": u.received_at.isoformat(),
            "ai_linked": u.ai_linked,
            "relinked": u.relinked,
            "media_count": mc,
        })

    media_result = await db.execute(
        select(IncidentMedia)
        .where(IncidentMedia.incident_id == incident_id)
        .order_by(IncidentMedia.received_at.asc())
    )
    media_rows = [
        {
            "id": m.id,
            "filename": m.filename,
            "mimetype": m.mimetype,
            "update_id": m.update_id,
        }
        for m in media_result.scalars().all()
    ]

    history_result = await db.execute(
        select(IncidentStatusHistory)
        .where(IncidentStatusHistory.incident_id == incident_id)
        .order_by(IncidentStatusHistory.id.asc())
    )
    history_rows = [
        {
            "from_status": h.from_status,
            "to_status": h.to_status,
            "changed_at": h.changed_at.isoformat(),
            "changed_by": h.changed_by,
        }
        for h in history_result.scalars().all()
    ]

    audit_result = await db.execute(
        select(AuditLog)
        .where(AuditLog.incident_id == incident_id)
        .order_by(AuditLog.created_at.asc())
    )
    audit_rows = [
        {
            "username": a.username,
            "action": a.action,
            "detail": a.detail,
            "created_at": a.created_at.isoformat(),
        }
        for a in audit_result.scalars().all()
    ]

    sibling_tickets: list[dict] = []
    if incident.message_id:
        siblings_result = await db.execute(
            select(Incident)
            .where(Incident.message_id == incident.message_id)
            .where(Incident.id != incident.id)
            .order_by(Incident.issue_index.asc())
        )
        sibling_tickets = [
            {
                "id": s.id,
                "property_name": s.property_name,
                "category": s.category,
                "vehicle_plate": s.vehicle_plate,
                "priority": s.priority,
                "status": s.status,
            }
            for s in siblings_result.scalars().all()
        ]

    return {
        "id": incident.id,
        "property_name": incident.property_name,
        "reporter_name": incident.reporter_name,
        "reporter_phone": incident.reporter_phone,
        "lead_agent": incident.lead_agent,
        "contact_name": incident.contact_name,
        "contact_phone": incident.contact_phone,
        "lead_location": incident.lead_location,
        "lead_budget": incident.lead_budget,
        "transaction_type": incident.transaction_type,
        "lead_source": incident.lead_source,
        "category": incident.category,
        "vehicle_plate": incident.vehicle_plate,
        "priority": incident.priority,
        "end_date": incident.end_date.isoformat() if incident.end_date else None,
        "escalated": incident.escalated,
        "reminder_offset_hours": incident.reminder_offset_hours,
        "reminder_sent_at": incident.reminder_sent_at.isoformat() if incident.reminder_sent_at else None,
        "confidence": round(incident.confidence, 2),
        "status": incident.status,
        "message_body": incident.message_body,
        "received_at": incident.received_at.isoformat(),
        "updated_at": incident.updated_at.isoformat() if incident.updated_at else None,
        "updates": update_rows,
        "media": media_rows,
        "status_history": history_rows,
        "audit_log": audit_rows,
        "sibling_tickets": sibling_tickets,
    }


@app.get("/media/{media_id}")
async def serve_media(
    media_id: int,
    x_api_key: str = Header(None, alias="X-API-Key"),
    db: AsyncSession = Depends(get_db),
):
    if not hmac.compare_digest(x_api_key or "", GATEWAY_SECRET_TOKEN):
        raise HTTPException(status_code=401, detail="Unauthorized")
    result = await db.execute(select(IncidentMedia).where(IncidentMedia.id == media_id))
    media = result.scalar_one_or_none()
    if not media:
        raise HTTPException(status_code=404, detail="Media not found")
    if not os.path.exists(media.file_path):
        raise HTTPException(status_code=404, detail="Media file not found on disk")
    media_root = os.path.realpath(MEDIA_DIR)
    file_real = os.path.realpath(media.file_path)
    if not file_real.startswith(media_root + os.sep):
        raise HTTPException(status_code=403, detail="Forbidden")
    return FileResponse(media.file_path, media_type=media.mimetype, filename=media.filename)


@app.patch("/incidents/{update_id}/relink")
async def relink_update(
    update_id: int,
    body: RelinkBody,
    actor: Optional[str] = Depends(require_write_auth),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(IncidentUpdate).where(IncidentUpdate.id == update_id))
    update = result.scalar_one_or_none()
    if not update:
        raise HTTPException(status_code=404, detail="Update not found")

    await check_incident_group_access(actor, update.incident_id, db)
    now = datetime.now(timezone.utc)

    if body.incident_id is None:
        old_parent = await db.get(Incident, update.incident_id)
        new_incident = Incident(
            group_id=old_parent.group_id if old_parent else "",
            property_name=old_parent.property_name if old_parent else "Unknown",
            reporter_name=update.reporter_name,
            reporter_phone=update.reporter_phone,
            message_body=update.message_body,
            category="other",
            priority="low",
            confidence=0.0,
            status=_LEAD_INITIAL_STATUS if LEAD_MODE else "review",
            received_at=update.received_at,
            message_id=update.message_id,
        )
        db.add(new_incident)
        await db.flush()
        db.add(IncidentStatusHistory(
            incident_id=new_incident.id,
            from_status=None,
            to_status=_LEAD_INITIAL_STATUS if LEAD_MODE else "review",
            changed_at=new_incident.received_at,
        ))
        media_res = await db.execute(
            select(IncidentMedia).where(IncidentMedia.update_id == update_id)
        )
        for m in media_res.scalars().all():
            m.incident_id = new_incident.id
            m.update_id = None
        await db.delete(update)
        if actor:
            db.add(AuditLog(
                username=actor,
                action="relink",
                incident_id=new_incident.id,
                detail="promoted to standalone incident",
                created_at=now,
            ))
        await db.commit()
        return {"update_id": update_id, "incident_id": new_incident.id, "promoted": True}

    target = await db.get(Incident, body.incident_id)
    if not target:
        raise HTTPException(status_code=404, detail="Target incident not found")

    await check_incident_group_access(actor, body.incident_id, db)

    original_incident_id = update.incident_id
    update.incident_id = body.incident_id
    update.ai_linked = False
    update.relinked = True
    media_res = await db.execute(
        select(IncidentMedia).where(IncidentMedia.update_id == update_id)
    )
    for m in media_res.scalars().all():
        m.incident_id = body.incident_id
    target.updated_at = now
    if actor:
        db.add(AuditLog(
            username=actor,
            action="relink",
            incident_id=body.incident_id,
            detail=f"update {update_id} moved from incident {original_incident_id}",
            created_at=now,
        ))
    await db.commit()
    return {"update_id": update_id, "incident_id": body.incident_id}


@app.patch("/incidents/{incident_id}/status")
async def update_incident_status(
    incident_id: int,
    body: StatusUpdate,
    actor: Optional[str] = Depends(require_write_auth),
    db: AsyncSession = Depends(get_db),
):
    await check_incident_group_access(actor, incident_id, db)
    if body.status not in _valid_statuses():
        raise HTTPException(status_code=422, detail=f"status must be one of {sorted(_valid_statuses())}")
    result = await db.execute(select(Incident).where(Incident.id == incident_id))
    incident = result.scalar_one_or_none()
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    old_status = incident.status
    incident.status = body.status
    now = datetime.now(timezone.utc)
    db.add(incident)
    db.add(IncidentStatusHistory(
        incident_id=incident_id,
        from_status=old_status,
        to_status=body.status,
        changed_at=now,
        changed_by=actor,
    ))
    if actor:
        db.add(AuditLog(
            username=actor,
            action="status_change",
            incident_id=incident_id,
            detail=f"{old_status} → {body.status}",
            created_at=now,
        ))
    await db.commit()
    return {"id": incident.id, "status": incident.status}


@app.patch("/incidents/{incident_id}")
async def update_incident_fields(
    incident_id: int,
    body: TicketDetailUpdateBody,
    actor: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    fields_set = body.model_fields_set
    if not fields_set:
        raise HTTPException(status_code=422, detail="At least one field must be provided")

    result = await db.execute(select(Incident).where(Incident.id == incident_id))
    incident = result.scalar_one_or_none()
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")

    now = datetime.now(timezone.utc)
    changes = []

    if "priority" in fields_set:
        if body.priority not in _VALID_PRIORITIES:
            raise HTTPException(
                status_code=422, detail=f"priority must be one of {sorted(_VALID_PRIORITIES)}"
            )
        if body.priority != incident.priority:
            changes.append(f"priority: {incident.priority} → {body.priority}")
            incident.priority = body.priority

    if "category" in fields_set:
        cat_result = await db.execute(
            select(IncidentCategory).where(IncidentCategory.slug == body.category)
        )
        if not cat_result.scalar_one_or_none():
            raise HTTPException(status_code=422, detail=f"Unknown category: {body.category}")
        if body.category != incident.category:
            changes.append(f"category: {incident.category} → {body.category}")
            incident.category = body.category

    if "end_date" in fields_set:
        if body.end_date != incident.end_date:
            changes.append(f"end_date: {incident.end_date} → {body.end_date}")
            incident.end_date = body.end_date
            if incident.reminder_sent_at is not None:
                changes.append(f"reminder_sent_at: {incident.reminder_sent_at} → None (end_date changed)")
                incident.reminder_sent_at = None
            if body.end_date is not None and body.end_date > now:
                if incident.escalated:
                    changes.append(f"escalated: {incident.escalated} → False (end_date changed)")
                    incident.escalated = False

    if "escalated" in fields_set:
        if body.escalated is None:
            raise HTTPException(status_code=422, detail="escalated must be a boolean")
        if body.escalated != incident.escalated:
            changes.append(f"escalated: {incident.escalated} → {body.escalated}")
            incident.escalated = body.escalated

    if "reminder_offset_hours" in fields_set:
        if body.reminder_offset_hours is not None and body.reminder_offset_hours not in _VALID_REMINDER_OFFSETS:
            raise HTTPException(
                status_code=422,
                detail=f"reminder_offset_hours must be one of {sorted(_VALID_REMINDER_OFFSETS)} or null",
            )
        if body.reminder_offset_hours != incident.reminder_offset_hours:
            changes.append(
                f"reminder_offset_hours: {incident.reminder_offset_hours} → {body.reminder_offset_hours}"
            )
            incident.reminder_offset_hours = body.reminder_offset_hours

    if "vehicle_plate" in fields_set:
        if body.vehicle_plate != incident.vehicle_plate:
            changes.append(f"vehicle_plate: {incident.vehicle_plate} → {body.vehicle_plate}")
            incident.vehicle_plate = body.vehicle_plate

    if "lead_agent" in fields_set:
        if body.lead_agent != incident.lead_agent:
            changes.append(f"lead_agent: {incident.lead_agent} → {body.lead_agent}")
            incident.lead_agent = body.lead_agent

    if "contact_name" in fields_set:
        if body.contact_name != incident.contact_name:
            changes.append(f"contact_name: {incident.contact_name} → {body.contact_name}")
            incident.contact_name = body.contact_name

    if "contact_phone" in fields_set:
        if body.contact_phone != incident.contact_phone:
            changes.append(f"contact_phone: {incident.contact_phone} → {body.contact_phone}")
            incident.contact_phone = body.contact_phone

    if "lead_location" in fields_set:
        if body.lead_location != incident.lead_location:
            changes.append(f"lead_location: {incident.lead_location} → {body.lead_location}")
            incident.lead_location = body.lead_location

    if "lead_budget" in fields_set:
        if body.lead_budget != incident.lead_budget:
            changes.append(f"lead_budget: {incident.lead_budget} → {body.lead_budget}")
            incident.lead_budget = body.lead_budget

    if "transaction_type" in fields_set:
        if body.transaction_type != incident.transaction_type:
            changes.append(f"transaction_type: {incident.transaction_type} → {body.transaction_type}")
            incident.transaction_type = body.transaction_type

    if "lead_source" in fields_set:
        if body.lead_source != incident.lead_source:
            changes.append(f"lead_source: {incident.lead_source} → {body.lead_source}")
            incident.lead_source = body.lead_source

    db.add(incident)
    for change in changes:
        db.add(AuditLog(
            username=actor,
            action="ticket_detail_update",
            incident_id=incident_id,
            detail=change,
            created_at=now,
        ))
    await db.commit()
    await db.refresh(incident)

    return {
        "id": incident.id,
        "priority": incident.priority,
        "category": incident.category,
        "vehicle_plate": incident.vehicle_plate,
        "end_date": incident.end_date.isoformat() if incident.end_date else None,
        "escalated": incident.escalated,
        "reminder_offset_hours": incident.reminder_offset_hours,
        "reminder_sent_at": incident.reminder_sent_at.isoformat() if incident.reminder_sent_at else None,
        "lead_agent": incident.lead_agent,
        "contact_name": incident.contact_name,
        "contact_phone": incident.contact_phone,
        "lead_location": incident.lead_location,
        "lead_budget": incident.lead_budget,
        "transaction_type": incident.transaction_type,
        "lead_source": incident.lead_source,
    }


def _aware(dt: datetime) -> datetime:
    """SQLite drops tzinfo on datetime columns read back from the DB (see the
    same pattern in _check_ticket_reminders); treat a naive value as UTC
    rather than the server's local timezone."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


@app.post("/incidents/{incident_id}/reply")
async def reply_to_incident(
    incident_id: int,
    body: ReplyBody,
    actor: Optional[str] = Depends(require_write_auth),
    db: AsyncSession = Depends(get_db),
):
    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="text must not be empty")
    text = text[:4000]

    await check_incident_group_access(actor, incident_id, db)
    result = await db.execute(select(Incident).where(Incident.id == incident_id))
    incident = result.scalar_one_or_none()
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")

    try:
        wa_message_id = await reply_to_message(
            incident.group_id,
            incident.message_id or "",
            text,
            author_hint=incident.reporter_phone,
            timestamp_hint=int(_aware(incident.received_at).timestamp()),
            context_snippet=incident.message_body[:200],
        )
    except Exception as exc:
        logger.error("reply_to_message failed: %s", exc)
        raise HTTPException(status_code=502, detail="Failed to send message to WhatsApp")

    now = datetime.now(timezone.utc)
    reporter = actor or "Dashboard"
    update = IncidentUpdate(
        incident_id=incident_id,
        message_id=wa_message_id,
        reporter_name=reporter,
        reporter_phone=None,
        message_body=text,
        received_at=now,
        ai_linked=False,
    )
    db.add(update)
    incident.updated_at = now
    if actor:
        db.add(AuditLog(
            username=actor,
            action="reply",
            incident_id=incident_id,
            detail=text[:120],
            created_at=now,
        ))
    try:
        await db.commit()
        await db.refresh(update)
    except Exception as exc:
        await db.rollback()
        logger.error("DB commit failed after send: %s", exc)
        raise HTTPException(status_code=500, detail="Message sent but could not be saved")

    return {
        "id": update.id,
        "reporter_name": update.reporter_name,
        "message_body": update.message_body,
        "received_at": update.received_at.isoformat(),
        "ai_linked": update.ai_linked,
        "media_count": 0,
    }


@app.get("/super-admin/categories", response_class=HTMLResponse)
async def super_admin_categories_page(
    request: Request,
    username: str = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(
            IncidentCategory,
            func.count(Incident.id).label("incident_count"),
        )
        .outerjoin(Incident, Incident.category == IncidentCategory.slug)
        .group_by(IncidentCategory.id)
        .order_by(IncidentCategory.id)
    )
    categories = [
        {
            "id": cat.id,
            "slug": cat.slug,
            "label": cat.label,
            "is_protected": cat.is_protected,
            "incident_count": count,
        }
        for cat, count in result.all()
    ]
    return templates.TemplateResponse(
        "super_admin_categories.html",
        {
            "request": request,
            "username": username,
            "role": "super_admin",
            "categories": categories,
        },
    )


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if request.session.get("username"):
        return RedirectResponse(url="/", status_code=302)
    error = request.session.pop("login_error", None)
    return templates.TemplateResponse("login.html", {"request": request, "error": error})


@app.post("/login")
async def login_submit(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    form = await request.form()
    username = (form.get("username") or "").strip()
    password = form.get("password") or ""
    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()
    if not user or not verify_password(password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    request.session["username"] = user.username
    request.session["role"] = user.role
    return RedirectResponse(url="/", status_code=302)


@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)


@app.get("/users")
async def list_users(
    request: Request,
    username: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).order_by(User.created_at.asc()))
    users = result.scalars().all()
    all_groups_result = await db.execute(select(UserGroup))
    groups_by_user: dict[int, list[str]] = {}
    for ug in all_groups_result.scalars().all():
        groups_by_user.setdefault(ug.user_id, []).append(ug.group_id)
    user_list = [
        {
            "id": u.id,
            "username": u.username,
            "role": u.role,
            "created_at": u.created_at.isoformat(),
            "created_by": u.created_by,
            "group_ids": groups_by_user.get(u.id, []),
        }
        for u in users
    ]
    if "text/html" in request.headers.get("accept", ""):
        return templates.TemplateResponse("users.html", {
            "request": request,
            "username": username,
            "role": request.session.get("role", "admin"),
            "users": user_list,
        })
    return user_list


@app.post("/users", status_code=201)
async def create_user(
    body: CreateUserBody,
    actor: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    if body.role not in ("admin", "user"):
        raise HTTPException(status_code=422, detail="role must be 'admin' or 'user'")
    username = body.username.strip()
    if not username:
        raise HTTPException(status_code=422, detail="username must not be empty")
    if len(username) > 64:
        raise HTTPException(status_code=422, detail="username too long (max 64 chars)")
    if len(body.password) < 8:
        raise HTTPException(status_code=422, detail="password must be at least 8 characters")
    existing = await db.execute(select(User).where(User.username == username))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="username already exists")
    now = datetime.now(timezone.utc)
    user = User(
        username=username,
        hashed_password=hash_password(body.password),
        role=body.role,
        created_at=now,
        created_by=actor,
    )
    try:
        db.add(user)
        await db.commit()
        await db.refresh(user)
    except IntegrityError:
        await db.rollback()
        raise HTTPException(status_code=409, detail="username already exists")
    if body.role == "user" and body.group_ids:
        for gid in dict.fromkeys(body.group_ids):
            db.add(UserGroup(user_id=user.id, group_id=gid))
        await db.commit()
    return {"id": user.id, "username": user.username, "role": user.role, "created_by": user.created_by}


@app.post("/users/{user_id}/delete")
async def delete_user(
    user_id: int,
    actor: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    if user.username == actor:
        raise HTTPException(status_code=400, detail="Cannot delete your own account")
    await db.delete(user)
    await db.commit()
    return {"deleted": user_id}


@app.get("/api/groups")
async def list_groups(
    _: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(Incident.group_id, Incident.property_name)
        .distinct()
        .order_by(Incident.property_name)
    )
    return [{"group_id": gid, "property_name": pname} for gid, pname in result.all()]


@app.get("/api/summaries")
async def get_summaries(
    group_id: Optional[str] = None,
    date: Optional[str] = None,
    username: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    kenya_tz = zoneinfo.ZoneInfo(SUMMARY_TIMEZONE)
    if date:
        try:
            d = _date.fromisoformat(date)
        except ValueError:
            raise HTTPException(status_code=422, detail="date must be YYYY-MM-DD")
    else:
        d = datetime.now(kenya_tz).date()

    date_from, date_to, period_label = window_for_date(d)

    if group_id:
        groups = [group_id]
    else:
        groups = await _distinct_group_ids(db)

    results = []
    for gid in groups:
        summary = await build_summary(gid, date_from, date_to, period_label, db)
        results.append(summary)
    return results


@app.get("/api/admin/profile")
async def get_admin_profile(
    username: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    profile_result = await db.execute(
        select(AdminProfile).where(AdminProfile.user_id == user.id)
    )
    profile = profile_result.scalar_one_or_none()

    subs_result = await db.execute(
        select(AdminGroupSubscription.group_id).where(
            AdminGroupSubscription.user_id == user.id
        )
    )
    group_ids = [row[0] for row in subs_result.all()]

    return {
        "whatsapp_phone": profile.whatsapp_phone if profile else None,
        "group_ids": group_ids,
    }


@app.put("/api/admin/profile")
async def update_admin_profile(
    body: AdminProfileBody,
    username: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    profile_result = await db.execute(
        select(AdminProfile).where(AdminProfile.user_id == user.id)
    )
    profile = profile_result.scalar_one_or_none()

    if profile:
        profile.whatsapp_phone = body.whatsapp_phone
    else:
        db.add(AdminProfile(user_id=user.id, whatsapp_phone=body.whatsapp_phone))

    await db.commit()
    return {"whatsapp_phone": body.whatsapp_phone}


@app.post("/api/admin/subscriptions")
async def update_admin_subscriptions(
    body: AdminSubscriptionsBody,
    username: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    existing = await db.execute(
        select(AdminGroupSubscription).where(AdminGroupSubscription.user_id == user.id)
    )
    for sub in existing.scalars().all():
        await db.delete(sub)

    for gid in dict.fromkeys(body.group_ids):
        db.add(AdminGroupSubscription(user_id=user.id, group_id=gid))

    await db.commit()
    return {"group_ids": body.group_ids}


@app.post("/api/chat")
async def chat(
    body: ChatBody,
    username: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    text = body.message.strip()
    if not text:
        raise HTTPException(status_code=422, detail="message must not be empty")
    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    reply = await answer_query(text, f"web:{user.id}", db)
    return {"reply": reply}


@app.get("/users/{user_id}/groups")
async def get_user_groups(
    user_id: int,
    _: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    result = await db.execute(
        select(UserGroup.group_id).where(UserGroup.user_id == user_id)
    )
    return [row[0] for row in result.all()]


@app.post("/users/{user_id}/groups")
async def set_user_groups(
    user_id: int,
    body: GroupAssignBody,
    _: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    user = await db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    existing = await db.execute(select(UserGroup).where(UserGroup.user_id == user_id))
    for ug in existing.scalars().all():
        await db.delete(ug)
    for gid in dict.fromkeys(body.group_ids):  # deduplicate preserving order
        db.add(UserGroup(user_id=user_id, group_id=gid))
    await db.commit()
    return {"user_id": user_id, "group_ids": list(dict.fromkeys(body.group_ids))}


@app.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    username: str = Depends(require_login),
    db: AsyncSession = Depends(get_db),
):
    update_count_sq = (
        select(func.count(IncidentUpdate.id))
        .where(IncidentUpdate.incident_id == Incident.id)
        .correlate(Incident)
        .scalar_subquery()
    )
    media_count_sq = (
        select(func.count(IncidentMedia.id))
        .where(IncidentMedia.incident_id == Incident.id)
        .correlate(Incident)
        .scalar_subquery()
    )
    query = (
        select(Incident, update_count_sq.label("uc"), media_count_sq.label("mc"))
        .where(~Incident.status.in_(_archived_statuses()))
        .order_by(Incident.received_at.desc())
    )
    allowed = await _get_allowed_groups(username, db)
    if allowed is not None:
        query = query.where(Incident.group_id.in_(allowed))
    result = await db.execute(query)
    rows = result.all()
    incidents_with_counts = [
        {"incident": i, "update_count": uc, "media_count": mc}
        for i, uc, mc in rows
    ]
    # Pass both variables: incidents_with_counts for future template use,
    # and incidents (list of Incident objects) for backward compat with current template.
    incidents = [row["incident"] for row in incidents_with_counts]
    user_result = await db.execute(select(User).where(User.username == username))
    user_obj = user_result.scalar_one_or_none()
    role = user_obj.role if user_obj else "user"
    cats_result = await db.execute(select(IncidentCategory).order_by(IncidentCategory.label))
    categories = cats_result.scalars().all()
    categories_json = [{"slug": c.slug, "label": c.label} for c in categories]
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "incidents": incidents,
            "incidents_with_counts": incidents_with_counts,
            "title": os.getenv("DASHBOARD_TITLE", "Ops Incident Monitor"),
            "username": username,
            "role": role,
            "mode": "live",
            "categories": categories,
            "categories_json": categories_json,
            "fleet_plate_mode": FLEET_PLATE_MODE,
            "lead_mode": LEAD_MODE,
            "terminal_statuses": sorted(_archived_statuses()),
        },
    )


@app.get("/archive", response_class=HTMLResponse)
async def archive_dashboard(
    request: Request,
    username: str = Depends(require_login),
    db: AsyncSession = Depends(get_db),
):
    update_count_sq = (
        select(func.count(IncidentUpdate.id))
        .where(IncidentUpdate.incident_id == Incident.id)
        .correlate(Incident)
        .scalar_subquery()
    )
    media_count_sq = (
        select(func.count(IncidentMedia.id))
        .where(IncidentMedia.incident_id == Incident.id)
        .correlate(Incident)
        .scalar_subquery()
    )
    query = (
        select(Incident, update_count_sq.label("uc"), media_count_sq.label("mc"))
        .where(Incident.status.in_(_archived_statuses()))
        .order_by(Incident.received_at.desc())
    )
    allowed = await _get_allowed_groups(username, db)
    if allowed is not None:
        query = query.where(Incident.group_id.in_(allowed))
    result = await db.execute(query)
    rows = result.all()
    incidents_with_counts = [
        {"incident": i, "update_count": uc, "media_count": mc}
        for i, uc, mc in rows
    ]
    incidents = [row["incident"] for row in incidents_with_counts]
    user_result = await db.execute(select(User).where(User.username == username))
    user_obj = user_result.scalar_one_or_none()
    role = user_obj.role if user_obj else "user"
    cats_result = await db.execute(select(IncidentCategory).order_by(IncidentCategory.label))
    categories = cats_result.scalars().all()
    categories_json = [{"slug": c.slug, "label": c.label} for c in categories]
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "incidents": incidents,
            "incidents_with_counts": incidents_with_counts,
            "title": os.getenv("DASHBOARD_TITLE", "Ops Incident Monitor"),
            "username": username,
            "role": role,
            "mode": "archive",
            "categories": categories,
            "categories_json": categories_json,
            "fleet_plate_mode": FLEET_PLATE_MODE,
            "lead_mode": LEAD_MODE,
            "terminal_statuses": sorted(_archived_statuses()),
        },
    )


@app.get("/overview", response_class=HTMLResponse)
async def overview(
    request: Request,
    username: str = Depends(require_login),
    db: AsyncSession = Depends(get_db),
):
    if not LEAD_MODE:
        raise HTTPException(status_code=404)
    user_result = await db.execute(select(User).where(User.username == username))
    user_obj = user_result.scalar_one_or_none()
    role = user_obj.role if user_obj else "user"

    allowed = await _get_allowed_groups(username, db)

    def _scope(q):
        return q.where(Incident.group_id.in_(allowed)) if allowed is not None else q

    kenya_tz = zoneinfo.ZoneInfo(SUMMARY_TIMEZONE)
    now = datetime.now(kenya_tz)
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)

    received_today = (await db.execute(_scope(
        select(func.count(Incident.id)).where(Incident.received_at >= start_of_day)
    ))).scalar_one()
    stat_new = (await db.execute(_scope(
        select(func.count(Incident.id)).where(Incident.status == "new")
    ))).scalar_one()
    stat_contacted = (await db.execute(_scope(
        select(func.count(Incident.id)).where(Incident.status == "contacted")
    ))).scalar_one()

    won_month_q = (
        select(func.count(IncidentStatusHistory.id))
        .select_from(IncidentStatusHistory)
        .join(Incident, Incident.id == IncidentStatusHistory.incident_id)
        .where(IncidentStatusHistory.to_status == "closed_won")
        .where(IncidentStatusHistory.changed_at >= start_of_month)
    )
    lost_month_q = (
        select(func.count(IncidentStatusHistory.id))
        .select_from(IncidentStatusHistory)
        .join(Incident, Incident.id == IncidentStatusHistory.incident_id)
        .where(IncidentStatusHistory.to_status == "closed_lost")
        .where(IncidentStatusHistory.changed_at >= start_of_month)
    )
    if allowed is not None:
        won_month_q = won_month_q.where(Incident.group_id.in_(allowed))
        lost_month_q = lost_month_q.where(Incident.group_id.in_(allowed))
    stat_won_month = (await db.execute(won_month_q)).scalar_one()
    stat_lost_month = (await db.execute(lost_month_q)).scalar_one()

    flow_rows = (await db.execute(_scope(
        select(Incident.status, func.count(Incident.id))
        .where(Incident.received_at >= start_of_day)
        .group_by(Incident.status)
    ))).all()
    flow_counts = {status: count for status, count in flow_rows}
    lead_flow = {
        "total": received_today,
        "new": flow_counts.get("new", 0),
        "contacted": flow_counts.get("contacted", 0),
        "closed_won": flow_counts.get("closed_won", 0),
        "closed_lost": flow_counts.get("closed_lost", 0),
    }

    # Conversion rate uses all-time current-status counts, matching spec §5's
    # "simple arithmetic on existing counts" wording (not month-scoped).
    all_contacted = stat_contacted
    all_won = (await db.execute(_scope(
        select(func.count(Incident.id)).where(Incident.status == "closed_won")
    ))).scalar_one()
    all_lost = (await db.execute(_scope(
        select(func.count(Incident.id)).where(Incident.status == "closed_lost")
    ))).scalar_one()
    denom = all_contacted + all_won + all_lost
    conversion_rate = round((all_won / denom) * 100, 1) if denom else 0.0

    newest_unactioned = (await db.execute(_scope(
        select(Incident).where(Incident.status == "new")
        .order_by(Incident.received_at.desc())
        .limit(5)
    ))).scalars().all()

    # received_at is stored as UTC but round-trips as a naive datetime on
    # SQLite (see the ticket.end_date normalization above); convert each
    # lead's timestamp to SUMMARY_TIMEZONE for display so the widget matches
    # every other Kenya-tz-scoped figure on this page.
    newest_unactioned_local_times = {}
    for lead in newest_unactioned:
        lead_received_at = lead.received_at
        if lead_received_at.tzinfo is None:
            lead_received_at = lead_received_at.replace(tzinfo=timezone.utc)
        newest_unactioned_local_times[lead.id] = lead_received_at.astimezone(kenya_tz).strftime("%H:%M")

    return templates.TemplateResponse(
        "overview.html",
        {
            "request": request,
            "title": os.getenv("DASHBOARD_TITLE", "Ops Incident Monitor"),
            "username": username,
            "role": role,
            "stat_received_today": received_today,
            "stat_new": stat_new,
            "stat_contacted": stat_contacted,
            "stat_won_month": stat_won_month,
            "stat_lost_month": stat_lost_month,
            "lead_flow": lead_flow,
            "conversion_rate": conversion_rate,
            "newest_unactioned": newest_unactioned,
            "newest_unactioned_local_times": newest_unactioned_local_times,
        },
    )


@app.get("/summaries", response_class=HTMLResponse)
async def summaries_page(
    request: Request,
    date: Optional[str] = None,
    username: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    kenya_tz = zoneinfo.ZoneInfo(SUMMARY_TIMEZONE)
    today = datetime.now(kenya_tz).date().isoformat()
    try:
        selected_date = _date.fromisoformat(date).isoformat() if date else today
    except ValueError:
        selected_date = today
    user_result = await db.execute(select(User).where(User.username == username))
    user_obj = user_result.scalar_one_or_none()
    role = user_obj.role if user_obj else "user"
    return templates.TemplateResponse(
        "summaries.html",
        {
            "request": request,
            "username": username,
            "role": role,
            "selected_date": selected_date,
            "today": today,
        },
    )


@app.get("/admin/profile", response_class=HTMLResponse)
async def admin_profile_page(
    request: Request,
    username: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    user_result = await db.execute(select(User).where(User.username == username))
    user_obj = user_result.scalar_one_or_none()
    role = user_obj.role if user_obj else "user"
    return templates.TemplateResponse(
        "profile.html",
        {
            "request": request,
            "username": username,
            "role": role,
        },
    )


# ---------------------------------------------------------------------------
# Settings page + WhatsApp reconnect API
# ---------------------------------------------------------------------------

async def _openwa_find_session() -> tuple[str | None, str | None]:
    """Return (session_uuid, status) for the configured OPENWA_SESSION, or (None, None)."""
    import whatsapp as _wa
    async with httpx.AsyncClient(timeout=10.0) as client:
        r = await client.get(
            f"{_wa.OPENWA_URL}/api/sessions",
            headers={"X-API-Key": _wa.OPENWA_API_KEY},
        )
        r.raise_for_status()
        for s in r.json():
            if s.get("name") == _wa.OPENWA_SESSION:
                return s["id"], s.get("status", "UNKNOWN")
    return None, None


@app.get("/billing", response_class=HTMLResponse)
async def billing_page(
    request: Request,
    username: str = Depends(require_login),
    db: AsyncSession = Depends(get_db),
):
    user_result = await db.execute(select(User).where(User.username == username))
    user_obj = user_result.scalar_one_or_none()
    role = user_obj.role if user_obj else "user"
    statement = None
    error = None
    if BILLING_SERVICE_URL and CLIENT_SUBDOMAIN:
        try:
            import httpx as _httpx
            async with _httpx.AsyncClient(timeout=8.0) as _http:
                r = await _http.get(
                    f"{BILLING_SERVICE_URL}/api/clients/{CLIENT_SUBDOMAIN}/statement",
                    headers={"X-Billing-Secret": BILLING_WEBHOOK_SECRET},
                )
                if r.status_code == 200:
                    statement = r.json()
                else:
                    error = f"Billing service returned {r.status_code}"
        except Exception as exc:
            error = f"Could not reach billing service: {exc}"
    elif not CLIENT_SUBDOMAIN:
        error = "CLIENT_SUBDOMAIN not configured"
    elif not BILLING_SERVICE_URL:
        error = "BILLING_SERVICE_URL not configured"
    return templates.TemplateResponse(
        "billing.html",
        {"request": request, "username": username, "role": role,
         "statement": statement, "error": error},
    )


@app.get("/settings", response_class=HTMLResponse)
async def settings_page(
    request: Request,
    username: str = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    user_result = await db.execute(select(User).where(User.username == username))
    user_obj = user_result.scalar_one_or_none()
    role = user_obj.role if user_obj else "user"
    return templates.TemplateResponse(
        "settings.html",
        {"request": request, "username": username, "role": role},
    )


@app.get("/api/settings/whatsapp-status")
async def api_whatsapp_status(_: str = Depends(require_admin)):
    try:
        session_id, status = await _openwa_find_session()
        if not session_id:
            return JSONResponse({"status": "NOT_FOUND"})
        return JSONResponse({"status": status, "id": session_id})
    except Exception as exc:
        return JSONResponse({"status": "ERROR", "detail": str(exc)}, status_code=200)


@app.post("/api/settings/whatsapp-reconnect")
async def api_whatsapp_reconnect(_: str = Depends(require_admin)):
    import whatsapp as _wa
    try:
        session_id, _ = await _openwa_find_session()
        async with httpx.AsyncClient(timeout=15.0) as client:
            if not session_id:
                create_r = await client.post(
                    f"{_wa.OPENWA_URL}/api/sessions",
                    headers={"X-API-Key": _wa.OPENWA_API_KEY, "Content-Type": "application/json"},
                    json={"name": _wa.OPENWA_SESSION},
                )
                if create_r.status_code == 409:
                    session_id, _ = await _openwa_find_session()
                else:
                    create_r.raise_for_status()
                    session_id = create_r.json()["id"]
            else:
                await client.post(
                    f"{_wa.OPENWA_URL}/api/sessions/{session_id}/stop",
                    headers={"X-API-Key": _wa.OPENWA_API_KEY},
                )
            r = await client.post(
                f"{_wa.OPENWA_URL}/api/sessions/{session_id}/start",
                headers={"X-API-Key": _wa.OPENWA_API_KEY},
            )
            return JSONResponse({"ok": r.status_code < 400, "status": r.status_code})
    except Exception as exc:
        return JSONResponse({"ok": False, "detail": str(exc)}, status_code=500)


@app.get("/api/settings/whatsapp-qr")
async def api_whatsapp_qr(_: str = Depends(require_admin)):
    import whatsapp as _wa
    try:
        session_id, _ = await _openwa_find_session()
        if not session_id:
            return JSONResponse({"error": "Session not found"}, status_code=404)
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(
                f"{_wa.OPENWA_URL}/api/sessions/{session_id}/qr",
                headers={"X-API-Key": _wa.OPENWA_API_KEY},
            )
            if r.status_code == 200:
                return JSONResponse(r.json())
            sr = await client.get(
                f"{_wa.OPENWA_URL}/api/sessions/{session_id}",
                headers={"X-API-Key": _wa.OPENWA_API_KEY},
            )
            return JSONResponse({"status": sr.json().get("status", "UNKNOWN")})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


_GROUP_JID_RE = re.compile(r"[\w-]+@g\.us")


@app.get("/api/settings/whatsapp-groups")
async def api_settings_whatsapp_groups(_: str = Depends(require_admin)):
    groups = await list_whatsapp_groups()
    return {"groups": groups}


@app.get("/api/settings/ticket-groups")
async def settings_ticket_groups(
    username: str = Depends(require_admin),
):
    allowed_groups = await _get_allowed_ticket_groups()
    tier_limit = None
    if BILLING_SERVICE_URL and CLIENT_SUBDOMAIN:
        try:
            async with httpx.AsyncClient(timeout=5.0) as http:
                r = await http.get(
                    f"{BILLING_SERVICE_URL}/api/clients/{CLIENT_SUBDOMAIN}/ticket-groups",
                    headers={"X-Billing-Secret": BILLING_WEBHOOK_SECRET},
                )
                r.raise_for_status()
                tier_limit = r.json().get("tier_limit")
        except Exception:
            logger.warning("Ticket-groups tier-limit fetch failed")
    return {"allowed_groups": allowed_groups, "tier_limit": tier_limit}


@app.post("/api/settings/ticket-groups/add")
async def settings_add_ticket_group(
    body: TicketGroupAddBody,
    username: str = Depends(require_admin),
):
    group_id = body.group_id.strip()
    if not _GROUP_JID_RE.fullmatch(group_id):
        raise HTTPException(status_code=422, detail="group_id doesn't look like a WhatsApp group JID")
    if not BILLING_SERVICE_URL or not CLIENT_SUBDOMAIN:
        raise HTTPException(status_code=503, detail="Billing service not configured")
    try:
        async with httpx.AsyncClient(timeout=8.0) as http:
            r = await http.post(
                f"{BILLING_SERVICE_URL}/api/clients/{CLIENT_SUBDOMAIN}/ticket-groups/add",
                json={"group_id": group_id},
                headers={"X-Billing-Secret": BILLING_WEBHOOK_SECRET},
            )
            r.raise_for_status()
            global _ticket_groups_cache
            _ticket_groups_cache = None
            return r.json()
    except HTTPException:
        raise
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=e.response.json().get("detail", "Request failed"))
    except Exception:
        logger.warning("Ticket-group add proxy to billing failed")
        raise HTTPException(status_code=502, detail="Could not reach billing service")


@app.post("/api/settings/ticket-groups/upgrade")
async def settings_upgrade_ticket_tier(
    body: TicketGroupUpgradeBody,
    username: str = Depends(require_admin),
):
    group_id = body.group_id.strip()
    if not _GROUP_JID_RE.fullmatch(group_id):
        raise HTTPException(status_code=422, detail="group_id doesn't look like a WhatsApp group JID")
    if not BILLING_SERVICE_URL or not CLIENT_SUBDOMAIN:
        raise HTTPException(status_code=503, detail="Billing service not configured")
    try:
        async with httpx.AsyncClient(timeout=8.0) as http:
            r = await http.post(
                f"{BILLING_SERVICE_URL}/api/clients/{CLIENT_SUBDOMAIN}/ticket-groups/upgrade",
                json={"group_id": group_id, "phone": body.phone.strip()},
                headers={"X-Billing-Secret": BILLING_WEBHOOK_SECRET},
            )
            r.raise_for_status()
            global _ticket_groups_cache
            _ticket_groups_cache = None
            return r.json()
    except HTTPException:
        raise
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=e.response.status_code, detail=e.response.json().get("detail", "Request failed"))
    except Exception:
        logger.warning("Ticket-group upgrade proxy to billing failed")
        raise HTTPException(status_code=502, detail="Could not reach billing service")


# ---------------------------------------------------------------------------
# Super-admin category management
# ---------------------------------------------------------------------------

@app.get("/api/super-admin/categories")
async def list_categories(
    _: str = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(
        select(
            IncidentCategory,
            func.count(Incident.id).label("incident_count"),
        )
        .outerjoin(Incident, Incident.category == IncidentCategory.slug)
        .group_by(IncidentCategory.id)
        .order_by(IncidentCategory.id)
    )
    return [
        {
            "id": cat.id,
            "slug": cat.slug,
            "label": cat.label,
            "is_protected": cat.is_protected,
            "incident_count": count,
        }
        for cat, count in result.all()
    ]


@app.post("/api/super-admin/categories", status_code=201)
async def create_category(
    body: CreateCategoryBody,
    _: str = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    existing = await db.execute(
        select(IncidentCategory).where(IncidentCategory.slug == body.slug)
    )
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="slug already exists")
    cat = IncidentCategory(
        slug=body.slug,
        label=body.label,
        is_protected=False,
        created_at=datetime.now(timezone.utc),
    )
    db.add(cat)
    await db.commit()
    await db.refresh(cat)
    return {
        "id": cat.id,
        "slug": cat.slug,
        "label": cat.label,
        "is_protected": cat.is_protected,
        "created_at": cat.created_at.isoformat(),
    }


@app.get("/api/super-admin/categories/{slug}/usage")
async def category_usage(
    slug: str,
    _: str = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    cat = await db.execute(
        select(IncidentCategory).where(IncidentCategory.slug == slug)
    )
    if not cat.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Category not found")
    count_result = await db.execute(
        select(func.count(Incident.id)).where(Incident.category == slug)
    )
    return {"slug": slug, "incident_count": count_result.scalar() or 0}


@app.post("/api/super-admin/categories/{slug}/delete", status_code=204)
async def delete_category(
    slug: str,
    body: Optional[DeleteCategoryBody] = Body(None),
    _: str = Depends(require_super_admin),
    db: AsyncSession = Depends(get_db),
):
    cat_result = await db.execute(
        select(IncidentCategory).where(IncidentCategory.slug == slug)
    )
    cat = cat_result.scalar_one_or_none()
    if not cat:
        raise HTTPException(status_code=404, detail="Category not found")
    if cat.is_protected:
        raise HTTPException(status_code=403, detail="Cannot delete a protected category")

    count_result = await db.execute(
        select(func.count(Incident.id)).where(Incident.category == slug)
    )
    incident_count = count_result.scalar() or 0
    remap_to = body.remap_to if body else None

    if incident_count > 0 and not remap_to:
        return JSONResponse(
            status_code=409,
            content={"incident_count": incident_count, "message": f"{incident_count} incidents use '{slug}'. Provide remap_to."},
        )

    if remap_to:
        remap_cat = await db.execute(
            select(IncidentCategory).where(IncidentCategory.slug == remap_to)
        )
        if not remap_cat.scalar_one_or_none():
            raise HTTPException(status_code=422, detail="remap_to slug does not exist")
        await db.execute(
            sa_update(Incident)
            .where(Incident.category == slug)
            .values(category=remap_to)
        )

    await db.delete(cat)
    await db.commit()
