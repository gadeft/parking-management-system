from datetime import datetime
from typing import Optional
from pydantic import BaseModel, EmailStr, Field, field_validator


# --- Авторизація та користувачі ---
class UserRegister(BaseModel):
    name: str = Field(..., min_length=2, max_length=100)
    surname: str = Field(..., min_length=2, max_length=100)
    middlename: Optional[str] = Field(None, max_length=100)
    email: EmailStr
    phone: str = Field(..., min_length=10, max_length=20)
    password: str = Field(..., min_length=6, max_length=100)


class UserLogin(BaseModel):
    login: str = Field(..., description="Email або номер телефону")  # Як на прототипі рис. 2.11
    password: str


class TokenResponse(BaseModel):
    access_token: str
    refresh_token: str
    token_type: str = "bearer"
    role: int
    user_id: int
    full_name: str


class RefreshTokenRequest(BaseModel):
    refresh_token: str


class UserOut(BaseModel):
    id: int
    name: str
    surname: str
    middlename: Optional[str]
    email: str
    phone: str
    role: int

    class Config:
        from_attributes = True


# --- Транспортні засоби ---
class VehicleCreate(BaseModel):
    plate_number: str = Field(..., min_length=4, max_length=15)
    type: str = Field(..., max_length=50)
    brand: str = Field(..., max_length=100)
    model: str = Field(..., max_length=100)

    @field_validator("plate_number")
    @classmethod
    def normalize_plate(cls, v: str) -> str:
        return v.strip().upper().replace(" ", "")


class VehicleOut(BaseModel):
    id: int
    plate_number: str
    type: str
    brand: str
    model: str

    class Config:
        from_attributes = True


# --- Абонементи ---
class SubscriptionCheckoutRequest(BaseModel):
    plan_code: str = Field(..., description="Код плану з таблиці subscription_plans")


class SubscriptionCheckoutResponse(BaseModel):
    payment_id: int
    invoice_id: str
    plan_name: str
    amount: float
    status: str
    checkout_url: str


class SubscriptionPlanOut(BaseModel):
    id: int
    code: str
    name: str
    days: int
    price: float

    class Config:
        from_attributes = True


class SubscriptionOut(BaseModel):
    id: int
    plan_id: int
    plan_name: str
    valid_until: datetime


# --- Паркувальні місця та тарифи ---
class SlotOut(BaseModel):
    id: int
    number: int
    zone: str
    floor: int
    status: int
    label: str  # Наприклад: "A1", "B3"


class SlotStatusUpdate(BaseModel):
    status: int = Field(..., ge=0, le=3)


class TariffCreate(BaseModel):
    name: str = Field(..., min_length=2, max_length=100)
    price_per_hour: float = Field(..., gt=0)


class TariffUpdate(BaseModel):
    name: Optional[str] = Field(None, min_length=2, max_length=100)
    price_per_hour: Optional[float] = Field(None, gt=0)


class TariffOut(BaseModel):
    id: int
    name: str
    price_per_hour: float

    class Config:
        from_attributes = True


# --- Бронювання ---
class ReservationCreate(BaseModel):
    vehicle_id: int = Field(..., description="ID автомобіля водія, для якого створюється бронювання")
    parking_slot_id: int
    tariff_id: Optional[int] = Field(None, description="ID обраного тарифу (якщо не вказано — стандартний)")
    start_time: datetime
    duration_hours: int = Field(..., ge=1, le=24)


class ReservationOut(BaseModel):
    id: int
    vehicle_id: int
    plate_number: str
    parking_slot_id: int
    slot_label: str
    tariff_id: Optional[int]
    tariff_name: str
    start_time: datetime
    end_time: datetime
    status: str
    qr_code_data: str


# --- КПП (В'їзд / Виїзд) та Сесії ---
class GateScanRequest(BaseModel):
    plate_number: str = Field(..., min_length=4, max_length=15)

    @field_validator("plate_number")
    @classmethod
    def normalize_plate(cls, v: str) -> str:
        return v.strip().upper().replace(" ", "")


class GateQrRequest(BaseModel):
    qr_code_data: str = Field(..., description="Рядок із QR-коду бронювання, наприклад PARK-RES-1-USER-2-SLOT-A1")
    vehicle_id: Optional[int] = Field(None, description="ID авто водія (якщо у нього кілька авто)")


class SessionOut(BaseModel):
    id: int
    plate_number: str
    slot_label: str
    tariff_name: str
    entry_time: datetime
    exit_time: Optional[datetime]
    duration_minutes: int
    calculated_amount: float
    is_paid: bool


# --- Онлайн-оплата ---
class CheckoutResponse(BaseModel):
    payment_id: int
    invoice_id: str
    amount: float
    status: str
    checkout_url: str


class MockPaymentConfirmRequest(BaseModel):
    method: str = Field(
        default="google_pay",
        description="Спосіб оплати: google_pay | apple_pay | card",
    )
    simulate_failure: bool = Field(
        default=False,
        description="Якщо True — імітує відхилення платежу банком (для тест-кейсу помилки оплати)",
    )


class PaymentStatusOut(BaseModel):
    id: int
    invoice_id: Optional[str]
    amount: float
    method: str
    status: int
    created_at: datetime

    class Config:
        from_attributes = True