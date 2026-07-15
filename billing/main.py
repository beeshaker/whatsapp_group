import hashlib
import hmac as _hmac
import json
import os
import re
from contextlib import asynccontextmanager

import httpx
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select, text
from starlette.middleware.sessions import SessionMiddleware

from auth import hash_password, verify_password, require_login
from database import get_db, init_db, AsyncSessionLocal, engine
from docker_manager import start_client, stop_client
from models import AdminUser, Client, GroupTierPrice, GroupUpgradeRequest, Payment, PaymentSession
from mpesa import initiate_stk_push
from payment_history import unified_payment_history
from scheduler import start_scheduler
from nginx_manager import add_client_port, remove_client_port
from whatsapp import send_to_group, send_dm_text, send_document_to_group

BILLING_WEBHOOK_SECRET = os.getenv("BILLING_WEBHOOK_SECRET", "")
MPESA_CALLBACK_BASE_URL = os.getenv("MPESA_CALLBACK_BASE_URL", "https://whats2eat.com/billing")

templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))
templates.env.filters["fromjson"] = json.loads

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "changeme")
SECRET_KEY = os.getenv("SECRET_KEY", "billing-secret-change-in-prod")


async def _seed_group_tier_prices():
    """Seed the three fixed, non-overlapping group-count tiers if absent.

    Thin boot-time wrapper around _get_or_seed_group_tiers() so the seed-row
    definitions (names, group ranges, amount 0) live in exactly one place rather
    than being duplicated between boot-time and request-scoped seeding.
    """
    async with AsyncSessionLocal() as db:
        await _get_or_seed_group_tiers(db)


async def _seed_admin():
    async with AsyncSessionLocal() as db:
        existing = await db.scalar(select(AdminUser).where(AdminUser.username == ADMIN_USERNAME))
        if not existing:
            db.add(AdminUser(
                username=ADMIN_USERNAME,
                hashed_password=hash_password(ADMIN_PASSWORD),
                created_at=datetime.now(timezone.utc),
            ))
            await db.commit()


async def _migrate_db():
    """Evolve existing tables/columns without a migration framework.

    Additive changes (new columns) follow the original safe pattern: try an
    ADD COLUMN, swallow the failure if it already exists. That's a harmless
    no-op for ADD COLUMN specifically — "column already exists" is the only
    realistic failure mode.

    Destructive changes (DROP TABLE / DROP COLUMN) do NOT use that blanket
    try/except: swallowing a real failure there would leave the app silently
    broken (e.g. every future `INSERT INTO clients` crashing on a NOT NULL
    constraint the model no longer supplies a value for) instead of a
    harmless no-op. They're made idempotent explicitly instead, and any
    unexpected failure is left to propagate and crash app boot loudly.
    """
    async with engine.begin() as conn:
        migrations = [
            ("clients", "admin_whatsapp_phone TEXT"),
            ("clients", "whatsapp_invite_link TEXT"),
            ("clients", "backend_port INTEGER"),
            ("payment_sessions", "phone TEXT"),
            ("payment_sessions", "checkout_request_id TEXT"),
            ("payment_sessions", "payment_id INTEGER"),
            ("clients", "billing_only_started_at DATETIME"),
            ("clients", "last_warning_sent_at DATETIME"),
            ("clients", "data_retention_days INTEGER DEFAULT 90"),
            ("clients", "pre_expiry_14_warned BOOLEAN DEFAULT 0"),
            ("clients", "pre_expiry_2_warned BOOLEAN DEFAULT 0"),
            ("clients", "allowed_ticket_groups TEXT"),
            ("clients", "ticket_group_tier_id INTEGER"),
            ("group_tier_prices", "name TEXT DEFAULT ''"),
            ("group_upgrade_requests", "mpesa_transaction_id TEXT"),
        ]
        for table, col_def in migrations:
            try:
                await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col_def}"))
            except Exception:
                pass  # Column already exists

        # Backfill placeholder names for the 3 fixed group-count tiers. Idempotent:
        # only touches rows whose name is still unset, so it's safe to run on every
        # boot and won't clobber a real name an admin has since set.
        for min_groups, placeholder in ((1, "Tier 1"), (6, "Tier 2"), (11, "Tier 3")):
            await conn.execute(
                text(
                    "UPDATE group_tier_prices SET name = :placeholder "
                    "WHERE min_groups = :min_groups AND (name IS NULL OR name = '')"
                ),
                {"placeholder": placeholder, "min_groups": min_groups},
            )

        # --- Destructive migrations: replaced by group-tier-only pricing. ---
        # Naturally idempotent via IF EXISTS.
        await conn.execute(text("DROP TABLE IF EXISTS plan_prices"))

        # clients.plan predates the app's add-column migration mechanism (it was
        # part of the original CREATE TABLE, NOT NULL with no DB-level default),
        # so it must be dropped explicitly rather than left for SQLAlchemy to stop
        # populating. Only attempt the drop if the column is still present, so
        # repeated boots are a no-op; let any failure raise and crash app boot
        # rather than swallowing it — a boot-time crash is loud and actionable,
        # a silently-broken client-creation path is not.
        result = await conn.execute(text("PRAGMA table_info(clients)"))
        existing_columns = {row[1] for row in result.fetchall()}
        if "plan" in existing_columns:
            await conn.execute(text("ALTER TABLE clients DROP COLUMN plan"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    await _migrate_db()
    await _seed_group_tier_prices()
    await _seed_admin()
    import logging as _log
    if SECRET_KEY == "billing-secret-change-in-prod":
        _log.getLogger(__name__).warning(
            "SECURITY WARNING: SECRET_KEY is using the insecure default. "
            "Set SECRET_KEY env var before production deployment."
        )
    if not BILLING_WEBHOOK_SECRET:
        _log.getLogger(__name__).warning(
            "SECURITY WARNING: BILLING_WEBHOOK_SECRET is not set. "
            "Webhook signature verification is disabled."
        )
    scheduler = start_scheduler()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse(request, "login.html", {"request": request, "error": None})


@app.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db=Depends(get_db),
):
    user = await db.scalar(select(AdminUser).where(AdminUser.username == username))
    if not user or not verify_password(password, user.hashed_password):
        return templates.TemplateResponse(request, "login.html", {"request": request, "error": "Invalid credentials"})
    request.session["username"] = user.username
    return RedirectResponse("/", status_code=303)


@app.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


def _next_renewal() -> date:
    return date.today() + timedelta(days=30)


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, username: str = Depends(require_login), db=Depends(get_db)):
    clients = (await db.execute(select(Client).order_by(Client.name))).scalars().all()
    group_tiers = await _get_or_seed_group_tiers(db)
    tiers_by_id = {t.id: t for t in group_tiers}
    return templates.TemplateResponse(request, "dashboard.html", {
        "request": request, "clients": clients, "group_tiers": group_tiers,
        "tiers_by_id": tiers_by_id, "username": username,
    })


