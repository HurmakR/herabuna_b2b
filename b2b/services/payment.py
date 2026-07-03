# b2b/services/payment.py
"""
IBAN payment helpers for B2B orders.

Generates:
  - Payment purpose string (призначення платежу)
  - NBU QR-code string (UA standard, readable by all Ukrainian banking apps)
  - Monobank deep-link
  - Privat24 deep-link

Monobank API:
  - Webhook for incoming payments (auto-confirm)
  - Statement polling fallback
"""
import hashlib
import hmac
import json
import re
from decimal import Decimal

import requests
from django.conf import settings
from django.utils import timezone


# ── Helpers ───────────────────────────────────────────────────────────────────

def _get(attr: str, default: str = "") -> str:
    return str(getattr(settings, attr, None) or default).strip()


def get_iban()       -> str: return _get("PAYMENT_IBAN")
def get_recipient()  -> str: return _get("PAYMENT_RECIPIENT_NAME")
def get_edrpou()     -> str: return _get("PAYMENT_EDRPOU")
def get_bank_name()  -> str: return _get("PAYMENT_BANK_NAME")
def get_mono_token() -> str: return _get("MONOBANK_API_TOKEN")


def payment_purpose(order) -> str:
    """Призначення платежу для банківського переказу."""
    from django.utils.formats import date_format
    date_str = date_format(order.created_at, "d.m.Y")
    return f"Оплата за замовлення №{order.id} від {date_str} р. Без ПДВ."


def amount_kopecks(order) -> int:
    """Сума в копійках (Mono API використовує копійки)."""
    return int(Decimal(str(order.total)) * 100)


# ── NBU QR-code (UA standard) ─────────────────────────────────────────────────

def nbu_qr_string(order) -> str:
    """
    Generates a UA-standard payment QR string.
    Supported by: Monobank, Privat24, PUMB, Oschadbank, and all NBU-compliant apps.

    Format: BCD\n002\n2\nSCT\n{BIC}\n{Name}\n{IBAN}\nUAH{amount}\n\n\n{purpose}
    Ukrainian banks also accept a simplified format used by ibanoplata.
    """
    iban   = get_iban().replace(" ", "")
    name   = get_recipient()
    amount = f"{order.total:.2f}"
    purpose = payment_purpose(order)

    # Simplified UA format (works in Mono, Privat, most UA apps)
    lines = [
        "BCD",
        "002",
        "1",
        "SCT",
        "",           # BIC — optional for UA domestic
        name,
        iban,
        f"UAH{amount}",
        "",
        "",
        purpose,
    ]
    return "\n".join(lines)


# ── Deep links ────────────────────────────────────────────────────────────────

def monobank_deeplink(order) -> str:
    """
    Monobank deep-link that pre-fills transfer details.
    Opens Monobank app directly on Android/iOS.
    """
    from urllib.parse import quote
    iban    = get_iban().replace(" ", "")
    amount  = f"{order.total:.2f}"
    purpose = quote(payment_purpose(order))
    return f"https://send.monobank.ua/pay?iban={iban}&amount={amount}&comment={purpose}"


def privat24_deeplink(order) -> str:
    """Privat24 deep-link for pre-filled transfer."""
    from urllib.parse import urlencode
    params = urlencode({
        "iban":    get_iban().replace(" ", ""),
        "amount":  f"{order.total:.2f}",
        "comment": payment_purpose(order),
    })
    return f"https://next.privat24.ua/payments/transfer/iban?{params}"


# ── Monobank API ──────────────────────────────────────────────────────────────

MONO_API = "https://api.monobank.ua"


def _mono_headers() -> dict:
    return {"X-Token": get_mono_token()}


def mono_set_webhook(webhook_url: str) -> bool:
    """Register webhook URL with Monobank. Call once during setup."""
    r = requests.post(
        f"{MONO_API}/personal/webhook",
        headers=_mono_headers(),
        json={"webHookUrl": webhook_url},
        timeout=15,
    )
    return r.ok


def mono_verify_webhook(request_body: bytes, x_sign: str) -> bool:
    """Verify Monobank webhook signature."""
    secret = get_mono_token().encode()
    sig = hmac.new(secret, request_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(sig, x_sign or "")


def mono_find_payment(order) -> dict | None:
    """
    Poll Monobank statement for a matching payment.
    Looks back 7 days, matches by amount + order ID in description.
    Returns statement item dict or None.
    """
    import time
    token = get_mono_token()
    if not token:
        return None

    # Get account list first
    try:
        r = requests.get(f"{MONO_API}/personal/client-info",
                         headers=_mono_headers(), timeout=15)
        if not r.ok:
            return None
        accounts = r.json().get("accounts", [])
        # Find UAH account matching PAYMENT_IBAN
        iban = get_iban().replace(" ", "")
        account_id = None
        for acc in accounts:
            if acc.get("iban", "").replace(" ", "") == iban:
                account_id = acc["id"]
                break
        if not account_id and accounts:
            account_id = accounts[0]["id"]
        if not account_id:
            return None
    except Exception:
        return None

    # Statement for last 7 days
    now = int(time.time())
    from_ts = now - 7 * 24 * 3600
    try:
        r = requests.get(
            f"{MONO_API}/personal/statement/{account_id}/{from_ts}/{now}",
            headers=_mono_headers(), timeout=15,
        )
        if not r.ok:
            return None
        items = r.json()
    except Exception:
        return None

    target_amount = amount_kopecks(order)
    order_id_str = str(order.id)

    for item in items:
        if item.get("amount") != target_amount:
            continue
        desc = (item.get("description") or "") + (item.get("comment") or "")
        if order_id_str in desc:
            return item

    return None
