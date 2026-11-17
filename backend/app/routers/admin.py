from datetime import datetime, timezone
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import and_, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.redis_client import get_device_events, invalidate_slots_cache, log_device_event
from app.core.security import require_admin
from app.database import get_db
from app.models import (
    ParkingSession,
    ParkingSlot,
    Payment,
    PaymentStatus,
    SlotStatus,
    Tariff,
    User,
)
from app.schemas import SlotStatusUpdate, TariffCreate, TariffOut, TariffUpdate

router = APIRouter(prefix="/api/admin", tags=["Admin Panel & Analytics"])


@router.get("/stats")
async def get_dashboard_stats(
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    total_slots = await db.scalar(select(func.count(ParkingSlot.id))) or 0
    occupied_or_reserved = (
        await db.scalar(
            select(func.count(ParkingSlot.id)).where(
                ParkingSlot.status.in_([SlotStatus.OCCUPIED, SlotStatus.RESERVED])
            )
        )
        or 0
    )
    occupancy_percent = round((occupied_or_reserved / total_slots * 100), 1) if total_slots else 0.0

    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
    daily_revenue = (
        await db.scalar(
            select(func.coalesce(func.sum(Payment.amount), 0.0)).where(
                and_(
                    Payment.status == PaymentStatus.COMPLETED,
                    Payment.created_at >= today_start,
                )
            )
        )
        or 0.0
    )

    active_sessions_count = (
        await db.scalar(
            select(func.count(ParkingSession.id)).where(ParkingSession.exit_time.is_(None))
        )
        or 0
    )

    events = await get_device_events(limit=15)
    errors_count = sum(1 for e in events if e.get("is_error"))

    return {
        "total_slots": total_slots,
        "occupied_or_reserved": occupied_or_reserved,
        "occupancy_percent": occupancy_percent,
        "daily_revenue": round(float(daily_revenue), 2),
        "active_sessions": active_sessions_count,
        "recent_errors_count": errors_count,
        "device_events": events,
    }


@router.post("/tariffs", response_model=TariffOut, status_code=status.HTTP_201_CREATED)
async def create_tariff(
    data: TariffCreate,
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    tariff = Tariff(name=data.name, price_per_hour=data.price_per_hour)
    db.add(tariff)
    await db.commit()
    await db.refresh(tariff)
    return tariff


@router.patch("/tariffs/{tariff_id}", response_model=TariffOut)
async def update_tariff(
    tariff_id: int,
    data: TariffUpdate,
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    tariff = await db.get(Tariff, tariff_id)
    if not tariff:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Тариф не знайдено")

    if data.name is not None:
        tariff.name = data.name
    if data.price_per_hour is not None:
        tariff.price_per_hour = data.price_per_hour

    await db.commit()
    await db.refresh(tariff)
    return tariff


@router.patch("/slots/{slot_id}/status")
async def change_slot_status(
    slot_id: int,
    data: SlotStatusUpdate,
    _: User = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
):
    slot = await db.get(ParkingSlot, slot_id)
    if not slot:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Місце не знайдено")

    slot.status = data.status
    await db.commit()
    await invalidate_slots_cache()
    await log_device_event(
        "Admin-Console",
        "SLOT_STATUS_CHANGED",
        f"Адміністратор змінив статус місця {slot.zone}{slot.number} на код {data.status}",
    )
    return {"id": slot.id, "label": f"{slot.zone}{slot.number}", "status": slot.status}


@router.post("/devices/simulate-fault") # TODO: How does it work?
async def simulate_device_fault(
    device_name: str = "ANPR-Camera-02",
    _: User = Depends(require_admin),
):
    """Тестовий ендпоінт для перевірки тест-кейсу №8 (відмова камери/сенсора)."""
    await log_device_event(
        device_name,
        "DEVICE_OFFLINE",
        f"Втрачено зв'язок із пристроєм {device_name}. Потрібна технічна перевірка!",
        is_error=True,
    )
    return {"status": "logged", "device": device_name}