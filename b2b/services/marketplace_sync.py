from __future__ import annotations

from datetime import timedelta
from typing import List

from django.utils import timezone

from b2b.models import Order
from b2b.services.marketplace_orders import SyncResult, safe_apply_policy, upsert_external_order
from b2b.services.rozetka_orders import RozetkaClient, normalize_rozetka_order
from b2b.services.woo_orders import WooOrdersClient, normalize_woo_order


def _woo_status_to_action(status: str) -> str:
    s = (status or "").strip().lower()
    if s in {"cancelled", "refunded", "failed"}:
        return "release"
    if s in {"processing", "on-hold", "completed"}:
        return "reserve"
    return ""


def _rozetka_to_action(status_group: int | None) -> str:
    if status_group is None:
        return ""
    # status_group: 1 - processing, 2 - successful, 3 - unsuccessful
    if int(status_group) == 3:
        return "release"
    return "reserve"


def sync_woo_orders(*, days: int = 14, auto_apply: bool = True) -> SyncResult:
    res = SyncResult()
    client = WooOrdersClient()
    raw_orders = client.fetch_orders(days=days)

    for raw in raw_orders:
        norm = normalize_woo_order(raw)
        if not norm.external_id:
            continue

        items = [(i.product, i.variant, i.qty, i.unit_price, i.name, i.raw) for i in norm.items]

        order, created, items_changed, _unmatched = upsert_external_order(
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

        if auto_apply:
            safe_apply_policy(
                order=order,
                desired_action=_woo_status_to_action(norm.external_status),
                result=res,
                items_changed=items_changed,
            )

    return res


def sync_rozetka_orders(*, days: int = 14, auto_apply: bool = True, types: int = 1) -> SyncResult:
    res = SyncResult()
    client = RozetkaClient()
    raw_orders = client.fetch_orders(days=days, types=types)

    for raw in raw_orders:
        norm = normalize_rozetka_order(raw)
        if not norm.external_id:
            continue

        items = [(i.product, None, i.qty, i.unit_price, i.name, i.raw) for i in norm.items]

        order, created, items_changed, _unmatched = upsert_external_order(
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

        if auto_apply:
            safe_apply_policy(
                order=order,
                desired_action=_rozetka_to_action(norm.status_group),
                result=res,
                items_changed=items_changed,
            )

    return res
