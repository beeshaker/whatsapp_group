from passlib.context import CryptContext
from fastapi import HTTPException, Request
from typing import Optional
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto", bcrypt__rounds=12)


def hash_password(password: str) -> str:
    return pwd_context.hash(password)


def verify_password(plain: str, hashed: str) -> bool:
    return pwd_context.verify(plain, hashed)


async def require_login(request: Request) -> str:
    username = request.session.get("username")
    if not username:
        raise HTTPException(status_code=302, headers={"Location": "/login"})
    return username


async def require_admin(request: Request) -> str:
    username = request.session.get("username")
    if not username:
        raise HTTPException(status_code=302, headers={"Location": "/login"})
    if request.session.get("role", "user") != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return username


async def check_incident_group_access(
    actor: Optional[str],
    incident_id: int,
    db: AsyncSession,
) -> None:
    """Raises 403 if a session user lacks group membership for the incident's group.
    No-ops when actor is None (API-key callers bypass scoping)."""
    if actor is None:
        return
    from models import User, UserGroup, Incident
    result = await db.execute(select(User).where(User.username == actor))
    user = result.scalar_one_or_none()
    if not user or user.role == "admin":
        return
    incident = await db.get(Incident, incident_id)
    if not incident:
        return
    membership = await db.execute(
        select(UserGroup).where(
            UserGroup.user_id == user.id,
            UserGroup.group_id == incident.group_id,
        )
    )
    if not membership.scalar_one_or_none():
        raise HTTPException(status_code=403, detail="Access denied")
