import hmac
import logging
import os
import re
import sys
import zoneinfo
from contextlib import asynccontextmanager
from datetime import date as _date, datetime, timedelta, timezone
from typing import Optional

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, field_validator
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from starlette.middleware.sessions import SessionMiddleware

from auth import require_login, require_admin, hash_password, verify_password, check_incident_group_access
from chat import answer_query
from classifier import classify_message, classify_update_or_new
from database import get_db, init_db, AsyncSessionLocal
from media import MEDIA_DIR, download_media
from models import Incident, IncidentMedia, IncidentStatusHistory, IncidentUpdate, User, UserGroup, AuditLog, AdminProfile, AdminGroupSubscription
from odoo_stub import push_incident
from summaries import build_summary, format_whatsapp_summary, window_for_date
from whatsapp import reply_to_message, send_group_message

_VALID_STATUSES = {"new", "review", "acknowledged", "resolved", "ignored"}
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


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

GATEWAY_SECRET_TOKEN = os.getenv("GATEWAY_SECRET_TOKEN", "change-me")
SUMMARY_TIMEZONE = os.getenv("SUMMARY_TIMEZONE", "Africa/Nairobi")
try:
    SUMMARY_SCHEDULE_HOUR = int(os.getenv("SUMMARY_SCHEDULE_HOUR", "8"))
except ValueError:
    logger.warning("Invalid SUMMARY_SCHEDULE_HOUR env var, using default of 8")
    SUMMARY_SCHEDULE_HOUR = 8
DASHBOARD_URL = os.getenv("DASHBOARD_URL", "http://localhost:8000")
SECRET_KEY = os.getenv("SECRET_KEY", "")
if not SECRET_KEY or SECRET_KEY == "change-me":
    logger.error("SECRET_KEY env var is not set or is the default value. Refusing to start.")
    sys.exit(1)


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


@app.get("/health")
async def health():
    return {"status": "ok"}


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


async def _get_open_tickets(db: AsyncSession, group_id: str) -> list[dict]:
    result = await db.execute(
        select(Incident)
        .where(Incident.group_id == group_id)
        .where(~Incident.status.in_(["resolved", "ignored"]))
        .order_by(Incident.received_at.desc())
        .limit(5)
    )
    return [
        {"id": i.id, "category": i.category, "message_body": i.message_body}
        for i in result.scalars().all()
    ]


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


