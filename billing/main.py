import os
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import select
from starlette.middleware.sessions import SessionMiddleware

from auth import hash_password, verify_password, require_login
from database import get_db, init_db, AsyncSessionLocal
from models import AdminUser

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


@app.get("/", response_class=HTMLResponse)
async def root_stub(request: Request, username: str = Depends(require_login)):
    return HTMLResponse("<h1>Billing Dashboard stub</h1>")
