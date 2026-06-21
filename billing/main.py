import os
from contextlib import asynccontextmanager
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from starlette.middleware.sessions import SessionMiddleware

from auth import hash_password, verify_password, require_login
from database import get_db, init_db, AsyncSessionLocal
from models import AdminUser, Client, Payment, PlanPrice, PaymentSession

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
    yield


app = FastAPI(lifespan=lifespan)
app.add_middleware(SessionMiddleware, secret_key=SECRET_KEY)


@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/login", response_class=HTMLResponse)
async def login_submit(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db=Depends(get_db),
):
    user = await db.scalar(select(AdminUser).where(AdminUser.username == username))
    if not user or not verify_password(password, user.hashed_password):
        return templates.TemplateResponse("login.html", {"request": request, "error": "Invalid credentials"})
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
    return templates.TemplateResponse("dashboard.html", {
        "request": request, "clients": clients, "prices": prices, "username": username,
    })


@app.get("/clients/new", response_class=HTMLResponse)
async def new_client_form(request: Request, username: str = Depends(require_login)):
    return templates.TemplateResponse("client_form.html", {"request": request, "error": None})


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
        return templates.TemplateResponse("client_form.html", {
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
    return templates.TemplateResponse("client_detail.html", {
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
