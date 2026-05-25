import logging
import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from classifier import classify_message
from database import get_db, init_db
from models import Incident
from odoo_stub import push_incident

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
    if x_api_key != GATEWAY_SECRET_TOKEN:
        raise HTTPException(status_code=401, detail="Unauthorized")

    payload = await request.json()
    event_type = payload.get("event")
    data = payload.get("data", {})

    if event_type != "message" or data.get("type") != "chat" or not data.get("isGroup", False):
        return {"status": "ignored", "message": "Non-group or non-chat event skipped"}

    group_id = data.get("chatId") or data.get("from", "")
    group_name = data.get("chat", {}).get("name", "Unmapped Property Group")
    sender = data.get("sender", {})
    reporter_name = sender.get("name") or sender.get("pushname") or "Anonymous"
    reporter_phone = data.get("author", "").split("@")[0]
    message_body = data.get("body", "").strip()
    epoch = data.get("timestamp", datetime.now(timezone.utc).timestamp())
    received_at = datetime.fromtimestamp(epoch, tz=timezone.utc)

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
        status="new",
        received_at=received_at,
    )
    db.add(incident)
    await db.commit()
    await db.refresh(incident)
    await push_incident(incident)

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
        },
    )
