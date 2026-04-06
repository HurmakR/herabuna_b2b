from decimal import Decimal
from django.conf import settings
from django.contrib import messages
from django.core.mail import EmailMessage, send_mail
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse, HttpResponseForbidden, Http404
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods
from django.http import JsonResponse
from django.views.decorators.http import require_GET
from .services import np_client
from django.core.paginator import Paginator
from urllib.parse import urlencode
from warehouse import services as wh

try:
    from weasyprint import HTML
    WEASYPRINT_AVAILABLE = True
except Exception:
    WEASYPRINT_AVAILABLE = False

from django.forms import modelform_factory
from .forms import (
    DealerSignUpForm,
    ProfileForm,
    AddressForm,
    AdminOrderCreateForm,
    AdminOrderLineFormSet,
    OrderShippingForm,
)
from .models import Brand, Category, Order, OrderItem, Product, ProductVariant, Address, Dealer
from .services import woo_sync, np_api, telegram as tg
from .services.marketplace_meta import enrich_order_ui_meta


def _safe_next_url(request, default_name="b2b:product_list"):
    """Return a safe redirect target from ?next= or POST; fallback to catalog."""
    nxt = request.POST.get("next") or request.GET.get("next")
    if nxt and isinstance(nxt, str) and nxt.startswith("/"):
        return nxt
    from django.urls import reverse
    return reverse(default_name)


def signup(request):
    if request.method == "POST":
        form = DealerSignUpForm(request.POST)
        if form.is_valid():
            user = form.save(commit=False)
            # Нові дилери чекають підтвердження адміном
            user.is_active = False
            user.is_dealer = True
            user.save()

            # Сповістити адміна (не критично, fail_silently)
            admin_email = getattr(settings, "ORDER_NOTIFY_EMAIL", "")
            if admin_email:
                try:
                    send_mail(
                        subject="Нова реєстрація дилера",
                        message=f"Користувач {user.username} ({user.email}) зареєструвався.",
                        from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
                        recipient_list=[admin_email],
                        fail_silently=True,
                    )
                except Exception:
                    pass

            messages.info(
                request,
                "Реєстрація надіслана на модерацію. Ви отримаєте сповіщення після підтвердження."
            )
            return redirect("b2b:login")
        # не валідна форма — показуємо з помилками
        return render(request, "b2b/signup.html", {"form": form})
    else:
        form = DealerSignUpForm()
        return render(request, "b2b/signup.html", {"form": form})


@login_required
def dashboard(request):
    """Dealer dashboard: recent orders + a compact purchase summary."""
    from datetime import timedelta
    from decimal import Decimal

    from django.db.models import Count, DecimalField, ExpressionWrapper, F, Q, Sum
    from django.utils import timezone

    # Show only non-draft orders; draft is the cart.
    base_qs = request.user.order_set.exclude(status="draft")

    period = (request.GET.get("period") or "90").strip()
    top_scope = (request.GET.get("top_scope") or "mine").strip()  # mine | all
    top_by = (request.GET.get("top_by") or "qty").strip()  # qty | sum

    if top_scope not in {"mine", "all"}:
        top_scope = "mine"
    if top_by not in {"qty", "sum"}:
        top_by = "qty"

    # Default UX: dealer's top-5 by quantity
    if "top_scope" not in request.GET:
        top_scope = "mine"
    if "top_by" not in request.GET:
        top_by = "qty"

    now = timezone.now()

    period_options = {
        "30": ("Останні 30 днів", now - timedelta(days=30)),
        "90": ("Останні 90 днів", now - timedelta(days=90)),
        "365": ("Останній рік", now - timedelta(days=365)),
        "all": ("Весь час", None),
    }
    if period not in period_options:
        period = "365"
    period_label, since_dt = period_options[period]

    period_qs = base_qs
    if since_dt:
        period_qs = period_qs.filter(created_at__gte=since_dt)

    # Recent orders list (filtered by period)
    orders = period_qs.order_by("-created_at")[:20]

    summary = period_qs.aggregate(
        orders_count=Count("id"),
        total_sum=Sum("total"),
        shipped_count=Count("id", filter=Q(status="shipped")),
        shipped_sum=Sum("total", filter=Q(status="shipped")),
        pending_count=Count("id", filter=Q(status__in=["submitted", "pending_payment"])),
        cancelled_count=Count("id", filter=Q(status="cancelled")),
    )

    shipped_count = int(summary.get("shipped_count") or 0)
    shipped_sum = summary.get("shipped_sum") or Decimal("0")
    avg_shipped = (shipped_sum / shipped_count) if shipped_count else Decimal("0")

    # Items stats (shipped only, to reflect actual purchases)
    from .models import OrderItem

    shipped_orders_qs = period_qs.filter(status="shipped")
    items_summary = OrderItem.objects.filter(order__in=shipped_orders_qs).aggregate(
        items_qty=Sum("qty"),
    )

    # ---- Top products ----
    show_top_metrics = (top_scope == "mine")
    top_scope_label = "Мій топ‑5" if top_scope == "mine" else "Топ‑5 по всіх дилерах"

    money_expr = ExpressionWrapper(
        F("qty") * F("price"),
        output_field=DecimalField(max_digits=12, decimal_places=2),
    )

    if top_scope == "all":
        # Overall top-5 across all dealers (shipped orders only).
        top_items_base = OrderItem.objects.filter(order__status="shipped")
        if since_dt:
            top_items_base = top_items_base.filter(order__created_at__gte=since_dt)
        top_by_effective = top_by
    else:
        # Dealer's own top-5 (shipped orders only).
        top_items_base = OrderItem.objects.filter(order__in=shipped_orders_qs)
        top_by_effective = top_by

    top_products_qs = (
        top_items_base.values("product_id", "product__sku", "product__name", "product__main_image_url")
        .annotate(total_qty=Sum("qty"), total_sum=Sum(money_expr))
    )

    if top_by_effective == "sum":
        top_products_qs = top_products_qs.order_by("-total_sum", "product__name")
        top_metric_label = "за сумою"
    else:
        top_products_qs = top_products_qs.order_by("-total_qty", "product__name")
        top_metric_label = "за кількістю"

    top_products = top_products_qs[:5]

    context = {
        "orders": orders,
        "period": period,
        "period_label": period_label,
        "summary": summary,
        "avg_shipped": avg_shipped,
        "items_summary": items_summary,
        "top_products": top_products,
        "top_by": top_by,
        "top_scope": top_scope,
        "top_scope_label": top_scope_label,
        "top_metric_label": top_metric_label,
        "show_top_metrics": show_top_metrics,
    }
    return render(request, "b2b/dashboard.html", context)


