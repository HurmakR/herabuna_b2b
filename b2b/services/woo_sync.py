"""WooCommerce -> B2B catalog sync helpers.

Rules:
- Woo is *catalog only*. It must never change local stock, lots, or prices.
- We import ONLY products missing in the local catalog.
- Identification: prefer woo_id, but SKU is the canonical unique key locally.
- If a product exists locally by SKU and has no woo_id, we only link woo_id.
- Local products are never deactivated if absent in Woo.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests
from django.conf import settings
from django.db import transaction

from b2b.models import Brand, Category, Product, ProductCategory, ProductVariant


@dataclass
class WooMissingItem:
    woo_id: int
    sku: str
    name: str
    status: str
    image_url: str


@dataclass
class WooImportResult:
    created: int
    linked_by_sku: int
    skipped_existing: int
    categories_created: int
    brands_created: int


class WooClient:
    """Read-only WooCommerce REST client."""

    def __init__(self) -> None:
        root = settings.WOO_BASE_URL.rstrip("/")
        api_root = getattr(settings, "WOO_API_ROOT", "/wp-json/wc/v3").strip("/")
        self.api = f"{root}/{api_root}"
        self.ck = settings.WOO_CONSUMER_KEY
        self.cs = settings.WOO_CONSUMER_SECRET

    def _get(self, path: str, params: Optional[Dict[str, Any]] = None) -> Tuple[List[dict], Dict[str, str]]:
        url = f"{self.api}/{path.lstrip('/')}"
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

    def fetch_variations(self, product_id: int) -> List[dict]:
        """Fetch all variations for a variable product."""
        page = 1
        all_items: List[dict] = []
        while True:
            data, headers = self._get(
                f"products/{product_id}/variations",
                params={"page": page, "per_page": 100, "status": "publish"},
            )
            if not data:
                break
            all_items.extend(data)
            total_pages = headers.get("X-WP-TotalPages")
            if total_pages and page >= int(total_pages):
                break
            page += 1
        return all_items

    def fetch_products_all(self, *, status: str = "publish") -> List[dict]:
        """Fetch all products with pagination."""
        page = 1
        all_items: List[dict] = []

        while True:
            data, headers = self._get("products", params={"status": status, "page": page})
            if not data:
                break
            all_items.extend(data)

            total_pages = headers.get("X-WP-TotalPages")
            if total_pages and page >= int(total_pages):
                break
            page += 1

        return all_items


def _safe_str(value: Any) -> str:
    return (value or "").strip()


def _parse_weight_g(wc_product: dict) -> int:
    """Woo 'weight' is usually a string in kg. Convert to grams."""
    raw = wc_product.get("weight")
    if raw is None:
        return 0
    s = str(raw).strip().replace(",", ".")
    if not s:
        return 0
    try:
        kg = float(s)
        if kg <= 0:
            return 0
        if kg <= 500:
            return int(round(kg))
        return int(round(kg * 1000))
    except ValueError:
        return 0


def _extract_images(wc_product: dict) -> Tuple[str, List[str]]:
    images = wc_product.get("images") or []
    urls = [img.get("src") for img in images if img.get("src")]
    if not urls:
        return "", []
    return urls[0], urls[1:]


def _extract_attributes(wc_product: dict) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    attrs = wc_product.get("attributes") or []
    for a in attrs:
        name = _safe_str(a.get("name"))
        if not name:
            continue
        options = a.get("options") or []
        out[name] = options
    return out


def _brand_from_woo(wc_product: dict) -> Optional[Tuple[Optional[int], str, str]]:
    """Best-effort brand detection."""
    brands = wc_product.get("brands")
    if isinstance(brands, list) and brands:
        b = brands[0] or {}
        name = _safe_str(b.get("name"))
        slug = _safe_str(b.get("slug"))
        woo_id = b.get("id")
        if name:
            return (int(woo_id) if woo_id is not None else None, name, slug)

    attrs = wc_product.get("attributes") or []
    for a in attrs:
        n = _safe_str(a.get("name")).lower()
        if n in {"brand", "бренд"}:
            options = a.get("options") or []
            if options:
                name = _safe_str(options[0])
                if name:
                    slug = re.sub(r"[^a-z0-9\-]+", "-", name.lower()).strip("-")
                    return (None, name, slug)

    return None


def _get_or_create_category(wc_cat: dict) -> Tuple[Optional[Category], bool]:
    woo_id = wc_cat.get("id")
    if woo_id is None:
        return None, False

    woo_id_int = int(woo_id)
    obj = Category.objects.filter(woo_id=woo_id_int).first()
    if obj:
        return obj, False

    name = _safe_str(wc_cat.get("name"))
    slug = _safe_str(wc_cat.get("slug"))

    obj = Category.objects.create(
        woo_id=woo_id_int,
        name=name or f"Woo Category {woo_id_int}",
        slug=slug,
        is_active=True,
    )
    return obj, True


def _get_or_create_brand(brand_tuple: Tuple[Optional[int], str, str]) -> Tuple[Optional[Brand], bool]:
    woo_id, name, slug = brand_tuple
    if woo_id is not None:
        obj = Brand.objects.filter(woo_id=woo_id).first()
        if obj:
            return obj, False

    obj = Brand.objects.filter(name=name).first()
    if obj:
        if woo_id is not None and obj.woo_id is None:
            obj.woo_id = woo_id
            if slug and not obj.slug:
                obj.slug = slug
            obj.save(update_fields=["woo_id", "slug"])
        return obj, False

    obj = Brand.objects.create(name=name, slug=slug, woo_id=woo_id)
    return obj, True


def list_missing_products_from_woo(*, status: str = "publish") -> List[WooMissingItem]:
    """Return Woo products that are missing locally by SKU."""
    client = WooClient()
    wc_products = client.fetch_products_all(status=status)
    existing_skus = set(Product.objects.values_list("sku", flat=True))

    missing: List[WooMissingItem] = []
    for wp in wc_products:
        sku = _safe_str(wp.get("sku"))
        if not sku:
            continue
        if sku in existing_skus:
            continue
        woo_id = wp.get("id")
        if woo_id is None:
            continue

        img, _ = _extract_images(wp)
        missing.append(
            WooMissingItem(
                woo_id=int(woo_id),
                sku=sku,
                name=_safe_str(wp.get("name")) or sku,
                status=_safe_str(wp.get("status")),
                image_url=img,
            )
        )

    missing.sort(key=lambda x: x.sku)
    return missing


def _parse_variation_attributes(wc_variation: dict) -> dict:
    """Extract {name: value} dict from variation attributes."""
    attrs = {}
    for a in (wc_variation.get("attributes") or []):
        name = (a.get("name") or "").strip()
        val  = (a.get("option") or "").strip()
        if name and val:
            attrs[name] = val
    return attrs


def _import_variations(client: WooClient, product: Product, woo_product_id: int) -> int:
    """Fetch and upsert all variations for a variable product.

    Returns count of created/updated variants.
    """
    variations = client.fetch_variations(woo_product_id)
    count = 0

    for wv in variations:
        woo_var_id = wv.get("id")
        if woo_var_id is None:
            continue
        woo_var_id = int(woo_var_id)

        sku        = (wv.get("sku") or "").strip()
        attributes = _parse_variation_attributes(wv)
        stock_qty  = int(wv.get("stock_quantity") or 0)
        is_active  = wv.get("status") == "publish"

        # Prices
        retail_price    = 0
        wholesale_price = 0
        try:
            retail_price = float(wv.get("regular_price") or wv.get("price") or 0)
        except (ValueError, TypeError):
            pass

        # Weight
        weight_g = _parse_weight_g(wv)

        # Image
        img_data = wv.get("image") or {}
        image_url = (img_data.get("src") or "").strip()

        obj, created = ProductVariant.objects.update_or_create(
            woo_variation_id=woo_var_id,
            defaults={
                "product":         product,
                "sku":             sku,
                "attributes":      attributes,
                "retail_price":    retail_price,
                "wholesale_price": wholesale_price,
                "stock_qty":       stock_qty,
                "is_active":       is_active,
                "image_url":       image_url,
                "weight_g":        weight_g,
            },
        )
        count += 1

    return count


def sync_variations_for_product(product: Product) -> int:
    """Public helper: re-sync variations for an already-imported product.

    Call from shell or service page to refresh variants without reimporting
    the whole product.
    """
    client = WooClient()
    if not product.woo_id:
        raise ValueError(f"Product {product.sku} has no woo_id")
    return _import_variations(client, product, product.woo_id)



@transaction.atomic
def import_missing_products_from_woo(*, woo_ids: Iterable[int], status: str = "publish") -> WooImportResult:
    """Import selected Woo products into catalog (only missing/new)."""
    selected_ids = {int(x) for x in woo_ids}
    if not selected_ids:
        return WooImportResult(0, 0, 0, 0, 0)

    client = WooClient()
    wc_products = client.fetch_products_all(status=status)
    wc_by_id = {int(p.get("id")): p for p in wc_products if p.get("id") is not None}

    existing_woo_ids = set(Product.objects.exclude(woo_id__isnull=True).values_list("woo_id", flat=True))
    existing_skus = set(Product.objects.values_list("sku", flat=True))

    created = 0
    linked_by_sku = 0
    skipped_existing = 0
    categories_created = 0
    brands_created = 0

    for woo_id in sorted(selected_ids):
        wp = wc_by_id.get(woo_id)
        if not wp:
            continue

        if woo_id in existing_woo_ids:
            skipped_existing += 1
            continue

        sku = _safe_str(wp.get("sku"))
        if not sku:
            continue

        if sku in existing_skus:
            p = Product.objects.filter(sku=sku).first()
            if p and p.woo_id is None:
                p.woo_id = woo_id
                p.save(update_fields=["woo_id"])
                linked_by_sku += 1
                existing_woo_ids.add(woo_id)
            else:
                skipped_existing += 1
            continue

        name = _safe_str(wp.get("name")) or sku
        short_description = wp.get("short_description") or ""
        description = wp.get("description") or ""
        weight_g = _parse_weight_g(wp)
        main_image_url, gallery = _extract_images(wp)
        attributes = _extract_attributes(wp)

        p = Product.objects.create(
            sku=sku,
            name=name,
            woo_id=woo_id,
            is_active=True,
            wholesale_price=0,
            cost_price=0,
            retail_price=0,
            stock_qty=0,
            short_description=short_description,
            description=description,
            weight_g=weight_g,
            main_image_url=main_image_url,
            gallery=gallery,
            attributes=attributes,
        )

        for wc_cat in (wp.get("categories") or []):
            cat, was_created = _get_or_create_category(wc_cat)
            if cat:
                ProductCategory.objects.get_or_create(product=p, category=cat)
            if was_created:
                categories_created += 1

        brand_tuple = _brand_from_woo(wp)
        if brand_tuple:
            brand_obj, was_created = _get_or_create_brand(brand_tuple)
            if brand_obj:
                p.brand = brand_obj
                p.save(update_fields=["brand"])
            if was_created:
                brands_created += 1

        # Import variations if this is a variable product
        if wp.get("type") == "variable":
            try:
                _import_variations(client, p, woo_id)
            except Exception as e:
                import logging
                logging.getLogger(__name__).warning(
                    "Failed to import variations for %s: %s", sku, e
                )

        created += 1
        existing_skus.add(sku)
        existing_woo_ids.add(woo_id)

    return WooImportResult(
        created=created,
        linked_by_sku=linked_by_sku,
        skipped_existing=skipped_existing,
        categories_created=categories_created,
        brands_created=brands_created,
    )
