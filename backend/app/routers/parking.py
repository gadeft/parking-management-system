import math
from datetime import datetime, timedelta, timezone
from typing import List
import uuid

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.redis_client import get_cached_slots, invalidate_slots_cache, set_cached_slots
from app.core.security import get_current_user
from app.database import get_db
from app.models import (
    ParkingSession,
    ParkingSlot,
    PaymentStatus,
    Payment,
    Reservation,
    SlotStatus,
    Subscription,
    Tariff,
    User,
    Vehicle,
    SubscriptionPlan,
)
from app.schemas import (
    ReservationCreate,
    ReservationOut,
    SessionOut,
    SlotOut,
    SubscriptionOut,
    TariffOut,
    VehicleCreate,
    VehicleOut,
    SubscriptionCheckoutRequest,
    SubscriptionCheckoutResponse,
    SubscriptionPlanOut
)


router = APIRouter(prefix="/api/parking", tags=["Driver Parking Operations"])


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


async def sync_expired_reservations(db: AsyncSession) -> None:
    """Переводить прострочені бронювання в статус expired і звільняє місця."""
    now = datetime.now(timezone.utc)
    expired_res = (
        await db.scalars(
            select(Reservation).where(
                and_(Reservation.status == "active", Reservation.end_time < now)
            )
        )
    ).all()

    if expired_res:
        for res in expired_res:
            res.status = "expired"
            slot = await db.get(ParkingSlot, res.parking_slot_id)
            if slot and slot.status == SlotStatus.RESERVED:
                slot.status = SlotStatus.AVAILABLE
        await db.commit()
        await invalidate_slots_cache()


@router.get("/slots", response_model=List[SlotOut])
async def get_parking_slots(db: AsyncSession = Depends(get_db)):
    await sync_expired_reservations(db)

    cached = await get_cached_slots()
    if cached is not None:
        return cached

    slots = (
        await db.scalars(
            select(ParkingSlot).order_by(ParkingSlot.floor, ParkingSlot.zone, ParkingSlot.number)
        )
    ).all()

    result = [
        {
            "id": s.id,
            "number": s.number,
            "zone": s.zone,
            "floor": s.floor,
            "status": s.status,
            "label": f"{s.zone}{s.number}",
        }
        for s in slots
    ]
    await set_cached_slots(result, ttl=30)
    return result


@router.get("/tariffs", response_model=List[TariffOut])
async def list_tariffs(db: AsyncSession = Depends(get_db)):
    return (await db.scalars(select(Tariff).order_by(Tariff.id))).all()