# ---------- PROFILE ----------
@login_required

def profile_view(request):
    """Dealer profile with two tabs: profile form and addresses link."""
    form = ProfileForm(instance=request.user)
    if request.method == "POST":
        form = ProfileForm(request.POST, instance=request.user)
        if form.is_valid():
            # Ensure only one default address across user's addresses if changed elsewhere.
            form.save()
            messages.success(request, "Профіль збережено.")
            return redirect("b2b:profile")
    return render(request, "b2b/profile.html", {"form": form})

@login_required
def address_list(request):
    """List and manage addresses; links to create/edit/delete."""
    addrs = Address.objects.filter(dealer=request.user).order_by("-is_default", "-created_at")
    return render(request, "b2b/address_list.html", {"addresses": addrs})

@login_required
def address_create(request):
    """Create a new NP address."""
    form = AddressForm()
    if request.method == "POST":
        form = AddressForm(request.POST)
        if form.is_valid():
            addr = form.save(commit=False)
            addr.dealer = request.user
            # Keep only one default
            if addr.is_default:
                Address.objects.filter(dealer=request.user, is_default=True).update(is_default=False)
            addr.save()
            messages.success(request, "Адресу додано.")
            return redirect("b2b:address_list")
    return render(request, "b2b/address_form.html", {"form": form, "is_edit": False})

@login_required
def address_edit(request, pk: int):
    """Edit an NP address."""
    addr = get_object_or_404(Address, pk=pk, dealer=request.user)
    form = AddressForm(instance=addr)
    if request.method == "POST":
        form = AddressForm(request.POST, instance=addr)
        if form.is_valid():
            addr = form.save(commit=False)
            if addr.is_default:
                Address.objects.filter(dealer=request.user, is_default=True).exclude(pk=addr.pk).update(is_default=False)
            addr.save()
            messages.success(request, "Адресу збережено.")
            return redirect("b2b:address_list")
    return render(request, "b2b/address_form.html", {"form": form, "is_edit": True})

@login_required
@require_http_methods(["POST"])
def address_delete(request, pk: int):
    """Delete an NP address."""
    addr = get_object_or_404(Address, pk=pk, dealer=request.user)
    addr.delete()
    messages.info(request, "Адресу видалено.")
    return redirect("b2b:address_list")

def _windowed_range(page_obj, width=2):
    cur = page_obj.number
    total = page_obj.paginator.num_pages
    start = max(1, cur - width)
    end = min(total, cur + width)
    pages = []
    if start > 1:
        pages.extend([1, None])  # None = ellipsis
    pages.extend(range(start, end + 1))
    if end < total:
        pages.extend([None, total])
    return pages

@login_required
def product_list(request):
    """
    Catalog with search (name + sku), filters (category, brand) and sorting.
    Default sorting: in-stock first (stock_desc).
    """
    q = (request.GET.get("q") or "").strip()
    cat = request.GET.get("category") or request.GET.get("cat")
    brand = request.GET.get("brand")
    sort = (request.GET.get("sort") or "stock_desc").strip()

    qs = Product.objects.select_related("brand").prefetch_related("categories").all()

    # Dealers should not see products without wholesale price.
    # Also hide inactive products for non-staff users.
    if not request.user.is_authenticated or not request.user.is_staff:
        qs = qs.filter(is_active=True, wholesale_price__gt=0)

    # Staff: show last inbound unit cost from warehouse lots.
    if request.user.is_authenticated and request.user.is_staff:
        from django.db.models import OuterRef, Subquery
        from warehouse.models import InventoryLot

        qs = qs.annotate(
            last_unit_cost=Subquery(
                InventoryLot.objects.filter(product_id=OuterRef("pk"))
                .order_by("-received_at", "-id")
                .values("unit_cost")[:1]
            )
        )

    if q:
        qs = qs.filter(Q(name__icontains=q) | Q(sku__icontains=q))

    if cat:
        qs = qs.filter(categories__id=cat)

    if brand:
        qs = qs.filter(brand_id=brand)

    # Sorting options
    if sort == "price_asc":
        qs = qs.order_by("wholesale_price", "name")
    elif sort == "price_desc":
        qs = qs.order_by("-wholesale_price", "name")
    elif sort == "stock_asc":
        qs = qs.order_by("stock_qty", "name")
    elif sort == "name_asc":
        qs = qs.order_by("name")
    elif sort == "name_desc":
        qs = qs.order_by("-name")
    elif sort == "sku_asc":
        qs = qs.order_by("sku")
    elif sort == "sku_desc":
        qs = qs.order_by("-sku")
    elif sort == "brand_asc":
        qs = qs.order_by("brand__name", "name")
    elif sort == "brand_desc":
        qs = qs.order_by("-brand__name", "name")
    else:
        # stock_desc (default)
        qs = qs.order_by("-stock_qty", "name")

    paginator = Paginator(qs, 24)
    page_number = request.GET.get("page")
    page_obj = paginator.get_page(page_number)

    # keep current filters without 'page'
    qs_params = request.GET.copy()
    qs_params.pop("page", None)
    qs_str = qs_params.urlencode()

    context = {
        "products": page_obj.object_list,
        "categories": Category.objects.all(),
        "brands": Brand.objects.all(),
        "q": q,
        "selected_cat": int(cat) if cat else "",
        "selected_brand": int(brand) if brand else "",
        "sort": sort,
        "page_obj": page_obj,
        "page_numbers": _windowed_range(page_obj, width=2),
        "qs": qs_str,
    }
    return render(request, "b2b/product_list.html", context)


