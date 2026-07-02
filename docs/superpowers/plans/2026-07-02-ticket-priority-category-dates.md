# Ticket Priority, Category, End Date & Escalation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rename the existing `severity` field to a 4-level `priority` field, add admin-editable `category` reassignment, an `end_date` (deadline), and a manually-toggled `escalated` flag to tickets, all editable from the existing ticket detail modal in the dashboard.

**Architecture:** `backend/models.py`'s `Incident.severity` column is renamed to `priority` (values low/medium/high/urgent, was low/medium/high) via a manual SQL migration in `backend/database.py`. Two new nullable/defaulted columns (`end_date`, `escalated`) are added the same way. A new admin-only `PATCH /incidents/{incident_id}` endpoint in `backend/main.py` lets any of the four fields be updated, each change logged to `AuditLog`. The existing ticket detail modal in `backend/templates/dashboard.html` gains a "Ticket details" section with editable controls (admins) or read-only text (regular users). Because `severity` is referenced throughout the backend (ingest, summaries, odoo stub) and the dashboard's filter/stat UI, the rename fans out into every one of those call sites and their tests.

**Tech Stack:** FastAPI, SQLAlchemy (async, SQLite in tests / Postgres in prod), Jinja2 + vanilla JS dashboard, pytest + pytest-asyncio + httpx.

## Global Constraints

- Priority levels are exactly: `low`, `medium`, `high`, `urgent` (spec-approved, binary escalation only — no multi-level escalation).
- `end_date` is deadline-only; ticket creation time (`received_at`) is unchanged and is *not* renamed to "start date."
- Auto-escalation on overdue `end_date` is explicitly out of scope for this plan (deferred to the reminder-timers plan). This plan only adds the `escalated` column and a manual toggle.
- The new `PATCH /incidents/{incident_id}` endpoint and the new UI controls are admin-only (`admin` or `super_admin` role via the existing `require_admin` dependency). Regular `user`-role accounts see read-only values.
- Follow the existing migration pattern in `backend/database.py`: each migration statement runs in its own `try/except` block so a no-op failure (column already renamed/added) doesn't abort later migrations.
- Follow the existing audit-log pattern: one `AuditLog` row per changed field, `action` field value under 30 chars (column is `String(30)`).

---

### Task 1: Rename `severity` → `priority` in the data model, add `end_date` and `escalated`

**Files:**
- Modify: `backend/models.py:19` (Incident.severity → Incident.priority)
- Modify: `backend/database.py` (append 3 new migration blocks after line 230, before the final seed-categories block ends — i.e. insert as new blocks at the end of `init_db()`)
- Test: `backend/tests/test_db_migrations.py`

**Interfaces:**
- Produces: `Incident.priority: Mapped[str]` (String(20), not nullable) — replaces `Incident.severity`.
- Produces: `Incident.end_date: Mapped[Optional[datetime]]` (DateTime(timezone=True), nullable).
- Produces: `Incident.escalated: Mapped[bool]` (Boolean, not nullable, default `False`).

- [ ] **Step 1: Write failing migration tests**

Append to `backend/tests/test_db_migrations.py`:

```python
async def test_incidents_table_has_priority_column(migrated_engine):
    async with migrated_engine.connect() as conn:
        result = await conn.execute(text("PRAGMA table_info(incidents)"))
        columns = [row[1] for row in result.all()]
        assert "priority" in columns
        assert "severity" not in columns


async def test_incidents_table_has_end_date_column(migrated_engine):
    async with migrated_engine.connect() as conn:
        result = await conn.execute(text("PRAGMA table_info(incidents)"))
        columns = [row[1] for row in result.all()]
        assert "end_date" in columns


async def test_incidents_table_has_escalated_column(migrated_engine):
    async with migrated_engine.connect() as conn:
        result = await conn.execute(text("PRAGMA table_info(incidents)"))
        columns = [row[1] for row in result.all()]
        assert "escalated" in columns


async def test_escalated_defaults_to_false(db_session):
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
    assert incident.escalated is False
    assert incident.end_date is None


async def test_severity_rename_preserves_existing_data():
    """Simulates upgrading a pre-existing DB that still has the old `severity` column."""
    upgrade_engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    async with upgrade_engine.begin() as conn:
        await conn.execute(text("""
            CREATE TABLE incidents (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT NOT NULL,
                property_name TEXT NOT NULL,
                reporter_name TEXT,
                reporter_phone TEXT,
                message_body TEXT NOT NULL,
                category VARCHAR(50) NOT NULL,
                severity VARCHAR(20) NOT NULL,
                confidence FLOAT NOT NULL,
                status VARCHAR(20) NOT NULL DEFAULT 'review',
                received_at TIMESTAMP NOT NULL,
                message_id TEXT
            )
        """))
        await conn.execute(text(
            "INSERT INTO incidents (group_id, property_name, message_body, category, "
            "severity, confidence, status, received_at) VALUES "
            "('g1@g.us', 'Block A', 'Pump leaking', 'plumbing', 'high', 0.9, 'review', "
            "'2026-01-01 00:00:00')"
        ))

    import database
    original_engine = database.engine
    database.engine = upgrade_engine
    try:
        await init_db()
    finally:
        database.engine = original_engine

    async with upgrade_engine.connect() as conn:
        result = await conn.execute(text("SELECT priority FROM incidents"))
        assert result.scalar_one() == "high"
        columns = [row[1] for row in (await conn.execute(text("PRAGMA table_info(incidents)"))).all()]
        assert "severity" not in columns
        assert "end_date" in columns
        assert "escalated" in columns
    await upgrade_engine.dispose()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_db_migrations.py -v -k "priority or end_date or escalated"`
Expected: FAIL — `priority`/`end_date`/`escalated` columns don't exist yet, `severity` still present.

- [ ] **Step 3: Rename the column in the model and add the two new columns**

In `backend/models.py`, replace line 19:

```python
    severity: Mapped[str] = mapped_column(String(20), nullable=False)
```

with:

```python
    priority: Mapped[str] = mapped_column(String(20), nullable=False)
```

Then, immediately after `received_at`/`message_id`/`updated_at` (after line 24, `updated_at: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)`), add:

```python
    end_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    escalated: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")
```

- [ ] **Step 4: Add the migration blocks**

In `backend/database.py`, append these three blocks at the end of `init_db()` (after the seed-categories `try/except` block that ends the function, i.e. as new code after line 230):

