from datetime import datetime, timezone

from sqlalchemy import select

from auth import hash_password, verify_password
from models import AdminGroupSubscription, AdminProfile, AuditLog, User, UserGroup


async def _add_user(db, username, role="user"):
    user = User(
        username=username,
        hashed_password=hash_password("oldpassword"),
        role=role,
        created_at=datetime.now(timezone.utc),
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def test_delete_user_removes_dependent_rows(authenticated_client, db_session):
    user = await _add_user(db_session, "rider", role="admin")
    db_session.add_all([
        UserGroup(user_id=user.id, group_id="g1@g.us"),
        AdminProfile(user_id=user.id, whatsapp_phone="254700000001"),
        AdminGroupSubscription(user_id=user.id, group_id="g1@g.us"),
    ])
    await db_session.commit()

    resp = await authenticated_client.post(f"/users/{user.id}/delete")

    assert resp.status_code == 200
    assert await db_session.get(User, user.id, populate_existing=True) is None
    for model in (UserGroup, AdminGroupSubscription, AdminProfile):
        rows = (await db_session.execute(select(model).where(model.user_id == user.id))).all()
        assert rows == []
    audit = (await db_session.execute(select(AuditLog).where(AuditLog.action == "user_delete"))).scalar_one()
    assert audit.username == "testadmin"


async def test_admin_cannot_delete_super_admin(authenticated_client, db_session):
    boss = await _add_user(db_session, "boss", role="super_admin")

    resp = await authenticated_client.post(f"/users/{boss.id}/delete")

    assert resp.status_code == 403
    assert await db_session.get(User, boss.id, populate_existing=True) is not None


async def test_super_admin_resets_password(super_admin_client, db_session):
    user = await _add_user(db_session, "rider")

    resp = await super_admin_client.post(f"/users/{user.id}/password", json={"password": "newpassword"})

    assert resp.status_code == 200
    refreshed = await db_session.get(User, user.id, populate_existing=True)
    assert verify_password("newpassword", refreshed.hashed_password)


async def test_password_reset_rejects_short_password(super_admin_client, db_session):
    user = await _add_user(db_session, "rider")

    resp = await super_admin_client.post(f"/users/{user.id}/password", json={"password": "short"})

    assert resp.status_code == 422


async def test_super_admin_cannot_reset_other_super_admin(super_admin_client, db_session):
    other = await _add_user(db_session, "otherboss", role="super_admin")

    resp = await super_admin_client.post(f"/users/{other.id}/password", json={"password": "newpassword"})

    assert resp.status_code == 403


async def test_admin_cannot_reset_password(authenticated_client, db_session):
    user = await _add_user(db_session, "rider")

    resp = await authenticated_client.post(
        f"/users/{user.id}/password", json={"password": "newpassword"}, follow_redirects=False,
    )

    assert resp.status_code in (302, 403)
    assert verify_password("oldpassword", (await db_session.get(User, user.id, populate_existing=True)).hashed_password)