@login_required
def product_detail(request, product_id: int):
    """Product detail page with variant options and quantity."""
    p = get_object_or_404(Product, id=product_id, is_active=True)

    # Dealers should not access products without wholesale price.
    if (not request.user.is_authenticated or not request.user.is_staff) and (p.wholesale_price or 0) <= 0:
        raise Http404
    variant_options = {}
    for v in p.variants.filter(is_active=True):
        for k, val in (v.attributes or {}).items():
            variant_options.setdefault(k, set()).add(val)
    variant_options = {k: sorted(list(vals)) for k, vals in variant_options.items()}
    return render(request, "b2b/product_detail.html", {"product": p, "variant_options": variant_options})


@login_required
@transaction.atomic
def add_to_cart(request, product_id):
    """Add simple product with optional qty; enforce stock; stay on same page."""
    product = get_object_or_404(Product, id=product_id, is_active=True)
    if not request.user.is_staff and (product.wholesale_price or 0) <= 0:
        messages.error(request, "Для цього товару не встановлена гуртова ціна.")
        return redirect(_safe_next_url(request))
    available = max(0, int(product.stock_qty))
    if available <= 0:
        messages.info(request, "Немає в наявності.")
        return redirect(_safe_next_url(request))
    qty_raw = request.POST.get("qty") or request.GET.get("qty") or "1"
    try:
        qty_req = max(1, int(qty_raw))
    except Exception:
        qty_req = 1
    order, _ = Order.objects.get_or_create(dealer=request.user, status="draft")
    item, _ = OrderItem.objects.get_or_create(
        order=order, product=product, variant=None,
        defaults={"qty": 0, "price": product.wholesale_price, "variant_attrs": {}},
    )
    current = int(item.qty or 0)
    to_add = min(qty_req, available - current)
    if to_add <= 0:
        messages.warning(request, f"Максимально доступно для {product.sku}: {available}.")
        return redirect(_safe_next_url(request))
    item.qty = current + to_add
    item.save(update_fields=["qty"])
    order.recalc()
    messages.success(request, f"Додано у кошик: {product.sku} × {to_add}.")
    return redirect(_safe_next_url(request))


@login_required
@require_http_methods(["POST"])
@transaction.atomic
def add_to_cart_with_attrs(request, product_id: int):
    """Add concrete variant by attributes; enforce stock; stay on same page."""
    product = get_object_or_404(Product, id=product_id, is_active=True)
    if not request.user.is_staff and (product.wholesale_price or 0) <= 0:
        messages.error(request, "Для цього товару не встановлена гуртова ціна.")
        return redirect(_safe_next_url(request))
    order, _ = Order.objects.get_or_create(dealer=request.user, status="draft")
    try:
        qty_req = max(1, int(request.POST.get("qty", "1")))
    except Exception:
        qty_req = 1
    selected = {}
    for k, v in request.POST.items():
        if k.startswith("attrs[") and k.endswith("]") and v:
            selected[k[6:-1]] = v
    variant = None
    available = max(0, int(product.stock_qty))
    if product.variants.exists():
        for v in product.variants.filter(is_active=True):
            if (v.attributes or {}) == selected:
                variant = v
                break
        if not variant:
            messages.error(request, "Комбінацію не знайдено. Оберіть доступні значення.")
            return redirect(_safe_next_url(request, default_name="b2b:product_detail"))
        available = max(0, min(int(product.stock_qty or 0), int(variant.stock_qty or 0)))
    if available <= 0:
        messages.info(request, "Немає в наявності для обраної комбінації.")
        return redirect(_safe_next_url(request))
    price = (variant.wholesale_price if variant else product.wholesale_price)
    if not request.user.is_staff and (price or 0) <= 0:
        messages.error(request, "Для цього варіанту не встановлена гуртова ціна.")
        return redirect(_safe_next_url(request))
    item, _ = OrderItem.objects.get_or_create(
        order=order, product=product, variant=variant,
        defaults={"qty": 0, "price": price, "variant_attrs": selected},
    )
    if item.price != price and item.qty == 0:
        item.price = price
    current = int(item.qty or 0)
    to_add = min(qty_req, available - current)
    if to_add <= 0:
        messages.warning(request, f"Максимально доступно: {available}.")
        return redirect(_safe_next_url(request))
    item.qty = current + to_add
    item.save(update_fields=["qty", "price"])
    order.recalc()
    messages.success(request, "Додано у кошик.")
    return redirect(_safe_next_url(request))


@login_required
def cart(request):
    order = Order.objects.filter(dealer=request.user, status="draft").first()
    return render(request, "b2b/cart.html", {"order": order})


@login_required
@require_http_methods(["POST"])
@transaction.atomic
def cart_update_item(request, item_id: int):
    """Dealer can edit qty of draft items; stock limits are enforced."""
    item = get_object_or_404(OrderItem.objects.select_related("order", "product", "variant"), id=item_id)
    if item.order.dealer_id != request.user.id or item.order.status != "draft":
        return HttpResponseForbidden("Forbidden")
    if item.variant:
        available = max(0, min(int(item.product.stock_qty or 0), int(item.variant.stock_qty or 0)))
    else:
        available = max(0, int(item.product.stock_qty or 0))
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
    order = Order.objects.filter(id=item.order_id).first()
    if order:
        order.recalc()
    if error:
        messages.warning(request, error)
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
def cart_clear(request):
    """Remove all items from the current draft cart (delete the draft order)."""
    order = Order.objects.filter(dealer=request.user, status="draft").first()
    if order:
        order.delete()
        messages.info(request, "Кошик очищено.")
    return redirect("b2b:product_list")


