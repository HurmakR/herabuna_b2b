from decimal import Decimal
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_http_methods

try:
    from weasyprint import HTML
    WEASYPRINT_AVAILABLE = True
except Exception:
    WEASYPRINT_AVAILABLE = False

from .forms import DealerSignUpForm
from .models import Brand, Category, Order, OrderItem, Product, ProductVariant
from .services import woo_sync


def signup(request):
    if request.method == "POST":
        form = DealerSignUpForm(request.POST)
        if form.is_valid():
            user = form.save(commit=False)
            user.is_active = True  # set False to require admin approval
            user.is_dealer = True
            user.save()
            login(request, user)
            return redirect("b2b:dashboard")
    else:
        form = DealerSignUpForm()
    return render(request, "b2b/signup.html", {"form": form})


@login_required
def dashboard(request):
    qs = request.user.order_set.order_by("-created_at")[:20]
    return render(request, "b2b/dashboard.html", {"orders": qs})


@login_required
def product_list(request):
    """
    Catalog with search + filters (category, brand).
    Dealers can add to cart; staff can edit price/stock inline.
    """
    q = request.GET.get("q", "").strip()
    cat = request.GET.get("cat")
    brand = request.GET.get("brand")

    products = Product.objects.all() if request.user.is_staff else Product.objects.filter(is_active=True)

    if q:
        products = products.filter(Q(name__icontains=q) | Q(sku__icontains=q))
    if cat and cat.isdigit():
        products = products.filter(categories__id=int(cat))
    if brand and brand.isdigit():
        products = products.filter(brand_id=int(brand))

    products = products.distinct().order_by("name")
    categories = Category.objects.order_by("name")
    brands = Brand.objects.order_by("name")

    ctx = {
        "products": products,
        "q": q,
        "categories": categories,
        "brands": brands,
        "selected_cat": int(cat) if (cat and cat.isdigit()) else None,
        "selected_brand": int(brand) if (brand and brand.isdigit()) else None,
    }
    return render(request, "b2b/product_list.html", ctx)


@login_required
def product_detail(request, product_id: int):
    """Product detail page with variant options and quantity."""
    p = get_object_or_404(Product, id=product_id, is_active=True)

    # Build variant options: {"Length": ["4.5","5.4"], ...}
    variant_options = {}
    for v in p.variants.filter(is_active=True):
        for k, val in (v.attributes or {}).items():
            variant_options.setdefault(k, set()).add(val)
    variant_options = {k: sorted(list(vals)) for k, vals in variant_options.items()}

    return render(request, "b2b/product_detail.html", {"product": p, "variant_options": variant_options})


@login_required
@transaction.atomic
def add_to_cart(request, product_id):
    """
    Add simple product (no variants) with optional qty.
    Enforces stock: item quantity cannot exceed available stock.
    """
    product = get_object_or_404(Product, id=product_id, is_active=True)
    available = max(0, int(product.stock_qty))

    # If no stock, do nothing and return to catalog
    if available <= 0:
        return redirect("b2b:product_list")

    qty_raw = request.POST.get("qty") or request.GET.get("qty") or "1"
    try:
        qty_req = max(1, int(qty_raw))
    except Exception:
        qty_req = 1

    order, _ = Order.objects.get_or_create(dealer=request.user, status="draft")
    item, created = OrderItem.objects.get_or_create(
        order=order,
        product=product,
        variant=None,
        defaults={"qty": 0, "price": product.wholesale_price, "variant_attrs": {}},
    )

    # Compute allowed addition respecting available stock
    current = int(item.qty or 0)
    to_add = min(qty_req, available - current)
    if to_add <= 0:
        # Nothing can be added; show cart with an error
        error = f"Максимально доступно для {product.sku}: {available}."
        order.refresh_from_db()
        return render(request, "b2b/cart.html", {"order": order, "error": error})

    item.qty = current + to_add
    item.save(update_fields=["qty"])
    order.recalc()
    return redirect("b2b:cart")


