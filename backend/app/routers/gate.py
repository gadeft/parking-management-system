import math
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.redis_client import invalidate_slots_cache, log_device_event
from app.database import get_db
from app.models import (
    ParkingSession,
    ParkingSlot,
    Payment,
    PaymentStatus,
    Reservation,
    SlotStatus,
    Subscription,
    Tariff,
    Vehicle,
)
from app.routers.parking import sync_expired_reservations
from app.schemas import GateScanRequest, GateQrRequest

router = APIRouter(prefix="/api/gate", tags=["Gate Terminal & ANPR Simulator"])


async def get_default_tariff(db: AsyncSession) -> Tariff:
    tariff = await db.scalar(select(Tariff).where(Tariff.is_default.is_(True)).limit(1))
    if not tariff:
        tariff = await db.scalar(select(Tariff).order_by(Tariff.id.asc()).limit(1))
    if not tariff:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="У системі не налаштовано жодного тарифу. Зверніться до адміністратора.",
        )
    return tariff


async def has_valid_subscription(db: AsyncSession, user_id: int, now: datetime) -> Optional[Subscription]:
    return await db.scalar(
        select(Subscription)
        .options(selectinload(Subscription.plan))
        .where(and_(Subscription.user_id == user_id, Subscription.valid_until >= now))
    )


async def resolve_session_tariff(db: AsyncSession, reservation: Optional[Reservation]) -> Tariff:
    if reservation and reservation.tariff_id:
        reserved_tariff = await db.get(Tariff, reservation.tariff_id)
        if reserved_tariff:
            return reserved_tariff
    return await get_default_tariff(db)


@router.post("/entry")
async def simulate_vehicle_entry(data: GateScanRequest, db: AsyncSession = Depends(get_db)):
    await sync_expired_reservations(db)
    plate = data.plate_number
    now = datetime.now(timezone.utc)

    await log_device_event("ANPR-Entry-01", "PLATE_SCANNED", f"Розпізнано номерний знак на в'їзді: {plate}")

    vehicle = await db.scalar(select(Vehicle).where(Vehicle.plate_number == plate))
    if not vehicle:
        await log_device_event(
            "Gate-Entry-01", "ACCESS_DENIED", f"Авто {plate} не зареєстровано в системі", is_error=True
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Автомобіль із номером {plate} не знайдено. Зареєструйте авто у застосунку.",
        )

    active_session = await db.scalar(
        select(ParkingSession).where(
            and_(ParkingSession.vehicle_id == vehicle.id, ParkingSession.exit_time.is_(None))
        )
    )
    if active_session:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Автомобіль {plate} уже має активну паркувальну сесію",
        )

    reservation = await db.scalar(
        select(Reservation)
        .where(
            and_(
                Reservation.vehicle_id == vehicle.id,
                Reservation.status == "active",
                Reservation.end_time >= now,
            )
        )
        .order_by(Reservation.start_time.asc())
        .limit(1)
    )

    if reservation:
        assigned_slot = await db.get(ParkingSlot, reservation.parking_slot_id)
        reservation.status = "completed"
    else:
        assigned_slot = await db.scalar(
            select(ParkingSlot)
            .where(ParkingSlot.status == SlotStatus.AVAILABLE)
            .order_by(ParkingSlot.floor, ParkingSlot.zone, ParkingSlot.number)
            .limit(1)
        )

    if not assigned_slot:
        await log_device_event(
            "Gate-Entry-01", "NO_FREE_SLOTS", f"Відмова у в'їзді для {plate}: немає вільних місць", is_error=True
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Вільних місць на паркінгу немає",
        )

    tariff = await resolve_session_tariff(db, reservation)
    active_sub = await has_valid_subscription(db, vehicle.user_id, now)

    assigned_slot.status = SlotStatus.OCCUPIED
    new_session = ParkingSession(
        vehicle_id=vehicle.id,
        parking_slot_id=assigned_slot.id,
        tariff_id=tariff.id,
    )
    db.add(new_session)
    await db.commit()
    await db.refresh(new_session)
    await invalidate_slots_cache()

    slot_label = f"{assigned_slot.zone}{assigned_slot.number}"
    await log_device_event(
        "Barrier-Entry-01",
        "BARRIER_OPENED",
        f"Шлагбаум відкрито для {plate}. Місце {slot_label}, тариф «{tariff.name}» (Сесія #{new_session.id})",
    )

    return {
        "action": "OPEN_ENTRY_BARRIER",
        "session_id": new_session.id,
        "plate_number": plate,
        "assigned_slot": slot_label,
        "floor": assigned_slot.floor,
        "tariff": tariff.name,
        "price_per_hour": tariff.price_per_hour,
        "by_reservation": bool(reservation),
        "covered_by_subscription": bool(active_sub),
        "message": f"В'їзд дозволено. Прямуйте на місце {slot_label} (поверх {assigned_slot.floor}).",
    }


