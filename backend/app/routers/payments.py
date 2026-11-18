import base64
import hashlib
import hmac
import json
import math
import os
import uuid
from datetime import datetime, timezone
from typing import Optional

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.core.redis_client import log_device_event
from app.core.security import get_current_user
from app.database import get_db
from app.models import ParkingSession, Payment, PaymentStatus, User
from app.routers.parking import activate_subscription_after_payment
from app.schemas import CheckoutResponse, MockPaymentConfirmRequest, PaymentStatusOut

router = APIRouter(prefix="/api/payments", tags=["Online Payments & Webhooks"])

MONOBANK_TOKEN = os.getenv("MONOBANK_TOKEN", "").strip()
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "parking_webhook_hmac_secret_2026")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "http://localhost:8080").rstrip("/")

_MONOBANK_PUBKEY_PEM: Optional[bytes] = None


# TODO: Delete
# class InternalWebhookPayload(BaseModel):
#     invoiceId: str
#     status: str  # success | failure
#     method: Optional[str] = "card"


def generate_internal_signature(invoice_id: str, payment_status: str) -> str:
    message = f"{invoice_id}:{payment_status}".encode("utf-8")
    return hmac.new(WEBHOOK_SECRET.encode("utf-8"), message, hashlib.sha256).hexdigest()


async def verify_monobank_signature(raw_body: bytes, x_sign_b64: str) -> bool:
    """Перевіряє ECDSA SHA-256 підпис Monobank за публічним ключем еквайрингу."""
    global _MONOBANK_PUBKEY_PEM
    if not MONOBANK_TOKEN:
        return False

    try:
        if _MONOBANK_PUBKEY_PEM is None:
            async with httpx.AsyncClient(timeout=10.0) as client:
                resp = await client.get(
                    "https://api.monobank.ua/api/merchant/pubkey",
                    headers={"X-Token": MONOBANK_TOKEN},
                )
                if resp.status_code != 200:
                    return False
                key_b64 = resp.json().get("key")
                if not key_b64:
                    return False
                _MONOBANK_PUBKEY_PEM = base64.b64decode(key_b64)

        public_key = serialization.load_pem_public_key(_MONOBANK_PUBKEY_PEM)
        signature_bytes = base64.b64decode(x_sign_b64)
        public_key.verify(signature_bytes, raw_body, ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, Exception):
        _MONOBANK_PUBKEY_PEM = None  # Скидаємо кеш ключа у разі ротації ключів банком
        return False


async def create_monobank_invoice(amount_uah: float, invoice_id: str, destination: str) -> Optional[str]:
    """Створює рахунок у Monobank Acquiring (Google Pay, Apple Pay, картка)."""
    if not MONOBANK_TOKEN:
        return None

    payload = {
        "amount": int(round(amount_uah * 100)),
        "ccy": 980,
        "merchantPaymInfo": {
            "reference": invoice_id,
            "destination": destination,
        },
        "redirectUrl": f"{PUBLIC_BASE_URL}/",
        "webHookUrl": f"{PUBLIC_BASE_URL}/api/payments/webhook",
    }
    headers = {"X-Token": MONOBANK_TOKEN}

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(
                "https://api.monobank.ua/api/merchant/invoice/create",
                json=payload,
                headers=headers,
            )
            if response.status_code == 200:
                return response.json().get("pageUrl")
    except httpx.HTTPError:
        return None
    return None


