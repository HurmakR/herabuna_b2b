from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from b2b.models import Order, OrderItem, Product
from .models import (
    InboundReceipt,
    InboundReceiptLine,
    InventoryLot,
    InventoryMove,
    InventoryReservation,
    recompute_product_stock,
)


class WarehouseError(Exception):
    pass


@dataclass(frozen=True)
class ReserveResult:
    reserved_total: int
    reservations: list[InventoryReservation]


def _recompute_products_stock(product_ids: Iterable[int]) -> None:
    for pid in sorted({int(p) for p in product_ids if p}):
        recompute_product_stock(pid)


@transaction.atomic
def receive_lot(
    *,
    product: Product,
    qty: int,
    unit_cost,
    reference: str = "",
    note: str = "",
    supplier: str = "",
    currency: str = "UAH",
    external_ref: str = "",
    created_by=None,
    recompute: bool = True,
):
    """Create a new inbound lot and register an IN move."""
    qty = int(qty)
    if qty <= 0:
        raise ValueError("qty must be positive")

    lot = InventoryLot.objects.create(
        product=product,
        qty_in=qty,
        unit_cost=unit_cost,
        reference=reference or "",
        note=note or "",
        supplier=supplier or "",
        currency=currency or "UAH",
        external_ref=external_ref or "",
    )

    InventoryMove.objects.create(
        move_type=InventoryMove.MOVE_IN,
        product=product,
        lot=lot,
        qty=qty,
        note=reference or note or "",
        created_at=timezone.now(),
    )

    if recompute:
        recompute_product_stock(product.id)
    return lot


def _iter_fifo_lots(product: Product) -> Iterable[InventoryLot]:
    return (
        InventoryLot.objects.filter(product=product)
        .order_by("received_at", "id")
        .select_for_update()
    )


def _iter_lifo_lots(product: Product) -> Iterable[InventoryLot]:
    """Newest lots first.

    Used for inventory corrections where it's usually preferable to adjust the most
    recent receipt rather than rewriting history across all old lots.
    """
    return (
        InventoryLot.objects.filter(product=product)
        .order_by("-received_at", "-id")
        .select_for_update()
    )


def _release_existing_reservations(*, order: Order, reason: str) -> set[int]:
    """Release all existing reservations for an order and return affected product ids."""
    product_ids: set[int] = set()
    res_qs = (
        InventoryReservation.objects
        .select_related("lot", "order_item")
        .filter(order_item__order=order)
    )

    # Important: adjust lot.qty_reserved BEFORE deleting reservations.
    for res in res_qs:
        lot = InventoryLot.objects.select_for_update().get(id=res.lot_id)
        qty = int(res.qty)

        lot.qty_reserved = F("qty_reserved") - qty
        lot.save(update_fields=["qty_reserved"])
        product_ids.add(lot.product_id)

        InventoryMove.objects.create(
            move_type=InventoryMove.MOVE_RELEASE,
            product=lot.product,
            lot=lot,
            order=order,
            order_item=res.order_item,
            qty=qty,
            note=reason,
        )

    res_qs.delete()
    return product_ids


@transaction.atomic
def reserve_order(order: Order) -> None:
    """Reserve stock for a *draft* order using FIFO lots."""
    if order.status != "draft":
        return

    touched: set[int] = set()

    # If the order is being re-reserved (e.g. cart changed), undo previous reservations first.
    touched |= _release_existing_reservations(order=order, reason="re-reserve")

    for item in order.items.select_related("product", "variant").all():
        touched |= _reserve_order_item(item)

    _recompute_products_stock(touched)
    # Order.recalc() already persists subtotal/total.
    order.recalc()


@transaction.atomic
def ensure_order_reserved(order: Order) -> None:
    """Make sure an order has FIFO reservations.

    Why: statuses can be changed from admin actions; if reservations were not created
    (or were released) we must reserve again so stock numbers are consistent.

    - For draft orders: performs full re-reserve.
    - For submitted/pending_payment: reserves only if there are no reservations yet.
    """
    if order.status == "draft":
        reserve_order(order)
        return

    if order.status not in {"submitted", "pending_payment"}:
        return

    if InventoryReservation.objects.filter(order_item__order=order).exists():
        return

    touched: set[int] = set()
    for item in order.items.select_related("product", "variant").all():
        touched |= _reserve_order_item(item)

    _recompute_products_stock(touched)
    order.recalc()


