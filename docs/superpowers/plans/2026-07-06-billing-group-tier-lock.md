# Super-Admin Billing: Group Selection & Tiered Pricing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the billing admin restrict which WhatsApp groups may raise tickets for a client (an opt-in, per-client "allowed ticket-raising groups" list capped by a paid tier), let the client self-service *add* a group (paying to upgrade tiers via M-Pesa if it would exceed their current tier), and enforce the allow-list in the ticketing backend's message ingestion.

**Architecture:** The billing service (`billing/`) gains a new `GroupTierPrice` table (three fixed, non-overlapping tiers: 1-5, 6-10, 11+ groups) and two nullable columns on `Client` (`allowed_ticket_groups` — a JSON-encoded array of group-ID strings, `None` = unrestricted; `ticket_group_tier_id` — which tier the client currently pays for). The billing admin manages the allow-list and tier prices with no restriction via `client_detail.html`/`prices.html`. A new cross-service endpoint, `GET /api/clients/{subdomain}/ticket-groups`, mirrors the existing `X-Billing-Secret`-authenticated `/api/clients/{subdomain}/status` pattern; the ticketing backend (`backend/main.py`) fetches it through a new cached function, `_get_allowed_ticket_groups()`, structurally identical to the existing `_get_client_billing_status()` (60s TTL, fail-open to unrestricted). `ingest()` gains one early-return check, `{"status": "group_not_licensed"}`, placed after today's existing group/message-type filtering and after the `SUPERUSERS_GROUP_ID` sales-agent carve-out (so the separate billing/payment group is never affected). The client's own self-service "add a group" flow (`backend/templates/settings.html`) calls a new backend proxy endpoint, which calls a new *tier-limited* billing endpoint (distinct from the billing-admin's unrestricted one); if adding would exceed the client's tier, the client is offered an M-Pesa upgrade via the existing `initiate_stk_push()` mechanism, tracked by a new `GroupUpgradeRequest` table (deliberately **not** reusing `PaymentSession`/the renewal grace/billing_only state machine, since a tier upgrade has nothing to do with renewal or account status). `POST /webhook/mpesa` gains a second branch, keyed by `checkout_request_id`, that only runs when no `PaymentSession` matches — on success it appends the group and bumps `ticket_group_tier_id` in one transaction.

**Tech Stack:** FastAPI, SQLAlchemy (async, SQLite in tests / Postgres-or-SQLite in prod per service), httpx (cross-service calls), M-Pesa Daraja STK Push (`billing/mpesa.py`), Jinja2 + vanilla JS templates, pytest + pytest-asyncio + httpx `ASGITransport`.

## Global Constraints

- `GroupTierPrice` is seeded with exactly three non-overlapping tiers: `(min_groups=1, max_groups=5)`, `(min_groups=6, max_groups=10)`, `(min_groups=11, max_groups=None)` — `max_groups=None` means no upper bound. Seed `amount=Decimal("0")`, `currency="KES"` — the billing admin must set real prices via the `/prices` page before the tiers mean anything commercially; this is intentional, mirroring how `PlanPrice` rows start unpriced until an admin calls `set_prices`.
- `Client.allowed_ticket_groups` (`Text`, nullable, JSON-encoded array of strings) and `Client.ticket_group_tier_id` (`Integer`, nullable, `ForeignKey("group_tier_prices.id")`) both default to `None`. `None` on `allowed_ticket_groups` means **unrestricted** — today's behavior, zero change for any client that hasn't opted in. **Invariant:** once `allowed_ticket_groups` is no longer `None` (i.e. the client has opted in), `ticket_group_tier_id` must also be non-`None` — the billing-admin "add group" endpoint (Task 3) is responsible for setting `ticket_group_tier_id` to the lowest tier (`min_groups=1`) at the moment it first sets `allowed_ticket_groups` from `None` to `[]`. Every other codepath that reads `ticket_group_tier_id` (self-service tier-limit checks, upgrade flow) may assume it is set whenever `allowed_ticket_groups is not None`.
- The billing-admin management endpoints (`client_detail.html` / `POST /clients/{client_id}/ticket-groups/...`) can freely add, remove, or swap any group with **no tier-limit enforcement** — this is the only place removal or swapping is possible. The client-facing `/settings` self-service flow can only **add**, is tier-limited, and never gets a remove control.
- This feature is completely separate from `Client.whatsapp_group_id` / `SUPERUSERS_GROUP_ID` (the single billing/payment-command group where `/payment` is typed) — never touch that column, never gate the superusers-group branch in `ingest()`.
- No Alembic in this repo — follow the existing hand-rolled migration pattern: `billing/main.py`'s `_migrate_db()` (a list of `(table, column_def)` tuples, each `ALTER TABLE` wrapped in its own `try/except`). New tables (`GroupTierPrice`, `GroupUpgradeRequest`) need no migration entry — `init_db()`'s `Base.metadata.create_all` creates brand-new tables automatically; the manual list is only for adding columns to already-existing tables (`clients`).
- Billing-service cross-service endpoints (`GET/POST /api/clients/{subdomain}/...`) all use the existing `X-Billing-Secret` header check: `secret = request.headers.get("X-Billing-Secret", ""); if BILLING_WEBHOOK_SECRET and secret != BILLING_WEBHOOK_SECRET: raise HTTPException(401)`.
- Backend-service caching of billing-service reads follows the existing `_get_client_billing_status()` shape exactly: a module-level `dict | None` cache, a `_CACHE_TTL_SECONDS`-second TTL, fail open (return an unrestricted/safe default) on missing `BILLING_SERVICE_URL`/`CLIENT_SUBDOMAIN` config or any exception from the HTTP call, logged as a warning.
- Duplicate-add (a group already in `allowed_ticket_groups`) is a no-op, not an error, at every layer that can add a group.
- A malformed group ID (doesn't look like a WhatsApp JID) is rejected in the backend's `/api/settings/ticket-groups/add` endpoint *before* any billing call is made — validate with `re.fullmatch(r"[\w-]+@g\.us", group_id)`.
- The M-Pesa tier-upgrade flow is a **new, separate, simpler** mechanism — it must not touch `PaymentSession`, `Payment`, `client.status`, `client.renewal_date`, or any of the grace/billing_only warned-flag fields. If a client already has a `GroupUpgradeRequest` with `status="pending"`, a new upgrade request reuses/reports that one instead of triggering a second STK push.
- Follow existing test conventions: billing tests use the `db_session` fixture (fresh in-memory SQLite, `create_all`/`drop_all` per test) from `billing/tests/conftest.py`, and monkeypatch `database.AsyncSessionLocal` (for HTTP-level flows) to point at `db_session.bind`; backend tests reset any new module-level cache (`_ticket_groups_cache = None`) exactly like existing tests reset `_billing_status_cache = None`.

---

### Task 1: Billing data model — `GroupTierPrice`, `Client` columns, `GroupUpgradeRequest`, seeding

**Files:**
- Modify: `billing/models.py` (add `GroupTierPrice` and `GroupUpgradeRequest` classes; add two columns to `Client`)
- Modify: `billing/main.py` (extend `_migrate_db()`; add `_seed_group_tier_prices()`; call it from `lifespan()`)
- Test: `billing/tests/test_models.py`

**Interfaces:**
- Produces: `GroupTierPrice(id, min_groups: int, max_groups: Optional[int], amount: Decimal, currency: str, set_at: datetime, set_by: str)`.
- Produces: `Client.allowed_ticket_groups: Mapped[Optional[str]]`, `Client.ticket_group_tier_id: Mapped[Optional[int]]`.
- Produces: `GroupUpgradeRequest(id, client_id: int, group_id: str, target_tier_id: int, phone: str, amount: Decimal, checkout_request_id: Optional[str], status: str, created_at: datetime)` — `status` is one of `"pending" | "confirmed" | "failed"`, default `"pending"`.
- Produces: `main._seed_group_tier_prices() -> None` (async), called once from `lifespan()`.

- [ ] **Step 1: Write failing model tests**

Append to `billing/tests/test_models.py`:

```python
@pytest.mark.asyncio
async def test_client_ticket_group_columns_default_none(db_session):
    from models import Client
    from datetime import date, datetime, timezone
    c = Client(
        name="Acme", subdomain="acme-tg", plan="monthly",
        renewal_date=date.today(), created_at=datetime.now(timezone.utc),
    )
    db_session.add(c)
    await db_session.commit()
    await db_session.refresh(c)
    assert c.allowed_ticket_groups is None
    assert c.ticket_group_tier_id is None


@pytest.mark.asyncio
async def test_group_upgrade_request_defaults_to_pending(db_session):
    from models import Client, GroupTierPrice, GroupUpgradeRequest
    from datetime import date, datetime, timezone
    from decimal import Decimal
    c = Client(
        name="Acme", subdomain="acme-tg2", plan="monthly",
        renewal_date=date.today(), created_at=datetime.now(timezone.utc),
    )
    db_session.add(c)
    await db_session.flush()
    tier = GroupTierPrice(
        min_groups=6, max_groups=10, amount=Decimal("500"),
        set_at=datetime.now(timezone.utc), set_by="admin",
    )
    db_session.add(tier)
    await db_session.flush()
    req = GroupUpgradeRequest(
        client_id=c.id, group_id="120363XXXXXXXXXX@g.us",
        target_tier_id=tier.id, phone="254712345678", amount=Decimal("500"),
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    await db_session.commit()
    await db_session.refresh(req)
    assert req.status == "pending"


@pytest.mark.asyncio
async def test_seed_group_tier_prices_creates_three_non_overlapping_tiers(db_session, monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from sqlalchemy import select
    from models import GroupTierPrice
    import main
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    monkeypatch.setattr(main, "AsyncSessionLocal", factory)

    await main._seed_group_tier_prices()

    tiers = (await db_session.execute(
        select(GroupTierPrice).order_by(GroupTierPrice.min_groups)
    )).scalars().all()
    assert len(tiers) == 3
    assert (tiers[0].min_groups, tiers[0].max_groups) == (1, 5)
    assert (tiers[1].min_groups, tiers[1].max_groups) == (6, 10)
    assert (tiers[2].min_groups, tiers[2].max_groups) == (11, None)


@pytest.mark.asyncio
async def test_seed_group_tier_prices_is_idempotent(db_session, monkeypatch):
    from sqlalchemy.ext.asyncio import async_sessionmaker
    from sqlalchemy import select
    from models import GroupTierPrice
    import main
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    monkeypatch.setattr(main, "AsyncSessionLocal", factory)

    await main._seed_group_tier_prices()
    await main._seed_group_tier_prices()

    tiers = (await db_session.execute(select(GroupTierPrice))).scalars().all()
    assert len(tiers) == 3
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd billing && python -m pytest tests/test_models.py -v -k "ticket_group or upgrade_request or seed_group_tier"`
Expected: FAIL — `GroupTierPrice`/`GroupUpgradeRequest` don't exist yet, `Client` has no `allowed_ticket_groups`/`ticket_group_tier_id`, `main._seed_group_tier_prices` doesn't exist.

- [ ] **Step 3: Add the new columns and models**

In `billing/models.py`, add `Optional` is already imported; add these two columns to `Client` (after `backend_port`, before `created_at`):

```python
    backend_port: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    allowed_ticket_groups: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ticket_group_tier_id: Mapped[Optional[int]] = mapped_column(Integer, ForeignKey("group_tier_prices.id"), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
```

Add these two new classes at the end of `billing/models.py` (after `PaymentSession`):

```python


class GroupTierPrice(Base):
    __tablename__ = "group_tier_prices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    min_groups: Mapped[int] = mapped_column(Integer, nullable=False)
    max_groups: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)  # None = no upper bound
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    currency: Mapped[str] = mapped_column(String(5), nullable=False, default="KES")
    set_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    set_by: Mapped[str] = mapped_column(Text, nullable=False)

    def __init__(self, **kw):
        if "currency" not in kw:
            kw["currency"] = "KES"
        super().__init__(**kw)


class GroupUpgradeRequest(Base):
    """Tracks an in-progress (or completed) tier-upgrade M-Pesa payment triggered
    from the client's self-service /settings 'add group' flow. Deliberately
    separate from PaymentSession/Payment — a tier upgrade has nothing to do
    with renewal or the grace/billing_only state machine."""
    __tablename__ = "group_upgrade_requests"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    client_id: Mapped[int] = mapped_column(Integer, ForeignKey("clients.id"), nullable=False, index=True)
    group_id: Mapped[str] = mapped_column(Text, nullable=False)
    target_tier_id: Mapped[int] = mapped_column(Integer, ForeignKey("group_tier_prices.id"), nullable=False)
    phone: Mapped[str] = mapped_column(Text, nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    checkout_request_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(String(15), nullable=False, default="pending")
    # "pending" | "confirmed" | "failed"
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    def __init__(self, **kw):
        if "status" not in kw:
            kw["status"] = "pending"
        super().__init__(**kw)
```

- [ ] **Step 4: Add the migration entries and seeding function**

In `billing/main.py`, add `GroupTierPrice` and `GroupUpgradeRequest` to the models import:

```python
from models import AdminUser, Client, GroupTierPrice, GroupUpgradeRequest, Payment, PaymentSession, PlanPrice
```

In `_migrate_db()`, append two entries to the `migrations` list (after `("clients", "pre_expiry_2_warned BOOLEAN DEFAULT 0")`):

```python
            ("clients", "pre_expiry_2_warned BOOLEAN DEFAULT 0"),
            ("clients", "allowed_ticket_groups TEXT"),
            ("clients", "ticket_group_tier_id INTEGER"),
        ]
```

Add a new function right after `_migrate_db()`:

```python
async def _seed_group_tier_prices():
    """Seed the three fixed, non-overlapping group-count tiers if absent."""
    async with AsyncSessionLocal() as db:
        existing = (await db.execute(select(GroupTierPrice))).scalars().all()
        if existing:
            return
        now = datetime.now(timezone.utc)
        db.add_all([
            GroupTierPrice(min_groups=1, max_groups=5, amount=Decimal("0"), set_at=now, set_by="system"),
            GroupTierPrice(min_groups=6, max_groups=10, amount=Decimal("0"), set_at=now, set_by="system"),
            GroupTierPrice(min_groups=11, max_groups=None, amount=Decimal("0"), set_at=now, set_by="system"),
        ])
        await db.commit()
```

In `lifespan()`, call it after `_migrate_db()`:

```python
    await init_db()
    await _migrate_db()
    await _seed_group_tier_prices()
    await _seed_admin()
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd billing && python -m pytest tests/test_models.py -v`
Expected: PASS — all tests in the file, including pre-existing ones (regression check).

- [ ] **Step 6: Run the full billing test suite to confirm no regressions**

Run: `cd billing && python -m pytest -v`
Expected: PASS — all existing tests still pass (the two new nullable columns and two new tables must not affect any existing behavior).

- [ ] **Step 7: Commit**

```bash
git add billing/models.py billing/main.py billing/tests/test_models.py
git commit -m "feat: add GroupTierPrice/GroupUpgradeRequest models and ticket-group columns on Client"
```

---

### Task 2: Billing admin — group tier price management on `/prices`

**Files:**
- Modify: `billing/main.py` (extend `prices_page`; add `POST /prices/group-tiers`)
- Modify: `billing/templates/prices.html` (add a tier-pricing form)
- Test: `billing/tests/test_clients.py`

**Interfaces:**
- Consumes: `GroupTierPrice` (Task 1).
- Produces: `POST /prices/group-tiers` (Form: `tier1_amount`, `tier2_amount`, `tier3_amount`) — upserts the three seeded tiers' `amount`/`set_at`/`set_by`, ordered by `min_groups` ascending (tier1 = lowest `min_groups`).

- [ ] **Step 1: Write failing tests**

Append to `billing/tests/test_clients.py`:

```python
@pytest.mark.asyncio
async def test_prices_page_shows_group_tiers(auth_http):
    r = await auth_http.get("/prices")
    assert r.status_code == 200
    assert b"1" in r.content and b"5" in r.content  # tier boundaries rendered


@pytest.mark.asyncio
async def test_set_group_tier_prices(auth_http, db_session):
    r = await auth_http.post("/prices/group-tiers", data={
        "tier1_amount": "500.00", "tier2_amount": "1200.00", "tier3_amount": "2500.00",
    })
    assert r.status_code in (200, 303)
    from models import GroupTierPrice
    from sqlalchemy import select
    tiers = (await db_session.execute(
        select(GroupTierPrice).order_by(GroupTierPrice.min_groups)
    )).scalars().all()
    assert [str(t.amount) for t in tiers] == ["500.00", "1200.00", "2500.00"]
    assert all(t.set_by == "admin" for t in tiers)
```

Note: `auth_http` in this file doesn't currently trigger `lifespan()` seeding, so `GroupTierPrice` rows won't exist yet when these tests run against the test DB. `prices_page`/`set_group_tier_prices` (Step 3) must handle the "no rows yet" case by seeding on read, same as how `set_prices` already handles `PlanPrice` rows not existing (`if existing: ... else: db.add(...)`).

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd billing && python -m pytest tests/test_clients.py -v -k "group_tier"`
Expected: FAIL — no group-tier rows/section/route exist yet.

- [ ] **Step 3: Add the tier-management route and extend `prices_page`**

In `billing/main.py`, replace:

```python
@app.get("/prices", response_class=HTMLResponse)
async def prices_page(request: Request, username: str = Depends(require_login), db=Depends(get_db)):
    prices = {p.plan_type: p for p in (await db.execute(select(PlanPrice))).scalars().all()}
    return templates.TemplateResponse(request, "prices.html", {"request": request, "prices": prices, "username": username})
```

with:

```python
async def _get_or_seed_group_tiers(db) -> list[GroupTierPrice]:
    tiers = (await db.execute(select(GroupTierPrice).order_by(GroupTierPrice.min_groups))).scalars().all()
    if tiers:
        return list(tiers)
    now = datetime.now(timezone.utc)
    tiers = [
        GroupTierPrice(min_groups=1, max_groups=5, amount=Decimal("0"), set_at=now, set_by="system"),
        GroupTierPrice(min_groups=6, max_groups=10, amount=Decimal("0"), set_at=now, set_by="system"),
        GroupTierPrice(min_groups=11, max_groups=None, amount=Decimal("0"), set_at=now, set_by="system"),
    ]
    db.add_all(tiers)
    await db.commit()
    for t in tiers:
        await db.refresh(t)
    return tiers


@app.get("/prices", response_class=HTMLResponse)
async def prices_page(request: Request, username: str = Depends(require_login), db=Depends(get_db)):
    prices = {p.plan_type: p for p in (await db.execute(select(PlanPrice))).scalars().all()}
    group_tiers = await _get_or_seed_group_tiers(db)
    return templates.TemplateResponse(request, "prices.html", {
        "request": request, "prices": prices, "group_tiers": group_tiers, "username": username,
    })


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
            "error": f"Invalid amount: {e}",
        })
    for tier, amount in zip(group_tiers, amounts):
        tier.amount = amount
        tier.set_at = now
        tier.set_by = username
    await db.commit()
    return RedirectResponse("/prices", status_code=303)
