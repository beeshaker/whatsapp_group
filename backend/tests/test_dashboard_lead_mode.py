import importlib
from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
import pytest_asyncio
from httpx import AsyncClient, ASGITransport

import main as backend_main
from database import get_db

GATEWAY_TOKEN = "lead-dashboard-gateway-secret"


@pytest.fixture(autouse=True)
def _restore_main_module_state():
    yield
    importlib.reload(backend_main)


@pytest_asyncio.fixture
async def lead_client(monkeypatch):
    monkeypatch.setenv("LEAD_MODE", "true")
    monkeypatch.setenv("GATEWAY_SECRET_TOKEN", GATEWAY_TOKEN)
    from tests.conftest import _TestSession
    from auth import require_login, require_admin, hash_password
    from models import User, IncidentCategory
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
        # IncidentCategory rows are normally seeded by database.init_db() during the
        # FastAPI lifespan, which httpx's ASGITransport never triggers in tests (see
        # tests/test_ticket_detail_update.py for the same pattern). Without at least
        # one row, `categories` in the dashboard template context is always empty,
        # so the property-type breakdown pills and category filter chips (which both
        # iterate `categories`) would never render regardless of lead_mode.
        now = datetime.now(timezone.utc)
        session.add(IncidentCategory(slug="apartment", label="Apartment", is_protected=False, created_at=now))
        session.add(IncidentCategory(slug="house", label="House", is_protected=False, created_at=now))
        await session.commit()
    async with AsyncClient(transport=ASGITransport(app=backend_main.app), base_url="http://test") as c:
        yield c
    backend_main.app.dependency_overrides.clear()


async def test_dashboard_shows_contact_name_and_hides_priority_column(lead_client):
    classification = {"issues": [{
        "category": "apartment", "priority": "low", "confidence": 0.9,
        "message_snippet": "looking for a 4br", "contact_name": "Samson",
        "contact_phone": "254746823554", "lead_location": "General Mathenge",
        "lead_budget": "3000usd", "transaction_type": "rent",
        "lead_agent": "Jabeen", "lead_source": "Website Enquiry",
    }]}
    payload = {
        "event": "message.received",
        "data": {
            "type": "chat", "isGroup": True,
            "chatId": "dunhill@g.us", "chat": {"name": "Dunhill Sales"},
            "author": "254790458670@c.us",
            "body": "@~Jabeen kindly contact Samson 0746823554 for a 4br (Website Enquiry)",
            "timestamp": 1782300000,
        },
    }
    with patch("main.classify_message", new=AsyncMock(return_value=classification)):
        with patch("main.push_incident", new=AsyncMock()):
            await lead_client.post(
                "/api/v1/ops/ingest", json=payload, headers={"X-API-Key": GATEWAY_TOKEN}
            )
    response = await lead_client.get("/")
    assert response.status_code == 200
    assert b"Samson" in response.content
    # Scope this check to the server-rendered markup, not the whole response body.
    # dashboard.html's shared inline <script> intentionally contains the literal
    # string `class="col-prio"` inside buildRow()'s non-lead-mode branch (Step 11) —
    # that's client-side JS source text present for every client regardless of the
    # runtime LEAD_MODE value, since JS can't omit an unreached ternary branch's
    # source. What must actually be absent in lead mode is the server-rendered
    # <th>/<td class="col-prio"> markup, i.e. everything before the <script> tag.
    rendered_markup = response.content.split(b"<script>")[0]
    assert b'class="col-prio"' not in rendered_markup


async def test_dashboard_shows_property_type_label_not_category(lead_client):
    response = await lead_client.get("/")
    assert response.status_code == 200
    assert b"Property type" in response.content
    assert b"data-cnt-cat=" in response.content  # breakdown pills present


async def test_non_lead_dashboard_unaffected():
    """Regression: default client (LEAD_MODE unset) keeps the Category label and priority column."""
    import importlib as _importlib
    _importlib.reload(backend_main)
    from tests.conftest import _TestSession
    from auth import require_login, require_admin, hash_password
    from models import User
    from datetime import datetime as _dt, timezone as _tz

    async def _override_get_db():
        async with _TestSession() as session:
            yield session

    async def _override_require_login():
        return "plainadmin"

    async def _override_require_admin():
        return "plainadmin"

    backend_main.app.dependency_overrides[get_db] = _override_get_db
    backend_main.app.dependency_overrides[require_login] = _override_require_login
    backend_main.app.dependency_overrides[require_admin] = _override_require_admin
    async with _TestSession() as session:
        session.add(User(
            username="plainadmin", hashed_password=hash_password("irrelevant"),
            created_at=_dt.now(_tz.utc), role="admin",
        ))
        await session.commit()
    async with AsyncClient(transport=ASGITransport(app=backend_main.app), base_url="http://test") as c:
        response = await c.get("/")
    backend_main.app.dependency_overrides.clear()
    assert response.status_code == 200
    assert b'class="col-prio"' in response.content
    assert b">Category</th>" in response.content