@app.get("/clients/new", response_class=HTMLResponse)
async def new_client_form(request: Request, username: str = Depends(require_login), db=Depends(get_db)):
    group_tiers = await _get_or_seed_group_tiers(db)
    return templates.TemplateResponse(request, "client_form.html", {
        "request": request, "error": None, "group_tiers": group_tiers,
    })


@app.post("/clients", response_class=HTMLResponse)
async def create_client(
    request: Request,
    name: str = Form(...),
    subdomain: str = Form(...),
    ticket_group_tier_id: str = Form(default=""),
    admin_whatsapp_phone: str = Form(default=""),
    backend_port: str = Form(default=""),
    username: str = Depends(require_login),
    db=Depends(get_db),
):
    subdomain = subdomain.lower().strip()
    group_tiers = await _get_or_seed_group_tiers(db)
    if await db.scalar(select(Client).where(Client.subdomain == subdomain)):
        return templates.TemplateResponse(request, "client_form.html", {
            "request": request,
            "error": f"Subdomain '{subdomain}' already exists",
            "group_tiers": group_tiers,
        })
    port_int = int(backend_port.strip()) if backend_port.strip().isdigit() else None
    # New clients get a billing tier auto-assigned so the fixed 30-day renewal
    # billing works from day one. Default to the lowest tier when the form leaves
    # it blank/invalid. This deliberately does NOT touch allowed_ticket_groups:
    # tier assignment and the ticket-groups allow-list are orthogonal concerns —
    # setting the allow-list here would silently block all ticket ingestion at the
    # backend gate (allowed_groups is not None => restricted). Leave it None.
    tier_id = group_tiers[0].id
    if ticket_group_tier_id.strip().isdigit():
        chosen = int(ticket_group_tier_id.strip())
        if any(t.id == chosen for t in group_tiers):
            tier_id = chosen
    client = Client(
        name=name, subdomain=subdomain, status="active",
        renewal_date=_next_renewal(), created_at=datetime.now(timezone.utc),
        admin_whatsapp_phone=admin_whatsapp_phone.strip() or None,
        backend_port=port_int,
        ticket_group_tier_id=tier_id,
    )
    db.add(client)
    await db.commit()
    if port_int:
        import asyncio
        await asyncio.get_event_loop().run_in_executor(None, add_client_port, subdomain, port_int)
    return RedirectResponse(f"/clients/{client.id}", status_code=303)


@app.get("/clients/{client_id}", response_class=HTMLResponse)
async def client_detail(
    request: Request, client_id: int,
    username: str = Depends(require_login),
    db=Depends(get_db),
):
    client = await db.get(Client, client_id)
    if not client:
        return HTMLResponse("Not found", status_code=404)
    payments = await unified_payment_history(db, client_id, confirmed_only=False)
    group_tiers = await _get_or_seed_group_tiers(db)
    current_tier = (
        await db.get(GroupTierPrice, client.ticket_group_tier_id)
        if client.ticket_group_tier_id else None
    )
    return templates.TemplateResponse(request, "client_detail.html", {
        "request": request, "client": client, "payments": payments,
        "group_tiers": group_tiers, "current_tier": current_tier, "username": username,
    })


