from django.contrib import admin, messages
from django.urls import path
from django.shortcuts import redirect
import decimal

from .models import (
    Dealer,
    Brand,
    Category,
    Facet,
    Product,
    ProductImage,
    ProductCategory,
    ProductVariant,
    Order,
    OrderItem,
)
from .services.woo_sync import list_missing_products_from_woo


@admin.register(Dealer)
class DealerAdmin(admin.ModelAdmin):
    """Dealer admin configuration."""
    list_display = ("username", "company_name", "email", "is_active", "is_staff")
    search_fields = ("username", "company_name", "email")


@admin.register(Brand)
class BrandAdmin(admin.ModelAdmin):
    """Brand admin configuration."""
    list_display = ("name", "slug", "woo_id")
    search_fields = ("name", "slug")


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    """Category admin configuration."""
    list_display = ("name", "slug", "woo_id", "is_active", "parent")
    search_fields = ("name", "slug")
    list_filter = ("is_active",)


class ProductImageInline(admin.TabularInline):
    """Inline editor for per-product images (admin UX)."""
    model = ProductImage
    extra = 0


def _facet_type_from_attr_name(attr_name: str):
    """Heuristic mapping of Woo attribute names to facet types (not order options)."""
    n = (attr_name or "").strip().lower()
    if "ingredient" in n or "інгреді" in n:
        return "ingredient"
    if "effective" in n or "ефектив" in n:
        return "effect"
    if "season" in n or "сезон" in n:
        return "season"
    return None


def sync_with_woo(modeladmin, request, queryset):
    """DEPRECATED.

    Woo is used as a catalog-only source.
    Imports are managed from Reports -> Service -> Woo Import.

    This action performs no writes; it only shows how many products are missing.
    """
    missing = list_missing_products_from_woo()
    modeladmin.message_user(
        request,
        f"Woo sync перенесено в меню 'Сервіс'. У Woo знайдено {len(missing)} товар(ів), яких нема в каталозі.",
        level=messages.WARNING,
    )


sync_with_woo.short_description = "Синхронізувати зараз"


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    """Product admin with sync action and inline images."""
    list_display = ("sku", "name", "brand", "wholesale_price", "retail_price", "stock_qty", "weight_g", "is_active")
    search_fields = ("sku", "name")
    list_editable = ("wholesale_price", "is_active")
    readonly_fields = ("stock_qty",)
    list_filter = ("is_active", "brand")
    actions = [sync_with_woo]
    inlines = [ProductImageInline]

    def get_urls(self):
        """Add a custom admin URL used by the visible 'Sync now' button in template."""
        urls = super().get_urls()
        custom = [
            path(
                "sync-now/",
                self.admin_site.admin_view(self.sync_now_view),
                name="b2b_product_sync_now",
            ),
        ]
        return custom + urls

    def sync_now_view(self, request):
        """Redirect to Service -> Woo Import page."""
        return redirect("reports:service_woo_import")


class OrderItemInline(admin.TabularInline):
    """Inline editor for order items inside Order admin."""
    model = OrderItem
    extra = 0


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    """Order admin with inline items."""
    list_display = ("id", "dealer", "status", "subtotal", "total", "created_at")
    list_filter = ("status", "created_at")
    inlines = [OrderItemInline]
