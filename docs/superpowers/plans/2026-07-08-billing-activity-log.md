# Billing Admin Activity Log Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give the billing admin app (`billing/`) a persistent record of who changed a client's config, who clicked an action button, and what the scheduler fired automatically — so an incident like "a client's billing group was misconfigured and nobody knows who set it or when" is a lookup instead of an SSH/DB excavation.

**Architecture:** One new table, `ActivityLog` (`billing/models.py`), written via a tiny helper module `billing/activity_log.py` (`record_activity`, `diff_summary`, `recent_activity`). Every mutation handler in `billing/main.py` and every automated branch in `billing/scheduler.py` gets one `record_activity(...)` call added right before its existing `db.commit()`, so the log entry is always atomic with the change it describes. Two read-only UI sections surface it: a per-client "Activity" card on `client_detail.html`, and a "Price change history" table on `prices.html` for the two global (not client-scoped) actions.

**Tech Stack:** FastAPI, SQLAlchemy (async), SQLite (aiosqlite), Jinja2, pytest + pytest-asyncio + httpx `ASGITransport`.

**Spec:** `docs/superpowers/specs/2026-07-08-billing-activity-log-design.md`

## Global Constraints

- Run all commands from the `billing/` directory using the project's venv: `.venv/bin/python -m pytest ...` — a bare `python`/`pytest` is not on PATH in this environment.
- Every `db.add(ActivityLog(...))` call must be followed by a `db.commit()` already present (or newly added, see Task 3) in the same handler — never call `db.commit()` an extra time just for the log; ride the existing transaction.
- `openwa_api_key` is a credential — its value must never appear in a log `detail` string. Only ever log that it changed.
- If nothing tracked actually changed on a save, write no log entry (no-op saves must not create noise rows).
- Follow existing code style: no comments unless explaining a non-obvious "why"; keep diffs minimal and match surrounding formatting exactly.
- New table is picked up automatically by the existing `Base.metadata.create_all()` in `billing/database.py::init_db()` — no manual migration script needed.

---

## File Structure

- **Create** `billing/activity_log.py` — `record_activity()`, `diff_summary()`, `recent_activity()`. The single place that knows how to write/read `ActivityLog` rows.
- **Modify** `billing/models.py` — add `ActivityLog` table.
- **Modify** `billing/main.py` — import `activity_log` helpers; instrument `update_client`, `push_reminder`, `send_invite`, `manual_reactivate`, `close_client`, `admin_add_ticket_group`, `admin_remove_ticket_group`, `admin_reset_ticket_groups_unrestricted`, `set_prices`, `set_group_tier_prices`, `client_detail`, `prices_page`.
- **Modify** `billing/scheduler.py` — instrument all 6 automated-warning branches in `_check_client_status`.
- **Modify** `billing/templates/client_detail.html` — new "Activity" card.
- **Modify** `billing/templates/prices.html` — new "Price change history" table.
- **Create** `billing/tests/test_activity_log.py` — unit tests for the new helper module.
- **Modify** `billing/tests/test_clients.py` — add coverage for every instrumented `main.py` handler.
- **Modify** `billing/tests/test_scheduler_logic.py` — extend the 6 existing branch tests with log assertions.

---

### Task 1: Data model + `activity_log` helper module

**Files:**
- Modify: `billing/models.py` (append at end of file, after `GroupUpgradeRequest`, currently ending at line 142)
- Create: `billing/activity_log.py`
- Test: `billing/tests/test_activity_log.py`

**Interfaces:**
- Produces: `models.ActivityLog(id, client_id: int | None, actor: str, action: str, detail: str, created_at: datetime)`
- Produces: `activity_log.record_activity(db, client_id: int | None, actor: str, action: str, detail: str = "") -> None` (async — stages via `db.add`, does **not** commit)
- Produces: `activity_log.diff_summary(changes: list[tuple[str, object, object]]) -> str` (sync)
- Produces: `activity_log.recent_activity(db, client_id: int | None, limit: int = 50) -> list[ActivityLog]` (async)

- [ ] **Step 1: Write the failing tests**

Create `billing/tests/test_activity_log.py`:
```python
import pytest
from datetime import date, datetime, timezone
from models import Client


@pytest.mark.asyncio
async def test_record_activity_persists_row(db_session):
    from activity_log import record_activity
    from models import ActivityLog
    from sqlalchemy import select

    client = Client(
        name="Acme", subdomain="acme-activity", plan="monthly",
        renewal_date=date.today(), created_at=datetime.now(timezone.utc),
    )
    db_session.add(client)
    await db_session.commit()

    await record_activity(db_session, client.id, "admin", "push_reminder", "Sent to group@g.us")
    await db_session.commit()

    row = await db_session.scalar(select(ActivityLog).where(ActivityLog.client_id == client.id))
    assert row.actor == "admin"
    assert row.action == "push_reminder"
    assert row.detail == "Sent to group@g.us"
    assert row.created_at is not None


@pytest.mark.asyncio
async def test_record_activity_allows_null_client_id_for_global_events(db_session):
    from activity_log import record_activity
    from models import ActivityLog
    from sqlalchemy import select

    await record_activity(db_session, None, "admin", "set_plan_prices", "monthly: 1500 -> 1800")
    await db_session.commit()

    row = await db_session.scalar(select(ActivityLog).where(ActivityLog.action == "set_plan_prices"))
    assert row.client_id is None


def test_diff_summary_skips_unchanged_fields():
    from activity_log import diff_summary
    result = diff_summary([
        ("plan", "monthly", "monthly"),
        ("renewal_date", "2026-07-25", "2026-07-27"),
    ])
    assert result == "renewal_date: 2026-07-25 → 2026-07-27"


def test_diff_summary_returns_empty_string_when_nothing_changed():
    from activity_log import diff_summary
    assert diff_summary([("plan", "monthly", "monthly")]) == ""


def test_diff_summary_labels_unset_old_value():
    from activity_log import diff_summary
    result = diff_summary([("whatsapp_group_id", None, "120363@g.us")])
    assert result == "whatsapp_group_id: (unset) → 120363@g.us"


@pytest.mark.asyncio
async def test_recent_activity_orders_newest_first_and_respects_limit(db_session):
    from activity_log import record_activity, recent_activity

    client = Client(
        name="Acme", subdomain="acme-activity-order", plan="monthly",
        renewal_date=date.today(), created_at=datetime.now(timezone.utc),
    )
    db_session.add(client)
    await db_session.commit()

    for i in range(3):
        await record_activity(db_session, client.id, "admin", "push_reminder", f"send #{i}")
    await db_session.commit()

    rows = await recent_activity(db_session, client.id, limit=2)
    assert len(rows) == 2
    assert rows[0].detail == "send #2"
    assert rows[1].detail == "send #1"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_activity_log.py -v` (from `billing/`)
