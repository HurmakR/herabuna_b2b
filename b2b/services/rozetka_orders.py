from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any, Dict, List, Optional

import requests
from django.conf import settings
from django.core.cache import cache
from django.utils import timezone

from b2b.models import Product


@dataclass(frozen=True)
class NormalizedItem:
    product: Optional[Product]
    qty: int
    unit_price: Decimal
    name: str
    sku: str
    raw: dict


@dataclass(frozen=True)
class NormalizedOrder:
    external_id: str
    external_status: str
    external_created_at: Optional[datetime]
    payload: dict
    items: List[NormalizedItem]
    note: str
    status_group: Optional[int]


class RozetkaClient:
    """Rozetka Seller API client (orders only)."""

    TOKEN_CACHE_KEY = "rozetka_access_token"

    def __init__(self) -> None:
        self.base = (settings.ROZETKA_API_URL or "https://api-seller.rozetka.com.ua").rstrip("/")
        self.username = (getattr(settings, "ROZETKA_USERNAME", "") or "").strip()
        self.password_b64 = (getattr(settings, "ROZETKA_PASSWORD_B64", "") or "").strip()

    def _login(self) -> str:
        if not self.username or not self.password_b64:
            raise RuntimeError("Rozetka credentials are not configured")

        url = f"{self.base}/sites"
        r = requests.post(
            url,
            json={"username": self.username, "password": self.password_b64},
            headers={"Content-Type": "application/json"},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json() or {}
        if not data.get("success"):
            raise RuntimeError(f"Rozetka login failed: {data.get('errors')}")
        token = (data.get("content") or {}).get("access_token") or ""
        token = str(token).strip()
        if not token:
            raise RuntimeError("Rozetka login response has no access_token")
        cache.set(self.TOKEN_CACHE_KEY, token, timeout=23 * 60 * 60)
        return token

    def _token(self) -> str:
        tok = cache.get(self.TOKEN_CACHE_KEY)
        if tok:
            return str(tok)
        return self._login()

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> dict:
        url = f"{self.base}/{path.lstrip('/')}"  # noqa: S108
        params = params or {}
        r = requests.get(
            url,
            params=params,
            headers={"Authorization": f"Bearer {self._token()}"},
            timeout=30,
        )
        r.raise_for_status()
        return r.json() or {}

    def fetch_orders(self, *, days: int = 14, types: int = 1) -> List[dict]:
        """Fetch orders using /orders/search with changed_from window."""
        start = (timezone.now() - timedelta(days=int(days))).date().isoformat()
        page = 1
        all_orders: List[dict] = []

        while True:
            data = self._get(
                "orders/search",
                params={
                    "page": page,
                    "types": types,
                    "changed_from": start,
                    "sort": "-changed",
                    "expand": "purchases,user,delivery",
                },
            )
            if not data.get("success"):
                raise RuntimeError(f"Rozetka orders/search failed: {data.get('errors')}")
            content = data.get("content") or {}
            orders = content.get("orders") or []
            if not orders:
                break

            all_orders.extend(orders)

            meta = content.get("_meta") or {}
            total_pages = meta.get("totalPages") or meta.get("total_pages")
            current_page = meta.get("currentPage") or meta.get("current_page") or page
            if total_pages and int(current_page) >= int(total_pages):
                break

            page += 1

        return all_orders


def _to_decimal(value: Any) -> Decimal:
    try:
        return Decimal(str(value or "0"))
    except (InvalidOperation, ValueError, TypeError):
        return Decimal("0")


def normalize_rozetka_order(order: dict) -> NormalizedOrder:
    external_id = str(order.get("id") or "").strip()
    status = order.get("status")
    status_group = order.get("status_group")
    external_status = str(status) if status is not None else ""

    created_raw = order.get("created")
    created_at: Optional[datetime] = None
    if created_raw:
        try:
            # Example format: "YYYY-MM-DD HH:MM:SS"
            created_at = datetime.strptime(str(created_raw), "%Y-%m-%d %H:%M:%S")
            created_at = timezone.make_aware(created_at, timezone=timezone.get_current_timezone())
        except Exception:
            created_at = None

    user = order.get("user") or {}
    delivery = order.get("delivery") or {}
    name = str(user.get("fio") or user.get("name") or "").strip()
    phone = str(order.get("user_phone") or user.get("phone") or "").strip()

    note_parts = [f"Rozetka #{external_id}"]
    if name:
        note_parts.append(name)
    if phone:
        note_parts.append(f"тел: {phone}")
    note = " | ".join(note_parts)

    items: List[NormalizedItem] = []
    for p in (order.get("purchases") or []):
        qty = int(p.get("quantity") or 0)
        if qty <= 0:
            continue

        unit_price = _to_decimal(p.get("price_with_discount") or p.get("price"))
        item_name = str(p.get("item_name") or "").strip()

        item_obj = p.get("item") or {}
        sku = str(item_obj.get("article") or item_obj.get("price_offer_id") or "").strip()

        product = Product.objects.filter(sku=sku).first() if sku else None

        items.append(
            NormalizedItem(
                product=product,
                qty=qty,
                unit_price=unit_price,
                name=item_name,
                sku=sku,
                raw=p,
            )
        )

    payload = {"raw": order, "user": user, "delivery": delivery}

    return NormalizedOrder(
        external_id=external_id,
        external_status=external_status,
        external_created_at=created_at,
        payload=payload,
        items=items,
        note=note,
        status_group=int(status_group) if status_group is not None else None,
    )
