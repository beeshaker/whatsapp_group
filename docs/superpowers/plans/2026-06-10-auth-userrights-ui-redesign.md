# Auth, User Rights & UI Redesign Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add session-based login, user management, per-action audit attribution, and redesign the dashboard as a Kanban board.

**Architecture:** `SessionMiddleware` on top of FastAPI; `auth.py` provides `require_login` (HTML routes, 302 redirect) and a `require_write_auth` helper (API write routes, accepts X-API-Key OR session so gateway callbacks keep working); new `users` and `audit_log` tables are added via the existing migration pattern in `database.py`; dashboard.html is completely rewritten as a 5-column Kanban board; `login.html` and `users.html` are new templates.

**Tech Stack:** FastAPI, SQLAlchemy async (SQLite), Jinja2, passlib[bcrypt], itsdangerous (via Starlette), pytest-asyncio

---

## File Map

| File | Action | Purpose |
|------|--------|---------|
| `backend/requirements.txt` | Modify | Add passlib[bcrypt], itsdangerous |
| `backend/models.py` | Modify | Add User, AuditLog; add changed_by to IncidentStatusHistory |
| `backend/database.py` | Modify | Migrations for new tables/columns; bootstrap admin in lifespan |
| `backend/auth.py` | Create | hash_password, verify_password, require_login |
| `backend/main.py` | Modify | SessionMiddleware, SECRET_KEY guard, new routes, updated routes |
| `backend/templates/login.html` | Create | Login page |
| `backend/templates/users.html` | Create | User management page |
| `backend/templates/dashboard.html` | Rewrite | Kanban board layout |
| `backend/tests/conftest.py` | Modify | Add SECRET_KEY env, authenticated_client fixture |
| `backend/tests/test_auth.py` | Create | Login/logout/require_login tests |
| `backend/tests/test_users.py` | Create | User CRUD route tests |
| `backend/tests/test_dashboard.py` | Modify | Switch HTML-route tests to authenticated_client |

---

## Task 1: Add dependencies

**Files:**
- Modify: `backend/requirements.txt`

- [ ] **Step 1: Add passlib and itsdangerous to requirements.txt**

Open `backend/requirements.txt` and add after `pydantic-settings==2.5.2`:

```
passlib[bcrypt]==1.7.4
itsdangerous==2.2.0
```

- [ ] **Step 2: Install in the venv**

```bash
cd backend && pip install passlib[bcrypt]==1.7.4 itsdangerous==2.2.0
```

Expected: packages install without error.

- [ ] **Step 3: Verify import**

```bash
python -c "from passlib.context import CryptContext; print('ok')"
```

Expected: `ok`

- [ ] **Step 4: Commit**

```bash
git add backend/requirements.txt
git commit -m "chore: add passlib[bcrypt] and itsdangerous for auth"
```

---

## Task 2: Add User and AuditLog models; add changed_by to IncidentStatusHistory

**Files:**
- Modify: `backend/models.py`

- [ ] **Step 1: Write failing test**

Create `backend/tests/test_models_schema.py`:

```python
import pytest
from sqlalchemy import inspect
from database import Base


def test_user_model_columns():
    cols = {c.name for c in Base.metadata.tables["users"].columns}
    assert cols == {"id", "username", "hashed_password", "created_at", "created_by"}


def test_audit_log_model_columns():
    cols = {c.name for c in Base.metadata.tables["audit_log"].columns}
    assert cols == {"id", "username", "action", "incident_id", "detail", "created_at"}


def test_incident_status_history_has_changed_by():
    cols = {c.name for c in Base.metadata.tables["incident_status_history"].columns}
    assert "changed_by" in cols
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd backend && python -m pytest tests/test_models_schema.py -v
```

Expected: FAIL with `KeyError: 'users'`

- [ ] **Step 3: Add models to models.py**

Append to `backend/models.py` (after IncidentStatusHistory):

```python
class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    hashed_password: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_by: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    username: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[str] = mapped_column(String(30), nullable=False)
    incident_id: Mapped[int] = mapped_column(Integer, nullable=False)
    detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
```

Also update `IncidentStatusHistory` — add `changed_by` field after `changed_at`:

```python
    changed_by: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd backend && python -m pytest tests/test_models_schema.py -v
```

Expected: PASS (3 tests)

- [ ] **Step 5: Commit**

```bash
git add backend/models.py backend/tests/test_models_schema.py
git commit -m "feat: add User, AuditLog models; add changed_by to IncidentStatusHistory"
```

---

## Task 3: Add migrations and bootstrap admin to database.py

**Files:**
- Modify: `backend/database.py`

The `init_db` function uses a try/except pattern per migration. The lifespan in `main.py` calls `init_db()`. Bootstrap logic (create admin if no users) goes in `main.py`'s lifespan, not here.

- [ ] **Step 1: Write failing test for bootstrap (in test_auth.py)**

Create `backend/tests/test_auth.py`:

```python
import os
os.environ.setdefault("SECRET_KEY", "test-secret-key-32chars-padding1")

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base, get_db
from main import app


_auth_engine = create_async_engine(
    "sqlite+aiosqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_AuthSession = async_sessionmaker(_auth_engine, expire_on_commit=False)


@pytest_asyncio.fixture(scope="module", autouse=True)
async def auth_schema():
    async with _auth_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@pytest_asyncio.fixture(autouse=True)
async def clean_auth_tables():
    yield
    async with _auth_engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            await conn.execute(table.delete())


@pytest_asyncio.fixture
async def auth_client():
    async def _override_get_db():
        async with _AuthSession() as session:
            yield session

    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


async def test_login_get_returns_html(auth_client):
    r = await auth_client.get("/login")
    assert r.status_code == 200
    assert b"form" in r.content


async def test_login_wrong_password_returns_401(auth_client):
    from models import User
    from auth import hash_password
    from datetime import datetime, timezone
    async with _AuthSession() as session:
        session.add(User(
            username="admin",
            hashed_password=hash_password("correct"),
            created_at=datetime.now(timezone.utc),
            created_by=None,
        ))
        await session.commit()
    r = await auth_client.post("/login", data={"username": "admin", "password": "wrong"})
    assert r.status_code == 401


async def test_login_correct_password_redirects(auth_client):
    from models import User
    from auth import hash_password
    from datetime import datetime, timezone
    async with _AuthSession() as session:
        session.add(User(
            username="admin",
            hashed_password=hash_password("correct"),
            created_at=datetime.now(timezone.utc),
            created_by=None,
        ))
        await session.commit()
    r = await auth_client.post(
        "/login",
        data={"username": "admin", "password": "correct"},
        follow_redirects=False,
    )
    assert r.status_code == 302
    assert r.headers["location"] == "/"


async def test_logout_clears_session(auth_client):
    from models import User
    from auth import hash_password
    from datetime import datetime, timezone
    async with _AuthSession() as session:
        session.add(User(
            username="admin",
            hashed_password=hash_password("pw"),
            created_at=datetime.now(timezone.utc),
            created_by=None,
        ))
        await session.commit()
    await auth_client.post("/login", data={"username": "admin", "password": "pw"})
    r = await auth_client.post("/logout", follow_redirects=False)
    assert r.status_code == 302
    assert r.headers["location"] == "/login"


async def test_dashboard_redirects_when_not_logged_in(auth_client):
    r = await auth_client.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert "/login" in r.headers["location"]
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd backend && python -m pytest tests/test_auth.py -v 2>&1 | head -40
```

