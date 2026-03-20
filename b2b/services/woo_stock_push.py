"""B2B -> WooCommerce stock sync.

This module pushes local availability (stock_qty) to Woo.

Intended usage:
- Service screen identifies Woo products that are still "in stock" on Woo while
  missing/zero-stock in B2B.
- Admin can bulk mark those Woo products as out-of-stock.

Notes:
- We only update Woo product stock flags. Local warehouse/lots are never changed here.
- For variable products, parent stock status may be derived from variations; we update
  parent stock_status but it may not fully affect storefront. Such items are marked
  and can be handled manually if needed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

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


class WooStockClient:
    def __init__(self) -> None:
        root = (settings.WOO_BASE_URL or "").rstrip("/")
        api_root = getattr(settings, "WOO_API_ROOT", "/wp-json/wc/v3").strip("/")
        self.api = f"{root}/{api_root}"
        self.ck = getattr(settings, "WOO_CONSUMER_KEY", "")
        self.cs = getattr(settings, "WOO_CONSUMER_SECRET", "")
        # Auth mode:
        # - "auto" (default): try Basic Auth first, then query-string auth.
        # - "basic": only Basic Auth.
        # - "query": only query-string auth.
        self.auth_mode = str(getattr(settings, "WOO_AUTH_MODE", "auto") or "auto").strip().lower()
        self.session = requests.Session()

    def _check(self) -> None:
        if not self.api or not self.ck or not self.cs:
            raise RuntimeError("Woo credentials are not configured")

    @staticmethod
    def _safe_error(resp: requests.Response) -> str:
        """Return a redacted, human-readable error string without leaking credentials."""
        code = ""
        message = ""
        try:
            payload = resp.json() or {}
            code = str(payload.get("code") or "").strip()
            message = str(payload.get("message") or "").strip()
            if not message:
                message = str(payload.get("data") or "").strip()
        except Exception:
            message = (resp.text or "").strip()

        message = message.replace("\n", " ").strip()
        if len(message) > 180:
            message = message[:180] + "…"

        if code:
            return f"HTTP {resp.status_code} ({code}): {message}" if message else f"HTTP {resp.status_code} ({code})"
        return f"HTTP {resp.status_code}: {message}" if message else f"HTTP {resp.status_code}"

    def _request(self, method: str, path: str, *, params: Optional[Dict[str, Any]] = None, json_body: Any = None) -> requests.Response:
        """Perform a Woo request with safe auth handling.

        Important:
        - Avoid leaking credentials in URLs and error messages.
        - Try Basic Auth first (more secure), and fall back to query-string auth.
        """
        self._check()
        url = f"{self.api}/{path.lstrip('/')}"
        base_params = params or {}

        def _do(mode: str) -> requests.Response:
            if mode == "query":
                p = dict(base_params)
                p.update({"consumer_key": self.ck, "consumer_secret": self.cs})
                return self.session.request(method, url, params=p, json=json_body, timeout=30)
            return self.session.request(method, url, params=base_params, json=json_body, auth=(self.ck, self.cs), timeout=30)

        if self.auth_mode == "basic":
            modes = ["basic"]
        elif self.auth_mode == "query":
            modes = ["query"]
        else:
            modes = ["basic", "query"]

        last: Optional[requests.Response] = None
        for m in modes:
            r = _do(m)
            last = r
            if 200 <= r.status_code < 300:
                return r
            if r.status_code in {401, 403}:
                # Try next auth mode
                continue
            break

        assert last is not None
        raise RuntimeError(self._safe_error(last))

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
            try:
                # Read current first to know manage_stock (avoid forcing it on).
                r = self._request("GET", f"products/{int(wid)}")
                p = r.json() or {}
                manage_stock = bool(p.get("manage_stock"))
                payload: Dict[str, Any] = {"stock_status": "outofstock"}
                if manage_stock:
                    payload["stock_quantity"] = 0
                # Also disable backorders if present (optional, but helps prevent instock UI).
                if p.get("backorders") and p.get("backorders") != "no":
                    payload["backorders"] = "no"

                self._request("PUT", f"products/{int(wid)}", json_body=payload)
                updated += 1
            except Exception as e:
                errors.append(f"{wid}: {str(e)}")
        return WooStockSyncResult(updated=updated, skipped=skipped, errors=errors)


def _woo_in_stock(p: dict) -> bool:
    status = (p.get("stock_status") or "").strip().lower()
    if status in {"instock", "onbackorder"}:
        return True
    # Sometimes stock_status is not reliable; fall back to numeric stock_quantity.
    qty = p.get("stock_quantity")
    try:
        return int(qty or 0) > 0
    except Exception:
        return False


def list_woo_instock_but_missing_locally() -> List[WooStockMismatch]:
    """Return Woo products that are in-stock on Woo but missing or zero-stock locally."""
    from b2b.models import Product

    client = WooStockClient()
    woo_products = client.fetch_products_all(status="publish")

    # Local index by SKU
    local_by_sku = {p.sku: p for p in Product.objects.all().only("id", "sku", "name", "stock_qty", "is_active")}
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

        # mismatch criteria:
        # - local product missing OR stock_qty <= 0
        if local is None or (local_stock is not None and local_stock <= 0):
            images = wp.get("images") or []
            img = ""
            if images:
                img = (images[0] or {}).get("src") or ""
            try:
                qty = wp.get("stock_quantity")
                woo_qty = int(qty) if qty is not None else None
            except Exception:
                woo_qty = None

            out.append(
                WooStockMismatch(
                    woo_id=int(wp.get("id") or 0),
                    sku=sku,
                    name=(wp.get("name") or "").strip(),
                    product_type=(wp.get("type") or "").strip(),
                    image_url=img,
                    woo_stock_status=(wp.get("stock_status") or "").strip(),
                    woo_stock_qty=woo_qty,
                    woo_manage_stock=bool(wp.get("manage_stock")),
                    local_stock_qty=local_stock,
                    local_is_active=local_active,
                )
            )

    # Sort: missing first, then by sku
    out.sort(key=lambda x: (0 if x.local_stock_qty is None else 1, x.sku))
    return out
