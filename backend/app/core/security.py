import os
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

import bcrypt
import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis_client import redis_client
from app.database import get_db
from app.models import User, UserRole

SECRET_KEY = os.getenv("SECRET_KEY", "fallback_dev_secret_key_change_in_env")
ALGORITHM = "HS256"
ACCESS_TOKEN_EXPIRE_MINUTES = 30 # TODO: Change
REFRESH_TOKEN_EXPIRE_DAYS = 7

bearer_scheme = HTTPBearer()


def hash_password(plain_password: str) -> str:
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(plain_password.encode("utf-8"), salt).decode("utf-8")


def verify_password(plain_password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(
        plain_password.encode("utf-8"),
        password_hash.encode("utf-8"),
    )


def create_access_token(user_id: int, role: int) -> str:
    expire = datetime.now(timezone.utc) + timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    payload = {
        "sub": str(user_id),
        "role": role,
        "type": "access",
        "exp": expire,
    }
    return jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)


async def create_refresh_token(user_id: int) -> str:
    jti = uuid.uuid4().hex
    ttl_seconds = REFRESH_TOKEN_EXPIRE_DAYS * 24 * 60 * 60
    expire = datetime.now(timezone.utc) + timedelta(seconds=ttl_seconds)

    payload = {
        "sub": str(user_id),
        "jti": jti,
        "type": "refresh",
        "exp": expire,
    }
    token = jwt.encode(payload, SECRET_KEY, algorithm=ALGORITHM)

    # Зберігаємо активний refresh-токен у Redis для підтримки ротації та виходу
    await redis_client.set(f"refresh_token:{user_id}:{jti}", "1", ex=ttl_seconds)
    return token


async def verify_and_rotate_refresh_token(refresh_token: str) -> int:
    """Перевіряє refresh-токен, видаляє його з Redis (одноразове використання) та повертає user_id."""
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Недійсний або прострочений refresh-токен. Виконайте вхід повторно.",
    )
    try:
        payload = jwt.decode(refresh_token, SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("type") != "refresh":
            raise credentials_exception
        user_id = int(payload["sub"])
        jti = payload["jti"]
    except (jwt.PyJWTError, KeyError, ValueError):
        raise credentials_exception

    redis_key = f"refresh_token:{user_id}:{jti}"
    exists = await redis_client.get(redis_key)
    if not exists:
        raise credentials_exception

    # Видаляємо використаний токен (Token Rotation — захист від повторного використання)
    await redis_client.delete(redis_key)
    return user_id


async def revoke_refresh_token(refresh_token: str) -> None:
    try:
        payload = jwt.decode(refresh_token, SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("type") == "refresh":
            await redis_client.delete(f"refresh_token:{payload['sub']}:{payload['jti']}")
    except (jwt.PyJWTError, KeyError):
        pass


async def get_current_user(
    credentials: HTTPAuthorizationCredentials = Depends(bearer_scheme),
    db: AsyncSession = Depends(get_db),
) -> User:
    token = credentials.credentials
    credentials_exception = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Недійсний або прострочений токен авторизації",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        payload = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        if payload.get("type") != "access":
            raise credentials_exception
        user_id_str: Optional[str] = payload.get("sub")
        if user_id_str is None:
            raise credentials_exception
        user_id = int(user_id_str)
    except (jwt.PyJWTError, ValueError):
        raise credentials_exception

    user = await db.scalar(select(User).where(User.id == user_id))
    if user is None:
        raise credentials_exception
    return user


async def require_admin(current_user: User = Depends(get_current_user)) -> User:
    if current_user.role != UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Доступ дозволено лише адміністратору паркінгу",
        )
    return current_user