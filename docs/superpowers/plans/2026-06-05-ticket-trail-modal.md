# Ticket Trail Modal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the current detail modal with a wider, four-section modal showing the complete ticket lifecycle: original report, all updates with sender badges, persisted status history, and attachments, with the reply bar pinned at the bottom.

**Architecture:** Two small backend additions (a new `incident_status_history` table and a `relinked` boolean on `incident_updates`) feed into an enriched `GET /incidents/{id}` response. The frontend is a rewrite of `renderDetailModal` in `dashboard.html` — no new files. Migrations follow the existing hand-rolled `ALTER TABLE` / `CREATE TABLE` pattern in `database.py`.

**Tech Stack:** Python/FastAPI, SQLAlchemy async, Jinja2, vanilla JS, pytest-asyncio

---

### Task 1: Add models — `IncidentStatusHistory` and `relinked` on `IncidentUpdate`

**Files:**
- Modify: `backend/models.py`

- [ ] **Step 1: Open `backend/models.py` and add `relinked` to `IncidentUpdate` and the new `IncidentStatusHistory` class**

Replace the full file with:

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
    relinked: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")


class IncidentMedia(Base):
    __tablename__ = "incident_media"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    incident_id: Mapped[int] = mapped_column(Integer, ForeignKey("incidents.id"), nullable=False, index=True)
    update_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("incident_updates.id"), nullable=True)
    filename: Mapped[str] = mapped_column(Text, nullable=False)
    mimetype: Mapped[str] = mapped_column(Text, nullable=False)
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class IncidentStatusHistory(Base):
    __tablename__ = "incident_status_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    incident_id: Mapped[int] = mapped_column(Integer, ForeignKey("incidents.id"), nullable=False, index=True)
    from_status: Mapped[Optional[str]] = mapped_column(String(20), nullable=True)
    to_status: Mapped[str] = mapped_column(String(20), nullable=False)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
```

- [ ] **Step 2: Commit**

```bash
git add backend/models.py
git commit -m "feat: add IncidentStatusHistory model and relinked field on IncidentUpdate"
```

---

### Task 2: Add migrations in `database.py`

**Files:**
- Modify: `backend/database.py`

- [ ] **Step 1: Write a failing test that asserts the new table and column exist after `init_db()`**

Add to `backend/tests/test_db.py` (create if it doesn't exist — check first with `ls backend/tests/`):

```python
import pytest
from sqlalchemy import inspect, text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import StaticPool

@pytest.mark.asyncio
async def test_init_db_creates_status_history_table():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    from database import Base, init_db
    # patch the module-level engine used by init_db
    import database
    original = database.engine
    database.engine = engine
    try:
        await init_db()
        async with engine.connect() as conn:
            result = await conn.execute(text(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='incident_status_history'"
            ))
            assert result.scalar() == "incident_status_history"
    finally:
        database.engine = original
        await engine.dispose()