@login_required
@require_http_methods(["POST"])
@transaction.atomic
def add_to_cart_with_attrs(request, product_id: int):
    """
    Resolve a concrete variant by exact attribute set and add to cart with qty.
    Enforces stock for the matched variant (or product if no variants exist).
    """
    product = get_object_or_404(Product, id=product_id, is_active=True)
    order, _ = Order.objects.get_or_create(dealer=request.user, status="draft")

    # qty request
    try:
        qty_req = max(1, int(request.POST.get("qty", "1")))
    except Exception:
        qty_req = 1

    # attributes
    selected = {}
    for k, v in request.POST.items():
        if k.startswith("attrs[") and k.endswith("]") and v:
            selected[k[6:-1]] = v

    variant = None
    available = max(0, int(product.stock_qty))
    if product.variants.exists():
        # Find exact variant by attributes
        for v in product.variants.filter(is_active=True):
            if (v.attributes or {}) == selected:
                variant = v
                break
        if not variant:
            # Rebuild options and return with error
            variant_options = {}
            for vv in product.variants.filter(is_active=True):
                for kk, val in (vv.attributes or {}).items():
                    variant_options.setdefault(kk, set()).add(val)
            variant_options = {kk: sorted(list(vals)) for kk, vals in variant_options.items()}
            return render(
                request,
                "b2b/product_detail.html",
                {"product": product, "variant_options": variant_options, "error": "Комбінацію не знайдено. Оберіть доступні значення."},
            )
        available = max(0, int(variant.stock_qty))

    # If no stock, return to detail with error
    if available <= 0:
        variant_options = {}
        for vv in product.variants.filter(is_active=True):
            for kk, val in (vv.attributes or {}).items():
                variant_options.setdefault(kk, set()).add(val)
        variant_options = {kk: sorted(list(vals)) for kk, vals in variant_options.items()}
        return render(
            request,
            "b2b/product_detail.html",
            {
                "product": product,
                "variant_options": variant_options,
                "error": "Немає в наявності для обраної комбінації." if variant else "Немає в наявності.",
            },
        )

    price = (variant.wholesale_price if variant else product.wholesale_price)
    item, _ = OrderItem.objects.get_or_create(
        order=order,
        product=product,
        variant=variant,
        defaults={"qty": 0, "price": price, "variant_attrs": selected},
    )
    # Ensure price is in sync when first created
    if item.price != price and item.qty == 0:
        item.price = price

    current = int(item.qty or 0)
    to_add = min(qty_req, available - current)
    if to_add <= 0:
        error = f"Максимально доступно: {available}."
        order.refresh_from_db()
        return render(request, "b2b/cart.html", {"order": order, "error": error})

    item.qty = current + to_add
    item.save(update_fields=["qty", "price"])
    order.recalc()
    return redirect("b2b:cart")


@login_required
def cart(request):
    order = Order.objects.filter(dealer=request.user, status="draft").first()
    return render(request, "b2b/cart.html", {"order": order})


@login_required
@require_http_methods(["POST"])
@transaction.atomic
def cart_update_item(request, item_id: int):
    """
    Dealer can edit qty of draft items via +/- or direct set.
    Enforces stock when increasing or setting quantities.
    """
    item = get_object_or_404(OrderItem.objects.select_related("order", "product", "variant"), id=item_id)
    if item.order.dealer_id != request.user.id or item.order.status != "draft":
        return HttpResponseForbidden("Forbidden")

    # Determine available stock for this line (variant first, else product)
    available = max(0, int(item.variant.stock_qty if item.variant else item.product.stock_qty))
    op = request.POST.get("op")
    error = None

    if op == "inc":
        if item.qty >= available:
            error = f"Максимально доступно: {available}."
        else:
            item.qty += 1
            item.save(update_fields=["qty"])
    elif op == "dec":
        item.qty -= 1
        if item.qty <= 0:
            item.delete()
        else:
            item.save(update_fields=["qty"])
    else:
        # Direct set
        try:
            q = int(request.POST.get("qty", item.qty))
        except Exception:
            q = item.qty
        q = max(0, min(q, available))
        if q <= 0:
            item.delete()
        else:
            item.qty = q
            item.save(update_fields=["qty"])
        if q < int(request.POST.get("qty", q)):
            error = f"Максимально доступно: {available}."

    # Recalculate and render
    order = Order.objects.filter(id=item.order_id).first()
    if order:
        order.recalc()

    if error:
        return render(request, "b2b/cart.html", {"order": order, "error": error})
    return redirect("b2b:cart")


@login_required
@require_http_methods(["POST"])
@transaction.atomic
def cart_remove_item(request, item_id: int):
    """Remove an item from a draft order."""
    item = get_object_or_404(OrderItem.objects.select_related("order"), id=item_id)
    if item.order.dealer_id != request.user.id or item.order.status != "draft":
        return HttpResponseForbidden("Forbidden")
    order = item.order
    item.delete()
    order.recalc()
    return redirect("b2b:cart")


@login_required
@require_http_methods(["POST"])
@transaction.atomic
def cancel_draft_order(request):
    """Dealer can cancel (delete) their current draft order."""
    order = Order.objects.filter(dealer=request.user, status="draft").first()
    if order:
        order.delete()
    return redirect("b2b:product_list")


@login_required
@require_http_methods(["POST"])
@transaction.atomic
def order_delete(request, order_id: int):
    """
    Dealer can delete their own order.
    Allowed statuses: draft, submitted, cancelled.
    Confirmed/fulfilled are not allowed to be deleted.
    """
    order = get_object_or_404(Order, id=order_id)
    if order.dealer_id != request.user.id:
        return HttpResponseForbidden("Forbidden")
    if order.status not in {"draft", "submitted", "cancelled"}:
        return HttpResponse("Неможливо видалити замовлення зі статусом, що обробляється.", status=400)
    order.delete()
    return redirect("b2b:dashboard")


