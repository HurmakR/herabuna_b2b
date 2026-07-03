from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

from django.conf import settings
from django.db import transaction
from django.utils import timezone

from b2b.models import Dealer, Order, OrderItem, Product, ProductVariant
from warehouse.models import InventoryReservation
from warehouse.services import WarehouseError, cancel_order, ensure_order_reserved, ship_order
from b2b.services.marketplace_meta import extract_ttn_from_payload


@dataclass
class SyncResult:
    created: int = 0
    updated: int = 0
    reserved: int = 0
    released: int = 0
    shipped: int = 0
    skipped_unmapped: int = 0
    errors: List[str] = field(default_factory=list)


def get_marketplace_dealer() -> Dealer:
    """Return a dedicated system dealer for marketplace orders."""
    username = (getattr(settings, "MARKETPLACE_DEALER_USERNAME", "marketplace") or "marketplace").strip()

    dealer = Dealer.objects.filter(username=username).first()
    if dealer:
        return dealer

    dealer = Dealer.objects.create(
        username=username,
        email="",
        company_name="Marketplace",
        is_active=True,
        is_dealer=False,
    )
    dealer.set_unusable_password()
    dealer.save(update_fields=["password"])
    return dealer


def _to_decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def order_has_reservations(order: Order) -> bool:
    return InventoryReservation.objects.filter(order_item__order=order).exists()


def rereserve_order(order: Order) -> None:
    """Release existing reservations (if any) and reserve again."""
    cancel_order(order)
    if order.status == "draft":
        order.status = "submitted"
        order.save(update_fields=["status"])
    ensure_order_reserved(order)


def apply_stock_action(*, order: Order, action: str) -> None:
    """Apply warehouse action manually (from UI only).

    Supported actions:
      - accept/reserve : reserve stock, move to pending_payment
      - reject/release/cancel : release stock, cancel order
      - ship : consume lots, mark shipped
    """
    action = (action or "").strip().lower()

    if action in {"accept", "reserve"}:
        if order.status == "draft":
            order.status = "submitted"
            order.save(update_fields=["status"])
        ensure_order_reserved(order)
        if order.status != "pending_payment":
            order.status = "pending_payment"
            order.save(update_fields=["status"])
        return

    if action in {"reject", "release", "cancel"}:
        cancel_order(order)
        if order.status != "cancelled":
            order.status = "cancelled"
            order.save(update_fields=["status"])
        return

    if action == "ship":
        if order.status == "shipped":
            return
        ship_order(order)
        if order.status != "shipped":
            order.status = "shipped"
            order.shipped_at = timezone.now()
            order.save(update_fields=["status", "shipped_at"])
        return

    raise ValueError("Unknown action")


@transaction.atomic
def accept_marketplace_order(order: Order) -> None:
    """Accept a marketplace order into the B2B workflow.

    This is the single entry point for operator action on the service page.
    After acceptance:
      - Stock is reserved
      - Order moves to pending_payment
      - Order is marked as accepted (external_payload._accepted = True)
        so it no longer appears in the "new imports" queue.

    Idempotent: calling twice on an already-accepted order is safe.
    """
    if order.status != "draft":
        # Already accepted or processed — idempotently mark and return
        _mark_accepted(order)
        return

    ensure_order_reserved(order)
    order.status = "pending_payment"
    p = order.external_payload or {}
    p["_accepted"] = True
    order.external_payload = p
    order.save(update_fields=["status", "external_payload"])


@transaction.atomic
def dismiss_marketplace_order(order: Order) -> None:
    """Dismiss a marketplace order without accepting it into stock.

    Use when the order is cancelled on marketplace side or irrelevant.
    Marks as cancelled in B2B and hides from the import queue.
    """
    if order.status != "draft":
        raise ValueError(
            f"Неможливо відхилити замовлення #{order.id}: "
            f"статус '{order.status}' — вже прийняте в роботу. "
            f"Для скасування використовуйте основний список замовлень."
        )
    # Cancelled draft — no stock reservation needed
    order.status = "cancelled"
    p = order.external_payload or {}
    p["_accepted"] = True
    order.external_payload = p
    order.save(update_fields=["status", "external_payload"])


def _mark_accepted(order: Order) -> None:
    p = order.external_payload or {}
    if not p.get("_accepted"):
        p["_accepted"] = True
        order.external_payload = p
        order.save(update_fields=["external_payload"])


