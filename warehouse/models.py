from __future__ import annotations

from django.db import models
from django.db.models import F, Sum


class InventoryLot(models.Model):
    product = models.ForeignKey("b2b.Product", on_delete=models.CASCADE, related_name="lots")
    received_at = models.DateTimeField(auto_now_add=True)

    unit_cost = models.DecimalField(max_digits=10, decimal_places=2)
    reference = models.CharField(max_length=120, blank=True)

    qty_in = models.PositiveIntegerField()
    qty_reserved = models.PositiveIntegerField(default=0)
    qty_out = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ("-received_at", "-id")
        indexes = [
            models.Index(fields=["product"], name="warehouse_i_product_a30a23_idx"),
        ]

    @property
    def qty_available(self) -> int:
        qty_in = int(self.qty_in or 0)
        qty_reserved = int(self.qty_reserved or 0)
        qty_out = int(self.qty_out or 0)
        return max(0, qty_in - qty_reserved - qty_out)

    def __str__(self) -> str:
        return f"Lot #{self.id} {self.product.sku} ({self.qty_available}/{self.qty_in})"


class InventoryMove(models.Model):
    MOVE_IN = "IN"
    MOVE_RESERVE = "RSV"
    MOVE_RELEASE = "REL"
    MOVE_SHIP = "SHP"
    MOVE_ADJUST = "ADJ"

    MOVE_TYPES = [
        (MOVE_IN, "Inbound"),
        (MOVE_RESERVE, "Reserve"),
        (MOVE_RELEASE, "Release"),
        (MOVE_SHIP, "Ship"),
        (MOVE_ADJUST, "Adjust"),
    ]

    move_type = models.CharField(max_length=3, choices=MOVE_TYPES)
    product = models.ForeignKey("b2b.Product", on_delete=models.CASCADE)
    lot = models.ForeignKey("warehouse.InventoryLot", null=True, blank=True, on_delete=models.SET_NULL)

    order = models.ForeignKey("b2b.Order", null=True, blank=True, on_delete=models.SET_NULL)
    order_item = models.ForeignKey("b2b.OrderItem", null=True, blank=True, on_delete=models.SET_NULL)

    qty = models.IntegerField()
    note = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["product"], name="warehouse_i_product_36a03a_idx"),
            models.Index(fields=["move_type", "created_at"], name="warehouse_i_move_ty_c20af1_idx"),
        ]
        ordering = ("-created_at", "-id")

    def __str__(self) -> str:
        return f"{self.move_type} {self.product.sku} {self.qty} @ {self.created_at:%Y-%m-%d}"


class InventoryReservation(models.Model):
    lot = models.ForeignKey("warehouse.InventoryLot", on_delete=models.CASCADE, related_name="reservations")
    order_item = models.ForeignKey("b2b.OrderItem", on_delete=models.CASCADE, related_name="lot_reservations")
    qty = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ("-created_at", "-id")

    @property
    def order_id(self) -> int | None:
        return getattr(self.order_item, "order_id", None)

    def __str__(self) -> str:
        return f"RSV lot#{self.lot_id} item#{self.order_item_id} qty={self.qty}"


def recompute_product_stock(product_id: int) -> int:
    total = (
        InventoryLot.objects.filter(product_id=product_id)
        .aggregate(available=Sum(F("qty_in") - F("qty_reserved") - F("qty_out")))
        .get("available")
    )
    available = int(total or 0)
    from b2b.models import Product
    Product.objects.filter(id=product_id).update(stock_qty=available)
    return available