@login_required
@transaction.atomic
def submit_order(request):
    """Submit current draft order, reserve stock locally, and best-effort push to Woo."""
    order = Order.objects.filter(dealer=request.user, status="draft").first()
    if not order or order.items.count() == 0:
        return redirect("b2b:product_list")

    # availability check
    for it in order.items.select_related("product", "variant"):
        available = max(0, int(it.variant.stock_qty if it.variant else it.product.stock_qty))
        if available < it.qty:
            return render(
                request,
                "b2b/cart.html",
                {"order": order, "error": f"Недостатньо на складі для {it.product.sku}. Доступно: {available}"},
            )

    # reserve locally
    for it in order.items.select_related("product", "variant"):
        if it.variant:
            it.variant.stock_qty -= it.qty
            it.variant.save(update_fields=["stock_qty"])
        else:
            it.product.stock_qty -= it.qty
            it.product.save(update_fields=["stock_qty"])

    order.status = "submitted"
    order.recalc()
    order.save(update_fields=["status", "subtotal", "total"])

    client = woo_sync.WooClient()
    for it in order.items.select_related("product", "variant"):
        try:
            if it.variant and it.product.woo_id:
                client.update_variation_stock(it.product.woo_id, it.variant.woo_variation_id, it.variant.stock_qty)
            elif it.product.woo_id:
                client.update_stock(it.product.woo_id, it.product.stock_qty)
        except Exception:
            # Ignore network errors; admin can resync
            pass

    return redirect("b2b:order_detail", order_id=order.id)


@login_required
def order_detail(request, order_id):
    order = get_object_or_404(Order, id=order_id)
    if not (request.user.is_staff or order.dealer_id == request.user.id):
        return HttpResponseForbidden("Forbidden")
    return render(request, "b2b/order_detail.html", {"order": order})


# ---- Staff views ----
def _is_staff(u): return u.is_staff

@user_passes_test(_is_staff)
def orders_admin(request):
    status = request.GET.get("status")
    qs = Order.objects.all().order_by("-created_at")
    if status:
        qs = qs.filter(status=status)
    return render(request, "b2b/orders_admin.html", {"orders": qs, "status": status or ""})


@user_passes_test(_is_staff)
@require_http_methods(["POST"])
def product_update_inline(request, product_id: int):
    """Staff inline update for price/stock/active from catalog list."""
    p = get_object_or_404(Product, id=product_id)
    try:
        p.wholesale_price = Decimal(request.POST.get("wholesale_price", p.wholesale_price))
    except Exception:
        pass
    try:
        p.stock_qty = int(request.POST.get("stock_qty", p.stock_qty))
    except Exception:
        pass
    p.is_active = bool(request.POST.get("is_active"))
    p.save(update_fields=["wholesale_price", "stock_qty", "is_active"])
    return redirect("b2b:product_list")


@user_passes_test(_is_staff)
def order_set_status(request, order_id, status):
    order = get_object_or_404(Order, id=order_id)
    valid = {"draft", "submitted", "confirmed", "fulfilled", "cancelled"}
    if status not in valid:
        return HttpResponse("Invalid status", status=400)
    order.status = status
    order.save(update_fields=["status"])
    return redirect("b2b:orders_admin")


@require_http_methods(["POST", "GET"])
def logout_view(request):
    logout(request)
    return redirect("b2b:login")


def _render_pdf_from_template(request, template_name, context, filename_prefix):
    if not WEASYPRINT_AVAILABLE:
        return HttpResponse("PDF генерація недоступна (WeasyPrint не встановлено). Використайте HTML-друк.", status=501)
    html_string = render(request, template_name, context).content.decode("utf-8")
    pdf = HTML(string=html_string, base_url=request.build_absolute_uri("/")).write_pdf()
    response = HttpResponse(pdf, content_type="application/pdf")
    response["Content-Disposition"] = f'inline; filename="{filename_prefix}_{context.get("order").id}.pdf"'
    return response


@login_required
def invoice_print(request, order_id):
    order = get_object_or_404(Order, id=order_id)
    if not (request.user.is_staff or order.dealer_id == request.user.id):
        return HttpResponseForbidden("Forbidden")
    return render(request, "b2b/invoice_print.html", {"order": order})


@login_required
def waybill_print(request, order_id):
    order = get_object_or_404(Order, id=order_id)
    if not (request.user.is_staff or order.dealer_id == request.user.id):
        return HttpResponseForbidden("Forbidden")
    return render(request, "b2b/waybill_print.html", {"order": order})


@login_required
def invoice_pdf(request, order_id):
    order = get_object_or_404(Order, id=order_id)
    if not (request.user.is_staff or order.dealer_id == request.user.id):
        return HttpResponseForbidden("Forbidden")
    return _render_pdf_from_template(request, "b2b/invoice_print.html", {"order": order}, "invoice")


@login_required
def waybill_pdf(request, order_id):
    order = get_object_or_404(Order, id=order_id)
    if not (request.user.is_staff or order.dealer_id == request.user.id):
        return HttpResponseForbidden("Forbidden")
    return _render_pdf_from_template(request, "b2b/waybill_print.html", {"order": order}, "waybill")