```python
    try:
        async with engine.begin() as conn:
            await conn.execute(text("ALTER TABLE incidents RENAME COLUMN severity TO priority"))
    except Exception:
        pass

    try:
        async with engine.begin() as conn:
            await conn.execute(text("ALTER TABLE incidents ADD COLUMN end_date TIMESTAMP"))
    except Exception:
        pass

    try:
        async with engine.begin() as conn:
            await conn.execute(text(
                "ALTER TABLE incidents ADD COLUMN escalated BOOLEAN NOT NULL DEFAULT FALSE"
            ))
    except Exception:
        pass
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_db_migrations.py -v`
Expected: PASS (all tests in the file, including pre-existing ones).

Note: this will not yet make the full suite pass — every other file that does `Incident(severity=...)` or reads `.severity` will now fail to import/run until Tasks 2–3 update them. That's expected at this point.

- [ ] **Step 6: Commit**

```bash
git add backend/models.py backend/database.py backend/tests/test_db_migrations.py
git commit -m "feat: rename Incident.severity to priority, add end_date and escalated columns"
```

---

### Task 2: Classifier — 4-level priority, AI auto-assigns initial value

**Files:**
- Modify: `backend/classifier.py`
- Test: `backend/tests/test_classifier.py`

**Interfaces:**
- Consumes: nothing new.
- Produces: `classify_message()` returns a dict with key `"priority"` (one of `low`/`medium`/`high`/`urgent`) instead of `"severity"`. All other keys (`is_incident`, `category`, `confidence`) unchanged.

- [ ] **Step 1: Update the failing tests first**

In `backend/tests/test_classifier.py`, replace every `"severity"` JSON key in mock LLM responses and every `result["severity"]` assertion with `"priority"` / `result["priority"]`, and widen the values used to include an urgent-level test. Specifically:

Replace (line 23):
```python
        "response": '{"is_incident": true, "category": "plumbing", "severity": "high", "confidence": 0.92}'
```
with:
```python
        "response": '{"is_incident": true, "category": "plumbing", "priority": "high", "confidence": 0.92}'
```

Replace (line 31):
```python
    assert result["severity"] == "high"
```
with:
```python
    assert result["priority"] == "high"
```

Replace (line 65):
```python
        "response": '{"is_incident": false, "category": "other", "severity": "low", "confidence": 0.95}'
```
with:
```python
        "response": '{"is_incident": false, "category": "other", "priority": "low", "confidence": 0.95}'
```

Replace (line 79):
```python
        "response": '{"is_incident": true, "category": "magic", "severity": "high", "confidence": 0.9}'
```
with:
```python
        "response": '{"is_incident": true, "category": "magic", "priority": "high", "confidence": 0.9}'
```

Add a new test after `test_unknown_category_falls_back_to_other`:

```python
async def test_urgent_priority_is_accepted():
    mock_db = _make_mock_db()
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "response": '{"is_incident": true, "category": "plumbing", "priority": "urgent", "confidence": 0.95}'
    }
    with patch("classifier.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.post = AsyncMock(return_value=mock_resp)
        result = await classify_message("Pipe burst, flooding the lobby", mock_db)
    assert result["priority"] == "urgent"


async def test_unknown_priority_falls_back_to_medium():
    mock_db = _make_mock_db()
    mock_resp = MagicMock()
    mock_resp.raise_for_status = MagicMock()
    mock_resp.json.return_value = {
        "response": '{"is_incident": true, "category": "plumbing", "priority": "critical", "confidence": 0.9}'
    }
    with patch("classifier.httpx.AsyncClient") as mock_client:
        mock_client.return_value.__aenter__.return_value.post = AsyncMock(return_value=mock_resp)
        result = await classify_message("Something urgent-ish", mock_db)
    assert result["priority"] == "medium"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_classifier.py -v`
Expected: FAIL — `classifier.py` still returns `"severity"` and only accepts 3 values.

- [ ] **Step 3: Update `classifier.py`**

Replace lines 19–26:
```python
_FALLBACK: dict = {
    "is_incident": False,
    "category": "other",
    "severity": "low",
    "confidence": 0.0,
}

_VALID_SEVERITIES = {"low", "medium", "high"}
```
with:
```python
_FALLBACK: dict = {
    "is_incident": False,
    "category": "other",
    "priority": "low",
    "confidence": 0.0,
}

_VALID_PRIORITIES = {"low", "medium", "high", "urgent"}
```

Replace lines 37–43 (inside `_build_prompt`):
```python
        "Return ONLY valid JSON, no explanation, no markdown:\n"
        "{\n"
        '  "is_incident": true or false,\n'
        f'  "category": "{pipe_cats}",\n'
        '  "severity": "low|medium|high",\n'
        '  "confidence": 0.0 to 1.0\n'
        "}\n\n"
```
with:
```python
        "Return ONLY valid JSON, no explanation, no markdown:\n"
        "{\n"
        '  "is_incident": true or false,\n'
        f'  "category": "{pipe_cats}",\n'
        '  "priority": "low|medium|high|urgent",\n'
        '  "confidence": 0.0 to 1.0\n'
        "}\n\n"
```

Replace lines 65–73 (inside `classify_message`):
```python
            raw_category = str(parsed.get("category", "other")).lower()
            raw_severity = str(parsed.get("severity", "low")).lower()
            raw_confidence = float(parsed.get("confidence", 0.0))
            return {
                "is_incident": bool(parsed.get("is_incident", False)),
                "category": raw_category if raw_category in valid_set else "other",
                "severity": raw_severity if raw_severity in _VALID_SEVERITIES else "low",
                "confidence": max(0.0, min(1.0, raw_confidence)),
            }
```
with:
```python
            raw_category = str(parsed.get("category", "other")).lower()
            raw_priority = str(parsed.get("priority", "medium")).lower()
            raw_confidence = float(parsed.get("confidence", 0.0))
            return {
                "is_incident": bool(parsed.get("is_incident", False)),
                "category": raw_category if raw_category in valid_set else "other",
                "priority": raw_priority if raw_priority in _VALID_PRIORITIES else "medium",
                "confidence": max(0.0, min(1.0, raw_confidence)),
            }
```

