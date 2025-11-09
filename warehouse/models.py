from django.db import models
from django.conf import settings

class InventoryLot(models.Model):
    """Single inbound lot with purchase unit cost."""
    product = models.ForeignKey('b2b.Product', on_delete=models.CASCADE, related_name='lots')
    qty_in = models.PositiveIntegerField()
    qty_reserved = models.PositiveIntegerField(default=0)
    qty_out = models.PositiveIntegerField(default=0)
    unit_cost = models.DecimalField(max_digits=10, decimal_places=2)  # cost per unit in UAH
    reference = models.CharField(max_length=120, blank=True)  # e.g., supplier doc #
    received_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['received_at', 'id']

    @property
    def qty_available(self) -> int:
        return max(0, self.qty_in - self.qty_reserved - self.qty_out)

    def __str__(self):
        return f"{self.product.sku} lot#{self.pk} @ {self.unit_cost} ({self.qty_available}/{self.qty_in})"


class InventoryReservation(models.Model):
    """Lot reservations per order item."""
    order_item = models.ForeignKey('b2b.OrderItem', on_delete=models.CASCADE, related_name='lot_reservations')
    lot = models.ForeignKey(InventoryLot, on_delete=models.CASCADE, related_name='reservations')
    qty = models.PositiveIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)


class InventoryMove(models.Model):
    """Audit trail of inventory movements."""
    INBOUND = 'IN'
    RESERVE = 'RSV'
    RELEASE = 'REL'
    SHIP = 'SHP'
    ADJUST = 'ADJ'
    MOVE_TYPES = [
        (INBOUND, 'Inbound'),
        (RESERVE, 'Reserve'),
        (RELEASE, 'Release'),
        (SHIP, 'Ship'),
        (ADJUST, 'Adjust'),
    ]

    product = models.ForeignKey('b2b.Product', on_delete=models.CASCADE)
    lot = models.ForeignKey(InventoryLot, on_delete=models.SET_NULL, null=True, blank=True)
    order = models.ForeignKey('b2b.Order', on_delete=models.SET_NULL, null=True, blank=True)
    order_item = models.ForeignKey('b2b.OrderItem', on_delete=models.SET_NULL, null=True, blank=True)
    move_type = models.CharField(max_length=3, choices=MOVE_TYPES)
    qty = models.IntegerField()  # positive for IN/RESERVE, negative for RELEASE/ADJUST if needed
    note = models.CharField(max_length=200, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['product']),
            models.Index(fields=['move_type', 'created_at']),
        ]
