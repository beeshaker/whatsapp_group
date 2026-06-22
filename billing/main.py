import hashlib
import hmac as _hmac
import json
import os
import re
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from starlette.middleware.sessions import SessionMiddleware

from auth import hash_password, verify_password, require_login
from database import get_db, init_db, AsyncSessionLocal
from docker_manager import start_client, stop_client
from models import AdminUser, Client, Payment, PaymentSession, PlanPrice
from mpesa import initiate_stk_push
from scheduler import start_scheduler
from whatsapp import send_to_group

BILLING_WEBHOOK_SECRET = os.getenv("BILLING_WEBHOOK_SECRET", "")
MPESA_CALLBACK_BASE_URL = os.getenv("MPESA_CALLBACK_BASE_URL", "https://whats2eat.com/billing")

templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

ADMIN_USERNAME = os.getenv("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.getenv("ADMIN_PASSWORD", "changeme")
SECRET_KEY = os.getenv("SECRET_KEY", "billing-secret-change-in-prod")


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


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
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
    username: str = Depends(require_login),
    db=Depends(get_db),
):
    subdomain = subdomain.lower().strip()
    if await db.scalar(select(Client).where(Client.subdomain == subdomain)):
        return templates.TemplateResponse(request, "client_form.html", {
            "request": request,
            "error": f"Subdomain '{subdomain}' already exists",
        })
    client = Client(
        name=name, subdomain=subdomain, plan=plan, status="active",
        renewal_date=_next_renewal(plan), created_at=datetime.now(timezone.utc),
    )
    db.add(client)
    await db.commit()
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
    await db.commit()
    return RedirectResponse(f"/clients/{client_id}", status_code=303)


@app.get("/prices", response_class=HTMLResponse)
async def prices_page(request: Request, username: str = Depends(require_login), db=Depends(get_db)):
    prices = {p.plan_type: p for p in (await db.execute(select(PlanPrice))).scalars().all()}
    return templates.TemplateResponse(request, "prices.html", {"request": request, "prices": prices, "username": username})


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

    if active_session and active_session.state == "awaiting_phone":
        phone = _normalize_phone(message_text)
        if not phone:
            await send_to_group(client, "❌ Invalid number. Please reply with your M-Pesa number (e.g. 0712345678):")
            return {"ok": True}

        prices = {p.plan_type: p for p in (await db.execute(select(PlanPrice))).scalars().all()}
        if client.plan not in prices:
            await send_to_group(client, "❌ Payment is not configured yet. Please contact your administrator.")
            return {"ok": True}
        amount = prices[client.plan].amount

        today = date.today()
        payment = Payment(
            client_id=client.id, phone=phone, amount=amount, status="pending",
            initiated_at=now, period_start=today, period_end=_period_end(client.plan, today),
        )
        db.add(payment)
        await db.flush()

        stk = await initiate_stk_push(
            phone=phone,
            amount=amount,
            account_ref=f"{client.subdomain}-sub",
            callback_url=f"{MPESA_CALLBACK_BASE_URL}/webhook/mpesa",
        )

        active_session.state = "awaiting_stk_confirm"
        active_session.phone = phone
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
        client.warning_sent_at = None
        client.renewal_date = payment.period_end if payment else _period_end(client.plan, date.today())
        await db.delete(session)
        await db.commit()

        await start_client(client)
        await send_to_group(
            client,
            f"🎉 Payment confirmed! Your subscription is active until {client.renewal_date}. "
            f"M-Pesa receipt: {mpesa_id}",
        )
    else:
        if payment:
            payment.status = "failed"
        await db.delete(session)
        await db.commit()
        await send_to_group(client, "❌ Payment failed or was cancelled. Type /payment to try again.")

    return {"ResultCode": 0, "ResultDesc": "Accepted"}


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
    client.warning_sent_at = None
    await db.commit()
    await start_client(client)
    await send_to_group(client, "✅ Your account has been manually reactivated by the administrator.")
    return RedirectResponse(f"/clients/{client_id}", status_code=303)


# ---------------------------------------------------------------------------
# Nginx auth-check gate
# ---------------------------------------------------------------------------

@app.get("/internal/auth-check")
async def auth_check(request: Request, db=Depends(get_db)):
    subdomain = request.headers.get("X-Client-Subdomain", "")
    if not subdomain:
        return JSONResponse(status_code=200, content={"ok": True})
    client = await db.scalar(select(Client).where(Client.subdomain == subdomain))
    if client and client.status in ("grace", "warning", "suspended"):
        raise HTTPException(status_code=403, detail="Subscription inactive")
    return JSONResponse(status_code=200, content={"ok": True})


# ---------------------------------------------------------------------------
# Payment-expired static page
# ---------------------------------------------------------------------------

@app.get("/payment-expired", response_class=HTMLResponse)
async def payment_expired_page(request: Request):
    return templates.TemplateResponse(request, "payment_expired.html", {})