@router.post("/entry-qr")
async def simulate_qr_entry(data: GateQrRequest, db: AsyncSession = Depends(get_db)):
    await sync_expired_reservations(db)
    now = datetime.now(timezone.utc)

    parts = data.qr_code_data.strip().split("-")
    if len(parts) < 3 or parts[0] != "PARK" or parts[1] != "RES" or not parts[2].isdigit():
        await log_device_event("Gate-Entry-01", "INVALID_QR", "Зчитано некоректний QR-код", is_error=True)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Невірний формат QR-коду")

    reservation_id = int(parts[2])
    reservation = await db.get(Reservation, reservation_id)
    if not reservation or reservation.status != "active" or reservation.end_time < now:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Активне бронювання за цим QR-кодом не знайдено або термін його дії вичерпано",
        )

    vehicle = await db.get(Vehicle, reservation.vehicle_id)
    if not vehicle:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Прив'язаний до бронювання автомобіль не знайдено",
        )

    assigned_slot = await db.get(ParkingSlot, reservation.parking_slot_id)
    tariff = await resolve_session_tariff(db, reservation)
    active_sub = await has_valid_subscription(db, reservation.user_id, now)

    reservation.status = "completed"
    assigned_slot.status = SlotStatus.OCCUPIED
    new_session = ParkingSession(
        vehicle_id=vehicle.id,
        parking_slot_id=assigned_slot.id,
        tariff_id=tariff.id,
    )
    db.add(new_session)
    await db.commit()
    await db.refresh(new_session)
    await invalidate_slots_cache()

    slot_label = f"{assigned_slot.zone}{assigned_slot.number}"
    await log_device_event(
        "Gate-Entry-01",
        "QR_VERIFIED_BARRIER_OPENED",
        f"Підтверджено QR-код #{reservation.id}. Відкрито шлагбаум для {vehicle.plate_number} -> {slot_label}",
    )

    return {
        "action": "OPEN_ENTRY_BARRIER",
        "session_id": new_session.id,
        "plate_number": vehicle.plate_number,
        "assigned_slot": slot_label,
        "floor": assigned_slot.floor,
        "tariff": tariff.name,
        "price_per_hour": tariff.price_per_hour,
        "covered_by_subscription": bool(active_sub),
        "message": f"QR-код підтверджено. Заїжджайте на місце {slot_label}.",
    }


