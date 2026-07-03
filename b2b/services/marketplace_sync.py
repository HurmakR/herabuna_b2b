from __future__ import annotations

from b2b.services.marketplace_orders import SyncResult, upsert_external_order
from b2b.services.rozetka_orders import RozetkaClient, normalize_rozetka_order
from b2b.services.woo_orders import WooOrdersClient, normalize_woo_order


def sync_woo_orders(*, days: int = 14) -> SyncResult:
    """Import orders from WooCommerce — data only, never touches B2B status.

    Status lifecycle is managed exclusively in B2B via the service UI
    (accept/ship/cancel). The sync only updates: items, address snapshot, TTN.
    """
    res = SyncResult()
    client = WooOrdersClient()
    raw_orders = client.fetch_orders(days=days)

    for raw in raw_orders:
        norm = normalize_woo_order(raw)
        if not norm.external_id:
            continue

        items = [(i.product, i.variant, i.qty, i.unit_price, i.name, i.raw) for i in norm.items]

        order, created, _items_changed, _unmatched = upsert_external_order(
            channel="woo",
            external_id=norm.external_id,
            external_status=norm.external_status,
            external_created_at=norm.external_created_at,
            note=norm.note,
            payload=norm.payload,
            items=items,
        )

        if created:
            res.created += 1
        else:
            res.updated += 1

    return res


def sync_rozetka_orders(*, days: int = 14, types: int = 1) -> SyncResult:
    """Import orders from Rozetka — data only, never touches B2B status."""
    res = SyncResult()
    client = RozetkaClient()
    raw_orders = client.fetch_orders(days=days, types=types)

    for raw in raw_orders:
        norm = normalize_rozetka_order(raw)
        if not norm.external_id:
            continue

        items = [(i.product, None, i.qty, i.unit_price, i.name, i.raw) for i in norm.items]

        order, created, _items_changed, _unmatched = upsert_external_order(
            channel="rozetka",
            external_id=norm.external_id,
            external_status=norm.external_status,
            external_created_at=norm.external_created_at,
            note=norm.note,
            payload=norm.payload,
            items=items,
        )

        if created:
            res.created += 1
        else:
            res.updated += 1

    return res