@transaction.atomic
def rereserve_order(order: Order) -> None:
    """Rebuild FIFO reservations for an order regardless of current status.

    Use this when staff edits order items/qty after the initial reservation step.
    We first release existing reservations, then reserve again for current items.

    Allowed statuses: draft/submitted/pending_payment.
    """
    if order.status not in {"draft", "submitted", "pending_payment"}:
        return

    touched: set[int] = set()
    touched |= _release_existing_reservations(order=order, reason="edit")

    for item in order.items.select_related("product", "variant").all():
        touched |= _reserve_order_item(item)

    _recompute_products_stock(touched)
    order.recalc()


def _reserve_order_item(item: OrderItem) -> set[int]:
    product = item.product
    qty_need = int(item.qty or 0)
    if qty_need <= 0:
        return set()

    reserved_total = 0
    touched: set[int] = {product.id}

    for lot in _iter_fifo_lots(product):
        if reserved_total >= qty_need:
            break

        can = min(lot.qty_available, qty_need - reserved_total)
        if can <= 0:
            continue

        lot.qty_reserved = F("qty_reserved") + can
        lot.save(update_fields=["qty_reserved"])

        res = InventoryReservation.objects.create(lot=lot, order_item=item, qty=can)

        InventoryMove.objects.create(
            move_type=InventoryMove.MOVE_RESERVE,
            product=product,
            lot=lot,
            order=item.order,
            order_item=item,
            qty=-can,
            note="reserve",
        )

        reserved_total += can

    if reserved_total < qty_need:
        raise WarehouseError(f"Not enough stock for {product.sku}. Need {qty_need}, reserved {reserved_total}")

    return touched


@transaction.atomic
def cancel_order(order: Order) -> None:
    """Release reservations for an order (any status) and recompute product stock."""
    touched = _release_existing_reservations(order=order, reason="cancel")
    _recompute_products_stock(touched)
    # Order.recalc() already persists subtotal/total.
    order.recalc()


@transaction.atomic
def ship_order(order: Order) -> None:
    """Consume reserved lots and freeze COGS on order items."""
    res_qs = InventoryReservation.objects.select_related("lot", "order_item").filter(order_item__order=order)
    if not res_qs.exists():
        # Shipping without reservations is dangerous. Try to create reservations only for allowed statuses.
        ensure_order_reserved(order)
        res_qs = InventoryReservation.objects.select_related("lot", "order_item").filter(order_item__order=order)
        if not res_qs.exists():
            raise WarehouseError("Order has no reservations; cannot ship")

    item_totals: dict[int, Decimal] = {}
    item_qty: dict[int, int] = {}
    touched: set[int] = set()

    for res in res_qs:
        lot = InventoryLot.objects.select_for_update().get(id=res.lot_id)
        qty = int(res.qty)

        lot.qty_reserved = F("qty_reserved") - qty
        lot.qty_out = F("qty_out") + qty
        lot.save(update_fields=["qty_reserved", "qty_out"])
        touched.add(lot.product_id)

        InventoryMove.objects.create(
            move_type=InventoryMove.MOVE_SHIP,
            product=lot.product,
            lot=lot,
            order=order,
            order_item=res.order_item,
            qty=-qty,
            note="ship",
        )

        item_totals[res.order_item_id] = item_totals.get(res.order_item_id, Decimal("0")) + (lot.unit_cost * qty)
        item_qty[res.order_item_id] = item_qty.get(res.order_item_id, 0) + qty

    for item_id, total in item_totals.items():
        qty = item_qty.get(item_id, 0)
        unit = (total / qty) if qty else Decimal("0")
        OrderItem.objects.filter(id=item_id).update(cost_unit=unit, cost_total=total)

    res_qs.delete()
    _recompute_products_stock(touched)


@transaction.atomic
def adjust_lot(*, lot: InventoryLot, delta: int, note: str = "") -> InventoryLot:
    if delta == 0:
        return lot

    if delta < 0:
        needed = abs(int(delta))
        if lot.qty_available < needed:
            raise WarehouseError("Not enough available qty in this lot to decrease")

        lot.qty_out = F("qty_out") + needed
        lot.save(update_fields=["qty_out"])
        lot.refresh_from_db(fields=["qty_in", "qty_reserved", "qty_out"])
    else:
        lot.qty_in = F("qty_in") + int(delta)
        lot.save(update_fields=["qty_in"])
        lot.refresh_from_db(fields=["qty_in", "qty_reserved", "qty_out"])

    InventoryMove.objects.create(
        move_type=InventoryMove.MOVE_ADJUST,
        product=lot.product,
        lot=lot,
        qty=int(delta),
        note=(note or "").strip(),
    )
    recompute_product_stock(lot.product_id)
    return lot


