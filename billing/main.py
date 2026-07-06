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
from models import AdminUser, Client, GroupTierPrice, GroupUpgradeRequest, Payment, PaymentSession, PlanPrice
from mpesa import initiate_stk_push
from scheduler import start_scheduler
from nginx_manager import add_client_port, remove_client_port
from whatsapp import send_to_group, send_dm_text, send_document_to_group

BILLING_WEBHOOK_SECRET = os.getenv("BILLING_WEBHOOK_SECRET", "")
MPESA_CALLBACK_BASE_URL = os.getenv("MPESA_CALLBACK_BASE_URL", "https://whats2eat.com/billing")

templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "changeme")
SECRET_KEY = os.getenv("SECRET_KEY", "billing-secret-change-in-prod")


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
    """Add new columns to existing tables without a migration framework."""
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
        ]
        for table, col_def in migrations:
            try:
                await conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col_def}"))
            except Exception:
                pass  # Column already exists


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


def _next_renewal(plan: str) -> date:
    return date.today() + timedelta(days=30 if plan == "monthly" else 365)


@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request, username: str = Depends(require_login), db=Depends(get_db)):
    clients = (await db.execute(select(Client).order_by(Client.name))).scalars().all()
    prices = {p.plan_type: p for p in (await db.execute(select(PlanPrice))).scalars().all()}
    return templates.TemplateResponse(request, "dashboard.html", {
        "request": request, "clients": clients, "prices": prices, "username": username,
    })


@app.get("/clients/new", response_class=HTMLResponse)
async def new_client_form(request: Request, username: str = Depends(require_login)):
    return templates.TemplateResponse(request, "client_form.html", {"request": request, "error": None})


