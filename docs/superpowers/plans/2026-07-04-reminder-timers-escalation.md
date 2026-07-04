# Per-Ticket Reminder Timers & Auto-Escalation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let admins configure a one-time reminder (sent to a ticket's WhatsApp group as its `end_date` deadline approaches) and automatically flip `escalated` to `true` once a ticket's deadline passes without resolution.

**Architecture:** `Incident` gains two nullable columns (`reminder_offset_hours`, `reminder_sent_at`) added via the existing manual-migration pattern in `backend/database.py`. A new scheduler job, `_check_ticket_reminders()`, runs every 15 minutes on the same `AsyncIOScheduler` instance already created in `lifespan()` (currently only running the daily `_push_summaries` job) and independently checks each open, deadlined ticket for a due reminder and for deadline overrun. The existing `PATCH /incidents/{incident_id}` endpoint (from the priority/category/end_date/escalated plan) gains a fifth field, `reminder_offset_hours`, plus a side effect: changing `end_date` resets `reminder_sent_at` and conditionally resets `escalated`. The existing "Ticket details" section in `backend/templates/dashboard.html`'s detail modal gains a fifth control (a Reminder `<select>`).

**Tech Stack:** FastAPI, SQLAlchemy (async, SQLite in tests / Postgres in prod), APScheduler (`AsyncIOScheduler`, `IntervalTrigger`), Jinja2 + vanilla JS dashboard, pytest + pytest-asyncio + httpx.

## Global Constraints

- `reminder_offset_hours` is one of exactly `{0, 1, 6, 24}` when set, or `null` (reminders disabled — the default for every ticket; opt-in only, no per-client/global defaults).
- The reminder message goes to the ticket's own `group_id` via the existing `send_group_message(chat_id, text)` (imported from `whatsapp.py` into `main.py`) — never an admin DM.
- The reminder check and the escalation check are fully independent: a ticket with no reminder configured still auto-escalates when overdue; a ticket with `reminder_offset_hours=0` can fire both messages in the same scheduler run. This overlap is intentional, not a bug.
- The scheduler job runs on an `IntervalTrigger(minutes=15)`, registered on the same `AsyncIOScheduler` instance as the existing daily `_push_summaries` `CronTrigger` job — not a new scheduler.
- Each ticket's checks must not let one failure (e.g. a `send_group_message` error) block another ticket's checks in the same run — match the existing per-recipient `try/except` pattern in `_push_summaries`.
- State (`reminder_sent_at` / `escalated`) is only persisted after a successful `send_group_message` call, so a failed send retries on the next 15-minute run.
- Changing `end_date` in `PATCH /incidents/{incident_id}` always resets `reminder_sent_at` to `None` (so a reminder can fire again against the new deadline) when the new value differs from the current one. `escalated` is reset to `False` only if the new `end_date` is in the future; if the new `end_date` is not in the future (or is cleared to `null`), `escalated` is left untouched.
- No multi-level escalation, no repeating/recurring reminders — this spec is single-shot only (both explicitly out of scope, per the approved design).
- Follow the existing migration pattern in `backend/database.py`: each migration statement runs in its own `try/except` block.
- Follow the existing audit-log pattern: one `AuditLog` row per changed field, `action="ticket_detail_update"` (existing action string, unchanged).

---

### Task 1: Data model — `reminder_offset_hours` and `reminder_sent_at` columns

**Files:**
- Modify: `backend/models.py` (append 2 columns after `Incident.escalated`, line 26)
- Modify: `backend/database.py` (append 2 migration blocks after line 250, the end of `init_db()`)
- Test: `backend/tests/test_db_migrations.py`

**Interfaces:**
- Produces: `Incident.reminder_offset_hours: Mapped[Optional[int]]` (Integer, nullable, default `None`). `Incident.reminder_sent_at: Mapped[Optional[datetime]]` (DateTime(timezone=True), nullable, default `None`).

- [ ] **Step 1: Write failing migration tests**

Append to the end of `backend/tests/test_db_migrations.py` (after `test_severity_rename_preserves_existing_data`):

