from django.core.management.base import BaseCommand

from b2b.services.woo_sync import import_missing_products_from_woo


class Command(BaseCommand):
    help = "Import missing products from WooCommerce into local catalog (catalog-only; no stock/price sync)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--status",
            default="publish",
            help="Woo product status to fetch (default: publish).",
        )

    def handle(self, *args, **options):
        res = import_missing_products_from_woo(status=options["status"])
        self.stdout.write(
            self.style.SUCCESS(
                "Woo import done. "
                f"Created: {res.created}, linked_by_sku: {res.linked_by_sku}, "
                f"skipped: {res.skipped_existing}, categories_created: {res.categories_created}, "
                f"brands_created: {res.brands_created}"
            )
        )