@router.post("/session/{session_id}/checkout", response_model=CheckoutResponse)
async def initiate_session_payment(
    session_id: int,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    session = await db.scalar(
        select(ParkingSession)
        .options(
            selectinload(ParkingSession.vehicle),
            selectinload(ParkingSession.tariff),
            selectinload(ParkingSession.payment),
        )
        .where(ParkingSession.id == session_id)
    )
    if not session or session.vehicle.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Паркувальну сесію не знайдено")

    if session.payment and session.payment.status == PaymentStatus.COMPLETED:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Ця паркувальна сесія вже оплачена")

    now = datetime.now(timezone.utc)
    end_ref = session.exit_time or now
    duration_sec = max((end_ref - session.entry_time).total_seconds(), 60)
    billable_hours = max(1, round(duration_sec / 3600)) # TODO: was math.ceil
    amount = round(billable_hours * session.tariff.price_per_hour, 2)

    invoice_id = f"ses_{session.id}_{uuid.uuid4().hex[:8]}"

    if session.payment:
        payment = session.payment
        payment.amount = amount
        payment.invoice_id = invoice_id
        payment.status = PaymentStatus.PENDING
    else:
        payment = Payment(
            amount=amount,
            method="monobank",
            status=PaymentStatus.PENDING,
            invoice_id=invoice_id,
        )
        db.add(payment)
        await db.flush()
        session.payment_id = payment.id

    await db.commit()
    await db.refresh(payment)

    mono_url = await create_monobank_invoice(
        amount_uah=amount,
        invoice_id=invoice_id,
        destination=f"Оплата паркування авто {session.vehicle.plate_number} (Сесія #{session.id})",
    )
    checkout_url = mono_url or f"/api/payments/mock-checkout/{invoice_id}"

    return CheckoutResponse(
        payment_id=payment.id,
        invoice_id=invoice_id,
        amount=amount,
        status="pending",
        checkout_url=checkout_url,
    )


@router.post("/webhook", status_code=status.HTTP_200_OK)
async def payment_gateway_webhook(
    request: Request,
    x_sign: Optional[str] = Header(None),
    x_internal_sign: Optional[str] = Header(None),
    db: AsyncSession = Depends(get_db),
):
    raw_body = await request.body()
    body = json.loads(raw_body.decode("utf-8"))

    # Monobank передає наш код у полі reference; локальний симулятор — у полі invoiceId
    our_invoice_id = body.get("reference") or body.get("invoiceId")
    tx_status = body.get("status")
    chosen_method = body.get("paymentMethod") or body.get("method") or "monobank"

    if not our_invoice_id or not tx_status:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Некоректний формат webhook")

    # 1. Верифікація підпису: або реальний X-Sign від Monobank, або локальний HMAC
    if x_sign:
        is_valid = await verify_monobank_signature(raw_body, x_sign)
        if not is_valid:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Недійсний підпис X-Sign Monobank")
    else:
        expected_sign = generate_internal_signature(our_invoice_id, tx_status)
        if not x_internal_sign or not hmac.compare_digest(x_internal_sign, expected_sign):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Недійсний внутрішній підпис")

    # 2. Ігноруємо проміжні статуси Monobank (created, processing, hold), щоб не скасувати платіж завчасно
    if tx_status in {"created", "processing", "hold"}:
        return {"status": "intermediate_ignored", "tx_status": tx_status}

    payment = await db.scalar(select(Payment).where(Payment.invoice_id == our_invoice_id))
    if not payment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Платіж не знайдено")

    if payment.status == PaymentStatus.COMPLETED:
        return {"status": "already_processed", "invoice_id": our_invoice_id}

    if tx_status == "success":
        payment.status = PaymentStatus.COMPLETED
        payment.method = chosen_method
        await db.commit()

        if our_invoice_id.startswith("sub_"):
            parts = our_invoice_id.split("_")
            if len(parts) >= 3 and parts[1].isdigit() and parts[2].isdigit():
                await activate_subscription_after_payment(
                    db, user_id=int(parts[1]), plan_id=int(parts[2])
                )

        await log_device_event(
            "Payment-Gateway",
            "PAYMENT_COMPLETED",
            f"Підтверджено оплату {payment.amount} грн (метод: {chosen_method}, рахунок: {our_invoice_id})",
        )
        return {"status": "ok", "payment_status": "completed"}

    # Остаточна невдача (failure, expired, reversed)
    payment.status = PaymentStatus.FAILED
    payment.method = chosen_method
    await db.commit()
    await log_device_event(
        "Payment-Gateway",
        "PAYMENT_FAILED",
        f"Відхилено оплату за рахунком {our_invoice_id} (статус: {tx_status})",
        is_error=True,
    )
    return {"status": "ok", "payment_status": "failed"}


@router.post("/mock-confirm/{invoice_id}")
async def confirm_mock_payment(
    invoice_id: str,
    data: MockPaymentConfirmRequest,
    db: AsyncSession = Depends(get_db),
):
    """Симулює відповідь еквайрингу (Google Pay / Apple Pay / Card) та викликає webhook."""
    allowed_methods = {"google_pay", "apple_pay", "card"}
    if data.method not in allowed_methods:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Непідтримуваний метод оплати. Оберіть google_pay, apple_pay або card.",
        )

    tx_status = "failure" if data.simulate_failure else "success"
    signature = generate_internal_signature(invoice_id, tx_status)

    # Викликаємо локальний webhook через внутрішній HTTP-клієнт із криптографічним підписом
    async with httpx.AsyncClient(base_url="http://127.0.0.1:8000") as client:
        resp = await client.post(
            "/api/payments/webhook",
            json={"invoiceId": invoice_id, "status": tx_status, "method": data.method},
            headers={"X-Internal-Sign": signature},
        )
    if resp.status_code != 200:
        raise HTTPException(status_code=resp.status_code, detail=resp.text)

    return {
        "invoice_id": invoice_id,
        "method": data.method,
        "result": tx_status,
        "message": "Оплату успішно проведено!" if tx_status == "success" else "Банк відхилив транзакцію.",
    }


