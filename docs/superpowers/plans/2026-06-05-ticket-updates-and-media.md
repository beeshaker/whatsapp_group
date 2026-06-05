# Ticket Updates & Media Attachments — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add two-stage LLM routing so follow-up WhatsApp messages link to existing tickets, and download/attach media files (images, videos, documents) from WhatsApp to those tickets with dashboard visibility.

**Architecture:** A new `classify_update_or_new()` classifier function handles Stage 2 routing. The ingest handler branches on message `type` — `chat` messages go through both classifier stages, media messages additionally download the file via a `download_media()` helper. Three new DB tables (`incident_updates`, `incident_media`, plus `updated_at` on `incidents`) back the data model. New API endpoints serve media files and support admin re-linking.

**Tech Stack:** FastAPI, SQLAlchemy 2.0 async ORM, SQLite (dev) / PostgreSQL (prod), httpx (already installed), Jinja2, vanilla JS dashboard.

---

## File Map

| File | Change |
|---|---|
| `backend/models.py` | Add `updated_at` to `Incident`; add `IncidentUpdate`, `IncidentMedia` models |
| `backend/database.py` | Add `ALTER TABLE` migration for `updated_at` |
| `backend/classifier.py` | Add `classify_update_or_new()` |
| `backend/media.py` | New — `download_media()` helper |
| `backend/main.py` | Refactor ingest into helpers; add Stage 2 routing; add 4 new endpoints; update dashboard route |
| `backend/tests/test_classifier.py` | Add tests for `classify_update_or_new` |
| `backend/tests/test_media.py` | New — tests for `download_media` |
| `backend/tests/test_updates.py` | New — integration tests for update routing and media ingest |
| `backend/tests/test_api.py` | New — tests for new API endpoints |
| `backend/templates/dashboard.html` | Add badges, detail modal, re-link UI |
| `docker-compose.yml` | Add `media_data` volume |

---

## Task 1: Data Models and DB Migrations

**Files:**
- Modify: `backend/models.py`
- Modify: `backend/database.py`

- [ ] **Step 1: Replace the contents of `backend/models.py`**

```python
from datetime import datetime
from typing import Optional
from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from database import Base


class Incident(Base):
    __tablename__ = "incidents"
    __table_args__ = (UniqueConstraint("message_id", name="uq_incidents_message_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    group_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    property_name: Mapped[str] = mapped_column(Text, nullable=False)
    reporter_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    reporter_phone: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    message_body: Mapped[str] = mapped_column(Text, nullable=False)
    category: Mapped[str] = mapped_column(String(50), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="review", server_default="review")
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    message_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)


class IncidentUpdate(Base):
    __tablename__ = "incident_updates"
    __table_args__ = (UniqueConstraint("message_id", name="uq_incident_updates_message_id"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    incident_id: Mapped[int] = mapped_column(Integer, ForeignKey("incidents.id"), nullable=False, index=True)
    message_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    reporter_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    reporter_phone: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    message_body: Mapped[str] = mapped_column(Text, nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ai_linked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class IncidentMedia(Base):
    __tablename__ = "incident_media"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    incident_id: Mapped[int] = mapped_column(Integer, ForeignKey("incidents.id"), nullable=False, index=True)
    update_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("incident_updates.id"), nullable=True)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    mimetype: Mapped[str] = mapped_column(Text, nullable=False)
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
```

- [ ] **Step 2: Add `updated_at` migration to `backend/database.py`**

Add this block inside `init_db()`, after the existing `message_id` migration blocks:

```python
    try:
        async with engine.begin() as conn:
            await conn.execute(text("ALTER TABLE incidents ADD COLUMN updated_at TIMESTAMP"))
    except Exception:
        pass
```

- [ ] **Step 3: Verify the test suite still passes (schema check)**

Run:
```bash
cd backend && python -m pytest tests/ -v -x
```
Expected: all existing tests pass (new tables are created by `Base.metadata.create_all` in conftest).

- [ ] **Step 4: Commit**

```bash
git add backend/models.py backend/database.py
git commit -m "feat: add IncidentUpdate, IncidentMedia models and updated_at migration"
```

---

## Task 2: `classify_update_or_new` Classifier Function

**Files:**
- Modify: `backend/classifier.py`
- Modify: `backend/tests/test_classifier.py`

- [ ] **Step 1: Write failing tests — add to end of `backend/tests/test_classifier.py`**

```python
from classifier import classify_update_or_new


async def test_classify_update_or_new_returns_new_when_no_open_tickets():
    result = await classify_update_or_new("More water leaking", [])
    assert result == {"routing": "new"}


async def test_classify_update_or_new_llm_says_new():
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"response": '{"routing": "new"}'}
    open_tickets = [{"id": 1, "category": "plumbing", "message_body": "Pump leaking floor 3"}]
    with patch("classifier.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.post = AsyncMock(return_value=mock_resp)
        result = await classify_update_or_new("Broken window in lobby", open_tickets)
    assert result == {"routing": "new"}


async def test_classify_update_or_new_llm_says_update():
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {"response": '{"routing": "update", "ticket_id": 1}'}
    open_tickets = [{"id": 1, "category": "plumbing", "message_body": "Pump leaking floor 3"}]
    with patch("classifier.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.post = AsyncMock(return_value=mock_resp)
        result = await classify_update_or_new("Still leaking, getting worse", open_tickets)
    assert result == {"routing": "update", "ticket_id": 1}


async def test_classify_update_or_new_rejects_invalid_ticket_id():
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    # LLM returns a ticket_id not in our open list — fall back to new
    mock_resp.json.return_value = {"response": '{"routing": "update", "ticket_id": 999}'}
    open_tickets = [{"id": 1, "category": "plumbing", "message_body": "Pump leaking floor 3"}]
    with patch("classifier.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.post = AsyncMock(return_value=mock_resp)
        result = await classify_update_or_new("Still leaking, getting worse", open_tickets)
    assert result == {"routing": "new"}


async def test_classify_update_or_new_falls_back_on_llm_failure():
    open_tickets = [{"id": 1, "category": "plumbing", "message_body": "Pump leaking floor 3"}]
    with patch("classifier.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.post = AsyncMock(
            side_effect=Exception("timeout")
        )
        result = await classify_update_or_new("Still leaking, getting worse", open_tickets)
    assert result == {"routing": "new"}
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd backend && python -m pytest tests/test_classifier.py -k "classify_update_or_new" -v
```
Expected: `ImportError` or `AttributeError` — `classify_update_or_new` not yet defined.

