import pytest
from datetime import datetime, timezone
from models import IncidentCategory, Incident


async def _seed_categories(session):
    """Insert the 8 default categories into the test DB."""
    now = datetime.now(timezone.utc)
    seeds = [
        IncidentCategory(slug="plumbing",   label="Plumbing",   is_protected=False, created_at=now),
        IncidentCategory(slug="electrical", label="Electrical", is_protected=False, created_at=now),
        IncidentCategory(slug="lift",       label="Lift",       is_protected=False, created_at=now),
        IncidentCategory(slug="security",   label="Security",   is_protected=False, created_at=now),
        IncidentCategory(slug="structural", label="Structural", is_protected=False, created_at=now),
        IncidentCategory(slug="cleaning",   label="Cleaning",   is_protected=False, created_at=now),
        IncidentCategory(slug="access",     label="Access",     is_protected=False, created_at=now),
        IncidentCategory(slug="other",      label="Other",      is_protected=True,  created_at=now),
    ]
    for cat in seeds:
        session.add(cat)
    await session.commit()


# ── Auth tests ──────────────────────────────────────────────────────────────

async def test_categories_api_requires_super_admin_role(client):
    resp = await client.get("/api/super-admin/categories")
    assert resp.status_code == 403


async def test_categories_api_rejects_admin_role(authenticated_client):
    resp = await authenticated_client.get("/api/super-admin/categories")
    # authenticated_client uses dependency overrides (not a real session), so
    # require_super_admin sees no session username and returns 302 redirect.
    # Both 302 and 403 mean access is denied to non-super-admin users.
    assert resp.status_code in (302, 403)


# ── GET /api/super-admin/categories ─────────────────────────────────────────

async def test_list_categories_returns_all(super_admin_client, db_session):
    await _seed_categories(db_session)
    resp = await super_admin_client.get("/api/super-admin/categories")
    assert resp.status_code == 200
    data = resp.json()
    assert len(data) == 8
    slugs = {c["slug"] for c in data}
    assert slugs == {"plumbing", "electrical", "lift", "security", "structural", "cleaning", "access", "other"}


async def test_list_categories_includes_incident_count(super_admin_client, db_session):
    await _seed_categories(db_session)
    now = datetime.now(timezone.utc)
    db_session.add(Incident(
        group_id="g1@g.us",
        property_name="Block A",
        reporter_name="Alice",
        message_body="Pump leaking",
        category="plumbing",
        severity="high",
        confidence=0.9,
        status="review",
        received_at=now,
    ))
    await db_session.commit()
    resp = await super_admin_client.get("/api/super-admin/categories")
    assert resp.status_code == 200
    data = resp.json()
    plumbing = next(c for c in data if c["slug"] == "plumbing")
    assert plumbing["incident_count"] == 1
    electrical = next(c for c in data if c["slug"] == "electrical")
    assert electrical["incident_count"] == 0


async def test_list_categories_includes_is_protected(super_admin_client, db_session):
    await _seed_categories(db_session)
    resp = await super_admin_client.get("/api/super-admin/categories")
    data = resp.json()
    other = next(c for c in data if c["slug"] == "other")
    assert other["is_protected"] is True
    plumbing = next(c for c in data if c["slug"] == "plumbing")
    assert plumbing["is_protected"] is False


# ── POST /api/super-admin/categories ────────────────────────────────────────

async def test_create_category_success(super_admin_client, db_session):
    await _seed_categories(db_session)
    resp = await super_admin_client.post(
        "/api/super-admin/categories",
        json={"slug": "pest_control", "label": "Pest Control"},
    )
    assert resp.status_code == 201
    data = resp.json()
    assert data["slug"] == "pest_control"
    assert data["label"] == "Pest Control"
    assert data["is_protected"] is False
    assert "id" in data


async def test_create_category_duplicate_slug_returns_409(super_admin_client, db_session):
    await _seed_categories(db_session)
    resp = await super_admin_client.post(
        "/api/super-admin/categories",
        json={"slug": "plumbing", "label": "Plumbing Dupe"},
    )
    assert resp.status_code == 409


async def test_create_category_invalid_slug_returns_422(super_admin_client, db_session):
    await _seed_categories(db_session)
    resp = await super_admin_client.post(
        "/api/super-admin/categories",
        json={"slug": "Bad Slug!", "label": "Bad"},
    )
    assert resp.status_code == 422


async def test_create_category_empty_label_returns_422(super_admin_client, db_session):
    await _seed_categories(db_session)
    resp = await super_admin_client.post(
        "/api/super-admin/categories",
        json={"slug": "validslug", "label": ""},
    )
    assert resp.status_code == 422