Expected: ImportError or failures (auth.py, login route don't exist yet)

- [ ] **Step 3: Add migrations to database.py**

After the existing migration blocks in `init_db()`, add:

```python
    try:
        async with engine.begin() as conn:
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE NOT NULL,
                    hashed_password TEXT NOT NULL,
                    created_at TIMESTAMP NOT NULL,
                    created_by TEXT
                )
            """))
    except Exception:
        pass

    try:
        async with engine.begin() as conn:
            await conn.execute(text("""
                CREATE TABLE IF NOT EXISTS audit_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT NOT NULL,
                    action VARCHAR(30) NOT NULL,
                    incident_id INTEGER NOT NULL,
                    detail TEXT,
                    created_at TIMESTAMP NOT NULL
                )
            """))
    except Exception:
        pass

    try:
        async with engine.begin() as conn:
            await conn.execute(text(
                "ALTER TABLE incident_status_history ADD COLUMN changed_by TEXT"
            ))
    except Exception:
        pass
```

- [ ] **Step 4: Commit**

```bash
git add backend/database.py
git commit -m "feat: add migrations for users, audit_log, and changed_by column"
```

---

## Task 4: Create auth.py

**Files:**
- Create: `backend/auth.py`

- [ ] **Step 1: Write failing import test**

Add to `backend/tests/test_auth.py` (at the top of the file after imports):

The test `test_login_wrong_password_returns_401` already imports `from auth import hash_password` — this serves as the import test.

- [ ] **Step 2: Create backend/auth.py**

```python
import os
from passlib.context import CryptContext
from fastapi import HTTPException, Request

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=12)


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


async def require_login(request: Request) -> str:
    username = request.session.get("username")
    if not username:
        raise HTTPException(status_code=302, headers={"Location": "/login"})
    return username
```

- [ ] **Step 3: Run existing auth tests**

```bash
cd backend && python -m pytest tests/test_auth.py::test_login_wrong_password_returns_401 -v 2>&1 | head -20
```

Expected: FAIL (login route not yet in main.py — that's fine, import works)

- [ ] **Step 4: Commit**

```bash
git add backend/auth.py
git commit -m "feat: add auth.py with hash_password, verify_password, require_login"
```

---

## Task 5: Wire SessionMiddleware and SECRET_KEY guard into main.py

**Files:**
- Modify: `backend/main.py`

- [ ] **Step 1: Add SECRET_KEY to conftest.py**

Edit `backend/tests/conftest.py` — add this line in the env setup block at the top (before any imports):

```python
os.environ["SECRET_KEY"] = "test-secret-key-for-testing-only1"
```

- [ ] **Step 2: Update main.py — imports and middleware**

At the top of `backend/main.py`, add these imports:

```python
import sys
from starlette.middleware.sessions import SessionMiddleware
from auth import require_login, hash_password, verify_password
from models import User, AuditLog
```

Replace the existing `from models import ...` line with:

```python
from models import Incident, IncidentMedia, IncidentStatusHistory, IncidentUpdate, User, AuditLog
```

Add `SECRET_KEY` reading after `GATEWAY_SECRET_TOKEN` line:

```python
SECRET_KEY = os.getenv("SECRET_KEY", "")
if not SECRET_KEY or SECRET_KEY == "change-me":
    logger.error("SECRET_KEY env var is not set or is the default value. Refusing to start.")
    sys.exit(1)
```

After the existing `app.add_middleware(CORSMiddleware, ...)` block, add:

```python
app.add_middleware(
    SessionMiddleware,
    secret_key=SECRET_KEY,
    https_only=False,
    same_site="lax",
)
```

- [ ] **Step 3: Add bootstrap admin to lifespan**

Replace the existing `lifespan` function:

```python
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
            ))
            await session.commit()
            logger.info("Bootstrap admin user '%s' created.", admin_user)
    yield
```

- [ ] **Step 4: Run tests to verify nothing explodes**

```bash
cd backend && python -m pytest tests/test_auth.py::test_dashboard_redirects_when_not_logged_in -v
```

Expected: FAIL with "login route not found" or similar — meaning SessionMiddleware is wired but login route isn't yet.

- [ ] **Step 5: Commit**

```bash
git add backend/main.py backend/tests/conftest.py
git commit -m "feat: wire SessionMiddleware and SECRET_KEY startup guard"
```

---

## Task 6: Add login/logout routes and update HTML routes to require auth

**Files:**
- Modify: `backend/main.py`

- [ ] **Step 1: Add login/logout routes to main.py**

Add these routes before the existing `@app.get("/")` route:

```python
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if request.session.get("username"):
        from fastapi.responses import RedirectResponse
        return RedirectResponse(url="/", status_code=302)
    error = request.session.pop("login_error", None)
    return templates.TemplateResponse("login.html", {"request": request, "error": error})


@app.post("/login")
async def login_submit(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    from fastapi.responses import RedirectResponse
    form = await request.form()
    username = (form.get("username") or "").strip()
    password = form.get("password") or ""
    result = await db.execute(select(User).where(User.username == username))
    user = result.scalar_one_or_none()
    if not user or not verify_password(password, user.hashed_password):
        raise HTTPException(status_code=401, detail="Invalid username or password")
    request.session["username"] = user.username
    return RedirectResponse(url="/", status_code=302)


@app.post("/logout")
async def logout(request: Request):
    from fastapi.responses import RedirectResponse
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)
```

- [ ] **Step 2: Update GET / and GET /archive to require auth**

In the `dashboard` route, add `username: str = Depends(require_login)` parameter and pass it to the template context. Remove `api_key` from the context:

```python
@app.get("/", response_class=HTMLResponse)
async def dashboard(
    request: Request,
    username: str = Depends(require_login),
    db: AsyncSession = Depends(get_db),
):
    # ... (keep existing query unchanged) ...
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "incidents": incidents,
            "incidents_with_counts": incidents_with_counts,
            "title": os.getenv("DASHBOARD_TITLE", "Ops Incident Monitor"),
            "username": username,
            "mode": "live",
        },
    )
```

Do the same for `archive_dashboard`:

```python
@app.get("/archive", response_class=HTMLResponse)
async def archive_dashboard(
    request: Request,
    username: str = Depends(require_login),
    db: AsyncSession = Depends(get_db),
):
    # ... (keep existing query unchanged) ...
    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "incidents": incidents,
            "incidents_with_counts": incidents_with_counts,
            "title": os.getenv("DASHBOARD_TITLE", "Ops Incident Monitor"),
            "username": username,
            "mode": "archive",
        },
    )
```

- [ ] **Step 3: Run auth tests**

```bash
cd backend && python -m pytest tests/test_auth.py -v
```

Expected: `test_login_get_returns_html` FAIL (no login.html yet), others may pass or fail due to template.

- [ ] **Step 4: Commit**

```bash
git add backend/main.py
git commit -m "feat: add login/logout routes; protect dashboard and archive with require_login"
```

---

## Task 7: Add require_write_auth helper and update write endpoints for audit logging

**Files:**
- Modify: `backend/main.py`

Write endpoints accept **X-API-Key** (for gateway) OR **session** (for browser). Whichever is used, we capture the username for audit attribution (None for X-API-Key calls).

- [ ] **Step 1: Write failing tests for audit logging**

Create `backend/tests/test_audit.py`:

```python
import os
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-testing-only1")

from unittest.mock import AsyncMock, patch
import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base, get_db
from main import app

_audit_engine = create_async_engine(
    "sqlite+aiosqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_AuditSession = async_sessionmaker(_audit_engine, expire_on_commit=False)

_INCIDENT_CLASS = {"is_incident": True, "category": "plumbing", "severity": "high", "confidence": 0.92}
_ORIGINAL = {
    "event": "message.received",
    "data": {
        "id": "msg-audit1", "type": "chat", "isGroup": True,
        "chatId": "123@g.us", "chat": {"name": "Block A"},
        "author": "2541@c.us", "notifyName": "Alice",
        "body": "Pump leaking", "timestamp": 1782293340,
    },
}


@pytest_asyncio.fixture(scope="module", autouse=True)
async def audit_schema():
    async with _audit_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@pytest_asyncio.fixture(autouse=True)
async def clean_audit_tables():
    yield
    async with _audit_engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            await conn.execute(table.delete())


@pytest_asyncio.fixture
async def audit_client():
    async def _override_get_db():
        async with _AuditSession() as session:
            yield session

    app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


async def test_status_change_writes_audit_log(audit_client):
    with patch("main.classify_message", new=AsyncMock(return_value=_INCIDENT_CLASS)):
        with patch("main.push_incident", new=AsyncMock()):
            await audit_client.post("/api/v1/ops/ingest", json=_ORIGINAL, headers={"X-API-Key": "test-secret"})
    incident_id = (await audit_client.get("/incidents")).json()[0]["id"]
    await audit_client.patch(
        f"/incidents/{incident_id}/status",
        json={"status": "acknowledged"},
        headers={"X-API-Key": "test-secret"},
    )
    detail = (await audit_client.get(f"/incidents/{incident_id}")).json()
    assert "audit_log" in detail
    assert len(detail["audit_log"]) == 1
    assert detail["audit_log"][0]["action"] == "status_change"
    assert detail["audit_log"][0]["detail"] == "review → acknowledged"


async def test_reply_writes_audit_log(audit_client):
    with patch("main.classify_message", new=AsyncMock(return_value=_INCIDENT_CLASS)):
        with patch("main.push_incident", new=AsyncMock()):
            await audit_client.post("/api/v1/ops/ingest", json=_ORIGINAL, headers={"X-API-Key": "test-secret"})
    incident_id = (await audit_client.get("/incidents")).json()[0]["id"]
    with patch("main.reply_to_message", new=AsyncMock(return_value="wa-msg-1")):
        await audit_client.post(
            f"/incidents/{incident_id}/reply",
            json={"text": "We are investigating"},
            headers={"X-API-Key": "test-secret"},
        )
    detail = (await audit_client.get(f"/incidents/{incident_id}")).json()
    assert len(detail["audit_log"]) == 1
    assert detail["audit_log"][0]["action"] == "reply"
    assert "We are investigating" in detail["audit_log"][0]["detail"]


async def test_status_change_records_changed_by_from_api_key(audit_client):
    with patch("main.classify_message", new=AsyncMock(return_value=_INCIDENT_CLASS)):
        with patch("main.push_incident", new=AsyncMock()):
            await audit_client.post("/api/v1/ops/ingest", json=_ORIGINAL, headers={"X-API-Key": "test-secret"})
    incident_id = (await audit_client.get("/incidents")).json()[0]["id"]
    await audit_client.patch(
        f"/incidents/{incident_id}/status",
        json={"status": "acknowledged"},
        headers={"X-API-Key": "test-secret"},
    )
    detail = (await audit_client.get(f"/incidents/{incident_id}")).json()
    # X-API-Key calls have no session username — changed_by is None
    assert detail["status_history"][-1]["changed_by"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd backend && python -m pytest tests/test_audit.py -v 2>&1 | head -30
```

Expected: FAIL — `audit_log` key missing in detail response.

- [ ] **Step 3: Add require_write_auth to main.py**

Add this function after the existing `GATEWAY_SECRET_TOKEN` line in `main.py`:

```python
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
```

- [ ] **Step 4: Update update_incident_status in main.py**

Replace the existing `update_incident_status` function:

```python
@app.patch("/incidents/{incident_id}/status")
async def update_incident_status(
    incident_id: int,
    body: StatusUpdate,
    actor: Optional[str] = Depends(require_write_auth),
    db: AsyncSession = Depends(get_db),
):
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
```

- [ ] **Step 5: Update reply_to_incident in main.py**

Replace the existing `reply_to_incident` function:

```python
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
```

- [ ] **Step 6: Update relink_update in main.py**

Replace the existing `relink_update` function signature and add audit logging at commit:

```python
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
```

- [ ] **Step 7: Update get_incident_detail to include audit_log**

In the `get_incident_detail` function, add this query before the return statement:

```python
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
```

And add `"audit_log": audit_rows` to the return dict.

Also add `"changed_by": h.changed_by` to each entry in `history_rows`.

- [ ] **Step 8: Run audit tests**

```bash
cd backend && python -m pytest tests/test_audit.py -v
```

Expected: all 3 tests PASS

- [ ] **Step 9: Run full test suite to check for regressions**

```bash
cd backend && python -m pytest tests/ -v --ignore=tests/test_auth.py --ignore=tests/test_audit.py 2>&1 | tail -20
```

Expected: existing tests still PASS (write endpoints accept X-API-Key via require_write_auth)

- [ ] **Step 10: Commit**

```bash
git add backend/main.py backend/tests/test_audit.py
git commit -m "feat: add require_write_auth, audit_log writes on status_change/reply/relink"
```

---

## Task 8: Add user management routes

**Files:**
- Modify: `backend/main.py`
- Create: `backend/tests/test_users.py`

- [ ] **Step 1: Write failing tests**

Create `backend/tests/test_users.py`:

```python
import os
os.environ.setdefault("SECRET_KEY", "test-secret-key-for-testing-only1")

import pytest
import pytest_asyncio
from datetime import datetime, timezone
from httpx import AsyncClient, ASGITransport
from sqlalchemy.ext.asyncio import create_async_engine, async_sessionmaker
from sqlalchemy.pool import StaticPool

from database import Base, get_db
from auth import require_login, hash_password
from main import app
from models import User

_users_engine = create_async_engine(
    "sqlite+aiosqlite:///:memory:",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
_UsersSession = async_sessionmaker(_users_engine, expire_on_commit=False)


@pytest_asyncio.fixture(scope="module", autouse=True)
async def users_schema():
    async with _users_engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


@pytest_asyncio.fixture(autouse=True)
async def clean_users_tables():
    yield
    async with _users_engine.begin() as conn:
        for table in reversed(Base.metadata.sorted_tables):
            await conn.execute(table.delete())


@pytest_asyncio.fixture
async def users_client():
    async def _override_get_db():
        async with _UsersSession() as session:
            yield session

    async def _override_require_login():
        return "admin"

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[require_login] = _override_require_login
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()


async def _seed_admin(session):
    session.add(User(
        username="admin",
        hashed_password=hash_password("adminpw"),
        created_at=datetime.now(timezone.utc),
        created_by=None,
    ))
    await session.commit()


async def test_users_page_returns_html(users_client):
    r = await users_client.get("/users")
    assert r.status_code == 200
    assert b"Team Members" in r.content


async def test_create_user(users_client):
    async with _UsersSession() as session:
        await _seed_admin(session)
    r = await users_client.post("/users", data={"username": "bob", "password": "securepass"})
    assert r.status_code in (200, 302)
    async with _UsersSession() as session:
        from sqlalchemy import select
        result = await session.execute(select(User).where(User.username == "bob"))
        user = result.scalar_one_or_none()
        assert user is not None
        assert user.created_by == "admin"


async def test_create_user_duplicate_returns_422(users_client):
    async with _UsersSession() as session:
        await _seed_admin(session)
    await users_client.post("/users", data={"username": "bob", "password": "pass1"})
    r = await users_client.post("/users", data={"username": "bob", "password": "pass2"})
    assert r.status_code == 422


async def test_delete_user(users_client):
    async with _UsersSession() as session:
        await _seed_admin(session)
    await users_client.post("/users", data={"username": "bob", "password": "pass"})
    async with _UsersSession() as session:
        from sqlalchemy import select
        result = await session.execute(select(User).where(User.username == "bob"))
        bob = result.scalar_one()
    r = await users_client.post(f"/users/{bob.id}/delete")
    assert r.status_code in (200, 302)
    async with _UsersSession() as session:
        from sqlalchemy import select
        result = await session.execute(select(User).where(User.username == "bob"))
        assert result.scalar_one_or_none() is None


async def test_cannot_delete_yourself(users_client):
    async with _UsersSession() as session:
        await _seed_admin(session)
        from sqlalchemy import select
        result = await session.execute(select(User).where(User.username == "admin"))
        admin = result.scalar_one()
    r = await users_client.post(f"/users/{admin.id}/delete")
    assert r.status_code == 422
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
cd backend && python -m pytest tests/test_users.py -v 2>&1 | head -20
```

Expected: FAIL — `/users` routes don't exist yet.

- [ ] **Step 3: Add user management routes to main.py**

Add before the `@app.get("/login")` route:

```python
@app.get("/users", response_class=HTMLResponse)
async def users_page(
    request: Request,
    username: str = Depends(require_login),
    db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(User).order_by(User.created_at.asc()))
    users = result.scalars().all()
    return templates.TemplateResponse(
        "users.html",
        {
            "request": request,
            "users": users,
            "username": username,
            "title": "Team Members",
        },
    )


@app.post("/users", response_class=HTMLResponse)
async def create_user(
    request: Request,
    username: str = Depends(require_login),
    db: AsyncSession = Depends(get_db),
):
    from fastapi.responses import RedirectResponse
    form = await request.form()
    new_username = (form.get("username") or "").strip()
    new_password = form.get("password") or ""
    if not new_username or not new_password:
        raise HTTPException(status_code=422, detail="Username and password are required")
    existing = await db.execute(select(User).where(User.username == new_username))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=422, detail="Username already exists")
    db.add(User(
        username=new_username,
        hashed_password=hash_password(new_password),
        created_at=datetime.now(timezone.utc),
        created_by=username,
    ))
    await db.commit()
    return RedirectResponse(url="/users", status_code=302)


@app.post("/users/{user_id}/delete", response_class=HTMLResponse)
async def delete_user(
    user_id: int,
    request: Request,
    username: str = Depends(require_login),
    db: AsyncSession = Depends(get_db),
):
    from fastapi.responses import RedirectResponse
    target = await db.get(User, user_id)
    if not target:
        raise HTTPException(status_code=404, detail="User not found")
    if target.username == username:
        raise HTTPException(status_code=422, detail="Cannot delete your own account")
    await db.delete(target)
    await db.commit()
    return RedirectResponse(url="/users", status_code=302)
```

- [ ] **Step 4: Run user tests**

```bash
cd backend && python -m pytest tests/test_users.py -v
```

Expected: all 5 tests PASS

- [ ] **Step 5: Commit**

```bash
git add backend/main.py backend/tests/test_users.py
git commit -m "feat: add user management routes GET/POST /users and POST /users/{id}/delete"
```

---

## Task 9: Update conftest.py and test_dashboard.py for auth

**Files:**
- Modify: `backend/tests/conftest.py`
- Modify: `backend/tests/test_dashboard.py`

- [ ] **Step 1: Add authenticated_client fixture to conftest.py**

The `SECRET_KEY` env var was already added in Task 5. Now add the `authenticated_client` fixture.

In `backend/tests/conftest.py`, add these imports at the top:

```python
from auth import require_login
```

Then add after the existing `client` fixture:

```python
@pytest_asyncio.fixture
async def authenticated_client():
    async def _override_get_db():
        async with _TestSession() as session:
            yield session

    async def _override_require_login():
        return "testuser"

    app.dependency_overrides[get_db] = _override_get_db
    app.dependency_overrides[require_login] = _override_require_login
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as ac:
        yield ac
    app.dependency_overrides.clear()
```

- [ ] **Step 2: Update test_dashboard.py to use authenticated_client for HTML routes**

Replace the fixture name in the following tests from `client` to `authenticated_client`:

- `test_dashboard_returns_html(client)` → `test_dashboard_returns_html(authenticated_client)`
- `test_dashboard_contains_incident_card_markup(client)` → `test_dashboard_contains_incident_card_markup(authenticated_client)`
- `test_dashboard_has_filter_controls(client)` → `test_dashboard_has_filter_controls(authenticated_client)`
- `test_dashboard_shows_review_badge(client)` → `test_dashboard_shows_review_badge(authenticated_client)`
- `test_archive_route_returns_html(client)` → `test_archive_route_returns_html(authenticated_client)`
- `test_archive_route_shows_only_resolved_incidents(client)` → `test_archive_route_shows_only_resolved_incidents(authenticated_client)`
- `test_live_dashboard_excludes_resolved_incidents(client)` → `test_live_dashboard_excludes_resolved_incidents(authenticated_client)`

The `client` fixture is still used for `test_incidents_*` tests (JSON API, no auth needed on those routes).

- [ ] **Step 3: Run the full test suite**

```bash
cd backend && python -m pytest tests/ -v 2>&1 | tail -30
```

Expected: all previously passing tests still pass. `test_auth.py` tests may fail (no templates yet).

- [ ] **Step 4: Commit**

```bash
git add backend/tests/conftest.py backend/tests/test_dashboard.py
git commit -m "test: add authenticated_client fixture; update HTML-route tests to use it"
```

---

## Task 10: Create login.html template

**Files:**
- Create: `backend/templates/login.html`

- [ ] **Step 1: Run login test that checks template renders**

```bash
cd backend && python -m pytest tests/test_auth.py::test_login_get_returns_html -v
```

Expected: FAIL — `login.html` not found.

- [ ] **Step 2: Create login.html**

Create `backend/templates/login.html`:

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Sign in — Ops Ticketing</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; }
    * { margin: 0; padding: 0; }
    html, body { height: 100%; }
    body {
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, sans-serif;
      background:
        radial-gradient(circle at 20% 30%, rgba(56, 189, 248, 0.18), transparent 40%),
        radial-gradient(circle at 80% 70%, rgba(168, 85, 247, 0.15), transparent 40%),
        #07111f;
      color: #eef6ff;
      display: flex;
      align-items: center;
      justify-content: center;
      min-height: 100vh;
    }
    .card {
      width: 100%;
      max-width: 380px;
      padding: 40px 36px;
      background: rgba(16, 31, 52, 0.92);
      border: 1px solid rgba(148, 163, 184, 0.18);
      border-radius: 24px;
      box-shadow: 0 24px 60px rgba(0, 0, 0, 0.4);
      backdrop-filter: blur(20px);
    }
    .logo {
      display: flex;
      align-items: center;
      gap: 12px;
      margin-bottom: 28px;
    }
    .logo-mark {
      width: 46px;
      height: 46px;
      border-radius: 14px;
      background: linear-gradient(145deg, #0ea5e9, #1e3a8a);
      display: grid;
      place-items: center;
      font-size: 22px;
      flex-shrink: 0;
      box-shadow: 0 10px 24px rgba(14, 165, 233, 0.25);
    }
    .logo-text .eyebrow {
      font-size: 10px;
      font-weight: 800;
      letter-spacing: 0.12em;
      text-transform: uppercase;
      color: #38bdf8;
      margin-bottom: 3px;
    }
    .logo-text h1 {
      font-size: 18px;
      font-weight: 900;
      letter-spacing: -0.03em;
    }
    label {
      display: block;
      font-size: 12px;
      font-weight: 700;
      color: #91a3b8;
      letter-spacing: 0.06em;
      text-transform: uppercase;
      margin-bottom: 7px;
    }
    input[type="text"], input[type="password"] {
      width: 100%;
      height: 46px;
      border: 1px solid rgba(148, 163, 184, 0.22);
      border-radius: 14px;
      background: rgba(7, 17, 31, 0.84);
      color: #eef6ff;
      padding: 0 16px;
      font: inherit;
      font-size: 14px;
      outline: none;
      transition: border 0.2s, box-shadow 0.2s;
      margin-bottom: 18px;
    }
    input:focus {
      border-color: rgba(56, 189, 248, 0.7);
      box-shadow: 0 0 0 3px rgba(56, 189, 248, 0.12);
    }
    button[type="submit"] {
      width: 100%;
      height: 48px;
      border: none;
      border-radius: 14px;
      background: linear-gradient(135deg, #0ea5e9, #2563eb);
      color: white;
      font: inherit;
      font-size: 15px;
      font-weight: 800;
      letter-spacing: 0.02em;
      cursor: pointer;
      margin-top: 6px;
      box-shadow: 0 8px 24px rgba(14, 165, 233, 0.3);
      transition: filter 0.2s, transform 0.1s;
    }
    button[type="submit"]:hover { filter: brightness(1.08); }
    button[type="submit"]:active { transform: scale(0.98); }
    .error {
      margin-bottom: 18px;
      padding: 12px 14px;
      background: rgba(239, 68, 68, 0.14);
      border: 1px solid rgba(239, 68, 68, 0.35);
      border-radius: 12px;
      color: #fca5a5;
      font-size: 13px;
      font-weight: 600;
    }
  </style>
</head>
<body>
  <div class="card">
    <div class="logo">
      <div class="logo-mark">🎫</div>
      <div class="logo-text">
        <div class="eyebrow">Operations</div>
        <h1>Ops Ticketing</h1>
      </div>
    </div>
    {% if error %}
    <div class="error">{{ error }}</div>
    {% endif %}
    <form method="post" action="/login">
      <label for="username">Username</label>
      <input type="text" id="username" name="username" autocomplete="username" autofocus required>
      <label for="password">Password</label>
      <input type="password" id="password" name="password" autocomplete="current-password" required>
      <button type="submit">Sign in →</button>
    </form>
  </div>
</body>
</html>
```

- [ ] **Step 3: Run auth tests**

```bash
cd backend && python -m pytest tests/test_auth.py -v
```

Expected: all auth tests PASS

- [ ] **Step 4: Commit**

```bash
git add backend/templates/login.html
git commit -m "feat: add login.html template"
```

---

## Task 11: Create users.html template

**Files:**
- Create: `backend/templates/users.html`

- [ ] **Step 1: Run users page test**

```bash
cd backend && python -m pytest tests/test_users.py::test_users_page_returns_html -v
```

Expected: FAIL — `users.html` not found.

- [ ] **Step 2: Create users.html**

Create `backend/templates/users.html`:

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Team Members — Ops Ticketing</title>
  <style>
    *, *::before, *::after { box-sizing: border-box; }
    * { margin: 0; padding: 0; }
    html, body { height: 100%; }
    body {
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, sans-serif;
      background: radial-gradient(circle at top left, rgba(56,189,248,.16), transparent 34%),
                  radial-gradient(circle at top right, rgba(168,85,247,.14), transparent 30%),
                  #07111f;
      color: #eef6ff;
      min-height: 100vh;
    }
    nav.topbar {
      position: sticky; top: 0; z-index: 50;
      height: 64px; padding: 0 24px;
      display: flex; align-items: center; justify-content: space-between; gap: 18px;
      border-bottom: 1px solid rgba(148,163,184,.18);
      background: rgba(7,17,31,.82); backdrop-filter: blur(18px);
    }
    .brand { display: flex; align-items: center; gap: 10px; text-decoration: none; color: inherit; }
    .brand-mark {
      width: 38px; height: 38px; border-radius: 12px;
      background: linear-gradient(145deg,#0ea5e9,#1e3a8a);
      display: grid; place-items: center; font-size: 18px;
    }
    .brand-name { font-size: 16px; font-weight: 900; letter-spacing: -0.02em; }
    .nav-links { display: flex; align-items: center; gap: 4px; }
    .nav-links a {
      padding: 7px 14px; border-radius: 10px; font-size: 13px; font-weight: 700;
      color: #91a3b8; text-decoration: none; transition: background .15s, color .15s;
    }
    .nav-links a:hover { background: rgba(56,189,248,.08); color: #eef6ff; }
    .nav-links a.active { background: rgba(56,189,248,.15); color: #38bdf8; }
    .nav-right { display: flex; align-items: center; gap: 10px; }
    .user-pill {
      display: flex; align-items: center; gap: 8px;
      padding: 6px 12px; border-radius: 999px;
      background: rgba(255,255,255,.07); border: 1px solid rgba(148,163,184,.18);
      font-size: 13px; font-weight: 700;
    }
    .avatar {
      width: 26px; height: 26px; border-radius: 50%;
      background: linear-gradient(135deg,#0ea5e9,#7c3aed);
      display: grid; place-items: center; font-size: 10px; font-weight: 900; color: white;
    }
    .sign-out-btn {
      padding: 7px 14px; border-radius: 10px; border: 1px solid rgba(148,163,184,.22);
      background: transparent; color: #91a3b8; cursor: pointer; font: inherit; font-size: 13px;
      font-weight: 700; transition: background .15s, color .15s;
    }
    .sign-out-btn:hover { background: rgba(239,68,68,.12); color: #fca5a5; border-color: rgba(239,68,68,.3); }
    .page-content { max-width: 760px; margin: 0 auto; padding: 36px 24px; }
    .page-header { margin-bottom: 28px; }
    .page-header h2 { font-size: 26px; font-weight: 900; letter-spacing: -0.04em; }
    .page-header p { color: #91a3b8; font-size: 14px; margin-top: 6px; }
    .add-user-toggle {
      display: inline-flex; align-items: center; gap: 8px;
      padding: 10px 18px; border-radius: 13px; border: none;
      background: linear-gradient(135deg,#0ea5e9,#2563eb);
      color: white; font: inherit; font-size: 13px; font-weight: 800; cursor: pointer;
      box-shadow: 0 6px 18px rgba(14,165,233,.25); transition: filter .2s;
    }
    .add-user-toggle:hover { filter: brightness(1.08); }
    #add-user-form {
      display: none; margin-top: 20px; padding: 22px;
      background: rgba(16,31,52,.92); border: 1px solid rgba(148,163,184,.18);
      border-radius: 18px;
    }
    #add-user-form.open { display: block; }
    #add-user-form h3 { font-size: 14px; font-weight: 900; margin-bottom: 16px; }
    #add-user-form label { display: block; font-size: 11px; font-weight: 700; color: #91a3b8;
      letter-spacing: .08em; text-transform: uppercase; margin-bottom: 6px; }
    #add-user-form input {
      width: 100%; height: 42px; border: 1px solid rgba(148,163,184,.22);
      border-radius: 12px; background: rgba(7,17,31,.84); color: #eef6ff;
      padding: 0 14px; font: inherit; font-size: 13px; outline: none; margin-bottom: 14px;
    }
    #add-user-form input:focus { border-color: rgba(56,189,248,.7); }
    .create-btn {
      padding: 10px 20px; border-radius: 12px; border: none;
      background: linear-gradient(135deg,#0ea5e9,#2563eb);
      color: white; font: inherit; font-size: 13px; font-weight: 800; cursor: pointer;
    }
    table { width: 100%; border-collapse: collapse; margin-top: 24px; }
    thead th {
      text-align: left; padding: 10px 14px;
      font-size: 11px; font-weight: 900; color: #65758a;
      letter-spacing: .1em; text-transform: uppercase;
      border-bottom: 1px solid rgba(148,163,184,.14);
    }
    tbody tr:hover { background: rgba(56,189,248,.04); }
    td { padding: 13px 14px; font-size: 13px; border-bottom: 1px solid rgba(148,163,184,.08); }
    .td-user { display: flex; align-items: center; gap: 10px; }
    .table-avatar {
      width: 32px; height: 32px; border-radius: 50%;
      display: grid; place-items: center; font-size: 11px; font-weight: 900; color: white;
      flex-shrink: 0;
    }
    .remove-btn {
      padding: 6px 13px; border-radius: 10px;
      border: 1px solid rgba(239,68,68,.3); background: rgba(239,68,68,.08);
      color: #fca5a5; font: inherit; font-size: 12px; font-weight: 700; cursor: pointer;
      transition: background .15s;
    }
    .remove-btn:hover { background: rgba(239,68,68,.18); }
    .remove-btn:disabled { opacity: .3; cursor: not-allowed; }
    .date-cell { color: #65758a; }
  </style>
</head>
<body>
  <nav class="topbar">
    <a class="brand" href="/">
      <div class="brand-mark">🎫</div>
      <span class="brand-name">Ops Ticketing</span>
    </a>
    <div class="nav-links">
      <a href="/">📋 Live Queue</a>
      <a href="/archive">📁 Archive</a>
      <a href="/users" class="active">👥 Users</a>
    </div>
    <div class="nav-right">
      <div class="user-pill">
        <div class="avatar">{{ username[:2].upper() }}</div>
        {{ username }}
      </div>
      <form method="post" action="/logout" style="margin:0">
        <button class="sign-out-btn" type="submit">Sign out</button>
      </form>
    </div>
  </nav>

  <div class="page-content">
    <div class="page-header">
      <h2>Team Members</h2>
      <p>{{ users|length }} member{% if users|length != 1 %}s{% endif %}</p>
    </div>

    <button class="add-user-toggle" onclick="document.getElementById('add-user-form').classList.toggle('open')">
      + Add user
    </button>

    <div id="add-user-form">
      <h3>New team member</h3>
      <form method="post" action="/users">
        <label for="new-username">Username</label>
        <input type="text" id="new-username" name="username" required autocomplete="off">
        <label for="new-password">Password</label>
        <input type="password" id="new-password" name="password" required>
        <button type="submit" class="create-btn">Create user</button>
      </form>
    </div>

    <table>
      <thead>
        <tr>
          <th>User</th>
          <th>Created</th>
          <th>Added by</th>
          <th></th>
        </tr>
      </thead>
      <tbody>
        {% for u in users %}
        {% set hue = (loop.index0 * 67) % 360 %}
        <tr>
          <td>
            <div class="td-user">
              <div class="table-avatar" style="background: hsl({{ hue }},65%,38%)">{{ u.username[:2].upper() }}</div>
              {{ u.username }}
            </div>
          </td>
          <td class="date-cell">{{ u.created_at.strftime('%Y-%m-%d') }}</td>
          <td class="date-cell">{{ u.created_by or '—' }}</td>
          <td>
            <form method="post" action="/users/{{ u.id }}/delete" style="margin:0">
              <button class="remove-btn" type="submit"
                {% if u.username == username %}disabled title="Can't remove yourself"{% endif %}>
                Remove
              </button>
            </form>
          </td>
        </tr>
        {% endfor %}
      </tbody>
    </table>
  </div>
</body>
</html>
```

- [ ] **Step 3: Run user tests**

```bash
cd backend && python -m pytest tests/test_users.py -v
```

Expected: all 5 tests PASS

- [ ] **Step 4: Commit**

```bash
git add backend/templates/users.html
git commit -m "feat: add users.html template"
```

---

## Task 12: Rewrite dashboard.html as Kanban board

**Files:**
- Modify: `backend/templates/dashboard.html`

The new template receives `incidents_with_counts` (list of `{incident, update_count, media_count}`), `username`, and `mode`. It groups incidents into 5 columns by status using Jinja2 `selectattr`.

- [ ] **Step 1: Run dashboard tests to establish baseline**

```bash
cd backend && python -m pytest tests/test_dashboard.py -v
```

Note which tests pass. `test_dashboard_has_filter_controls` checks for `id="search-input"` and `id="sidebar"` — these IDs will disappear in the new layout. Update those tests first.

- [ ] **Step 2: Update test_dashboard.py filter controls test**

Replace `test_dashboard_has_filter_controls`:

```python
async def test_dashboard_has_filter_controls(authenticated_client):
    response = await authenticated_client.get("/")
    assert response.status_code == 200
    assert b'class="kanban-board"' in response.content
    assert b'class="kanban-col"' in response.content
```

- [ ] **Step 3: Write the new dashboard.html**

Replace the entire `backend/templates/dashboard.html` with:

```html
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{{ title }}</title>
  <style>
    :root {
      --bg: #07111f; --surface: #101f34; --surface-2: #132840;
      --line: rgba(148,163,184,.18); --text: #eef6ff; --muted: #91a3b8; --muted-2: #65758a;
      --blue: #38bdf8; --red: #ef4444; --amber: #f59e0b; --green: #22c55e; --purple: #a855f7;
      --red-soft: rgba(239,68,68,.14); --amber-soft: rgba(245,158,11,.14);
      --green-soft: rgba(34,197,94,.14); --shadow: 0 14px 36px rgba(0,0,0,.26);
    }
    *, *::before, *::after { box-sizing: border-box; }
    * { margin: 0; padding: 0; }
    html, body { height: 100%; }
    body {
      font-family: Inter, ui-sans-serif, system-ui, -apple-system, sans-serif;
      background: radial-gradient(circle at top left,rgba(56,189,248,.16),transparent 34%),
                  radial-gradient(circle at top right,rgba(168,85,247,.14),transparent 30%),
                  var(--bg);
      color: var(--text); overflow: hidden;
    }
    .app-shell { height: 100vh; display: grid; grid-template-rows: 64px auto 1fr; }

    /* ── TOP NAV ── */
    nav.topbar {
      position: sticky; top: 0; z-index: 50;
      display: flex; align-items: center; justify-content: space-between; gap: 18px;
      padding: 0 22px; border-bottom: 1px solid var(--line);
      background: rgba(7,17,31,.82); backdrop-filter: blur(18px);
    }
    .brand { display: flex; align-items: center; gap: 10px; text-decoration: none; color: inherit; }
    .brand-mark { width: 38px; height: 38px; border-radius: 12px; background: linear-gradient(145deg,#0ea5e9,#1e3a8a); display: grid; place-items: center; font-size: 18px; }
    .brand-name { font-size: 16px; font-weight: 900; letter-spacing: -0.02em; }
    .nav-links { display: flex; align-items: center; gap: 4px; }
    .nav-links a { padding: 7px 14px; border-radius: 10px; font-size: 13px; font-weight: 700; color: var(--muted); text-decoration: none; transition: background .15s, color .15s; }
    .nav-links a:hover { background: rgba(56,189,248,.08); color: var(--text); }
    .nav-links a.active { background: rgba(56,189,248,.15); color: var(--blue); }
    .nav-right { display: flex; align-items: center; gap: 10px; }
    .user-pill { display: flex; align-items: center; gap: 8px; padding: 5px 12px 5px 6px; border-radius: 999px; background: rgba(255,255,255,.07); border: 1px solid var(--line); font-size: 13px; font-weight: 700; }
    .avatar { width: 26px; height: 26px; border-radius: 50%; background: linear-gradient(135deg,#0ea5e9,#7c3aed); display: grid; place-items: center; font-size: 10px; font-weight: 900; }
    .sign-out-btn { padding: 7px 14px; border-radius: 10px; border: 1px solid var(--line); background: transparent; color: var(--muted); cursor: pointer; font: inherit; font-size: 13px; font-weight: 700; transition: background .15s, color .15s; }
    .sign-out-btn:hover { background: rgba(239,68,68,.1); color: #fca5a5; border-color: rgba(239,68,68,.3); }

    /* ── FILTER BAR ── */
    .filter-bar {
      display: flex; align-items: center; gap: 10px; padding: 10px 22px;
      border-bottom: 1px solid var(--line); background: rgba(7,17,31,.6); overflow-x: auto;
    }
    .filter-bar::-webkit-scrollbar { display: none; }
    .pill {
      flex-shrink: 0; padding: 5px 14px; border-radius: 999px; font-size: 12px; font-weight: 800;
      border: 1px solid var(--line); background: rgba(255,255,255,.04);
      color: var(--muted); cursor: pointer; transition: background .15s, color .15s, border-color .15s;
      user-select: none;
    }
    .pill:hover { background: rgba(56,189,248,.08); color: var(--text); }
    .pill.active { background: rgba(56,189,248,.16); border-color: rgba(56,189,248,.4); color: var(--blue); }
    .pill.high-toggle.active { background: rgba(239,68,68,.14); border-color: rgba(239,68,68,.4); color: #fca5a5; }
    .filter-sep { width: 1px; height: 20px; background: var(--line); flex-shrink: 0; }
    .filter-stats { display: flex; align-items: center; gap: 14px; margin-left: auto; flex-shrink: 0; }
    .stat-chip { font-size: 12px; font-weight: 700; color: var(--muted); }
    .stat-chip strong { color: var(--text); }

    /* ── KANBAN BOARD ── */
    .kanban-board {
      display: grid; grid-template-columns: repeat(5, minmax(200px, 1fr));
      gap: 12px; padding: 14px 16px; overflow-x: auto; overflow-y: hidden; min-height: 0;
    }
    .kanban-col { display: flex; flex-direction: column; gap: 8px; min-height: 0; }
    .col-header {
      display: flex; align-items: center; justify-content: space-between; gap: 8px;
      padding: 8px 12px; border-radius: 12px;
      background: rgba(255,255,255,.04); border: 1px solid var(--line);
      font-size: 12px; font-weight: 900; letter-spacing: .08em; text-transform: uppercase; color: var(--muted);
    }
    .col-count { min-width: 22px; text-align: center; padding: 2px 7px; border-radius: 999px; background: rgba(255,255,255,.08); font-size: 11px; }
    .col-cards { flex: 1; overflow-y: auto; display: flex; flex-direction: column; gap: 8px; padding: 2px; }
    .col-cards::-webkit-scrollbar { width: 4px; }
    .col-cards::-webkit-scrollbar-thumb { background: rgba(148,163,184,.15); border-radius: 99px; }

    /* ── CARD ── */
    .card {
      position: relative; padding: 13px 13px 13px 17px;
      background: linear-gradient(180deg,rgba(255,255,255,.055),rgba(255,255,255,.022));
      border: 1px solid var(--line); border-radius: 16px;
      cursor: pointer; transition: border-color .18s, box-shadow .18s, transform .14s;
      overflow: hidden;
    }
    .card::before {
      content: ""; position: absolute; left: 0; top: 10%; bottom: 10%; width: 4px; border-radius: 0 4px 4px 0;
    }
    .card.sev-high::before { background: var(--red); }
    .card.sev-medium::before { background: var(--amber); }
    .card.sev-low::before { background: var(--green); }
    .card:hover { border-color: rgba(56,189,248,.35); box-shadow: var(--shadow); transform: translateY(-1px); }
    .card-top { display: flex; align-items: flex-start; justify-content: space-between; gap: 8px; margin-bottom: 7px; }
    .card-id { font-size: 10px; font-weight: 900; color: var(--muted-2); letter-spacing: .08em; }
    .card-badges { display: flex; gap: 5px; flex-wrap: wrap; justify-content: flex-end; }
    .badge { font-size: 10px; font-weight: 800; padding: 2px 7px; border-radius: 999px; }
    .badge-high { background: var(--red-soft); color: #fca5a5; }
    .badge-medium { background: var(--amber-soft); color: #fcd34d; }
    .badge-low { background: var(--green-soft); color: #86efac; }
    .badge-review { background: rgba(56,189,248,.14); color: #7dd3fc; }
    .badge-new { background: rgba(168,85,247,.16); color: #d8b4fe; }
    .badge-acknowledged { background: rgba(245,158,11,.14); color: #fcd34d; }
    .badge-resolved { background: rgba(34,197,94,.14); color: #86efac; }
    .badge-ignored { background: rgba(148,163,184,.12); color: var(--muted); }
    .badge-attach { background: rgba(255,255,255,.08); color: var(--muted); }
    .card-prop { font-size: 13px; font-weight: 900; letter-spacing: -0.01em; margin-bottom: 4px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .card-body { font-size: 12px; color: var(--muted); line-height: 1.45; overflow: hidden; display: -webkit-box; -webkit-line-clamp: 2; -webkit-box-orient: vertical; }
    .card-footer { display: flex; align-items: center; justify-content: space-between; margin-top: 9px; font-size: 11px; color: var(--muted-2); }
    .cat-icon { font-size: 13px; }
    .empty-col { text-align: center; padding: 24px 12px; color: var(--muted-2); font-size: 12px; }

    /* ── MODAL ── */
    .modal-overlay { display: none; position: fixed; inset: 0; z-index: 100; background: rgba(0,0,0,.65); backdrop-filter: blur(6px); align-items: flex-start; justify-content: center; padding: 40px 20px; overflow-y: auto; }
    .modal-overlay.open { display: flex; }
    .modal {
      width: 100%; max-width: 680px; background: #0e1e33;
      border: 1px solid var(--line); border-radius: 24px;
      box-shadow: 0 28px 72px rgba(0,0,0,.45); overflow: hidden;
    }
    .modal-header {
      display: flex; align-items: flex-start; justify-content: space-between; gap: 14px;
      padding: 22px 24px 18px; border-bottom: 1px solid var(--line);
    }
    .modal-title { font-size: 17px; font-weight: 900; letter-spacing: -0.02em; }
    .modal-meta { margin-top: 5px; font-size: 12px; color: var(--muted); }
    .close-btn { width: 34px; height: 34px; border-radius: 10px; border: 1px solid var(--line); background: rgba(255,255,255,.05); color: var(--muted); cursor: pointer; font-size: 18px; flex-shrink: 0; display: grid; place-items: center; }
    .close-btn:hover { background: rgba(255,255,255,.1); color: var(--text); }
    .modal-body { padding: 20px 24px; display: flex; flex-direction: column; gap: 20px; max-height: 70vh; overflow-y: auto; }
    .modal-body::-webkit-scrollbar { width: 6px; }
    .modal-body::-webkit-scrollbar-thumb { background: rgba(148,163,184,.15); border-radius: 99px; }
    .section-label { font-size: 11px; font-weight: 900; letter-spacing: .1em; text-transform: uppercase; color: var(--muted-2); margin-bottom: 10px; }
    .msg-bubble { padding: 14px 16px; background: rgba(255,255,255,.04); border: 1px solid var(--line); border-radius: 14px; font-size: 13px; line-height: 1.55; white-space: pre-wrap; word-break: break-word; }
    .status-row { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
    .status-btn {
      padding: 7px 16px; border-radius: 999px; border: 1px solid var(--line);
      background: rgba(255,255,255,.04); color: var(--muted); cursor: pointer;
      font: inherit; font-size: 12px; font-weight: 800; transition: background .15s, color .15s, border-color .15s;
    }
    .status-btn:hover { background: rgba(56,189,248,.1); color: var(--text); border-color: rgba(56,189,248,.3); }
    .status-btn.current { background: rgba(56,189,248,.18); color: var(--blue); border-color: rgba(56,189,248,.45); }
    .updates-list { display: flex; flex-direction: column; gap: 10px; }
    .update-item { padding: 12px 14px; background: rgba(255,255,255,.03); border: 1px solid var(--line); border-radius: 12px; font-size: 12px; }
    .update-meta { color: var(--muted-2); margin-bottom: 5px; }
    .audit-list { display: flex; flex-direction: column; gap: 6px; }
    .audit-row { display: grid; grid-template-columns: 90px 110px 1fr auto; gap: 8px; align-items: center; font-size: 12px; padding: 8px 12px; background: rgba(255,255,255,.025); border-radius: 10px; }
    .audit-user { font-weight: 700; }
    .audit-action { color: var(--muted); }
    .audit-detail { color: var(--muted); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .audit-time { color: var(--muted-2); font-size: 11px; white-space: nowrap; }
    .reply-bar {
      position: sticky; bottom: 0;
      display: flex; gap: 10px; align-items: flex-end;
      padding: 16px 24px; border-top: 1px solid var(--line);
      background: #0e1e33;
    }
    .reply-bar textarea {
      flex: 1; min-height: 44px; max-height: 120px; padding: 12px 14px;
      border: 1px solid var(--line); border-radius: 14px;
      background: rgba(7,17,31,.84); color: var(--text); font: inherit; font-size: 13px;
      resize: vertical; outline: none; transition: border .2s;
    }
    .reply-bar textarea:focus { border-color: rgba(56,189,248,.6); }
    .reply-bar textarea::placeholder { color: var(--muted-2); }
    .send-btn {
      height: 44px; padding: 0 18px; border-radius: 12px; border: none;
      background: linear-gradient(135deg,#0ea5e9,#2563eb); color: white;
      font: inherit; font-size: 13px; font-weight: 800; cursor: pointer;
      box-shadow: 0 6px 18px rgba(14,165,233,.25); white-space: nowrap;
    }
    .send-btn:hover { filter: brightness(1.08); }
    .reply-user { font-size: 12px; color: var(--muted); display: flex; align-items: center; gap: 6px; white-space: nowrap; }
    .btn-reply {
      display: inline-flex; align-items: center; gap: 6px;
      padding: 6px 13px; border-radius: 10px; border: 1px solid rgba(56,189,248,.3);
      background: rgba(56,189,248,.1); color: var(--blue); cursor: pointer;
      font: inherit; font-size: 12px; font-weight: 800; transition: background .15s;
    }
    .btn-reply:hover { background: rgba(56,189,248,.18); }
    .relink-btn {
      padding: 5px 12px; border-radius: 10px; border: 1px solid var(--line);
      background: rgba(255,255,255,.04); color: var(--muted); cursor: pointer;
      font: inherit; font-size: 11px; font-weight: 700; transition: background .15s;
    }
    .relink-btn:hover { background: rgba(56,189,248,.08); color: var(--text); }
    .media-grid { display: flex; flex-wrap: wrap; gap: 8px; }
    .media-thumb { border-radius: 10px; overflow: hidden; }
    .media-thumb img { width: 90px; height: 70px; object-fit: cover; display: block; }
    .media-link { font-size: 12px; color: var(--blue); text-decoration: none; padding: 6px 10px; background: rgba(56,189,248,.08); border-radius: 8px; display: inline-block; }
  </style>
</head>
<body>
<div class="app-shell">

  <!-- TOP NAV -->
  <nav class="topbar">
    <a class="brand" href="/">
      <div class="brand-mark">🎫</div>
      <span class="brand-name">Ops Ticketing</span>
    </a>
    <div class="nav-links">
      <a href="/" {% if mode == 'live' %}class="active"{% endif %}>📋 Live Queue</a>
      <a href="/archive" {% if mode == 'archive' %}class="active"{% endif %}>📁 Archive</a>
      <a href="/users">👥 Users</a>
    </div>
    <div class="nav-right">
      <div class="user-pill">
        <div class="avatar">{{ username[:2].upper() }}</div>
        {{ username }}
      </div>
      <form method="post" action="/logout" style="margin:0">
        <button class="sign-out-btn" type="submit">Sign out</button>
      </form>
    </div>
  </nav>

  <!-- FILTER BAR -->
  <div class="filter-bar">
    <button class="pill active" data-cat="all" onclick="filterCat(this,'all')">All</button>
    <button class="pill" data-cat="electrical" onclick="filterCat(this,'electrical')">⚡ Electrical</button>
    <button class="pill" data-cat="plumbing" onclick="filterCat(this,'plumbing')">🔧 Plumbing</button>
    <button class="pill" data-cat="security" onclick="filterCat(this,'security')">🔒 Security</button>
    <button class="pill" data-cat="other" onclick="filterCat(this,'other')">📦 Other</button>
    <div class="filter-sep"></div>
    <button class="pill high-toggle" onclick="toggleHigh(this)">🔴 High only</button>
    <div class="filter-stats" id="stats-bar"></div>
  </div>

  <!-- KANBAN BOARD -->
  <div class="kanban-board" id="kanban">
    {% set statuses = [('new','🆕','New'),('review','🔍','Review'),('acknowledged','✅','Acknowledged'),('resolved','✔','Resolved'),('ignored','🚫','Ignored')] %}
    {% for status_key, status_icon, status_label in statuses %}
    {% set col_items = incidents_with_counts | selectattr("incident.status", "equalto", status_key) | list %}
    <div class="kanban-col" data-col="{{ status_key }}">
      <div class="col-header">
        <span>{{ status_icon }} {{ status_label }}</span>
        <span class="col-count">{{ col_items | length }}</span>
      </div>
      <div class="col-cards" id="col-{{ status_key }}">
        {% if col_items %}
        {% for item in col_items %}
        {% set i = item.incident %}
        <div class="card sev-{{ i.severity }}"
             data-id="{{ i.id }}" data-cat="{{ i.category }}" data-sev="{{ i.severity }}"
             onclick="openModal({{ i.id }})">
          <div class="card-top">
            <span class="card-id">#{{ i.id }}</span>
            <div class="card-badges">
              <span class="badge badge-{{ i.severity }}">{{ i.severity }}</span>
              {% if item.update_count > 0 %}<span class="badge badge-attach">+{{ item.update_count }}</span>{% endif %}
              {% if item.media_count > 0 %}<span class="badge badge-attach">📎{{ item.media_count }}</span>{% endif %}
            </div>
          </div>
          <div class="card-prop">{{ i.property_name }}</div>
          <div class="card-body">{{ i.message_body }}</div>
          <div class="card-footer">
            <span class="cat-icon">
              {% if i.category == 'electrical' %}⚡
              {% elif i.category == 'plumbing' %}🔧
              {% elif i.category == 'security' %}🔒
              {% else %}📦{% endif %}
            </span>
            <span>{{ i.received_at.strftime('%H:%M') if i.received_at else '' }}</span>
          </div>
        </div>
        {% endfor %}
        {% else %}
        <div class="empty-col">No tickets</div>
        {% endif %}
      </div>
    </div>
    {% endfor %}
  </div>

</div><!-- /app-shell -->

<!-- MODAL -->
<div class="modal-overlay" id="modal-overlay" onclick="if(event.target===this)closeModal()">
  <div class="modal" id="modal">
    <div class="modal-header">
      <div>
        <div class="modal-title" id="modal-title">Loading…</div>
        <div class="modal-meta" id="modal-meta"></div>
      </div>
      <button class="close-btn" onclick="closeModal()">✕</button>
    </div>
    <div class="modal-body" id="modal-body"></div>
    <div class="reply-bar" id="reply-bar">
      <span class="reply-user">as <strong>{{ username }}</strong></span>
      <textarea id="reply-text" placeholder="Type a reply…" rows="2"></textarea>
      <button class="send-btn" onclick="sendReply()">Send</button>
    </div>
  </div>
</div>

<script>
const STATUSES = ['new','review','acknowledged','resolved','ignored'];
let currentDetailId = null;
let activeCat = 'all';
let highOnly = false;

function filterCat(btn, cat) {
  activeCat = cat;
  document.querySelectorAll('.pill[data-cat]').forEach(p => p.classList.remove('active'));
  btn.classList.add('active');
  applyFilters();
}
function toggleHigh(btn) {
  highOnly = !highOnly;
  btn.classList.toggle('active', highOnly);
  applyFilters();
}
function applyFilters() {
  document.querySelectorAll('.card').forEach(c => {
    const matchCat = activeCat === 'all' || c.dataset.cat === activeCat;
    const matchHigh = !highOnly || c.dataset.sev === 'high';
    c.style.display = (matchCat && matchHigh) ? '' : 'none';
  });
  updateStats();
}
function updateStats() {
  const all = [...document.querySelectorAll('.card')].filter(c => c.style.display !== 'none');
  const high = all.filter(c => c.dataset.sev === 'high').length;
  const review = all.filter(c => c.closest('.kanban-col').dataset.col === 'review').length;
  document.getElementById('stats-bar').innerHTML =
    `<span class="stat-chip">🔴 High: <strong>${high}</strong></span>` +
    `<span class="stat-chip">🔍 Review: <strong>${review}</strong></span>` +
    `<span class="stat-chip">Total: <strong>${all.length}</strong></span>`;
}

async function openModal(id) {
  currentDetailId = id;
  document.getElementById('modal-overlay').classList.add('open');
  document.getElementById('modal-title').textContent = '#' + id;
  document.getElementById('modal-body').innerHTML = '<p style="color:var(--muted);padding:20px">Loading…</p>';
  try {
    const r = await fetch('/incidents/' + id);
    if (!r.ok) throw new Error('HTTP ' + r.status);
    const d = await r.json();
    renderModal(d);
  } catch(e) {
    currentDetailId = null;
    document.getElementById('modal-body').innerHTML = '<p style="color:#fca5a5;padding:20px">Failed to load: ' + e.message + '</p>';
  }
}
function closeModal() {
  document.getElementById('modal-overlay').classList.remove('open');
  currentDetailId = null;
}

function catIcon(cat) {
  return cat === 'electrical' ? '⚡' : cat === 'plumbing' ? '🔧' : cat === 'security' ? '🔒' : '📦';
}
function fmtTime(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  return d.toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'});
}

function renderModal(d) {
  document.getElementById('modal-title').textContent = '#' + d.id + ' · ' + d.property_name;
  document.getElementById('modal-meta').textContent =
    catIcon(d.category) + ' ' + d.category + '  ·  ' + d.severity + '  ·  ' + d.status + '  ·  ' + d.reporter_name;

  let html = '';

  // Message
  html += '<div><div class="section-label">Original message</div>';
  html += '<div class="msg-bubble">' + esc(d.message_body) + '</div></div>';

  // Status
  html += '<div><div class="section-label">Status</div><div class="status-row">';
  STATUSES.forEach(s => {
    const cur = s === d.status ? ' current' : '';
    html += `<button class="status-btn${cur}" onclick="changeStatus(${d.id},'${s}')">${s}</button>`;
  });
  html += '</div></div>';

  // Updates
  if (d.updates && d.updates.length) {
    html += '<div><div class="section-label">Updates (' + d.updates.length + ')</div><div class="updates-list">';
    d.updates.forEach(u => {
      html += `<div class="update-item">
        <div class="update-meta">${esc(u.reporter_name)} · ${fmtTime(u.received_at)}</div>
        <div>${esc(u.message_body)}</div>
        <div style="margin-top:6px;display:flex;gap:8px">
          <button class="btn-reply" onclick="openModal(${d.id})">View / Reply</button>
          <button class="relink-btn" onclick="promptRelink(${u.id})">Relink</button>
        </div>
      </div>`;
    });
    html += '</div></div>';
  }

  // Media
  if (d.media && d.media.length) {
    html += '<div><div class="section-label">Attachments</div><div class="media-grid">';
    d.media.forEach(m => {
      if (m.mimetype && m.mimetype.startsWith('image/')) {
        html += `<div class="media-thumb"><a href="/media/${m.id}" target="_blank"><img src="/media/${m.id}" alt="${esc(m.filename)}"></a></div>`;
      } else {
        html += `<a class="media-link" href="/media/${m.id}" target="_blank">📎 ${esc(m.filename)}</a>`;
      }
    });
    html += '</div></div>';
  }

  // Audit log
  if (d.audit_log && d.audit_log.length) {
    html += '<div><div class="section-label">Activity</div><div class="audit-list">';
    d.audit_log.forEach(a => {
      html += `<div class="audit-row">
        <span class="audit-user">${esc(a.username)}</span>
        <span class="audit-action">${esc(a.action.replace('_',' '))}</span>
        <span class="audit-detail">${esc(a.detail || '')}</span>
        <span class="audit-time">${fmtTime(a.created_at)}</span>
      </div>`;
    });
    html += '</div></div>';
  }

  document.getElementById('modal-body').innerHTML = html;
}

function esc(s) {
  if (!s) return '';
  return String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

async function changeStatus(id, newStatus) {
  const r = await fetch('/incidents/' + id + '/status', {
    method: 'PATCH', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({status: newStatus}), credentials: 'same-origin',
  });
  if (r.ok) {
    // Update card in board
    const card = document.querySelector('.card[data-id="' + id + '"]');
    if (card) {
      const oldCol = card.closest('.kanban-col');
      const newColCards = document.getElementById('col-' + newStatus);
      if (newColCards) {
        newColCards.appendChild(card);
        // Update empty states
        if (oldCol && !oldCol.querySelector('.card')) {
          const empty = oldCol.querySelector('.empty-col') || document.createElement('div');
          empty.className = 'empty-col'; empty.textContent = 'No tickets';
          oldCol.querySelector('.col-cards').appendChild(empty);
        }
        newColCards.querySelector('.empty-col')?.remove();
      }
    }
    await openModal(id);
  }
}

async function sendReply() {
  if (!currentDetailId) return;
  const text = document.getElementById('reply-text').value.trim();
  if (!text) return;
  const r = await fetch('/incidents/' + currentDetailId + '/reply', {
    method: 'POST', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({text}), credentials: 'same-origin',
  });
  if (r.ok) {
    document.getElementById('reply-text').value = '';
    await openModal(currentDetailId);
  } else {
    alert('Failed to send reply');
  }
}

async function promptRelink(updateId) {
  const targetId = prompt('Enter incident ID to relink this update to (leave blank to promote as new incident):');
  if (targetId === null) return;
  const body = targetId.trim() === '' ? {incident_id: null} : {incident_id: parseInt(targetId)};
  const r = await fetch('/incidents/' + updateId + '/relink', {
    method: 'PATCH', headers: {'Content-Type': 'application/json'},
    body: JSON.stringify(body), credentials: 'same-origin',
  });
  if (r.ok) { location.reload(); }
  else { alert('Relink failed'); }
}

document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });

// Initial stats
updateStats();
</script>
</body>
</html>
```

- [ ] **Step 4: Run full test suite**

```bash
cd backend && python -m pytest tests/ -v 2>&1 | tail -40
```

Expected: all tests PASS. `test_dashboard_has_filter_controls` now checks for `kanban-board` class.

- [ ] **Step 5: Commit**

```bash
git add backend/templates/dashboard.html backend/tests/test_dashboard.py
git commit -m "feat: rewrite dashboard.html as 5-column Kanban board with top nav and audit trail"
```

---

## Task 13: Final integration verification

- [ ] **Step 1: Run the complete test suite one final time**

```bash
cd backend && python -m pytest tests/ -v 2>&1 | tail -50
```

Expected: all tests PASS with 0 failures.

- [ ] **Step 2: Start the server and smoke-test manually**

```bash
SECRET_KEY=dev-secret-key-at-least-32-chars GATEWAY_SECRET_TOKEN=devtoken uvicorn main:app --reload --app-dir backend 2>&1 &
sleep 3
curl -s http://localhost:8000/health
```

Expected: `{"status":"ok"}`

- [ ] **Step 3: Verify redirect to login**

```bash
curl -s -o /dev/null -w "%{http_code}" http://localhost:8000/
```

Expected: `302`

- [ ] **Step 4: Kill dev server**

```bash
pkill -f "uvicorn main:app" || true
```

- [ ] **Step 5: Final commit**

```bash
git add -A
git commit -m "feat: complete auth, user rights, audit logging, and Kanban UI redesign"
```

---

## Self-Review

### Spec coverage check

| Spec requirement | Task(s) |
|-----------------|---------|
| Session-based login | Task 5, 6 |
| All logged-in users have full access | Task 6 (no roles) |
| Every write action attributed to user | Task 7 (require_write_auth + AuditLog) |
| Admins can add/remove users | Task 8 |
| Kanban board redesign | Task 12 |
| Top nav bar with logo, nav links, user pill | Task 12 |
| Filter bar (category pills, high-only toggle, stats) | Task 12 |
| Audit trail in ticket modal | Task 7 + Task 12 |
| Login page (centred card) | Task 10 |
| User management page with table | Task 11 |
| Cannot delete yourself | Task 8 |
| SECRET_KEY startup guard | Task 5 |
| Bootstrap admin on empty users table | Task 5 |
| passlib[bcrypt] bcrypt cost 12 | Task 4 |
| api_key not exposed in template | Task 6 (removed from context) |
| Gateway callbacks still use X-API-Key | Task 7 (require_write_auth) |
| changed_by on incident_status_history | Task 2, 3, 7 |

All spec requirements covered.

### Placeholder scan

No TBD/TODO/placeholder steps found. All steps contain complete code.

### Type consistency

- `AuditLog` defined in Task 2, imported in Task 5, used in Task 7 — consistent.
- `User` defined in Task 2, routes use `User` model — consistent.
- `require_login` defined in Task 4, imported in Task 5, used in Tasks 6 and 8 — consistent.
- `require_write_auth` defined in Task 7 — used in same task — consistent.
- `changed_by` added to `IncidentStatusHistory` model in Task 2, migration in Task 3, written in Task 7 — consistent.
- `audit_log` key added to `get_incident_detail` return in Task 7; template reads `d.audit_log` in Task 12 — consistent.

---

**Plan complete and saved to `docs/superpowers/plans/2026-06-10-auth-userrights-ui-redesign.md`.**

**Two execution options:**

**1. Subagent-Driven (recommended)** — Fresh subagent per task, review between tasks, fast iteration.
**REQUIRED SUB-SKILL:** Use superpowers:subagent-driven-development

**2. Inline Execution** — Execute tasks in this session using executing-plans, batch execution with checkpoints.
**REQUIRED SUB-SKILL:** Use superpowers:executing-plans

**Which approach?**
