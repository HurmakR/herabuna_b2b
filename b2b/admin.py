from django.contrib import admin, messages
from django.urls import path
from django.shortcuts import redirect
import decimal

from .models import (
    Dealer,
    Brand,
    Category,
    Facet,
    Product,
    ProductImage,
    ProductCategory,
    ProductVariant,
    Order,
    OrderItem,
)
from .services.woo_sync import WooClient


@admin.register(Dealer)
class DealerAdmin(admin.ModelAdmin):
    """Dealer admin configuration."""
    list_display = ("username", "company_name", "email", "is_active", "is_staff")
    search_fields = ("username", "company_name", "email")


@admin.register(Brand)
class BrandAdmin(admin.ModelAdmin):
    """Brand admin configuration."""
    list_display = ("name", "slug", "woo_id")
    search_fields = ("name", "slug")


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    """Category admin configuration."""
    list_display = ("name", "slug", "woo_id", "is_active", "parent")
    search_fields = ("name", "slug")
    list_filter = ("is_active",)


class ProductImageInline(admin.TabularInline):
    """Inline editor for per-product images (admin UX)."""
    model = ProductImage
    extra = 0


def _facet_type_from_attr_name(attr_name: str):
    """Heuristic mapping of Woo attribute names to facet types (not order options)."""
    n = (attr_name or "").strip().lower()
    if "ingredient" in n or "інгреді" in n:
        return "ingredient"
    if "effective" in n or "ефектив" in n:
        return "effect"
    if "season" in n or "сезон" in n:
        return "season"
    return None


