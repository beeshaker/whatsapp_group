import pytest
from unittest.mock import AsyncMock, patch
from datetime import datetime, timezone


async def test_categories_api_requires_super_admin_role(client):
    """Regular admin (via session login) is rejected by super-admin endpoints."""
    resp = await client.get("/api/super-admin/categories")
    assert resp.status_code == 403


async def test_categories_api_rejects_unauthenticated(authenticated_client):
    """authenticated_client has admin role — still rejected by super-admin endpoint.

    The fixture uses dependency overrides for require_login/require_admin but not for
    require_super_admin, so the real require_super_admin sees no session username and
    redirects (302) or returns 403 — both mean access is denied.
    """
    resp = await authenticated_client.get("/api/super-admin/categories")
    assert resp.status_code in (302, 403)


async def test_categories_api_allows_super_admin(super_admin_client):
    """super_admin_client is accepted — gets anything other than 403."""
    resp = await super_admin_client.get("/api/super-admin/categories")
    assert resp.status_code != 403