@router.get("/mock-checkout/{invoice_id}", response_class=HTMLResponse)
async def render_mock_checkout_page(invoice_id: str, db: AsyncSession = Depends(get_db)):
    """Інтерактивна сторінка оплати (Google Pay / Apple Pay / Card) для тестування в браузері."""
    payment = await db.scalar(select(Payment).where(Payment.invoice_id == invoice_id))
    if not payment:
        return HTMLResponse("<h3>Рахунок не знайдено</h3>", status_code=404)

    html = f"""
    <!DOCTYPE html>
    <html lang="uk">
    <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Оплата паркування — Еквайринг Sandbox</title>
        <style>
            body {{ font-family: system-ui, sans-serif; background: #f3f4f6; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }}
            .card {{ background: white; padding: 2rem; border-radius: 16px; box-shadow: 0 10px 25px rgba(0,0,0,0.08); max-width: 380px; width: 100%; text-align: center; }}
            .amount {{ font-size: 2.2rem; font-weight: 700; color: #111827; margin: 1rem 0; }}
            .inv {{ font-size: 0.85rem; color: #6b7280; margin-bottom: 1.5rem; }}
            button {{ width: 100%; padding: 0.85rem; margin-bottom: 0.75rem; border: none; border-radius: 10px; font-size: 1rem; font-weight: 600; cursor: pointer; }}
            .gpay {{ background: #000; color: #fff; }}
            .apay {{ background: #1f2937; color: #fff; }}
            .card-btn {{ background: #2563eb; color: #fff; }}
            .fail-btn {{ background: #fee2e2; color: #b91c1c; font-size: 0.85rem; margin-top: 0.5rem; }}
            #status {{ margin-top: 1rem; font-weight: 600; }}
        </style>
    </head>
    <body>
        <div class="card">
            <h2>Smart Parking Pay</h2>
            <div class="amount">{payment.amount:.2f} ₴</div>
            <div class="inv">Рахунок: {invoice_id}</div>
            <button class="gpay" onclick="pay('google_pay', false)">Оплатити через Google Pay</button>
            <button class="apay" onclick="pay('apple_pay', false)">Оплатити через Apple Pay</button>
            <button class="card-btn" onclick="pay('card', false)">Оплатити банківською карткою</button>
            <button class="fail-btn" onclick="pay('card', true)">Імітувати помилку оплати</button>
            <div id="status"></div>
        </div>
        <script>
            async function pay(method, simulateFailure) {{
                const statusEl = document.getElementById('status');
                statusEl.textContent = 'Обробка платежу...';
                const res = await fetch('/api/payments/mock-confirm/{invoice_id}', {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ method, simulate_failure: simulateFailure }})
                }});
                const data = await res.json();
                statusEl.style.color = simulateFailure ? '#dc2626' : '#16a34a';
                statusEl.textContent = data.message + ' Можете закрити це вікно або перевірити виїзд на КПП.';
            }}
        </script>
    </body>
    </html>
    """
    return HTMLResponse(content=html)


@router.get("/{payment_id}", response_model=PaymentStatusOut)
async def get_payment_details(
    payment_id: int,
    _: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    payment = await db.get(Payment, payment_id)
    if not payment:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Платіж не знайдено")
    return payment