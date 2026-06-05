import hmac
import logging
import os
import socket
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from classifier import classify_message, classify_update_or_new
from database import get_db, init_db
from media import MEDIA_DIR, download_media
from models import Incident, IncidentMedia, IncidentStatusHistory, IncidentUpdate
from odoo_stub import push_incident
from whatsapp import reply_to_message, send_group_message

_VALID_STATUSES = {"new", "review", "acknowledged", "resolved", "ignored"}
_MEDIA_TYPES = {"image", "video", "document", "audio"}


class StatusUpdate(BaseModel):
    status: str


class RelinkBody(BaseModel):
    incident_id: Optional[int]


class ReplyBody(BaseModel):
    text: str


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

GATEWAY_SECRET_TOKEN = os.getenv("GATEWAY_SECRET_TOKEN", "change-me")
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
    yield


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


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/webhook-url")
async def webhook_url():
    ip = socket.gethostbyname(socket.gethostname())
    return {"url": f"http://{ip}:8000/api/v1/ops/ingest"}


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
    msg_type = data.get("type", "")

    if event_type != "message.received" or not data.get("isGroup", False):
        return {"status": "ignored", "message": "Non-group or non-chat event skipped"}

    if msg_type != "chat" and msg_type not in _MEDIA_TYPES:
        return {"status": "ignored", "message": "Non-group or non-chat event skipped"}

    group_id = data.get("chatId") or data.get("from", "")
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
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(Incident).where(Incident.id == incident_id))
    incident = result.scalar_one_or_none()
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")

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
    x_api_key: str = Header(None, alias="X-API-Key"),
    db: AsyncSession = Depends(get_db),
):
    if not hmac.compare_digest(x_api_key or "", GATEWAY_SECRET_TOKEN):
        raise HTTPException(status_code=401, detail="Unauthorized")

    result = await db.execute(select(IncidentUpdate).where(IncidentUpdate.id == update_id))
    update = result.scalar_one_or_none()
    if not update:
        raise HTTPException(status_code=404, detail="Update not found")

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
        await db.commit()
        return {"update_id": update_id, "incident_id": new_incident.id, "promoted": True}

    target = await db.get(Incident, body.incident_id)
    if not target:
        raise HTTPException(status_code=404, detail="Target incident not found")

    update.incident_id = body.incident_id
    update.ai_linked = False
    update.relinked = True
    media_res = await db.execute(
        select(IncidentMedia).where(IncidentMedia.update_id == update_id)
    )
    for m in media_res.scalars().all():
        m.incident_id = body.incident_id
    target.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return {"update_id": update_id, "incident_id": body.incident_id}


@app.patch("/incidents/{incident_id}/status")
async def update_incident_status(
    incident_id: int,
    body: StatusUpdate,
    x_api_key: str = Header(None, alias="X-API-Key"),
    db: AsyncSession = Depends(get_db),
):
    if not hmac.compare_digest(x_api_key or "", GATEWAY_SECRET_TOKEN):
        raise HTTPException(status_code=401, detail="Unauthorized")
    if body.status not in _VALID_STATUSES:
        raise HTTPException(status_code=422, detail=f"status must be one of {sorted(_VALID_STATUSES)}")
    result = await db.execute(select(Incident).where(Incident.id == incident_id))
    incident = result.scalar_one_or_none()
    if not incident:
        raise HTTPException(status_code=404, detail="Incident not found")
    old_status = incident.status
    incident.status = body.status
    db.add(IncidentStatusHistory(
        incident_id=incident_id,
        from_status=old_status,
        to_status=body.status,
        changed_at=datetime.now(timezone.utc),
    ))
    await db.commit()
    return {"id": incident.id, "status": incident.status}


@app.post("/incidents/{incident_id}/reply")
async def reply_to_incident(
    incident_id: int,
    body: ReplyBody,
    x_api_key: str = Header(None, alias="X-API-Key"),
    db: AsyncSession = Depends(get_db),
):
    if not hmac.compare_digest(x_api_key or "", GATEWAY_SECRET_TOKEN):
        raise HTTPException(status_code=401, detail="Unauthorized")

    text = body.text.strip()
    if not text:
        raise HTTPException(status_code=422, detail="text must not be empty")
    text = text[:4000]

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
    update = IncidentUpdate(
        incident_id=incident_id,
        message_id=wa_message_id,
        reporter_name="Dashboard",
        reporter_phone=None,
        message_body=text,
        received_at=now,
        ai_linked=False,
    )
    db.add(update)
    incident.updated_at = now
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


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, db: AsyncSession = Depends(get_db)):
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
    result = await db.execute(
        select(Incident, update_count_sq.label("uc"), media_count_sq.label("mc"))
        .where(~Incident.status.in_(["resolved"]))
        .order_by(Incident.received_at.desc())
    )
    rows = result.all()
    incidents_with_counts = [
        {"incident": i, "update_count": uc, "media_count": mc}
        for i, uc, mc in rows
    ]
    # Pass both variables: incidents_with_counts for future template use,
    # and incidents (list of Incident objects) for backward compat with current template.
    incidents = [row["incident"] for row in incidents_with_counts]
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "incidents": incidents,
            "incidents_with_counts": incidents_with_counts,
            "title": os.getenv("DASHBOARD_TITLE", "Ops Incident Monitor"),
            "api_key": GATEWAY_SECRET_TOKEN,
            "mode": "live",
        },
    )


@app.get("/archive", response_class=HTMLResponse)
async def archive_dashboard(request: Request, db: AsyncSession = Depends(get_db)):
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
    result = await db.execute(
        select(Incident, update_count_sq.label("uc"), media_count_sq.label("mc"))
        .where(Incident.status == "resolved")
        .order_by(Incident.received_at.desc())
    )
    rows = result.all()
    incidents_with_counts = [
        {"incident": i, "update_count": uc, "media_count": mc}
        for i, uc, mc in rows
    ]
    incidents = [row["incident"] for row in incidents_with_counts]
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "incidents": incidents,
            "incidents_with_counts": incidents_with_counts,
            "title": os.getenv("DASHBOARD_TITLE", "Ops Incident Monitor"),
            "api_key": GATEWAY_SECRET_TOKEN,
            "mode": "archive",
        },
    )
