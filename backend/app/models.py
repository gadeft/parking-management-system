from datetime import datetime
from enum import IntEnum
from typing import List, Optional

from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


class SlotStatus(IntEnum):
    AVAILABLE = 0       # Вільне
    RESERVED = 1        # Заброньоване
    OCCUPIED = 2        # Зайняте
    OUT_OF_SERVICE = 3  # Технічне обслуговування / недоступне


class PaymentStatus(IntEnum):
    PENDING = 0         # Очікує оплати
    COMPLETED = 1       # Оплачено успішно
    FAILED = 2          # Помилка оплати


class UserRole(IntEnum):
    DRIVER = 0          # Водій
    ADMIN = 1           # Адміністратор


class User(Base):
    __tablename__ = "user"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    surname: Mapped[str] = mapped_column(String(100), nullable=False)
    middlename: Mapped[Optional[str]] = mapped_column(String(100), nullable=True)
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True, nullable=False)
    phone: Mapped[str] = mapped_column(String(20), unique=True, index=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    role: Mapped[int] = mapped_column(
        Integer, default=UserRole.DRIVER, nullable=False
    )

    vehicles: Mapped[List["Vehicle"]] = relationship(
        back_populates="owner", cascade="all, delete-orphan"
    )
    subscriptions: Mapped[List["Subscription"]] = relationship(
        back_populates="owner", cascade="all, delete-orphan"
    )
    reservations: Mapped[List["Reservation"]] = relationship(
        back_populates="owner", cascade="all, delete-orphan"
    )


class Vehicle(Base):
    __tablename__ = "vehicle"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), nullable=False)
    plate_number: Mapped[str] = mapped_column(String(20), unique=True, index=True, nullable=False)
    type: Mapped[str] = mapped_column(String(50), nullable=False)   # sedan, suv, electric тощо
    brand: Mapped[str] = mapped_column(String(100), nullable=False)
    model: Mapped[str] = mapped_column(String(100), nullable=False)

    owner: Mapped["User"] = relationship(back_populates="vehicles")
    sessions: Mapped[List["ParkingSession"]] = relationship(back_populates="vehicle")


class Subscription(Base):
    __tablename__ = "subscription"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), nullable=False)
    type: Mapped[str] = mapped_column(String(50), nullable=False)   # monthly, quarterly, vip
    valid_until: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    owner: Mapped["User"] = relationship(back_populates="subscriptions")


class ParkingLot(Base):
    __tablename__ = "parking_lot"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    address: Mapped[str] = mapped_column(String(255), nullable=False)
    zones: Mapped[str] = mapped_column(String(100), nullable=False)  # Наприклад: "A,B,C"
    floors: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    slots: Mapped[List["ParkingSlot"]] = relationship(
        back_populates="parking_lot", cascade="all, delete-orphan"
    )


class ParkingSlot(Base):
    __tablename__ = "parking_slot"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    parking_lot_id: Mapped[int] = mapped_column(
        ForeignKey("parking_lot.id", ondelete="CASCADE"), nullable=False
    )
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[int] = mapped_column(
        Integer, default=SlotStatus.AVAILABLE, nullable=False
    )
    zone: Mapped[str] = mapped_column(String(20), nullable=False)
    floor: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    parking_lot: Mapped["ParkingLot"] = relationship(back_populates="slots")
    reservations: Mapped[List["Reservation"]] = relationship(back_populates="parking_slot")
    sessions: Mapped[List["ParkingSession"]] = relationship(back_populates="parking_slot")


class Tariff(Base):
    __tablename__ = "tariff"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    price_per_hour: Mapped[float] = mapped_column(Float, nullable=False)

    sessions: Mapped[List["ParkingSession"]] = relationship(back_populates="tariff")


class Reservation(Base):
    __tablename__ = "reservation"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), nullable=False)
    parking_slot_id: Mapped[int] = mapped_column(
        ForeignKey("parking_slot.id", ondelete="CASCADE"), nullable=False
    )
    start_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    end_time: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(20), default="active", nullable=False)  # active | completed | cancelled | expired

    owner: Mapped["User"] = relationship(back_populates="reservations")
    parking_slot: Mapped["ParkingSlot"] = relationship(back_populates="reservations")


class Payment(Base):
    __tablename__ = "payment"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    amount: Mapped[float] = mapped_column(Float, nullable=False)
    method: Mapped[str] = mapped_column(String(50), nullable=False)  # liqpay, monobank, subscription
    status: Mapped[int] = mapped_column(
        Integer, default=PaymentStatus.PENDING, nullable=False
    )
    invoice_id: Mapped[Optional[str]] = mapped_column(String(120), unique=True, nullable=True) # TODO: ???
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    session: Mapped[Optional["ParkingSession"]] = relationship(back_populates="payment")


class ParkingSession(Base):
    __tablename__ = "parking_session"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    vehicle_id: Mapped[int] = mapped_column(ForeignKey("vehicle.id"), nullable=False)
    parking_slot_id: Mapped[int] = mapped_column(ForeignKey("parking_slot.id"), nullable=False)
    tariff_id: Mapped[int] = mapped_column(ForeignKey("tariff.id"), nullable=False)
    payment_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("payment.id"), nullable=True
    )
    entry_time: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    exit_time: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    vehicle: Mapped["Vehicle"] = relationship(back_populates="sessions")
    parking_slot: Mapped["ParkingSlot"] = relationship(back_populates="sessions")
    tariff: Mapped["Tariff"] = relationship(back_populates="sessions")
    payment: Mapped[Optional["Payment"]] = relationship(back_populates="session")