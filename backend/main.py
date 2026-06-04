import hmac
import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from pydantic import BaseModel
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from classifier import classify_message
from database import get_db, init_db
from models import Incident
from odoo_stub import push_incident

_VALID_STATUSES = {"new", "review", "acknowledged", "resolved", "ignored"}


class StatusUpdate(BaseModel):
    status: str


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


@app.get("/health")
async def health():
    return {"status": "ok"}


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

    if event_type != "message.received" or data.get("type") != "chat" or not data.get("isGroup", False):
        return {"status": "ignored", "message": "Non-group or non-chat event skipped"}

    group_id = data.get("chatId") or data.get("from", "")
    group_name = (data.get("chat") or {}).get("name") or (group_id.split("@")[0] if group_id else "Unmapped Property Group")
    message_id: Optional[str] = data.get("id") or None

    if message_id:
        existing = await db.execute(select(Incident).where(Incident.message_id == message_id))
        if existing.scalar_one_or_none():
            return {"status": "duplicate", "message": "Message already processed"}

    reporter_name = (data.get("notifyName") or "").strip() or "Unknown"
    reporter_phone = (data.get("author") or "").split("@")[0].strip() or None
    message_body = data.get("body", "").strip()[:4000]
    epoch = data.get("timestamp") or datetime.now(timezone.utc).timestamp()
    received_at = datetime.fromtimestamp(epoch, tz=timezone.utc)

    if not message_body:
        return {"status": "ignored", "message": "Empty message body"}

    classification = await classify_message(message_body)

    if not classification["is_incident"] or classification["confidence"] < MIN_CONFIDENCE:
        return {"status": "noise", "message": "Message classified as non-incident"}

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


@app.get("/incidents")
async def list_incidents(db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Incident).order_by(Incident.received_at.desc()))
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
        }
        for i in result.scalars().all()
    ]


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
    incident.status = body.status
    await db.commit()
    return {"id": incident.id, "status": incident.status}


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, db: AsyncSession = Depends(get_db)):
    result = await db.execute(select(Incident).order_by(Incident.received_at.desc()))
    incidents = result.scalars().all()
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "incidents": incidents,
            "title": os.getenv("DASHBOARD_TITLE", "Ops Incident Monitor"),
            "api_key": GATEWAY_SECRET_TOKEN,
        },
    )