@login_required
@transaction.atomic
def submit_order(request):
    """Submit draft order, reserve stock, push to Woo, notify admin via email."""
    order = Order.objects.filter(dealer=request.user, status="draft").first()
    if not order or order.items.count() == 0:
        return redirect("b2b:product_list")
    # Check availability
    for it in order.items.select_related("product", "variant"):
        if it.variant:
            available = max(0, min(int(it.product.stock_qty or 0), int(it.variant.stock_qty or 0)))
        else:
            available = max(0, int(it.product.stock_qty or 0))
        if available < it.qty:
            messages.error(request, f"Недостатньо на складі для {it.product.sku}. Доступно: {available}")
            return redirect("b2b:cart")
    # Reserve locally via FIFO lots (warehouse)
    try:
        wh.reserve_order(order)
    except Exception as e:
        messages.error(request, f"Помилка резервування: {e}")
        return redirect("b2b:cart")
    order.status = "submitted"
    order.recalc()
    order.save(update_fields=["status", "subtotal", "total"])
    # NOTE: WooCommerce sync is catalog-only in this project.
    # Stock/price are managed exclusively in local warehouse (lots) + manual pricing.
    # Notify admin via email (brief)
    try:
        admin_email = getattr(settings, "ORDER_NOTIFY_EMAIL", None) or (settings.ADMINS[0][1] if getattr(settings, "ADMINS", None) else None)
        if admin_email:
            send_mail(
                subject=f"Нове замовлення #{order.id}",
                message=f"Надійшло нове замовлення #{order.id} від {order.dealer.username}.",
                from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
                recipient_list=[admin_email],
                fail_silently=True,
            )
    except Exception:
        pass
    messages.success(request, "Замовлення надіслано.")
    return redirect("b2b:order_detail", order_id=order.id)


@login_required
def order_detail(request, order_id):
    order = get_object_or_404(Order, id=order_id)
    if not (request.user.is_staff or order.dealer_id == request.user.id):
        return HttpResponseForbidden("Forbidden")

    # Display-only: compute total weight for UI.
    try:
        order_weight_kg = np_api._compute_order_weight_kg(order)
    except Exception:
        order_weight_kg = None

    enrich_order_ui_meta(order)

    return render(
        request,
        "b2b/order_detail.html",
        {"order": order, "order_weight_kg": order_weight_kg},
    )


# ---- Staff views ----
def _is_staff(u): return u.is_staff

def _np_phone_digits(raw: str) -> str:
    """Return phone as digits in 380XXXXXXXXX format when possible (no leading '+')."""
    try:
        norm = np_api._normalize_phone(raw or "")
    except Exception:
        norm = ""
    if norm.startswith("+"):
        norm = norm[1:]
    # Fallback: keep digits only
    if not norm:
        norm = "".join(ch for ch in str(raw or "") if ch.isdigit())
    return norm


def _np_recipient_ready(order) -> bool:
    """Check that NP recipient snapshot is complete enough to create a TTN."""
    city_ref = (getattr(order, "shipping_city_ref", "") or "").strip()
    wh_ref = (getattr(order, "shipping_warehouse_ref", "") or "").strip()
    recip = (getattr(order, "shipping_recipient", "") or "").strip()
    phone_digits = _np_phone_digits(getattr(order, "shipping_phone", "") or "")
    if not (city_ref and wh_ref and recip):
        return False
    return phone_digits.isdigit() and len(phone_digits) == 12 and phone_digits.startswith("380")

@user_passes_test(_is_staff)
def orders_admin(request):
    """Admin orders list with status + dealer filters."""
    status = (request.GET.get("status") or "").strip()
    dealer_id = (request.GET.get("dealer") or "").strip()

    qs = (
        Order.objects
        .select_related("dealer")
        .all()
        .order_by("-created_at")
    )

    if status:
        qs = qs.filter(status=status)
    if dealer_id:
        qs = qs.filter(dealer_id=dealer_id)

    dealers = Dealer.objects.filter(is_dealer=True).order_by("username")

    orders = list(qs)
    for o in orders:
        enrich_order_ui_meta(o)

    context = {
        "orders": orders,
        "status": status,
        "dealer_id": dealer_id,
        "dealers": dealers,
    }
    return render(request, "b2b/orders_admin.html", context)