@app.post("/clients/{client_id}", response_class=HTMLResponse)
async def update_client(
    request: Request, client_id: int,
    whatsapp_group_id: str = Form(default=""),
    openwa_url: str = Form(default=""),
    openwa_session: str = Form(default=""),
    openwa_api_key: str = Form(default=""),
    docker_project: str = Form(default=""),
    renewal_date: str = Form(default=""),
    ticket_group_tier_id: str = Form(default=""),
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
    if whatsapp_group_id:
        client.whatsapp_group_id = whatsapp_group_id.strip()
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
    if ticket_group_tier_id.strip().isdigit():
        # Admin (re)assigns the client's billing tier directly. This is the
        # mechanism for manually assigning tiers to legacy clients (which are not
        # auto-migrated and start at ticket_group_tier_id=None). Only accept an id
        # that maps to a real tier row; ignore anything else.
        tid = int(ticket_group_tier_id.strip())
        if await db.get(GroupTierPrice, tid):
            client.ticket_group_tier_id = tid
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
    await db.commit()
    if port_changed:
        import asyncio
        await asyncio.get_event_loop().run_in_executor(
            None, add_client_port, client.subdomain, new_port
        )
    return RedirectResponse(f"/clients/{client_id}", status_code=303)


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
    if client.allowed_ticket_groups is not None:
        groups = json.loads(client.allowed_ticket_groups)
        groups = [g for g in groups if g != group_id.strip()]
        client.allowed_ticket_groups = json.dumps(groups)
        await db.commit()
    return RedirectResponse(f"/clients/{client_id}", status_code=303)


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
    await db.commit()
    return RedirectResponse(f"/clients/{client_id}", status_code=303)


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
    return RedirectResponse(f"/clients/{client_id}?invited=1", status_code=303)


async def _get_or_seed_group_tiers(db) -> list[GroupTierPrice]:
    """Return the 3 fixed group-count tiers ordered by min_groups, seeding them
    (placeholder names, amount 0) if the table is empty. This is the single
    source of truth for the seed rows — both boot-time seeding
    (_seed_group_tier_prices) and every request-scoped tier lookup route it here.
    """
    tiers = (await db.execute(select(GroupTierPrice).order_by(GroupTierPrice.min_groups))).scalars().all()
    if tiers:
        return list(tiers)
    now = datetime.now(timezone.utc)
    tiers = [
        GroupTierPrice(name="Tier 1", min_groups=1, max_groups=5, amount=Decimal("0"), set_at=now, set_by="system"),
        GroupTierPrice(name="Tier 2", min_groups=6, max_groups=10, amount=Decimal("0"), set_at=now, set_by="system"),
        GroupTierPrice(name="Tier 3", min_groups=11, max_groups=None, amount=Decimal("0"), set_at=now, set_by="system"),
    ]
    db.add_all(tiers)
    await db.commit()
    for t in tiers:
        await db.refresh(t)
    return tiers


@app.get("/prices", response_class=HTMLResponse)
async def prices_page(request: Request, username: str = Depends(require_login), db=Depends(get_db)):
    group_tiers = await _get_or_seed_group_tiers(db)
    return templates.TemplateResponse(request, "prices.html", {
        "request": request, "group_tiers": group_tiers, "username": username,
    })


@app.post("/prices/group-tiers", response_class=HTMLResponse)
async def set_group_tier_prices(
    request: Request,
    tier1_name: str = Form(...),
    tier1_amount: str = Form(...),
    tier2_name: str = Form(...),
    tier2_amount: str = Form(...),
    tier3_name: str = Form(...),
    tier3_amount: str = Form(...),
    username: str = Depends(require_login),
    db=Depends(get_db),
):
    now = datetime.now(timezone.utc)
    group_tiers = await _get_or_seed_group_tiers(db)
    names = [tier1_name.strip(), tier2_name.strip(), tier3_name.strip()]

    def _reject(msg: str):
        return templates.TemplateResponse(request, "prices.html", {
            "request": request, "group_tiers": group_tiers, "username": username,
            "group_tier_error": msg,
        })

    # Group-count ranges are fixed; only name + amount are editable here.
    if any(not n for n in names):
        return _reject("Tier names cannot be blank.")
    if len({n.lower() for n in names}) != len(names):
        return _reject("Tier names must be unique.")
    try:
        amounts = [Decimal(tier1_amount), Decimal(tier2_amount), Decimal(tier3_amount)]
    except Exception as e:
        return _reject(f"Invalid amount: {e}")
    if any(v <= 0 for v in amounts):
        return _reject("Each tier amount must be greater than zero.")
    if not (amounts[0] <= amounts[1] <= amounts[2]):
        return _reject("Higher tiers cannot cost less than lower tiers.")

    for tier, name, amount in zip(group_tiers, names, amounts):
        tier.name = name
        tier.amount = amount
        tier.set_at = now
        tier.set_by = username
    await db.commit()
    return RedirectResponse("/prices", status_code=303)


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------

def _verify_sig(secret: str, body: bytes, signature: str) -> bool:
    expected = _hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return _hmac.compare_digest(expected, signature)


def _normalize_phone(raw: str) -> str | None:
    digits = re.sub(r"\D", "", raw)
    if re.fullmatch(r"07\d{8}", digits):
        return "254" + digits[1:]
    if re.fullmatch(r"2547\d{8}", digits):
        return digits
    return None


def _period_end(start: date) -> date:
    return start + timedelta(days=30)


async def _resolve_renewal_charge(client: Client, db) -> tuple["Decimal | None", "GroupTierPrice | None"]:
    """Resolve a client's fixed 30-day renewal charge from its assigned group tier.

    Returns (amount, tier). A None amount signals the client CANNOT be charged and
    the caller must fail gracefully (inform the user, no STK push) — this happens
    when either:
      * no tier is assigned (legacy clients aren't auto-migrated to a tier), or
      * the assigned tier is still unpriced (tiers seed at amount 0 until an admin
        sets real prices via /prices) — charging KES 0 is never valid.
    """
    tier = (
        await db.get(GroupTierPrice, client.ticket_group_tier_id)
        if client.ticket_group_tier_id else None
    )
    if tier is None or tier.amount is None or tier.amount <= 0:
        return None, tier
    return tier.amount, tier


# ---------------------------------------------------------------------------
# Webhook: WhatsApp messages from client backends
# ---------------------------------------------------------------------------

@app.post("/webhook/by-group/{group_id}")
async def group_webhook(group_id: str, request: Request, db=Depends(get_db)):
    body = await request.body()
    sig = request.headers.get("X-Webhook-Signature", "")
    if BILLING_WEBHOOK_SECRET and not _verify_sig(BILLING_WEBHOOK_SECRET, body, sig):
        raise HTTPException(status_code=401, detail="Invalid signature")

    import logging as _lg
    _lg.getLogger(__name__).warning("group_webhook: group_id=%r", group_id)
    client = await db.scalar(select(Client).where(Client.whatsapp_group_id == group_id))
    if not client:
        _lg.getLogger(__name__).warning("group_webhook: no client found for group_id=%r", group_id)
        return {"ok": True}
    _lg.getLogger(__name__).warning("group_webhook: found client=%s", client.subdomain)

    try:
        payload = json.loads(body)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    # OpenWA wraps message fields inside a "data" key
    data = payload.get("data", payload)
    return await _process_client_message(client, data, db)


async def _process_client_message(client: Client, data: dict, db) -> dict:
    if data.get("fromMe", False):
        return {"ok": True}

    message_text = (data.get("body") or "").strip()
    now = datetime.now(timezone.utc)

    active_session = await db.scalar(
        select(PaymentSession).where(
            PaymentSession.client_id == client.id,
            PaymentSession.expires_at > now,
        )
    )

    if message_text.lower() == "/close":
        client.status = "closed"
        await db.commit()
        await stop_client(client)
        await send_to_group(
            client,
            "\U0001f44b Your account has been closed. All services have been stopped. "
            "Thank you for using our service.",
        )
        return {"ok": True}

    if message_text.lower() == "/statement":
        try:
            from pdf import generate_statement
            history = await unified_payment_history(db, client.id, confirmed_only=True)
            tier = (
                await db.get(GroupTierPrice, client.ticket_group_tier_id)
                if client.ticket_group_tier_id else None
            )
            pdf_bytes = generate_statement(
                client_name=client.name,
                tier_name=tier.name if tier else None,
                client_status=client.status,
                renewal_date=client.renewal_date,
                payments=history,
                invoice_payment=history[0] if history else None,
            )
            filename = f"statement_{client.subdomain}_{date.today()}.pdf"
            await send_document_to_group(client, pdf_bytes, filename, caption="📄 Your latest payment statement")
        except Exception as exc:
            import logging as _l
            _l.getLogger(__name__).warning("Statement command failed for %s: %s", client.subdomain, exc)
            await send_to_group(client, "❌ Could not generate statement. Please try again later.")
        return {"ok": True}

    if message_text.lower() == "/payment":
        if active_session:
            await db.delete(active_session)
        db.add(PaymentSession(
            client_id=client.id, state="awaiting_phone",
            created_at=now, expires_at=now + timedelta(minutes=5),
        ))
        await db.commit()
        await send_to_group(client, "📱 Please reply with your M-Pesa phone number (e.g. 0712345678):")
        return {"ok": True}

    if not active_session:
        # Notify once if there is a recently-expired session so the user isn't left guessing.
        expired = await db.scalar(
            select(PaymentSession).where(
                PaymentSession.client_id == client.id,
                PaymentSession.expires_at <= now,
                PaymentSession.expires_at > now - timedelta(hours=1),
            )
        )
        if expired:
            await db.delete(expired)
            await db.commit()
            await send_to_group(client, "⏰ Your payment window expired. Type /payment to start again.")
        return {"ok": True}

    if active_session.state == "awaiting_phone":
        phone = _normalize_phone(message_text)
        if not phone:
            await send_to_group(client, "❌ Invalid number. Please reply with your M-Pesa number (e.g. 0712345678):")
            return {"ok": True}

        amount, tier = await _resolve_renewal_charge(client, db)
        if amount is None:
            await send_to_group(
                client,
                "❌ Your billing tier isn't set up yet, so we can't process a payment. "
                "Please contact your administrator to configure your pricing.",
            )
            await db.delete(active_session)
            await db.commit()
            return {"ok": True}

        active_session.state = "awaiting_confirm"
        active_session.phone = phone
        active_session.expires_at = now + timedelta(minutes=5)
        await db.commit()

        await send_to_group(
            client,
            f"💳 You'll be charged *KES {amount}* for your *{tier.name}* tier to {phone}.\n"
            f"Reply *YES* to confirm or *NO* to cancel.",
        )
        return {"ok": True}

    if active_session.state == "awaiting_confirm":
        reply = message_text.lower()
        if reply in ("no", "cancel"):
            await db.delete(active_session)
            await db.commit()
            await send_to_group(client, "❌ Payment cancelled. Type /payment to start again.")
            return {"ok": True}

        if reply not in ("yes", "y", "ok"):
            await send_to_group(client, "Please reply *YES* to confirm payment or *NO* to cancel.")
            return {"ok": True}

        phone = active_session.phone
        amount, tier = await _resolve_renewal_charge(client, db)
        if amount is None:
            await send_to_group(
                client,
                "❌ Your billing tier isn't set up yet, so we can't process a payment. "
                "Please contact your administrator to configure your pricing.",
            )
            await db.delete(active_session)
            await db.commit()
            return {"ok": True}

        today = date.today()
        payment = Payment(
            client_id=client.id, phone=phone, amount=amount, status="pending",
            initiated_at=now, period_start=today, period_end=_period_end(today),
        )
        db.add(payment)
        await db.flush()

        try:
            stk = await initiate_stk_push(
                phone=phone,
                amount=amount,
                account_ref=f"{client.subdomain}-sub",
                callback_url=f"{MPESA_CALLBACK_BASE_URL}/webhook/mpesa",
            )
        except Exception as exc:
            import logging as _log
            _log.getLogger(__name__).error("STK Push failed for %s: %s", client.subdomain, exc)
            payment.status = "failed"
            await db.delete(active_session)
            await db.commit()
            await send_to_group(client, "❌ M-Pesa payment request failed. Please try again with /payment.")
            return {"ok": True}

        active_session.state = "awaiting_stk_confirm"
        active_session.checkout_request_id = stk.get("CheckoutRequestID")
        active_session.payment_id = payment.id
        active_session.expires_at = now + timedelta(minutes=10)
        await db.commit()

        await send_to_group(
            client,
            f"✅ STK Push sent to {phone}. Enter your M-Pesa PIN on your phone to pay KES {amount}.",
        )
        return {"ok": True}

    return {"ok": True}


@app.post("/webhook/client/{subdomain}")
async def client_webhook(subdomain: str, request: Request, db=Depends(get_db)):
    body = await request.body()
    sig = request.headers.get("X-Webhook-Signature", "")
    if BILLING_WEBHOOK_SECRET and not _verify_sig(BILLING_WEBHOOK_SECRET, body, sig):
        raise HTTPException(status_code=401, detail="Invalid signature")

    client = await db.scalar(select(Client).where(Client.subdomain == subdomain))
    if not client:
        raise HTTPException(status_code=404)

    try:
        data = json.loads(body)
    except Exception:
        raise HTTPException(status_code=400, detail="Invalid JSON")

    return await _process_client_message(client, data, db)


# ---------------------------------------------------------------------------
# Webhook: MPesa Daraja STK callback
# ---------------------------------------------------------------------------

@app.post("/webhook/mpesa")
async def mpesa_callback(request: Request, db=Depends(get_db)):
    data = await request.json()
    callback = data.get("Body", {}).get("stkCallback", {})
    checkout_id = callback.get("CheckoutRequestID")
    if not checkout_id:
        return {"ResultCode": 0, "ResultDesc": "Accepted"}
    result_code = callback.get("ResultCode")
    # Extract the M-Pesa receipt up-front so BOTH the renewal branch and the
    # tier-upgrade branch can capture it. Previously only the renewal branch
    # pulled it, so a confirmed GroupUpgradeRequest never recorded its receipt.
    mpesa_id = next(
        (item["Value"] for item in callback.get("CallbackMetadata", {}).get("Item", [])
         if item["Name"] == "MpesaReceiptNumber"),
        None,
    )

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
            # Re-check status right before applying: a billing admin may have
            # cancelled this request in the meantime (e.g. via
            # admin_reset_ticket_groups_unrestricted). Don't resurrect a
            # cancelled request by overwriting it back to "confirmed".
            if upgrade_req.status != "pending":
                await db.commit()
                return {"ResultCode": 0, "ResultDesc": "Accepted"}
            client = await db.get(Client, upgrade_req.client_id)
            if not client:
                await db.delete(upgrade_req)
                await db.commit()
                return {"ResultCode": 0, "ResultDesc": "Accepted"}
            groups = json.loads(client.allowed_ticket_groups) if client.allowed_ticket_groups else []
            if upgrade_req.group_id not in groups:
                groups.append(upgrade_req.group_id)
            client.allowed_ticket_groups = json.dumps(groups)
            client.ticket_group_tier_id = upgrade_req.target_tier_id
            upgrade_req.mpesa_transaction_id = mpesa_id
            upgrade_req.status = "confirmed"
        else:
            upgrade_req.status = "failed"
        await db.commit()
        return {"ResultCode": 0, "ResultDesc": "Accepted"}

    client = await db.get(Client, session.client_id)
    if not client:
        await db.delete(session)
        await db.commit()
        return {"ResultCode": 0, "ResultDesc": "Accepted"}
    payment = await db.get(Payment, session.payment_id) if session.payment_id else None

    if result_code == 0:
        if payment:
            payment.status = "confirmed"
            payment.mpesa_transaction_id = mpesa_id
            payment.confirmed_at = datetime.now(timezone.utc)

        client.status = "active"
        client.grace_started_at = None
        client.billing_only_started_at = None
        client.last_warning_sent_at = None
        client.pre_expiry_14_warned = False
        client.pre_expiry_2_warned = False
        client.renewal_date = payment.period_end if payment else _period_end(date.today())
        await db.delete(session)
        await db.commit()

        await start_client(client)
        period_start = payment.period_start if payment else date.today()
        period_end = payment.period_end if payment else client.renewal_date
        amount = payment.amount if payment else "—"
        phone = payment.phone if payment else "—"
        confirmed_at = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M") + " UTC"
        await send_to_group(
            client,
            f"🎉 *Payment Confirmed!*\n\n"
            f"📋 *Payment Summary*\n"
            f"  • Amount: KES {amount}\n"
            f"  • M-Pesa Receipt: {mpesa_id or '—'}\n"
            f"  • Phone: {phone}\n"
            f"  • Date: {confirmed_at}\n\n"
            f"✅ Account Status: *Active*\n"
            f"📅 Period Covered: {period_start} → {period_end}\n"
            f"🔄 Next Renewal: *{client.renewal_date}*\n\n"
            f"Thank you for your payment!",
        )
        # Generate and send PDF statement
        try:
            from pdf import generate_statement
            invoice_data = {
                "amount": str(amount),
                "receipt": mpesa_id,
                "phone": str(phone),
                "period_start": str(period_start),
                "period_end": str(period_end),
                "date": confirmed_at,
            } if payment else None
            history = await unified_payment_history(db, client.id, confirmed_only=True)
            tier_row = (
                await db.get(GroupTierPrice, client.ticket_group_tier_id)
                if client.ticket_group_tier_id else None
            )
            pdf_bytes = generate_statement(
                client_name=client.name,
                tier_name=tier_row.name if tier_row else None,
                client_status=client.status,
                renewal_date=client.renewal_date,
                payments=history,
                invoice_payment=invoice_data,
            )
            filename = f"statement_{client.subdomain}_{date.today()}.pdf"
            await send_document_to_group(client, pdf_bytes, filename, caption="📄 Your payment statement")
        except Exception as exc:
            import logging as _log
            _log.getLogger(__name__).warning("PDF statement generation failed for %s: %s", client.subdomain, exc)
    else:
        if payment:
            payment.status = "failed"
        await db.delete(session)
        await db.commit()
        await send_to_group(client, "❌ Payment failed or was cancelled. Type /payment to try again.")

    return {"ResultCode": 0, "ResultDesc": "Accepted"}


# ---------------------------------------------------------------------------
# WhatsApp session reconnect
# ---------------------------------------------------------------------------

async def _get_session_id(client: Client) -> str | None:
    """Look up the OpenWA session UUID for a client by session name."""
    if not client.openwa_url or not client.openwa_session:
        return None
    async with httpx.AsyncClient(timeout=10.0) as http:
        r = await http.get(
            f"{client.openwa_url}/api/sessions",
            headers={"X-API-Key": client.openwa_api_key or ""},
        )
        r.raise_for_status()
        for s in r.json():
            if s.get("name") == client.openwa_session:
                return s["id"]
    return None


async def _get_session_status(client: Client) -> tuple[str, str | None]:
    """Return (OpenWA session status, connected phone number) — status is a
    descriptive error token ("NOT_CONFIGURED"/"NOT_FOUND"/"UNREACHABLE") on failure."""
    if not client.openwa_url or not client.openwa_session:
        return "NOT_CONFIGURED", None
    try:
        async with httpx.AsyncClient(timeout=8.0) as http:
            r = await http.get(
                f"{client.openwa_url}/api/sessions",
                headers={"X-API-Key": client.openwa_api_key or ""},
            )
            r.raise_for_status()
            for s in r.json():
                if s.get("name") == client.openwa_session:
                    return s.get("status", "UNKNOWN"), s.get("phone")
            return "NOT_FOUND", None
    except Exception:
        return "UNREACHABLE", None


async def _get_groups(client: Client) -> list[dict] | None:
    """Fetch the live WhatsApp groups list for a client's OpenWA session.

    Returns [{id, name}, ...] on success, or None (never raises) if no
    session is configured, the session can't be resolved, or OpenWA is
    unreachable.
    """
    try:
        session_id = await _get_session_id(client)
        if not session_id:
            return None
        async with httpx.AsyncClient(timeout=10.0) as http:
            r = await http.get(
                f"{client.openwa_url}/api/sessions/{session_id}/groups",
                headers={"X-API-Key": client.openwa_api_key or ""},
            )
            r.raise_for_status()
            return r.json()
    except Exception:
        return None


@app.get("/clients/{client_id}/whatsapp-status")
async def whatsapp_status(
    client_id: int,
    username: str = Depends(require_login),
    db=Depends(get_db),
):
    client = await db.get(Client, client_id)
    if not client:
        raise HTTPException(status_code=404)
    status, phone = await _get_session_status(client)
    admin_norm = _normalize_phone(client.admin_whatsapp_phone or "")
    live_norm = _normalize_phone(phone or "")
    mismatch = bool(admin_norm and live_norm and admin_norm != live_norm)
    return JSONResponse({
        "status": status,
        "phone": phone,
        "admin_phone": client.admin_whatsapp_phone,
        "phone_mismatch": mismatch,
    })


@app.get("/clients/{client_id}/whatsapp-groups")
async def whatsapp_groups(
    client_id: int,
    username: str = Depends(require_login),
    db=Depends(get_db),
):
    client = await db.get(Client, client_id)
    if not client:
        raise HTTPException(status_code=404)
    groups = await _get_groups(client)
    return JSONResponse({"groups": groups})


@app.post("/clients/{client_id}/reconnect-whatsapp", response_class=HTMLResponse)
async def reconnect_whatsapp(
    request: Request, client_id: int,
    username: str = Depends(require_login),
    db=Depends(get_db),
):
    client = await db.get(Client, client_id)
    if not client:
        raise HTTPException(status_code=404)
    try:
        session_id = await _get_session_id(client)
        if not session_id and (not client.openwa_url or not client.openwa_session):
            return RedirectResponse(f"/clients/{client_id}/reconnect", status_code=303)
        async with httpx.AsyncClient(timeout=15.0) as http:
            if not session_id:
                create_r = await http.post(
                    f"{client.openwa_url}/api/sessions",
                    headers={"X-API-Key": client.openwa_api_key or "", "Content-Type": "application/json"},
                    json={"name": client.openwa_session},
                )
                if create_r.status_code == 409:
                    session_id = await _get_session_id(client)
                else:
                    create_r.raise_for_status()
                    session_id = create_r.json()["id"]
            else:
                # Stop first so we get a clean QR — ignore errors (may already be stopped)
                await http.post(
                    f"{client.openwa_url}/api/sessions/{session_id}/stop",
                    headers={"X-API-Key": client.openwa_api_key or ""},
                )
            await http.post(
                f"{client.openwa_url}/api/sessions/{session_id}/start",
                headers={"X-API-Key": client.openwa_api_key or ""},
            )
    except Exception:
        pass
    return RedirectResponse(f"/clients/{client_id}/reconnect", status_code=303)


@app.post("/clients/{client_id}/disconnect-whatsapp", response_class=HTMLResponse)
async def disconnect_whatsapp(
    request: Request, client_id: int,
    username: str = Depends(require_login),
    db=Depends(get_db),
):
    client = await db.get(Client, client_id)
    if not client:
        raise HTTPException(status_code=404)
    try:
        session_id = await _get_session_id(client)
        if session_id:
            async with httpx.AsyncClient(timeout=15.0) as http:
                await http.delete(
                    f"{client.openwa_url}/api/sessions/{session_id}",
                    headers={"X-API-Key": client.openwa_api_key or ""},
                )
    except Exception as exc:
        import logging as _log
        _log.getLogger(__name__).warning("disconnect_whatsapp failed for %s: %s", client.subdomain, exc)
    return RedirectResponse(f"/clients/{client_id}?disconnected=1", status_code=303)


@app.get("/clients/{client_id}/reconnect", response_class=HTMLResponse)
async def reconnect_page(
    request: Request, client_id: int,
    username: str = Depends(require_login),
    db=Depends(get_db),
):
    client = await db.get(Client, client_id)
    if not client:
        raise HTTPException(status_code=404)
    return templates.TemplateResponse(request, "reconnect.html", {
        "request": request, "client": client,
    })


@app.get("/clients/{client_id}/whatsapp-qr")
async def whatsapp_qr(
    client_id: int,
    username: str = Depends(require_login),
    db=Depends(get_db),
):
    client = await db.get(Client, client_id)
    if not client:
        raise HTTPException(status_code=404)
    try:
        session_id = await _get_session_id(client)
        if not session_id:
            return JSONResponse({"error": "Session not found"}, status_code=404)
        async with httpx.AsyncClient(timeout=10.0) as http:
            r = await http.get(
                f"{client.openwa_url}/api/sessions/{session_id}/qr",
                headers={"X-API-Key": client.openwa_api_key or ""},
            )
            if r.status_code == 200:
                return JSONResponse(r.json())
            # Session already connected — check status
            sr = await http.get(
                f"{client.openwa_url}/api/sessions/{session_id}",
                headers={"X-API-Key": client.openwa_api_key or ""},
            )
            return JSONResponse({"status": sr.json().get("status", "UNKNOWN")})
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)