```python
async def test_incidents_table_has_reminder_offset_hours_column(migrated_engine):
    async with migrated_engine.connect() as conn:
        result = await conn.execute(text("PRAGMA table_info(incidents)"))
        columns = [row[1] for row in result.all()]
        assert "reminder_offset_hours" in columns


async def test_incidents_table_has_reminder_sent_at_column(migrated_engine):
    async with migrated_engine.connect() as conn:
        result = await conn.execute(text("PRAGMA table_info(incidents)"))
        columns = [row[1] for row in result.all()]
        assert "reminder_sent_at" in columns


async def test_reminder_fields_default_to_null(db_session):
    from models import Incident
    now = datetime.now(timezone.utc)
    incident = Incident(
        group_id="g1@g.us",
        property_name="Block A",
        reporter_name="Alice",
        message_body="Pump leaking",
        category="plumbing",
        priority="high",
        confidence=0.9,
        status="review",
        received_at=now,
    )
    db_session.add(incident)
    await db_session.commit()
    await db_session.refresh(incident)
    assert incident.reminder_offset_hours is None
    assert incident.reminder_sent_at is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_db_migrations.py -v -k "reminder"`
Expected: FAIL — `reminder_offset_hours`/`reminder_sent_at` columns don't exist yet.

- [ ] **Step 3: Add the two columns to the model**

In `backend/models.py`, replace:
```python
    end_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    escalated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
```
with:
```python
    end_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    escalated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
    reminder_offset_hours: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    reminder_sent_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
```

(`Integer` is already imported at the top of `models.py` — no import change needed.)

- [ ] **Step 4: Add the migration blocks**

In `backend/database.py`, append at the end of `init_db()` (after the existing `escalated` migration block that ends the file at line 250):

```python

    try:
        async with engine.begin() as conn:
            await conn.execute(text("ALTER TABLE incidents ADD COLUMN reminder_offset_hours INTEGER"))
    except Exception:
        pass

    try:
        async with engine.begin() as conn:
            await conn.execute(text("ALTER TABLE incidents ADD COLUMN reminder_sent_at TIMESTAMP"))
    except Exception:
        pass
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_db_migrations.py -v`
Expected: PASS — all tests in the file.

- [ ] **Step 6: Commit**

```bash
git add backend/models.py backend/database.py backend/tests/test_db_migrations.py
git commit -m "feat: add reminder_offset_hours and reminder_sent_at columns to Incident"
```

---

### Task 2: `PATCH /incidents/{incident_id}` — reminder field, end_date side effects

**Files:**
- Modify: `backend/main.py` — `_VALID_PRIORITIES` block (line 37), `TicketDetailUpdateBody` class (lines 112–123), `update_incident_fields` function (lines 1063–1130), `get_incident_detail` return dict (lines 899–917)
- Test: `backend/tests/test_ticket_detail_update.py`

**Interfaces:**
- Consumes: `Incident.reminder_offset_hours`, `Incident.reminder_sent_at` (Task 1).
- Produces: `PATCH /incidents/{incident_id}` accepts `reminder_offset_hours?: int | null` in its body; response and `GET /incidents/{incident_id}` both include `"reminder_offset_hours"` and `"reminder_sent_at"` keys. Used by Task 4's UI and Task 3's scheduler (which reads these columns directly).

- [ ] **Step 1: Write the failing tests**

Append to `backend/tests/test_ticket_detail_update.py`:

