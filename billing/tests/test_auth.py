import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport
from datetime import datetime, timezone
from passlib.context import CryptContext

pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")


@pytest_asyncio.fixture
async def http(db_session, monkeypatch):
    import database
    from sqlalchemy.ext.asyncio import async_sessionmaker
    factory = async_sessionmaker(db_session.bind, expire_on_commit=False)
    monkeypatch.setattr(database, "AsyncSessionLocal", factory)
    import main
    async with AsyncClient(transport=ASGITransport(app=main.app), base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_login_success(http, db_session):
    from models import AdminUser
    db_session.add(AdminUser(
        username="admin", hashed_password=pwd.hash("secret"),
        created_at=datetime.now(timezone.utc),
    ))
    await db_session.commit()
    r = await http.post("/login", data={"username": "admin", "password": "secret"})
    assert r.status_code in (200, 303)


@pytest.mark.asyncio
async def test_login_wrong_password(http, db_session):
    from models import AdminUser
    db_session.add(AdminUser(
        username="admin2", hashed_password=pwd.hash("right"),
        created_at=datetime.now(timezone.utc),
    ))
    await db_session.commit()
    r = await http.post("/login", data={"username": "admin2", "password": "wrong"})
    assert r.status_code == 200
    assert b"Invalid" in r.content


@pytest.mark.asyncio
async def test_protected_route_redirects_when_not_logged_in(http):
    r = await http.get("/", follow_redirects=False)
    assert r.status_code == 302
    assert "/login" in r.headers["location"]
