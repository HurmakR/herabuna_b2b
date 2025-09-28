from django.contrib import admin
from django.urls import path
from django.shortcuts import redirect
from django.contrib import messages
import decimal

from .models import (
    Dealer,
    Product,
    Order,
    OrderItem,
    Category,
    ProductImage,
    ProductCategory,
    Brand,
    Facet,
    ProductVariant,
)
from .services.woo_sync import WooClient


@admin.register(Dealer)
class DealerAdmin(admin.ModelAdmin):
    """Dealer admin configuration."""
    list_display = ("username", "company_name", "email", "is_active", "is_staff")
    search_fields = ("username", "company_name", "email")


@admin.register(Category)
class CategoryAdmin(admin.ModelAdmin):
    """Category admin configuration."""
    list_display = ("name", "slug", "woo_id", "is_active", "parent")
    search_fields = ("name", "slug")
    list_filter = ("is_active",)


class ProductImageInline(admin.TabularInline):
    """Inline editor for per-product images."""
    model = ProductImage
    extra = 0
    readonly_fields = ()


def _facet_type_from_attr_name(attr_name: str):
    """Heuristic mapping of Woo attribute names to facet types."""
    n = (attr_name or "").strip().lower()
    if "brand" in n or "бренд" in n:
        return "brand"
    if "ingredient" in n or "інгреді" in n:
        return "ingredient"
    if "effective" in n or "ефектив" in n:
        return "effect"
    if "season" in n or "сезон" in n:
        return "season"
    return None


def sync_with_woo(modeladmin, request, queryset):
    """
    Admin action: pull products + categories + images + descriptive facets + variants.
    Stock for variable products is taken from variations; simple products use product stock.
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

        # Core fields
        p.name = wp.get("name") or p.name
        p.retail_price = decimal.Decimal(str(wp.get("price") or p.retail_price or 0))
        p.is_active = (wp.get("status") == "publish")
        p.woo_id = wp.get("id")

        # Weight: Woo stores strings; store grams best-effort
        weight_str = (wp.get("weight") or "").strip()
        try:
            w = float(weight_str) if weight_str else 0
            p.weight_g = int(w) if w <= 10000 else int(w * 1000)
        except Exception:
            p.weight_g = 0

        # Media
        images = wp.get("images") or []
        p.main_image_url = images[0]["src"] if images else ""
        p.gallery = [img["src"] for img in images]

        # Informational attributes (non-order options)
        info_attrs = {}
        for a in (wp.get("attributes") or []):
            name = a.get("name") or ""
            options = a.get("options") or []
            if name:
                info_attrs[name] = options
        p.attributes = info_attrs

        p.save()

        # Categories
        for c in (wp.get("categories") or []):
            cat, _ = Category.objects.get_or_create(
                woo_id=c.get("id"),
                defaults={"name": c.get("name") or "", "slug": c.get("slug") or ""},
            )
            if c.get("name") and cat.name != c["name"]:
                cat.name = c["name"]
            if c.get("slug") and cat.slug != c["slug"]:
                cat.slug = c["slug"]
            cat.save()
            ProductCategory.objects.get_or_create(product=p, category=cat)

        # Brand and descriptive facets from attributes
        for a in (wp.get("attributes") or []):
            ftype = _facet_type_from_attr_name(a.get("name"))
            opts = a.get("options") or []
            if not ftype or not opts:
                continue
            if ftype == "brand":
                bname = opts[0]
                brand, _ = Brand.objects.get_or_create(name=bname)
                p.brand = brand
                p.save(update_fields=["brand"])
            else:
                for opt in opts:
                    facet, _ = Facet.objects.get_or_create(type=ftype, name=opt)
                    p.facets.add(facet)

        # Rebuild ProductImage table for admin UX
        ProductImage.objects.filter(product=p).delete()
        for idx, img in enumerate(images):
            ProductImage.objects.create(
                product=p,
                url=img.get("src"),
                position=idx,
                alt=img.get("alt") or "",
                is_main=(idx == 0),
            )

        # Variants (order options)
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
                    attrs = {}
                    for va in (v.get("attributes") or []):
                        if va.get("name") and va.get("option"):
                            attrs[va["name"]] = va["option"]
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
                    var.save()
                    if v.get("stock_quantity") is not None:
                        qty_sum += int(v["stock_quantity"])
                # Aggregate product stock from variants for convenience
                p.stock_qty = qty_sum
                p.save(update_fields=["stock_qty"])
                # Deactivate variants that disappeared
                ProductVariant.objects.filter(product=p).exclude(woo_variation_id__in=seen_ids).update(is_active=False)
            except Exception:
                # Keep product stock and variants as they were if fetching variations failed
                pass

        pulled += 1

    modeladmin.message_user(
        request,
        f"Woo sync complete. Pulled {pulled} products (with categories, images, facets, variants).",
    )


sync_with_woo.short_description = "Синхронізувати зараз"


@admin.register(Product)
class ProductAdmin(admin.ModelAdmin):
    """Product admin with inline images; wholesale price is edited manually."""
    list_display = ("sku", "name", "wholesale_price", "retail_price", "stock_qty", "is_active")
    search_fields = ("sku", "name")
    list_editable = ("wholesale_price", "stock_qty", "is_active")
    actions = [sync_with_woo]
    inlines = [ProductImageInline]

    def get_urls(self):
        """Add a custom admin URL used by the visible 'Sync now' button."""
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
        """Custom view triggered by the 'Sync now' button."""
        try:
            sync_with_woo(self, request, queryset=Product.objects.none())
        except Exception as e:
            messages.error(request, f"Woo sync failed: {e}")
        return redirect("..")


class OrderItemInline(admin.TabularInline):
    """Inline editor for order items."""
    model = OrderItem
    extra = 0


@admin.register(Order)
class OrderAdmin(admin.ModelAdmin):
    """Order admin with inline items."""
    list_display = ("id", "dealer", "status", "subtotal", "total", "created_at")
    list_filter = ("status", "created_at")
    inlines = [OrderItemInline]