async def test_create_category_slug_too_long_returns_422(super_admin_client, db_session):
    await _seed_categories(db_session)
    resp = await super_admin_client.post(
        "/api/super-admin/categories",
        json={"slug": "a" * 51, "label": "Too long"},
    )
    assert resp.status_code == 422


# ── GET /api/super-admin/categories/{slug}/usage ────────────────────────────

async def test_usage_returns_count_zero(super_admin_client, db_session):
    await _seed_categories(db_session)
    resp = await super_admin_client.get("/api/super-admin/categories/plumbing/usage")
    assert resp.status_code == 200
    data = resp.json()
    assert data["slug"] == "plumbing"
    assert data["incident_count"] == 0


async def test_usage_counts_incidents(super_admin_client, db_session):
    await _seed_categories(db_session)
    now = datetime.now(timezone.utc)
    for _ in range(3):
        db_session.add(Incident(
            group_id="g1@g.us",
            property_name="Block A",
            reporter_name="Alice",
            message_body="Pump leaking",
            category="plumbing",
            severity="high",
            confidence=0.9,
            status="review",
            received_at=now,
        ))
    await db_session.commit()
    resp = await super_admin_client.get("/api/super-admin/categories/plumbing/usage")
    assert resp.status_code == 200
    assert resp.json()["incident_count"] == 3


async def test_usage_returns_404_for_unknown_slug(super_admin_client, db_session):
    await _seed_categories(db_session)
    resp = await super_admin_client.get("/api/super-admin/categories/nonexistent/usage")
    assert resp.status_code == 404


# ── POST /api/super-admin/categories/{slug}/delete ──────────────────────────

async def test_delete_category_no_incidents(super_admin_client, db_session):
    await _seed_categories(db_session)
    resp = await super_admin_client.post("/api/super-admin/categories/plumbing/delete")
    assert resp.status_code == 204


async def test_delete_protected_category_returns_403(super_admin_client, db_session):
    await _seed_categories(db_session)
    resp = await super_admin_client.post("/api/super-admin/categories/other/delete")
    assert resp.status_code == 403


async def test_delete_with_incidents_no_remap_returns_409(super_admin_client, db_session):
    await _seed_categories(db_session)
    now = datetime.now(timezone.utc)
    db_session.add(Incident(
        group_id="g1@g.us",
        property_name="Block A",
        reporter_name="Alice",
        message_body="Pump leaking",
        category="plumbing",
        severity="high",
        confidence=0.9,
        status="review",
        received_at=now,
    ))
    await db_session.commit()
    resp = await super_admin_client.post("/api/super-admin/categories/plumbing/delete")
    assert resp.status_code == 409
    data = resp.json()
    # FastAPI wraps HTTPException detail under the "detail" key
    detail = data.get("detail", data)
    assert detail["incident_count"] == 1


async def test_delete_with_remap_reassigns_incidents(super_admin_client, db_session):
    await _seed_categories(db_session)
    now = datetime.now(timezone.utc)
    db_session.add(Incident(
        group_id="g1@g.us",
        property_name="Block A",
        reporter_name="Alice",
        message_body="Pump leaking",
        category="plumbing",
        severity="high",
        confidence=0.9,
        status="review",
        received_at=now,
    ))
    await db_session.commit()
    resp = await super_admin_client.post(
        "/api/super-admin/categories/plumbing/delete",
        json={"remap_to": "other"},
    )
    assert resp.status_code == 204
    # Verify incident was remapped
    from sqlalchemy import select
    result = await db_session.execute(select(Incident))
    incidents = result.scalars().all()
    assert all(i.category == "other" for i in incidents)


async def test_delete_with_invalid_remap_target_returns_422(super_admin_client, db_session):
    await _seed_categories(db_session)
    now = datetime.now(timezone.utc)
    db_session.add(Incident(
        group_id="g1@g.us",
        property_name="Block A",
        reporter_name="Alice",
        message_body="Pump leaking",
        category="plumbing",
        severity="high",
        confidence=0.9,
        status="review",
        received_at=now,
    ))
    await db_session.commit()
    resp = await super_admin_client.post(
        "/api/super-admin/categories/plumbing/delete",
        json={"remap_to": "nonexistent"},
    )
    assert resp.status_code == 422


async def test_delete_nonexistent_category_returns_404(super_admin_client, db_session):
    await _seed_categories(db_session)
    resp = await super_admin_client.post("/api/super-admin/categories/nonexistent/delete")
    assert resp.status_code == 404
