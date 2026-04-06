from __future__ import annotations

from typing import Any, Dict


def _payload_raw(order) -> dict:
    payload = getattr(order, "external_payload", None) or {}
    return payload.get("raw") or {}


def marketplace_label(channel: str) -> str:
    c = (channel or "").strip().lower()
    if c == "woo":
        return "Woo"
    if c == "rozetka":
        return "Rozetka"
    return c or "B2B"


def extract_payment_meta(channel: str, payload: dict | None) -> dict:
    payload = payload or {}
    raw = payload.get("raw") or {}
    payment_label = ""
    payment_code = ""
    requires_control_payment = False

    if channel == "woo":
        payment_label = str(raw.get("payment_method_title") or payload.get("payment_method_title") or "").strip()
        payment_code = str(raw.get("payment_method") or payload.get("payment_method") or "").strip()
    elif channel == "rozetka":
        payment_label = str(
            raw.get("payment_method_title")
            or raw.get("payment_method")
            or raw.get("payment")
            or payload.get("payment_method_title")
            or payload.get("payment_method")
            or ""
        ).strip()
        payment_code = str(raw.get("payment_method") or payload.get("payment_method") or "").strip()

    lower = f"{payment_label} {payment_code}".lower()
    if any(k in lower for k in ["на відділенні", "у відділенні", "при отриманні", "післяплата", "налож", "cashondelivery", "cod"]):
        requires_control_payment = True

    return {
        "payment_label": payment_label or (payment_code or "—"),
        "payment_code": payment_code,
        "requires_control_payment": requires_control_payment,
    }


def order_payment_label(order) -> str:
    return extract_payment_meta(getattr(order, "channel", ""), getattr(order, "external_payload", None)).get("payment_label") or "—"


def order_requires_control_payment(order) -> bool:
    return bool(extract_payment_meta(getattr(order, "channel", ""), getattr(order, "external_payload", None)).get("requires_control_payment"))


def enrich_order_ui_meta(order):
    meta = extract_payment_meta(getattr(order, "channel", ""), getattr(order, "external_payload", None))
    order.marketplace_label = marketplace_label(getattr(order, "channel", ""))
    order.payment_label = meta["payment_label"]
    order.requires_control_payment = meta["requires_control_payment"]
    return order


def extract_shipping_snapshot(channel: str, payload: dict | None) -> Dict[str, str]:
    payload = payload or {}
    raw = payload.get("raw") or {}
    out = {
        "shipping_city": "",
        "shipping_city_ref": "",
        "shipping_warehouse": "",
        "shipping_warehouse_ref": "",
        "shipping_recipient": "",
        "shipping_phone": "",
    }

    if channel == "woo":
        billing = raw.get("billing") or payload.get("billing") or {}
        shipping = raw.get("shipping") or payload.get("shipping") or {}
        out["shipping_city"] = str(shipping.get("city") or billing.get("city") or "").strip()
        out["shipping_warehouse"] = str(shipping.get("address_1") or shipping.get("address_2") or "").strip()
        fn = str(shipping.get("first_name") or billing.get("first_name") or "").strip()
        ln = str(shipping.get("last_name") or billing.get("last_name") or "").strip()
        out["shipping_recipient"] = (f"{fn} {ln}").strip()
        out["shipping_phone"] = str(billing.get("phone") or "").strip()
        return out

    if channel == "rozetka":
        user = raw.get("user") or payload.get("user") or {}
        delivery = raw.get("delivery") or payload.get("delivery") or {}
        out["shipping_city"] = str(
            delivery.get("city")
            or delivery.get("city_name")
            or delivery.get("city_label")
            or delivery.get("locality")
            or ""
        ).strip()
        out["shipping_city_ref"] = str(delivery.get("city_ref") or delivery.get("settlement_ref") or "").strip()
        out["shipping_warehouse"] = str(
            delivery.get("warehouse")
            or delivery.get("warehouse_name")
            or delivery.get("branch")
            or delivery.get("branch_name")
            or delivery.get("address")
            or ""
        ).strip()
        out["shipping_warehouse_ref"] = str(delivery.get("warehouse_ref") or delivery.get("branch_ref") or "").strip()
        out["shipping_recipient"] = str(user.get("fio") or user.get("name") or delivery.get("recipient") or "").strip()
        out["shipping_phone"] = str(raw.get("user_phone") or user.get("phone") or delivery.get("phone") or "").strip()
        return out

    return out