Note the fallback for an unrecognized priority value changes from `"low"` to `"medium"` per this plan's test — a genuinely unrecognized AI response is treated as medium urgency rather than assumed low, which is the safer default.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_classifier.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/classifier.py backend/tests/test_classifier.py
git commit -m "feat: classifier assigns 4-level priority (low/medium/high/urgent) instead of severity"
```

---

### Task 3: Rename `severity` → `priority` across the rest of the backend (ingest, summaries, odoo stub) and their tests

**Files:**
- Modify: `backend/main.py` (lines ~446, 477–490, 545, 791, 890, 948)
- Modify: `backend/summaries.py`
- Modify: `backend/templates/summaries.html` (data keys only, not CSS class names)
- Modify: `backend/odoo_stub.py`
- Modify: `backend/tests/test_ingest.py`, `test_updates.py`, `test_reply.py`, `test_audit.py`, `test_dm_routing.py`, `test_groups.py`, `test_write_scope.py`, `test_api.py`, `test_db.py`, `test_summaries.py`, `test_dashboard.py`, `test_super_admin_categories.py`

**Interfaces:**
- Consumes: `classify_message()` now returns `{"priority": ...}` (Task 2).
- Produces: `GET /incidents` and `GET /incidents/{id}` responses now have a `"priority"` key instead of `"severity"`. `Incident(...)` constructor now takes `priority=` instead of `severity=`.

This task is a mechanical rename with no behavior change except adding an `urgent` bucket to the daily-summary backlog counts (previously only high/medium/low existed, so a ticket classified `urgent` would otherwise silently vanish from the backlog breakdown).

- [ ] **Step 1: Update `backend/main.py`**

Replace line 445–446:
```python
        category=classification["category"],
        severity=classification["severity"],
```
with:
```python
        category=classification["category"],
        priority=classification["priority"],
```

Replace lines 476–488:
```python
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
```
with:
```python
    logger.info(
        "[INCIDENT] property=%s category=%s priority=%s confidence=%.2f",
        group_name,
        classification["category"],
        classification["priority"],
        classification["confidence"],
    )
    return {
        "status": "staged",
        "property": group_name,
        "category": classification["category"],
        "priority": classification["priority"],
    }
```

Replace lines 544–545:
```python
                    category=classification["category"],
                    severity=classification["severity"],
```
with:
```python
                    category=classification["category"],
                    priority=classification["priority"],
```

Replace line 791:
```python
            "severity": i.severity,
```
with:
```python
            "priority": i.priority,
```

Replace line 890:
```python
        "severity": incident.severity,
```
with:
```python
        "priority": incident.priority,
```

Replace line 948 (in `relink_update`'s promote-to-standalone-incident branch):
```python
            severity="low",
```
with:
```python
            priority="low",
```

- [ ] **Step 2: Update `backend/summaries.py`**

Replace lines 70–83:
```python
        "new_incidents": [
            {
                "id": i.id,
                "title": i.message_body[:80],
                "severity": i.severity,
                "status": i.status,
            }
            for i in new_incidents
        ],
        "open_backlog": {
            "high": sum(1 for i in open_incidents if i.severity == "high"),
            "medium": sum(1 for i in open_incidents if i.severity == "medium"),
            "low": sum(1 for i in open_incidents if i.severity == "low"),
        },
    }
```
with:
```python
        "new_incidents": [
            {
                "id": i.id,
                "title": i.message_body[:80],
                "priority": i.priority,
                "status": i.status,
            }
            for i in new_incidents
        ],
        "open_backlog": {
            "urgent": sum(1 for i in open_incidents if i.priority == "urgent"),
            "high": sum(1 for i in open_incidents if i.priority == "high"),
            "medium": sum(1 for i in open_incidents if i.priority == "medium"),
            "low": sum(1 for i in open_incidents if i.priority == "low"),
        },
    }
```

Replace line 95:
```python
        sev_emoji = {"high": "🔴", "medium": "🟡", "low": "⚪"}.get(inc["severity"], "⚪")
```
with:
```python
        sev_emoji = {"urgent": "🟣", "high": "🔴", "medium": "🟡", "low": "⚪"}.get(inc["priority"], "⚪")
```

Replace line 102:
```python
        f"  {backlog['high']} high · {backlog['medium']} medium · {backlog['low']} low",
```
with:
```python
        f"  {backlog.get('urgent', 0)} urgent · {backlog['high']} high · {backlog['medium']} medium · {backlog['low']} low",
```

`backlog.get('urgent', 0)` (not `backlog['urgent']`) is deliberate: `backlog` is always a fully-populated dict when it comes from `build_summary()`, but existing tests in `test_summaries.py` (lines 177, 189, 206 — untouched by this plan) call `format_whatsapp_summary()` directly with hand-built 3-key dicts that predate the `urgent` bucket. Using `.get()` keeps those tests passing without modification, consistent with how `templates/summaries.html` already reads these keys defensively (`backlog.high || 0`).

- [ ] **Step 3: Update `backend/templates/summaries.html`**

The `.severity-section` / `.severity-label` / `.sev-chip` CSS class names are cosmetic and stay as-is (they don't reference the renamed field). Only the data keys change.

Replace lines 453–456:
```javascript
    const backlog = s.open_backlog || {};
    const high = backlog.high || 0;
    const medium = backlog.medium || 0;
    const low = backlog.low || 0;
```
with:
```javascript
    const backlog = s.open_backlog || {};
    const urgent = backlog.urgent || 0;
    const high = backlog.high || 0;
    const medium = backlog.medium || 0;
    const low = backlog.low || 0;
```

Replace lines 474–477:
```html
            <div class="severity-bars">
              <span class="sev-chip high"><span class="dot" style="background:#ef4444"></span>High: ${esc(high)}</span>
              <span class="sev-chip medium"><span class="dot" style="background:#f59e0b"></span>Medium: ${esc(medium)}</span>
              <span class="sev-chip low"><span class="dot" style="background:#22c55e"></span>Low: ${esc(low)}</span>
            </div>
```
with:
```html
            <div class="severity-bars">
              <span class="sev-chip urgent"><span class="dot" style="background:#e11d48"></span>Urgent: ${esc(urgent)}</span>
              <span class="sev-chip high"><span class="dot" style="background:#ef4444"></span>High: ${esc(high)}</span>
              <span class="sev-chip medium"><span class="dot" style="background:#f59e0b"></span>Medium: ${esc(medium)}</span>
              <span class="sev-chip low"><span class="dot" style="background:#22c55e"></span>Low: ${esc(low)}</span>
            </div>
```

- [ ] **Step 4: Update `backend/odoo_stub.py`**

Replace:
```python
        "TODO: push to Odoo — id=%s property=%s category=%s severity=%s",
