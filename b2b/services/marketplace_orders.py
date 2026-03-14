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
    """Apply warehouse action for a marketplace order.

    action:
      - reserve: FIFO reserve (deducts available stock)
      - release: cancel + release reservations
      - ship: consume reserved lots (qty_out)
    """
    action = (action or "").strip().lower()

    if action == "reserve":
        if order.status == "draft":
            order.status = "submitted"
            order.save(update_fields=["status"])
        ensure_order_reserved(order)
        return

    if action == "release":
        cancel_order(order)
        if order.status != "cancelled":
            order.status = "cancelled"
            order.save(update_fields=["status"])
        return

    if action == "ship":
        ship_order(order)
        if order.status != "shipped":
            order.status = "shipped"
            order.shipped_at = timezone.now()
            order.save(update_fields=["status", "shipped_at"])
        return

    raise ValueError("Unknown action")


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
            "status": "submitted",
            "note": note or "",
            "external_status": external_status or "",
            "external_created_at": external_created_at,
            "external_payload": payload or {},
            "created_at": external_created_at or timezone.now(),
        },
    )

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


def safe_apply_policy(
    *,
    order: Order,
    desired_action: str,
    result: SyncResult,
    items_changed: bool = False,
    skip_if_unmatched: bool = True,
) -> None:
    """Apply policy while capturing errors into result."""
    desired_action = (desired_action or "").strip().lower()
    if desired_action not in {"reserve", "release", "ship"}:
        return

    payload = order.external_payload or {}
    if skip_if_unmatched and payload.get("unmatched_items") and desired_action == "reserve":
        result.skipped_unmapped += 1
        return

    try:
        if desired_action == "reserve" and items_changed and order_has_reservations(order):
            rereserve_order(order)
        else:
            apply_stock_action(order=order, action=desired_action)

        if desired_action == "reserve":
            result.reserved += 1
        elif desired_action == "release":
            result.released += 1
        elif desired_action == "ship":
            result.shipped += 1
    except WarehouseError as e:
        p = order.external_payload or {}
        p["sync_error"] = str(e)
        order.external_payload = p
        order.save(update_fields=["external_payload"])
        result.errors.append(f"{order.channel}:{order.external_id}: {e}")
