from django.core.management.base import BaseCommand
from b2b.models import Product
from b2b.services.woo_sync import WooClient

class Command(BaseCommand):
    help = 'Sync products & stock with WooCommerce (two-way MVP).'

    def handle(self, *args, **options):
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

        self.stdout.write(self.style.SUCCESS(f'Sync done. Pulled {pulled} products, pushed stock for {pushed}'))