```python
async def _set_incident_fields(incident_id, **fields):
    async with _Session() as session:
        incident = await session.get(Incident, incident_id)
        for k, v in fields.items():
            setattr(incident, k, v)
        session.add(incident)
        await session.commit()


async def test_patch_sets_reminder_offset_hours(tdu_client):
    incident_id = await _seed_incident()
    await tdu_client.post("/login", data={"username": "ticketadmin", "password": "pass1234"})
    resp = await tdu_client.patch(f"/incidents/{incident_id}", json={"reminder_offset_hours": 6})
    assert resp.status_code == 200
    assert resp.json()["reminder_offset_hours"] == 6


async def test_patch_clears_reminder_offset_hours(tdu_client):
    incident_id = await _seed_incident()
    await tdu_client.post("/login", data={"username": "ticketadmin", "password": "pass1234"})
    await tdu_client.patch(f"/incidents/{incident_id}", json={"reminder_offset_hours": 6})
    resp = await tdu_client.patch(f"/incidents/{incident_id}", json={"reminder_offset_hours": None})
    assert resp.status_code == 200
    assert resp.json()["reminder_offset_hours"] is None


async def test_patch_rejects_invalid_reminder_offset_hours(tdu_client):
    incident_id = await _seed_incident()
    await tdu_client.post("/login", data={"username": "ticketadmin", "password": "pass1234"})
    resp = await tdu_client.patch(f"/incidents/{incident_id}", json={"reminder_offset_hours": 12})
    assert resp.status_code == 422


async def test_patch_end_date_change_resets_reminder_sent_at(tdu_client):
    incident_id = await _seed_incident()
    await tdu_client.post("/login", data={"username": "ticketadmin", "password": "pass1234"})
    await tdu_client.patch(f"/incidents/{incident_id}", json={"end_date": "2026-08-01T00:00:00"})
    await _set_incident_fields(incident_id, reminder_sent_at=datetime(2026, 7, 1, tzinfo=timezone.utc))
    resp = await tdu_client.patch(f"/incidents/{incident_id}", json={"end_date": "2026-09-01T00:00:00"})
    assert resp.status_code == 200
    assert resp.json()["reminder_sent_at"] is None


async def test_patch_future_end_date_resets_escalated(tdu_client):
    incident_id = await _seed_incident()
    await tdu_client.post("/login", data={"username": "ticketadmin", "password": "pass1234"})
    await tdu_client.patch(f"/incidents/{incident_id}", json={"end_date": "2020-01-01T00:00:00"})
    await tdu_client.patch(f"/incidents/{incident_id}", json={"escalated": True})
    resp = await tdu_client.patch(f"/incidents/{incident_id}", json={"end_date": "2099-01-01T00:00:00"})
    assert resp.status_code == 200
    assert resp.json()["escalated"] is False


async def test_patch_past_end_date_leaves_escalated_untouched(tdu_client):
    incident_id = await _seed_incident()
    await tdu_client.post("/login", data={"username": "ticketadmin", "password": "pass1234"})
    await tdu_client.patch(f"/incidents/{incident_id}", json={"end_date": "2020-01-01T00:00:00"})
    await tdu_client.patch(f"/incidents/{incident_id}", json={"escalated": True})
    resp = await tdu_client.patch(f"/incidents/{incident_id}", json={"end_date": "2020-06-01T00:00:00"})
    assert resp.status_code == 200
    assert resp.json()["escalated"] is True


async def test_get_incident_detail_includes_reminder_fields(tdu_client):
    incident_id = await _seed_incident()
    await tdu_client.post("/login", data={"username": "ticketadmin", "password": "pass1234"})
    await tdu_client.patch(f"/incidents/{incident_id}", json={"reminder_offset_hours": 1})
    resp = await tdu_client.get(f"/incidents/{incident_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["reminder_offset_hours"] == 1
    assert data["reminder_sent_at"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_ticket_detail_update.py -v -k "reminder"`
Expected: FAIL — `reminder_offset_hours` isn't accepted by the endpoint yet and isn't in any response.

- [ ] **Step 3: Add the `_VALID_REMINDER_OFFSETS` constant**

In `backend/main.py`, replace:
```python
_VALID_STATUSES = {"new", "review", "acknowledged", "resolved", "ignored"}
_VALID_PRIORITIES = {"low", "medium", "high", "urgent"}
_MEDIA_TYPES = {"image", "video", "document", "audio"}
```
with:
```python
_VALID_STATUSES = {"new", "review", "acknowledged", "resolved", "ignored"}
_VALID_PRIORITIES = {"low", "medium", "high", "urgent"}
_VALID_REMINDER_OFFSETS = {0, 1, 6, 24}
_MEDIA_TYPES = {"image", "video", "document", "audio"}
```