@user_passes_test(_is_staff)
@transaction.atomic
def order_admin_create(request):
    """Create an order on behalf of a dealer.

    Staff fills dealer + SKU/qty lines. We create a draft order, reserve stock (FIFO),
    then mark it as 'submitted' to enter the normal admin workflow.
    """

    def _lookup_sku(sku: str):
        """Resolve SKU to (product, variant). Variant is preferred if SKU matches a variant."""
        sku = (sku or "").strip()
        if not sku:
            return None, None
        v = (
            ProductVariant.objects.select_related("product")
            .filter(sku__iexact=sku)
            .first()
        )
        if v:
            return v.product, v
        p = Product.objects.filter(sku__iexact=sku).first()
        if p:
            return p, None
        return None, None

    def _available_product_choices():
        """Return choices for products that can be ordered (active, priced, in stock)."""
        qs = (
            Product.objects.filter(is_active=True, wholesale_price__gt=0, stock_qty__gt=0)
            .order_by("name")
        )
        return [
            ("", "— виберіть товар —"),
            *[(p.sku, f"{p.sku} — {p.name} (залишок {p.stock_qty})") for p in qs],
        ]

    product_choices = _available_product_choices()

    if request.method == "POST":
        header_form = AdminOrderCreateForm(request.POST)
        formset = AdminOrderLineFormSet(
            request.POST,
            prefix="line",
            form_kwargs={"product_choices": product_choices},
        )

        if header_form.is_valid() and formset.is_valid():
            dealer = header_form.cleaned_data["dealer"]
            note = header_form.cleaned_data.get("note") or ""

            raw_lines = []
            for f in formset.forms:
                if not f.cleaned_data or f.cleaned_data.get("DELETE"):
                    continue
                sku = (f.cleaned_data.get("sku") or "").strip()
                qty = int(f.cleaned_data.get("qty") or 0)
                if not sku or qty <= 0:
                    continue
                raw_lines.append((sku, qty))

            if not raw_lines:
                messages.error(request, "Додайте хоча б один рядок (товар + кількість).")
                return render(
                    request,
                    "b2b/order_admin_create.html",
                    {"header_form": header_form, "formset": formset},
                )

            # Aggregate duplicates to avoid unique_together conflicts.
            aggregated = {}  # key=(product_id, variant_id or 0) -> dict
            errors = []
            for sku, qty in raw_lines:
                product, variant = _lookup_sku(sku)
                if not product:
                    errors.append(f"SKU не знайдено: {sku}")
                    continue

                price = None
                attrs = {}
                if variant:
                    price = variant.wholesale_price or product.wholesale_price
                    attrs = variant.attributes or {}
                else:
                    price = product.wholesale_price

                try:
                    price_val = Decimal(str(price or 0))
                except Exception:
                    price_val = Decimal("0")

                if price_val <= 0:
                    errors.append(f"Для SKU {sku} не задано гуртову ціну.")
                    continue

                key = (int(product.id), int(variant.id) if variant else 0)
                if key not in aggregated:
                    aggregated[key] = {
                        "product": product,
                        "variant": variant,
                        "qty": 0,
                        "price": price_val,
                        "attrs": attrs,
                        "sku": sku,
                    }
                aggregated[key]["qty"] += int(qty)

            if errors:
                for e in errors:
                    messages.error(request, e)
                return render(
                    request,
                    "b2b/order_admin_create.html",
                    {"header_form": header_form, "formset": formset},
                )

            try:
                order = Order.objects.create(dealer=dealer, status="draft", note=note)

                # Auto-fill shipping snapshot from dealer's default address (best-effort).
                addr = (
                    Address.objects.filter(dealer=dealer)
                    .order_by('-is_default', '-created_at')
                    .first()
                )
                if addr:
                    order.shipping_address = addr
                    order.shipping_city = addr.city_name
                    order.shipping_city_ref = addr.city_ref or ''
                    order.shipping_warehouse = addr.warehouse_name
                    order.shipping_warehouse_ref = addr.warehouse_ref or ''
                    order.shipping_recipient = addr.recipient_name
                    order.shipping_phone = _np_phone_digits(addr.recipient_phone)
                    order.save(update_fields=[
                        'shipping_address',
                        'shipping_city', 'shipping_city_ref',
                        'shipping_warehouse', 'shipping_warehouse_ref',
                        'shipping_recipient', 'shipping_phone',
                    ])


                for rec in aggregated.values():
                    OrderItem.objects.create(
                        order=order,
                        product=rec["product"],
                        variant=rec["variant"],
                        qty=int(rec["qty"]),
                        price=rec["price"],
                        variant_attrs=rec["attrs"] or {},
                    )

                # Reserve stock using FIFO lots (will raise WarehouseError on shortage).
                wh.reserve_order(order)

                order.status = "submitted"
                order.recalc()
                order.save(update_fields=["status", "subtotal", "total"])

            except Exception as e:
                messages.error(request, f"Не вдалося створити замовлення: {e}")
                # Transaction rollback will remove the order/items/reservations.
                return render(
                    request,
                    "b2b/order_admin_create.html",
                    {"header_form": header_form, "formset": formset},
                )

            messages.success(request, f"Замовлення #{order.id} створено для {dealer.username}.")
            return redirect("b2b:order_detail", order_id=order.id)

    else:
        header_form = AdminOrderCreateForm()
        formset = AdminOrderLineFormSet(prefix="line", form_kwargs={"product_choices": product_choices})

    return render(request, "b2b/order_admin_create.html", {"header_form": header_form, "formset": formset})


@user_passes_test(_is_staff)
def order_admin_edit_items(request, order_id: int):
    """Edit items for an existing order (staff-only).

    Allowed statuses: submitted, pending_payment.
    After saving, reservations are released and re-applied using FIFO lots.
    """
    order = get_object_or_404(Order, id=order_id)

    if order.status not in {"submitted", "pending_payment"}:
        messages.error(request, "Редагування доступне тільки для замовлень у статусі 'Надіслано' або 'Очікує оплату'.")
        return redirect("b2b:order_detail", order_id=order.id)

    # Build choices: keep existing variants as options, plus products list.
    products_qs = Product.objects.filter(is_active=True, wholesale_price__gt=0).order_by("name")
    product_choices = [(f"p:{p.id}", f"{p.sku} — {p.name} (залишок {p.stock_qty})") for p in products_qs]

    existing_variant_ids = list(
        order.items.exclude(variant_id=None).values_list("variant_id", flat=True).distinct()
    )
    variant_choices = []
    if existing_variant_ids:
        for v in ProductVariant.objects.select_related("product").filter(id__in=existing_variant_ids):
            label = f"{v.product.sku} — {v.name_with_weight}"
            variant_choices.append((f"v:{v.id}", label))

    choices = [("", "— виберіть товар —"), *variant_choices, *product_choices]

    initial = []
    for it in order.items.select_related("product", "variant").all():
        key = f"v:{it.variant_id}" if it.variant_id else f"p:{it.product_id}"
        initial.append({"sku": key, "qty": int(it.qty or 0)})

    if request.method == "POST":
        formset = AdminOrderLineFormSet(
            request.POST,
            prefix="line",
            form_kwargs={"product_choices": choices},
        )
        if formset.is_valid():
            raw_lines = []
            for f in formset.forms:
                if not f.cleaned_data or f.cleaned_data.get("DELETE"):
                    continue
                key = (f.cleaned_data.get("sku") or "").strip()
                qty = int(f.cleaned_data.get("qty") or 0)
                if not key or qty <= 0:
                    continue
                raw_lines.append((key, qty))

            if not raw_lines:
                messages.error(request, "Додайте хоча б один рядок (товар + кількість).")
                return render(request, "b2b/order_admin_edit.html", {"order": order, "formset": formset})

            # Aggregate duplicates to avoid unique_together conflicts.
            aggregated = {}  # key=(product_id, variant_id or 0) -> dict
            errors = []
            for key, qty in raw_lines:
                product = None
                variant = None
                attrs = {}

                if key.startswith("v:"):
                    try:
                        vid = int(key.split(":", 1)[1])
                        variant = ProductVariant.objects.select_related("product").get(id=vid)
                        product = variant.product
                        attrs = variant.attributes or {}
                    except Exception:
                        errors.append(f"Варіант не знайдено: {key}")
                        continue
                elif key.startswith("p:"):
                    try:
                        pid = int(key.split(":", 1)[1])
                        product = Product.objects.get(id=pid)
                    except Exception:
                        errors.append(f"Товар не знайдено: {key}")
                        continue
                else:
                    errors.append(f"Некоректне значення: {key}")
                    continue

                price = None
                if variant and (variant.wholesale_price or 0) > 0:
                    price = variant.wholesale_price
                else:
                    price = product.wholesale_price

                try:
                    price_val = Decimal(str(price or 0))
                except Exception:
                    price_val = Decimal("0")

                if price_val <= 0:
                    errors.append(f"Для {product.sku} не задано гуртову ціну.")
                    continue

                agg_key = (int(product.id), int(variant.id) if variant else 0)
                if agg_key not in aggregated:
                    aggregated[agg_key] = {
                        "product": product,
                        "variant": variant,
                        "qty": 0,
                        "price": price_val,
                        "attrs": attrs,
                    }
                aggregated[agg_key]["qty"] += int(qty)

            if errors:
                for e in errors:
                    messages.error(request, e)
                return render(request, "b2b/order_admin_edit.html", {"order": order, "formset": formset})

            try:
                with transaction.atomic():
                    # 1) Release existing reservations tied to current items
                    wh.release_order_reservations(order, reason="manual edit")

                    # 2) Replace items
                    order.items.all().delete()
                    for rec in aggregated.values():
                        OrderItem.objects.create(
                            order=order,
                            product=rec["product"],
                            variant=rec["variant"],
                            qty=int(rec["qty"]),
                            price=rec["price"],
                            variant_attrs=rec["attrs"] or {},
                        )

                    order.recalc()

                    # 3) Reserve again for new items
                    wh.ensure_order_reserved(order)

            except wh.WarehouseError as e:
                messages.error(request, f"Не вдалося перерахувати резерв: {e}")
                return render(request, "b2b/order_admin_edit.html", {"order": order, "formset": formset})

            messages.success(request, "Замовлення оновлено. Резерв перераховано.")
            return redirect("b2b:order_detail", order_id=order.id)

    else:
        formset = AdminOrderLineFormSet(
            prefix="line",
            initial=initial,
            form_kwargs={"product_choices": choices},
        )

    return render(request, "b2b/order_admin_edit.html", {"order": order, "formset": formset})