```

Note `_get_or_seed_group_tiers` duplicates `_seed_group_tier_prices`'s seed values but operates on the request-scoped `db` session (via `Depends(get_db)`) rather than a fresh `AsyncSessionLocal()` — this mirrors how `_seed_admin` (startup-time, own session) and request handlers (request-scoped session) are already separate code paths elsewhere in this file.

- [ ] **Step 4: Update the template**

In `billing/templates/prices.html`, add a second form after the existing plan-prices form (before `</body>`):

```html
<form method="post" action="/prices/group-tiers" style="margin-top:1.5rem">
  <h2>Ticket-Raising Group Tiers (KES)</h2>
  {% if error %}<p class="error">{{ error }}</p>{% endif %}
  <label>Tier 1 (1&ndash;5 groups)</label>
  <input name="tier1_amount" type="number" step="0.01" min="0"
    value="{{ group_tiers[0].amount if group_tiers else '' }}" required>
  <label>Tier 2 (6&ndash;10 groups)</label>
  <input name="tier2_amount" type="number" step="0.01" min="0"
    value="{{ group_tiers[1].amount if group_tiers|length > 1 else '' }}" required>
  <label>Tier 3 (11+ groups)</label>
  <input name="tier3_amount" type="number" step="0.01" min="0"
    value="{{ group_tiers[2].amount if group_tiers|length > 2 else '' }}" required>
  {% if group_tiers %}
  <p class="note">Last updated {{ group_tiers[0].set_at.strftime('%Y-%m-%d') }} by {{ group_tiers[0].set_by }}</p>
  {% endif %}
  <button type="submit">Save Tier Prices</button>