@transaction.atomic
def adjust_stock(*, product: Product, qty_delta: int, lot: InventoryLot | None = None, note: str = "", user=None) -> None:
    """Inventory adjustment (ADJ).

    - If `lot` is provided: apply delta to that lot.
    - If `lot` is not provided and delta < 0: consume LIFO lots (newest first).
    - If `lot` is not provided and delta > 0: create a new ADJ lot using the last receipt cost.
    """
    delta = int(qty_delta or 0)
    if delta == 0:
        return

    note = (note or "").strip()

    # Apply to a specific lot
    if lot is not None:
        adjust_lot(lot=lot, delta=delta, note=note)
        return

    # Negative adjustment: write-off from the newest lots first.
    if delta < 0:
        remaining = abs(delta)

        for lifo_lot in _iter_lifo_lots(product):
            if remaining <= 0:
                break

            available = int(lifo_lot.qty_available or 0)
            if available <= 0:
                continue

            take = min(available, remaining)

            lifo_lot.qty_out = F("qty_out") + take
            lifo_lot.save(update_fields=["qty_out"])
            lifo_lot.refresh_from_db(fields=["qty_in", "qty_reserved", "qty_out"])

            mv = InventoryMove(
                move_type=InventoryMove.MOVE_ADJUST,
                product=product,
                lot=lifo_lot,
                qty=-take,
                note=note,
            )
            if hasattr(mv, "created_by"):
                mv.created_by = user
            mv.save()

            remaining -= take

        if remaining > 0:
            raise WarehouseError("Not enough available stock across lots to decrease")

        recompute_product_stock(product.id)
        return

    # Positive adjustment: create a new lot (audit trail), but use the last receipt as a reference.
    last_lot = (
        InventoryLot.objects.filter(product=product)
        .order_by("-received_at", "-id")
        .select_for_update()
        .first()
    )

    unit_cost = Decimal("0")
    supplier = ""
    external_ref = ""
    currency = "UAH"
    reference = "ADJ+"

    if last_lot is not None:
        unit_cost = last_lot.unit_cost or Decimal("0")
        supplier = getattr(last_lot, "supplier", "") or ""
        external_ref = getattr(last_lot, "external_ref", "") or ""
        currency = getattr(last_lot, "currency", "UAH") or "UAH"

    adj_lot = InventoryLot(
        product=product,
        unit_cost=unit_cost,
        qty_in=delta,
        qty_out=0,
        qty_reserved=0,
        reference=reference,
        supplier=supplier,
        external_ref=external_ref,
        currency=currency,
        note=note,
    )
    adj_lot.save()

    mv = InventoryMove(
        move_type=InventoryMove.MOVE_ADJUST,
        product=product,
        lot=adj_lot,
        qty=delta,
        note=note or reference,
    )
    if hasattr(mv, "created_by"):
        mv.created_by = user
    mv.save()

    recompute_product_stock(product.id)


@transaction.atomic
def receive_receipt(
    *,
    created_by,
    supplier: str,
    external_ref: str,
    note: str,
    currency: str,
    received_date,
    lines: list[dict],
) -> InboundReceipt:
    """Create an inbound receipt with multiple lines and create lots for each line."""
    receipt = InboundReceipt.objects.create(
        supplier=supplier or "",
        external_ref=external_ref or "",
        note=note or "",
        currency=currency or "UAH",
        received_date=received_date or timezone.now().date(),
    )

    touched: set[int] = set()

    for row in lines:
        product = row["product"]
        qty = int(row["qty"])
        unit_cost = row["unit_cost"]

        line = InboundReceiptLine.objects.create(
            receipt=receipt,
            product=product,
            qty=qty,
            unit_cost=unit_cost,
        )

        lot = receive_lot(
            product=product,
            qty=qty,
            unit_cost=unit_cost,
            reference=receipt.external_ref or f"receipt#{receipt.id}",
            note=receipt.note or "",
            supplier=receipt.supplier or "",
            currency=receipt.currency or "UAH",
            external_ref=receipt.external_ref or "",
            created_by=created_by,
            recompute=False,
        )
        touched.add(product.id)

        line.created_lot = lot
        line.save(update_fields=["created_lot"])

    _recompute_products_stock(touched)
    return receipt
