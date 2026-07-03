from __future__ import annotations

from typing import Any, Dict

from django.utils import timezone

from b2b.services.rozetka_orders import RozetkaClient
from b2b.services.woo_orders import WooOrdersClient


class MarketplacePushError(RuntimeError):
    pass


def _merge_payload(order, response: dict) -> None:
    if not response:
        return
    payload = order.external_payload or {}
    raw = payload.get("raw") or {}
    content = response.get("content") or {}
    if isinstance(content, dict):
        raw.update(content)
        payload["raw"] = raw
    payload["last_push_response"] = response
    order.external_payload = payload


def push_shipment_to_marketplace(order) -> Dict[str, Any]:
    """Push local TTN/status to external marketplace order."""
    channel = (getattr(order, "channel", "") or "").strip().lower()
    external_id = str(getattr(order, "external_id", "") or "").strip()
    ttn = str(getattr(order, "shipping_ttn", "") or "").strip()
    np_ref = str(getattr(order, "shipping_np_ref", "") or "").strip()

    if channel not in {"woo", "rozetka"}:
        return {"skipped": True, "reason": "non_marketplace"}
    if not external_id:
        raise MarketplacePushError("External order id is missing.")
    if not ttn:
        raise MarketplacePushError("TTN is missing.")

    if channel == "woo":
        client = WooOrdersClient()
        response = client.push_shipment(external_id, ttn=ttn, np_ref=np_ref, status="completed")
        order.external_status = str((response or {}).get("status") or order.external_status or "completed")
        _merge_payload(order, response)
        order.save(update_fields=["external_status", "external_payload"])
        return {
            "channel": "woo",
            "external_id": external_id,
            "status": order.external_status,
            "message": f"Woo order #{external_id} updated to completed.",
        }

    client = RozetkaClient()
    raw_order = (order.external_payload or {}).get("raw") or {}
    response = client.push_shipment(
        external_id,
        raw_order=raw_order,
        ttn=ttn,
        seller_comment=f"TTN synced from Herabuna B2B: {ttn}",
    )
    content = (response or {}).get("content") or {}
    order.external_status = str(content.get("status") or order.external_status or "")
    _merge_payload(order, response)
    order.save(update_fields=["external_status", "external_payload"])
    return {
        "channel": "rozetka",
        "external_id": external_id,
        "status": order.external_status,
        "message": f"Rozetka order #{external_id} updated with TTN.",
    }