</form>
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd billing && python -m pytest tests/test_clients.py -v`
Expected: PASS — all tests in the file.

- [ ] **Step 6: Commit**

```bash
git add billing/main.py billing/templates/prices.html billing/tests/test_clients.py
git commit -m "feat: add group tier price management to billing /prices page"
```

---

### Task 3: Billing admin — free add/remove of ticket-raising groups on `client_detail.html`

**Files:**
- Modify: `billing/main.py` (add `POST /clients/{client_id}/ticket-groups/add`, `POST /clients/{client_id}/ticket-groups/remove`)
- Modify: `billing/templates/client_detail.html` (new "Ticket-raising groups" card)
- Test: `billing/tests/test_clients.py`

**Interfaces:**
- Consumes: `Client.allowed_ticket_groups`, `Client.ticket_group_tier_id`, `GroupTierPrice` (Task 1); `_get_or_seed_group_tiers(db) -> list[GroupTierPrice]` (Task 2, `billing/main.py`) — reused here to guarantee the 3 fixed tiers exist even if `/prices` was never opened first.
- Produces: `POST /clients/{client_id}/ticket-groups/add` (Form: `group_id`) — admin-only, no tier-limit check. First call for a client (when `allowed_ticket_groups is None`) initializes the list to `[]` and sets `ticket_group_tier_id` to the lowest tier (`min_groups=1`) before adding.
- Produces: `POST /clients/{client_id}/ticket-groups/remove` (Form: `group_id`).

- [ ] **Step 1: Write failing tests**

Append to `billing/tests/test_clients.py`:

```python
@pytest.mark.asyncio
async def test_admin_add_ticket_group_opts_in_and_sets_base_tier(auth_http, db_session):
    await auth_http.post("/clients", data={"name": "Acme", "subdomain": "acme-groups", "plan": "monthly"})
    from models import Client
    from sqlalchemy import select
    client = await db_session.scalar(select(Client).where(Client.subdomain == "acme-groups"))
    assert client.allowed_ticket_groups is None

    r = await auth_http.post(f"/clients/{client.id}/ticket-groups/add", data={"group_id": "120363111@g.us"})
    assert r.status_code in (200, 303)
    await db_session.refresh(client)
    import json
    assert json.loads(client.allowed_ticket_groups) == ["120363111@g.us"]
    assert client.ticket_group_tier_id is not None
    from models import GroupTierPrice
    tier = await db_session.get(GroupTierPrice, client.ticket_group_tier_id)
    assert tier.min_groups == 1


@pytest.mark.asyncio
async def test_admin_add_duplicate_group_is_noop(auth_http, db_session):
    await auth_http.post("/clients", data={"name": "Acme", "subdomain": "acme-dup", "plan": "monthly"})
    from models import Client
    from sqlalchemy import select
    client = await db_session.scalar(select(Client).where(Client.subdomain == "acme-dup"))
    await auth_http.post(f"/clients/{client.id}/ticket-groups/add", data={"group_id": "g1@g.us"})
    await auth_http.post(f"/clients/{client.id}/ticket-groups/add", data={"group_id": "g1@g.us"})
    await db_session.refresh(client)
    import json
    assert json.loads(client.allowed_ticket_groups) == ["g1@g.us"]


@pytest.mark.asyncio
async def test_admin_add_beyond_tier_limit_is_unrestricted(auth_http, db_session):
    """Billing admin bypasses the tier limit entirely — 6 groups on a 1-5 tier is allowed."""
    await auth_http.post("/clients", data={"name": "Acme", "subdomain": "acme-many", "plan": "monthly"})
    from models import Client
    from sqlalchemy import select
    client = await db_session.scalar(select(Client).where(Client.subdomain == "acme-many"))
    for i in range(6):
        r = await auth_http.post(f"/clients/{client.id}/ticket-groups/add", data={"group_id": f"g{i}@g.us"})
        assert r.status_code in (200, 303)
    await db_session.refresh(client)
    import json
    assert len(json.loads(client.allowed_ticket_groups)) == 6