@router.post("/exit")
async def simulate_vehicle_exit(data: GateScanRequest, db: AsyncSession = Depends(get_db)):
    plate = data.plate_number
    now = datetime.now(timezone.utc)

    await log_device_event("ANPR-Exit-01", "PLATE_SCANNED", f"Розпізнано номерний знак на виїзді: {plate}")

    vehicle = await db.scalar(select(Vehicle).where(Vehicle.plate_number == plate))
    if not vehicle:
        await log_device_event(
            "Barrier-Exit-01",
            "INCIDENT_UNKNOWN_VEHICLE_EXIT",
            f"Інцидент на виїзді: незареєстроване авто {plate}. Шлагбаум відкрито в аварійному режимі.",
            is_error=True,
        )
        return {
            "action": "OPEN_EXIT_BARRIER",
            "session_id": None,
            "plate_number": plate,
            "payment_status": "incident_unknown_vehicle",
            "message": "Автомобіль не знайдено в базі. Виїзд дозволено в аварійному режимі, інцидент зафіксовано для адміністратора.",
        }

    session = await db.scalar(
        select(ParkingSession)
        .options(
            selectinload(ParkingSession.parking_slot),
            selectinload(ParkingSession.tariff),
            selectinload(ParkingSession.payment),
        )
        .where(and_(ParkingSession.vehicle_id == vehicle.id, ParkingSession.exit_time.is_(None)))
    )
    if not session:
        await log_device_event(
            "Barrier-Exit-01",
            "INCIDENT_EXIT_NO_SESSION",
            f"Інцидент на виїзді: для авто {plate} відсутня активна сесія. Шлагбаум відкрито в аварійному режимі.",
            is_error=True,
        )
        return {
            "action": "OPEN_EXIT_BARRIER",
            "session_id": None,
            "plate_number": plate,
            "payment_status": "incident_no_session",
            "message": "Активну паркувальну сесію не знайдено. Виїзд дозволено в аварійному режимі, інцидент передано адміністратору.",
        }

    # 1. Перевірка чи сесія вже оплачена онлайн
    if session.payment and session.payment.status == PaymentStatus.COMPLETED:
        session.exit_time = now
        session.parking_slot.status = SlotStatus.AVAILABLE
        await db.commit()
        await invalidate_slots_cache()
        await log_device_event("Barrier-Exit-01", "BARRIER_OPENED", f"Виїзд дозволено (оплачено): {plate}")
        return {
            "action": "OPEN_EXIT_BARRIER",
            "session_id": session.id,
            "plate_number": plate,
            "payment_status": "paid",
            "message": "Оплату підтверджено",
        }

    # 2. Перевірка активного абонемента користувача
    active_sub = await has_valid_subscription(db, vehicle.user_id, now)
    if active_sub:
        sub_payment = Payment(
            amount=0.0,
            method="subscription",
            status=PaymentStatus.COMPLETED,
        )
        db.add(sub_payment)
        await db.flush()

        session.payment_id = sub_payment.id
        session.exit_time = now
        session.parking_slot.status = SlotStatus.AVAILABLE
        await db.commit()
        await invalidate_slots_cache()
        await log_device_event("Barrier-Exit-01", "BARRIER_OPENED", f"Виїзд за абонементом: {plate}")
        return {
            "action": "OPEN_EXIT_BARRIER",
            "session_id": session.id,
            "plate_number": plate,
            "payment_status": "covered_by_subscription",
            "message": f"Виїзд дозволено за абонементом «{active_sub.plan.name}».",
        }

    # 3. Розрахунок вартості за тарифом, зафіксованим у сесії, та блокування виїзду
    duration_sec = max((now - session.entry_time).total_seconds(), 60)
    billable_hours = max(1, round(duration_sec / 3600)) # TODO: Was math.ceil, now round
    amount = round(billable_hours * session.tariff.price_per_hour, 2)

    await log_device_event(
        "Barrier-Exit-01",
        "EXIT_BLOCKED_UNPAID",
        f"Виїзд заблоковано для {plate}: очікується оплата {amount} грн (тариф «{session.tariff.name}»)",
        is_error=True,
    )

    return {
        "action": "BLOCK_EXIT_PAYMENT_REQUIRED",
        "session_id": session.id,
        "plate_number": plate,
        "tariff": session.tariff.name,
        "amount_due": amount,
        "billable_hours": billable_hours,
        "message": f"Виїзд заблоковано. Сплатіть {amount} грн у застосунку.",
    }