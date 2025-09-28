from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth import login, logout
from django.db import transaction
from django.shortcuts import render, redirect, get_object_or_404
from django.http import HttpResponse, HttpResponseForbidden
from django.views.decorators.http import require_http_methods

try:
    from weasyprint import HTML
    WEASYPRINT_AVAILABLE = True
except Exception:
    WEASYPRINT_AVAILABLE = False

from .models import Product, Order, OrderItem, ProductVariant
from .forms import DealerSignUpForm
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
    # Dealers see their recent orders; staff can jump to admin orders view
    qs = request.user.order_set.order_by("-created_at")[:10]
    return render(request, "b2b/dashboard.html", {"orders": qs})


@login_required
def product_list(request):
    q = request.GET.get("q", "")
    products = Product.objects.filter(is_active=True)
    if q:
        products = products.filter(name__icontains=q) | products.filter(sku__icontains=q)
    return render(request, "b2b/product_list.html", {"products": products, "q": q})


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

    return render(
        request,
        "b2b/product_detail.html",
        {"product": p, "variant_options": variant_options},
    )


@login_required
@transaction.atomic
def add_to_cart(request, product_id):
    """Add simple product (no variants) with optional qty."""
    product = get_object_or_404(Product, id=product_id, is_active=True)
    # Accept qty from POST (preferred) or fallback to GET
    qty_raw = request.POST.get("qty") or request.GET.get("qty") or "1"
    try:
        qty = max(1, int(qty_raw))
    except Exception:
        qty = 1

    order, _ = Order.objects.get_or_create(dealer=request.user, status="draft")
    item, created = OrderItem.objects.get_or_create(
        order=order,
        product=product,
        variant=None,
        defaults={"qty": qty, "price": product.wholesale_price, "variant_attrs": {}},
    )
    if not created:
        item.qty += qty
        item.save(update_fields=["qty"])
    order.recalc()
    return redirect("b2b:cart")


@login_required
@require_http_methods(["POST"])
@transaction.atomic
def add_to_cart_with_attrs(request, product_id: int):
    """
    Resolve a concrete variant by exact attribute set and add to cart with qty.
    Attributes are read from POST keys: attrs[Length]=5.4 etc.
    """
    product = get_object_or_404(Product, id=product_id, is_active=True)
    order, _ = Order.objects.get_or_create(dealer=request.user, status="draft")

    # qty
    try:
        qty = max(1, int(request.POST.get("qty", "1")))
    except Exception:
        qty = 1

    # attributes
    selected = {}
    for k, v in request.POST.items():
        if k.startswith("attrs[") and k.endswith("]") and v:
            selected[k[6:-1]] = v

    variant = None
    if product.variants.exists():
        for v in product.variants.filter(is_active=True):
            if (v.attributes or {}) == selected:
                variant = v
                break
        if not variant:
            # Re-render detail with an error
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

    price = variant.wholesale_price if variant else product.wholesale_price
    item, created = OrderItem.objects.get_or_create(
        order=order,
        product=product,
        variant=variant,
        defaults={"qty": qty, "price": price, "variant_attrs": selected},
    )
    if not created:
        item.qty += qty
        item.save(update_fields=["qty"])
    order.recalc()
    return redirect("b2b:cart")


@login_required
def cart(request):
    order = Order.objects.filter(dealer=request.user, status="draft").first()
    return render(request, "b2b/cart.html", {"order": order})


@login_required
@transaction.atomic
def submit_order(request):
    """Submit current draft order, reserve stock locally, and best-effort push to Woo."""
    order = Order.objects.filter(dealer=request.user, status="draft").first()
    if not order or order.items.count() == 0:
        return redirect("b2b:product_list")

    # Availability check
    for it in order.items.select_related("product", "variant"):
        available = it.variant.stock_qty if it.variant else it.product.stock_qty
        if available < it.qty:
            return render(
                request,
                "b2b/cart.html",
                {"order": order, "error": f"Недостатньо на складі для {it.product.sku}. Доступно: {available}"},
            )

    # Reserve locally
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

    # Best-effort push to Woo
    client = woo_sync.WooClient()
    for it in order.items.select_related("product", "variant"):
        try:
            if it.variant and it.product.woo_id:
                client.update_variation_stock(it.product.woo_id, it.variant.woo_variation_id, it.variant.stock_qty)
            elif it.product.woo_id:
                client.update_stock(it.product.woo_id, it.product.stock_qty)
        except Exception:
            pass

    return redirect("b2b:order_detail", order_id=order.id)


@login_required
def order_detail(request, order_id):
    """Dealers can see their own orders; staff can see any order."""
    order = get_object_or_404(Order, id=order_id)
    if not (request.user.is_staff or order.dealer_id == request.user.id):
        return HttpResponseForbidden("Forbidden")
    return render(request, "b2b/order_detail.html", {"order": order})


# ---- Staff views for managing all orders ----

def _is_staff(u): return u.is_staff

@user_passes_test(_is_staff)
def orders_admin(request):
    """Staff-only: see and manage all dealer orders."""
    status = request.GET.get("status")
    qs = Order.objects.all().order_by("-created_at")
    if status:
        qs = qs.filter(status=status)
    return render(request, "b2b/orders_admin.html", {"orders": qs, "status": status or ""})


@user_passes_test(_is_staff)
def order_set_status(request, order_id, status):
    """Staff-only: update order status quickly from list."""
    order = get_object_or_404(Order, id=order_id)
    valid = {"draft", "submitted", "confirmed", "fulfilled", "cancelled"}
    if status not in valid:
        return HttpResponse("Invalid status", status=400)
    order.status = status
    order.save(update_fields=["status"])
    return redirect("b2b:orders_admin")


# Optional logout helper (POST recommended)
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
