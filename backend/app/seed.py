import asyncio
from datetime import datetime, timedelta, timezone

import bcrypt
from sqlalchemy import select

from app.database import AsyncSessionLocal, Base, engine
from app.models import (
    ParkingLot,
    ParkingSession,
    ParkingSlot,
    Payment,
    PaymentStatus,
    SlotStatus,
    Subscription,
    Tariff,
    User,
    UserRole,
    Vehicle,
)


def hash_password(plain_password: str) -> str:
    salt = bcrypt.gensalt()
    return bcrypt.hashpw(plain_password.encode("utf-8"), salt).decode("utf-8")


async def seed_database() -> None:
    # Створюємо таблиці, якщо вони ще не існують
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async with AsyncSessionLocal() as session:
        existing_user = await session.scalar(select(User).limit(1))
        if existing_user:
            print("База даних уже містить початкові дані. Пропуск ініціалізації.")
            return

        # 1. Користувачі (Адміністратор та Водій)
        admin = User(
            name="Олександр",
            surname="Коваленко",
            middlename="Ігорович",
            email="admin@parking.ua",
            phone="+380671112233",
            password_hash=hash_password("Admin1234!"),
            role=UserRole.ADMIN,
        )
        driver = User(
            name="Дмитро",
            surname="Ісай",
            middlename="Олександрович",
            email="driver@parking.ua",
            phone="+380934445566",
            password_hash=hash_password("Driver1234!"),
            role=UserRole.DRIVER,
        )
        session.add_all([admin, driver])
        await session.flush()

        # 2. Транспортні засоби водія
        car_1 = Vehicle(
            user_id=driver.id,
            plate_number="AA4589KB",
            type="sedan",
            brand="Volkswagen",
            model="Passat B8",
        )
        car_2 = Vehicle(
            user_id=driver.id,
            plate_number="KA9012EI",
            type="electric",
            brand="Hyundai",
            model="Ioniq 5",
        )
        session.add_all([car_1, car_2])
        await session.flush()

        # 3. Абонемент для другого авто (для тестування безкоштовного виїзду за абонементом)
        now = datetime.now(timezone.utc)
        subscription = Subscription(
            user_id=driver.id,
            type="Місячний безліміт",
            valid_until=now + timedelta(days=30),
        )
        session.add(subscription)

        # 4. Паркінг та 12 місць (зони A, B, C по 4 місця, як на прототипі)
        parking_lot = ParkingLot(
            address="м. Київ, вул. Політехнічна, 14",
            zones="A,B,C",
            floors=2,
        )
        session.add(parking_lot)
        await session.flush()

        slots = []
        zones_config = [("A", 1), ("B", 1), ("C", 2)]
        for zone_name, floor_num in zones_config:
            for num in range(1, 5):
                status = SlotStatus.AVAILABLE
                # Зробимо місце B2 на техобслуговуванні для наочності на карті
                if zone_name == "B" and num == 2:
                    status = SlotStatus.OUT_OF_SERVICE
                slots.append(
                    ParkingSlot(
                        parking_lot_id=parking_lot.id,
                        number=num,
                        status=status,
                        zone=zone_name,
                        floor=floor_num,
                    )
                )
        session.add_all(slots)
        await session.flush()

        # 5. Тарифи
        standard_tariff = Tariff(name="Стандартний денний", price_per_hour=40.0)
        night_tariff = Tariff(name="Нічний пільговий", price_per_hour=20.0)
        ev_tariff = Tariff(name="Електромобіль (із зарядкою)", price_per_hour=55.0)
        session.add_all([standard_tariff, night_tariff, ev_tariff])
        await session.flush()

        # 6. Завершена тестова сесія та оплата (для перевірки історії та звітів адміна)
        demo_payment = Payment(
            amount=80.0,
            method="monobank",
            status=PaymentStatus.COMPLETED,
            invoice_id="mono_test_inv_0001",
        )
        session.add(demo_payment)
        await session.flush()

        demo_session = ParkingSession(
            vehicle_id=car_1.id,
            parking_slot_id=slots[0].id,  # Місце A1
            tariff_id=standard_tariff.id,
            payment_id=demo_payment.id,
            entry_time=now - timedelta(hours=3),
            exit_time=now - timedelta(hours=1),
        )
        session.add(demo_session)

        await session.commit()
        print("Базу даних успішно заповнено тестовими даними!")


if __name__ == "__main__":
    asyncio.run(seed_database())