- [ ] **Step 4: Add the field to `TicketDetailUpdateBody`**

In `backend/main.py`, replace:
```python
class TicketDetailUpdateBody(BaseModel):
    priority: Optional[str] = None
    category: Optional[str] = None
    end_date: Optional[datetime] = None
    escalated: Optional[bool] = None

    @field_validator("end_date")
    @classmethod
    def normalize_end_date(cls, v: Optional[datetime]) -> Optional[datetime]:
        if v is not None and v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v
```
with:
```python
class TicketDetailUpdateBody(BaseModel):
    priority: Optional[str] = None
    category: Optional[str] = None
    end_date: Optional[datetime] = None
    escalated: Optional[bool] = None
    reminder_offset_hours: Optional[int] = None

    @field_validator("end_date")
    @classmethod
    def normalize_end_date(cls, v: Optional[datetime]) -> Optional[datetime]:
        if v is not None and v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc)
        return v
```

- [ ] **Step 5: Update `update_incident_fields`**

In `backend/main.py`, replace:
```python
    if "end_date" in fields_set:
        if body.end_date != incident.end_date:
            changes.append(f"end_date: {incident.end_date} → {body.end_date}")
            incident.end_date = body.end_date

    if "escalated" in fields_set:
        if body.escalated is None:
            raise HTTPException(status_code=422, detail="escalated must be a boolean")
        if body.escalated != incident.escalated:
            changes.append(f"escalated: {incident.escalated} → {body.escalated}")
            incident.escalated = body.escalated

    db.add(incident)
```
with:
```python
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

    db.add(incident)
```

- [ ] **Step 6: Update the endpoint's return dict**

In `backend/main.py`, replace:
```python
    return {
        "id": incident.id,
        "priority": incident.priority,
        "category": incident.category,
        "end_date": incident.end_date.isoformat() if incident.end_date else None,
        "escalated": incident.escalated,
    }
```
with:
```python
    return {
        "id": incident.id,
        "priority": incident.priority,
        "category": incident.category,
        "end_date": incident.end_date.isoformat() if incident.end_date else None,
        "escalated": incident.escalated,
        "reminder_offset_hours": incident.reminder_offset_hours,
        "reminder_sent_at": incident.reminder_sent_at.isoformat() if incident.reminder_sent_at else None,
    }
```

- [ ] **Step 7: Add the fields to `GET /incidents/{incident_id}`**

In `backend/main.py`, in `get_incident_detail`, replace:
```python
    return {
        "id": incident.id,
        "property_name": incident.property_name,
        "reporter_name": incident.reporter_name,
        "reporter_phone": incident.reporter_phone,
        "category": incident.category,
        "priority": incident.priority,
        "end_date": incident.end_date.isoformat() if incident.end_date else None,
        "escalated": incident.escalated,
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
```
with:
```python
    return {
        "id": incident.id,
        "property_name": incident.property_name,
        "reporter_name": incident.reporter_name,
        "reporter_phone": incident.reporter_phone,
        "category": incident.category,
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
    }
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_ticket_detail_update.py -v`
Expected: PASS — all tests in the file.

- [ ] **Step 9: Run the full backend test suite**

Run: `cd backend && python -m pytest -v`
Expected: PASS

- [ ] **Step 10: Commit**

```bash
git add backend/main.py backend/tests/test_ticket_detail_update.py
git commit -m "feat: PATCH /incidents/{id} accepts reminder_offset_hours, resets reminder/escalation on end_date change"
```

---

### Task 3: Scheduler job — `_check_ticket_reminders`

**Files:**
- Modify: `backend/main.py` — apscheduler imports (line 15), add `_check_ticket_reminders()` after `_push_summaries` (after line 389), register job in `lifespan()` (lines 251–259)
- Test: `backend/tests/test_reminder_scheduler.py` (new file)

