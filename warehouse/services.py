from decimal import Decimal
from django.db import transaction
from django.db.models import Sum
from .models import InventoryLot, InventoryReservation, InventoryMove

def recompute_product_stock(product):
    """Recalculate Product.stock_qty from lots.available."""
    agg = product.lots.aggregate(avail=Sum('qty_in') - Sum('qty_reserved') - Sum('qty_out'))
    product.stock_qty = int(agg['avail'] or 0)
    product.save(update_fields=['stock_qty'])

def receive_lot(product, qty: int, unit_cost: Decimal, reference: str = "") -> InventoryLot:
    """Create inbound lot and move."""
    lot = InventoryLot.objects.create(product=product, qty_in=qty, unit_cost=unit_cost, reference=reference)
    InventoryMove.objects.create(product=product, lot=lot, move_type=InventoryMove.INBOUND, qty=qty, note=reference)
    recompute_product_stock(product)
    return lot

def _ensure_bootstrap_lot_if_needed(product, needed_qty: int):
    """Create a synthetic lot if there is no stock but system must operate."""
    available = sum(l.qty_available for l in product.lots.all())
    if available >= needed_qty:
        return
    # Create a virtual lot using product.cost_price as unit cost
    note = "Auto-created bootstrap lot from product.cost_price"
    receive_lot(product, needed_qty, product.cost_price or Decimal('0.00'), reference=note)

@transaction.atomic
def reserve_order(order):
    """
    FIFO-reserve lots for each item; snapshot cost on item.
    If no lots exist, create a bootstrap lot from product.cost_price.
    """
    for it in order.items.select_related('product').all():
        need = int(it.qty)
        product = it.product

        # Ensure we have enough available lots (bootstrap if necessary)
        _ensure_bootstrap_lot_if_needed(product, need)

        remaining = need
        cost_sum = Decimal('0.00')
        reserved_total = 0

        for lot in product.lots.select_for_update().all():
            if remaining <= 0:
                break
            take = min(remaining, lot.qty_available)
            if take <= 0:
                continue
            # reserve
            lot.qty_reserved += take
            lot.save(update_fields=['qty_reserved'])
            InventoryReservation.objects.create(order_item=it, lot=lot, qty=take)
            InventoryMove.objects.create(product=product, lot=lot, order=order, order_item=it,
                                         move_type=InventoryMove.RESERVE, qty=take, note="Order submit")
            cost_sum += (lot.unit_cost * take)
            reserved_total += take
            remaining -= take

        # snapshot weighted average cost on OrderItem
        if reserved_total > 0:
            it.cost_unit = (cost_sum / Decimal(reserved_total)).quantize(Decimal('0.01'))
            it.cost_total = (it.cost_unit * Decimal(it.qty)).quantize(Decimal('0.01'))
            it.save(update_fields=['cost_unit', 'cost_total'])

        # Reflect total available stock on Product (for UI)
        recompute_product_stock(product)

@transaction.atomic
def cancel_order(order):
    """Release all reservations and clear cost snapshot."""
    for it in order.items.select_related('product').all():
        for r in it.lot_reservations.select_related('lot').all():
            lot = r.lot
            lot.qty_reserved = max(0, lot.qty_reserved - r.qty)
            lot.save(update_fields=['qty_reserved'])
            InventoryMove.objects.create(product=it.product, lot=lot, order=order, order_item=it,
                                         move_type=InventoryMove.RELEASE, qty=r.qty, note="Order cancel")
        it.lot_reservations.all().delete()
        it.cost_unit = None
        it.cost_total = None
        it.save(update_fields=['cost_unit', 'cost_total'])
        recompute_product_stock(it.product)

@transaction.atomic
def ship_order(order):
    """Consume reserved quantities from lots and finalize COGS."""
    for it in order.items.select_related('product').all():
        for r in it.lot_reservations.select_related('lot').all():
            lot = r.lot
            # move reserved -> out
            lot.qty_reserved = max(0, lot.qty_reserved - r.qty)
            lot.qty_out += r.qty
            lot.save(update_fields=['qty_reserved', 'qty_out'])
            InventoryMove.objects.create(product=it.product, lot=lot, order=order, order_item=it,
                                         move_type=InventoryMove.SHIP, qty=r.qty, note="Order ship")
        it.lot_reservations.all().delete()
        # Product stock already recomputed by recompute_product_stock below
        recompute_product_stock(it.product)