@user_passes_test(_is_staff)
def order_admin_edit_shipping(request, order_id: int):
    order = get_object_or_404(Order, id=order_id)

    default_addr = (
        Address.objects.filter(dealer=order.dealer)
        .order_by('-is_default', '-created_at')
        .first()
    )
    dealer_addresses = Address.objects.filter(dealer=order.dealer).order_by('-is_default', '-created_at')

    if request.method == 'POST' and request.POST.get('copy_from_address'):
        addr_id = request.POST.get('address_id')
        if addr_id:
            addr = get_object_or_404(Address, id=addr_id, dealer=order.dealer)
        else:
            addr = default_addr
        if not addr:
            messages.error(request, 'У клієнта немає збережених адрес.')
            return redirect('b2b:order_admin_edit_shipping', order_id=order.id)

        order.shipping_address = addr
        order.shipping_city = addr.city_name
        order.shipping_city_ref = addr.city_ref or ''
        order.shipping_warehouse = addr.warehouse_name
        order.shipping_warehouse_ref = addr.warehouse_ref or ''
        order.shipping_recipient = addr.recipient_name
        order.shipping_phone = _np_phone_digits(addr.recipient_phone)
        order.save(update_fields=[
            'shipping_address',
            'shipping_city', 'shipping_city_ref',
            'shipping_warehouse', 'shipping_warehouse_ref',
            'shipping_recipient', 'shipping_phone',
        ])
        messages.success(request, 'Адресу доставки оновлено з профілю клієнта.')
        return redirect('b2b:order_detail', order_id=order.id)

    initial = {
        'shipping_city': order.shipping_city or '',
        'shipping_city_ref': order.shipping_city_ref or '',
        'shipping_warehouse': order.shipping_warehouse or '',
        'shipping_warehouse_ref': order.shipping_warehouse_ref or '',
        'shipping_recipient': order.shipping_recipient or '',
        'shipping_phone': _np_phone_digits(order.shipping_phone or ''),
    }

    form = OrderShippingForm(initial=initial)
    if request.method == 'POST':
        form = OrderShippingForm(request.POST)
        if form.is_valid():
            order.shipping_address = None
            order.shipping_city = form.cleaned_data['shipping_city']
            order.shipping_city_ref = form.cleaned_data['shipping_city_ref']
            order.shipping_warehouse = form.cleaned_data['shipping_warehouse']
            order.shipping_warehouse_ref = form.cleaned_data['shipping_warehouse_ref']
            order.shipping_recipient = form.cleaned_data['shipping_recipient']
            order.shipping_phone = _np_phone_digits(form.cleaned_data['shipping_phone'])
            order.save(update_fields=[
                'shipping_address',
                'shipping_city', 'shipping_city_ref',
                'shipping_warehouse', 'shipping_warehouse_ref',
                'shipping_recipient', 'shipping_phone',
            ])
            messages.success(request, 'Адресу доставки збережено.')
            return redirect('b2b:order_detail', order_id=order.id)

    return render(
        request,
        'b2b/order_admin_shipping.html',
        {
            'order': order,
            'form': form,
            'dealer_addresses': dealer_addresses,
            'default_addr': default_addr,
        },
    )
def _render_invoice_pdf_bytes(request, order):
    """Render invoice HTML to PDF bytes; return None if WeasyPrint not available."""
    if not WEASYPRINT_AVAILABLE:
        return None
    html_string = render(request, "b2b/invoice_print.html", {"order": order}).content.decode("utf-8")
    return HTML(string=html_string, base_url=request.build_absolute_uri("/")).write_pdf()