async def test_non_lead_ticket_row_has_no_lead_mode_byte_differences():
    """Regression: the server-rendered <tr> for a non-lead client must not emit the
    lead-only data-agent attribute or leave empty contact_name/lead_agent segments
    behind in data-search — both are byte differences from the pre-lead-mode markup."""
    import importlib as _importlib
    _importlib.reload(backend_main)
    from tests.conftest import _TestSession
    from auth import require_login, require_admin, hash_password
    from models import User
    from datetime import datetime as _dt, timezone as _tz

    async def _override_get_db():
        async with _TestSession() as session:
            yield session

    async def _override_require_login():
        return "plainadmin2"

    async def _override_require_admin():
        return "plainadmin2"

    backend_main.app.dependency_overrides[get_db] = _override_get_db
    backend_main.app.dependency_overrides[require_login] = _override_require_login
    backend_main.app.dependency_overrides[require_admin] = _override_require_admin
    async with _TestSession() as session:
        session.add(User(
            username="plainadmin2", hashed_password=hash_password("irrelevant"),
            created_at=_dt.now(_tz.utc), role="admin",
        ))
        await session.commit()

    classification = {"issues": [{
        "category": "plumbing", "priority": "high", "confidence": 0.9,
        "message_snippet": "pump leaking",
    }]}
    payload = {
        "event": "message.received",
        "data": {
            "type": "chat", "isGroup": True,
            "chatId": "block-b@g.us", "chat": {"name": "Block B"},
            "author": "2542@c.us", "notifyName": "Bob",
            "body": "Pump leaking", "timestamp": 1782293341,
        },
    }
    async with AsyncClient(transport=ASGITransport(app=backend_main.app), base_url="http://test") as c:
        with patch("main.classify_message", new=AsyncMock(return_value=classification)):
            with patch("main.push_incident", new=AsyncMock()):
                await c.post("/api/v1/ops/ingest", json=payload, headers={"X-API-Key": "test-secret"})
        response = await c.get("/")
    backend_main.app.dependency_overrides.clear()
    assert response.status_code == 200
    # Scope to the server-rendered markup, not the shared <script> — buildRow()'s
    # JS source text always contains the literal "data-agent=" regardless of the
    # runtime LEAD_MODE value (see test_dashboard_shows_contact_name_and_hides_priority_column).
    rendered_markup = response.content.split(b"<script>")[0]
    assert b"data-agent=" not in rendered_markup
    assert b"block b bob   pump leaking" not in rendered_markup
    assert b"block b bob pump leaking" in rendered_markup


async def test_lead_dashboard_uses_sidebar_shell(lead_client):
    response = await lead_client.get("/")
    assert response.status_code == 200
    assert b'class="lead-shell"' in response.content
    assert b'class="lead-sidebar"' in response.content
    assert b'/static/css/lead-theme.css' in response.content


async def test_lead_archive_uses_sidebar_shell(lead_client):
    response = await lead_client.get("/archive")
    assert response.status_code == 200
    assert b'class="lead-shell"' in response.content
    assert b'href="/archive"' in response.content


async def test_non_lead_dashboard_does_not_use_sidebar_shell():
    import importlib as _importlib
    _importlib.reload(backend_main)
    from tests.conftest import _TestSession
    from auth import require_login, require_admin, hash_password
    from models import User
    from datetime import datetime as _dt, timezone as _tz

    async def _override_get_db():
        async with _TestSession() as session:
            yield session

    async def _override_require_login():
        return "plainadmin3"

    async def _override_require_admin():
        return "plainadmin3"

    backend_main.app.dependency_overrides[get_db] = _override_get_db
    backend_main.app.dependency_overrides[require_login] = _override_require_login
    backend_main.app.dependency_overrides[require_admin] = _override_require_admin
    async with _TestSession() as session:
        session.add(User(
            username="plainadmin3", hashed_password=hash_password("irrelevant"),
            created_at=_dt.now(_tz.utc), role="admin",
        ))
        await session.commit()
    async with AsyncClient(transport=ASGITransport(app=backend_main.app), base_url="http://test") as c:
        response = await c.get("/")
    backend_main.app.dependency_overrides.clear()
    assert response.status_code == 200
    assert b'class="lead-shell"' not in response.content
    assert b'lead-theme.css' not in response.content
    assert b'{% include "_nav.html"' not in response.content  # sanity: nav actually rendered, not left as raw Jinja
    assert b'class="nav"' in response.content