async def _get_allowed_groups(username: str, db: AsyncSession) -> Optional[list[str]]:
    """Returns list of allowed group_ids for a user-role user, or None for admins (no filter)."""
    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()
    if not user:
        return []  # fail-closed: unknown user sees nothing
    if user.role == "admin":
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
    classification = await classify_message(message_body)
    if not classification["is_incident"] or classification["confidence"] < MIN_CONFIDENCE:
        return {"status": "noise", "message": "Message classified as non-incident"}

    open_tickets = await _get_open_tickets(db, group_id)
    if open_tickets:
        routing = await classify_update_or_new(message_body, open_tickets)
    else:
        routing = {"routing": "new"}

    if routing["routing"] == "update":
        incident_id = routing["ticket_id"]
        update = IncidentUpdate(
            incident_id=incident_id,
            message_id=message_id,
            reporter_name=reporter_name,
            reporter_phone=reporter_phone,
            message_body=message_body,
            received_at=received_at,
            ai_linked=True,
        )
        try:
            db.add(update)
            parent = await db.get(Incident, incident_id)
            if parent:
                parent.updated_at = received_at
            await db.commit()
        except IntegrityError:
            await db.rollback()
            return {"status": "duplicate", "message": "Message already processed"}
        except Exception as exc:
            await db.rollback()
            logger.error("DB commit failed for update: %s", exc)
            return {"status": "error", "message": "Update could not be persisted"}

        logger.info("[UPDATE] incident_id=%d reporter=%s", incident_id, reporter_name)
        return {"status": "staged_update", "incident_id": incident_id}

    incident = Incident(
        group_id=group_id,
        property_name=group_name,
        reporter_name=reporter_name,
        reporter_phone=reporter_phone,
        message_body=message_body,
        category=classification["category"],
        severity=classification["severity"],
        confidence=classification["confidence"],
        status="review",
        received_at=received_at,
        message_id=message_id,
    )
    try:
        db.add(incident)
        await db.flush()
        db.add(IncidentStatusHistory(
            incident_id=incident.id,
            from_status=None,
            to_status="review",
            changed_at=received_at,
        ))
        await db.commit()
        await db.refresh(incident)
    except IntegrityError:
        await db.rollback()
        return {"status": "duplicate", "message": "Message already processed"}
    except Exception as exc:
        await db.rollback()
        logger.error("DB commit failed: %s", exc)
        return {"status": "error", "message": "Incident could not be persisted"}

    try:
        await push_incident(incident)
    except Exception as exc:
        logger.error("push_incident failed: %s", exc)

    logger.info(
        "[INCIDENT] property=%s category=%s severity=%s confidence=%.2f",
        group_name,
        classification["category"],
        classification["severity"],
        classification["confidence"],
    )
    return {
        "status": "staged",
        "property": group_name,
        "category": classification["category"],
        "severity": classification["severity"],
    }


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
    _created_incident: Optional[Incident] = None

    if caption:
        classification = await classify_message(caption)
        if classification["is_incident"] and classification["confidence"] >= MIN_CONFIDENCE:
            open_tickets = await _get_open_tickets(db, group_id)
            if open_tickets:
                routing = await classify_update_or_new(caption, open_tickets)
            else:
                routing = {"routing": "new"}

            if routing["routing"] == "update":
                parent_id = routing["ticket_id"]
                upd = IncidentUpdate(
                    incident_id=parent_id,
                    message_id=message_id,
                    reporter_name=reporter_name,
                    reporter_phone=reporter_phone,
                    message_body=caption,
                    received_at=received_at,
                    ai_linked=True,
                )
                try:
                    db.add(upd)
                    parent = await db.get(Incident, parent_id)
                    if parent:
                        parent.updated_at = received_at
                    await db.flush()
                    incident_id = parent_id
                    update_id = upd.id
                except IntegrityError:
                    await db.rollback()
                    return {"status": "duplicate", "message": "Message already processed"}
            else:
                new_inc = Incident(
                    group_id=group_id,
                    property_name=group_name,
                    reporter_name=reporter_name,
                    reporter_phone=reporter_phone,
                    message_body=caption,
                    category=classification["category"],
                    severity=classification["severity"],
                    confidence=classification["confidence"],
                    status="review",
                    received_at=received_at,
                    message_id=message_id,
                )
                try:
                    db.add(new_inc)
                    await db.flush()
                    incident_id = new_inc.id
                    _created_incident = new_inc
                except IntegrityError:
                    await db.rollback()
                    return {"status": "duplicate", "message": "Message already processed"}

    if incident_id is None and media_url:
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

    if _created_incident is not None:
        try:
            await push_incident(_created_incident)
        except Exception as exc:
            logger.error("push_incident failed for media incident: %s", exc)

    if incident_id is None:
        logger.warning("Orphaned media: no open ticket in group %s and no classified caption", group_id)
        return {"status": "staged_media", "message": "Media saved but no open ticket found"}

    return {"status": "staged_media", "incident_id": incident_id}


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
    group_name = (
        data.get("chatName")
        or (data.get("chat") or {}).get("name")
        or (group_id.split("@")[0] if group_id else "Unmapped Property Group")
    )
    message_id: Optional[str] = data.get("id") or None

    if message_id:
        existing_inc = await db.execute(select(Incident).where(Incident.message_id == message_id))
        if existing_inc.scalar_one_or_none():
            return {"status": "duplicate", "message": "Message already processed"}
        existing_upd = await db.execute(
            select(IncidentUpdate).where(IncidentUpdate.message_id == message_id)
        )
        if existing_upd.scalar_one_or_none():
            return {"status": "duplicate", "message": "Message already processed"}

    reporter_name = (data.get("notifyName") or "").strip() or "Unknown"
    reporter_phone = (data.get("author") or "").split("@")[0].strip() or None
    epoch = data.get("timestamp") or datetime.now(timezone.utc).timestamp()
    received_at = datetime.fromtimestamp(epoch, tz=timezone.utc)

    if msg_type == "chat":
        message_body = data.get("body", "").strip()[:4000]
        if not message_body:
            return {"status": "ignored", "message": "Empty message body"}
        return await _handle_text_ingest(
            db, group_id, group_name, reporter_name, reporter_phone,
            message_body, received_at, message_id,
        )

    # Media message
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
            "category": i.category,
            "severity": i.severity,
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

    return {
        "id": incident.id,
        "property_name": incident.property_name,
        "reporter_name": incident.reporter_name,
        "reporter_phone": incident.reporter_phone,
        "category": incident.category,
        "severity": incident.severity,
        "confidence": round(incident.confidence, 2),
        "status": incident.status,
        "message_body": incident.message_body,
        "received_at": incident.received_at.isoformat(),
        "updated_at": incident.updated_at.isoformat() if incident.updated_at else None,
        "updates": update_rows,
        "media": media_rows,
        "status_history": history_rows,
        "audit_log": audit_rows,
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
            severity="low",
            confidence=0.0,
            status="review",
            received_at=update.received_at,
            message_id=update.message_id,
        )
        db.add(new_incident)
        await db.flush()
        db.add(IncidentStatusHistory(
            incident_id=new_incident.id,
            from_status=None,
            to_status="review",
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
    if body.status not in _VALID_STATUSES:
        raise HTTPException(status_code=422, detail=f"status must be one of {sorted(_VALID_STATUSES)}")
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
        if incident.message_id:
            wa_message_id = await reply_to_message(incident.group_id, incident.message_id, text)
        else:
            wa_message_id = await send_group_message(incident.group_id, text)
    except Exception as exc:
        logger.error("send_group_message failed: %s", exc)
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
            "role": "admin",
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
        .where(~Incident.status.in_(["resolved"]))
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
        .where(Incident.status == "resolved")
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