@pytest.mark.asyncio
async def test_admin_remove_ticket_group(auth_http, db_session):
    await auth_http.post("/clients", data={"name": "Acme", "subdomain": "acme-rm", "plan": "monthly"})
    from models import Client
    from sqlalchemy import select
    client = await db_session.scalar(select(Client).where(Client.subdomain == "acme-rm"))
    await auth_http.post(f"/clients/{client.id}/ticket-groups/add", data={"group_id": "g1@g.us"})
    await auth_http.post(f"/clients/{client.id}/ticket-groups/add", data={"group_id": "g2@g.us"})
    r = await auth_http.post(f"/clients/{client.id}/ticket-groups/remove", data={"group_id": "g1@g.us"})
    assert r.status_code in (200, 303)
    await db_session.refresh(client)
    import json
    assert json.loads(client.allowed_ticket_groups) == ["g2@g.us"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd billing && python -m pytest tests/test_clients.py -v -k "ticket_group"`
Expected: FAIL — routes don't exist yet (404).

- [ ] **Step 3: Add the two routes**

In `billing/main.py`, add after `update_client` (after its closing `return RedirectResponse(...)`):

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
    groups = json.loads(client.allowed_ticket_groups) if client.allowed_ticket_groups else []
    if client.ticket_group_tier_id is None:
        # _get_or_seed_group_tiers (Task 2) guarantees the 3 fixed tiers exist —
        # a fresh install may reach this admin route before anyone has opened
        # /prices, so the lookup can't assume the rows are already there.
        base_tier = (await _get_or_seed_group_tiers(db))[0]
        client.ticket_group_tier_id = base_tier.id
    if group_id and group_id not in groups:
        groups.append(group_id)
    client.allowed_ticket_groups = json.dumps(groups)
    await db.commit()
    return RedirectResponse(f"/clients/{client_id}", status_code=303)


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
    groups = json.loads(client.allowed_ticket_groups) if client.allowed_ticket_groups else []
    groups = [g for g in groups if g != group_id.strip()]
    client.allowed_ticket_groups = json.dumps(groups)
    await db.commit()
    return RedirectResponse(f"/clients/{client_id}", status_code=303)
```

- [ ] **Step 4: Update `client_detail.html`**

In `billing/templates/client_detail.html`, add a new card after the "Actions" card (after its closing `</div>`, before the "Payment History" card):

```html
<div class="card">
  <h3 style="margin-top:0">Ticket-Raising Groups</h3>
  {% if client.allowed_ticket_groups %}
  <table>
    <tr><th>Group ID</th><th></th></tr>
    {% for gid in client.allowed_ticket_groups | fromjson %}
    <tr>
      <td>{{ gid }}</td>
      <td>
        <form method="post" action="/clients/{{ client.id }}/ticket-groups/remove" style="display:inline">
          <input type="hidden" name="group_id" value="{{ gid }}">
          <button class="action danger" type="submit">Remove</button>
        </form>
      </td>
    </tr>
    {% endfor %}
  </table>
  {% else %}
  <p style="color:#666">Unrestricted &mdash; any group can raise tickets. Add a group below to opt this client into tiered limits.</p>
  {% endif %}
  <form method="post" action="/clients/{{ client.id }}/ticket-groups/add" style="margin-top:.75rem">
    <input name="group_id" placeholder="120363XXXXXXXXXX@g.us" required>
    <button class="action" type="submit">Add Group</button>
  </form>
</div>
```

`fromjson` is not a built-in Jinja2 filter — register it. In `billing/main.py`, after `templates = Jinja2Templates(...)`:

```python
templates.env.filters["fromjson"] = json.loads
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd billing && python -m pytest tests/test_clients.py -v`
Expected: PASS — all tests in the file.

- [ ] **Step 6: Commit**

```bash
git add billing/main.py billing/templates/client_detail.html billing/tests/test_clients.py
git commit -m "feat: add billing-admin free add/remove of ticket-raising groups"
```

---

### Task 4: Cross-service read + backend enforcement in `ingest()`

**Files:**
- Modify: `billing/main.py` (add `GET /api/clients/{subdomain}/ticket-groups`)
- Modify: `backend/main.py` (add `_get_allowed_ticket_groups()`; gate `ingest()`)
- Test: `billing/tests/test_clients.py`, `backend/tests/test_billing_forward.py`

**Interfaces:**
- Produces (billing): `GET /api/clients/{subdomain}/ticket-groups` → `{"allowed_groups": list[str] | None, "tier_limit": int | None}`.
- Produces (backend): `async def _get_allowed_ticket_groups() -> Optional[list[str]]` — `None` means unrestricted (fail-open default). Module globals: `_ticket_groups_cache: dict | None = None` (reuses `_CACHE_TTL_SECONDS`).
- Consumes (backend): inserted into `ingest()` right after the existing `msg_type` filter (`backend/main.py:773-774`) and the `SUPERUSERS_GROUP_ID` sales-agent branch (`backend/main.py:777-787`), before `group_name` is resolved — applies once, to both `chat` and media messages, without duplicating the check the way `_get_client_billing_status()` currently is duplicated across the two branches.

- [ ] **Step 1: Write failing billing test**

Append to `billing/tests/test_clients.py`:

```python
@pytest.mark.asyncio
async def test_ticket_groups_endpoint_unrestricted_client(auth_http, db_session):
    await auth_http.post("/clients", data={"name": "Acme", "subdomain": "acme-unrestricted", "plan": "monthly"})
    r = await auth_http.get("/api/clients/acme-unrestricted/ticket-groups")
    assert r.status_code == 200
    assert r.json() == {"allowed_groups": None, "tier_limit": None}


@pytest.mark.asyncio
async def test_ticket_groups_endpoint_restricted_client(auth_http, db_session):
    await auth_http.post("/clients", data={"name": "Acme", "subdomain": "acme-restricted", "plan": "monthly"})
    from models import Client
    from sqlalchemy import select
    client = await db_session.scalar(select(Client).where(Client.subdomain == "acme-restricted"))
    await auth_http.post(f"/clients/{client.id}/ticket-groups/add", data={"group_id": "g1@g.us"})
    r = await auth_http.get("/api/clients/acme-restricted/ticket-groups")
    assert r.status_code == 200
    body = r.json()
    assert body["allowed_groups"] == ["g1@g.us"]
    assert body["tier_limit"] == 5
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd billing && python -m pytest tests/test_clients.py -v -k "ticket_groups_endpoint"`
Expected: FAIL — 404, route doesn't exist.

- [ ] **Step 3: Add the billing endpoint**

In `billing/main.py`, add after `client_billing_status` (`GET /api/clients/{subdomain}/status`):

```python
@app.get("/api/clients/{subdomain}/ticket-groups")
async def client_ticket_groups(subdomain: str, request: Request, db=Depends(get_db)):
    secret = request.headers.get("X-Billing-Secret", "")
    if BILLING_WEBHOOK_SECRET and secret != BILLING_WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")
    client = await db.scalar(select(Client).where(Client.subdomain == subdomain))
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")
    tier = await db.get(GroupTierPrice, client.ticket_group_tier_id) if client.ticket_group_tier_id else None
    return {
        "allowed_groups": json.loads(client.allowed_ticket_groups) if client.allowed_ticket_groups else None,
        "tier_limit": tier.max_groups if tier else None,
    }
```

- [ ] **Step 4: Run billing test to verify it passes**

Run: `cd billing && python -m pytest tests/test_clients.py -v -k "ticket_groups_endpoint"`
Expected: PASS.

- [ ] **Step 5: Write failing backend tests**

Append to `backend/tests/test_billing_forward.py`:

```python
@pytest.mark.asyncio
async def test_group_not_licensed_blocks_non_allowed_group(monkeypatch):
    monkeypatch.setenv("BILLING_SERVICE_URL", "http://billing:9000")
    monkeypatch.setenv("BILLING_WEBHOOK_SECRET", "test-secret")
    monkeypatch.setenv("CLIENT_SUBDOMAIN", "acme")
    monkeypatch.setenv("GATEWAY_SECRET_TOKEN", GATEWAY_TOKEN)
    import importlib, main as backend_main
    from tests.conftest import _TestSession
    from database import get_db
    importlib.reload(backend_main)
    backend_main._billing_status_cache = None
    backend_main._ticket_groups_cache = None

    async def _override_get_db():
        async with _TestSession() as session:
            yield session

    backend_main.app.dependency_overrides[get_db] = _override_get_db

    async with AsyncClient(transport=ASGITransport(app=backend_main.app), base_url="http://test") as c:
        mock_billing_client = AsyncMock()
        mock_billing_client.__aenter__ = AsyncMock(return_value=mock_billing_client)
        mock_billing_client.__aexit__ = AsyncMock(return_value=None)

        def fake_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            if "ticket-groups" in url:
                resp.json = MagicMock(return_value={"allowed_groups": ["allowed@g.us"], "tier_limit": 5})
            else:
                resp.json = MagicMock(return_value={"status": "active"})
            return resp

        mock_billing_client.get = AsyncMock(side_effect=fake_get)
        mock_billing_client.post = AsyncMock(return_value=MagicMock(status_code=200))

        with patch("main.httpx.AsyncClient", return_value=mock_billing_client):
            r = await c.post(
                "/api/v1/ops/ingest",
                headers={"X-API-Key": GATEWAY_TOKEN},
                json={
                    "event": "message.received",
                    "data": {
                        "chatId": "not-allowed@g.us",
                        "isGroup": True,
                        "type": "chat",
                        "body": "Pipes are leaking",
                        "fromMe": False,
                        "id": "msg-gnl-1",
                        "timestamp": 1700000020,
                    },
                },
            )
    backend_main.app.dependency_overrides.clear()
    assert r.status_code == 202
    assert r.json().get("status") == "group_not_licensed"


@pytest.mark.asyncio
async def test_allowed_group_passes_through_gate(monkeypatch):
    monkeypatch.setenv("BILLING_SERVICE_URL", "http://billing:9000")
    monkeypatch.setenv("BILLING_WEBHOOK_SECRET", "test-secret")
    monkeypatch.setenv("CLIENT_SUBDOMAIN", "acme")
    monkeypatch.setenv("GATEWAY_SECRET_TOKEN", GATEWAY_TOKEN)
    import importlib, main as backend_main
    from tests.conftest import _TestSession
    from database import get_db
    importlib.reload(backend_main)
    backend_main._billing_status_cache = None
    backend_main._ticket_groups_cache = None

    async def _override_get_db():
        async with _TestSession() as session:
            yield session

    backend_main.app.dependency_overrides[get_db] = _override_get_db

    async with AsyncClient(transport=ASGITransport(app=backend_main.app), base_url="http://test") as c:
        mock_billing_client = AsyncMock()
        mock_billing_client.__aenter__ = AsyncMock(return_value=mock_billing_client)
        mock_billing_client.__aexit__ = AsyncMock(return_value=None)

        def fake_get(url, **kwargs):
            resp = MagicMock()
            resp.status_code = 200
            if "ticket-groups" in url:
                resp.json = MagicMock(return_value={"allowed_groups": ["allowed@g.us"], "tier_limit": 5})
            else:
                resp.json = MagicMock(return_value={"status": "active"})
            return resp

        mock_billing_client.get = AsyncMock(side_effect=fake_get)
        mock_billing_client.post = AsyncMock(return_value=MagicMock(status_code=200))

        with patch("main.httpx.AsyncClient", return_value=mock_billing_client):
            r = await c.post(
                "/api/v1/ops/ingest",
                headers={"X-API-Key": GATEWAY_TOKEN},
                json={
                    "event": "message.received",
                    "data": {
                        "chatId": "allowed@g.us",
                        "isGroup": True,
                        "type": "chat",
                        "body": "Pipes are leaking badly in block A",
                        "fromMe": False,
                        "id": "msg-gnl-2",
                        "timestamp": 1700000021,
                    },
                },
            )
    backend_main.app.dependency_overrides.clear()
    assert r.status_code == 202
    assert r.json().get("status") != "group_not_licensed"


@pytest.mark.asyncio
async def test_ticket_groups_gate_fails_open_on_error(monkeypatch):
    monkeypatch.setenv("BILLING_SERVICE_URL", "http://billing:9000")
    monkeypatch.setenv("BILLING_WEBHOOK_SECRET", "test-secret")
    monkeypatch.setenv("CLIENT_SUBDOMAIN", "acme")
    monkeypatch.setenv("GATEWAY_SECRET_TOKEN", GATEWAY_TOKEN)
    import importlib, main as backend_main
    from tests.conftest import _TestSession
    from database import get_db
    importlib.reload(backend_main)
    backend_main._billing_status_cache = None
    backend_main._ticket_groups_cache = None

    async def _override_get_db():
        async with _TestSession() as session:
            yield session

    backend_main.app.dependency_overrides[get_db] = _override_get_db

    async with AsyncClient(transport=ASGITransport(app=backend_main.app), base_url="http://test") as c:
        mock_billing_client = AsyncMock()
        mock_billing_client.__aenter__ = AsyncMock(return_value=mock_billing_client)
        mock_billing_client.__aexit__ = AsyncMock(return_value=None)
        mock_billing_client.get = AsyncMock(side_effect=Exception("Connection refused"))
        mock_billing_client.post = AsyncMock(return_value=MagicMock(status_code=200))

        with patch("main.httpx.AsyncClient", return_value=mock_billing_client):
            r = await c.post(
                "/api/v1/ops/ingest",
                headers={"X-API-Key": GATEWAY_TOKEN},
                json={
                    "event": "message.received",
                    "data": {
                        "chatId": "anygroup@g.us",
                        "isGroup": True,
                        "type": "chat",
                        "body": "water leak in room 3",
                        "fromMe": False,
                        "id": "msg-gnl-3",
                        "timestamp": 1700000022,
                    },
                },
            )
    backend_main.app.dependency_overrides.clear()
    assert r.status_code == 202
    assert r.json().get("status") != "group_not_licensed"
```

- [ ] **Step 6: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_billing_forward.py -v -k "group_not_licensed or allowed_group or ticket_groups_gate"`
Expected: FAIL — `_get_allowed_ticket_groups` doesn't exist, no gate in `ingest()`.

- [ ] **Step 7: Add `_get_allowed_ticket_groups()` and gate `ingest()`**

In `backend/main.py`, add the cache global next to `_billing_status_cache`:

```python
_billing_status_cache: dict | None = None
_ticket_groups_cache: dict | None = None
_CACHE_TTL_SECONDS = 60
```

Add the function after `_get_client_billing_status()`:

```python
async def _get_allowed_ticket_groups() -> Optional[list[str]]:
    """Returns the client's allowed ticket-raising group IDs, or None if unrestricted
    (today's default) or the billing service is unreachable/unconfigured (fail open)."""
    global _ticket_groups_cache
    if not BILLING_SERVICE_URL or not CLIENT_SUBDOMAIN:
        return None
    now = datetime.now(timezone.utc)
    if (
        _ticket_groups_cache is not None
        and (now - _ticket_groups_cache["fetched_at"]).total_seconds() < _CACHE_TTL_SECONDS
    ):
        return _ticket_groups_cache["allowed_groups"]
    try:
        async with httpx.AsyncClient(timeout=5.0) as http:
            r = await http.get(
                f"{BILLING_SERVICE_URL}/api/clients/{CLIENT_SUBDOMAIN}/ticket-groups",
                headers={"X-Billing-Secret": BILLING_WEBHOOK_SECRET},
            )
            r.raise_for_status()
            allowed_groups = r.json().get("allowed_groups")
    except Exception:
        logger.warning("Ticket-groups check failed — defaulting to unrestricted")
        return None
    _ticket_groups_cache = {"allowed_groups": allowed_groups, "fetched_at": now}
    return allowed_groups
```

In `ingest()`, insert the gate right after the `SUPERUSERS_GROUP_ID` branch and before `group_name` is resolved — replace:

```python
    group_id = chat_id
    # Sales agent — handle the superusers group as a sales/support channel
    if SUPERUSERS_GROUP_ID and group_id == SUPERUSERS_GROUP_ID:
        if not data.get("fromMe", False):
            msg_body = (data.get("body") or "").strip()
            if msg_body:
                try:
                    reply = await answer_sales_query(msg_body, f"sales:{group_id}", db)
                    await send_group_message(group_id, reply)
                except Exception as exc:
                    logger.error("Sales agent reply failed: %s", exc)
        return JSONResponse({"status": "sales_handled"}, status_code=status.HTTP_202_ACCEPTED)
    group_name = (
```

with:

```python
    group_id = chat_id
    # Sales agent — handle the superusers group as a sales/support channel
    if SUPERUSERS_GROUP_ID and group_id == SUPERUSERS_GROUP_ID:
        if not data.get("fromMe", False):
            msg_body = (data.get("body") or "").strip()
            if msg_body:
                try:
                    reply = await answer_sales_query(msg_body, f"sales:{group_id}", db)
                    await send_group_message(group_id, reply)
                except Exception as exc:
                    logger.error("Sales agent reply failed: %s", exc)
        return JSONResponse({"status": "sales_handled"}, status_code=status.HTTP_202_ACCEPTED)

    allowed_groups = await _get_allowed_ticket_groups()
    if allowed_groups is not None and group_id not in allowed_groups:
        return {"status": "group_not_licensed"}

    group_name = (
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_billing_forward.py -v`
Expected: PASS — all tests in the file.

- [ ] **Step 9: Run the full backend test suite to confirm no regressions**

Run: `cd backend && python -m pytest -v`
Expected: PASS — every existing `ingest()`/dashboard/media test still passes (default test env has no `BILLING_SERVICE_URL`, so `_get_allowed_ticket_groups()` fails open to `None`/unrestricted, matching today's behavior exactly).

- [ ] **Step 10: Commit**

```bash
git add billing/main.py backend/main.py billing/tests/test_clients.py backend/tests/test_billing_forward.py
git commit -m "feat: cross-service ticket-group allow-list enforcement in ingest()"
```

---

### Task 5: Client self-service tier-limited "add group"

**Files:**
- Modify: `billing/main.py` (add `POST /api/clients/{subdomain}/ticket-groups/add`)
- Modify: `backend/main.py` (add `GET /api/settings/ticket-groups`, `POST /api/settings/ticket-groups/add`)
- Modify: `backend/templates/settings.html` (new "Ticket-raising groups" section)
- Test: `billing/tests/test_clients.py`, new `backend/tests/test_settings_ticket_groups.py`

**Interfaces:**
- Consumes: `Client.allowed_ticket_groups`/`ticket_group_tier_id`, `GroupTierPrice` (Task 1); `_get_allowed_ticket_groups()` (Task 4).
- Produces (billing): `POST /api/clients/{subdomain}/ticket-groups/add` (`X-Billing-Secret`, JSON body `{"group_id": str}`) → on success `{"status": "ok", "added": bool}`; when it would exceed the tier, does **not** add, returns `{"status": "limit_reached", "next_tier_amount": str, "next_tier_max": int | None}`.
- Produces (backend): `GET /api/settings/ticket-groups` → `{"allowed_groups": list[str] | None, "tier_limit": int | None}` (proxies `_get_allowed_ticket_groups()`'s cached value plus a fresh `tier_limit` read — see Step 3). `POST /api/settings/ticket-groups/add` (JSON body: `{"group_id": str}`, via a `TicketGroupAddBody` model) — validates JID shape, proxies to billing, returns billing's JSON response verbatim.

- [ ] **Step 1: Write failing billing tests**

Append to `billing/tests/test_clients.py`:

```python
@pytest.mark.asyncio
async def test_self_service_add_under_limit_succeeds(auth_http, db_session, monkeypatch):
    monkeypatch.setattr(main, "BILLING_WEBHOOK_SECRET", "test-secret")
    await auth_http.post("/clients", data={"name": "Acme", "subdomain": "acme-self1", "plan": "monthly"})
    from models import Client
    from sqlalchemy import select
    client = await db_session.scalar(select(Client).where(Client.subdomain == "acme-self1"))
    await auth_http.post(f"/clients/{client.id}/ticket-groups/add", data={"group_id": "g1@g.us"})

    r = await auth_http.post(
        "/api/clients/acme-self1/ticket-groups/add",
        json={"group_id": "g2@g.us"},
        headers={"X-Billing-Secret": "test-secret"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["added"] is True
    await db_session.refresh(client)
    import json as _json
    assert "g2@g.us" in _json.loads(client.allowed_ticket_groups)


@pytest.mark.asyncio
async def test_self_service_add_duplicate_is_noop(auth_http, db_session, monkeypatch):
    monkeypatch.setattr(main, "BILLING_WEBHOOK_SECRET", "test-secret")
    await auth_http.post("/clients", data={"name": "Acme", "subdomain": "acme-self2", "plan": "monthly"})
    from models import Client
    from sqlalchemy import select
    client = await db_session.scalar(select(Client).where(Client.subdomain == "acme-self2"))
    await auth_http.post(f"/clients/{client.id}/ticket-groups/add", data={"group_id": "g1@g.us"})

    r = await auth_http.post(
        "/api/clients/acme-self2/ticket-groups/add",
        json={"group_id": "g1@g.us"},
        headers={"X-Billing-Secret": "test-secret"},
    )
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "added": False}


@pytest.mark.asyncio
async def test_self_service_add_beyond_limit_returns_limit_reached(auth_http, db_session, monkeypatch):
    monkeypatch.setattr(main, "BILLING_WEBHOOK_SECRET", "test-secret")
    await auth_http.post("/clients", data={"name": "Acme", "subdomain": "acme-self3", "plan": "monthly"})
    from models import Client
    from sqlalchemy import select
    client = await db_session.scalar(select(Client).where(Client.subdomain == "acme-self3"))
    for i in range(5):
        await auth_http.post(f"/clients/{client.id}/ticket-groups/add", data={"group_id": f"g{i}@g.us"})
    await auth_http.post("/prices/group-tiers", data={
        "tier1_amount": "500.00", "tier2_amount": "1200.00", "tier3_amount": "2500.00",
    })

    r = await auth_http.post(
        "/api/clients/acme-self3/ticket-groups/add",
        json={"group_id": "g-extra@g.us"},
        headers={"X-Billing-Secret": "test-secret"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "limit_reached"
    assert body["next_tier_amount"] == "1200.00"
    assert body["next_tier_max"] == 10
    await db_session.refresh(client)
    import json as _json
    assert len(_json.loads(client.allowed_ticket_groups)) == 5  # not added
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd billing && python -m pytest tests/test_clients.py -v -k "self_service"`
Expected: FAIL — 404, route doesn't exist.

- [ ] **Step 3: Add the billing endpoint**

In `billing/main.py`, add after `client_ticket_groups` (Task 4's `GET .../ticket-groups`):

```python
@app.post("/api/clients/{subdomain}/ticket-groups/add")
async def client_self_service_add_ticket_group(subdomain: str, request: Request, db=Depends(get_db)):
    secret = request.headers.get("X-Billing-Secret", "")
    if BILLING_WEBHOOK_SECRET and secret != BILLING_WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")
    client = await db.scalar(select(Client).where(Client.subdomain == subdomain))
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")
    body = await request.json()
    group_id = (body.get("group_id") or "").strip()
    if not group_id:
        raise HTTPException(status_code=400, detail="group_id required")

    groups = json.loads(client.allowed_ticket_groups) if client.allowed_ticket_groups else []
    if group_id in groups:
        return {"status": "ok", "added": False}

    tier = await db.get(GroupTierPrice, client.ticket_group_tier_id) if client.ticket_group_tier_id else None
    limit = tier.max_groups if tier else None
    if limit is not None and len(groups) + 1 > limit:
        next_tier = await db.scalar(
            select(GroupTierPrice)
            .where(GroupTierPrice.min_groups > limit)
            .order_by(GroupTierPrice.min_groups)
            .limit(1)
        )
        return {
            "status": "limit_reached",
            "next_tier_amount": str(next_tier.amount) if next_tier else None,
            "next_tier_max": next_tier.max_groups if next_tier else None,
        }

    groups.append(group_id)
    client.allowed_ticket_groups = json.dumps(groups)
    await db.commit()
    return {"status": "ok", "added": True}
```

- [ ] **Step 4: Run billing tests to verify they pass**

Run: `cd billing && python -m pytest tests/test_clients.py -v`
Expected: PASS — all tests in the file.

- [ ] **Step 5: Write failing backend tests**

Create `backend/tests/test_settings_ticket_groups.py`:

```python
import os
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from httpx import AsyncClient, ASGITransport

os.environ.setdefault("SECRET_KEY", "test-key-for-settings-ticket-groups")
os.environ.setdefault("TESTING", "1")

GATEWAY_TOKEN = "ops-gateway-secret-2026"


@pytest.fixture
def admin_client(monkeypatch):
    monkeypatch.setenv("BILLING_SERVICE_URL", "http://billing:9000")
    monkeypatch.setenv("BILLING_WEBHOOK_SECRET", "test-secret")
    monkeypatch.setenv("CLIENT_SUBDOMAIN", "acme")
    monkeypatch.setenv("GATEWAY_SECRET_TOKEN", GATEWAY_TOKEN)
    import importlib, main as backend_main
    from tests.conftest import _TestSession, _HASHED_TESTPASS
    from database import get_db
    from models import User
    from datetime import datetime, timezone
    importlib.reload(backend_main)
    backend_main._billing_status_cache = None
    backend_main._ticket_groups_cache = None

    async def _override_get_db():
        async with _TestSession() as session:
            yield session

    backend_main.app.dependency_overrides[get_db] = _override_get_db
    return backend_main


@pytest.mark.asyncio
async def test_settings_ticket_groups_get_proxies_billing(admin_client, monkeypatch):
    from tests.conftest import _TestSession, _HASHED_TESTPASS
    from models import User
    from datetime import datetime, timezone

    async with _TestSession() as session:
        session.add(User(
            username="settingsadmin", hashed_password=_HASHED_TESTPASS,
            created_at=datetime.now(timezone.utc), created_by=None, role="admin",
        ))
        await session.commit()

    mock_billing_client = AsyncMock()
    mock_billing_client.__aenter__ = AsyncMock(return_value=mock_billing_client)
    mock_billing_client.__aexit__ = AsyncMock(return_value=None)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json = MagicMock(return_value={"allowed_groups": ["g1@g.us"], "tier_limit": 5})
    mock_billing_client.get = AsyncMock(return_value=mock_resp)

    async with AsyncClient(transport=ASGITransport(app=admin_client.app), base_url="http://test") as c:
        await c.post("/login", data={"username": "settingsadmin", "password": "testpass"})
        with patch("main.httpx.AsyncClient", return_value=mock_billing_client):
            r = await c.get("/api/settings/ticket-groups")
    admin_client.app.dependency_overrides.clear()
    assert r.status_code == 200
    assert r.json() == {"allowed_groups": ["g1@g.us"], "tier_limit": 5}


@pytest.mark.asyncio
async def test_settings_ticket_groups_add_rejects_malformed_id(admin_client):
    from tests.conftest import _TestSession, _HASHED_TESTPASS
    from models import User
    from datetime import datetime, timezone

    async with _TestSession() as session:
        session.add(User(
            username="settingsadmin2", hashed_password=_HASHED_TESTPASS,
            created_at=datetime.now(timezone.utc), created_by=None, role="admin",
        ))
        await session.commit()

    async with AsyncClient(transport=ASGITransport(app=admin_client.app), base_url="http://test") as c:
        await c.post("/login", data={"username": "settingsadmin2", "password": "testpass"})
        r = await c.post("/api/settings/ticket-groups/add", json={"group_id": "not-a-jid"})
    admin_client.app.dependency_overrides.clear()
    assert r.status_code == 422


@pytest.mark.asyncio
async def test_settings_ticket_groups_add_proxies_to_billing(admin_client):
    from tests.conftest import _TestSession, _HASHED_TESTPASS
    from models import User
    from datetime import datetime, timezone

    async with _TestSession() as session:
        session.add(User(
            username="settingsadmin3", hashed_password=_HASHED_TESTPASS,
            created_at=datetime.now(timezone.utc), created_by=None, role="admin",
        ))
        await session.commit()

    mock_billing_client = AsyncMock()
    mock_billing_client.__aenter__ = AsyncMock(return_value=mock_billing_client)
    mock_billing_client.__aexit__ = AsyncMock(return_value=None)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json = MagicMock(return_value={"status": "ok", "added": True})
    mock_billing_client.post = AsyncMock(return_value=mock_resp)

    async with AsyncClient(transport=ASGITransport(app=admin_client.app), base_url="http://test") as c:
        await c.post("/login", data={"username": "settingsadmin3", "password": "testpass"})
        with patch("main.httpx.AsyncClient", return_value=mock_billing_client):
            r = await c.post("/api/settings/ticket-groups/add", json={"group_id": "120363111@g.us"})
    admin_client.app.dependency_overrides.clear()
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "added": True}
    mock_billing_client.post.assert_called_once()
    call = mock_billing_client.post.call_args
    assert "ticket-groups/add" in call.args[0]
    assert call.kwargs["json"] == {"group_id": "120363111@g.us"}
```

- [ ] **Step 6: Run tests to verify they fail**

Run: `cd backend && python -m pytest tests/test_settings_ticket_groups.py -v`
Expected: FAIL — routes don't exist yet.

- [ ] **Step 7: Add the backend proxy endpoints**

`backend/main.py` has no `Form` import. Its established convention for JSON POST bodies is a `BaseModel` parameter (see `GroupAssignBody` at `backend/main.py:62-63`, used by the `/users/{user_id}/groups` endpoint) — follow that, not `Form(...)`.

Add near the other request-body models (after `GroupAssignBody`, `backend/main.py:62-64`):

```python
class TicketGroupAddBody(BaseModel):
    group_id: str
```

`import re` is already present at the top (`backend/main.py:5`). Add after the existing `/api/settings/whatsapp-qr` route:

```python
_GROUP_JID_RE = re.compile(r"[\w-]+@g\.us")


@app.get("/api/settings/ticket-groups")
async def settings_ticket_groups(
    username: str = Depends(require_admin),
):
    allowed_groups = await _get_allowed_ticket_groups()
    tier_limit = None
    if BILLING_SERVICE_URL and CLIENT_SUBDOMAIN:
        try:
            async with httpx.AsyncClient(timeout=5.0) as http:
                r = await http.get(
                    f"{BILLING_SERVICE_URL}/api/clients/{CLIENT_SUBDOMAIN}/ticket-groups",
                    headers={"X-Billing-Secret": BILLING_WEBHOOK_SECRET},
                )
                r.raise_for_status()
                tier_limit = r.json().get("tier_limit")
        except Exception:
            logger.warning("Ticket-groups tier-limit fetch failed")
    return {"allowed_groups": allowed_groups, "tier_limit": tier_limit}


@app.post("/api/settings/ticket-groups/add")
async def settings_add_ticket_group(
    body: TicketGroupAddBody,
    username: str = Depends(require_admin),
):
    group_id = body.group_id.strip()
    if not _GROUP_JID_RE.fullmatch(group_id):
        raise HTTPException(status_code=422, detail="group_id doesn't look like a WhatsApp group JID")
    if not BILLING_SERVICE_URL or not CLIENT_SUBDOMAIN:
        raise HTTPException(status_code=503, detail="Billing service not configured")
    async with httpx.AsyncClient(timeout=8.0) as http:
        r = await http.post(
            f"{BILLING_SERVICE_URL}/api/clients/{CLIENT_SUBDOMAIN}/ticket-groups/add",
            json={"group_id": group_id},
            headers={"X-Billing-Secret": BILLING_WEBHOOK_SECRET},
        )
        r.raise_for_status()
        return r.json()
```

`settings_ticket_groups` calls `_get_allowed_ticket_groups()` (cached, may be up to 60s stale) for `allowed_groups` but fetches `tier_limit` fresh each time — this mirrors the spec's "next cache refresh (≤60s) picks it up" note without adding a second cached global.

`HTTPException` and `BaseModel` are already imported in `backend/main.py` (used throughout the file).

- [ ] **Step 8: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_settings_ticket_groups.py -v`
Expected: PASS — all 3 tests.

- [ ] **Step 9: Update `settings.html`**

In `backend/templates/settings.html`, add a new section after the "WhatsApp Connection" section (after its closing `</div>` at the end of that `.section`, before the closing `</div>` of `.page`):

```html
  <div class="section">
    <div class="section-title">Ticket-Raising Groups</div>
    <div id="ticket-groups-list" style="font-size:.875rem;color:var(--muted)">Loading&hellip;</div>
    <div style="margin-top:1rem;display:flex;gap:.5rem;flex-wrap:wrap">
      <input id="new-group-id" placeholder="120363XXXXXXXXXX@g.us"
        style="flex:1;min-width:220px;padding:.5rem .75rem;border:1px solid var(--line);border-radius:8px;background:var(--surface-2);color:var(--text)">
      <button class="btn btn-primary" onclick="addTicketGroup()">Add Group</button>
    </div>
    <div id="ticket-groups-msg" style="margin-top:.75rem;font-size:.85rem"></div>
  </div>
```

Add to the `<script>` block (before the trailing `checkStatus(); setInterval(...)` calls):

```javascript
async function loadTicketGroups() {
  const el = document.getElementById('ticket-groups-list');
  try {
    const data = await fetch('/api/settings/ticket-groups').then(r => r.json());
    if (data.allowed_groups === null) {
      el.textContent = 'Unrestricted — any group can raise tickets.';
      return;
    }
    const limit = data.tier_limit != null ? ` (limit: ${data.tier_limit})` : '';
    el.innerHTML = data.allowed_groups.length
      ? `<ul style="margin:0;padding-left:1.25rem">${data.allowed_groups.map(g => `<li>${g}</li>`).join('')}</ul>${limit}`
      : `No groups configured yet${limit}`;
  } catch(e) {
    el.textContent = 'Could not load ticket-raising groups.';
  }
}

async function addTicketGroup() {
  const input = document.getElementById('new-group-id');
  const msg = document.getElementById('ticket-groups-msg');
  const groupId = input.value.trim();
  if (!groupId) return;
  msg.textContent = '';
  try {
    const res = await fetch('/api/settings/ticket-groups/add', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ group_id: groupId }),
    });
    const data = await res.json();
    if (!res.ok) {
      msg.style.color = 'var(--red)';
      msg.textContent = data.detail || 'Could not add group.';
      return;
    }
    if (data.status === 'limit_reached') {
      msg.style.color = 'var(--amber)';
      msg.textContent = `You've reached your plan's limit (${data.next_tier_max ?? 'current'} groups) — upgrade for KES ${data.next_tier_amount} to add more.`;
      return;
    }
    msg.style.color = 'var(--green)';
    msg.textContent = data.added ? 'Group added.' : 'Group already on your list.';
    input.value = '';
    loadTicketGroups();
  } catch(e) {
    msg.style.color = 'var(--red)';
    msg.textContent = 'Request failed.';
  }
}

loadTicketGroups();
```

- [ ] **Step 10: Commit**

```bash
git add billing/main.py backend/main.py backend/templates/settings.html billing/tests/test_clients.py backend/tests/test_settings_ticket_groups.py
git commit -m "feat: client self-service tier-limited ticket-group add via /settings"
```

---

### Task 6: M-Pesa tier-upgrade payment flow

**Files:**
- Modify: `billing/main.py` (add `POST /api/clients/{subdomain}/ticket-groups/upgrade`; extend `mpesa_callback`)
- Modify: `backend/main.py` (add `POST /api/settings/ticket-groups/upgrade`)
- Modify: `backend/templates/settings.html` (upgrade button wired to the `limit_reached` response)
- Test: `billing/tests/test_payment_flow.py`, `backend/tests/test_settings_ticket_groups.py`

**Interfaces:**
- Consumes: `GroupUpgradeRequest` (Task 1), `initiate_stk_push()` (`billing/mpesa.py:46`), `GroupTierPrice` (Task 1).
- Produces (billing): `POST /api/clients/{subdomain}/ticket-groups/upgrade` (`X-Billing-Secret`, JSON body `{"group_id": str, "phone": str}`) → `{"status": "stk_sent"}` on new STK push, `{"status": "pending_exists"}` if a `GroupUpgradeRequest` with `status="pending"` already exists for this client.
- Produces (billing): `mpesa_callback` gains a second lookup branch — after the existing `PaymentSession` lookup finds nothing, look up `GroupUpgradeRequest` by `checkout_request_id`; on `ResultCode == 0`, append `group_id` to `allowed_ticket_groups` and set `ticket_group_tier_id = target_tier_id` in one commit, mark the request `"confirmed"`; otherwise mark it `"failed"`.
- Produces (backend): `POST /api/settings/ticket-groups/upgrade` (JSON body: `{"group_id": str, "phone": str}`, via a `TicketGroupUpgradeBody` model) — proxies to the billing upgrade endpoint.

- [ ] **Step 1: Write failing billing tests**

Append to `billing/tests/test_payment_flow.py`:

```python
@pytest.mark.asyncio
async def test_upgrade_endpoint_triggers_stk_push(http, grace_client, db_session, monkeypatch):
    # BILLING_WEBHOOK_SECRET is read into a module-level constant at import time,
    # so monkeypatch.setenv (which only changes os.environ) has no effect on code
    # that already captured the old value — patch the constant on `main` directly.
    monkeypatch.setattr(main, "BILLING_WEBHOOK_SECRET", "test-secret")
    from models import GroupTierPrice
    from sqlalchemy import select
    from datetime import datetime, timezone
    from decimal import Decimal
    tier1 = GroupTierPrice(min_groups=1, max_groups=5, amount=Decimal("500"), set_at=datetime.now(timezone.utc), set_by="admin")
    tier2 = GroupTierPrice(min_groups=6, max_groups=10, amount=Decimal("1200"), set_at=datetime.now(timezone.utc), set_by="admin")
    db_session.add_all([tier1, tier2])
    await db_session.flush()
    grace_client.allowed_ticket_groups = "[]"
    grace_client.ticket_group_tier_id = tier1.id
    await db_session.commit()

    r = await http.post(
        "/api/clients/acme/ticket-groups/upgrade",
        json={"group_id": "g-new@g.us", "phone": "0712345678"},
        headers={"X-Billing-Secret": "test-secret"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "stk_sent"
    main.initiate_stk_push.assert_called_once()
    from models import GroupUpgradeRequest
    reqs = (await db_session.execute(select(GroupUpgradeRequest))).scalars().all()
    assert len(reqs) == 1
    assert reqs[0].status == "pending"
    assert reqs[0].checkout_request_id == "ws_CO_TEST_123"


@pytest.mark.asyncio
async def test_upgrade_endpoint_reuses_pending_request(http, grace_client, db_session, monkeypatch):
    monkeypatch.setattr(main, "BILLING_WEBHOOK_SECRET", "test-secret")
    from models import GroupTierPrice, GroupUpgradeRequest
    from datetime import datetime, timezone
    from decimal import Decimal
    tier1 = GroupTierPrice(min_groups=1, max_groups=5, amount=Decimal("500"), set_at=datetime.now(timezone.utc), set_by="admin")
    db_session.add(tier1)
    await db_session.flush()
    grace_client.allowed_ticket_groups = "[]"
    grace_client.ticket_group_tier_id = tier1.id
    existing = GroupUpgradeRequest(
        client_id=grace_client.id, group_id="g-new@g.us", target_tier_id=tier1.id,
        phone="0712345678", amount=Decimal("500"), checkout_request_id="ws_CO_EXISTING",
        status="pending", created_at=datetime.now(timezone.utc),
    )
    db_session.add(existing)
    await db_session.commit()

    r = await http.post(
        "/api/clients/acme/ticket-groups/upgrade",
        json={"group_id": "g-new@g.us", "phone": "0712345678"},
        headers={"X-Billing-Secret": "test-secret"},
    )
    assert r.status_code == 200
    assert r.json()["status"] == "pending_exists"
    main.initiate_stk_push.assert_not_called()


@pytest.mark.asyncio
async def test_mpesa_callback_confirms_group_upgrade(http, grace_client, db_session, monkeypatch):
    monkeypatch.setattr(main, "BILLING_WEBHOOK_SECRET", "test-secret")
    from models import GroupTierPrice, GroupUpgradeRequest
    from datetime import datetime, timezone
    from decimal import Decimal
    tier2 = GroupTierPrice(min_groups=6, max_groups=10, amount=Decimal("1200"), set_at=datetime.now(timezone.utc), set_by="admin")
    db_session.add(tier2)
    await db_session.flush()
    grace_client.allowed_ticket_groups = "[]"
    req = GroupUpgradeRequest(
        client_id=grace_client.id, group_id="g-new@g.us", target_tier_id=tier2.id,
        phone="254712345678", amount=Decimal("1200"), checkout_request_id="ws_CO_UPGRADE_1",
        status="pending", created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    await db_session.commit()

    r = await http.post("/webhook/mpesa", json={
        "Body": {"stkCallback": {
            "CheckoutRequestID": "ws_CO_UPGRADE_1",
            "ResultCode": 0,
            "CallbackMetadata": {"Item": [{"Name": "MpesaReceiptNumber", "Value": "QGH8XXXXX"}]},
        }}
    })
    assert r.status_code == 200
    await db_session.refresh(grace_client)
    await db_session.refresh(req)
    import json as _json
    assert _json.loads(grace_client.allowed_ticket_groups) == ["g-new@g.us"]
    assert grace_client.ticket_group_tier_id == tier2.id
    assert req.status == "confirmed"
    # Renewal state machine must be untouched
    assert grace_client.status == "grace"


@pytest.mark.asyncio
async def test_mpesa_callback_marks_group_upgrade_failed(http, grace_client, db_session, monkeypatch):
    monkeypatch.setattr(main, "BILLING_WEBHOOK_SECRET", "test-secret")
    from models import GroupTierPrice, GroupUpgradeRequest
    from datetime import datetime, timezone
    from decimal import Decimal
    tier2 = GroupTierPrice(min_groups=6, max_groups=10, amount=Decimal("1200"), set_at=datetime.now(timezone.utc), set_by="admin")
    db_session.add(tier2)
    await db_session.flush()
    grace_client.allowed_ticket_groups = "[]"
    req = GroupUpgradeRequest(
        client_id=grace_client.id, group_id="g-new@g.us", target_tier_id=tier2.id,
        phone="254712345678", amount=Decimal("1200"), checkout_request_id="ws_CO_UPGRADE_FAIL",
        status="pending", created_at=datetime.now(timezone.utc),
    )
    db_session.add(req)
    await db_session.commit()

    r = await http.post("/webhook/mpesa", json={
        "Body": {"stkCallback": {"CheckoutRequestID": "ws_CO_UPGRADE_FAIL", "ResultCode": 1032}}
    })
    assert r.status_code == 200
    await db_session.refresh(req)
    await db_session.refresh(grace_client)
    assert req.status == "failed"
    assert grace_client.allowed_ticket_groups == "[]"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd billing && python -m pytest tests/test_payment_flow.py -v -k "upgrade or group_upgrade"`
Expected: FAIL — route doesn't exist, `mpesa_callback` doesn't recognize the checkout ID.

- [ ] **Step 3: Add the upgrade-trigger endpoint**

In `billing/main.py`, add after `client_self_service_add_ticket_group` (Task 5):

```python
@app.post("/api/clients/{subdomain}/ticket-groups/upgrade")
async def client_self_service_upgrade_tier(subdomain: str, request: Request, db=Depends(get_db)):
    secret = request.headers.get("X-Billing-Secret", "")
    if BILLING_WEBHOOK_SECRET and secret != BILLING_WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")
    client = await db.scalar(select(Client).where(Client.subdomain == subdomain))
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")
    body = await request.json()
    group_id = (body.get("group_id") or "").strip()
    phone = _normalize_phone(body.get("phone") or "")
    if not group_id or not phone:
        raise HTTPException(status_code=400, detail="group_id and a valid phone are required")

    pending = await db.scalar(
        select(GroupUpgradeRequest).where(
            GroupUpgradeRequest.client_id == client.id,
            GroupUpgradeRequest.status == "pending",
        )
    )
    if pending:
        return {"status": "pending_exists", "checkout_request_id": pending.checkout_request_id}

    groups = json.loads(client.allowed_ticket_groups) if client.allowed_ticket_groups else []
    current_tier = await db.get(GroupTierPrice, client.ticket_group_tier_id) if client.ticket_group_tier_id else None
    if current_tier and current_tier.max_groups is None:
        raise HTTPException(status_code=400, detail="Already on the highest tier")
    current_limit = current_tier.max_groups if current_tier else 0
    next_tier = await db.scalar(
        select(GroupTierPrice)
        .where(GroupTierPrice.min_groups > current_limit)
        .order_by(GroupTierPrice.min_groups)
        .limit(1)
    )
    if not next_tier:
        raise HTTPException(status_code=400, detail="Already on the highest tier")

    upgrade_req = GroupUpgradeRequest(
        client_id=client.id, group_id=group_id, target_tier_id=next_tier.id,
        phone=phone, amount=next_tier.amount, created_at=datetime.now(timezone.utc),
    )
    db.add(upgrade_req)
    await db.flush()

    try:
        stk = await initiate_stk_push(
            phone=phone, amount=next_tier.amount,
            account_ref=f"{client.subdomain}-tier-upgrade",
            callback_url=f"{MPESA_CALLBACK_BASE_URL}/webhook/mpesa",
        )
    except Exception as exc:
        import logging as _log
        _log.getLogger(__name__).error("Tier-upgrade STK Push failed for %s: %s", client.subdomain, exc)
        upgrade_req.status = "failed"
        await db.commit()
        raise HTTPException(status_code=502, detail="M-Pesa payment request failed")

    upgrade_req.checkout_request_id = stk.get("CheckoutRequestID")
    await db.commit()
    return {"status": "stk_sent"}
```

`_normalize_phone` is the existing helper already used by `_process_client_message`'s `/payment` flow — reuse it, no reimplementation.

- [ ] **Step 4: Extend `mpesa_callback`**

In `billing/main.py`, in `mpesa_callback`, replace:

```python
    session = await db.scalar(
        select(PaymentSession).where(PaymentSession.checkout_request_id == checkout_id)
    )
    if not session:
        return {"ResultCode": 0, "ResultDesc": "Accepted"}
```

with:

```python
    session = await db.scalar(
        select(PaymentSession).where(PaymentSession.checkout_request_id == checkout_id)
    )
    if not session:
        upgrade_req = await db.scalar(
            select(GroupUpgradeRequest).where(GroupUpgradeRequest.checkout_request_id == checkout_id)
        )
        if not upgrade_req:
            return {"ResultCode": 0, "ResultDesc": "Accepted"}
        if result_code == 0:
            client = await db.get(Client, upgrade_req.client_id)
            groups = json.loads(client.allowed_ticket_groups) if client.allowed_ticket_groups else []
            if upgrade_req.group_id not in groups:
                groups.append(upgrade_req.group_id)
            client.allowed_ticket_groups = json.dumps(groups)
            client.ticket_group_tier_id = upgrade_req.target_tier_id
            upgrade_req.status = "confirmed"
        else:
            upgrade_req.status = "failed"
        await db.commit()
        return {"ResultCode": 0, "ResultDesc": "Accepted"}
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `cd billing && python -m pytest tests/test_payment_flow.py -v`
Expected: PASS — all tests in the file.

- [ ] **Step 6: Run the full billing test suite to confirm no regressions**

Run: `cd billing && python -m pytest -v`
Expected: PASS.

- [ ] **Step 7: Write failing backend test**

Append to `backend/tests/test_settings_ticket_groups.py`:

```python
@pytest.mark.asyncio
async def test_settings_ticket_groups_upgrade_proxies_to_billing(admin_client):
    from tests.conftest import _TestSession, _HASHED_TESTPASS
    from models import User
    from datetime import datetime, timezone

    async with _TestSession() as session:
        session.add(User(
            username="settingsadmin4", hashed_password=_HASHED_TESTPASS,
            created_at=datetime.now(timezone.utc), created_by=None, role="admin",
        ))
        await session.commit()

    mock_billing_client = AsyncMock()
    mock_billing_client.__aenter__ = AsyncMock(return_value=mock_billing_client)
    mock_billing_client.__aexit__ = AsyncMock(return_value=None)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json = MagicMock(return_value={"status": "stk_sent"})
    mock_billing_client.post = AsyncMock(return_value=mock_resp)

    async with AsyncClient(transport=ASGITransport(app=admin_client.app), base_url="http://test") as c:
        await c.post("/login", data={"username": "settingsadmin4", "password": "testpass"})
        with patch("main.httpx.AsyncClient", return_value=mock_billing_client):
            r = await c.post("/api/settings/ticket-groups/upgrade", json={
                "group_id": "120363111@g.us", "phone": "0712345678",
            })
    admin_client.app.dependency_overrides.clear()
    assert r.status_code == 200
    assert r.json() == {"status": "stk_sent"}
    call = mock_billing_client.post.call_args
    assert "ticket-groups/upgrade" in call.args[0]
    assert call.kwargs["json"] == {"group_id": "120363111@g.us", "phone": "0712345678"}
```

- [ ] **Step 8: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_settings_ticket_groups.py -v -k upgrade`
Expected: FAIL — route doesn't exist.

- [ ] **Step 9: Add the backend proxy endpoint**

Add near `TicketGroupAddBody` (same location, after `GroupAssignBody`):

```python
class TicketGroupUpgradeBody(BaseModel):
    group_id: str
    phone: str
```

In `backend/main.py`, add after `settings_add_ticket_group`:

```python
@app.post("/api/settings/ticket-groups/upgrade")
async def settings_upgrade_ticket_tier(
    body: TicketGroupUpgradeBody,
    username: str = Depends(require_admin),
):
    group_id = body.group_id.strip()
    if not _GROUP_JID_RE.fullmatch(group_id):
        raise HTTPException(status_code=422, detail="group_id doesn't look like a WhatsApp group JID")
    if not BILLING_SERVICE_URL or not CLIENT_SUBDOMAIN:
        raise HTTPException(status_code=503, detail="Billing service not configured")
    async with httpx.AsyncClient(timeout=8.0) as http:
        r = await http.post(
            f"{BILLING_SERVICE_URL}/api/clients/{CLIENT_SUBDOMAIN}/ticket-groups/upgrade",
            json={"group_id": group_id, "phone": body.phone.strip()},
            headers={"X-Billing-Secret": BILLING_WEBHOOK_SECRET},
        )
        r.raise_for_status()
        return r.json()
```

- [ ] **Step 10: Run tests to verify they pass**

Run: `cd backend && python -m pytest tests/test_settings_ticket_groups.py -v`
Expected: PASS — all tests in the file.

- [ ] **Step 11: Wire the upgrade button into `settings.html`**

In `backend/templates/settings.html`, replace the `limit_reached` branch inside `addTicketGroup()`:

```javascript
    if (data.status === 'limit_reached') {
      msg.style.color = 'var(--amber)';
      msg.textContent = `You've reached your plan's limit (${data.next_tier_max ?? 'current'} groups) — upgrade for KES ${data.next_tier_amount} to add more.`;
      return;
    }
```

with:

```javascript
    if (data.status === 'limit_reached') {
      msg.style.color = 'var(--amber)';
      const phone = prompt(`You've reached your plan's limit (${data.next_tier_max ?? 'current'} groups). Upgrade for KES ${data.next_tier_amount}? Enter your M-Pesa number to pay:`);
      if (!phone) { msg.textContent = 'Upgrade cancelled.'; return; }
      const upRes = await fetch('/api/settings/ticket-groups/upgrade', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ group_id: groupId, phone }),
      });
      const upData = await upRes.json();
      if (!upRes.ok) {
        msg.textContent = upData.detail || 'Upgrade request failed.';
        return;
      }
      msg.style.color = upData.status === 'pending_exists' ? 'var(--amber)' : 'var(--green)';
      msg.textContent = upData.status === 'pending_exists'
        ? 'An upgrade payment is already in progress — check your phone.'
        : 'STK push sent — enter your M-Pesa PIN to complete the upgrade.';
      return;
    }
```

- [ ] **Step 12: Commit**

```bash
git add billing/main.py backend/main.py backend/templates/settings.html billing/tests/test_payment_flow.py backend/tests/test_settings_ticket_groups.py
git commit -m "feat: M-Pesa tier-upgrade payment flow for self-service ticket-group add"
```

---

### Task 7: Full regression + manual verification

**Files:** None (verification only).

- [ ] **Step 1: Run the full billing test suite**

Run: `cd billing && python -m pytest -v`
Expected: PASS — every test, including all new ones from Tasks 1-6.

- [ ] **Step 2: Run the full backend test suite**

Run: `cd backend && python -m pytest -v`
Expected: PASS — every test, including `test_billing_forward.py` and the new `test_settings_ticket_groups.py`.

- [ ] **Step 3: Manual verification against a real running dev server**

Following the pattern used for prior features in this repo (see `.superpowers/sdd/progress.md` entries for precedent): start both services locally, log into the billing dashboard, opt a test client into the feature via `client_detail.html` (add 2 groups, confirm the tier auto-sets to tier 1), confirm `/prices` shows and saves the three group-tier prices, hit `GET /api/clients/{subdomain}/ticket-groups` directly with `curl` and the shared secret to confirm the JSON shape, send an `ingest()` request for a non-allowed group and confirm `group_not_licensed`, log into the client's own `/settings` page and confirm the "Ticket-raising groups" section renders and an add of a 6th group (beyond the 5-group tier) shows the upgrade prompt (a live M-Pesa sandbox payment is optional/best-effort depending on environment access — note in the final report if it could not be exercised, same as the documented Playwright/Chromium gap from the reminder-timers-escalation feature).

- [ ] **Step 4: Report**

Summarize pass/fail counts for both suites and note any manual-verification gaps (e.g. no sandbox M-Pesa credentials available) in the progress ledger, mirroring the format already used in `.superpowers/sdd/progress.md`.