Expected: FAIL/ERROR on every test — `ModuleNotFoundError: No module named 'activity_log'` (and `ImportError: cannot import name 'ActivityLog' from 'models'` once the module exists but the class doesn't).

- [ ] **Step 3: Add the `ActivityLog` model**

Append to `billing/models.py` (after the `GroupUpgradeRequest` class, end of file):
```python
class ActivityLog(Base):
    """Records who did what: a human admin action (actor = username) or an
    automated one (actor = "system"). client_id is None for platform-wide
    events (e.g. global price changes) that don't belong to one client."""
    __tablename__ = "activity_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("clients.id"), nullable=True, index=True)
    actor: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(String(40), nullable=False)
    detail: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    def __init__(self, **kw):
        if "detail" not in kw:
            kw["detail"] = ""
        super().__init__(**kw)
```
No new imports needed — `Optional`, `Integer`, `ForeignKey`, `String`, `Text`, `DateTime`, `Mapped`, `mapped_column` are already imported at the top of `billing/models.py`.

- [ ] **Step 4: Create `billing/activity_log.py`**

```python
from datetime import datetime, timezone

from sqlalchemy import select

from models import ActivityLog


async def record_activity(db, client_id, actor: str, action: str, detail: str = "") -> None:
    db.add(ActivityLog(
        client_id=client_id, actor=actor, action=action, detail=detail,
        created_at=datetime.now(timezone.utc),
    ))


def diff_summary(changes) -> str:
    parts = []
    for name, old, new in changes:
        if old == new:
            continue
        old_repr = "(unset)" if old in (None, "") else old
        parts.append(f"{name}: {old_repr} → {new}")
    return "; ".join(parts)


async def recent_activity(db, client_id, limit: int = 50):
    result = await db.execute(
        select(ActivityLog)
        .where(ActivityLog.client_id == client_id)
        .order_by(ActivityLog.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_activity_log.py -v`
Expected: 5 passed.

- [ ] **Step 6: Commit**

```bash
git add billing/models.py billing/activity_log.py billing/tests/test_activity_log.py
git commit -m "feat: add ActivityLog model and record/diff/read helpers"
```

---

### Task 2: Instrument `update_client` with diff-based logging

**Files:**
- Modify: `billing/main.py:18` (import line), `billing/main.py:208-271` (`update_client`)
- Test: `billing/tests/test_clients.py`

**Interfaces:**
- Consumes: `activity_log.record_activity(db, client_id, actor, action, detail="")`, `activity_log.diff_summary(changes)` from Task 1.

- [ ] **Step 1: Write the failing tests**

Append to `billing/tests/test_clients.py` (after the last existing test):
```python
@pytest.mark.asyncio
async def test_update_client_logs_diff_of_changed_fields(auth_http, db_session):
    await auth_http.post("/clients", data={"name": "Acme", "subdomain": "acme-log-diff", "plan": "monthly"})
    from models import Client, ActivityLog
    from sqlalchemy import select
    client = await db_session.scalar(select(Client).where(Client.subdomain == "acme-log-diff"))

    r = await auth_http.post(f"/clients/{client.id}", data={
        "whatsapp_group_id": "billing@g.us",
        "renewal_date": "2026-08-01",
    })
    assert r.status_code == 303

    log = await db_session.scalar(select(ActivityLog).where(ActivityLog.client_id == client.id))
    assert log.actor == "admin"
    assert log.action == "update_client"
    assert "whatsapp_group_id: (unset) → billing@g.us" in log.detail
    assert "renewal_date:" in log.detail


@pytest.mark.asyncio
async def test_update_client_logs_nothing_when_no_fields_change(auth_http, db_session):
    await auth_http.post("/clients", data={"name": "Acme", "subdomain": "acme-log-noop", "plan": "monthly"})
    from models import Client, ActivityLog
    from sqlalchemy import select
    client = await db_session.scalar(select(Client).where(Client.subdomain == "acme-log-noop"))

    r = await auth_http.post(f"/clients/{client.id}", data={})
    assert r.status_code == 303

    log = await db_session.scalar(select(ActivityLog).where(ActivityLog.client_id == client.id))
    assert log is None


@pytest.mark.asyncio
async def test_update_client_redacts_api_key_in_log(auth_http, db_session):
    await auth_http.post("/clients", data={"name": "Acme", "subdomain": "acme-log-key", "plan": "monthly"})
    from models import Client, ActivityLog
    from sqlalchemy import select
    client = await db_session.scalar(select(Client).where(Client.subdomain == "acme-log-key"))

    r = await auth_http.post(f"/clients/{client.id}", data={"openwa_api_key": "super-secret-key"})
    assert r.status_code == 303

    log = await db_session.scalar(select(ActivityLog).where(ActivityLog.client_id == client.id))
    assert "openwa_api_key: changed" in log.detail
    assert "super-secret-key" not in log.detail
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_clients.py -k "update_client_logs or redacts_api_key" -v`
Expected: FAIL — `AttributeError: 'NoneType' object has no attribute 'actor'` (no log row exists yet) on the first and third test; the noop test currently passes vacuously (no code writes any log yet) but must still pass after Task 2 — re-run once Task 1 is confirmed working to be sure your baseline is right.

- [ ] **Step 3: Instrument `update_client`**

Change the import line `billing/main.py:18`:
```python
from activity_log import record_activity, diff_summary
```
(add this new line right after the existing `from auth import ...` import line)

Replace the `update_client` handler (`billing/main.py:208-271`) with:
```python
@app.post("/clients/{client_id}", response_class=HTMLResponse)
async def update_client(
    request: Request, client_id: int,
    whatsapp_group_id: str = Form(default=""),
    openwa_url: str = Form(default=""),
    openwa_session: str = Form(default=""),
    openwa_api_key: str = Form(default=""),
    docker_project: str = Form(default=""),
    renewal_date: str = Form(default=""),
    plan: str = Form(default=""),
    admin_whatsapp_phone: str = Form(default=""),
    whatsapp_invite_link: str = Form(default=""),
    backend_port: str = Form(default=""),
    data_retention_days: str = Form(default=""),
    username: str = Depends(require_login),
    db=Depends(get_db),
):
    client = await db.get(Client, client_id)
    if not client:
        return HTMLResponse("Not found", status_code=404)
    tracked_fields = [
        "whatsapp_group_id", "openwa_url", "openwa_session", "docker_project",
        "renewal_date", "plan", "data_retention_days",
        "admin_whatsapp_phone", "whatsapp_invite_link", "backend_port",
    ]
    before = {f: getattr(client, f) for f in tracked_fields}
    before_api_key = client.openwa_api_key

    whatsapp_group_id = whatsapp_group_id.strip()
    if whatsapp_group_id:
        ticket_groups = json.loads(client.allowed_ticket_groups) if client.allowed_ticket_groups else []
        if whatsapp_group_id in ticket_groups:
            payments = (await db.execute(
                select(Payment).where(Payment.client_id == client_id).order_by(Payment.initiated_at.desc())
            )).scalars().all()
            return templates.TemplateResponse(request, "client_detail.html", {
                "request": request, "client": client, "payments": payments, "username": username,
                "group_id_error": (
                    f"'{whatsapp_group_id}' is already registered as a ticket group for this client — "
                    "the billing group must be a separate group, or reminders will spam a support group."
                ),
            })
        client.whatsapp_group_id = whatsapp_group_id
    if openwa_url:
        client.openwa_url = openwa_url.strip()
    if openwa_session:
        client.openwa_session = openwa_session.strip()
    if openwa_api_key:
        client.openwa_api_key = openwa_api_key.strip()
    if docker_project:
        client.docker_project = docker_project.strip()
    if renewal_date:
        client.renewal_date = date.fromisoformat(renewal_date)
    if plan in ("monthly", "annual"):
        client.plan = plan
    if data_retention_days.strip().isdigit():
        val = int(data_retention_days.strip())
        if 1 <= val <= 365:
            client.data_retention_days = val
    client.admin_whatsapp_phone = admin_whatsapp_phone.strip() or client.admin_whatsapp_phone
    client.whatsapp_invite_link = whatsapp_invite_link.strip() or client.whatsapp_invite_link
    new_port = int(backend_port.strip()) if backend_port.strip().isdigit() else None
    port_changed = new_port and new_port != client.backend_port
    if new_port:
        client.backend_port = new_port

    changes = [(f, before[f], getattr(client, f)) for f in tracked_fields]
    detail = diff_summary(changes)
    if client.openwa_api_key != before_api_key:
        detail = (detail + "; " if detail else "") + "openwa_api_key: changed"
    if detail:
        await record_activity(db, client.id, username, "update_client", detail)

    await db.commit()
    if port_changed:
        import asyncio
        await asyncio.get_event_loop().run_in_executor(
            None, add_client_port, client.subdomain, new_port
        )
    return RedirectResponse(f"/clients/{client_id}", status_code=303)
```
(Only additions: the `tracked_fields`/`before`/`before_api_key` snapshot near the top, and the `changes`/`detail`/`record_activity` block right before `await db.commit()`. Nothing else in this handler's logic changes — note this is the same handler that already got the `whatsapp_group_id`-vs-ticket-group guard in the prior incident fix.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_clients.py -v`
Expected: all tests pass (full file, to catch any regression in the other `update_client`-adjacent tests like `test_update_client_openwa_config`).

- [ ] **Step 5: Commit**

```bash
git add billing/main.py billing/tests/test_clients.py
git commit -m "feat: log a diff on every client config update"
```

---

### Task 3: Instrument the six simple admin-action endpoints

**Files:**
- Modify: `billing/main.py` — `push_reminder` (~line 971), `send_invite` (~line 341), `manual_reactivate` (~line 988), `close_client` (~line 1009), `admin_add_ticket_group` (~line 274), `admin_remove_ticket_group` (~line 300), `admin_reset_ticket_groups_unrestricted` (~line 318)
- Test: `billing/tests/test_clients.py`

**Interfaces:**
- Consumes: `activity_log.record_activity` (Task 1).

- [ ] **Step 1: Write the failing tests**

Append to `billing/tests/test_clients.py`:
```python
@pytest.mark.asyncio
async def test_push_reminder_logs_activity(auth_http, db_session):
    await auth_http.post("/clients", data={"name": "Acme", "subdomain": "acme-log-remind", "plan": "monthly"})
    from models import Client, ActivityLog
    from sqlalchemy import select
    client = await db_session.scalar(select(Client).where(Client.subdomain == "acme-log-remind"))

    await auth_http.post(f"/clients/{client.id}/push-reminder")

    log = await db_session.scalar(select(ActivityLog).where(ActivityLog.client_id == client.id))
    assert log.actor == "admin"
    assert log.action == "push_reminder"


@pytest.mark.asyncio
async def test_send_invite_logs_activity(auth_http, db_session):
    await auth_http.post("/clients", data={"name": "Acme", "subdomain": "acme-log-invite", "plan": "monthly"})
    from models import Client, ActivityLog
    from sqlalchemy import select
    client = await db_session.scalar(select(Client).where(Client.subdomain == "acme-log-invite"))
    client.admin_whatsapp_phone = "0712345678"
    client.whatsapp_invite_link = "https://chat.whatsapp.com/test"
    await db_session.commit()

    r = await auth_http.post(f"/clients/{client.id}/send-invite")
    assert r.status_code == 303

    log = await db_session.scalar(select(ActivityLog).where(ActivityLog.client_id == client.id))
    assert log.action == "send_invite"
    assert "254712345678" in log.detail


@pytest.mark.asyncio
async def test_reactivate_logs_activity(auth_http, db_session):
    await auth_http.post("/clients", data={"name": "Acme", "subdomain": "acme-log-react", "plan": "monthly"})
    from models import Client, ActivityLog
    from sqlalchemy import select
    client = await db_session.scalar(select(Client).where(Client.subdomain == "acme-log-react"))
    client.status = "grace"
    await db_session.commit()

    r = await auth_http.post(f"/clients/{client.id}/reactivate")
    assert r.status_code == 303

    log = await db_session.scalar(select(ActivityLog).where(ActivityLog.client_id == client.id))
    assert log.action == "reactivate"


@pytest.mark.asyncio
async def test_close_logs_activity(auth_http, db_session):
    await auth_http.post("/clients", data={"name": "Acme", "subdomain": "acme-log-close", "plan": "monthly"})
    from models import Client, ActivityLog
    from sqlalchemy import select
    client = await db_session.scalar(select(Client).where(Client.subdomain == "acme-log-close"))

    r = await auth_http.post(f"/clients/{client.id}/close")
    assert r.status_code == 303

    log = await db_session.scalar(select(ActivityLog).where(ActivityLog.client_id == client.id))
    assert log.action == "close"


@pytest.mark.asyncio
async def test_ticket_group_add_logs_activity(auth_http, db_session):
    await auth_http.post("/clients", data={"name": "Acme", "subdomain": "acme-log-tgadd", "plan": "monthly"})
    from models import Client, ActivityLog
    from sqlalchemy import select
    client = await db_session.scalar(select(Client).where(Client.subdomain == "acme-log-tgadd"))

    await auth_http.post(f"/clients/{client.id}/ticket-groups/add", data={"group_id": "g1@g.us"})

    log = await db_session.scalar(select(ActivityLog).where(ActivityLog.client_id == client.id))
    assert log.action == "ticket_group_add"
    assert log.detail == "g1@g.us"


@pytest.mark.asyncio
async def test_ticket_group_remove_logs_activity(auth_http, db_session):
    await auth_http.post("/clients", data={"name": "Acme", "subdomain": "acme-log-tgrm", "plan": "monthly"})
    from models import Client, ActivityLog
    from sqlalchemy import select
    client = await db_session.scalar(select(Client).where(Client.subdomain == "acme-log-tgrm"))
    await auth_http.post(f"/clients/{client.id}/ticket-groups/add", data={"group_id": "g1@g.us"})

    await auth_http.post(f"/clients/{client.id}/ticket-groups/remove", data={"group_id": "g1@g.us"})

    log = await db_session.scalar(
        select(ActivityLog)
        .where(ActivityLog.client_id == client.id, ActivityLog.action == "ticket_group_remove")
    )
    assert log.detail == "g1@g.us"


@pytest.mark.asyncio
async def test_ticket_group_reset_unrestricted_logs_activity(auth_http, db_session):
    await auth_http.post("/clients", data={"name": "Acme", "subdomain": "acme-log-tgreset", "plan": "monthly"})
    from models import Client, ActivityLog
    from sqlalchemy import select
    client = await db_session.scalar(select(Client).where(Client.subdomain == "acme-log-tgreset"))
    await auth_http.post(f"/clients/{client.id}/ticket-groups/add", data={"group_id": "g1@g.us"})

    await auth_http.post(f"/clients/{client.id}/ticket-groups/reset-unrestricted")

    log = await db_session.scalar(
        select(ActivityLog)
        .where(ActivityLog.client_id == client.id, ActivityLog.action == "ticket_group_reset_unrestricted")
    )
    assert log is not None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_clients.py -k "logs_activity" -v`
Expected: FAIL — every test hits `AttributeError: 'NoneType' object has no attribute 'action'` (no rows written yet).

- [ ] **Step 3: Instrument the six handlers**

`push_reminder` (currently has no `db.commit()` at all — add one):
```python
@app.post("/clients/{client_id}/push-reminder")
async def push_reminder(
    request: Request, client_id: int,
    username: str = Depends(require_login),
    db=Depends(get_db),
):
    client = await db.get(Client, client_id)
    if not client:
        raise HTTPException(status_code=404)
    await send_to_group(
        client,
        f"🔔 Payment reminder: Your subscription {'renews' if client.status == 'active' else 'expired'} "
        f"on {client.renewal_date}. Type /payment to pay now.",
    )
    await record_activity(db, client.id, username, "push_reminder", f"Sent to {client.whatsapp_group_id}")
    await db.commit()
    return RedirectResponse(f"/clients/{client_id}?reminded=1", status_code=303)
```

`send_invite` (also currently has no `db.commit()` — add one):
```python
@app.post("/clients/{client_id}/send-invite", response_class=HTMLResponse)
async def send_invite(
    request: Request, client_id: int,
    username: str = Depends(require_login),
    db=Depends(get_db),
):
    client = await db.get(Client, client_id)
    if not client:
        return HTMLResponse("Not found", status_code=404)
    phone = (client.admin_whatsapp_phone or "").strip()
    link = (client.whatsapp_invite_link or "").strip()
    if not phone or not link:
        return HTMLResponse(
            "<p>Admin phone or invite link not set. Go back and save them first.</p>"
            "<p><a href='/clients/" + str(client_id) + "'>Back</a></p>",
            status_code=400,
        )
    digits = re.sub(r"\D", "", phone)
    if re.fullmatch(r"07\d{8}", digits):
        digits = "254" + digits[1:]
    message = (
        f"Hi! You've been invited to join the WhatsApp monitoring group for *{client.name}*.\n\n"
        f"Click the link below to join:\n{link}\n\n"
        f"This group is managed by our incident ticketing system — all messages are tracked and actioned."
    )
    await send_dm_text(digits, message)
    await record_activity(db, client.id, username, "send_invite", f"Invite sent to {digits}")
    await db.commit()
    return RedirectResponse(f"/clients/{client_id}?invited=1", status_code=303)
```

`manual_reactivate` — add the call right before the existing `await db.commit()`:
```python
@app.post("/clients/{client_id}/reactivate")
async def manual_reactivate(
    request: Request, client_id: int,
    username: str = Depends(require_login),
    db=Depends(get_db),
):
    client = await db.get(Client, client_id)
    if not client:
        raise HTTPException(status_code=404)
    client.status = "active"
    client.grace_started_at = None
    client.billing_only_started_at = None
    client.last_warning_sent_at = None
    client.pre_expiry_14_warned = False
    client.pre_expiry_2_warned = False
    await record_activity(db, client.id, username, "reactivate")
    await db.commit()
    await start_client(client)
    await send_to_group(client, "✅ Your account has been manually reactivated by the administrator.")
    return RedirectResponse(f"/clients/{client_id}", status_code=303)
```

`close_client` — same pattern:
```python
@app.post("/clients/{client_id}/close")
async def close_client(
    request: Request, client_id: int,
    username: str = Depends(require_login),
    db=Depends(get_db),
):
    client = await db.get(Client, client_id)
    if not client:
        raise HTTPException(status_code=404)
    client.status = "closed"
    await record_activity(db, client.id, username, "close")
    await db.commit()
    await stop_client(client)
    await send_to_group(
        client,
        "\U0001f44b Your account has been closed. All services have been stopped. "
        "Thank you for using our service.",
    )
    return RedirectResponse(f"/clients/{client_id}", status_code=303)
```

`admin_add_ticket_group`:
```python
@app.post("/clients/{client_id}/ticket-groups/add", response_class=HTMLResponse)
async def admin_add_ticket_group(
    request: Request, client_id: int,
    group_id: str = Form(...),
    username: str = Depends(require_login),
    db=Depends(get_db),
):
    client = await db.get(Client, client_id)
    if not client:
        return HTMLResponse("Not found", status_code=404)
    group_id = group_id.strip()
    if group_id:
        groups = json.loads(client.allowed_ticket_groups) if client.allowed_ticket_groups else []
        if client.ticket_group_tier_id is None:
            # _get_or_seed_group_tiers (Task 2) guarantees the 3 fixed tiers exist —
            # a fresh install may reach this admin route before anyone has opened
            # /prices, so the lookup can't assume the rows are already there.
            base_tier = (await _get_or_seed_group_tiers(db))[0]
            client.ticket_group_tier_id = base_tier.id
        if group_id not in groups:
            groups.append(group_id)
        client.allowed_ticket_groups = json.dumps(groups)
        await record_activity(db, client.id, username, "ticket_group_add", group_id)
        await db.commit()
    return RedirectResponse(f"/clients/{client_id}", status_code=303)
```

`admin_remove_ticket_group`:
```python
@app.post("/clients/{client_id}/ticket-groups/remove", response_class=HTMLResponse)
async def admin_remove_ticket_group(
    request: Request, client_id: int,
    group_id: str = Form(...),
    username: str = Depends(require_login),
    db=Depends(get_db),
):
    client = await db.get(Client, client_id)
    if not client:
        return HTMLResponse("Not found", status_code=404)
    if client.allowed_ticket_groups is not None:
        groups = json.loads(client.allowed_ticket_groups)
        groups = [g for g in groups if g != group_id.strip()]
        client.allowed_ticket_groups = json.dumps(groups)
        await record_activity(db, client.id, username, "ticket_group_remove", group_id.strip())
        await db.commit()
    return RedirectResponse(f"/clients/{client_id}", status_code=303)
```

`admin_reset_ticket_groups_unrestricted`:
```python
@app.post("/clients/{client_id}/ticket-groups/reset-unrestricted", response_class=HTMLResponse)
async def admin_reset_ticket_groups_unrestricted(
    request: Request, client_id: int,
    username: str = Depends(require_login),
    db=Depends(get_db),
):
    client = await db.get(Client, client_id)
    if not client:
        return HTMLResponse("Not found", status_code=404)
    pending_requests = (await db.execute(
        select(GroupUpgradeRequest).where(
            GroupUpgradeRequest.client_id == client.id,
            GroupUpgradeRequest.status == "pending",
        )
    )).scalars().all()
    for req in pending_requests:
        req.status = "cancelled"
    client.allowed_ticket_groups = None
    client.ticket_group_tier_id = None
    await record_activity(db, client.id, username, "ticket_group_reset_unrestricted")
    await db.commit()
    return RedirectResponse(f"/clients/{client_id}", status_code=303)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_clients.py -v`
Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add billing/main.py billing/tests/test_clients.py
git commit -m "feat: log push-reminder, invite, reactivate, close, and ticket-group actions"
```

---

### Task 4: Instrument global price-setting endpoints

**Files:**
- Modify: `billing/main.py` — `set_group_tier_prices` (~line 396), `set_prices` (~line 425)
- Test: `billing/tests/test_clients.py`

**Interfaces:**
- Consumes: `activity_log.record_activity`, `activity_log.diff_summary` (Task 1).

- [ ] **Step 1: Write the failing tests**

Append to `billing/tests/test_clients.py`:
```python
@pytest.mark.asyncio
async def test_set_prices_logs_global_activity(auth_http, db_session):
    r = await auth_http.post("/prices", data={"monthly_amount": "1500.00", "annual_amount": "15000.00"})
    assert r.status_code == 303

    from models import ActivityLog
    from sqlalchemy import select
    log = await db_session.scalar(select(ActivityLog).where(ActivityLog.action == "set_plan_prices"))
    assert log is not None
    assert log.client_id is None
    assert "monthly: (unset) → 1500.00" in log.detail


@pytest.mark.asyncio
async def test_set_prices_logs_nothing_when_unchanged(auth_http, db_session):
    await auth_http.post("/prices", data={"monthly_amount": "1500.00", "annual_amount": "15000.00"})
    r = await auth_http.post("/prices", data={"monthly_amount": "1500.00", "annual_amount": "15000.00"})
    assert r.status_code == 303

    from models import ActivityLog
    from sqlalchemy import select
    count = len((await db_session.execute(
        select(ActivityLog).where(ActivityLog.action == "set_plan_prices")
    )).scalars().all())
    assert count == 1


@pytest.mark.asyncio
async def test_set_group_tier_prices_logs_global_activity(auth_http, db_session):
    r = await auth_http.post("/prices/group-tiers", data={
        "tier1_amount": "500.00", "tier2_amount": "1000.00", "tier3_amount": "1500.00",
    })
    assert r.status_code == 303

    from models import ActivityLog
    from sqlalchemy import select
    log = await db_session.scalar(select(ActivityLog).where(ActivityLog.action == "set_group_tier_prices"))
    assert log is not None
    assert log.client_id is None
    assert "tier 1-5: 0 → 500.00" in log.detail
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_clients.py -k "set_prices_logs or set_group_tier_prices_logs" -v`
Expected: FAIL — `log is None` / `AttributeError` (no rows written yet).

- [ ] **Step 3: Instrument both handlers**

`set_group_tier_prices`:
```python
@app.post("/prices/group-tiers", response_class=HTMLResponse)
async def set_group_tier_prices(
    request: Request,
    tier1_amount: str = Form(...),
    tier2_amount: str = Form(...),
    tier3_amount: str = Form(...),
    username: str = Depends(require_login),
    db=Depends(get_db),
):
    now = datetime.now(timezone.utc)
    group_tiers = await _get_or_seed_group_tiers(db)
    try:
        amounts = [Decimal(tier1_amount), Decimal(tier2_amount), Decimal(tier3_amount)]
        if any(v < 0 for v in amounts):
            raise ValueError("Amount must be non-negative")
    except Exception as e:
        prices = {p.plan_type: p for p in (await db.execute(select(PlanPrice))).scalars().all()}
        return templates.TemplateResponse(request, "prices.html", {
            "prices": prices, "group_tiers": group_tiers, "username": username,
            "group_tier_error": f"Invalid amount: {e}",
        })
    old_amounts = [t.amount for t in group_tiers]
    labels = [f"tier {t.min_groups}-{t.max_groups or '+'}" for t in group_tiers]
    for tier, amount in zip(group_tiers, amounts):
        tier.amount = amount
        tier.set_at = now
        tier.set_by = username
    detail = diff_summary(list(zip(labels, old_amounts, amounts)))
    if detail:
        await record_activity(db, None, username, "set_group_tier_prices", detail)
    await db.commit()
    return RedirectResponse("/prices", status_code=303)
```

`set_prices`:
```python
@app.post("/prices", response_class=HTMLResponse)
async def set_prices(
    request: Request,
    monthly_amount: str = Form(...),
    annual_amount: str = Form(...),
    username: str = Depends(require_login),
    db=Depends(get_db),
):
    now = datetime.now(timezone.utc)
    try:
        amounts = {
            "monthly": Decimal(monthly_amount),
            "annual": Decimal(annual_amount),
        }
        if any(v < 0 for v in amounts.values()):
            raise ValueError("Amount must be non-negative")
    except Exception as e:
        prices = {p.plan_type: p for p in (await db.execute(select(PlanPrice))).scalars().all()}
        return templates.TemplateResponse(request, "prices.html", {
            "prices": prices, "username": username, "error": f"Invalid amount: {e}",
        })
    changes = []
    for plan_type, amount in amounts.items():
        existing = await db.scalar(select(PlanPrice).where(PlanPrice.plan_type == plan_type))
        old = existing.amount if existing else None
        changes.append((plan_type, old, amount))
        if existing:
            existing.amount = amount
            existing.set_at = now
            existing.set_by = username
        else:
            db.add(PlanPrice(
                plan_type=plan_type, amount=amount,
                currency="KES", set_at=now, set_by=username,
            ))
    detail = diff_summary(changes)
    if detail:
        await record_activity(db, None, username, "set_plan_prices", detail)
    await db.commit()
    return RedirectResponse("/prices", status_code=303)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_clients.py -v`
Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add billing/main.py billing/tests/test_clients.py
git commit -m "feat: log global plan-price and group-tier-price changes"
```

---

### Task 5: Instrument the scheduler's six automated-warning branches

**Files:**
- Modify: `billing/scheduler.py` (entire `_check_client_status` function)
- Test: `billing/tests/test_scheduler_logic.py`

**Interfaces:**
- Consumes: `activity_log.record_activity` (Task 1).

- [ ] **Step 1: Extend the failing tests**

In `billing/tests/test_scheduler_logic.py`, change the import line near the top:
```python
from models import Client
```
to:
```python
from models import ActivityLog, Client
```

Then add the following assertions to the end of each of these 6 existing test functions (do not otherwise change these tests):

`test_active_sends_14day_pre_expiry_warning_once` — add after `mock_db.commit.assert_called_once()`:
```python
    mock_db.add.assert_called_once()
    logged = mock_db.add.call_args[0][0]
    assert isinstance(logged, ActivityLog)
    assert logged.actor == "system"
    assert logged.action == "scheduler_pre_expiry_14"
```

`test_active_sends_2day_pre_expiry_warning_once` — add:
```python
    mock_db.add.assert_called_once()
    logged = mock_db.add.call_args[0][0]
    assert logged.actor == "system"
    assert logged.action == "scheduler_pre_expiry_2"
```

`test_active_transitions_to_grace_when_expired` — add:
```python
    mock_db.add.assert_called_once()
    logged = mock_db.add.call_args[0][0]
    assert logged.actor == "system"
    assert logged.action == "scheduler_grace_start"
```

`test_grace_sends_reminder_every_2_days` — add:
```python
    mock_db.add.assert_called_once()
    logged = mock_db.add.call_args[0][0]
    assert logged.actor == "system"
    assert logged.action == "scheduler_grace_warning"
```

`test_grace_transitions_to_billing_only_after_14_days` — add:
```python
    mock_db.add.assert_called_once()
    logged = mock_db.add.call_args[0][0]
    assert logged.actor == "system"
    assert logged.action == "scheduler_billing_only_start"
```

`test_billing_only_sends_reminder_every_2_days` — add:
```python
    mock_db.add.assert_called_once()
    logged = mock_db.add.call_args[0][0]
    assert logged.actor == "system"
    assert logged.action == "scheduler_billing_only_warning"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_scheduler_logic.py -v`
Expected: the 6 modified tests FAIL with `AssertionError: Expected 'add' to have been called once. Called 0 times.` The other tests (already-warned / within-interval / closed-client) still pass unchanged.

- [ ] **Step 3: Instrument `_check_client_status`**

Replace the entire contents of `billing/scheduler.py`:
```python
import logging
from datetime import date, datetime, timedelta, timezone

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from activity_log import record_activity
from database import AsyncSessionLocal
from models import Client
from whatsapp import send_to_group

logger = logging.getLogger(__name__)

_GRACE_DAYS = 14
_WARNING_INTERVAL_HOURS = 48


async def _check_client_status(client: Client, db) -> None:
    today = date.today()
    now = datetime.now(timezone.utc)

    if client.status == "active":
        days_until_renewal = (client.renewal_date - today).days

        if today > client.renewal_date:
            client.status = "grace"
            client.grace_started_at = now
            client.last_warning_sent_at = now
            msg = (
                f"\U0001f512 Your subscription expired on {client.renewal_date}. "
                f"Your dashboard has been locked. Type /payment to restore access. "
                f"You have 14 days before ticketing is also suspended."
            )
            await record_activity(db, client.id, "system", "scheduler_grace_start", msg)
            await db.commit()
            await send_to_group(client, msg)
        elif days_until_renewal == 14 and not client.pre_expiry_14_warned:
            client.pre_expiry_14_warned = True
            msg = (
                f"\U0001f514 Reminder: Your subscription renews in 14 days on {client.renewal_date}. "
                f"Type /payment to pay early and avoid any interruption."
            )
            await record_activity(db, client.id, "system", "scheduler_pre_expiry_14", msg)
            await db.commit()
            await send_to_group(client, msg)
        elif days_until_renewal == 2 and not client.pre_expiry_2_warned:
            client.pre_expiry_2_warned = True
            msg = (
                f"⚠️ Urgent: Your subscription renews in 2 days on {client.renewal_date}. "
                f"Type /payment now to keep your service active."
            )
            await record_activity(db, client.id, "system", "scheduler_pre_expiry_2", msg)
            await db.commit()
            await send_to_group(client, msg)

    elif client.status == "grace":
        if not client.grace_started_at:
            return
        grace_age = now - client.grace_started_at
        if grace_age >= timedelta(days=_GRACE_DAYS):
            client.status = "billing_only"
            client.billing_only_started_at = now
            client.last_warning_sent_at = now
            msg = (
                f"\U0001f6a8 Your ticketing system has been suspended due to non-payment. "
                f"Your dashboard and ticketing groups are now offline. "
                f"Type /payment to reactivate. "
                f"Your data is retained for {client.data_retention_days} days."
            )
            await record_activity(db, client.id, "system", "scheduler_billing_only_start", msg)
            await db.commit()
            await send_to_group(client, msg)
        elif not client.last_warning_sent_at or (
            now - client.last_warning_sent_at >= timedelta(hours=_WARNING_INTERVAL_HOURS)
        ):
            days_overdue = (today - client.renewal_date).days
            days_left = max(0, _GRACE_DAYS - grace_age.days)
            client.last_warning_sent_at = now
            msg = (
                f"⚠️ Your subscription is unpaid ({days_overdue} days overdue). "
                f"Dashboard locked. Type /payment now — "
                f"ticketing will be suspended in {days_left} days."
            )
            await record_activity(db, client.id, "system", "scheduler_grace_warning", msg)
            await db.commit()
            await send_to_group(client, msg)

    elif client.status == "billing_only":
        if not client.last_warning_sent_at or (
            now - client.last_warning_sent_at >= timedelta(hours=_WARNING_INTERVAL_HOURS)
        ):
            days_overdue = (today - client.renewal_date).days
            billing_only_start = client.billing_only_started_at or now
            days_elapsed = (now - billing_only_start).days
            days_remaining = max(0, client.data_retention_days - days_elapsed)
            client.last_warning_sent_at = now
            msg = (
                f"\U0001f6a8 Urgent: Your service remains suspended ({days_overdue} days overdue). "
                f"Type /payment now. "
                f"Data will be retained for {days_remaining} more days."
            )
            await record_activity(db, client.id, "system", "scheduler_billing_only_warning", msg)
            await db.commit()
            await send_to_group(client, msg)


async def _run_daily_checks() -> None:
    async with AsyncSessionLocal() as db:
        clients = (await db.execute(
            select(Client).where(Client.status != "closed")
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
```
(This only restructures each branch's inline f-string into a local `msg` variable reused for both `record_activity` and `send_to_group` — the actual message text sent to clients is byte-for-byte unchanged.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_scheduler_logic.py -v`
Expected: all tests pass (the 6 extended tests plus the unchanged ones).

- [ ] **Step 5: Run the full test suite to check for regressions**

Run: `.venv/bin/python -m pytest -v`
Expected: all pass except the pre-existing, unrelated `test_phone_reply_triggers_stk_push` failure in `test_payment_flow.py` (known-stale test, not caused by this change).

- [ ] **Step 6: Commit**

```bash
git add billing/scheduler.py billing/tests/test_scheduler_logic.py
git commit -m "feat: log every automated scheduler warning/transition"
```

---

### Task 6: Per-client "Activity" card (`client_detail.html`)

**Files:**
- Modify: `billing/main.py:18` (import line), `billing/main.py` (`client_detail` GET handler, `update_client`'s `group_id_error` early return)
- Modify: `billing/templates/client_detail.html`
- Test: `billing/tests/test_clients.py`

**Interfaces:**
- Consumes: `activity_log.recent_activity(db, client_id, limit=50)` (Task 1).

- [ ] **Step 1: Write the failing tests**

Append to `billing/tests/test_clients.py`:
```python
@pytest.mark.asyncio
async def test_client_detail_shows_activity_log(auth_http, db_session):
    await auth_http.post("/clients", data={"name": "Acme", "subdomain": "acme-activity-ui", "plan": "monthly"})
    from models import Client
    from sqlalchemy import select
    client = await db_session.scalar(select(Client).where(Client.subdomain == "acme-activity-ui"))

    await auth_http.post(f"/clients/{client.id}/push-reminder")

    r = await auth_http.get(f"/clients/{client.id}")
    assert r.status_code == 200
    assert b"push_reminder" in r.content


@pytest.mark.asyncio
async def test_client_detail_shows_empty_activity_state(auth_http, db_session):
    await auth_http.post("/clients", data={"name": "Acme", "subdomain": "acme-activity-empty", "plan": "monthly"})
    from models import Client
    from sqlalchemy import select
    client = await db_session.scalar(select(Client).where(Client.subdomain == "acme-activity-empty"))

    r = await auth_http.get(f"/clients/{client.id}")
    assert r.status_code == 200
    assert b"No activity yet" in r.content


@pytest.mark.asyncio
async def test_group_id_error_page_still_renders_with_activity_log(auth_http, db_session):
    """The whatsapp_group_id-vs-ticket-group guard renders client_detail.html
    directly rather than redirecting — it must supply activity_log too, or the
    template's {% for row in activity_log %} raises UndefinedError."""
    await auth_http.post("/clients", data={"name": "Acme", "subdomain": "acme-activity-guard", "plan": "monthly"})
    from models import Client
    from sqlalchemy import select
    client = await db_session.scalar(select(Client).where(Client.subdomain == "acme-activity-guard"))
    await auth_http.post(f"/clients/{client.id}/ticket-groups/add", data={"group_id": "ticket-group@g.us"})

    r = await auth_http.post(f"/clients/{client.id}", data={"whatsapp_group_id": "ticket-group@g.us"})
    assert r.status_code == 200
    assert b"already registered as a ticket group" in r.content
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_clients.py -k "activity_log or activity_state or group_id_error_page" -v`
Expected: FAIL — `b"push_reminder" in r.content` and `b"No activity yet" in r.content` fail because the template doesn't render anything yet (`assert b"..." in r.content` → `AssertionError`); the guard test currently passes but must keep passing after the template change — re-check once Step 4 is done.

- [ ] **Step 3: Wire it up**

Change the import line `billing/main.py:18` (from Task 2) to add `recent_activity`:
```python
from activity_log import record_activity, diff_summary, recent_activity
```

Replace the `client_detail` GET handler:
```python
@app.get("/clients/{client_id}", response_class=HTMLResponse)
async def client_detail(
    request: Request, client_id: int,
    username: str = Depends(require_login),
    db=Depends(get_db),
):
    client = await db.get(Client, client_id)
    if not client:
        return HTMLResponse("Not found", status_code=404)
    payments = (await db.execute(
        select(Payment).where(Payment.client_id == client_id).order_by(Payment.initiated_at.desc())
    )).scalars().all()
    activity_log = await recent_activity(db, client_id)
    return templates.TemplateResponse(request, "client_detail.html", {
        "request": request, "client": client, "payments": payments, "username": username,
        "activity_log": activity_log,
    })
```

In `update_client`'s `group_id_error` early return (inside the `if whatsapp_group_id in ticket_groups:` block), add the `activity_log` fetch and pass it:
```python
        if whatsapp_group_id in ticket_groups:
            payments = (await db.execute(
                select(Payment).where(Payment.client_id == client_id).order_by(Payment.initiated_at.desc())
            )).scalars().all()
            activity_log = await recent_activity(db, client_id)
            return templates.TemplateResponse(request, "client_detail.html", {
                "request": request, "client": client, "payments": payments, "username": username,
                "activity_log": activity_log,
                "group_id_error": (
                    f"'{whatsapp_group_id}' is already registered as a ticket group for this client — "
                    "the billing group must be a separate group, or reminders will spam a support group."
                ),
            })
```

In `billing/templates/client_detail.html`, insert a new card immediately after the Payment History card's closing `</div>` (right before the `<!-- WhatsApp reconnect modal -->` comment):
```html
<div class="card">
  <h3 style="margin-top:0">Activity</h3>
  <table>
    <tr><th>Time</th><th>Actor</th><th>Action</th><th>Detail</th></tr>
    {% for row in activity_log %}
    <tr>
      <td>{{ row.created_at.strftime('%Y-%m-%d %H:%M') }} UTC</td>
      <td>{{ row.actor }}</td>
      <td>{{ row.action }}</td>
      <td>{{ row.detail }}</td>
    </tr>
    {% else %}
    <tr><td colspan="4" style="text-align:center;color:#888;padding:1rem">No activity yet</td></tr>
    {% endfor %}
  </table>
</div>
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_clients.py -v`
Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add billing/main.py billing/templates/client_detail.html billing/tests/test_clients.py
git commit -m "feat: show per-client activity log on the client detail page"
```

---

### Task 7: Global "Price change history" table (`prices.html`)

**Files:**
- Modify: `billing/main.py` — `prices_page` (~line 387), `set_group_tier_prices`'s error branch, `set_prices`'s error branch
- Modify: `billing/templates/prices.html`
- Test: `billing/tests/test_clients.py`

**Interfaces:**
- Consumes: `activity_log.recent_activity(db, None, limit=50)` (Task 1, imported in Task 6).

- [ ] **Step 1: Write the failing tests**

Append to `billing/tests/test_clients.py`:
```python
@pytest.mark.asyncio
async def test_prices_page_shows_activity_log(auth_http, db_session):
    await auth_http.post("/prices", data={"monthly_amount": "1500.00", "annual_amount": "15000.00"})

    r = await auth_http.get("/prices")
    assert r.status_code == 200
    assert b"set_plan_prices" in r.content


@pytest.mark.asyncio
async def test_prices_page_shows_empty_activity_state(auth_http):
    r = await auth_http.get("/prices")
    assert r.status_code == 200
    assert b"No price changes yet" in r.content


@pytest.mark.asyncio
async def test_prices_error_branch_still_renders_with_activity_log(auth_http):
    r = await auth_http.post("/prices", data={"monthly_amount": "not-a-number", "annual_amount": "15000.00"})
    assert r.status_code == 200
    assert b"Invalid amount" in r.content
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/bin/python -m pytest tests/test_clients.py -k "prices_page_shows or prices_error_branch" -v`
Expected: FAIL — `b"set_plan_prices" in r.content` and `b"No price changes yet" in r.content` assertions fail (template doesn't render the table yet). The error-branch test currently passes but re-check after Step 3.

- [ ] **Step 3: Wire it up**

Replace the `prices_page` handler:
```python
@app.get("/prices", response_class=HTMLResponse)
async def prices_page(request: Request, username: str = Depends(require_login), db=Depends(get_db)):
    prices = {p.plan_type: p for p in (await db.execute(select(PlanPrice))).scalars().all()}
    group_tiers = await _get_or_seed_group_tiers(db)
    activity_log = await recent_activity(db, None)
    return templates.TemplateResponse(request, "prices.html", {
        "request": request, "prices": prices, "group_tiers": group_tiers, "username": username,
        "activity_log": activity_log,
    })
```

In `set_group_tier_prices`'s `except` branch, add the fetch and pass it:
```python
    except Exception as e:
        prices = {p.plan_type: p for p in (await db.execute(select(PlanPrice))).scalars().all()}
        activity_log = await recent_activity(db, None)
        return templates.TemplateResponse(request, "prices.html", {
            "prices": prices, "group_tiers": group_tiers, "username": username,
            "group_tier_error": f"Invalid amount: {e}",
            "activity_log": activity_log,
        })
```

In `set_prices`'s `except` branch, same treatment:
```python
    except Exception as e:
        prices = {p.plan_type: p for p in (await db.execute(select(PlanPrice))).scalars().all()}
        activity_log = await recent_activity(db, None)
        return templates.TemplateResponse(request, "prices.html", {
            "prices": prices, "username": username, "error": f"Invalid amount: {e}",
            "activity_log": activity_log,
        })
```

In `billing/templates/prices.html`, add table styling to the existing `<style>` block:
```css
table{width:100%;border-collapse:collapse;margin-top:1.5rem;background:white;padding:1rem;border-radius:8px}
th,td{padding:.5rem;border-bottom:1px solid #eee;text-align:left;font-size:.85rem}
th{font-size:.75rem;text-transform:uppercase;letter-spacing:.05em;color:#666}
```

And add the table itself right before the closing `</body>` tag:
```html
<table>
  <caption style="text-align:left;font-weight:bold;margin-bottom:.5rem;caption-side:top">Price Change History</caption>
  <tr><th>Time</th><th>Actor</th><th>Action</th><th>Detail</th></tr>
  {% for row in activity_log %}
  <tr>
    <td>{{ row.created_at.strftime('%Y-%m-%d %H:%M') }} UTC</td>
    <td>{{ row.actor }}</td>
    <td>{{ row.action }}</td>
    <td>{{ row.detail }}</td>
  </tr>
  {% else %}
  <tr><td colspan="4" style="text-align:center;color:#888;padding:1rem">No price changes yet</td></tr>
  {% endfor %}
</table>
</body>
</html>
```
(Remove the old standalone `</body>\n</html>` lines at the end of the file since they're now part of this block.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/bin/python -m pytest tests/test_clients.py -v`
Expected: all tests pass.

- [ ] **Step 5: Run the full suite one more time**

Run: `.venv/bin/python -m pytest -v`
Expected: all pass except the pre-existing, unrelated `test_phone_reply_triggers_stk_push` failure.

- [ ] **Step 6: Commit**

```bash
git add billing/main.py billing/templates/prices.html billing/tests/test_clients.py
git commit -m "feat: show global price-change history on the prices page"
```