@user_passes_test(_is_staff)
@require_http_methods(["POST"])
@transaction.atomic
def order_admin_action(request, order_id: int, action: str):
    """
    Admin actions:
    - confirm: submitted -> pending_payment (email invoice to customer)
    - cancel: submitted/pending_payment -> cancelled (restock)
    - ship:   pending_payment -> shipped (create TTN and notify customer)
    """
    order = get_object_or_404(Order, id=order_id)
    if action == "confirm":
        if order.status != "submitted":
            messages.error(request, "Можна підтвердити лише замовлення у статусі 'Надіслано'.")
            return redirect("b2b:orders_admin")
        # Ensure FIFO reservations exist so stock levels are updated.
        try:
            wh.ensure_order_reserved(order)
        except Exception as e:
            messages.error(request, f"Не вдалося зарезервувати залишки: {e}")
            return redirect("b2b:orders_admin")
        order.status = "pending_payment"
        order.save(update_fields=["status"])

        # Email invoice with PDF attachment (best-effort)
        try:
            if order.dealer.email:
                msg = EmailMessage(
                    subject=f"Замовлення #{order.id} підтверджене.",
                    body=f"Доброго дня! \n Ваше замовлення {order.id} підтверджене та очікує на оплату."
                         f" \n Будь ласка, оплатіть замовлення для подальшого відвантаження. \n"
                         f" \n https://herabuna.com.ua/orders/{order.id}/invoice/",
                    from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
                    to=[order.dealer.email],
                )
                msg.send(fail_silently=True)
        except Exception:
            pass
        # inside action == "confirm" after sending email
        try:
            if getattr(order.dealer, "telegram_chat_id", None):
                tg.send_message(order.dealer.telegram_chat_id,
                                f"Ваше замовлення #{order.id} підтверджено. Очікує оплату.")
        except Exception:
            pass
        messages.success(request, f"Замовлення #{order.id} підтверджено. Статус: очікує оплату.")
        return redirect("b2b:orders_admin")

    elif action == "cancel":
        if order.status not in {"submitted", "pending_payment"}:
            messages.error(request, "Скасовувати можна лише 'Надіслано' або 'Очікує оплату'.")
            return redirect("b2b:orders_admin")

        # Return lots and sync aggregate stock (FIFO-aware)
        try:
            wh.cancel_order(order)  # puts back reserved lots, updates Product.stock_qty
        except Exception as e:
            messages.error(request, f"Помилка повернення товарів: {e}")
            return redirect("b2b:orders_admin")

        # NOTE: WooCommerce sync is catalog-only in this project.

        order.status = "cancelled"
        order.save(update_fields=["status"])
        messages.info(request, f"Замовлення #{order.id} скасовано. Товари повернуті на склад.")
        return redirect("b2b:orders_admin")


    elif action == "ship":
        if order.status != "pending_payment":
            messages.error(request, "Відвантажити можна лише замовлення, що очікує оплату.")
            return redirect("b2b:orders_admin")

        # Validate NP recipient snapshot before creating TTN.
        if not _np_recipient_ready(order):
            messages.error(request, 'Заповніть адресу доставки (Нова Пошта) перед відвантаженням.')
            return redirect('b2b:order_admin_edit_shipping', order_id=order.id)

        # Ensure we have valid reservations before generating TTN.
        try:
            wh.ensure_order_reserved(order)
        except Exception as e:
            messages.error(request, f"Не вдалося зарезервувати залишки: {e}")
            return redirect("b2b:orders_admin")

        # Create TTN first (fail fast if NP rejects)
        try:
            ttn, doc_ref = np_api.create_ttn(order)
        except Exception as e:
            messages.error(request, f"Помилка створення ТТН: {e}")
            return redirect("b2b:orders_admin")

        # Finalize lot movements (write-off) and sync aggregate stock
        try:
            wh.ship_order(order)  # consumes reserved lots, freezes COGS on items if not yet set
        except Exception as e:
            messages.error(request, f"Помилка списання партій: {e}")
            return redirect("b2b:orders_admin")

        # Persist shipping data and status
        order.shipping_ttn = ttn
        order.shipping_np_ref = doc_ref or ""
        order.shipped_at = timezone.now()
        order.status = "shipped"
        order.save(update_fields=["shipping_ttn", "shipping_np_ref", "shipped_at", "status"])

        # NOTE: WooCommerce sync is catalog-only in this project.

        # Notify customer about shipment
        try:
            if order.dealer.email:
                body = f"Ваше замовлення #{order.id} відправлено. ТТН: {order.shipping_ttn}"
                send_mail(
                    subject=f"Замовлення #{order.id} відправлено",
                    message=body,
                    from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
                    recipient_list=[order.dealer.email],
                    fail_silently=True,
                )
        except Exception:
            pass

        messages.success(request, f"Замовлення #{order.id} відвантажено. ТТН: {order.shipping_ttn}")
        return redirect("b2b:orders_admin")


    else:
        return HttpResponse("Unknown action", status=400)


@user_passes_test(_is_staff)
@require_http_methods(["POST"])
def product_update_inline(request, product_id: int):
    """Staff inline update for prices/active from catalog list (stock is managed by warehouse)."""
    p = get_object_or_404(Product, id=product_id)

    # Wholesale selling price
    try:
        p.wholesale_price = Decimal(request.POST.get("wholesale_price", p.wholesale_price))
    except Exception:
        pass

    # Purchase cost price (for quick reference; real COGS is computed from lots)
    try:
        p.cost_price = Decimal(request.POST.get("cost_price", p.cost_price))
    except Exception:
        pass

    # Stock editing is intentionally disabled (warehouse / lots control stock movements).
    if "stock_qty" in request.POST:
        messages.info(request, "Зміна залишків вимкнена. Оприбуткування/списання робиться у 'Склад'.")

    p.is_active = bool(request.POST.get("is_active"))
    p.save(update_fields=["wholesale_price", "cost_price", "is_active"])
    messages.success(request, f"Збережено: {p.sku}")
    return redirect(_safe_next_url(request))



@user_passes_test(_is_staff)
def order_set_status(request, order_id, status):
    # Deprecated by order_admin_action; keep for compatibility if referenced.
    order = get_object_or_404(Order, id=order_id)
    valid = {"draft", "submitted", "pending_payment", "shipped", "cancelled"}
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

# ---------- CHECKOUT (address selection) ----------
@login_required
def order_checkout(request):
    """
    Step between cart and submit: choose delivery address.
    Button label in cart becomes 'Підтвердити' (goes here).
    """
    order = Order.objects.filter(dealer=request.user, status="draft").first()
    addrs = Address.objects.filter(dealer=request.user).order_by("-is_default", "title")
    if not order or order.items.count() == 0:
        messages.info(request, "Кошик порожній.")
        return redirect("b2b:product_list")
    if not addrs:
        messages.warning(request, "Додайте адресу доставки у профілі.")
        return redirect("b2b:address_list")
    return render(request, "b2b/checkout_select_address.html", {"order": order, "addresses": addrs})

