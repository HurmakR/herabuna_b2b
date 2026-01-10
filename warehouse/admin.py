from django.contrib import admin

from .models import InventoryLot, InventoryMove, InventoryReservation, InboundReceipt, InboundReceiptLine


@admin.register(InventoryLot)
class InventoryLotAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "product",
        "received_at",
        "unit_cost",
        "qty_in",
        "qty_reserved",
        "qty_out",
        "qty_available",
        "reference",
    )
    list_filter = ("received_at", "product")
    search_fields = ("product__sku", "product__name", "reference")
    ordering = ("-received_at", "-id")
    readonly_fields = ("received_at", "qty_available")

    fieldsets = (
        (None, {"fields": ("product", "received_at", "reference")}),
        ("Pricing", {"fields": ("unit_cost",)}),
        ("Quantities", {"fields": ("qty_in", "qty_reserved", "qty_out", "qty_available")}),
    )


@admin.register(InventoryMove)
class InventoryMoveAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "move_type",
        "product",
        "lot",
        "order",
        "order_item",
        "qty",
        "created_at",
        "note",
    )
    list_filter = ("move_type", "created_at", "product")
    search_fields = (
        "product__sku",
        "product__name",
        "order__id",
        "order_item__order__id",
        "note",
    )
    ordering = ("-created_at", "-id")
    readonly_fields = ("created_at",)
    raw_id_fields = ("product", "lot", "order", "order_item")


@admin.register(InventoryReservation)
class InventoryReservationAdmin(admin.ModelAdmin):
    list_display = (
        "id",
        "order_id",
        "order_item",
        "product_sku",
        "lot",
        "qty",
        "created_at",
    )
    list_filter = ("created_at",)
    search_fields = (
        "order_item__order__id",
        "order_item__product__sku",
        "order_item__product__name",
        "lot__id",
        "lot__reference",
    )
    ordering = ("-created_at", "-id")
    readonly_fields = ("created_at",)
    raw_id_fields = ("order_item", "lot")

    @admin.display(description="Order")
    def order_id(self, obj):
        return getattr(obj.order_item, "order_id", None)

    @admin.display(description="SKU")
    def product_sku(self, obj):
        p = getattr(obj.order_item, "product", None)
        return getattr(p, "sku", "")


class InboundReceiptLineInline(admin.TabularInline):
    model = InboundReceiptLine
    extra = 0
    raw_id_fields = ("product", "created_lot")


@admin.register(InboundReceipt)
class InboundReceiptAdmin(admin.ModelAdmin):
    list_display = ("id", "received_date", "supplier", "external_ref", "currency", "note", "created_at")
    list_filter = ("currency", "received_date", "created_at")
    search_fields = ("supplier", "external_ref", "note")
    ordering = ("-received_date", "-id")
    readonly_fields = ("created_at",)
    inlines = (InboundReceiptLineInline,)