**Interfaces:**
- Consumes: `Incident.reminder_offset_hours`, `Incident.reminder_sent_at`, `Incident.escalated`, `Incident.end_date`, `Incident.status` (Task 1). `send_group_message(chat_id, text)` (existing, imported in `main.py` from `whatsapp.py`). `AsyncSessionLocal` (existing, imported in `main.py` from `database.py`).
- Produces: `_check_ticket_reminders()` async function, registered on the `AsyncIOScheduler` instance in `lifespan()` with `IntervalTrigger(minutes=15)`.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_reminder_scheduler.py`:

```python
import os
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("GATEWAY_SECRET_TOKEN", "test-secret")
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-testing-only1")

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest_asyncio
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import StaticPool

import main
from database import Base
from models import Incident

_engine = create_async_engine(
    "sqlite+aiosqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = async_sessionmaker(_engine, expire_on_commit=False)


@pytest_asyncio.fixture(scope="module", autouse=True)
async def schema():
    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@pytest_asyncio.fixture(autouse=True)
async def clean_tables():
    yield
    async with _engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            await conn.execute(table.delete())


async def _make_incident(**overrides):
    now = datetime.now(timezone.utc)
    defaults = dict(
        group_id="g1@g.us",
        property_name="Block A",
        reporter_name="Alice",
        message_body="Pump leaking",
        category="plumbing",
        priority="medium",
        confidence=0.9,
        status="review",
        received_at=now,
    )
    defaults.update(overrides)
    async with _Session() as session:
        incident = Incident(**defaults)
        session.add(incident)
        await session.commit()
        await session.refresh(incident)
        return incident.id


async def _get_incident(incident_id):
    async with _Session() as session:
        return await session.get(Incident, incident_id)


async def test_reminder_fires_once_past_offset_time():
    now = datetime.now(timezone.utc)
    incident_id = await _make_incident(
        end_date=now + timedelta(hours=1),
        reminder_offset_hours=1,
    )
    with patch("main.AsyncSessionLocal", _Session):
        with patch("main.send_group_message", new=AsyncMock()) as mock_send:
            await main._check_ticket_reminders()
    mock_send.assert_awaited_once()
    assert mock_send.await_args.args[0] == "g1@g.us"
    ticket = await _get_incident(incident_id)
    assert ticket.reminder_sent_at is not None


async def test_reminder_does_not_fire_before_offset_time():
    now = datetime.now(timezone.utc)
    incident_id = await _make_incident(
        end_date=now + timedelta(hours=5),
        reminder_offset_hours=1,
    )
    with patch("main.AsyncSessionLocal", _Session):
        with patch("main.send_group_message", new=AsyncMock()) as mock_send:
            await main._check_ticket_reminders()
    mock_send.assert_not_awaited()
    ticket = await _get_incident(incident_id)
    assert ticket.reminder_sent_at is None


async def test_reminder_does_not_refire_after_being_sent():
    now = datetime.now(timezone.utc)
    await _make_incident(
        end_date=now + timedelta(hours=1),
        reminder_offset_hours=1,
        reminder_sent_at=now - timedelta(minutes=5),
    )
    with patch("main.AsyncSessionLocal", _Session):
        with patch("main.send_group_message", new=AsyncMock()) as mock_send:
            await main._check_ticket_reminders()
    mock_send.assert_not_awaited()


async def test_escalation_fires_for_overdue_ticket_with_no_reminder_configured():
    now = datetime.now(timezone.utc)
    incident_id = await _make_incident(
        end_date=now - timedelta(hours=1),
        reminder_offset_hours=None,
    )
    with patch("main.AsyncSessionLocal", _Session):
        with patch("main.send_group_message", new=AsyncMock()) as mock_send:
            await main._check_ticket_reminders()
    mock_send.assert_awaited_once()
    ticket = await _get_incident(incident_id)
    assert ticket.escalated is True


async def test_ticket_with_zero_offset_receives_both_messages():
    now = datetime.now(timezone.utc)
    incident_id = await _make_incident(
        end_date=now - timedelta(minutes=1),
        reminder_offset_hours=0,
    )
    with patch("main.AsyncSessionLocal", _Session):
        with patch("main.send_group_message", new=AsyncMock()) as mock_send:
            await main._check_ticket_reminders()
    assert mock_send.await_count == 2
    ticket = await _get_incident(incident_id)
    assert ticket.reminder_sent_at is not None
    assert ticket.escalated is True


async def test_resolved_ticket_past_deadline_is_skipped():
    now = datetime.now(timezone.utc)
    incident_id = await _make_incident(
        end_date=now - timedelta(hours=1),
        reminder_offset_hours=0,
        status="resolved",
    )
    with patch("main.AsyncSessionLocal", _Session):
        with patch("main.send_group_message", new=AsyncMock()) as mock_send:
            await main._check_ticket_reminders()
    mock_send.assert_not_awaited()
    ticket = await _get_incident(incident_id)
    assert ticket.escalated is False
    assert ticket.reminder_sent_at is None


async def test_send_failure_for_one_ticket_does_not_block_others():
    now = datetime.now(timezone.utc)
    failing_id = await _make_incident(
        group_id="fail@g.us",
        end_date=now - timedelta(hours=1),
        reminder_offset_hours=None,
    )
    ok_id = await _make_incident(
        group_id="ok@g.us",
        end_date=now - timedelta(hours=1),
        reminder_offset_hours=None,
    )

    async def _side_effect(chat_id, text):
        if chat_id == "fail@g.us":
            raise RuntimeError("send failed")
        return "msg-id"

    with patch("main.AsyncSessionLocal", _Session):
        with patch("main.send_group_message", new=AsyncMock(side_effect=_side_effect)):
            await main._check_ticket_reminders()

    failing_ticket = await _get_incident(failing_id)
    ok_ticket = await _get_incident(ok_id)
    assert failing_ticket.escalated is False
    assert ok_ticket.escalated is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_reminder_scheduler.py -v`
Expected: FAIL — `main` has no attribute `_check_ticket_reminders`.

- [ ] **Step 3: Add the `IntervalTrigger` import**

In `backend/main.py`, replace:
```python
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
```
with:
```python
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
```

- [ ] **Step 4: Add `_check_ticket_reminders()`**

In `backend/main.py`, immediately after the `_push_summaries` function definition (after its last line, before `async def _get_allowed_groups`), add:

```python
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

        for ticket in tickets:
            try:
                if (
                    ticket.reminder_offset_hours is not None
                    and ticket.reminder_sent_at is None
                    and now >= ticket.end_date - timedelta(hours=ticket.reminder_offset_hours)
                ):
                    await send_group_message(
                        ticket.group_id,
                        f"⏰ Reminder: Ticket #{ticket.id} ({ticket.property_name}) is "
                        f"approaching its deadline.\n{ticket.message_body[:200]}",
                    )
                    ticket.reminder_sent_at = now
                    db.add(ticket)
                    await db.commit()
            except Exception as exc:
                logger.error("Reminder check failed for incident %s: %s", ticket.id, exc)

            try:
                if not ticket.escalated and now >= ticket.end_date:
                    await send_group_message(
                        ticket.group_id,
                        f"🚨 Ticket #{ticket.id} ({ticket.property_name}) has passed its "
                        f"deadline and has been escalated.\n{ticket.message_body[:200]}",
                    )
                    ticket.escalated = True
                    db.add(ticket)
                    await db.commit()
            except Exception as exc:
                logger.error("Escalation check failed for incident %s: %s", ticket.id, exc)
```

- [ ] **Step 5: Register the job in `lifespan()`**

In `backend/main.py`, replace:
```python
        scheduler.add_job(
            _push_summaries,
            CronTrigger(
                hour=SUMMARY_SCHEDULE_HOUR,
                day_of_week="mon-fri",
                timezone=SUMMARY_TIMEZONE,
            ),
        )
        scheduler.start()
```
with:
```python
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
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_reminder_scheduler.py -v`
Expected: PASS — all tests in the file.

- [ ] **Step 7: Run the full backend test suite**

Run: `cd backend && python -m pytest -v`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add backend/main.py backend/tests/test_reminder_scheduler.py
git commit -m "feat: add 15-minute scheduler job for ticket reminders and auto-escalation"
```

---

### Task 4: UI — Reminder control in the ticket detail modal

**Files:**
- Modify: `backend/templates/dashboard.html` — `renderTicketDetailsSection` function (around line 1478)

**Interfaces:**
- Consumes: `PATCH /incidents/{incident_id}` accepts `reminder_offset_hours` (Task 2); `GET /incidents/{incident_id}` returns `reminder_offset_hours`/`reminder_sent_at` (Task 2); existing `updateTicketField(incidentId, field, value)` JS function (unchanged).
- Produces: `reminderLabel(hours)` JS helper.

- [ ] **Step 1: Add the Reminder control**

In `backend/templates/dashboard.html`, replace:
```javascript
function renderTicketDetailsSection(detail) {
  const isAdmin = CURRENT_ROLE === 'admin' || CURRENT_ROLE === 'super_admin';
  const endDateValue = detail.end_date ? detail.end_date.slice(0, 10) : '';

  if (!isAdmin) {
    const catLabel = (CATEGORIES.find(c => c.slug === detail.category) || {}).label || detail.category;
    return `
      <div class="ticket-details-readonly">
        <div><span class="label">Priority</span>${esc(cap(detail.priority))}</div>
        <div><span class="label">Category</span>${esc(catLabel)}</div>
        <div><span class="label">End date</span>${endDateValue || '—'}</div>
        <div><span class="label">Escalated</span>${detail.escalated ? 'Yes' : 'No'}</div>
      </div>`;
  }

  const priorityOptions = ['low', 'medium', 'high', 'urgent'].map(p =>
    `<option value="${p}" ${p === detail.priority ? 'selected' : ''}>${cap(p)}</option>`
  ).join('');
  const categoryOptions = CATEGORIES.map(c =>
    `<option value="${c.slug}" ${c.slug === detail.category ? 'selected' : ''}>${esc(c.label)}</option>`
  ).join('');

  return `
    <div class="ticket-details-grid">
      <label>Priority
        <select onchange="updateTicketField(${detail.id}, 'priority', this.value)">${priorityOptions}</select>
      </label>
      <label>Category
        <select onchange="updateTicketField(${detail.id}, 'category', this.value)">${categoryOptions}</select>
      </label>
      <label>End date
        <input type="date" value="${endDateValue}"
          onchange="updateTicketField(${detail.id}, 'end_date', this.value || null)">
      </label>
      <button type="button" class="act-btn ${detail.escalated ? 'btn-unescalate' : 'btn-escalate'}"
        onclick="updateTicketField(${detail.id}, 'escalated', ${!detail.escalated})">
        ${detail.escalated ? '✗ Un-escalate' : '⚠ Escalate'}
      </button>
    </div>`;
}
```
with:
```javascript
function reminderLabel(hours) {
  if (hours === null || hours === undefined) return 'Off';
  if (hours === 0) return 'At deadline';
  return `${hours} hour${hours === 1 ? '' : 's'} before`;
}

function renderTicketDetailsSection(detail) {
  const isAdmin = CURRENT_ROLE === 'admin' || CURRENT_ROLE === 'super_admin';
  const endDateValue = detail.end_date ? detail.end_date.slice(0, 10) : '';

  if (!isAdmin) {
    const catLabel = (CATEGORIES.find(c => c.slug === detail.category) || {}).label || detail.category;
    return `
      <div class="ticket-details-readonly">
        <div><span class="label">Priority</span>${esc(cap(detail.priority))}</div>
        <div><span class="label">Category</span>${esc(catLabel)}</div>
        <div><span class="label">End date</span>${endDateValue || '—'}</div>
        <div><span class="label">Escalated</span>${detail.escalated ? 'Yes' : 'No'}</div>
        <div><span class="label">Reminder</span>${reminderLabel(detail.reminder_offset_hours)}</div>
      </div>`;
  }

  const priorityOptions = ['low', 'medium', 'high', 'urgent'].map(p =>
    `<option value="${p}" ${p === detail.priority ? 'selected' : ''}>${cap(p)}</option>`
  ).join('');
  const categoryOptions = CATEGORIES.map(c =>
    `<option value="${c.slug}" ${c.slug === detail.category ? 'selected' : ''}>${esc(c.label)}</option>`
  ).join('');
  const reminderChoices = [
    { value: '', label: 'None' },
    { value: '0', label: 'At deadline' },
    { value: '1', label: '1 hour before' },
    { value: '6', label: '6 hours before' },
    { value: '24', label: '24 hours before' },
  ];
  const reminderOptions = reminderChoices.map(o => {
    const selected = (detail.reminder_offset_hours === null || detail.reminder_offset_hours === undefined)
      ? o.value === ''
      : String(detail.reminder_offset_hours) === o.value;
    return `<option value="${o.value}" ${selected ? 'selected' : ''}>${o.label}</option>`;
  }).join('');

  return `
    <div class="ticket-details-grid">
      <label>Priority
        <select onchange="updateTicketField(${detail.id}, 'priority', this.value)">${priorityOptions}</select>
      </label>
      <label>Category
        <select onchange="updateTicketField(${detail.id}, 'category', this.value)">${categoryOptions}</select>
      </label>
      <label>End date
        <input type="date" value="${endDateValue}"
          onchange="updateTicketField(${detail.id}, 'end_date', this.value || null)">
      </label>
      <label>Reminder
        <select onchange="updateTicketField(${detail.id}, 'reminder_offset_hours', this.value === '' ? null : Number(this.value))">${reminderOptions}</select>
      </label>
      <button type="button" class="act-btn ${detail.escalated ? 'btn-unescalate' : 'btn-escalate'}"
        onclick="updateTicketField(${detail.id}, 'escalated', ${!detail.escalated})">
        ${detail.escalated ? '✗ Un-escalate' : '⚠ Escalate'}
      </button>
    </div>`;
}
```

- [ ] **Step 2: Verify no import/regression errors**

Run: `cd backend && python -c "import main"`
Expected: no import errors (this task is HTML/JS only, no Python change).

Run: `cd backend && python -m pytest -v`
Expected: PASS

- [ ] **Step 3: Commit**

```bash
git add backend/templates/dashboard.html
git commit -m "feat: add Reminder control to ticket detail modal"
```

---

### Task 5: Full browser verification

**Files:** none (verification only)

- [ ] **Step 1: Start the backend dev server**

Run: `cd backend && python -m pytest -v` one final time to confirm a clean baseline, then start the app locally (a `backend/.env` already exists in this repo with the required env vars):

Run: `cd backend && uvicorn main:app --reload --port 8000`
Expected: server starts, log line `Application startup complete.`

- [ ] **Step 2: Manually verify in a browser**

1. Log in as an `admin` user. Open the dashboard, click a ticket, set an End date a few hours in the future.
2. In the same modal, change the new "Reminder" dropdown to "1 hour before" — confirm a toast appears and the modal reloads with "1 hour before" still selected.
3. Close and reopen the modal — confirm the Reminder selection persisted.
4. Set the Reminder back to "None" — confirm it persists as "None" on reopen.
5. Log out, log in as a `user`-role account scoped to the same group. Open the same ticket — confirm the ticket details section (including the new Reminder line) renders as **read-only text** (e.g. "Reminder: Off" or "Reminder: 1 hour before").
6. Back as admin, open a Python shell in the same environment (`cd backend && python`) and run the scheduler function directly against the dev database to confirm it executes without error end-to-end:
   ```python
   import asyncio, main
   asyncio.run(main._check_ticket_reminders())
   ```
   Expected: completes without raising (the automated tests in Task 3 already cover the send/skip logic in isolation — this just confirms the function runs cleanly against the real dev DB and `send_group_message` wiring).

- [ ] **Step 3: Report results to the user**

Summarize what was verified and any deviations observed.