@transaction.atomic
def upsert_external_order(
    *,
    channel: str,
    external_id: str,
    external_status: str,
    external_created_at: Optional[datetime],
    note: str,
    payload: dict,
    items: List[Tuple[Optional[Product], Optional[ProductVariant], int, Decimal, str, dict]],
) -> Tuple[Order, bool, bool, List[dict]]:
    """Create or update an internal Order for a marketplace order.

    Returns:
      (order, created, items_changed, unmatched_items)
    """
    channel = (channel or "").strip().lower() or "unknown"
    external_id = str(external_id or "").strip()
    if not external_id:
        raise ValueError("external_id is required")

    dealer = get_marketplace_dealer()

    order, created = Order.objects.get_or_create(
        channel=channel,
        external_id=external_id,
        defaults={
            "dealer": dealer,
            "status": "draft",  # stays draft until operator accepts in sync queue
            "note": note or "",
            "external_status": external_status or "",
            "external_created_at": external_created_at,
            "external_payload": payload or {},
            "created_at": external_created_at or timezone.now(),
        },
    )

    # Freeze: once accepted — skip data update, only track external_status
    if not created and (order.external_payload or {}).get("_accepted"):
        if order.external_status != (external_status or ""):
            order.external_status = external_status or ""
            order.save(update_fields=["external_status"])
        return order, False, False, []

    # Update metadata
    update_fields: List[str] = []
    if order.external_status != (external_status or ""):
        order.external_status = external_status or ""
        update_fields.append("external_status")
    order.external_created_at = external_created_at
    update_fields.append("external_created_at")
    order.external_payload = payload or {}
    update_fields.append("external_payload")
    if note and order.note != note:
        order.note = note
        update_fields.append("note")
    if update_fields:
        order.save(update_fields=sorted(set(update_fields)))


    # Best-effort prefill shipping snapshot for Woo orders (do not overwrite manual edits).
    if channel == 'woo':
        raw = (payload or {}).get('raw') or {}
        billing = raw.get('billing') or (payload or {}).get('billing') or {}
        shipping = raw.get('shipping') or (payload or {}).get('shipping') or {}

        updated_ship_fields: List[str] = []

        if not (order.shipping_city or '').strip():
            city = str(shipping.get('city') or billing.get('city') or '').strip()
            if city:
                order.shipping_city = city
                updated_ship_fields.append('shipping_city')

        if not (order.shipping_warehouse or '').strip():
            wh = str(shipping.get('address_1') or shipping.get('address_2') or '').strip()
            if wh:
                order.shipping_warehouse = wh
                updated_ship_fields.append('shipping_warehouse')

        if not (order.shipping_recipient or '').strip():
            fn = str(shipping.get('first_name') or billing.get('first_name') or '').strip()
            ln = str(shipping.get('last_name') or billing.get('last_name') or '').strip()
            rec = (fn + ' ' + ln).strip()
            if rec:
                order.shipping_recipient = rec
                updated_ship_fields.append('shipping_recipient')

        if not (order.shipping_phone or '').strip():
            phone = str(billing.get('phone') or '').strip()
            if phone:
                order.shipping_phone = phone
                updated_ship_fields.append('shipping_phone')

        if updated_ship_fields:
            order.save(update_fields=updated_ship_fields)

    # Pull TTN from marketplace payload if not already set manually.
    # Rule: never overwrite a TTN that was filled in manually (non-empty before this sync).
    # Exception: if the existing TTN came from a previous sync (stored in payload),
    # we still allow updating it so stale TTNs don't get stuck.
    ttn_from_payload = extract_ttn_from_payload(channel, payload)
    if ttn_from_payload:
        current_ttn = (order.shipping_ttn or "").strip()
        prev_ttn_from_sync = str((order.external_payload or {}).get("_synced_ttn") or "").strip()
        # Update if: no TTN yet, OR current TTN was set by a previous sync (not manually)
        if not current_ttn or current_ttn == prev_ttn_from_sync:
            order.shipping_ttn = ttn_from_payload
            # Remember which TTN came from sync so we can distinguish it from manual edits
            p = order.external_payload or {}
            p["_synced_ttn"] = ttn_from_payload
            order.external_payload = p
            order.save(update_fields=["shipping_ttn", "external_payload"])

    # Normalize item keys and merge duplicates
    wanted: Dict[Tuple[int, int], Dict[str, Any]] = {}
    unmatched: List[dict] = []

    for product, variant, qty, unit_price, name, raw in items:
        qty = int(qty or 0)
        if qty <= 0:
            continue

        if not product and not variant:
            unmatched.append({"name": name, "qty": qty, "raw": raw})
            continue

        if variant and not product:
            product = variant.product

        pid = int(product.id)
        vid = int(variant.id) if variant else 0
        key = (pid, vid)

        if key not in wanted:
            wanted[key] = {
                "product": product,
                "variant": variant,
                "qty": qty,
                "price": unit_price,
                "name": name,
                "raw": raw,
            }
        else:
            wanted[key]["qty"] += qty

    existing = {
        (int(i.product_id), int(i.variant_id or 0)): i
        for i in order.items.select_related("product", "variant").all()
    }
    items_changed = False

    # Upsert items
    for key, data in wanted.items():
        qty = int(data["qty"])
        price = _to_decimal(data["price"])
        product = data["product"]
        variant = data["variant"]

        if key in existing:
            it = existing[key]
            upd: List[str] = []
            if int(it.qty) != qty:
                it.qty = qty
                upd.append("qty")
            if it.price != price:
                it.price = price
                upd.append("price")
            it.variant_attrs = {"source_name": data.get("name") or ""}
            upd.append("variant_attrs")
            if upd:
                it.save(update_fields=upd)
                items_changed = True
        else:
            OrderItem.objects.create(
                order=order,
                product=product,
                variant=variant if variant else None,
                qty=qty,
                price=price,
                variant_attrs={"source_name": data.get("name") or ""},
            )
            items_changed = True

    # Delete removed items
    for key, it in existing.items():
        if key not in wanted:
            it.delete()
            items_changed = True

    order.recalc()

    # Store unmatched items in payload for UI
    if unmatched:
        p = order.external_payload or {}
        p["unmatched_items"] = unmatched
        order.external_payload = p
        order.save(update_fields=["external_payload"])
    else:
        # Clean stale unmatched list if fixed
        p = order.external_payload or {}
        if "unmatched_items" in p:
            p.pop("unmatched_items", None)
            order.external_payload = p
            order.save(update_fields=["external_payload"])

    return order, created, items_changed, unmatched


# safe_apply_policy removed — sync no longer applies stock actions