@app.post("/clients", response_class=HTMLResponse)
async def create_client(
    request: Request,
    name: str = Form(...),
    subdomain: str = Form(...),
    plan: str = Form(...),
    admin_whatsapp_phone: str = Form(default=""),
    backend_port: str = Form(default=""),
    username: str = Depends(require_login),
    db=Depends(get_db),
):
    subdomain = subdomain.lower().strip()
    if await db.scalar(select(Client).where(Client.subdomain == subdomain)):
        return templates.TemplateResponse(request, "client_form.html", {
            "request": request,
            "error": f"Subdomain '{subdomain}' already exists",
        })
    port_int = int(backend_port.strip()) if backend_port.strip().isdigit() else None
    client = Client(
        name=name, subdomain=subdomain, plan=plan, status="active",
        renewal_date=_next_renewal(plan), created_at=datetime.now(timezone.utc),
        admin_whatsapp_phone=admin_whatsapp_phone.strip() or None,
        backend_port=port_int,
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
    payments = (await db.execute(
        select(Payment).where(Payment.client_id == client_id).order_by(Payment.initiated_at.desc())
    )).scalars().all()
    return templates.TemplateResponse(request, "client_detail.html", {
        "request": request, "client": client, "payments": payments, "username": username,
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
    await db.commit()
    if port_changed:
        import asyncio
        await asyncio.get_event_loop().run_in_executor(
            None, add_client_port, client.subdomain, new_port
        )
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
    for plan_type, amount in amounts.items():
        existing = await db.scalar(select(PlanPrice).where(PlanPrice.plan_type == plan_type))
        if existing:
            existing.amount = amount
            existing.set_at = now
            existing.set_by = username
        else:
            db.add(PlanPrice(
                plan_type=plan_type, amount=amount,
                currency="KES", set_at=now, set_by=username,
            ))
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


def _period_end(plan: str, start: date) -> date:
    return start + timedelta(days=30 if plan == "monthly" else 365)


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
            all_payments = (await db.execute(
                select(Payment).where(Payment.client_id == client.id, Payment.status == "confirmed").order_by(Payment.initiated_at.desc())
            )).scalars().all()
            history = [
                {
                    "date": p.initiated_at.strftime("%Y-%m-%d %H:%M"),
                    "phone": p.phone,
                    "amount": str(p.amount),
                    "receipt": p.mpesa_transaction_id,
                    "status": p.status,
                    "period_start": str(p.period_start),
                    "period_end": str(p.period_end),
                }
                for p in all_payments
            ]
            pdf_bytes = generate_statement(
                client_name=client.name,
                client_plan=client.plan,
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

        prices = {p.plan_type: p for p in (await db.execute(select(PlanPrice))).scalars().all()}
        if client.plan not in prices:
            await send_to_group(client, "❌ Payment is not configured yet. Please contact your administrator.")
            await db.delete(active_session)
            await db.commit()
            return {"ok": True}
        amount = prices[client.plan].amount

        active_session.state = "awaiting_confirm"
        active_session.phone = phone
        active_session.expires_at = now + timedelta(minutes=5)
        await db.commit()

        await send_to_group(
            client,
            f"💳 You'll be charged *KES {amount}* for your {client.plan} plan to {phone}.\n"
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
        prices = {p.plan_type: p for p in (await db.execute(select(PlanPrice))).scalars().all()}
        if client.plan not in prices:
            await send_to_group(client, "❌ Payment is not configured yet. Please contact your administrator.")
            await db.delete(active_session)
            await db.commit()
            return {"ok": True}
        amount = prices[client.plan].amount

        today = date.today()
        payment = Payment(
            client_id=client.id, phone=phone, amount=amount, status="pending",
            initiated_at=now, period_start=today, period_end=_period_end(client.plan, today),
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

    session = await db.scalar(
        select(PaymentSession).where(PaymentSession.checkout_request_id == checkout_id)
    )
    if not session:
        return {"ResultCode": 0, "ResultDesc": "Accepted"}

    client = await db.get(Client, session.client_id)
    if not client:
        await db.delete(session)
        await db.commit()
        return {"ResultCode": 0, "ResultDesc": "Accepted"}
    payment = await db.get(Payment, session.payment_id) if session.payment_id else None

    if result_code == 0:
        mpesa_id = next(
            (item["Value"] for item in callback.get("CallbackMetadata", {}).get("Item", [])
             if item["Name"] == "MpesaReceiptNumber"),
            None,
        )
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
        client.renewal_date = payment.period_end if payment else _period_end(client.plan, date.today())
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
            all_payments = (await db.execute(
                select(Payment).where(Payment.client_id == client.id, Payment.status == "confirmed").order_by(Payment.initiated_at.desc())
            )).scalars().all()
            invoice_data = {
                "amount": str(amount),
                "receipt": mpesa_id,
                "phone": str(phone),
                "period_start": str(period_start),
                "period_end": str(period_end),
                "date": confirmed_at,
            } if payment else None
            history = [
                {
                    "date": p.initiated_at.strftime("%Y-%m-%d %H:%M"),
                    "phone": p.phone,
                    "amount": str(p.amount),
                    "receipt": p.mpesa_transaction_id,
                    "status": p.status,
                    "period_start": str(p.period_start),
                    "period_end": str(p.period_end),
                }
                for p in all_payments
            ]
            pdf_bytes = generate_statement(
                client_name=client.name,
                client_plan=client.plan,
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


async def _get_session_status(client: Client) -> str:
    """Return the OpenWA session status string, or a descriptive error token."""
    if not client.openwa_url or not client.openwa_session:
        return "NOT_CONFIGURED"
    try:
        async with httpx.AsyncClient(timeout=8.0) as http:
            r = await http.get(
                f"{client.openwa_url}/api/sessions",
                headers={"X-API-Key": client.openwa_api_key or ""},
            )
            r.raise_for_status()
            for s in r.json():
                if s.get("name") == client.openwa_session:
                    return s.get("status", "UNKNOWN")
            return "NOT_FOUND"
    except Exception:
        return "UNREACHABLE"


@app.get("/clients/{client_id}/whatsapp-status")
async def whatsapp_status(
    client_id: int,
    username: str = Depends(require_login),
    db=Depends(get_db),
):
    client = await db.get(Client, client_id)
    if not client:
        raise HTTPException(status_code=404)
    status = await _get_session_status(client)
    return JSONResponse({"status": status})


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
        if session_id:
            async with httpx.AsyncClient(timeout=15.0) as http:
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
    payments = (await db.execute(
        select(Payment).where(Payment.client_id == client.id).order_by(Payment.initiated_at.desc())
    )).scalars().all()
    return {
        "client": {
            "name": client.name,
            "plan": client.plan,
            "status": client.status,
            "renewal_date": str(client.renewal_date),
        },
        "payments": [
            {
                "date": p.initiated_at.strftime("%Y-%m-%d %H:%M"),
                "phone": p.phone,
                "amount": str(p.amount),
                "receipt": p.mpesa_transaction_id,
                "status": p.status,
                "period_start": str(p.period_start),
                "period_end": str(p.period_end),
            }
            for p in payments
        ],
    }


@app.get("/api/clients/{subdomain}/status")
async def client_billing_status(subdomain: str, request: Request, db=Depends(get_db)):
    secret = request.headers.get("X-Billing-Secret", "")
    if BILLING_WEBHOOK_SECRET and secret != BILLING_WEBHOOK_SECRET:
        raise HTTPException(status_code=401, detail="Unauthorized")
    client = await db.scalar(select(Client).where(Client.subdomain == subdomain))
    if not client:
        raise HTTPException(status_code=404, detail="Client not found")
    return {"status": client.status}


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
