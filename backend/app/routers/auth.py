from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.security import (
    create_access_token,
    create_refresh_token,
    get_current_user,
    hash_password,
    revoke_refresh_token,
    verify_and_rotate_refresh_token,
    verify_password,
)
from app.database import get_db
from app.models import User, UserRole
from app.schemas import RefreshTokenRequest, TokenResponse, UserLogin, UserOut, UserRegister

router = APIRouter(prefix="/api/auth", tags=["Auth & Users"])


@router.post("/register", response_model=TokenResponse, status_code=status.HTTP_201_CREATED)
async def register_user(data: UserRegister, db: AsyncSession = Depends(get_db)):
    existing = await db.scalar(
        select(User).where(or_(User.email == data.email, User.phone == data.phone))
    )
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Користувач із таким email або номером телефону вже існує",
        )

    new_user = User(
        name=data.name,
        surname=data.surname,
        middlename=data.middlename,
        email=data.email,
        phone=data.phone,
        password_hash=hash_password(data.password),
        role=UserRole.DRIVER,
    )
    db.add(new_user)
    await db.commit()
    await db.refresh(new_user)

    access_token = create_access_token(user_id=new_user.id, role=new_user.role)
    refresh_token = await create_refresh_token(user_id=new_user.id)
    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        role=new_user.role,
        user_id=new_user.id,
        full_name=f"{new_user.surname} {new_user.name}",
    )


@router.post("/login", response_model=TokenResponse)
async def login_user(data: UserLogin, db: AsyncSession = Depends(get_db)):
    user = await db.scalar(
        select(User).where(or_(User.email == data.login, User.phone == data.login))
    )
    if not user or not verify_password(data.password, user.password_hash):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Невірний логін або пароль",
        )

    access_token = create_access_token(user_id=user.id, role=user.role)
    refresh_token = await create_refresh_token(user_id=user.id)
    return TokenResponse(
        access_token=access_token,
        refresh_token=refresh_token,
        role=user.role,
        user_id=user.id,
        full_name=f"{user.surname} {user.name}",
    )


@router.post("/refresh", response_model=TokenResponse)
async def refresh_tokens(data: RefreshTokenRequest, db: AsyncSession = Depends(get_db)):
    user_id = await verify_and_rotate_refresh_token(data.refresh_token)
    user = await db.scalar(select(User).where(User.id == user_id))
    if not user:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Користувача не знайдено")

    new_access = create_access_token(user_id=user.id, role=user.role)
    new_refresh = await create_refresh_token(user_id=user.id)
    return TokenResponse(
        access_token=new_access,
        refresh_token=new_refresh,
        role=user.role,
        user_id=user.id,
        full_name=f"{user.surname} {user.name}",
    )


@router.post("/logout", status_code=status.HTTP_200_OK)
async def logout_user(data: RefreshTokenRequest):
    await revoke_refresh_token(data.refresh_token)
    return {"message": "Вихід із системи виконано, токен анульовано"}


@router.get("/me", response_model=UserOut)
async def get_profile(current_user: User = Depends(get_current_user)):
    return current_user