async def test_lead_dashboard_stat_cards_use_lead_theme_classes(lead_client):
    response = await lead_client.get("/")
    assert response.status_code == 200
    assert b'class="lead-stat-card' in response.content


async def test_lead_dashboard_status_tabs_present_for_live(lead_client):
    response = await lead_client.get("/")
    assert response.status_code == 200
    body = response.content
    assert b'class="lead-status-tabs"' in body
    assert b">All<" in body
    assert b'data-val="new"' in body
    assert b'data-val="contacted"' in body
    assert b'data-val="closed_won"' not in body  # Won/Lost don't apply to the live queue
    assert b'data-val="closed_lost"' not in body


async def test_lead_archive_status_tabs_present_for_won_lost(lead_client):
    response = await lead_client.get("/archive")
    assert response.status_code == 200
    body = response.content
    assert b'class="lead-status-tabs"' in body
    assert b'data-val="closed_won"' in body
    assert b'data-val="closed_lost"' in body
    assert b'data-val="new"' not in body


async def test_lead_dashboard_toolbar_has_disabled_columns_and_download(lead_client):
    response = await lead_client.get("/")
    assert response.status_code == 200
    body = response.content
    assert b'>Columns<' in body
    assert b'>Download<' in body
    disabled_count = body.count(b'class="lead-toolbar-btn-disabled" disabled')
    assert disabled_count == 2


async def test_lead_dashboard_has_single_filters_button_wrapping_category_and_agent_chips(lead_client):
    response = await lead_client.get("/")
    assert response.status_code == 200
    body = response.content
    assert b'onclick="toggleLeadFiltersPanel()"' in body
    assert b'id="lead-filters-panel"' in body
    # the existing category/agent chip-dropdowns still render, just nested inside the panel now
    assert b'data-group="category"' in body
    assert b'data-group="agent"' in body


async def test_non_lead_dashboard_has_no_filters_button():
    import importlib as _importlib
    _importlib.reload(backend_main)
    from tests.conftest import _TestSession
    from auth import require_login, require_admin, hash_password
    from models import User
    from datetime import datetime as _dt, timezone as _tz

    async def _override_get_db():
        async with _TestSession() as session:
            yield session

    async def _override_require_login():
        return "plainadmin5"

    async def _override_require_admin():
        return "plainadmin5"

    backend_main.app.dependency_overrides[get_db] = _override_get_db
    backend_main.app.dependency_overrides[require_login] = _override_require_login
    backend_main.app.dependency_overrides[require_admin] = _override_require_admin
    async with _TestSession() as session:
        session.add(User(
            username="plainadmin5", hashed_password=hash_password("irrelevant"),
            created_at=_dt.now(_tz.utc), role="admin",
        ))
        await session.commit()
    async with AsyncClient(transport=ASGITransport(app=backend_main.app), base_url="http://test") as c:
        response = await c.get("/")
    backend_main.app.dependency_overrides.clear()
    rendered_markup = response.content.split(b"<script>")[0]
    assert b'toggleLeadFiltersPanel' not in rendered_markup
    assert b'id="lead-filters-panel"' not in rendered_markup


async def test_lead_dashboard_row_data_attributes_unchanged(lead_client):
    classification = {"issues": [{
        "category": "apartment", "priority": "low", "confidence": 0.9,
        "message_snippet": "looking for a 4br", "contact_name": "Frank",
        "contact_phone": "254700000000", "lead_agent": "Jabeen",
    }]}
    payload = {
        "event": "message.received",
        "data": {
            "type": "chat", "isGroup": True,
            "chatId": "dunhill@g.us", "chat": {"name": "Dunhill Sales"},
            "author": "254790000000@c.us",
            "body": "@~Jabeen contact Frank 0700000000",
            "timestamp": 1782300100,
        },
    }
    from unittest.mock import AsyncMock, patch
    with patch("main.classify_message", new=AsyncMock(return_value=classification)):
        with patch("main.push_incident", new=AsyncMock()):
            await lead_client.post(
                "/api/v1/ops/ingest", json=payload, headers={"X-API-Key": GATEWAY_TOKEN}
            )
    response = await lead_client.get("/")
    body = response.content
    assert b'data-id="' in body
    assert b'data-priority="' in body
    assert b'data-status="new"' in body
    assert b'data-cat="apartment"' in body
    assert b'data-agent="Jabeen"' in body
    assert b'data-search="' in body
