from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone as py_timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional, Tuple

import requests
from django.conf import settings
from django.utils import timezone

from b2b.models import Product, ProductVariant


@dataclass(frozen=True)
class NormalizedItem:
    product: Optional[Product]
    variant: Optional[ProductVariant]
    qty: int
    unit_price: Decimal
    name: str
    raw: dict


@dataclass(frozen=True)
class NormalizedOrder:
    external_id: str
    external_status: str
    external_created_at: Optional[datetime]
    payload: dict
    items: List[NormalizedItem]
    note: str


class WooOrdersClient:
    """WooCommerce orders reader/writer."""

    def __init__(self) -> None:
        root = (settings.WOO_BASE_URL or "").rstrip("/")
        api_root = getattr(settings, "WOO_API_ROOT", "/wp-json/wc/v3").strip("/")
        self.api = f"{root}/{api_root}"
        self.ck = settings.WOO_CONSUMER_KEY
        self.cs = settings.WOO_CONSUMER_SECRET

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Tuple[List[dict], Dict[str, str]]:
        if not self.api or not self.ck or not self.cs:
            raise RuntimeError("Woo credentials are not configured")

        url = f"{self.api}/{path.lstrip('/')}"  # noqa: S108
        params = params or {}
        params.update(
            {
                "consumer_key": self.ck,
                "consumer_secret": self.cs,
                "per_page": 100,
            }
        )
        r = requests.get(url, params=params, timeout=30)
        r.raise_for_status()
        return r.json(), dict(r.headers)


    def _post(self, path: str, data: Dict[str, Any]) -> dict:
        if not self.api or not self.ck or not self.cs:
            raise RuntimeError("Woo credentials are not configured")
        url = f"{self.api}/{path.lstrip('/')}"
        params = {"consumer_key": self.ck, "consumer_secret": self.cs}
        r = requests.post(url, json=data, params=params, timeout=30)
        r.raise_for_status()
        return r.json() or {}

    def _put(self, path: str, data: Dict[str, Any]) -> dict:
        if not self.api or not self.ck or not self.cs:
            raise RuntimeError("Woo credentials are not configured")
        url = f"{self.api}/{path.lstrip('/')}"
        params = {"consumer_key": self.ck, "consumer_secret": self.cs}
        r = requests.put(url, json=data, params=params, timeout=30)
        r.raise_for_status()
        return r.json() or {}

    def add_order_note(self, order_id: str | int, note: str, *, customer_note: bool = False) -> dict:
        return self._post(f"orders/{order_id}/notes", {"note": str(note), "customer_note": bool(customer_note)})

    def update_order(self, order_id: str | int, data: Dict[str, Any]) -> dict:
        return self._put(f"orders/{order_id}", data)

    def push_shipment(self, order_id: str | int, *, ttn: str, np_ref: str = "", status: str = "completed") -> dict:
        payload: Dict[str, Any] = {
            "status": status,
            "meta_data": [
                {"key": "mrkv_ua_ship_invoice_number", "value": str(ttn or "")},
                {"key": "mrkv_ua_ship_invoice_ref", "value": str(np_ref or "")},
                {"key": "shipping_ttn", "value": str(ttn or "")},
                {"key": "shipping_np_ref", "value": str(np_ref or "")},
            ],
        }
        data = self.update_order(order_id, payload)
        note_parts = [f"TTN synced from Herabuna B2B: {ttn}"]
        if np_ref:
            note_parts.append(f"NP ref: {np_ref}")
        try:
            self.add_order_note(order_id, "; ".join(note_parts), customer_note=False)
        except Exception:
            pass
        return data

    def fetch_orders(self, *, days: int = 14) -> List[dict]:
        """Fetch orders within last N days (best-effort)."""
        after_dt = timezone.now() - timedelta(days=int(days))
        after_iso = after_dt.isoformat()

        page = 1
        all_items: List[dict] = []
        while True:
            data, headers = self._get(
                "orders",
                params={
                    "after": after_iso,
                    "orderby": "modified",
                    "order": "desc",
                    "page": page,
                    "status": "any",
                },
            )
            if not data:
                break

            all_items.extend(data)
            total_pages = headers.get("X-WP-TotalPages")
            if total_pages and page >= int(total_pages):
                break
            page += 1

        return all_items


def _to_decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def _parse_woo_dt(value: Any) -> Optional[datetime]:
    if not value:
        return None
    s = str(value).strip()
    if not s:
        return None
    try:
        # Woo uses ISO 8601 strings; sometimes with Z suffix.
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None
    if timezone.is_naive(dt):
        dt = timezone.make_aware(dt, timezone=py_timezone.utc)
    return dt.astimezone(timezone.get_current_timezone())


def normalize_woo_order(order: dict) -> NormalizedOrder:
    external_id = str(order.get("id") or "").strip()
    external_status = str(order.get("status") or "").strip()

    created_at = _parse_woo_dt(order.get("date_created_gmt") or order.get("date_created"))

    billing = order.get("billing") or {}
    shipping = order.get("shipping") or {}

    customer = " ".join(
        [
            str(billing.get("first_name") or "").strip(),
            str(billing.get("last_name") or "").strip(),
        ]
    ).strip()
    phone = str(billing.get("phone") or "").strip()
    email = str(billing.get("email") or "").strip()

    note_parts = [f"Woo #{external_id}"]
    if customer:
        note_parts.append(customer)
    if phone:
        note_parts.append(f"тел: {phone}")
    if email:
        note_parts.append(email)
    note = " | ".join(note_parts)

    items: List[NormalizedItem] = []
    for li in (order.get("line_items") or []):
        qty = int(li.get("quantity") or 0)
        if qty <= 0:
            continue

        name = str(li.get("name") or "").strip()
        variation_id = int(li.get("variation_id") or 0)
        product_id = int(li.get("product_id") or 0)

        product: Optional[Product] = None
        variant: Optional[ProductVariant] = None

        if variation_id:
            variant = (
                ProductVariant.objects.select_related("product")
                .filter(woo_variation_id=variation_id)
                .first()
            )
            if variant:
                product = variant.product

        if not product and product_id:
            product = Product.objects.filter(woo_id=product_id).first()

        total = _to_decimal(li.get("total"))
        unit_price = (total / qty) if qty else _to_decimal(li.get("price"))

        items.append(
            NormalizedItem(
                product=product,
                variant=variant,
                qty=qty,
                unit_price=unit_price,
                name=name,
                raw=li,
            )
        )

    payload = {"raw": order, "billing": billing, "shipping": shipping}

    return NormalizedOrder(
        external_id=external_id,
        external_status=external_status,
        external_created_at=created_at,
        payload=payload,
        items=items,
        note=note,
    )