# ---------------------------------------------------------------------------
# Manual admin actions
# ---------------------------------------------------------------------------

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
    return RedirectResponse(f"/clients/{client_id}", status_code=303)


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
    await db.commit()
    await start_client(client)
    await send_to_group(client, "✅ Your account has been manually reactivated by the administrator.")
    return RedirectResponse(f"/clients/{client_id}", status_code=303)


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
    await db.commit()
    await stop_client(client)
    await send_to_group(
        client,
        "\U0001f44b Your account has been closed. All services have been stopped. "
        "Thank you for using our service.",
    )
    return RedirectResponse(f"/clients/{client_id}", status_code=303)


# ---------------------------------------------------------------------------
# Client-facing statement API (called by backend service)
# ---------------------------------------------------------------------------

@app.get("/api/clients/{subdomain}/statement")
async def client_statement(subdomain: str, request: Request, db=Depends(get_db)):
    secret = request.headers.get("X-Billing-Secret", "")
    if BILLING_WEBHOOK_SECRET and secret != BILLING_WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")
    client = await db.scalar(select(Client).where(Client.subdomain == subdomain))
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")
    payments = await unified_payment_history(db, client.id, confirmed_only=False)
    tier = (
        await db.get(GroupTierPrice, client.ticket_group_tier_id)
        if client.ticket_group_tier_id else None
    )
    return {
        "client": {
            "name": client.name,
            "tier_name": tier.name if tier else None,
            "tier_amount": str(tier.amount) if tier else None,
            "status": client.status,
            "renewal_date": str(client.renewal_date),
        },
        "payments": payments,
    }


