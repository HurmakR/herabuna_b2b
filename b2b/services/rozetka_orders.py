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
    """Rozetka Seller API client (orders only).

    Notes:
    - Uses token cached in Django cache for ~23h.
    - Adds pagination guards to avoid infinite loops if API does not return meta.
    - Retries once on 401 by re-authenticating.
    """

    TOKEN_CACHE_KEY = "rozetka_access_token"

    def __init__(self) -> None:
        self.base = (settings.ROZETKA_API_URL or "https://api-seller.rozetka.com.ua").rstrip("/")
        self.username = (getattr(settings, "ROZETKA_USERNAME", "") or "").strip()
        self.password_b64 = (getattr(settings, "ROZETKA_PASSWORD_B64", "") or "").strip()

        # Use a session to reuse connections (faster) and to keep behavior consistent.
        self.session = requests.Session()

        # Conservative request timeout (connect, read).
        # Read timeout must be present to avoid “page spinning forever”.
        self.timeout = (10, 30)

        # Safety limits for manual sync requests (to keep web request responsive).
        self.max_pages = int(getattr(settings, "ROZETKA_SYNC_MAX_PAGES", 50) or 50)
        self.max_orders = int(getattr(settings, "ROZETKA_SYNC_MAX_ORDERS", 500) or 500)

    def _login(self) -> str:
        if not self.username or not self.password_b64:
            raise RuntimeError("Rozetka credentials are not configured")

        url = f"{self.base}/sites"
        try:
            r = self.session.post(
                url,
                json={"username": self.username, "password": self.password_b64},
                headers={"Content-Type": "application/json"},
                timeout=self.timeout,
            )
            r.raise_for_status()
            data = r.json() or {}
        except requests.RequestException as e:
            raise RuntimeError(f"Rozetka login request failed: {e}") from e
        except ValueError as e:
            raise RuntimeError("Rozetka login response is not JSON") from e

        if not data.get("success"):
            raise RuntimeError(f"Rozetka login failed: {data.get('errors')}")

        token = (data.get("content") or {}).get("access_token") or ""
        token = str(token).strip()
        if not token:
            raise RuntimeError("Rozetka login response has no access_token")

        # Token is valid for 24h; keep a bit less to be safe.
        cache.set(self.TOKEN_CACHE_KEY, token, timeout=23 * 60 * 60)
        return token

    def _token(self) -> str:
        tok = cache.get(self.TOKEN_CACHE_KEY)
        if tok:
            return str(tok)
        return self._login()

    def _request(self, method: str, path: str, *, params: Optional[Dict[str, Any]] = None, json_body: Optional[dict] = None) -> dict:
        url = f"{self.base}/{path.lstrip('/')}"  # noqa: S108
        headers = {"Authorization": f"Bearer {self._token()}"}

        def _do() -> requests.Response:
            return self.session.request(
                method,
                url,
                params=params or None,
                json=json_body,
                headers=headers,
                timeout=self.timeout,
            )

        try:
            r = _do()
            if r.status_code == 401:
                # Token expired/invalid -> relogin once
                cache.delete(self.TOKEN_CACHE_KEY)
                headers["Authorization"] = f"Bearer {self._token()}"
                r = _do()

            r.raise_for_status()
            return r.json() or {}
        except requests.RequestException as e:
            raise RuntimeError(f"Rozetka API request failed: {method} {path} ({e})") from e
        except ValueError as e:
            raise RuntimeError(f"Rozetka API response is not JSON: {method} {path}") from e

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> dict:
        return self._request("GET", path, params=params)

    def fetch_orders(self, *, days: int = 14, types: int = 1) -> List[dict]:
        """Fetch orders using /orders/search with changed_from window.

        Guards:
        - Hard cap on pages and total orders (settings.ROZETKA_SYNC_MAX_PAGES / MAX_ORDERS).
        - Detects pagination loop when API ignores 'page' and returns same page repeatedly.
        """
        start = (timezone.now() - timedelta(days=int(days))).date().isoformat()
        page = 1
        all_orders: List[dict] = []
        prev_first_id: str = ""

        while True:
            if page > self.max_pages:
                break
            if len(all_orders) >= self.max_orders:
                break

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

            # Loop detection: some APIs ignore 'page' and always return page 1.
            first_id = str((orders[0] or {}).get("id") or "").strip()
            if page > 1 and first_id and first_id == prev_first_id:
                break
            prev_first_id = first_id

            all_orders.extend(orders)

            meta = content.get("_meta") or {}
            total_pages = meta.get("totalPages") or meta.get("total_pages")
            current_page = meta.get("currentPage") or meta.get("current_page") or page
            if total_pages and int(current_page) >= int(total_pages):
                break

            page += 1

        return all_orders[: self.max_orders]

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