- [ ] **Step 3: Add `classify_update_or_new` to `backend/classifier.py`**

Append after the existing `classify_message` function:

```python
def _build_routing_prompt(message: str, open_tickets: list[dict]) -> str:
    ticket_lines = "\n".join(
        f"- ticket_id={t['id']}: [{t['category']}] {t['message_body'][:200]}"
        for t in open_tickets
    )
    safe_message = json.dumps(message)
    return (
        "You are deciding whether a WhatsApp message is an update to an existing open ticket "
        "or a brand new issue.\n\n"
        "Open tickets in this group:\n"
        f"{ticket_lines}\n\n"
        "Return ONLY valid JSON, no explanation, no markdown:\n"
        '{"routing": "new"}\n'
        "or\n"
        '{"routing": "update", "ticket_id": <integer id from the list above>}\n\n'
        f"New message: {safe_message}"
    )


async def classify_update_or_new(message: str, open_tickets: list[dict]) -> dict:
    if not open_tickets:
        return {"routing": "new"}
    prompt = _build_routing_prompt(message, open_tickets)
    try:
        async with httpx.AsyncClient(timeout=OLLAMA_TIMEOUT) as client:
            response = await client.post(
                f"{OLLAMA_HOST}/api/generate",
                json={"model": OLLAMA_MODEL, "prompt": prompt, "stream": False},
            )
            response.raise_for_status()
            raw = response.json().get("response", "")
            parsed = json.loads(raw)
            routing = str(parsed.get("routing", "new")).lower()
            if routing == "update":
                ticket_id = int(parsed["ticket_id"])
                valid_ids = {t["id"] for t in open_tickets}
                if ticket_id in valid_ids:
                    return {"routing": "update", "ticket_id": ticket_id}
            return {"routing": "new"}
    except Exception as exc:
        logger.error("classify_update_or_new failed: %s", exc)
        return {"routing": "new"}
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd backend && python -m pytest tests/test_classifier.py -v
```
Expected: all 9 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/classifier.py backend/tests/test_classifier.py
git commit -m "feat: add classify_update_or_new for Stage 2 routing"
```

---

## Task 3: `download_media` Helper

**Files:**
- Create: `backend/media.py`
- Create: `backend/tests/test_media.py`

- [ ] **Step 1: Write failing tests — create `backend/tests/test_media.py`**

```python
import os
import tempfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from media import download_media