@app.get("/api/clients/{subdomain}/status")
async def client_billing_status(subdomain: str, request: Request, db=Depends(get_db)):
    secret = request.headers.get("X-Billing-Secret", "")
    if BILLING_WEBHOOK_SECRET and secret != BILLING_WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")
    client = await db.scalar(select(Client).where(Client.subdomain == subdomain))
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")
    return {"status": client.status, "whatsapp_group_id": client.whatsapp_group_id}


@app.get("/api/clients/{subdomain}/ticket-groups")
async def client_ticket_groups(subdomain: str, request: Request, db=Depends(get_db)):
    secret = request.headers.get("X-Billing-Secret", "")
    if BILLING_WEBHOOK_SECRET and secret != BILLING_WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")
    client = await db.scalar(select(Client).where(Client.subdomain == subdomain))
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")
    # Billing tier (ticket_group_tier_id) and the ticket-groups gating tier are
    # orthogonal: every client gets a billing tier auto-assigned at creation, but
    # the group-count cap should only apply once the client has actually opted
    # into ticket-groups restriction (allowed_ticket_groups is not None).
    tier = (
        await db.get(GroupTierPrice, client.ticket_group_tier_id)
        if client.ticket_group_tier_id and client.allowed_ticket_groups is not None
        else None
    )
    return {
        "allowed_groups": json.loads(client.allowed_ticket_groups) if client.allowed_ticket_groups else None,
        "tier_limit": tier.max_groups if tier else None,
    }


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

    if client.allowed_ticket_groups is None:
        # None means the client is fully unrestricted (no cap, no billing tie-in).
        # Self-service add is only for clients a billing admin has already opted
        # in via admin_add_ticket_group — bootstrapping a tier here would silently
        # convert an unrestricted client into one restricted to a single group.
        raise HTTPException(
            status_code=403,
            detail="Group management is not enabled for this account. Contact support to enable it.",
        )

    groups = json.loads(client.allowed_ticket_groups)
    if group_id in groups:
        return {"status": "ok", "added": False}

    if client.ticket_group_tier_id is None:
        # _get_or_seed_group_tiers (Task 2) guarantees the 3 fixed tiers exist —
        # a client may reach this self-service route before anyone has opened
        # /prices, so the lookup can't assume the rows are already there.
        base_tier = (await _get_or_seed_group_tiers(db))[0]
        client.ticket_group_tier_id = base_tier.id

    tier = await db.get(GroupTierPrice, client.ticket_group_tier_id)
    limit = tier.max_groups if tier else None
    if limit is not None and len(groups) + 1 > limit:
        next_tier = await db.scalar(
            select(GroupTierPrice)
            .where(GroupTierPrice.min_groups > limit)
            .order_by(GroupTierPrice.min_groups)
            .limit(1)
        )
        await db.commit()
        return {
            "status": "limit_reached",
            "next_tier_amount": str(next_tier.amount) if next_tier else None,
            "next_tier_max": next_tier.max_groups if next_tier else None,
        }

    groups.append(group_id)
    client.allowed_ticket_groups = json.dumps(groups)
    await db.commit()
    return {"status": "ok", "added": True}


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

    if client.allowed_ticket_groups is None:
        # None means the client is fully unrestricted (no cap, no billing tie-in).
        # Self-service upgrade is only for clients a billing admin has already
        # opted in via admin_add_ticket_group — starting an upgrade flow here
        # would silently convert an unrestricted client into a restricted one
        # once the M-Pesa callback lands.
        raise HTTPException(
            status_code=403,
            detail="Group management is not enabled for this account. Contact support to enable it.",
        )

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
    if next_tier.amount <= 0:
        raise HTTPException(status_code=400, detail="This tier hasn't been priced yet. Contact support.")

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


# ---------------------------------------------------------------------------
# Nginx auth-check gate
# ---------------------------------------------------------------------------

@app.get("/internal/auth-check")
async def auth_check(request: Request, db=Depends(get_db)):
    subdomain = request.headers.get("X-Client-Subdomain", "")
    if not subdomain:
        return JSONResponse(status_code=200, content={"ok": True})
    client = await db.scalar(select(Client).where(Client.subdomain == subdomain))
    if client and client.status in ("grace", "billing_only", "closed"):
        raise HTTPException(status_code=403, detail="Subscription inactive")
    return JSONResponse(status_code=200, content={"ok": True})


# ---------------------------------------------------------------------------
# Payment-expired static page
# ---------------------------------------------------------------------------

@app.get("/payment-expired", response_class=HTMLResponse)
async def payment_expired_page(request: Request):
    return templates.TemplateResponse(request, "payment_expired.html", {})
