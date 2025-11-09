from django.contrib import admin
from .models import InventoryLot, InventoryReservation, InventoryMove

@admin.register(InventoryLot)
class InventoryLotAdmin(admin.ModelAdmin):
    list_display = ('id', 'product', 'unit_cost', 'qty_in', 'qty_reserved', 'qty_out', 'qty_available', 'reference', 'received_at')
    list_filter = ('product',)
    search_fields = ('product__sku', 'product__name', 'reference')
    readonly_fields = ('received_at',)

@admin.register(InventoryReservation)
class InventoryReservationAdmin(admin.ModelAdmin):
    list_display = ('id', 'order_item', 'lot', 'qty', 'created_at')
    search_fields = ('order_item__order__id', 'order_item__product__sku')
    readonly_fields = ('created_at',)

@admin.register(InventoryMove)
class InventoryMoveAdmin(admin.ModelAdmin):
    list_display = ('id', 'move_type', 'product', 'lot', 'order', 'order_item', 'qty', 'note', 'created_at')
    list_filter = ('move_type', 'product')
    search_fields = ('product__sku', 'order__id', 'note')
    readonly_fields = ('created_at',)