```
with:
```python
        "TODO: push to Odoo — id=%s property=%s category=%s priority=%s",
```

Replace:
```python
        incident.severity,
```
with:
```python
        incident.priority,
```

- [ ] **Step 5: Update every test file's `severity` references**

In each of `backend/tests/test_ingest.py`, `test_updates.py`, `test_reply.py`, `test_audit.py`, `test_dm_routing.py`, `test_groups.py`, `test_write_scope.py`, `test_api.py`, `test_dashboard.py`, `test_super_admin_categories.py`, apply this mechanical substitution:

- `"severity":` → `"priority":` (mocked classification dicts and JSON response assertions, e.g. `body["severity"]` → `body["priority"]`)
- `severity=` → `priority=` (keyword arg when constructing `Incident(...)`)

Run this to apply it across those ten files, then review the diff:

```bash
cd backend
for f in tests/test_ingest.py tests/test_updates.py tests/test_reply.py tests/test_audit.py \
         tests/test_dm_routing.py tests/test_groups.py tests/test_write_scope.py \
         tests/test_api.py tests/test_dashboard.py tests/test_super_admin_categories.py; do
  sed -i 's/"severity":/"priority":/g; s/severity=/priority=/g; s/\["severity"\]/["priority"]/g' "$f"
done
git diff --stat tests/
```

Manually review each changed file's diff (`git diff tests/test_ingest.py` etc.) to confirm no unintended matches.

`test_db.py` and `test_summaries.py` are deliberately **excluded** from the sed loop above and fixed by hand instead:

- `test_db.py` uses `"severity"` as a bare string inside a set literal (no trailing `:` or `=`), which the sed patterns don't match.
- `test_summaries.py`'s `_add_incident` helper has a line `severity=severity,` where the sed pattern `severity=` would only rewrite the *first* occurrence (the keyword name) to `priority=severity,`, leaving a stale reference to the old parameter name `severity` that no longer exists once the signature is renamed — running sed here would silently produce broken code.

In `backend/tests/test_db.py`, replace lines 15–21:
```python
async def test_incident_model_columns():
    cols = {c.name for c in Incident.__table__.columns}
    assert cols == {
        "id", "group_id", "property_name", "reporter_name", "reporter_phone",
        "message_body", "category", "severity", "confidence", "status", "received_at",
        "message_id", "updated_at",
    }
```
with:
```python
async def test_incident_model_columns():
    cols = {c.name for c in Incident.__table__.columns}
    assert cols == {
        "id", "group_id", "property_name", "reporter_name", "reporter_phone",
        "message_body", "category", "priority", "confidence", "status", "received_at",
        "message_id", "updated_at", "end_date", "escalated",
    }
```

In `backend/tests/test_summaries.py`, replace lines 77–90 (the `_add_incident` helper — note its call sites all pass the priority value positionally, e.g. `_add_incident(db, gid, "Pump leaking badly", "high", "review", ...)`, so no call site needs to change):
```python
async def _add_incident(db, group_id, message_body, severity, status, received_at):
    inc = Incident(
        group_id=group_id,
        property_name="Test",
        message_body=message_body,
        category="plumbing",
        severity=severity,
        confidence=0.9,
        status=status,
        received_at=received_at,
    )
    db.add(inc)
    await db.flush()
    return inc
```
with:
```python
async def _add_incident(db, group_id, message_body, priority, status, received_at):
    inc = Incident(
        group_id=group_id,
        property_name="Test",
        message_body=message_body,
        category="plumbing",
        priority=priority,
        confidence=0.9,
        status=status,
        received_at=received_at,
    )
    db.add(inc)
    await db.flush()
    return inc
```

Also in `test_summaries.py`, replace line 176:
```python
        "new_incidents": [{"id": 1, "title": "x", "severity": "high", "status": "review"}],
```
with:
```python
        "new_incidents": [{"id": 1, "title": "x", "priority": "high", "status": "review"}],
```

and replace line 204:
```python
            {"id": 1, "title": "Pump leaking", "severity": "high", "status": "review"},
```
with:
```python
            {"id": 1, "title": "Pump leaking", "priority": "high", "status": "review"},
```

The `open_backlog` dicts in `test_summaries.py` (lines 177, 189, 206) intentionally stay as 3-key `{"high": ..., "medium": ..., "low": ...}` — they exercise `format_whatsapp_summary()` directly and Task 3 Step 2's `.get('urgent', 0)` change means they don't need an `"urgent"` key to keep passing.

- [ ] **Step 6: Run the full backend test suite**

Run: `cd backend && python -m pytest -v`
Expected: PASS — all tests green. If any test still references `.severity` or `"severity"` (e.g. missed by the sed pass), fix it directly based on the failure message.

- [ ] **Step 7: Commit**

```bash
git add backend/main.py backend/summaries.py backend/templates/summaries.html backend/odoo_stub.py backend/tests/
git commit -m "refactor: rename severity to priority across ingest, summaries, and odoo stub"
```

---

### Task 4: New `PATCH /incidents/{incident_id}` endpoint

**Files:**
- Modify: `backend/main.py` (add pydantic body class near the other `*Body` classes at line ~107, add `_VALID_PRIORITIES` constant near `_VALID_STATUSES` at line 36, add the endpoint near `update_incident_status` at line ~1043)
- Test: `backend/tests/test_ticket_detail_update.py` (new file)

**Interfaces:**
- Consumes: `Incident`, `IncidentCategory`, `AuditLog` models (existing), `require_admin` dependency (existing).
- Produces: `PATCH /incidents/{incident_id}` — admin-only, accepts partial JSON body `{priority?, category?, end_date?, escalated?}`, returns `{id, priority, category, end_date, escalated}`. Used by Task 6's UI.

- [ ] **Step 1: Write the failing tests**

Create `backend/tests/test_ticket_detail_update.py`:

```python
import os
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
os.environ.setdefault("GATEWAY_SECRET_TOKEN", "test-secret")
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-testing-only1")

from datetime import datetime, timezone

import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base, get_db
from main import app
from models import User, Incident, IncidentCategory
from auth import hash_password

