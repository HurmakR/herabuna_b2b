# b2b/payment_views.py
"""
Payment views for B2B IBAN orders.
"""
import json
import logging

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from b2b.models import Order
from b2b.services.payment import (
    get_bank_name, get_edrpou, get_iban, get_recipient,
    mono_find_payment, mono_verify_webhook,
    monobank_deeplink, nbu_qr_string, payment_purpose, privat24_deeplink,
)

logger = logging.getLogger(__name__)


def _order_for_payment(request, order_id: int) -> Order:
    """Get order that belongs to current dealer (or staff)."""
    qs = Order.objects.select_related("dealer")
    if request.user.is_staff:
        return get_object_or_404(qs, id=order_id)
    return get_object_or_404(qs, id=order_id, dealer=request.user)


@login_required
def payment_page(request, order_id: int):
    """Payment instruction page for a specific order."""
    order = _order_for_payment(request, order_id)

    if order.status not in ("pending_payment", "submitted"):
        messages.warning(request, "Замовлення не потребує оплати.")
        return redirect("b2b:order_detail", order_id=order.id)

    context = {
        "order":        order,
        "iban":         get_iban(),
        "recipient":    get_recipient(),
        "edrpou":       get_edrpou(),
        "bank_name":    get_bank_name(),
        "purpose":      payment_purpose(order),
        "qr_string":    nbu_qr_string(order),
        "mono_link":    monobank_deeplink(order),
        "privat_link":  privat24_deeplink(order),
    }
    return render(request, "b2b/payment_page.html", context)


@login_required
@require_POST
def payment_proof_upload(request, order_id: int):
    """Client uploads payment screenshot/receipt."""
    order = _order_for_payment(request, order_id)

    if order.status not in ("pending_payment", "submitted"):
        messages.error(request, "Неможливо підтвердити оплату для цього замовлення.")
        return redirect("b2b:order_detail", order_id=order.id)

    note = (request.POST.get("payment_note") or "").strip()
    p = order.external_payload or {}
    p["payment_proof_note"] = note
    p["payment_proof_at"] = str(__import__("django.utils.timezone", fromlist=["now"]).now())
    order.external_payload = p

    # Mark as awaiting admin confirmation
    if "payment_pending_confirm" not in p:
        p["payment_pending_confirm"] = True

    order.save(update_fields=["external_payload"])

    # Notify admin via Telegram
    try:
        from b2b.services.telegram import notify_admins
        notify_admins(
            f"💳 Клієнт <b>{order.dealer.username}</b> підтвердив оплату\n"
            f"Замовлення <b>#{order.id}</b> на суму <b>{order.total} ₴</b>\n"
            f"Коментар: {note or '—'}"
        )
    except Exception:
        pass

    messages.success(request, "Дякуємо! Ми перевіримо оплату і підтвердимо замовлення.")
    return redirect("b2b:order_detail", order_id=order.id)


@login_required
def payment_check(request, order_id: int):
    """AJAX: check if payment arrived via Monobank API."""
    order = _order_for_payment(request, order_id)

    if order.status not in ("pending_payment", "submitted"):
        return JsonResponse({"paid": False, "reason": "wrong_status"})

    item = mono_find_payment(order)
    if not item:
        return JsonResponse({"paid": False, "reason": "not_found"})

    # Auto-confirm
    try:
        from b2b.services.marketplace_orders import apply_stock_action
        from django.utils import timezone
        p = order.external_payload or {}
        p["mono_payment"] = {
            "id":     item.get("id"),
            "amount": item.get("amount"),
            "time":   item.get("time"),
            "desc":   item.get("description"),
        }
        order.external_payload = p
        order.save(update_fields=["external_payload"])
    except Exception as e:
        logger.exception("payment_check save error: %s", e)

    return JsonResponse({"paid": True, "amount": item.get("amount", 0) / 100})


@csrf_exempt
def mono_webhook(request):
    """Monobank webhook endpoint — auto-confirm payment."""
    if request.method != "POST":
        return HttpResponse(status=405)

    body = request.body
    x_sign = request.headers.get("X-Sign", "")

    if not mono_verify_webhook(body, x_sign):
        logger.warning("Mono webhook: invalid signature")
        return HttpResponse(status=403)

    try:
        data = json.loads(body)
        stmt = data.get("data", {}).get("statementItem", {})
        amount_kopecks = stmt.get("amount", 0)
        description = (stmt.get("description") or "") + (stmt.get("comment") or "")

        # Find matching order by amount + ID in description
        import re
        m = re.search(r"№(\d+)", description)
        if m:
            order_id = int(m.group(1))
            order = Order.objects.filter(
                id=order_id,
                status__in=["pending_payment", "submitted"]
            ).first()
            if order and int(order.total * 100) == amount_kopecks:
                p = order.external_payload or {}
                p["mono_payment"] = {"id": stmt.get("id"), "amount": amount_kopecks,
                                     "auto_confirmed": True}
                order.external_payload = p
                order.save(update_fields=["external_payload"])

                from b2b.services.telegram import notify_admins
                notify_admins(
                    f"✅ Автооплата підтверджена!\n"
                    f"Замовлення <b>#{order.id}</b> · {order.dealer.username}\n"
                    f"Сума: <b>{order.total} ₴</b>"
                )
    except Exception as e:
        logger.exception("mono_webhook error: %s", e)

    return HttpResponse(status=200)


@login_required
def payment_status_badge(request, order_id: int):
    """AJAX: return payment status badge HTML."""
    order = _order_for_payment(request, order_id)
    p = order.external_payload or {}

    if p.get("mono_payment"):
        html = '<span class="badge bg-success">✓ Оплачено (Mono)</span>'
    elif p.get("payment_pending_confirm"):
        html = '<span class="badge bg-warning text-dark">⏳ Очікує підтвердження</span>'
    else:
        html = '<span class="badge bg-secondary">Не оплачено</span>'

    return HttpResponse(html)
