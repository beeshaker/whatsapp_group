import os
from passlib.context import CryptContext
from fastapi import HTTPException, Request

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
