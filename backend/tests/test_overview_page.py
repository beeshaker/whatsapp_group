import importlib
from datetime import datetime, timezone

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

import main as backend_main
from database import get_db


@pytest.fixture(autouse=True)
def _restore_main_module_state():
    yield
    importlib.reload(backend_main)


@pytest_asyncio.fixture
async def lead_client(monkeypatch):
    monkeypatch.setenv("LEAD_MODE", "true")
    from tests.conftest import _TestSession
    from auth import require_login, require_admin, hash_password
    from models import User
    importlib.reload(backend_main)

    async def _override_get_db():
        async with _TestSession() as session:
            yield session

    async def _override_require_login():
        return "leadadmin"

    async def _override_require_admin():
        return "leadadmin"

    backend_main.app.dependency_overrides[get_db] = _override_get_db
    backend_main.app.dependency_overrides[require_login] = _override_require_login
    backend_main.app.dependency_overrides[require_admin] = _override_require_admin
    async with _TestSession() as session:
        session.add(User(
            username="leadadmin",
            hashed_password=hash_password("irrelevant"),
            created_at=datetime.now(timezone.utc),
            role="admin",
        ))
        await session.commit()
    async with AsyncClient(transport=ASGITransport(app=backend_main.app), base_url="http://test") as c:
        yield c
    backend_main.app.dependency_overrides.clear()


async def test_overview_404_when_lead_mode_unset(authenticated_client):
    response = await authenticated_client.get("/overview")
    assert response.status_code == 404


async def test_overview_requires_login(monkeypatch):
    monkeypatch.setenv("LEAD_MODE", "true")
    from tests.conftest import _TestSession
    importlib.reload(backend_main)

    async def _override_get_db():
        async with _TestSession() as session:
            yield session

    backend_main.app.dependency_overrides[get_db] = _override_get_db
    async with AsyncClient(transport=ASGITransport(app=backend_main.app), base_url="http://test") as c:
        response = await c.get("/overview")
    backend_main.app.dependency_overrides.clear()
    assert response.status_code == 302
    assert response.headers["location"] == "/login"


async def test_overview_200_and_sidebar_when_lead_mode_set(lead_client):
    response = await lead_client.get("/overview")
    assert response.status_code == 200
    assert b'class="lead-sidebar"' in response.content
    assert b'href="/overview"' in response.content
    assert b'/static/css/lead-theme.css' in response.content
