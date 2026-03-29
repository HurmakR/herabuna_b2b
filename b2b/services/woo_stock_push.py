"""
B2B <-> WooCommerce stock sync.

Service screen supports two mismatch lists:
1) Woo in-stock but local product is missing/zero-stock -> mark outofstock on Woo.
2) Local in-stock but Woo is outofstock -> mark instock on Woo.

Notes:
- Only Woo product flags are updated here. Local warehouse/lots are never changed.
- Matching is done by SKU (Woo product SKU <-> local Product.sku).
- Variable products: parent stock_status may not fully control variations. We still
  update the parent and mark such items for manual review if needed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional

import requests
from django.conf import settings


@dataclass(frozen=True)
class WooStockMismatch:
    woo_id: int
    sku: str
    name: str
    product_type: str
    image_url: str
    woo_stock_status: str
    woo_stock_qty: Optional[int]
    woo_manage_stock: bool
    local_stock_qty: Optional[int]  # None when product is missing locally
    local_is_active: Optional[bool]


@dataclass(frozen=True)
class WooStockSyncResult:
    updated: int
    skipped: int
    errors: List[str]


def _safe_http_error(e: Exception) -> str:
    """
    Avoid leaking consumer_key/consumer_secret in error messages (requests can include full URL).
    """
    if isinstance(e, requests.HTTPError) and getattr(e, "response", None) is not None:
        r = e.response
        return f"HTTP {r.status_code} {getattr(r, 'reason', '')}".strip()
    return e.__class__.__name__


class WooStockClient:
    def __init__(self) -> None:
        root = (settings.WOO_BASE_URL or "").rstrip("/")
        api_root = getattr(settings, "WOO_API_ROOT", "/wp-json/wc/v3").strip("/")
        self.api = f"{root}/{api_root}"
        self.ck = getattr(settings, "WOO_CONSUMER_KEY", "")
        self.cs = getattr(settings, "WOO_CONSUMER_SECRET", "")

    def _check(self) -> None:
        if not self.api or not self.ck or not self.cs:
            raise RuntimeError("Woo credentials are not configured")

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Any = None,
    ) -> requests.Response:
        self._check()
        url = f"{self.api}/{path.lstrip('/')}"
        params = params or {}
        # Keep query auth for compatibility with your current setup.
        # IMPORTANT: do NOT surface request URLs in user-visible errors.
        params.update({"consumer_key": self.ck, "consumer_secret": self.cs})
        r = requests.request(method, url, params=params, json=json_body, timeout=30)
        r.raise_for_status()
        return r

    def fetch_products_all(self, *, status: str = "publish") -> List[dict]:
        """Fetch all Woo products with pagination."""
        self._check()
        page = 1
        all_items: List[dict] = []
        while True:
            r = self._request("GET", "products", params={"status": status, "page": page, "per_page": 100})
            data = r.json() or []
            if not data:
                break
            all_items.extend(data)

            total_pages = r.headers.get("X-WP-TotalPages")
            if total_pages and page >= int(total_pages):
                break
            page += 1
        return all_items

    def mark_out_of_stock(self, *, woo_ids: List[int]) -> WooStockSyncResult:
        """Mark Woo products as out-of-stock (best-effort)."""
        updated = 0
        skipped = 0
        errors: List[str] = []

        for wid in woo_ids:
            wid = int(wid)
            if wid <= 0:
                skipped += 1
                continue
            try:
                # Read current first to know manage_stock (avoid forcing it on).
                r = self._request("GET", f"products/{wid}")
                p = r.json() or {}
                manage_stock = bool(p.get("manage_stock"))

                payload: Dict[str, Any] = {"stock_status": "outofstock"}
                if manage_stock:
                    payload["stock_quantity"] = 0

                # Disable backorders to avoid storefront showing as available.
                if p.get("backorders") and p.get("backorders") != "no":
                    payload["backorders"] = "no"

                self._request("PUT", f"products/{wid}", json_body=payload)
                updated += 1
            except Exception as e:
                errors.append(f"{wid}: {_safe_http_error(e)}")

        return WooStockSyncResult(updated=updated, skipped=skipped, errors=errors)

    def mark_in_stock(self, *, woo_ids: List[int]) -> WooStockSyncResult:
        """
        Mark Woo products as in-stock.
        If Woo product has manage_stock enabled and SKU is mapped locally, we also set stock_quantity.
        """
        from b2b.models import Product

        updated = 0
        skipped = 0
        errors: List[str] = []

        local_by_sku = {p.sku: p for p in Product.objects.all().only("sku", "stock_qty", "is_active")}

        for wid in woo_ids:
            wid = int(wid)
            if wid <= 0:
                skipped += 1
                continue
            try:
                r = self._request("GET", f"products/{wid}")
                p = r.json() or {}
                sku = (p.get("sku") or "").strip()
                manage_stock = bool(p.get("manage_stock"))

                payload: Dict[str, Any] = {"stock_status": "instock"}

                if manage_stock and sku and sku in local_by_sku:
                    local_qty = int(getattr(local_by_sku[sku], "stock_qty", 0) or 0)
                    payload["stock_quantity"] = max(local_qty, 1)

                # Keep backorders off by default.
                if p.get("backorders") and p.get("backorders") != "no":
                    payload["backorders"] = "no"

                self._request("PUT", f"products/{wid}", json_body=payload)
                updated += 1
            except Exception as e:
                errors.append(f"{wid}: {_safe_http_error(e)}")

        return WooStockSyncResult(updated=updated, skipped=skipped, errors=errors)


def _woo_in_stock(p: dict) -> bool:
    status = (p.get("stock_status") or "").strip().lower()
    if status in {"instock", "onbackorder"}:
        return True
    qty = p.get("stock_quantity")
    try:
        return int(qty or 0) > 0
    except Exception:
        return False


def _woo_out_of_stock(p: dict) -> bool:
    return not _woo_in_stock(p)


def _first_image_url(wp: dict) -> str:
    images = wp.get("images") or []
    if images:
        return (images[0] or {}).get("src") or ""
    return ""


def _woo_qty(wp: dict) -> Optional[int]:
    try:
        qty = wp.get("stock_quantity")
        return int(qty) if qty is not None else None
    except Exception:
        return None


def list_woo_instock_but_missing_locally() -> List[WooStockMismatch]:
    """Woo in-stock but local product is missing or stock_qty <= 0."""
    from b2b.models import Product

    client = WooStockClient()
    woo_products = client.fetch_products_all(status="publish")

    local_by_sku = {p.sku: p for p in Product.objects.all().only("sku", "name", "stock_qty", "is_active")}
    out: List[WooStockMismatch] = []

    for wp in woo_products:
        sku = (wp.get("sku") or "").strip()
        if not sku:
            continue
        if not _woo_in_stock(wp):
            continue

        local = local_by_sku.get(sku)
        local_stock = int(getattr(local, "stock_qty", 0)) if local else None
        local_active = bool(getattr(local, "is_active", True)) if local else None

        if local is None or (local_stock is not None and local_stock <= 0):
            out.append(
                WooStockMismatch(
                    woo_id=int(wp.get("id") or 0),
                    sku=sku,
                    name=(wp.get("name") or "").strip(),
                    product_type=(wp.get("type") or "").strip(),
                    image_url=_first_image_url(wp),
                    woo_stock_status=(wp.get("stock_status") or "").strip(),
                    woo_stock_qty=_woo_qty(wp),
                    woo_manage_stock=bool(wp.get("manage_stock")),
                    local_stock_qty=local_stock,
                    local_is_active=local_active,
                )
            )

    out.sort(key=lambda x: (0 if x.local_stock_qty is None else 1, x.sku))
    return out


def list_local_instock_but_woo_outofstock(*, include_missing_on_woo: bool = True) -> List[WooStockMismatch]:
    """Local stock_qty > 0 but Woo is outofstock (or missing on Woo)."""
    from b2b.models import Product

    client = WooStockClient()
    woo_products = client.fetch_products_all(status="publish")
    woo_by_sku: Dict[str, dict] = {}
    for wp in woo_products:
        sku = (wp.get("sku") or "").strip()
        if sku:
            woo_by_sku[sku] = wp

    out: List[WooStockMismatch] = []
    locals_qs = Product.objects.filter(is_active=True, stock_qty__gt=0).only("sku", "name", "stock_qty", "is_active")
    for lp in locals_qs:
        sku = (lp.sku or "").strip()
        if not sku:
            continue

        wp = woo_by_sku.get(sku)
        if wp is None:
            if not include_missing_on_woo:
                continue
            out.append(
                WooStockMismatch(
                    woo_id=0,
                    sku=sku,
                    name=(lp.name or "").strip(),
                    product_type="missing",
                    image_url="",
                    woo_stock_status="missing",
                    woo_stock_qty=None,
                    woo_manage_stock=False,
                    local_stock_qty=int(lp.stock_qty or 0),
                    local_is_active=bool(lp.is_active),
                )
            )
            continue

        if _woo_out_of_stock(wp):
            out.append(
                WooStockMismatch(
                    woo_id=int(wp.get("id") or 0),
                    sku=sku,
                    name=(wp.get("name") or lp.name or "").strip(),
                    product_type=(wp.get("type") or "").strip(),
                    image_url=_first_image_url(wp),
                    woo_stock_status=(wp.get("stock_status") or "").strip(),
                    woo_stock_qty=_woo_qty(wp),
                    woo_manage_stock=bool(wp.get("manage_stock")),
                    local_stock_qty=int(lp.stock_qty or 0),
                    local_is_active=bool(lp.is_active),
                )
            )

    out.sort(key=lambda x: (0 if x.woo_id <= 0 else 1, x.sku))
    return out