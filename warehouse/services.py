from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Iterable

from django.db import transaction
from django.db.models import F
from django.utils import timezone

from b2b.models import Order, OrderItem, Product
from .models import InventoryLot, InventoryMove, InventoryReservation, recompute_product_stock


class WarehouseError(Exception):
    pass


@dataclass(frozen=True)
class ReserveResult:
    reserved_total: int
    reservations: list[InventoryReservation]


@transaction.atomic
def receive_lot(*, product, qty: int, unit_cost, reference: str = "", note: str = "", user=None):
    # Create lot
    lot = InventoryLot(
        product=product,
        unit_cost=unit_cost,
        qty_in=qty,
        qty_out=0,
        qty_reserved=0,
        reference=reference or "",
    )

    # Save optional note if the model has this field
    if hasattr(lot, "note"):
        lot.note = note or ""
    lot.save()

    # Create move IN
    move = InventoryMove(
        move_type="IN",
        product=product,
        lot=lot,
        qty=qty,
        created_at=timezone.now(),
    )

    # Save optional note/created_by if those fields exist on the model
    if hasattr(move, "note"):
        move.note = note or reference or ""
    if hasattr(move, "created_by"):
        move.created_by = user
    move.save()

    # Update aggregate stock on Product (single source for catalog availability)
    product.stock_qty = (product.stock_qty or 0) + int(qty)
    product.save(update_fields=["stock_qty"])

    return lot


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


def _iter_fifo_lots(product: Product) -> Iterable[InventoryLot]:
    return InventoryLot.objects.filter(product=product).order_by("received_at", "id").select_for_update()


@transaction.atomic
def reserve_order(order: Order) -> None:
    if order.status != "draft":
        return

    InventoryReservation.objects.filter(order_item__order=order).delete()

    for item in order.items.select_related("product", "variant").all():
        _reserve_order_item(item)

    order.recalc()
    order.save(update_fields=["subtotal", "total"])


def _reserve_order_item(item: OrderItem) -> ReserveResult:
    product = item.product
    qty_need = int(item.qty or 0)
    if qty_need <= 0:
        return ReserveResult(0, [])

    reserved_total = 0
    reservations: list[InventoryReservation] = []

    for lot in _iter_fifo_lots(product):
        if reserved_total >= qty_need:
            break
        can = min(lot.qty_available, qty_need - reserved_total)
        if can <= 0:
            continue

        lot.qty_reserved = F("qty_reserved") + can
        lot.save(update_fields=["qty_reserved"])
        lot.refresh_from_db(fields=["qty_in", "qty_reserved", "qty_out"])

        res = InventoryReservation.objects.create(lot=lot, order_item=item, qty=can)
        reservations.append(res)

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

    recompute_product_stock(product.id)

    if reserved_total < qty_need:
        raise WarehouseError(f"Not enough stock for {product.sku}. Need {qty_need}, reserved {reserved_total}")

    return ReserveResult(reserved_total, reservations)


@transaction.atomic
def cancel_order(order: Order) -> None:
    res_qs = InventoryReservation.objects.select_related("lot", "order_item").filter(order_item__order=order)
    for res in res_qs:
        lot = res.lot
        lot.qty_reserved = F("qty_reserved") - int(res.qty)
        lot.save(update_fields=["qty_reserved"])
        InventoryMove.objects.create(
            move_type=InventoryMove.MOVE_RELEASE,
            product=lot.product,
            lot=lot,
            order=order,
            order_item=res.order_item,
            qty=int(res.qty),
            note="cancel",
        )
        recompute_product_stock(lot.product_id)

    res_qs.delete()
    order.recalc()
    order.save(update_fields=["subtotal", "total"])


@transaction.atomic
def ship_order(order: Order) -> None:
    res_qs = InventoryReservation.objects.select_related("lot", "order_item").filter(order_item__order=order)
    if not res_qs.exists():
        reserve_order(order)
        res_qs = InventoryReservation.objects.select_related("lot", "order_item").filter(order_item__order=order)

    item_totals: dict[int, Decimal] = {}
    item_qty: dict[int, int] = {}

    for res in res_qs:
        lot = res.lot
        qty = int(res.qty)

        lot.qty_reserved = F("qty_reserved") - qty
        lot.qty_out = F("qty_out") + qty
        lot.save(update_fields=["qty_reserved", "qty_out"])

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

        recompute_product_stock(lot.product_id)

    for item_id, total in item_totals.items():
        qty = item_qty.get(item_id, 0)
        unit = (total / qty) if qty else Decimal("0")
        OrderItem.objects.filter(id=item_id).update(cost_unit=unit, cost_total=total)

    res_qs.delete()

# --- add below at the end of warehouse/services.py ---

@transaction.atomic
def adjust_stock(*, product: Product, qty_delta: int, lot: InventoryLot | None = None, note: str = "", user=None) -> None:
    """
    Inventory adjustment (ADJ).
    - If `lot` is provided: apply delta to that lot.
    - If `lot` is not provided and delta < 0: consume FIFO lots until delta is satisfied.
    - If `lot` is not provided and delta > 0: create a new lot as an adjustment lot.
    """
    delta = int(qty_delta or 0)
    if delta == 0:
        return

    note = (note or "").strip()

    # Apply to a specific lot
    if lot is not None:
        adjust_lot(lot=lot, delta=delta, note=note)
        return

    # Negative adjustment: write-off from FIFO lots
    if delta < 0:
        remaining = abs(delta)

        for fifo_lot in _iter_fifo_lots(product):
            if remaining <= 0:
                break

            available = int(fifo_lot.qty_available or 0)
            if available <= 0:
                continue

            take = min(available, remaining)

            fifo_lot.qty_out = F("qty_out") + take
            fifo_lot.save(update_fields=["qty_out"])
            fifo_lot.refresh_from_db(fields=["qty_in", "qty_reserved", "qty_out"])

            mv = InventoryMove(
                move_type=InventoryMove.MOVE_ADJUST,
                product=product,
                lot=fifo_lot,
                qty=-take,
                note=note,
            )
            if hasattr(mv, "created_by"):
                mv.created_by = user
            mv.save()

            recompute_product_stock(fifo_lot.product_id)
            remaining -= take

        if remaining > 0:
            raise WarehouseError("Not enough available stock across lots to decrease")

        return

    # Positive adjustment: create a new lot (so we keep audit trail via lots)
    lot_ref = "ADJ+"
    adj_lot = InventoryLot(
        product=product,
        unit_cost=getattr(product, "cost_price", Decimal("0")) or Decimal("0"),
        qty_in=delta,
        qty_out=0,
        qty_reserved=0,
        reference=lot_ref,
    )
    if hasattr(adj_lot, "note"):
        adj_lot.note = note
    adj_lot.save()

    mv = InventoryMove(
        move_type=InventoryMove.MOVE_ADJUST,
        product=product,
        lot=adj_lot,
        qty=delta,
        note=note or lot_ref,
    )
    if hasattr(mv, "created_by"):
        mv.created_by = user
    mv.save()

    recompute_product_stock(product.id)