@login_required
@require_http_methods(["POST"])
@transaction.atomic
def order_checkout_confirm(request):
    """
    Confirm address and submit the order:
    - validate stock
    - reserve stock
    - set status submitted
    - attach shipping_address
    - notify admins (email + Telegram)
    - push stock to Woo
    """
    order = Order.objects.filter(dealer=request.user, status="draft").first()
    addr_id = request.POST.get("address_id")
    if not order or order.items.count() == 0:
        return redirect("b2b:product_list")

    addr = get_object_or_404(Address, pk=addr_id, dealer=request.user)

    # Check availability
    for it in order.items.select_related("product", "variant"):
        if it.variant:
            available = max(0, min(int(it.product.stock_qty or 0), int(it.variant.stock_qty or 0)))
        else:
            available = max(0, int(it.product.stock_qty or 0))
        if available < it.qty:
            messages.error(request, f"Недостатньо на складі для {it.product.sku}. Доступно: {available}")
            return redirect("b2b:cart")

    # Reserve lots via FIFO and snapshot COGS on items
    wh.reserve_order(order)  # updates Product.stock_qty internally

    # Snapshot shipping address on the order
    order.status = "submitted"
    order.shipping_address = addr
    order.shipping_city = addr.city_name
    order.shipping_city_ref = addr.city_ref or ""
    order.shipping_warehouse = addr.warehouse_name
    order.shipping_warehouse_ref = addr.warehouse_ref or ""
    order.shipping_recipient = addr.recipient_name
    order.shipping_phone = _np_phone_digits(addr.recipient_phone)

    # Recalculate totals and persist
    order.recalc()
    order.save(update_fields=[
        "status",
        "shipping_address",
        "shipping_city", "shipping_city_ref",
        "shipping_warehouse", "shipping_warehouse_ref",
        "shipping_recipient", "shipping_phone",
        "subtotal", "total",
    ])

    # NOTE: WooCommerce sync is catalog-only in this project.

    # Notify admins: email + Telegram
    try:
        admin_email = getattr(settings, "ORDER_NOTIFY_EMAIL", None) or (settings.ADMINS[0][1] if getattr(settings, "ADMINS", None) else None)
        if admin_email:
            lines = [
                f"Нове замовлення #{order.id}",
                f"Клієнт: {order.dealer.username} ({order.dealer.email})",
                f"Адреса: {addr.city_name}, {addr.warehouse_name}",
                "",
            ]
            for it in order.items.select_related("product", "variant"):
                name = it.variant.name_with_weight if it.variant else it.product.name_with_weight
                lines.append(f"- {it.product.sku} | {name} | {it.qty} × {it.price} = {it.line_total}")
            lines.append("")
            lines.append(f"Сума: {order.total}")
            send_mail(
                subject=f"Нове замовлення #{order.id}",
                message="\n".join(lines),
                from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
                recipient_list=[admin_email],
                fail_silently=True,
            )
        # Telegram admin
        tg.notify_admins(f"Нове замовлення #{order.id}\nКлієнт: {order.dealer.username}\nСума: {order.total} грн\nАдреса: {addr.city_name}, {addr.warehouse_name}")
    except Exception:
        pass

    messages.success(request, "Замовлення надіслано.")
    return redirect("b2b:order_detail", order_id=order.id)

def np_cities(request):
    """AJAX: search cities by query (q)."""
    q = (request.GET.get("q") or "").strip()
    data = np_client.search_cities(q) if q else []
    return JsonResponse({"results": data})

@login_required
@require_GET
def np_warehouses(request):
    """AJAX: warehouses by city_ref and optional query (q)."""
    city_ref = (request.GET.get("city_ref") or "").strip()
    q = (request.GET.get("q") or "").strip()
    data = np_client.get_warehouses(city_ref, q) if city_ref else []
    return JsonResponse({"results": data})


@user_passes_test(lambda u: u.is_staff)
def order_np_label(request, order_id: int):
    order = get_object_or_404(Order, id=order_id)
    if not (order.shipping_np_ref or order.shipping_ttn):
        return HttpResponse("Немає NP Ref або номера ТТН для цього замовлення.", status=400)
    try:
        pdf = np_api.get_label_100x100_pdf_by_ref(order.shipping_np_ref, ttn_number=order.shipping_ttn)
    except Exception as e:
        return HttpResponse(f"Помилка отримання етикетки: {e}", status=500)
    resp = HttpResponse(pdf, content_type="application/pdf")
    resp["Content-Disposition"] = f'inline; filename="label_{order.id}.pdf"'
    return resp

@login_required
@require_http_methods(["POST"])
@transaction.atomic
def order_delete(request, order_id: int):
    """Allow a dealer to delete their own order if it is draft or cancelled."""
    order = get_object_or_404(Order, id=order_id, dealer=request.user)
    if order.status not in ("draft", "cancelled"):
        messages.error(request, "Замовлення можна видалити лише якщо воно чернетка або скасоване.")
        return redirect("b2b:order_detail", order_id=order.id)

    # No stock changes
    order.delete()
    messages.info(request, "Замовлення видалено.")
    return redirect("b2b:dashboard")

def _bootstrapize_form(form):
    """Add Bootstrap classes to form fields (text/select vs checkbox)."""
    for name, field in form.fields.items():
        w = field.widget
        klass = w.attrs.get("class", "")
        if getattr(w, "input_type", "") == "checkbox":
            w.attrs["class"] = (klass + " form-check-input").strip()
        else:
            # text, email, number, select, textarea
            w.attrs["class"] = (klass + " form-control").strip()

@login_required
def profile(request):
    if request.method == "POST":
        form = ProfileForm(request.POST, instance=request.user)
    else:
        form = ProfileForm(instance=request.user)
    _bootstrapize_form(form)
    if request.method == "POST" and form.is_valid():
        form.save()
        messages.success(request, "Профіль збережено.")
        return redirect("b2b:profile")
    return render(request, "b2b/profile.html", {"form": form})