async def test_download_media_saves_file_and_returns_metadata():
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.headers = {"content-type": "image/jpeg"}
    mock_resp.content = b"\xff\xd8\xff"  # minimal JPEG header bytes

    with tempfile.TemporaryDirectory() as tmpdir:
        with patch("media.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(return_value=mock_resp)
            filename, mimetype, file_path = await download_media("http://fake/media/abc", tmpdir)

        assert mimetype == "image/jpeg"
        assert filename.endswith(".jpg")
        assert file_path == os.path.join(tmpdir, filename)
        assert os.path.exists(file_path)
        with open(file_path, "rb") as f:
            assert f.read() == b"\xff\xd8\xff"


async def test_download_media_handles_video():
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.headers = {"content-type": "video/mp4"}
    mock_resp.content = b"fakevideodata"

    with tempfile.TemporaryDirectory() as tmpdir:
        with patch("media.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(return_value=mock_resp)
            filename, mimetype, file_path = await download_media("http://fake/media/vid", tmpdir)

        assert mimetype == "video/mp4"
        assert filename.endswith(".mp4")


async def test_download_media_raises_on_http_error():
    with tempfile.TemporaryDirectory() as tmpdir:
        with patch("media.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(
                side_effect=Exception("connection refused")
            )
            with pytest.raises(Exception, match="connection refused"):
                await download_media("http://fake/media/bad", tmpdir)


async def test_download_media_creates_dest_dir_if_missing():
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.headers = {"content-type": "image/png"}
    mock_resp.content = b"pngdata"

    with tempfile.TemporaryDirectory() as tmpdir:
        new_dir = os.path.join(tmpdir, "subdir", "media")
        with patch("media.httpx.AsyncClient") as mock_client:
            mock_client.return_value.__aenter__.return_value.get = AsyncMock(return_value=mock_resp)
            filename, mimetype, file_path = await download_media("http://fake/media/img", new_dir)

        assert os.path.exists(new_dir)
        assert os.path.exists(file_path)
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd backend && python -m pytest tests/test_media.py -v
```
Expected: `ModuleNotFoundError: No module named 'media'`.

- [ ] **Step 3: Create `backend/media.py`**

```python
import asyncio
import logging
import mimetypes
import os
import uuid
from pathlib import Path

import httpx

logger = logging.getLogger(__name__)

MEDIA_DIR = os.getenv("MEDIA_DIR", "/app/media")

_MIMETYPE_EXT_FIXES = {
    "image/jpeg": ".jpg",
    "image/tiff": ".tiff",
}


async def download_media(url: str, dest_dir: str = MEDIA_DIR) -> tuple[str, str, str]:
    """Download media from url, save to dest_dir. Returns (filename, mimetype, file_path)."""
    Path(dest_dir).mkdir(parents=True, exist_ok=True)

    async with httpx.AsyncClient(timeout=30.0) as client:
        response = await client.get(url)
        response.raise_for_status()

        content_type = response.headers.get("content-type", "application/octet-stream")
        mimetype = content_type.split(";")[0].strip()
        ext = _MIMETYPE_EXT_FIXES.get(mimetype) or mimetypes.guess_extension(mimetype) or ".bin"

        filename = f"{uuid.uuid4().hex}{ext}"
        file_path = os.path.join(dest_dir, filename)
        data = response.content

    await asyncio.to_thread(_write_file, file_path, data)
    return filename, mimetype, file_path


def _write_file(path: str, data: bytes) -> None:
    with open(path, "wb") as f:
        f.write(data)
```

- [ ] **Step 4: Run tests to verify they pass**

```bash
cd backend && python -m pytest tests/test_media.py -v
```
Expected: all 4 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/media.py backend/tests/test_media.py
git commit -m "feat: add download_media helper for WhatsApp media attachments"
```

---

## Task 4: Stage 2 Routing for Text Messages in Ingest

**Files:**
- Modify: `backend/main.py`
- Create: `backend/tests/test_updates.py`

- [ ] **Step 1: Write failing tests — create `backend/tests/test_updates.py`**

```python
from unittest.mock import AsyncMock, patch

_GROUP_PAYLOAD = {
    "event": "message.received",
    "data": {
        "id": "msg-original",
        "type": "chat",
        "isGroup": True,
        "chatId": "120363@g.us",
        "chat": {"name": "Oakridge Block A"},
        "author": "254711111111@c.us",
        "notifyName": "Caretaker A",
        "body": "The water pump on floor 3 is leaking",
        "timestamp": 1782293340,
    },
}

_FOLLOWUP_PAYLOAD = {
    "event": "message.received",
    "data": {
        "id": "msg-followup",
        "type": "chat",
        "isGroup": True,
        "chatId": "120363@g.us",
        "chat": {"name": "Oakridge Block A"},
        "author": "254711111111@c.us",
        "notifyName": "Caretaker A",
        "body": "Still leaking badly, now flooding",
        "timestamp": 1782293400,
    },
}

_INCIDENT_CLASS = {"is_incident": True, "category": "plumbing", "severity": "high", "confidence": 0.92}
_NOISE_CLASS = {"is_incident": False, "category": "other", "severity": "low", "confidence": 0.95}


async def test_followup_creates_update_when_llm_says_update(client):
    with patch("main.classify_message", new=AsyncMock(return_value=_INCIDENT_CLASS)):
        with patch("main.push_incident", new=AsyncMock()):
            r1 = await client.post("/api/v1/ops/ingest", json=_GROUP_PAYLOAD, headers={"X-API-Key": "test-secret"})
    assert r1.json()["status"] == "staged"
    incident_id = (await client.get("/incidents")).json()[0]["id"]

    routing = {"routing": "update", "ticket_id": incident_id}
    with patch("main.classify_message", new=AsyncMock(return_value=_INCIDENT_CLASS)):
        with patch("main.classify_update_or_new", new=AsyncMock(return_value=routing)):
            r2 = await client.post("/api/v1/ops/ingest", json=_FOLLOWUP_PAYLOAD, headers={"X-API-Key": "test-secret"})
    assert r2.json()["status"] == "staged_update"
    assert r2.json()["incident_id"] == incident_id


async def test_followup_creates_new_incident_when_llm_says_new(client):
    with patch("main.classify_message", new=AsyncMock(return_value=_INCIDENT_CLASS)):
        with patch("main.push_incident", new=AsyncMock()):
            await client.post("/api/v1/ops/ingest", json=_GROUP_PAYLOAD, headers={"X-API-Key": "test-secret"})

    with patch("main.classify_message", new=AsyncMock(return_value=_INCIDENT_CLASS)):
        with patch("main.classify_update_or_new", new=AsyncMock(return_value={"routing": "new"})):
            with patch("main.push_incident", new=AsyncMock()):
                r2 = await client.post("/api/v1/ops/ingest", json=_FOLLOWUP_PAYLOAD, headers={"X-API-Key": "test-secret"})
    assert r2.json()["status"] == "staged"

    incidents = (await client.get("/incidents")).json()
    assert len(incidents) == 2


async def test_no_open_tickets_skips_stage2(client):
    """When no open tickets exist, Stage 2 is not called."""
    with patch("main.classify_message", new=AsyncMock(return_value=_INCIDENT_CLASS)):
        with patch("main.classify_update_or_new", new=AsyncMock()) as mock_stage2:
            with patch("main.push_incident", new=AsyncMock()):
                r = await client.post("/api/v1/ops/ingest", json=_GROUP_PAYLOAD, headers={"X-API-Key": "test-secret"})
    assert r.json()["status"] == "staged"
    mock_stage2.assert_not_called()


async def test_update_deduplication(client):
    with patch("main.classify_message", new=AsyncMock(return_value=_INCIDENT_CLASS)):
        with patch("main.push_incident", new=AsyncMock()):
            await client.post("/api/v1/ops/ingest", json=_GROUP_PAYLOAD, headers={"X-API-Key": "test-secret"})
    incident_id = (await client.get("/incidents")).json()[0]["id"]

    routing = {"routing": "update", "ticket_id": incident_id}
    with patch("main.classify_message", new=AsyncMock(return_value=_INCIDENT_CLASS)):
        with patch("main.classify_update_or_new", new=AsyncMock(return_value=routing)):
            r1 = await client.post("/api/v1/ops/ingest", json=_FOLLOWUP_PAYLOAD, headers={"X-API-Key": "test-secret"})
            r2 = await client.post("/api/v1/ops/ingest", json=_FOLLOWUP_PAYLOAD, headers={"X-API-Key": "test-secret"})
    assert r1.json()["status"] == "staged_update"
    assert r2.json()["status"] == "duplicate"
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd backend && python -m pytest tests/test_updates.py -v
```
Expected: tests fail because `staged_update` status doesn't exist yet.

- [ ] **Step 3: Refactor `backend/main.py` — add imports, helpers, and refactored ingest**

Replace the top imports block and add the new helpers. The full updated `main.py`:

```python
import hmac
import logging
import os
import socket
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Optional

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
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
from models import Incident, IncidentMedia, IncidentUpdate
from odoo_stub import push_incident

_VALID_STATUSES = {"new", "review", "acknowledged", "resolved", "ignored"}
_MEDIA_TYPES = {"image", "video", "document", "audio"}


class StatusUpdate(BaseModel):
    status: str


class RelinkBody(BaseModel):
    incident_id: Optional[int]


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
    routing = await classify_update_or_new(message_body, open_tickets)

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

    # Media message — handled in Task 5
    return {"status": "ignored", "message": "Media handling not yet implemented"}


@app.get("/incidents")
async def list_incidents(
    since_id: Optional[int] = None,
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
        .order_by(Incident.received_at.desc())
    )
    rows = result.all()
    incidents_with_counts = [
        {"incident": i, "update_count": uc, "media_count": mc}
        for i, uc, mc in rows
    ]
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "incidents_with_counts": incidents_with_counts,
            "title": os.getenv("DASHBOARD_TITLE", "Ops Incident Monitor"),
            "api_key": GATEWAY_SECRET_TOKEN,
        },
    )
```

- [ ] **Step 4: Run tests to verify update routing tests pass**

```bash
cd backend && python -m pytest tests/test_updates.py tests/test_ingest.py -v
```
Expected: all update routing tests PASS. Note: existing test_ingest tests still pass because `_handle_text_ingest` returns the same `staged` shape.

- [ ] **Step 5: Commit**

```bash
git add backend/main.py
git commit -m "feat: add Stage 2 update routing for text message ingest"
```

---

## Task 5: Media Message Ingest

**Files:**
- Modify: `backend/main.py` — replace the `# Media message` stub
- Modify: `backend/tests/test_updates.py` — add media tests

- [ ] **Step 1: Add media ingest tests to `backend/tests/test_updates.py`**

Append to the file:

```python
_IMAGE_PAYLOAD = {
    "event": "message.received",
    "data": {
        "id": "msg-image-1",
        "type": "image",
        "isGroup": True,
        "chatId": "120363@g.us",
        "chat": {"name": "Oakridge Block A"},
        "author": "254711111111@c.us",
        "notifyName": "Caretaker A",
        "caption": "Burst pipe flooding the corridor",
        "mediaUrl": "http://openwa.local/media/abc.jpg",
        "timestamp": 1782293500,
    },
}

_IMAGE_NO_CAPTION = {
    "event": "message.received",
    "data": {
        "id": "msg-image-2",
        "type": "image",
        "isGroup": True,
        "chatId": "120363@g.us",
        "chat": {"name": "Oakridge Block A"},
        "author": "254711111111@c.us",
        "mediaUrl": "http://openwa.local/media/def.jpg",
        "timestamp": 1782293600,
    },
}


async def test_media_with_caption_creates_incident_and_media_row(client):
    fake_media = ("abc123.jpg", "image/jpeg", "/app/media/abc123.jpg")
    with patch("main.classify_message", new=AsyncMock(return_value=_INCIDENT_CLASS)):
        with patch("main.classify_update_or_new", new=AsyncMock(return_value={"routing": "new"})):
            with patch("main.push_incident", new=AsyncMock()):
                with patch("main.download_media", new=AsyncMock(return_value=fake_media)):
                    r = await client.post(
                        "/api/v1/ops/ingest", json=_IMAGE_PAYLOAD, headers={"X-API-Key": "test-secret"}
                    )
    assert r.json()["status"] == "staged_media"
    assert "incident_id" in r.json()


async def test_media_no_caption_attaches_to_open_ticket(client):
    # Create an open ticket first
    with patch("main.classify_message", new=AsyncMock(return_value=_INCIDENT_CLASS)):
        with patch("main.push_incident", new=AsyncMock()):
            await client.post("/api/v1/ops/ingest", json=_GROUP_PAYLOAD, headers={"X-API-Key": "test-secret"})
    incident_id = (await client.get("/incidents")).json()[0]["id"]

    fake_media = ("def456.jpg", "image/jpeg", "/app/media/def456.jpg")
    with patch("main.download_media", new=AsyncMock(return_value=fake_media)):
        r = await client.post(
            "/api/v1/ops/ingest", json=_IMAGE_NO_CAPTION, headers={"X-API-Key": "test-secret"}
        )
    assert r.json()["status"] == "staged_media"
    assert r.json()["incident_id"] == incident_id


async def test_media_no_caption_no_open_ticket_returns_staged_media(client):
    r = await client.post(
        "/api/v1/ops/ingest", json=_IMAGE_NO_CAPTION, headers={"X-API-Key": "test-secret"}
    )
    assert r.json()["status"] == "staged_media"
    assert "incident_id" not in r.json()


async def test_media_download_failure_still_creates_incident_from_caption(client):
    with patch("main.classify_message", new=AsyncMock(return_value=_INCIDENT_CLASS)):
        with patch("main.classify_update_or_new", new=AsyncMock(return_value={"routing": "new"})):
            with patch("main.push_incident", new=AsyncMock()):
                with patch("main.download_media", new=AsyncMock(side_effect=Exception("network error"))):
                    r = await client.post(
                        "/api/v1/ops/ingest", json=_IMAGE_PAYLOAD, headers={"X-API-Key": "test-secret"}
                    )
    assert r.json()["status"] == "staged_media"
    incidents = (await client.get("/incidents")).json()
    assert len(incidents) == 1
```

- [ ] **Step 2: Run tests to verify new media tests fail**

```bash
cd backend && python -m pytest tests/test_updates.py::test_media_with_caption_creates_incident_and_media_row -v
```
Expected: FAIL — returns `ignored` not `staged_media`.

- [ ] **Step 3: Replace the media stub in `_handle_text_ingest` with `_handle_media_ingest` and wire it up**

Add this function to `backend/main.py` (after `_handle_text_ingest`, before the `ingest` route):

```python
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

    if caption:
        classification = await classify_message(caption)
        if classification["is_incident"] and classification["confidence"] >= MIN_CONFIDENCE:
            open_tickets = await _get_open_tickets(db, group_id)
            routing = await classify_update_or_new(caption, open_tickets)

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

    if incident_id is None:
        logger.warning("Orphaned media: no open ticket in group %s and no classified caption", group_id)
        return {"status": "staged_media", "message": "Media saved but no open ticket found"}

    return {"status": "staged_media", "incident_id": incident_id}
```

Then replace the stub at the bottom of the `ingest` handler:

```python
    # Media message
    caption = (data.get("caption") or "").strip()[:4000]
    media_url: Optional[str] = data.get("mediaUrl") or None
    return await _handle_media_ingest(
        db, group_id, group_name, reporter_name, reporter_phone,
        caption, media_url, received_at, message_id,
    )
```

- [ ] **Step 4: Run all tests**

```bash
cd backend && python -m pytest tests/ -v
```
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/main.py backend/tests/test_updates.py
git commit -m "feat: handle media messages in ingest pipeline with IncidentMedia attachment"
```

---

## Task 6: `GET /incidents/{id}` Detail Endpoint + New API Tests

**Files:**
- Modify: `backend/main.py`
- Create: `backend/tests/test_api.py`

- [ ] **Step 1: Write failing tests — create `backend/tests/test_api.py`**

```python
from unittest.mock import AsyncMock, patch

_INCIDENT_CLASS = {"is_incident": True, "category": "plumbing", "severity": "high", "confidence": 0.92}

_ORIGINAL = {
    "event": "message.received",
    "data": {
        "id": "msg-a",
        "type": "chat",
        "isGroup": True,
        "chatId": "123@g.us",
        "chat": {"name": "Block A"},
        "author": "2541@c.us",
        "notifyName": "Alice",
        "body": "Pump leaking",
        "timestamp": 1782293340,
    },
}

_FOLLOWUP = {
    "event": "message.received",
    "data": {
        "id": "msg-b",
        "type": "chat",
        "isGroup": True,
        "chatId": "123@g.us",
        "chat": {"name": "Block A"},
        "author": "2541@c.us",
        "notifyName": "Alice",
        "body": "Still leaking",
        "timestamp": 1782293400,
    },
}


async def test_list_incidents_includes_counts(client):
    with patch("main.classify_message", new=AsyncMock(return_value=_INCIDENT_CLASS)):
        with patch("main.push_incident", new=AsyncMock()):
            await client.post("/api/v1/ops/ingest", json=_ORIGINAL, headers={"X-API-Key": "test-secret"})
    incidents = (await client.get("/incidents")).json()
    assert "update_count" in incidents[0]
    assert "media_count" in incidents[0]
    assert incidents[0]["update_count"] == 0
    assert incidents[0]["media_count"] == 0


async def test_list_incidents_update_count_increments(client):
    with patch("main.classify_message", new=AsyncMock(return_value=_INCIDENT_CLASS)):
        with patch("main.push_incident", new=AsyncMock()):
            await client.post("/api/v1/ops/ingest", json=_ORIGINAL, headers={"X-API-Key": "test-secret"})
    incident_id = (await client.get("/incidents")).json()[0]["id"]

    routing = {"routing": "update", "ticket_id": incident_id}
    with patch("main.classify_message", new=AsyncMock(return_value=_INCIDENT_CLASS)):
        with patch("main.classify_update_or_new", new=AsyncMock(return_value=routing)):
            await client.post("/api/v1/ops/ingest", json=_FOLLOWUP, headers={"X-API-Key": "test-secret"})

    incidents = (await client.get("/incidents")).json()
    assert incidents[0]["update_count"] == 1


async def test_get_incident_detail_returns_updates(client):
    with patch("main.classify_message", new=AsyncMock(return_value=_INCIDENT_CLASS)):
        with patch("main.push_incident", new=AsyncMock()):
            await client.post("/api/v1/ops/ingest", json=_ORIGINAL, headers={"X-API-Key": "test-secret"})
    incident_id = (await client.get("/incidents")).json()[0]["id"]

    routing = {"routing": "update", "ticket_id": incident_id}
    with patch("main.classify_message", new=AsyncMock(return_value=_INCIDENT_CLASS)):
        with patch("main.classify_update_or_new", new=AsyncMock(return_value=routing)):
            await client.post("/api/v1/ops/ingest", json=_FOLLOWUP, headers={"X-API-Key": "test-secret"})

    detail = (await client.get(f"/incidents/{incident_id}")).json()
    assert detail["id"] == incident_id
    assert len(detail["updates"]) == 1
    assert detail["updates"][0]["message_body"] == "Still leaking"
    assert detail["updates"][0]["ai_linked"] is True
    assert detail["updates"][0]["media_count"] == 0
    assert detail["media"] == []


async def test_get_incident_detail_404(client):
    r = await client.get("/incidents/9999")
    assert r.status_code == 404
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd backend && python -m pytest tests/test_api.py -v
```
Expected: `test_get_incident_detail_*` fail with 404/422 (route not yet defined), count tests fail.

- [ ] **Step 3: Add `GET /incidents/{incident_id}` to `backend/main.py`**

Add after the `list_incidents` endpoint:

```python
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
```

- [ ] **Step 4: Run tests**

```bash
cd backend && python -m pytest tests/test_api.py tests/test_ingest.py -v
```
Expected: all PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/main.py backend/tests/test_api.py
git commit -m "feat: add GET /incidents/{id} detail endpoint with updates and media"
```

---

## Task 7: `GET /media/{id}` File Serving + `PATCH /incidents/{update_id}/relink`

**Files:**
- Modify: `backend/main.py`
- Modify: `backend/tests/test_api.py`

- [ ] **Step 1: Add tests to `backend/tests/test_api.py`**

Append:

```python
import tempfile
import os
from sqlalchemy import insert
from models import IncidentMedia
from database import get_db
from main import app


async def test_serve_media_returns_file(client, db_session):
    # Create incident
    with patch("main.classify_message", new=AsyncMock(return_value=_INCIDENT_CLASS)):
        with patch("main.push_incident", new=AsyncMock()):
            await client.post("/api/v1/ops/ingest", json=_ORIGINAL, headers={"X-API-Key": "test-secret"})
    incident_id = (await client.get("/incidents")).json()[0]["id"]

    # Write a real temp file
    with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
        f.write(b"\xff\xd8\xff")
        tmp_path = f.name

    try:
        media = IncidentMedia(
            incident_id=incident_id,
            update_id=None,
            filename=os.path.basename(tmp_path),
            mimetype="image/jpeg",
            file_path=tmp_path,
            received_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc),
        )
        db_session.add(media)
        await db_session.commit()
        await db_session.refresh(media)

        r = await client.get(f"/media/{media.id}")
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("image/jpeg")
        assert r.content == b"\xff\xd8\xff"
    finally:
        os.unlink(tmp_path)


async def test_serve_media_404_for_missing_record(client):
    r = await client.get("/media/9999")
    assert r.status_code == 404


async def test_relink_update_to_different_incident(client):
    # Create two incidents in the same group
    payload_b = {**_ORIGINAL, "data": {**_ORIGINAL["data"], "id": "msg-c", "chatId": "999@g.us", "chat": {"name": "Block B"}}}
    with patch("main.classify_message", new=AsyncMock(return_value=_INCIDENT_CLASS)):
        with patch("main.push_incident", new=AsyncMock()):
            await client.post("/api/v1/ops/ingest", json=_ORIGINAL, headers={"X-API-Key": "test-secret"})
            await client.post("/api/v1/ops/ingest", json=payload_b, headers={"X-API-Key": "test-secret"})

    incidents = (await client.get("/incidents")).json()
    inc_a_id = next(i["id"] for i in incidents if "Block A" in i["property_name"])
    inc_b_id = next(i["id"] for i in incidents if "Block B" in i["property_name"])

    # Create an update attached to inc_a
    routing = {"routing": "update", "ticket_id": inc_a_id}
    with patch("main.classify_message", new=AsyncMock(return_value=_INCIDENT_CLASS)):
        with patch("main.classify_update_or_new", new=AsyncMock(return_value=routing)):
            await client.post("/api/v1/ops/ingest", json=_FOLLOWUP, headers={"X-API-Key": "test-secret"})

    detail_a = (await client.get(f"/incidents/{inc_a_id}")).json()
    update_id = detail_a["updates"][0]["id"]

    # Relink it to inc_b
    r = await client.patch(
        f"/incidents/{update_id}/relink",
        json={"incident_id": inc_b_id},
        headers={"X-API-Key": "test-secret"},
    )
    assert r.status_code == 200
    assert r.json()["incident_id"] == inc_b_id

    # Check it moved
    detail_a2 = (await client.get(f"/incidents/{inc_a_id}")).json()
    detail_b2 = (await client.get(f"/incidents/{inc_b_id}")).json()
    assert len(detail_a2["updates"]) == 0
    assert len(detail_b2["updates"]) == 1


async def test_relink_requires_auth(client):
    r = await client.patch("/incidents/1/relink", json={"incident_id": 2}, headers={"X-API-Key": "wrong"})
    assert r.status_code == 401


async def test_relink_404_for_missing_update(client):
    r = await client.patch(
        "/incidents/9999/relink",
        json={"incident_id": 1},
        headers={"X-API-Key": "test-secret"},
    )
    assert r.status_code == 404
```

- [ ] **Step 2: Run to verify new tests fail**

```bash
cd backend && python -m pytest tests/test_api.py::test_serve_media_returns_file tests/test_api.py::test_relink_update_to_different_incident -v
```
Expected: 404 / route not found.

- [ ] **Step 3: Add the two new endpoints to `backend/main.py`**

Add after `get_incident_detail`:

```python
@app.get("/media/{media_id}")
async def serve_media(
    media_id: int,
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(IncidentMedia).where(IncidentMedia.id == media_id))
    media = result.scalar_one_or_none()
    if not media:
        raise HTTPException(status_code=404, detail="Media not found")
    if not os.path.exists(media.file_path):
        raise HTTPException(status_code=404, detail="Media file not found on disk")
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
    media_res = await db.execute(
        select(IncidentMedia).where(IncidentMedia.update_id == update_id)
    )
    for m in media_res.scalars().all():
        m.incident_id = body.incident_id
    target.updated_at = datetime.now(timezone.utc)
    await db.commit()
    return {"update_id": update_id, "incident_id": body.incident_id}
```

- [ ] **Step 4: Run full test suite**

```bash
cd backend && python -m pytest tests/ -v
```
Expected: all tests PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/main.py backend/tests/test_api.py
git commit -m "feat: add GET /media/{id} file serving and PATCH /incidents/{id}/relink"
```

---

## Task 8: Docker Volume for Media Files

**Files:**
- Modify: `docker-compose.yml`

- [ ] **Step 1: Add `media_data` volume to `docker-compose.yml`**

In the `backend` service block, add a `volumes` key (after `extra_hosts`):

```yaml
    volumes:
      - media_data:/app/media
```

In the top-level `volumes` block, add:

```yaml
  media_data:
```

The final `volumes` block should look like:

```yaml
volumes:
  postgres_data:
  openwa_data:
  media_data:
```

- [ ] **Step 2: Verify compose file parses**

```bash
docker compose config --quiet
```
Expected: no errors.

- [ ] **Step 3: Commit**

```bash
git add docker-compose.yml
git commit -m "infra: add media_data volume for WhatsApp media file storage"
```

---

## Task 9: Dashboard UI — Badges, Detail Modal, Re-link

**Files:**
- Modify: `backend/templates/dashboard.html`

- [ ] **Step 1: Update the Jinja2 card template to use `incidents_with_counts`**

The dashboard route now passes `incidents_with_counts` (a list of `{incident, update_count, media_count}` dicts). Update the template's `{% for %}` loop and card `data-*` attributes. Find:

```jinja2
          {% if incidents %}
            {% for i in incidents %}
            <article class="card"
                 data-id="{{ i.id }}"
```

Replace the entire `{% if incidents %}...{% endif %}` block with:

```jinja2
          {% if incidents_with_counts %}
            {% for row in incidents_with_counts %}
            {% set i = row.incident %}
            {% set update_count = row.update_count %}
            {% set media_count = row.media_count %}
            <article class="card"
                 data-id="{{ i.id }}"
                 data-sev="{{ i.severity }}"
                 data-status="{{ i.status }}"
                 data-cat="{{ i.category }}"
                 data-updates="{{ update_count }}"
                 data-media="{{ media_count }}">
              <div class="card-head" onclick="toggleCard(this.closest('.card'))">
                <div class="category-icon" data-cat="{{ i.category }}">📋</div>
                <div class="info">
                  <div class="title-row">
                    <div class="title">{{ i.property_name }} — {{ i.category | capitalize }}</div>
                  </div>
                  <div class="meta">
                    <span>{{ i.reporter_name or "Unknown reporter" }}</span>
                    {% if i.reporter_phone %}<span class="meta-dot"></span><span>+{{ i.reporter_phone }}</span>{% endif %}
                    <span class="meta-dot"></span><span>{{ i.received_at.strftime("%H:%M") }}</span>
                  </div>
                  <p class="message-preview">{{ i.message_body }}</p>
                  {% if update_count > 0 or media_count > 0 %}
                  <div class="card-badges">
                    {% if update_count > 0 %}
                    <button class="card-badge-btn" onclick="event.stopPropagation(); openDetailModal({{ i.id }})">↩ {{ update_count }} update{{ 's' if update_count != 1 else '' }}</button>
                    {% endif %}
                    {% if media_count > 0 %}
                    <button class="card-badge-btn" onclick="event.stopPropagation(); openDetailModal({{ i.id }})">📎 {{ media_count }} attachment{{ 's' if media_count != 1 else '' }}</button>
                    {% endif %}
                  </div>
                  {% endif %}
                </div>
                <div class="card-right">
                  <span class="badge badge-{{ i.severity }}">{{ i.severity }}</span>
                  <span class="badge badge-{{ i.status }}" id="status-badge-{{ i.id }}">{{ i.status }}</span>
                  <span class="chevron">⌄</span>
                </div>
              </div>
              <div class="card-body">
                <div class="details-panel">
                  <div>
                    <div class="message-box">
                      <div class="group-label">Reported message</div>
                      <div class="message">{{ i.message_body }}</div>
                    </div>
                    <div class="actions" id="actions-{{ i.id }}">
                      <button class="act-btn btn-ack" onclick="setStatus({{ i.id }}, 'acknowledged', event)">✓ Acknowledge</button>
                      <button class="act-btn btn-resolve" onclick="setStatus({{ i.id }}, 'resolved', event)">✓ Resolve</button>
                      <button class="act-btn btn-ignore" onclick="setStatus({{ i.id }}, 'ignored', event)">✗ Ignore</button>
                      <button class="act-btn btn-review" onclick="setStatus({{ i.id }}, 'review', event)">⟳ Send to Review</button>
                    </div>
                  </div>
                  <div class="side-details">
                    <div class="detail-card">
                      <div class="label">Property group</div>
                      <div class="value">{{ i.property_name }}</div>
                    </div>
                    <div class="detail-card">
                      <div class="label">Reporter</div>
                      <div class="value">{{ i.reporter_name or "Unknown" }}{% if i.reporter_phone %}<br>+{{ i.reporter_phone }}{% endif %}</div>
                    </div>
                    <div class="detail-card">
                      <div class="label">Incident ID</div>
                      <div class="value">#{{ i.id }}</div>
                    </div>
                  </div>
                </div>
              </div>
            </article>
            {% endfor %}
          {% else %}
            <div class="empty" id="empty-state"><div><strong>No incidents yet</strong>Waiting for property group messages.</div></div>
          {% endif %}
```

- [ ] **Step 2: Add CSS for `.card-badges`, `.card-badge-btn`, and the modal**

Add inside the `<style>` block, before the closing `</style>`:

```css
    .card-badges {
      display: flex;
      flex-wrap: wrap;
      gap: 6px;
      margin-top: 8px;
    }

    .card-badge-btn {
      display: inline-flex;
      align-items: center;
      gap: 4px;
      padding: 3px 10px;
      border-radius: 999px;
      border: 1px solid rgba(56, 189, 248, 0.3);
      background: rgba(56, 189, 248, 0.08);
      color: #7dd3fc;
      font-size: 11px;
      font-weight: 800;
      cursor: pointer;
      transition: background 0.15s, border-color 0.15s;
    }

    .card-badge-btn:hover {
      background: rgba(56, 189, 248, 0.16);
      border-color: rgba(56, 189, 248, 0.55);
    }

    /* Detail modal */
    .modal-overlay {
      display: none;
      position: fixed;
      inset: 0;
      z-index: 50;
      background: rgba(0,0,0,0.6);
      backdrop-filter: blur(4px);
      align-items: center;
      justify-content: center;
      padding: 20px;
    }

    .modal-overlay.open { display: flex; }

    .modal {
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: var(--radius-xl);
      width: 100%;
      max-width: 680px;
      max-height: 85vh;
      display: flex;
      flex-direction: column;
      box-shadow: var(--shadow);
    }

    .modal-header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      padding: 18px 22px 14px;
      border-bottom: 1px solid var(--line);
      flex-shrink: 0;
    }

    .modal-header h2 { font-size: 16px; font-weight: 900; }

    .modal-close {
      width: 32px;
      height: 32px;
      border-radius: 50%;
      border: 1px solid var(--line);
      background: transparent;
      color: var(--muted);
      cursor: pointer;
      font-size: 16px;
      display: grid;
      place-items: center;
    }

    .modal-close:hover { background: rgba(255,255,255,0.06); color: white; }

    .modal-body {
      overflow-y: auto;
      padding: 18px 22px;
      display: flex;
      flex-direction: column;
      gap: 20px;
    }

    .modal-section-label {
      font-size: 11px;
      font-weight: 900;
      letter-spacing: 0.1em;
      text-transform: uppercase;
      color: var(--muted);
      margin-bottom: 8px;
    }

    .update-thread { display: flex; flex-direction: column; gap: 10px; }

    .update-row {
      background: var(--surface-2);
      border: 1px solid var(--line);
      border-radius: var(--radius-md);
      padding: 12px 14px;
    }

    .update-meta {
      font-size: 12px;
      color: var(--muted);
      margin-bottom: 6px;
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 8px;
    }

    .update-body { font-size: 13px; line-height: 1.55; color: var(--text); }

    .relink-wrap { margin-top: 8px; display: flex; align-items: center; gap: 8px; }

    .relink-select {
      flex: 1;
      padding: 5px 8px;
      border-radius: 8px;
      border: 1px solid var(--line);
      background: var(--surface-3);
      color: var(--text);
      font-size: 12px;
    }

    .relink-btn {
      padding: 5px 12px;
      border-radius: 8px;
      border: 1px solid rgba(56, 189, 248, 0.3);
      background: rgba(56, 189, 248, 0.08);
      color: #7dd3fc;
      font-size: 12px;
      font-weight: 800;
      cursor: pointer;
    }

    .relink-btn:hover { background: rgba(56, 189, 248, 0.18); }

    .media-grid {
      display: grid;
      grid-template-columns: repeat(auto-fill, minmax(110px, 1fr));
      gap: 8px;
    }

    .media-thumb {
      aspect-ratio: 1;
      border-radius: var(--radius-sm);
      overflow: hidden;
      border: 1px solid var(--line);
      background: var(--surface-2);
    }

    .media-thumb img {
      width: 100%;
      height: 100%;
      object-fit: cover;
      display: block;
    }

    .media-file-row {
      display: flex;
      align-items: center;
      gap: 8px;
      padding: 8px 10px;
      background: var(--surface-2);
      border: 1px solid var(--line);
      border-radius: var(--radius-sm);
      font-size: 12px;
      color: var(--muted);
      text-decoration: none;
    }

    .media-file-row:hover { color: var(--text); border-color: rgba(56,189,248,0.3); }
```

- [ ] **Step 3: Add the modal HTML and JavaScript**

After the `<div class="toast" id="toast"></div>` line, add:

```html
  <!-- Detail modal -->
  <div class="modal-overlay" id="detail-modal-overlay" onclick="if(event.target===this)closeDetailModal()">
    <div class="modal">
      <div class="modal-header">
        <h2 id="modal-title">Incident Detail</h2>
        <button class="modal-close" onclick="closeDetailModal()">✕</button>
      </div>
      <div class="modal-body" id="modal-body">
        <div style="color:var(--muted);text-align:center;padding:20px">Loading…</div>
      </div>
    </div>
  </div>
```

Then inside the `<script>` block, add these functions (before `init()`):

```javascript
let _openIncidents = [];  // used for relink dropdown

async function openDetailModal(incidentId) {
  document.getElementById('detail-modal-overlay').classList.add('open');
  document.getElementById('modal-body').innerHTML = '<div style="color:var(--muted);text-align:center;padding:20px">Loading…</div>';
  try {
    const detail = await fetch(`/incidents/${incidentId}`).then(r => r.json());
    const allInc = await fetch('/incidents').then(r => r.json());
    _openIncidents = allInc.filter(i => i.id !== incidentId && !['resolved','ignored'].includes(i.status));
    document.getElementById('modal-title').textContent = `#${detail.id} — ${esc(detail.property_name)}`;
    document.getElementById('modal-body').innerHTML = renderDetailModal(detail);
  } catch(e) {
    document.getElementById('modal-body').innerHTML = '<div style="color:var(--red)">Failed to load detail.</div>';
  }
}

function closeDetailModal() {
  document.getElementById('detail-modal-overlay').classList.remove('open');
}

function renderDetailModal(detail) {
  const updatesHtml = detail.updates.length === 0
    ? '<div style="color:var(--muted);font-size:13px">No updates yet.</div>'
    : detail.updates.map(u => {
        const relinkOpts = _openIncidents.map(i =>
          `<option value="${i.id}">#${i.id} — ${esc(i.property_name)}</option>`
        ).join('');
        const relinkHtml = API_KEY ? `
          <div class="relink-wrap">
            <select class="relink-select" id="relink-select-${u.id}">
              <option value="">Move to…</option>
              ${relinkOpts}
            </select>
            <button class="relink-btn" onclick="relinkUpdate(${u.id}, ${detail.id})">Re-link</button>
          </div>` : '';
        const mediaBadge = u.media_count > 0
          ? `<span style="color:var(--blue);font-size:11px">📎 ${u.media_count}</span>` : '';
        return `<div class="update-row">
          <div class="update-meta">
            <span>${esc(u.reporter_name || 'Unknown')} · ${formatTime(u.received_at)}${u.ai_linked ? ' · <em style="opacity:.6">AI-linked</em>' : ''}</span>
            ${mediaBadge}
          </div>
          <div class="update-body">${esc(u.message_body)}</div>
          ${relinkHtml}
        </div>`;
      }).join('');

  const mediaHtml = detail.media.length === 0
    ? '<div style="color:var(--muted);font-size:13px">No attachments.</div>'
    : `<div class="media-grid">${detail.media.map(m => {
        const url = `/media/${m.id}`;
        if (m.mimetype.startsWith('image/')) {
          return `<a href="${url}" target="_blank" class="media-thumb"><img src="${url}" alt="${esc(m.filename)}" loading="lazy"></a>`;
        }
        return `<a href="${url}" target="_blank" class="media-file-row" download="${esc(m.filename)}">📄 ${esc(m.filename)}</a>`;
      }).join('')}</div>`;

  return `
    <div>
      <div class="modal-section-label">Original report</div>
      <div class="message-box"><div class="message">${esc(detail.message_body)}</div></div>
    </div>
    <div>
      <div class="modal-section-label">Updates (${detail.updates.length})</div>
      <div class="update-thread">${updatesHtml}</div>
    </div>
    <div>
      <div class="modal-section-label">Attachments (${detail.media.length})</div>
      ${mediaHtml}
    </div>`;
}

async function relinkUpdate(updateId, currentIncidentId) {
  const sel = document.getElementById(`relink-select-${updateId}`);
  const targetId = parseInt(sel.value, 10);
  if (!targetId) return;
  try {
    await fetch(`/incidents/${updateId}/relink`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json', 'X-API-Key': API_KEY },
      body: JSON.stringify({ incident_id: targetId }),
    });
    showToast('Update re-linked');
    await openDetailModal(currentIncidentId);
  } catch(e) {
    showToast('Re-link failed');
  }
}
```

- [ ] **Step 4: Update `buildCard` in the JS to render badges for polled incidents**

In the `buildCard` function, find the `<div class="info">` section and add badge rendering. Find the line that ends `<p class="message-preview">${esc(i.message_body)}</p>` and add after it:

```javascript
      ${(i.update_count > 0 || i.media_count > 0) ? `
      <div class="card-badges">
        ${i.update_count > 0 ? `<button class="card-badge-btn" onclick="event.stopPropagation(); openDetailModal(${i.id})">↩ ${i.update_count} update${i.update_count !== 1 ? 's' : ''}</button>` : ''}
        ${i.media_count > 0 ? `<button class="card-badge-btn" onclick="event.stopPropagation(); openDetailModal(${i.id})">📎 ${i.media_count} attachment${i.media_count !== 1 ? 's' : ''}</button>` : ''}
      </div>` : ''}
