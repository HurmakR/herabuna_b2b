from django.core.management.base import BaseCommand

from b2b.services.marketplace_sync import sync_rozetka_orders, sync_woo_orders


class Command(BaseCommand):
    help = "Import orders from Woo and/or Rozetka and (optionally) apply warehouse reservation policy."

    def add_arguments(self, parser):
        parser.add_argument("--days", type=int, default=14, help="Time window in days (default: 14).")
        parser.add_argument("--woo", action="store_true", help="Sync Woo orders.")
        parser.add_argument("--rozetka", action="store_true", help="Sync Rozetka orders.")
        parser.add_argument(
            "--no-apply",
            action="store_true",
            help="Do not apply stock policy (only import/update orders).",
        )
        parser.add_argument(
            "--rozetka-types",
            type=int,
            default=1,
            help="Rozetka orders/search 'types' parameter (default: 1 for all).",
        )

    def handle(self, *args, **options):
        days = int(options["days"])
        auto_apply = not bool(options["no_apply"])

        do_woo = bool(options["woo"])
        do_rozetka = bool(options["rozetka"])

        # If no explicit source is provided, sync both.
        if not do_woo and not do_rozetka:
            do_woo = True
            do_rozetka = True

        if do_woo:
            res = sync_woo_orders(days=days, auto_apply=auto_apply)
            self.stdout.write(
                self.style.SUCCESS(
                    f"Woo orders: created={res.created}, updated={res.updated}, "
                    f"reserved={res.reserved}, released={res.released}, shipped={res.shipped}, "
                    f"skipped_unmapped={res.skipped_unmapped}, errors={len(res.errors)}"
                )
            )
            for e in res.errors:
                self.stdout.write(self.style.WARNING(f"  - {e}"))

        if do_rozetka:
            res = sync_rozetka_orders(days=days, auto_apply=auto_apply, types=int(options["rozetka_types"]))
            self.stdout.write(
                self.style.SUCCESS(
                    f"Rozetka orders: created={res.created}, updated={res.updated}, "
                    f"reserved={res.reserved}, released={res.released}, shipped={res.shipped}, "
                    f"skipped_unmapped={res.skipped_unmapped}, errors={len(res.errors)}"
                )
            )
            for e in res.errors:
                self.stdout.write(self.style.WARNING(f"  - {e}"))