_engine = create_async_engine(
    "sqlite+aiosqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_Session = async_sessionmaker(_engine, expire_on_commit=False)
_HASHED = hash_password("pass1234")


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


@pytest_asyncio.fixture
async def tdu_client():
    async def _override_get_db():
        async with _Session() as session:
            yield session

    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


async def _seed_incident(role="admin", username="ticketadmin"):
    now = datetime.now(timezone.utc)
    async with _Session() as session:
        session.add(User(username=username, hashed_password=_HASHED, created_at=now, role=role))
        session.add(IncidentCategory(slug="plumbing", label="Plumbing", is_protected=False, created_at=now))
        session.add(IncidentCategory(slug="electrical", label="Electrical", is_protected=False, created_at=now))
        incident = Incident(
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
        session.add(incident)
        await session.commit()
        await session.refresh(incident)
        return incident.id


async def test_patch_requires_login(tdu_client):
    incident_id = await _seed_incident()
    resp = await tdu_client.patch(f"/incidents/{incident_id}", json={"priority": "high"})
    assert resp.status_code == 302


async def test_patch_rejects_user_role(tdu_client):
    incident_id = await _seed_incident(role="user", username="planeuser")
    await tdu_client.post("/login", data={"username": "planeuser", "password": "pass1234"})
    resp = await tdu_client.patch(f"/incidents/{incident_id}", json={"priority": "high"})
    assert resp.status_code == 403


async def test_patch_updates_priority(tdu_client):
    incident_id = await _seed_incident()
    await tdu_client.post("/login", data={"username": "ticketadmin", "password": "pass1234"})
    resp = await tdu_client.patch(f"/incidents/{incident_id}", json={"priority": "urgent"})
    assert resp.status_code == 200
    assert resp.json()["priority"] == "urgent"


async def test_patch_rejects_invalid_priority(tdu_client):
    incident_id = await _seed_incident()
    await tdu_client.post("/login", data={"username": "ticketadmin", "password": "pass1234"})
    resp = await tdu_client.patch(f"/incidents/{incident_id}", json={"priority": "critical"})
    assert resp.status_code == 422


async def test_patch_updates_category(tdu_client):
    incident_id = await _seed_incident()
    await tdu_client.post("/login", data={"username": "ticketadmin", "password": "pass1234"})
    resp = await tdu_client.patch(f"/incidents/{incident_id}", json={"category": "electrical"})
    assert resp.status_code == 200
    assert resp.json()["category"] == "electrical"


async def test_patch_rejects_unknown_category(tdu_client):
    incident_id = await _seed_incident()
    await tdu_client.post("/login", data={"username": "ticketadmin", "password": "pass1234"})
    resp = await tdu_client.patch(f"/incidents/{incident_id}", json={"category": "nonexistent"})
    assert resp.status_code == 422


async def test_patch_sets_end_date(tdu_client):
    incident_id = await _seed_incident()
    await tdu_client.post("/login", data={"username": "ticketadmin", "password": "pass1234"})
    resp = await tdu_client.patch(f"/incidents/{incident_id}", json={"end_date": "2026-08-01T00:00:00"})
    assert resp.status_code == 200
    assert resp.json()["end_date"].startswith("2026-08-01")


async def test_patch_clears_end_date(tdu_client):
    incident_id = await _seed_incident()
    await tdu_client.post("/login", data={"username": "ticketadmin", "password": "pass1234"})
    await tdu_client.patch(f"/incidents/{incident_id}", json={"end_date": "2026-08-01T00:00:00"})
    resp = await tdu_client.patch(f"/incidents/{incident_id}", json={"end_date": None})
    assert resp.status_code == 200
    assert resp.json()["end_date"] is None


async def test_patch_toggles_escalated(tdu_client):
    incident_id = await _seed_incident()
    await tdu_client.post("/login", data={"username": "ticketadmin", "password": "pass1234"})
    resp = await tdu_client.patch(f"/incidents/{incident_id}", json={"escalated": True})
    assert resp.status_code == 200
    assert resp.json()["escalated"] is True


async def test_patch_rejects_empty_body(tdu_client):
    incident_id = await _seed_incident()
    await tdu_client.post("/login", data={"username": "ticketadmin", "password": "pass1234"})
    resp = await tdu_client.patch(f"/incidents/{incident_id}", json={})
    assert resp.status_code == 422


async def test_patch_404_for_missing_incident(tdu_client):
    await _seed_incident()
    await tdu_client.post("/login", data={"username": "ticketadmin", "password": "pass1234"})
    resp = await tdu_client.patch("/incidents/999999", json={"priority": "high"})
    assert resp.status_code == 404


async def test_patch_writes_audit_log_per_field(tdu_client):
    from sqlalchemy import select
    from models import AuditLog
    incident_id = await _seed_incident()
    await tdu_client.post("/login", data={"username": "ticketadmin", "password": "pass1234"})
    resp = await tdu_client.patch(
        f"/incidents/{incident_id}",
        json={"priority": "urgent", "escalated": True},
    )
    assert resp.status_code == 200
    async with _Session() as session:
        result = await session.execute(
            select(AuditLog).where(AuditLog.incident_id == incident_id)
        )
        rows = result.scalars().all()
    actions = [r.action for r in rows]
    assert actions.count("ticket_detail_update") == 2
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_ticket_detail_update.py -v`
Expected: FAIL with 404 (route doesn't exist yet) on every test.

- [ ] **Step 3: Add the `_VALID_PRIORITIES` constant and request body class**

In `backend/main.py`, change line 36:
```python
_VALID_STATUSES = {"new", "review", "acknowledged", "resolved", "ignored"}
```
to:
```python
_VALID_STATUSES = {"new", "review", "acknowledged", "resolved", "ignored"}
_VALID_PRIORITIES = {"low", "medium", "high", "urgent"}
```

After the `DeleteCategoryBody` class (after line 108), add:

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

- [ ] **Step 4: Add the endpoint**

In `backend/main.py`, immediately after the `update_incident_status` function (after line 1042, before the blank lines preceding `@app.post("/incidents/{incident_id}/reply")`), add:

```python
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

    if "escalated" in fields_set:
        if body.escalated is None:
            raise HTTPException(status_code=422, detail="escalated must be a boolean")
        if body.escalated != incident.escalated:
            changes.append(f"escalated: {incident.escalated} → {body.escalated}")
            incident.escalated = body.escalated

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
        "end_date": incident.end_date.isoformat() if incident.end_date else None,
        "escalated": incident.escalated,
    }
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_ticket_detail_update.py -v`
Expected: PASS — all 12 tests.

- [ ] **Step 6: Run the full suite to check for regressions**

Run: `cd backend && python -m pytest -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add backend/main.py backend/tests/test_ticket_detail_update.py
git commit -m "feat: add admin-only PATCH /incidents/{id} for priority, category, end_date, escalated"
```

---

### Task 5: Dashboard rename — `sev` → `priority` in filters/stats/CSS, add "Urgent" level

**Files:**
- Modify: `backend/main.py` (dashboard route context, lines ~1476–1490 and ~1529–1543)
- Modify: `backend/templates/dashboard.html`

**Interfaces:**
- Consumes: `/incidents` and `/incidents/{id}` now return `priority` (Task 3).
- Produces: `categories_json` template variable (list of `{slug, label}` dicts) available to `dashboard.html`'s script block, for Task 6's category dropdown.

This task only renames the existing filter/stat/badge plumbing and adds the new "Urgent" bucket. It does not touch the detail modal — that's Task 6.

- [ ] **Step 1: Add `categories_json` to both dashboard routes in `backend/main.py`**

In the `/` route (around line 1476–1488), after:
```python
    cats_result = await db.execute(select(IncidentCategory).order_by(IncidentCategory.label))
    categories = cats_result.scalars().all()
```
add:
```python
    categories_json = [{"slug": c.slug, "label": c.label} for c in categories]
```
and add `"categories_json": categories_json,` to the `TemplateResponse` context dict (alongside the existing `"categories": categories,` line).

Do the same in the `/archive` route (around line 1529–1541): same two additions.

- [ ] **Step 2: CSS — add urgent variants**

In `backend/templates/dashboard.html`, after line 21 (`--red-soft: rgba(239, 68, 68, 0.14);`), add:
```css
      --urgent: #e11d48;
      --urgent-soft: rgba(225, 29, 72, 0.18);
```

Replace line 588–590:
```css
    .card[data-sev="high"]::before { background: var(--red); }
    .card[data-sev="medium"]::before { background: var(--amber); }
    .card[data-sev="low"]::before { background: var(--green); }
```
with:
```css
    .card[data-priority="urgent"]::before { background: var(--urgent); }
    .card[data-priority="high"]::before { background: var(--red); }
    .card[data-priority="medium"]::before { background: var(--amber); }
    .card[data-priority="low"]::before { background: var(--green); }
```

Replace line 787 (`.badge-high { background: var(--red-soft); color: #fecaca; }`) — keep it, but add a new line directly above it:
```css
    .badge-urgent { background: var(--urgent-soft); color: #fecdd3; }
    .badge-high { background: var(--red-soft); color: #fecaca; }
```

- [ ] **Step 3: Sidebar filter — rename group, add Urgent chip**

Replace lines 1214–1224:
```html
        <div class="filter-group collapsible" data-group="severity">
          <button type="button" class="filter-header" onclick="toggleFilterGroup(this)">
            <h4>Severity</h4>
            <div class="filter-header-meta"><span class="cnt" id="selected-sev-count">0</span><span class="filter-chevron">⌄</span></div>
          </button>
          <div class="filter-content">
            <div class="fopt" data-filter="sev" data-val="high" onclick="toggleFilter(this)"><span class="left"><span class="dot" style="background:#ef4444"></span>High Priority</span><span class="cnt" data-cnt-sev="high">0</span></div>
            <div class="fopt" data-filter="sev" data-val="medium" onclick="toggleFilter(this)"><span class="left"><span class="dot" style="background:#f59e0b"></span>Medium</span><span class="cnt" data-cnt-sev="medium">0</span></div>
            <div class="fopt" data-filter="sev" data-val="low" onclick="toggleFilter(this)"><span class="left"><span class="dot" style="background:#22c55e"></span>Low</span><span class="cnt" data-cnt-sev="low">0</span></div>
          </div>
        </div>
```
with:
```html
        <div class="filter-group collapsible" data-group="priority">
          <button type="button" class="filter-header" onclick="toggleFilterGroup(this)">
            <h4>Priority</h4>
            <div class="filter-header-meta"><span class="cnt" id="selected-priority-count">0</span><span class="filter-chevron">⌄</span></div>
          </button>
          <div class="filter-content">
            <div class="fopt" data-filter="priority" data-val="urgent" onclick="toggleFilter(this)"><span class="left"><span class="dot" style="background:#e11d48"></span>Urgent</span><span class="cnt" data-cnt-priority="urgent">0</span></div>
            <div class="fopt" data-filter="priority" data-val="high" onclick="toggleFilter(this)"><span class="left"><span class="dot" style="background:#ef4444"></span>High</span><span class="cnt" data-cnt-priority="high">0</span></div>
            <div class="fopt" data-filter="priority" data-val="medium" onclick="toggleFilter(this)"><span class="left"><span class="dot" style="background:#f59e0b"></span>Medium</span><span class="cnt" data-cnt-priority="medium">0</span></div>
            <div class="fopt" data-filter="priority" data-val="low" onclick="toggleFilter(this)"><span class="left"><span class="dot" style="background:#22c55e"></span>Low</span><span class="cnt" data-cnt-priority="low">0</span></div>
          </div>
        </div>
```

- [ ] **Step 4: Jinja-rendered card — rename `data-sev`/`i.severity`**

Replace lines 1281–1287:
```html
          <article class="card"
               data-id="{{ i.id }}"
               data-sev="{{ i.severity }}"
               data-status="{{ i.status }}"
               data-cat="{{ i.category }}"
               data-updates="{{ update_count }}"
               data-media="{{ media_count }}">
```
with:
```html
          <article class="card"
               data-id="{{ i.id }}"
               data-priority="{{ i.priority }}"
               data-status="{{ i.status }}"
               data-cat="{{ i.category }}"
               data-updates="{{ update_count }}"
               data-media="{{ media_count }}">
```

Replace line 1312:
```html
                <span class="badge badge-{{ i.severity }}">{{ i.severity }}</span>
```
with:
```html
                <span class="badge badge-{{ i.priority }}">{{ i.priority }}</span>
```

- [ ] **Step 5: JS — rename `sev`/`severity` identifiers**

Replace line 1396:
```javascript
let activeFilters = { status: new Set(), sev: new Set(), cat: new Set() };
```
with:
```javascript
let activeFilters = { status: new Set(), priority: new Set(), cat: new Set() };
```

Replace line 1617 (`extractIncidentFromCard`):
```javascript
    severity: card.dataset.sev,
```
with:
```javascript
    priority: card.dataset.priority,
```

Replace lines 1648–1652 (`poll()`'s title-bar high-priority counter):
```javascript
      const highOpen = [...allIncidents, ...pendingNew].filter(
        i => i.severity === 'high' && !['resolved','ignored'].includes(i.status)
      ).length;
      document.title = highOpen > 0 ? `(${highOpen} HIGH) {{ title }}` : '{{ title }}';
```
with:
```javascript
      const highOpen = [...allIncidents, ...pendingNew].filter(
        i => i.priority === 'high' && !['resolved','ignored'].includes(i.status)
      ).length;
      document.title = highOpen > 0 ? `(${highOpen} HIGH) {{ title }}` : '{{ title }}';
```

Replace line 1680 (`normalizeIncident`):
```javascript
    severity: i.severity || 'low',
```
with:
```javascript
    priority: i.priority || 'medium',
```

Replace line 1699 (`buildCard`):
```javascript
<article class="card" data-id="${i.id}" data-sev="${esc(i.severity)}" data-status="${esc(i.status)}" data-cat="${esc(i.category)}">
```
with:
```javascript
<article class="card" data-id="${i.id}" data-priority="${esc(i.priority)}" data-status="${esc(i.status)}" data-cat="${esc(i.category)}">
```

Replace line 1713 (`buildCard`'s badge):
```javascript
      <span class="badge badge-${esc(i.severity)}">${esc(i.severity)}</span>
```
with:
```javascript
      <span class="badge badge-${esc(i.priority)}">${esc(i.priority)}</span>
```

Replace lines 1841–1847 (`updateSelectedFilterCounts`):
```javascript
  const statusCount = document.getElementById('selected-status-count');
  const sevCount = document.getElementById('selected-sev-count');
  const catCount = document.getElementById('selected-cat-count');
  if (statusCount) statusCount.textContent = activeFilters.status.size;
  if (sevCount) sevCount.textContent = activeFilters.sev.size;
  if (catCount) catCount.textContent = activeFilters.cat.size;
```
with:
```javascript
  const statusCount = document.getElementById('selected-status-count');
  const priorityCount = document.getElementById('selected-priority-count');
  const catCount = document.getElementById('selected-cat-count');
  if (statusCount) statusCount.textContent = activeFilters.status.size;
  if (priorityCount) priorityCount.textContent = activeFilters.priority.size;
  if (catCount) catCount.textContent = activeFilters.cat.size;
```

Replace line 1857 (`clearFilters`):
```javascript
  activeFilters = { status: new Set(), sev: new Set(), cat: new Set() };
```
with:
```javascript
  activeFilters = { status: new Set(), priority: new Set(), cat: new Set() };
```

Replace lines 1869–1879 (`applyFilters`):
```javascript
    const matchStatus = activeFilters.status.size === 0 || activeFilters.status.has(card.dataset.status);
    const matchSev = activeFilters.sev.size === 0 || activeFilters.sev.has(card.dataset.sev);
    const matchCat = activeFilters.cat.size === 0 || activeFilters.cat.has(card.dataset.cat);
    const searchable = [
      card.querySelector('.title')?.textContent,
      card.querySelector('.message')?.textContent,
      card.querySelector('.meta')?.textContent,
      card.dataset.status, card.dataset.sev, card.dataset.cat
    ].join(' ').toLowerCase();
    const matchSearch = !q || searchable.includes(q);
    const shouldShow = matchStatus && matchSev && matchCat && matchSearch;
```
with:
```javascript
    const matchStatus = activeFilters.status.size === 0 || activeFilters.status.has(card.dataset.status);
    const matchPriority = activeFilters.priority.size === 0 || activeFilters.priority.has(card.dataset.priority);
    const matchCat = activeFilters.cat.size === 0 || activeFilters.cat.has(card.dataset.cat);
    const searchable = [
      card.querySelector('.title')?.textContent,
      card.querySelector('.message')?.textContent,
      card.querySelector('.meta')?.textContent,
      card.dataset.status, card.dataset.priority, card.dataset.cat
    ].join(' ').toLowerCase();
    const matchSearch = !q || searchable.includes(q);
    const shouldShow = matchStatus && matchPriority && matchCat && matchSearch;
```

Replace line 1899 (`updateStats`):
```javascript
  const highOpen = allIncidents.filter(i => i.severity === 'high' && !['resolved','ignored'].includes(i.status)).length;
```
with:
```javascript
  const highOpen = allIncidents.filter(i => i.priority === 'high' && !['resolved','ignored'].includes(i.status)).length;
```

Replace lines 1911–1914 (`updateCounts`):
```javascript
  ['high','medium','low'].forEach(s => {
    const el = document.querySelector(`[data-cnt-sev="${s}"]`);
    if (el) el.textContent = allIncidents.filter(i => i.severity === s).length;
  });
```
with:
```javascript
  ['urgent','high','medium','low'].forEach(s => {
    const el = document.querySelector(`[data-cnt-priority="${s}"]`);
    if (el) el.textContent = allIncidents.filter(i => i.priority === s).length;
  });
```

- [ ] **Step 6: Manual verification (no automated test for template rendering)**

Run: `cd backend && python -m pytest -v` (confirms nothing broke — this task is HTML/JS/CSS only, no Python test coverage for template rendering itself)
Then start the dev server (see Task 7 for the full manual verification pass — the browser check happens once after Task 6 too, so it's fine to defer a live check to that step if preferred, but at minimum confirm the app still boots):

Run: `cd backend && python -c "import main"`
Expected: no import errors.

- [ ] **Step 7: Commit**

```bash
git add backend/main.py backend/templates/dashboard.html
git commit -m "refactor: rename severity filter/stat/badge plumbing to priority, add urgent level"
```

---

### Task 6: Detail modal — editable "Ticket details" section

**Files:**
- Modify: `backend/templates/dashboard.html`

**Interfaces:**
- Consumes: `PATCH /incidents/{incident_id}` (Task 4), `GET /incidents/{incident_id}` response now includes `priority`, `end_date`, `escalated` (already true after Task 3/1 — the endpoint returns whatever's on the ORM object, no endpoint change needed since it does `"priority": incident.priority,` already from Task 3's rename — but `end_date`/`escalated` still need adding to the detail response).
- Produces: `updateTicketField(incidentId, field, value)` JS function.

- [ ] **Step 1: Add `end_date`/`escalated` to the `GET /incidents/{incident_id}` response**

In `backend/main.py`, in `get_incident_detail` (around line 884–900), replace:
```python
    return {
        "id": incident.id,
        "property_name": incident.property_name,
        "reporter_name": incident.reporter_name,
        "reporter_phone": incident.reporter_phone,
        "category": incident.category,
        "priority": incident.priority,
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

(Note: this response already carries `"priority"` because Task 3 Step 1 renamed line 890's key. If Tasks are executed strictly in order this diff will apply cleanly.)

Add a matching test to `backend/tests/test_ticket_detail_update.py` (append):
```python
async def test_get_incident_detail_includes_new_fields(tdu_client):
    incident_id = await _seed_incident()
    await tdu_client.post("/login", data={"username": "ticketadmin", "password": "pass1234"})
    await tdu_client.patch(f"/incidents/{incident_id}", json={"end_date": "2026-08-01T00:00:00", "escalated": True})
    resp = await tdu_client.get(f"/incidents/{incident_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["end_date"].startswith("2026-08-01")
    assert data["escalated"] is True
```

Run: `cd backend && python -m pytest tests/test_ticket_detail_update.py -v` — expect PASS (this exercises code already written in Task 4; the assertion just confirms the GET endpoint surfaces it).

- [ ] **Step 2: Add `CATEGORIES` and `CURRENT_ROLE` JS constants**

In `backend/templates/dashboard.html`, replace line 1390:
```javascript
const CURRENT_USER = {{ username | tojson }};
```
with:
```javascript
const CURRENT_USER = {{ username | tojson }};
const CURRENT_ROLE = {{ role | tojson }};
const CATEGORIES = {{ categories_json | tojson }};
```

- [ ] **Step 3: Add CSS for the ticket-details section and escalate button**

After the `.modal-section-label` rule (find it around line 914 — insert immediately after its closing brace), add:
```css
    .ticket-details-grid {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 12px;
      align-items: end;
    }
    .ticket-details-grid label {
      display: flex;
      flex-direction: column;
      gap: 6px;
      font-size: 12px;
      color: var(--muted);
    }
    .ticket-details-grid select,
    .ticket-details-grid input[type="date"] {
      background: var(--surface-2);
      border: 1px solid var(--line);
      border-radius: 10px;
      color: var(--text);
      padding: 8px 10px;
      font-size: 13px;
    }
    .ticket-details-readonly {
      display: grid;
      grid-template-columns: 1fr 1fr;
      gap: 10px;
      font-size: 13px;
    }
    .ticket-details-readonly .label {
      color: var(--muted);
      font-size: 11px;
      text-transform: uppercase;
      letter-spacing: 0.06em;
      display: block;
    }
    .btn-escalate { background: linear-gradient(135deg, #dc2626, #991b1b); }
    .btn-unescalate { background: linear-gradient(135deg, #334155, #1f2937); }
```

- [ ] **Step 4: Add the "Ticket details" section to `renderDetailModal`**

In `renderDetailModal(detail)`, the function currently `return`s a template literal starting with `<div><div class="modal-section-label">Original report</div>...`. Insert a new section *before* that "Original report" `<div>`. Find:
```javascript
  return `
    <div>
      <div class="modal-section-label">Original report</div>
      <div class="message-box"><div class="message">${esc(detail.message_body)}</div></div>
    </div>
```
and replace with:
```javascript
  return `
    <div>
      <div class="modal-section-label">Ticket details</div>
      ${renderTicketDetailsSection(detail)}
    </div>
    <div>
      <div class="modal-section-label">Original report</div>
      <div class="message-box"><div class="message">${esc(detail.message_body)}</div></div>
    </div>
```

Immediately before the `renderDetailModal` function definition (find `function renderDetailModal(detail) {` and insert above it), add:
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

- [ ] **Step 5: Add `updateTicketField` JS function**

Immediately after the `sendReplyFromBar` function definition (find where it ends — it's followed by `async function relinkUpdate` per the earlier read; insert `updateTicketField` right before `async function relinkUpdate`), add:
```javascript
async function updateTicketField(incidentId, field, value) {
  try {
    const r = await fetch(`/incidents/${incidentId}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ [field]: value }),
    });
    if (!r.ok) throw new Error('Update failed');
    showToast(`Ticket #${incidentId} updated`);
    if (_currentDetailId === incidentId) {
      await openDetailModal(incidentId);
    }
  } catch (e) {
    showToast('Could not update ticket. Please try again.');
  }
}
```

- [ ] **Step 6: Manual verification**

Run: `cd backend && python -c "import main"` to confirm no import errors, then run the full test suite:

Run: `cd backend && python -m pytest -v`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add backend/main.py backend/templates/dashboard.html backend/tests/test_ticket_detail_update.py
git commit -m "feat: editable priority/category/end date/escalation section in ticket detail modal"
```

---

### Task 7: Full browser verification

**Files:** none (verification only)

- [ ] **Step 1: Start the backend dev server**

Run: `cd backend && python -m pytest -v` one final time to confirm a clean baseline, then start the app locally (a `backend/.env` already exists in this repo with the required env vars):

Run: `cd backend && uvicorn main:app --reload --port 8000`
Expected: server starts, log line `Application startup complete.`

- [ ] **Step 2: Manually verify in a browser**

1. Log in as an `admin` user. Open the dashboard, click a ticket to open the detail modal.
2. Confirm a "Ticket details" section appears above "Original report" with Priority/Category selects, an End date picker, and an Escalate button.
3. Change priority to "Urgent" — confirm a toast appears, the modal reloads, and the new value is selected.
4. Change category — confirm it persists on modal reload.
5. Set an end date, reload the modal (close and reopen), confirm the date persisted. Clear it (set the date input back to empty and trigger change) — confirm it clears.
6. Click "Escalate" — confirm the button flips to "✗ Un-escalate" and is now the neutral color.
7. Log out, log in as a `user`-role account scoped to the same group. Open the same ticket — confirm the four fields render as **read-only text**, no selects/buttons.
8. Back in the main list view (not the modal), confirm the sidebar "Priority" filter group (previously "Severity") shows Urgent/High/Medium/Low chips with correct counts, and filtering by "Urgent" only shows urgent-priority cards.
9. Confirm the "High · Open" stat tile and page-title `(N HIGH)` badge still work (should reflect `priority === 'high'` tickets only, unchanged behavior).

- [ ] **Step 3: Report results to the user**

Summarize what was verified and any deviations observed, before considering Group A complete.