def sync_with_woo(modeladmin, request, queryset):
    """
    Pull products from Woo:
    - core fields (name, price, status, weight)
    - categories
    - brand (from top-level `brands`)
    - informational attributes as JSON
    - images (main + gallery + inline table)
    - variants (attrs, price, stock, status, image, weight)
    """
    client = WooClient()
    woo_products = client.fetch_products()
    pulled = 0

    for wp in woo_products:
        sku = (wp.get("sku") or "").strip()
        if not sku:
            continue

        p, _ = Product.objects.get_or_create(
            sku=sku,
            defaults={
                "name": wp.get("name") or sku,
                "retail_price": decimal.Decimal(str(wp.get("price") or 0)),
                "stock_qty": wp.get("stock_quantity") or 0,
                "woo_id": wp.get("id"),
                "is_active": (wp.get("status") == "publish"),
                "short_description": wp.get("short_description") or "",
                "description": wp.get("description") or "",
            },
        )

        # --- core fields ---
        p.name = wp.get("name") or p.name
        p.retail_price = decimal.Decimal(str(wp.get("price") or p.retail_price or 0))
        p.is_active = (wp.get("status") == "publish")
        p.woo_id = wp.get("id")

        # weight @ product level (store grams; Woo weight is a string)
        weight_str = (wp.get("weight") or "").strip()
        try:
            w = float(weight_str) if weight_str else 0
            p.weight_g = int(w) if w <= 10000 else int(w * 1000)  # best-effort grams
        except Exception:
            p.weight_g = 0

        # media
        images = wp.get("images") or []
        p.main_image_url = images[0]["src"] if images else ""
        p.gallery = [img["src"] for img in images]

        # informational attributes (non-order options)
        info_attrs = {}
        for a in (wp.get("attributes") or []):
            name = a.get("name") or ""
            options = a.get("options") or []
            if name:
                info_attrs[name] = options
        p.attributes = info_attrs

        p.save()

        # --- categories ---
        for c in (wp.get("categories") or []):
            cat, _ = Category.objects.get_or_create(
                woo_id=c.get("id"),
                defaults={"name": c.get("name") or "", "slug": c.get("slug") or ""},
            )
            changed = False
            if c.get("name") and cat.name != c["name"]:
                cat.name = c["name"]; changed = True
            if c.get("slug") and cat.slug != c["slug"]:
                cat.slug = c["slug"]; changed = True
            if changed:
                cat.save()
            ProductCategory.objects.get_or_create(product=p, category=cat)

        # --- brand from top-level `brands` ---
        # Example: 'brands': [{'id': 66, 'name': 'Huashi', 'slug': 'huashi'}]
        brands_payload = wp.get("brands") or []
        brand_obj = None
        if isinstance(brands_payload, list) and brands_payload:
            b = brands_payload[0]  # if multiple brands are assigned, use the first
            bid = b.get("id")
            bname = (b.get("name") or "").strip()
            bslug = (b.get("slug") or "").strip()

            if bid:
                brand_obj, _created = Brand.objects.get_or_create(
                    woo_id=bid,
                    defaults={"name": bname or "Brand", "slug": bslug},
                )
                changed = False
                if bname and brand_obj.name != bname:
                    brand_obj.name = bname; changed = True
                if bslug and brand_obj.slug != bslug:
                    brand_obj.slug = bslug; changed = True
                if changed:
                    brand_obj.save(update_fields=["name", "slug"])
            elif bname:
                brand_obj, _ = Brand.objects.get_or_create(
                    name=bname,
                    defaults={"slug": bslug},
                )

        if brand_obj and p.brand_id != brand_obj.id:
            p.brand = brand_obj
            p.save(update_fields=["brand"])

        # --- descriptive facets from attributes (ingredient/effect/season) ---
        for a in (wp.get("attributes") or []):
            ftype = _facet_type_from_attr_name(a.get("name"))
            if not ftype:
                continue
            for opt in (a.get("options") or []):
                facet, _ = Facet.objects.get_or_create(type=ftype, name=opt)
                p.facets.add(facet)

        # --- rebuild ProductImage table for admin UX ---
        ProductImage.objects.filter(product=p).delete()
        for idx, img in enumerate(images):
            ProductImage.objects.create(
                product=p,
                url=img.get("src"),
                position=idx,
                alt=img.get("alt") or "",
                is_main=(idx == 0),
            )

        # --- variants (order options) ---
        if (wp.get("type") == "variable") and p.woo_id:
            try:
                vars_ = client.fetch_variations(p.woo_id)
                seen_ids = set()
                qty_sum = 0
                for v in vars_:
                    vid = v.get("id")
                    if not vid:
                        continue
                    seen_ids.add(vid)

                    # attributes of variant
                    attrs = {}
                    for va in (v.get("attributes") or []):
                        if va.get("name") and va.get("option"):
                            attrs[va["name"]] = va["option"]

                    # variant weight (store grams)
                    vw_str = (v.get("weight") or "").strip()
                    try:
                        vw = float(vw_str) if vw_str else 0
                        weight_g = int(vw) if vw <= 10000 else int(vw * 1000)
                    except Exception:
                        weight_g = 0

                    var, _ = ProductVariant.objects.get_or_create(
                        woo_variation_id=vid, defaults={"product": p}
                    )
                    var.product = p
                    var.sku = v.get("sku") or ""
                    var.attributes = attrs
                    var.retail_price = decimal.Decimal(str(v.get("price") or var.retail_price or p.retail_price or 0))
                    if not var.wholesale_price:
                        var.wholesale_price = p.wholesale_price
                    var.stock_qty = v.get("stock_quantity") or 0
                    var.is_active = (v.get("status") == "publish")
                    var.image_url = (v.get("image") or {}).get("src", "")
                    var.weight_g = weight_g
                    var.save()

                    if v.get("stock_quantity") is not None:
                        qty_sum += int(v["stock_quantity"])

                # aggregate stock by variants for convenience
                p.stock_qty = qty_sum
                p.save(update_fields=["stock_qty"])

                # deactivate missing variants
                ProductVariant.objects.filter(product=p).exclude(woo_variation_id__in=seen_ids).update(is_active=False)

            except Exception:
                # Keep product stock/variants as-is if variations fetch fails
                pass

        pulled += 1

    modeladmin.message_user(
        request,
        f"Woo sync complete. Pulled {pulled} products (categories, brands, images, facets, variants).",
    )


sync_with_woo.short_description = "Синхронізувати зараз"


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    """Product admin with sync action and inline images."""
    list_display = ("sku", "name", "brand", "wholesale_price", "retail_price", "stock_qty", "weight_g", "is_active")
    search_fields = ("sku", "name")
    list_editable = ("wholesale_price", "stock_qty", "is_active")
    list_filter = ("is_active", "brand")
    actions = [sync_with_woo]
    inlines = [ProductImageInline]

    def get_urls(self):
        """Add a custom admin URL used by the visible 'Sync now' button in template."""
        urls = super().get_urls()
        custom = [
            path(
                "sync-now/",
                self.admin_site.admin_view(self.sync_now_view),
                name="b2b_product_sync_now",
            ),
        ]
        return custom + urls

    def sync_now_view(self, request):
        """Custom view triggered by the 'Sync now' button (calls the action)."""
        try:
            sync_with_woo(self, request, queryset=Product.objects.none())
        except Exception as e:
            messages.error(request, f"Woo sync failed: {e}")
        return redirect("..")


class OrderItemInline(admin.TabularInline):
    """Inline editor for order items inside Order admin."""
    model = OrderItem
    extra = 0


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    """Order admin with inline items."""
    list_display = ("id", "dealer", "status", "subtotal", "total", "created_at")
    list_filter = ("status", "created_at")
    inlines = [OrderItemInline]