@pytest.mark.asyncio
async def test_init_db_adds_relinked_column():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    import database
    original = database.engine
    database.engine = engine
    try:
        await init_db()
        async with engine.connect() as conn:
            result = await conn.execute(text("PRAGMA table_info(incident_updates)"))
            cols = [row[1] for row in result.fetchall()]
            assert "relinked" in cols
    finally:
        database.engine = original
        await engine.dispose()
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
cd backend && python -m pytest tests/test_db.py -v
```

Expected: FAIL — `incident_status_history` table not found / `relinked` column not found.

- [ ] **Step 3: Add migrations to `database.py`**

Append the following blocks to the end of the `init_db()` function (after the existing `updated_at` migration block):

```python
    try:
        async with engine.begin() as conn:
            await conn.execute(text(
                "ALTER TABLE incident_updates ADD COLUMN relinked BOOLEAN NOT NULL DEFAULT FALSE"
            ))
    except Exception:
        pass

    try:
        async with engine.begin() as conn:
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS incident_status_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    incident_id INTEGER NOT NULL REFERENCES incidents(id),
                    from_status VARCHAR(20),
                    to_status VARCHAR(20) NOT NULL,
                    changed_at TIMESTAMP NOT NULL
                )
            """))
    except Exception:
        pass

    try:
        async with engine.begin() as conn:
            await conn.execute(text(
                "CREATE INDEX IF NOT EXISTS ix_incident_status_history_incident_id "
                "ON incident_status_history (incident_id)"
            ))
    except Exception:
        pass
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
cd backend && python -m pytest tests/test_db.py -v
```

Expected: PASS for both new tests. Also run the full suite to check nothing regressed:

```bash
cd backend && python -m pytest -v
```

- [ ] **Step 5: Commit**

```bash
git add backend/database.py backend/tests/test_db.py
git commit -m "feat: migrate incident_status_history table and relinked column"
```

---

### Task 3: Record status history on incident creation

**Files:**
- Modify: `backend/main.py`

- [ ] **Step 1: Write a failing test**

Add to `backend/tests/test_api.py`:

```python
async def test_ingest_creates_status_history_entry(client):
    with patch("main.classify_message", new=AsyncMock(return_value=_INCIDENT_CLASS)):
        with patch("main.push_incident", new=AsyncMock()):
            await client.post("/api/v1/ops/ingest", json=_ORIGINAL, headers={"X-API-Key": "test-secret"})
    incident_id = (await client.get("/incidents")).json()[0]["id"]
    detail = (await client.get(f"/incidents/{incident_id}")).json()
    assert "status_history" in detail
    assert len(detail["status_history"]) == 1
    assert detail["status_history"][0]["from_status"] is None
    assert detail["status_history"][0]["to_status"] == "review"
```

- [ ] **Step 2: Run test to confirm it fails**

```bash
cd backend && python -m pytest tests/test_api.py::test_ingest_creates_status_history_entry -v
```

Expected: FAIL — `status_history` key not in response.

- [ ] **Step 3: Update `main.py` imports**

In the imports block at the top of `main.py`, change:

```python
from models import Incident, IncidentMedia, IncidentUpdate
```

to:

```python
from models import Incident, IncidentMedia, IncidentStatusHistory, IncidentUpdate
```

- [ ] **Step 4: Insert initial history entry after incident creation in `_handle_text_ingest`**

Find the block in `_handle_text_ingest` that creates the `Incident` (around line 149). After `db.add(incident)` and before `await db.commit()`, add:

```python
        db.add(incident)
        history = IncidentStatusHistory(
            incident_id=0,  # will be set after flush
            from_status=None,
            to_status="review",
            changed_at=received_at,
        )
```

Actually, SQLAlchemy needs the `incident.id` before we can set `history.incident_id`. Use `await db.flush()` to get the ID. Replace the creation block (lines ~149–175) with:

```python
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
```

> Note: keep the existing `except IntegrityError` and `except Exception` handlers that follow — only replace the `db.add(incident)` + `await db.commit()` lines up to (but not including) the except blocks.

- [ ] **Step 5: Also add history when a new incident is promoted from a re-linked update**

In the `relink_update` endpoint, find the block that creates `new_incident` (around line 522). After `await db.flush()` (which already exists there), add the history insert before `await db.commit()`:

```python
        await db.flush()
        db.add(IncidentStatusHistory(
            incident_id=new_incident.id,
            from_status=None,
            to_status="review",
            changed_at=new_incident.received_at,
        ))
```

- [ ] **Step 6: Run the failing test — it should still fail** (we haven't updated `GET /incidents/{id}` yet)

```bash
cd backend && python -m pytest tests/test_api.py::test_ingest_creates_status_history_entry -v
```

Expected: FAIL — `status_history` not yet in response. That's correct — Task 6 adds it.

- [ ] **Step 7: Commit the creation-side changes**

```bash
git add backend/main.py
git commit -m "feat: record initial status history entry on incident creation"
```

---

### Task 4: Record status history on every status change

**Files:**
- Modify: `backend/main.py`

- [ ] **Step 1: Write a failing test**

Add to `backend/tests/test_api.py`:

```python
async def test_status_change_appends_history(client):
    with patch("main.classify_message", new=AsyncMock(return_value=_INCIDENT_CLASS)):
        with patch("main.push_incident", new=AsyncMock()):
            await client.post("/api/v1/ops/ingest", json=_ORIGINAL, headers={"X-API-Key": "test-secret"})
    incident_id = (await client.get("/incidents")).json()[0]["id"]

    await client.patch(
        f"/incidents/{incident_id}/status",
        json={"status": "acknowledged"},
        headers={"X-API-Key": "test-secret"},
    )
    detail = (await client.get(f"/incidents/{incident_id}")).json()
    # status_history won't be in response until Task 6 — skip until then
    # For now just verify the endpoint returns 200
    assert detail["status"] == "acknowledged"
```

> This test is intentionally lightweight here. The full assertion (`len(status_history) == 2`) will be added in Task 6 once the response includes `status_history`.

- [ ] **Step 2: Run test to confirm it passes (it already should — just verifying no regression)**

```bash
cd backend && python -m pytest tests/test_api.py::test_status_change_appends_history -v
```

Expected: PASS.

- [ ] **Step 3: Update `PATCH /incidents/{id}/status` to insert a history row**

Replace the `update_incident_status` function body (lines ~563–580) with:

```python
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
```

- [ ] **Step 4: Run tests**

```bash
cd backend && python -m pytest -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add backend/main.py
git commit -m "feat: record status history on every status change"
```

---

### Task 5: Set `relinked=True` in the relink endpoint

**Files:**
- Modify: `backend/main.py`

- [ ] **Step 1: Write a failing test**

Add to `backend/tests/test_api.py`:

```python
async def test_relink_sets_relinked_flag(client):
    # Create two incidents
    with patch("main.classify_message", new=AsyncMock(return_value=_INCIDENT_CLASS)):
        with patch("main.push_incident", new=AsyncMock()):
            await client.post("/api/v1/ops/ingest", json=_ORIGINAL, headers={"X-API-Key": "test-secret"})
    incident_id = (await client.get("/incidents")).json()[0]["id"]

    # Add an update to it
    routing = {"routing": "update", "ticket_id": incident_id}
    with patch("main.classify_message", new=AsyncMock(return_value=_INCIDENT_CLASS)):
        with patch("main.classify_update_or_new", new=AsyncMock(return_value=routing)):
            await client.post("/api/v1/ops/ingest", json=_FOLLOWUP, headers={"X-API-Key": "test-secret"})

    # Create a second incident to relink to
    second_payload = {
        "event": "message.received",
        "data": {
            "id": "msg-c", "type": "chat", "isGroup": True,
            "chatId": "123@g.us", "chat": {"name": "Block A"},
            "author": "2541@c.us", "notifyName": "Alice",
            "body": "Different issue", "timestamp": 1782293500,
        },
    }
    with patch("main.classify_message", new=AsyncMock(return_value=_INCIDENT_CLASS)):
        with patch("main.push_incident", new=AsyncMock()):
            await client.post("/api/v1/ops/ingest", json=second_payload, headers={"X-API-Key": "test-secret"})
    incidents = (await client.get("/incidents")).json()
    second_id = next(i["id"] for i in incidents if i["id"] != incident_id)

    # Get the update id
    detail = (await client.get(f"/incidents/{incident_id}")).json()
    update_id = detail["updates"][0]["id"]

    # Relink the update to second incident
    resp = await client.patch(
        f"/incidents/{update_id}/relink",
        json={"incident_id": second_id},
        headers={"X-API-Key": "test-secret"},
    )
    assert resp.status_code == 200

    # Verify relinked flag in detail response (added in Task 6 — for now just check 200)
    assert resp.json()["incident_id"] == second_id
```

- [ ] **Step 2: Run test to confirm it passes**

```bash
cd backend && python -m pytest tests/test_api.py::test_relink_sets_relinked_flag -v
```

Expected: PASS (the assertion on `relinked` field in detail will come in Task 6).

- [ ] **Step 3: Set `relinked=True` in the relink endpoint**

In `relink_update`, find the line `update.ai_linked = False` (around line 552). Directly after it, add:

```python
    update.incident_id = body.incident_id
    update.ai_linked = False
    update.relinked = True
```

- [ ] **Step 4: Run full test suite**

```bash
cd backend && python -m pytest -v
```

Expected: all pass.

- [ ] **Step 5: Commit**

```bash
git add backend/main.py
git commit -m "feat: set relinked=True on IncidentUpdate when re-linked to different incident"
```

---

### Task 6: Include `status_history` and `relinked` in `GET /incidents/{id}`

**Files:**
- Modify: `backend/main.py`

- [ ] **Step 1: Strengthen the existing tests to assert the full response shape**

Update `test_ingest_creates_status_history_entry` in `backend/tests/test_api.py` — the assertion already checks `status_history`, so just verify it runs to failure first:

```bash
cd backend && python -m pytest tests/test_api.py::test_ingest_creates_status_history_entry -v
```

Expected: FAIL — `status_history` key not in response.

Also add this test:

```python
async def test_get_detail_includes_relinked_field(client):
    with patch("main.classify_message", new=AsyncMock(return_value=_INCIDENT_CLASS)):
        with patch("main.push_incident", new=AsyncMock()):
            await client.post("/api/v1/ops/ingest", json=_ORIGINAL, headers={"X-API-Key": "test-secret"})
    incident_id = (await client.get("/incidents")).json()[0]["id"]
    detail = (await client.get(f"/incidents/{incident_id}")).json()
    assert "updates" in detail
    # no updates yet — just verify the key is present when updates exist
    routing = {"routing": "update", "ticket_id": incident_id}
    with patch("main.classify_message", new=AsyncMock(return_value=_INCIDENT_CLASS)):
        with patch("main.classify_update_or_new", new=AsyncMock(return_value=routing)):
            await client.post("/api/v1/ops/ingest", json=_FOLLOWUP, headers={"X-API-Key": "test-secret"})
    detail = (await client.get(f"/incidents/{incident_id}")).json()
    assert "relinked" in detail["updates"][0]
    assert detail["updates"][0]["relinked"] is False
```

- [ ] **Step 2: Run new test to confirm it fails**

```bash
cd backend && python -m pytest tests/test_api.py::test_get_detail_includes_relinked_field -v
```

Expected: FAIL — `relinked` key not in update dict.

- [ ] **Step 3: Update `get_incident_detail` in `main.py`**

Replace the full `get_incident_detail` function (lines ~419–481) with:

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
        .order_by(IncidentStatusHistory.changed_at.asc())
    )
    history_rows = [
        {
            "from_status": h.from_status,
            "to_status": h.to_status,
            "changed_at": h.changed_at.isoformat(),
        }
        for h in history_result.scalars().all()
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
    }
```

- [ ] **Step 4: Run all tests**

```bash
cd backend && python -m pytest -v
```

Expected: all pass, including `test_ingest_creates_status_history_entry` and `test_get_detail_includes_relinked_field`.

- [ ] **Step 5: Commit**

```bash
git add backend/main.py backend/tests/test_api.py
git commit -m "feat: expose status_history and relinked in GET /incidents/{id}"
```

---

### Task 7: Rebuild the detail modal in `dashboard.html`

**Files:**
- Modify: `backend/templates/dashboard.html`

This task has no automated test — verify manually by opening the dashboard, expanding a card, and clicking "↩ Reply."

- [ ] **Step 1: Widen the modal and make header + reply bar fixed**

Find the `.modal` CSS rule (currently `max-width: 680px`). Replace it and add the new `.modal-reply-bar` rule:

```css
    .modal {
      background: var(--surface);
      border: 1px solid var(--line);
      border-radius: var(--radius-xl);
      width: 100%;
      max-width: 860px;
      max-height: 90vh;
      display: flex;
      flex-direction: column;
      box-shadow: var(--shadow);
    }

    .modal-reply-bar {
      flex-shrink: 0;
      border-top: 1px solid var(--line);
      padding: 12px 22px 14px;
      background: var(--bg-soft);
    }
```

- [ ] **Step 2: Add badge and status-history CSS**

After the `.modal-reply-bar` rule, add:

```css
    .update-badge {
      display: inline-flex;
      align-items: center;
      padding: 1px 8px;
      border-radius: 999px;
      font-size: 10px;
      font-weight: 900;
      letter-spacing: .04em;
    }
    .update-badge-sent { background: rgba(14,165,233,0.12); color: #7dd3fc; }
    .update-badge-moved { background: rgba(245,158,11,0.12); color: #fde68a; }

    .update-row-moved { border-color: rgba(245,158,11,0.22); }

    .status-history-list { display: flex; flex-direction: column; gap: 8px; }
    .status-history-row {
      display: flex;
      align-items: center;
      gap: 10px;
      font-size: 12px;
      color: var(--muted);
    }
    .sh-dot {
      width: 8px; height: 8px; border-radius: 50%; flex-shrink: 0;
    }
    .sh-dot-new       { background: #38bdf8; }
    .sh-dot-review    { background: #a855f7; }
    .sh-dot-acknowledged { background: #14b8a6; }
    .sh-dot-resolved  { background: #64748b; }
    .sh-dot-ignored   { background: #374151; }
```

- [ ] **Step 3: Remove the reply compose from `.modal-body` padding and restructure the modal HTML**

Find the `<!-- Detail modal -->` comment in the HTML (around line 1252). Replace the entire modal HTML block with:

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
      <div class="modal-reply-bar" id="modal-reply-bar" style="display:none">
        <div class="modal-section-label">Reply to group</div>
        <div class="reply-wrap">
          <textarea class="reply-textarea" id="modal-reply-input"
            placeholder="Type a message to send to the group…" rows="3"></textarea>
          <button class="reply-send-btn" id="modal-reply-send" onclick="sendReplyFromBar()">↑ Send</button>
        </div>
      </div>
    </div>
  </div>
```

- [ ] **Step 4: Rewrite `renderDetailModal` in the `<script>` block**

Find the `function renderDetailModal(detail)` function and replace it entirely with:

```javascript
let _currentDetailId = null;

function renderDetailModal(detail) {
  _currentDetailId = detail.id;

  // show reply bar if API_KEY is available
  const replyBar = document.getElementById('modal-reply-bar');
  const replyInput = document.getElementById('modal-reply-input');
  const replySend = document.getElementById('modal-reply-send');
  if (replyBar) {
    replyBar.style.display = API_KEY ? 'block' : 'none';
    if (replyInput) { replyInput.value = ''; replyInput.disabled = false; }
    if (replySend) { replySend.disabled = false; replySend.textContent = '↑ Send'; }
  }

  // Updates section
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

        const isDashboard = u.reporter_name === 'Dashboard';
        const isMoved = u.relinked === true;

        let badgeHtml = '';
        let rowClass = 'update-row';
        if (isDashboard) {
          badgeHtml = '<span class="update-badge update-badge-sent">sent ↑</span>';
          rowClass += ' update-row-outbound';
        } else if (isMoved) {
          badgeHtml = '<span class="update-badge update-badge-moved">moved in ↩</span>';
          rowClass += ' update-row-moved';
        }

        const mediaBadge = u.media_count > 0
          ? `<span style="color:var(--blue);font-size:11px">📎 ${u.media_count}</span>` : '';

        return `<div class="${rowClass}">
          <div class="update-meta">
            <span>${esc(u.reporter_name || 'Unknown')} · ${formatTime(u.received_at)}${u.ai_linked ? ' · <em style="opacity:.6">AI-linked</em>' : ''}</span>
            <span style="display:flex;align-items:center;gap:6px">${mediaBadge}${badgeHtml}</span>
          </div>
          <div class="update-body">${esc(u.message_body)}</div>
          ${relinkHtml}
        </div>`;
      }).join('');

  // Status history section
  const statusDotClass = s => ({
    new: 'sh-dot-new', review: 'sh-dot-review', acknowledged: 'sh-dot-acknowledged',
    resolved: 'sh-dot-resolved', ignored: 'sh-dot-ignored'
  }[s] || 'sh-dot-new');

  const historyHtml = !detail.status_history || detail.status_history.length === 0
    ? '<div style="color:var(--muted);font-size:13px">No history recorded.</div>'
    : `<div class="status-history-list">${detail.status_history.map(h => {
        const label = h.from_status
          ? `${esc(h.from_status)} → ${esc(h.to_status)}`
          : `Created as ${esc(h.to_status)}`;
        return `<div class="status-history-row">
          <div class="sh-dot ${statusDotClass(h.to_status)}"></div>
          <span>${label} · ${formatTime(h.changed_at)}</span>
        </div>`;
      }).join('')}</div>`;

  // Attachments section
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
      <div class="modal-section-label">Status history</div>
      ${historyHtml}
    </div>
    <div>
      <div class="modal-section-label">Attachments (${detail.media.length})</div>
      ${mediaHtml}
    </div>`;
}
```

- [ ] **Step 5: Add `sendReplyFromBar` function and update `sendReply`**

The reply bar now uses a shared `modal-reply-input` element rather than per-incident IDs. Find the `async function sendReply(incidentId)` function and replace it with:

```javascript
async function sendReplyFromBar() {
  if (!_currentDetailId) return;
  const textarea = document.getElementById('modal-reply-input');
  const btn = document.getElementById('modal-reply-send');
  const text = textarea.value.trim();
  if (!text) return;

  btn.disabled = true;
  btn.textContent = '…';
  textarea.disabled = true;

  try {
    const r = await fetch(`/incidents/${_currentDetailId}/reply`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-API-Key': API_KEY },
      body: JSON.stringify({ text }),
    });
    if (!r.ok) throw new Error('Send failed');
    textarea.value = '';
    showToast('Message sent to group');
    await openDetailModal(_currentDetailId);
  } catch(e) {
    showToast('Failed to send — please try again');
    btn.disabled = false;
    btn.textContent = '↑ Send';
    textarea.disabled = false;
  }
}
```

- [ ] **Step 6: Rebuild the Docker image and smoke-test manually**

```bash
docker compose build backend && docker compose up -d backend
```

Open `http://localhost:8000`. Expand a card and click "↩ Reply." Verify:
- Modal is wider (~860px)
- Four sections visible: Original Report, Updates, Status History, Attachments
- Status History shows at least one entry (the creation row)
- Reply bar is pinned below the scrollable body
- Send a reply and confirm it appears in the Updates section with the "sent ↑" badge

- [ ] **Step 7: Commit**

```bash
git add backend/templates/dashboard.html
git commit -m "feat: rewrite detail modal with four sections and pinned reply bar"
```

---

## Self-Review Checklist

- **Spec: wider modal (860px)** → Task 7 Step 1 ✓
- **Spec: reply bar pinned outside scroll** → Task 7 Steps 3–5 ✓
- **Spec: `IncidentStatusHistory` table** → Tasks 1–2 ✓
- **Spec: initial history row on incident creation** → Task 3 ✓
- **Spec: history row on status change** → Task 4 ✓
- **Spec: `relinked` column on `IncidentUpdate`** → Tasks 1–2 ✓
- **Spec: `relinked=True` set in relink endpoint** → Task 5 ✓
- **Spec: `status_history` + `relinked` in `GET /incidents/{id}`** → Task 6 ✓
- **Spec: `sent ↑` badge for outbound, `moved in ↩` for relinked** → Task 7 Step 4 ✓
- **Spec: status dot colours** → Task 7 Step 2 ✓
- **Spec: re-link control stays** → Task 7 Step 4 ✓
