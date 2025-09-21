from django.contrib import admin
from django.urls import path
from django.shortcuts import redirect
from django.contrib import messages

from .models import Dealer, Product, Order, OrderItem

@admin.register(Dealer)
class DealerAdmin(admin.ModelAdmin):
    list_display = ('username', 'company_name', 'email', 'is_active', 'is_staff')
    search_fields = ('username', 'company_name', 'email')


def sync_with_woo(modeladmin, request, queryset):
    """Bulk action (у дропдауні «Дії»)."""
    from .services.woo_sync import WooClient
    from .models import Product

    client = WooClient()
    woo_products = client.fetch_products()

    by_sku = {}
    pulled = 0
    for wp in woo_products:
        sku = (wp.get('sku') or '').strip()
        if not sku:
            continue
        by_sku[sku] = wp
        p, created = Product.objects.get_or_create(sku=sku, defaults={
            'name': wp.get('name') or sku,
            'retail_price': wp.get('price') or 0,
            'stock_qty': wp.get('stock_quantity') or 0,
            'woo_id': wp.get('id'),
            'is_active': wp.get('status') == 'publish',
        })
        if not created:
            p.name = wp.get('name') or p.name
            p.retail_price = wp.get('price') or p.retail_price
            p.stock_qty = wp.get('stock_quantity') or p.stock_qty
            p.is_active = (wp.get('status') == 'publish')
            p.woo_id = wp.get('id')
            p.save()
        pulled += 1

    pushed = 0
    for p in Product.objects.exclude(woo_id__isnull=True):
        wp = by_sku.get(p.sku)
        if not wp:
            continue
        wp_stock = wp.get('stock_quantity') or 0
        if int(wp_stock) != int(p.stock_qty):
            client.update_stock(p.woo_id, p.stock_qty)
            pushed += 1

    modeladmin.message_user(request, f'Woo sync complete. Pulled {pulled}, pushed stock for {pushed}.')
sync_with_woo.short_description = "Синхронізувати зараз (action)"


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    list_display = ('sku', 'name', 'wholesale_price', 'retail_price', 'stock_qty', 'is_active')
    search_fields = ('sku', 'name')
    list_editable = ('wholesale_price', 'stock_qty', 'is_active')
    actions = [sync_with_woo]  # лишаємо action

    # окремий URL + кнопка у верхній панелі списку
    def get_urls(self):
        urls = super().get_urls()
        custom = [
            path('sync-now/', self.admin_site.admin_view(self.sync_now_view), name='b2b_product_sync_now'),
        ]
        return custom + urls

    def sync_now_view(self, request):
        """Клік на кнопку «Синхронізувати зараз» у списку товарів."""
        try:
            from .services.woo_sync import WooClient
            from .models import Product

            client = WooClient()
            woo_products = client.fetch_products()

            by_sku = {}
            for wp in woo_products:
                sku = (wp.get('sku') or '').strip()
                if not sku:
                    continue
                by_sku[sku] = wp
                p, created = Product.objects.get_or_create(sku=sku, defaults={
                    'name': wp.get('name') or sku,
                    'retail_price': wp.get('price') or 0,
                    'stock_qty': wp.get('stock_quantity') or 0,
                    'woo_id': wp.get('id'),
                    'is_active': wp.get('status') == 'publish',
                })
                if not created:
                    p.name = wp.get('name') or p.name
                    p.retail_price = wp.get('price') or p.retail_price
                    p.stock_qty = wp.get('stock_quantity') or p.stock_qty
                    p.is_active = (wp.get('status') == 'publish')
                    p.woo_id = wp.get('id')
                    p.save()

            pushed = 0
            for p in Product.objects.exclude(woo_id__isnull=True):
                wp = by_sku.get(p.sku)
                if not wp:
                    continue
                wp_stock = wp.get('stock_quantity') or 0
                if int(wp_stock) != int(p.stock_qty):
                    client.update_stock(p.woo_id, p.stock_qty)
                    pushed += 1

            messages.success(request, f'Woo sync complete. Pulled {len(by_sku)}, pushed stock for {pushed}.')
        except Exception as e:
            messages.error(request, f'Woo sync failed: {e}')
        return redirect('..')


class OrderItemInline(admin.TabularInline):
    model = OrderItem
    extra = 0


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    list_display = ('id', 'dealer', 'status', 'subtotal', 'total', 'created_at')
    list_filter = ('status', 'created_at')
    inlines = [OrderItemInline]