```

Also update `normalizeIncident` to carry the new fields:

```javascript
function normalizeIncident(i) {
  return {
    id: i.id,
    property_name: i.property_name || 'Unknown property',
    severity: i.severity || 'low',
    status: i.status || 'new',
    category: i.category || 'other',
    message_body: i.message_body || '',
    reporter_name: i.reporter_name || 'Unknown reporter',
    reporter_phone: i.reporter_phone || '',
    received_at: i.received_at || new Date().toISOString(),
    update_count: i.update_count || 0,
    media_count: i.media_count || 0,
  };
}
```

- [ ] **Step 5: Run all tests**

```bash
cd backend && python -m pytest tests/ -v
```
Expected: all tests PASS (dashboard tests use the template; verify no Jinja2 errors).

- [ ] **Step 6: Commit**

```bash
git add backend/templates/dashboard.html
git commit -m "feat: add update/media badges, detail modal, and re-link control to dashboard"
```

---

## Known Limitation

The real-time poll (`GET /incidents?since_id=...`) only returns new incidents (higher IDs). If an update or media attachment arrives for an existing ticket, the badge counts on the already-rendered card will not update until the page is refreshed. This is a known trade-off of the current polling design.

---

## Spec Coverage Check

| Spec requirement | Task |
|---|---|
| `incident_updates` table | Task 1 |
| `incident_media` table | Task 1 |
| `incidents.updated_at` | Task 1 |
| `classify_update_or_new()` + LLM fallback to `"new"` | Task 2 |
| `download_media()` to `/app/media` | Task 3 |
| Text ingest Stage 2 routing | Task 4 |
| Media message ingest (caption + attachment) | Task 5 |
| Orphaned media (no caption, no open ticket) | Task 5 |
| Download failure → still create ticket from caption | Task 5 |
| `GET /incidents` with `update_count`/`media_count` | Task 4 |
| `GET /incidents/{id}` detail | Task 6 |
| `GET /media/{id}` file serving | Task 7 |
| `PATCH /incidents/{update_id}/relink` | Task 7 |
| `incident_id: null` promotes update to incident | Task 7 |
| Docker `media_data` volume | Task 8 |
| Dashboard badge row | Task 9 |
| Detail modal with update thread | Task 9 |
| Attachment thumbnails / file links in modal | Task 9 |
| Re-link dropdown in modal | Task 9 |
| `buildCard` JS updated for polled incidents | Task 9 |
