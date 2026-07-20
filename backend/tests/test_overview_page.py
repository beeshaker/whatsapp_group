import importlib
import re
import zoneinfo
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

import main as backend_main
from database import get_db

KENYA_TZ = zoneinfo.ZoneInfo("Africa/Nairobi")


def _boundaries():
    now = datetime.now(KENYA_TZ)
    start_of_day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    start_of_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start_of_day, start_of_month


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


async def test_overview_stat_cards_and_widgets_compute_correct_numbers(lead_client):
    from tests.conftest import _TestSession
    from models import Incident, IncidentStatusHistory

    start_of_day, start_of_month = _boundaries()
    today_ts = start_of_day + timedelta(hours=2)
    yesterday_ts = start_of_day - timedelta(hours=1)
    this_month_ts = start_of_month + timedelta(days=1, hours=1)
    last_month_ts = start_of_month - timedelta(days=1)

    def _incident(status, received_at, contact_name):
        return Incident(
            group_id="dunhill@g.us", property_name="n/a", message_body="msg",
            category="apartment", priority="medium", confidence=0.9,
            status=status, received_at=received_at, contact_name=contact_name,
        )

    async with _TestSession() as session:
        i1 = _incident("new", today_ts, "Alice")
        i2 = _incident("new", yesterday_ts, "Bob")
        i3 = _incident("contacted", today_ts, "Carol")
        i4 = _incident("closed_won", yesterday_ts, "Dan")
        i5 = _incident("closed_lost", yesterday_ts, "Eve")
        session.add_all([i1, i2, i3, i4, i5])
        await session.commit()
        for i in (i1, i2, i3, i4, i5):
            await session.refresh(i)
        session.add(IncidentStatusHistory(
            incident_id=i4.id, from_status="contacted", to_status="closed_won",
            changed_at=this_month_ts, changed_by="agent1",
        ))
        session.add(IncidentStatusHistory(
            incident_id=i5.id, from_status="contacted", to_status="closed_lost",
            changed_at=last_month_ts, changed_by="agent1",
        ))
        await session.commit()

    response = await lead_client.get("/overview")
    assert response.status_code == 200
    body = response.content

    assert b'id="ov-received-today">2<' in body
    assert b'id="ov-new">2<' in body
    assert b'id="ov-contacted">1<' in body
    assert b'id="ov-won-month">1<' in body
    assert b'id="ov-lost-month">0<' in body

    assert b'id="ov-flow-total">2<' in body
    assert b'id="ov-flow-new">1<' in body
    assert b'id="ov-flow-contacted">1<' in body
    assert b'id="ov-flow-won">0<' in body
    assert b'id="ov-flow-lost">0<' in body

    assert b'id="ov-conversion-rate">33.3%' in body

    assert b"Alice" in body
    assert b"Bob" in body


async def test_overview_newest_unactioned_leads_capped_at_five(lead_client):
    from tests.conftest import _TestSession
    from models import Incident

    start_of_day, _ = _boundaries()
    async with _TestSession() as session:
        for n in range(7):
            session.add(Incident(
                group_id="dunhill@g.us", property_name="n/a", message_body="msg",
                category="apartment", priority="medium", confidence=0.9,
                status="new", received_at=start_of_day + timedelta(hours=n),
                contact_name=f"Contact{n}",
            ))
        await session.commit()

    response = await lead_client.get("/overview")
    assert response.status_code == 200
    # NOTE: count only "Contact<digit>" (the seeded contact_name values), not
    # bare "Contact" — the page's static "Contacted" labels (stat card +
    # Lead Flow widget, per spec §5's wording) also contain "Contact" as a
    # substring, so a bare substring count would always overcount by 2
    # regardless of how many leads are in the "newest unactioned" list.
    assert len(re.findall(rb"Contact\d", response.content)) == 5


async def test_overview_no_unactioned_leads_shows_empty_state(lead_client):
    response = await lead_client.get("/overview")
    assert response.status_code == 200
    assert b"No new leads waiting" in response.content


async def test_overview_newest_unactioned_shows_nairobi_local_time_not_utc(lead_client):
    from tests.conftest import _TestSession
    from models import Incident

    # UTC 22:00 is 01:00 the next day in Africa/Nairobi (UTC+3).
    utc_received_at = datetime(2026, 7, 19, 22, 0, tzinfo=timezone.utc)
    expected_local = utc_received_at.astimezone(KENYA_TZ).strftime("%H:%M")
    assert expected_local == "01:00"

    async with _TestSession() as session:
        session.add(Incident(
            group_id="dunhill@g.us", property_name="n/a", message_body="msg",
            category="apartment", priority="medium", confidence=0.9,
            status="new", received_at=utc_received_at, contact_name="LateNightLead",
        ))
        await session.commit()

    response = await lead_client.get("/overview")
    assert response.status_code == 200
    assert b"LateNightLead &middot; 01:00" in response.content
    assert b"22:00" not in response.content