# --- Керування транспортними засобами ---
@router.get("/vehicles", response_model=List[VehicleOut])
async def get_my_vehicles(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    return (
        await db.scalars(select(Vehicle).where(Vehicle.user_id == current_user.id))
    ).all()


@router.post("/vehicles", response_model=VehicleOut, status_code=status.HTTP_201_CREATED)
async def add_vehicle(
    data: VehicleCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    exists = await db.scalar(select(Vehicle).where(Vehicle.plate_number == data.plate_number))
    if exists:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Автомобіль із таким номерним знаком уже зареєстрований",
        )

    vehicle = Vehicle(
        user_id=current_user.id,
        plate_number=data.plate_number,
        type=data.type,
        brand=data.brand,
        model=data.model,
    )
    db.add(vehicle)
    await db.commit()
    await db.refresh(vehicle)
    return vehicle


@router.delete("/vehicles/{vehicle_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_vehicle(
    vehicle_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    vehicle = await db.scalar(
        select(Vehicle).where(and_(Vehicle.id == vehicle_id, Vehicle.user_id == current_user.id))
    )
    if not vehicle:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Автомобіль не знайдено")
    await db.delete(vehicle)
    await db.commit()


# --- Абонементи ---
@router.get("/subscriptions", response_model=List[SubscriptionPlanOut])
async def list_available_subscriptions(db: AsyncSession = Depends(get_db)):
    """Повертає всі активні тарифні плани абонементів із таблиці subscription_plans."""
    return (
        await db.scalars(
            select(SubscriptionPlan)
            .where(SubscriptionPlan.is_active.is_(True))
            .order_by(SubscriptionPlan.days.asc())
        )
    ).all()


@router.get("/subscribed", response_model=List[SubscriptionOut])
async def get_my_subscribed_plans(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    """Повертає оформлені абонементи поточного водія."""
    subs = (
        await db.scalars(
            select(Subscription)
            .options(selectinload(Subscription.plan))
            .where(Subscription.user_id == current_user.id)
            .order_by(Subscription.valid_until.desc())
        )
    ).all()

    return [
        SubscriptionOut(
            id=s.id,
            plan_id=s.plan_id,
            plan_name=s.plan.name,
            valid_until=s.valid_until,
        )
        for s in subs
    ]


@router.post("/subscriptions/checkout", response_model=SubscriptionCheckoutResponse)
async def initiate_subscription_payment(
    data: SubscriptionCheckoutRequest,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    plan = await db.scalar(
        select(SubscriptionPlan).where(
            and_(
                SubscriptionPlan.code == data.plan_code,
                SubscriptionPlan.is_active.is_(True),
            )
        )
    )
    if not plan:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Обраний тарифний план абонемента не знайдено",
        )

    invoice_id = f"sub_{current_user.id}_{plan.id}_{uuid.uuid4().hex[:8]}"

    payment = Payment(
        amount=plan.price,
        method="monobank",
        status=PaymentStatus.PENDING,
        invoice_id=invoice_id,
    )
    db.add(payment)
    await db.commit()
    await db.refresh(payment)

    from app.routers.payments import create_monobank_invoice

    mono_url = await create_monobank_invoice(
        amount_uah=plan.price,
        invoice_id=invoice_id,
        destination=f"Оформлення абонемента «{plan.name}»",
    )
    checkout_url = mono_url or f"/api/payments/mock-checkout/{invoice_id}"

    return SubscriptionCheckoutResponse(
        payment_id=payment.id,
        invoice_id=invoice_id,
        plan_name=plan.name,
        amount=plan.price,
        status="pending",
        checkout_url=checkout_url,
    )


async def activate_subscription_after_payment(
    db: AsyncSession, user_id: int, plan_id: int
) -> Subscription:
    """Внутрішня функція: викликається після підтвердження оплати від платіжного шлюзу."""
    plan = await db.get(SubscriptionPlan, plan_id)
    if not plan:
        raise ValueError(f"План підписки з ID {plan_id} не знайдено")

    sub = Subscription(
        user_id=user_id,
        plan_id=plan.id,
        valid_until=datetime.now(timezone.utc) + timedelta(days=plan.days),
    )
    db.add(sub)
    await db.commit()
    await db.refresh(sub)
    return sub


# --- Бронювання місць ---
@router.post("/reservations", response_model=ReservationOut, status_code=status.HTTP_201_CREATED)
async def create_reservation(
    data: ReservationCreate,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await sync_expired_reservations(db)
    now = datetime.now(timezone.utc)

    vehicle = await db.scalar(
        select(Vehicle).where(
            and_(Vehicle.id == data.vehicle_id, Vehicle.user_id == current_user.id)
        )
    )
    if not vehicle:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="Автомобіль не знайдено або він не належить вашому обліковому запису",
        )

    slot = await db.get(ParkingSlot, data.parking_slot_id)
    if not slot:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Паркувальне місце не знайдено")
    if slot.status != SlotStatus.AVAILABLE:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="Обране місце наразі недоступне для бронювання",
        )

    if data.tariff_id is not None:
        tariff = await db.get(Tariff, data.tariff_id)
        if not tariff:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Обраний тариф не знайдено")
    else:
        tariff = await get_default_tariff(db)

    start = data.start_time if data.start_time.tzinfo else data.start_time.replace(tzinfo=timezone.utc)
    end = start + timedelta(hours=data.duration_hours)
    if end <= now:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Час завершення бронювання не може бути в минулому",
        )

    reservation = Reservation(
        user_id=current_user.id,
        vehicle_id=vehicle.id,
        parking_slot_id=slot.id,
        tariff_id=tariff.id,
        start_time=start,
        end_time=end,
        status="active",
    )
    slot.status = SlotStatus.RESERVED

    db.add(reservation)
    await db.commit()
    await db.refresh(reservation)
    await invalidate_slots_cache()

    return ReservationOut(
        id=reservation.id,
        vehicle_id=vehicle.id,
        plate_number=vehicle.plate_number,
        parking_slot_id=slot.id,
        slot_label=f"{slot.zone}{slot.number}",
        tariff_id=tariff.id,
        tariff_name=tariff.name,
        start_time=reservation.start_time,
        end_time=reservation.end_time,
        status=reservation.status,
        qr_code_data=f"PARK-RES-{reservation.id}-USER-{current_user.id}-SLOT-{slot.zone}{slot.number}",
    )


@router.get("/reservations", response_model=List[ReservationOut])
async def get_my_reservations(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    await sync_expired_reservations(db)
    reservations = (
        await db.scalars(
            select(Reservation)
            .options(
                selectinload(Reservation.vehicle),
                selectinload(Reservation.parking_slot),
                selectinload(Reservation.tariff),
            )
            .where(Reservation.user_id == current_user.id)
            .order_by(Reservation.start_time.asc())
        )
    ).all()

    return [
        ReservationOut(
            id=r.id,
            vehicle_id=r.vehicle_id,
            plate_number=r.vehicle.plate_number,
            parking_slot_id=r.parking_slot_id,
            slot_label=f"{r.parking_slot.zone}{r.parking_slot.number}",
            tariff_id=r.tariff_id,
            tariff_name=r.tariff.name if r.tariff else "Стандартний",
            start_time=r.start_time,
            end_time=r.end_time,
            status=r.status,
            qr_code_data=f"PARK-RES-{r.id}-USER-{current_user.id}-SLOT-{r.parking_slot.zone}{r.parking_slot.number}",
        )
        for r in reservations
    ]


@router.delete("/reservations/{reservation_id}", status_code=status.HTTP_200_OK)
async def cancel_reservation(
    reservation_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    res = await db.scalar(
        select(Reservation).where(
            and_(Reservation.id == reservation_id, Reservation.user_id == current_user.id)
        )
    )
    if not res or res.status != "active":
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Активне бронювання не знайдено")

    res.status = "cancelled"
    slot = await db.get(ParkingSlot, res.parking_slot_id)
    if slot and slot.status == SlotStatus.RESERVED:
        slot.status = SlotStatus.AVAILABLE

    await db.commit()
    await invalidate_slots_cache()
    return {"message": "Бронювання скасовано"}


# --- Паркувальні сесії та історія ---
@router.get("/sessions", response_model=List[SessionOut])
async def get_my_sessions(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    user_vehicle_ids = (
        await db.scalars(select(Vehicle.id).where(Vehicle.user_id == current_user.id))
    ).all()
    if not user_vehicle_ids:
        return []

    sessions = (
        await db.scalars(
            select(ParkingSession)
            .options(
                selectinload(ParkingSession.vehicle),
                selectinload(ParkingSession.parking_slot),
                selectinload(ParkingSession.tariff),
                selectinload(ParkingSession.payment),
            )
            .where(ParkingSession.vehicle_id.in_(user_vehicle_ids))
            .order_by(ParkingSession.entry_time.desc())
        )
    ).all()

    now = datetime.now(timezone.utc)
    result = []
    for s in sessions:
        end_ref = s.exit_time or now
        duration_sec = max((end_ref - s.entry_time).total_seconds(), 60)
        duration_min = int(duration_sec // 60)
        billable_hours = max(1, round(duration_sec / 3600)) # TODO: Maybe fix. Before was math.ceil, not round
        amount = (
            s.payment.amount
            if (s.payment and s.payment.status == PaymentStatus.COMPLETED)
            else round(billable_hours * s.tariff.price_per_hour, 2)
        )
        is_paid = bool(s.payment and s.payment.status == PaymentStatus.COMPLETED)

        result.append(
            SessionOut(
                id=s.id,
                plate_number=s.vehicle.plate_number,
                slot_label=f"{s.parking_slot.zone}{s.parking_slot.number}",
                tariff_name=s.tariff.name,
                entry_time=s.entry_time,
                exit_time=s.exit_time,
                duration_minutes=duration_min,
                calculated_amount=amount,
                is_paid=is_paid,
            )
        )
    return result