from decimal import Decimal
from django.conf import settings
from django.contrib import messages
from django.core.mail import EmailMessage, send_mail
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required, user_passes_test
from django.db import transaction
from django.db.models import Q
from django.http import HttpResponse, HttpResponseForbidden
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_http_methods
from django.http import JsonResponse
from django.views.decorators.http import require_GET
from .services import np_client
from django.core.paginator import Paginator
from urllib.parse import urlencode

try:
    from weasyprint import HTML
    WEASYPRINT_AVAILABLE = True
except Exception:
    WEASYPRINT_AVAILABLE = False

from django.forms import modelform_factory
from .forms import DealerSignUpForm, ProfileForm, AddressForm
from .models import Brand, Category, Order, OrderItem, Product, ProductVariant, Address
from .services import woo_sync, np_api, telegram as tg


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
            user.is_active = True  # set False to require admin approval
            user.is_dealer = True
            user.save()
            try:
                admin_email = getattr(settings, "ORDER_NOTIFY_EMAIL", None)
                if admin_email:
                    send_mail(
                        subject="Нова реєстрація дилера",
                        message=f"Користувач {user.username} ({user.email}) зареєструвався.",
                        from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
                        recipient_list=[admin_email],
                        fail_silently=True,
                    )
            except Exception:
                pass
            login(request, user)
            return redirect("b2b:dashboard")
    else:
        form = DealerSignUpForm()
    return render(request, "b2b/signup.html", {"form": form})


@login_required
def dashboard(request):
    # Show only non-draft orders; draft is the cart.
    qs = request.user.order_set.exclude(status="draft").order_by("-created_at")[:20]
    return render(request, "b2b/dashboard.html", {"orders": qs})

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
        available = max(0, int(variant.stock_qty))
    if available <= 0:
        messages.info(request, "Немає в наявності для обраної комбінації.")
        return redirect(_safe_next_url(request))
    price = (variant.wholesale_price if variant else product.wholesale_price)
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
        available = max(0, int(it.variant.stock_qty if it.variant else it.product.stock_qty))
        if available < it.qty:
            messages.error(request, f"Недостатньо на складі для {it.product.sku}. Доступно: {available}")
            return redirect("b2b:cart")
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
    # Push stock to Woo (best-effort)
    client = woo_sync.WooClient()
    for it in order.items.select_related("product", "variant"):
        try:
            if it.variant and it.product.woo_id:
                client.update_variation_stock(it.product.woo_id, it.variant.woo_variation_id, it.variant.stock_qty)
            elif it.product.woo_id:
                client.update_stock(it.product.woo_id, it.product.stock_qty)
        except Exception:
            pass
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
        order.status = "pending_payment"
        order.save(update_fields=["status"])

        # Email invoice with PDF attachment (best-effort)
        try:
            if order.dealer.email:
                msg = EmailMessage(
                    subject=f"Рахунок на оплату #{order.id}",
                    body="Доброго дня! Надсилаємо рахунок на оплату. Будь ласка, оплатіть для подальшого відвантаження.",
                    from_email=getattr(settings, "DEFAULT_FROM_EMAIL", None),
                    to=[order.dealer.email],
                )
                pdf = _render_invoice_pdf_bytes(request, order)
                if pdf:
                    msg.attach(f"invoice_{order.id}.pdf", pdf, "application/pdf")
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

        # Restock items
        for it in order.items.select_related("product", "variant"):
            if it.variant:
                it.variant.stock_qty += it.qty
                it.variant.save(update_fields=["stock_qty"])
            else:
                it.product.stock_qty += it.qty
                it.product.save(update_fields=["stock_qty"])
            # Push to Woo best-effort
            try:
                client = woo_sync.WooClient()
                if it.variant and it.product.woo_id:
                    client.update_variation_stock(it.product.woo_id, it.variant.woo_variation_id, it.variant.stock_qty)
                elif it.product.woo_id:
                    client.update_stock(it.product.woo_id, it.product.stock_qty)
            except Exception:
                pass

        order.status = "cancelled"
        order.save(update_fields=["status"])
        messages.info(request, f"Замовлення #{order.id} скасовано. Товари повернуті на склад.")
        return redirect("b2b:orders_admin")

    elif action == "ship":
        if order.status != "pending_payment":
            messages.error(request, "Відвантажити можна лише замовлення, що очікує оплату.")
            return redirect("b2b:orders_admin")

        # Create TTN (stub)
        try:
            ttn, doc_ref = np_api.create_ttn(order)
        except Exception as e:
            messages.error(request, f"Помилка створення ТТН: {e}")
            return redirect("b2b:orders_admin")

        order.shipping_ttn = ttn
        order.shipping_np_ref = doc_ref or ""
        order.shipped_at = timezone.now()
        order.status = "shipped"
        order.save(update_fields=["shipping_ttn", "shipping_np_ref", "shipped_at", "status"])

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
        available = max(0, int(it.variant.stock_qty if it.variant else it.product.stock_qty))
        if available < it.qty:
            messages.error(request, f"Недостатньо на складі для {it.product.sku}. Доступно: {available}")
            return redirect("b2b:cart")

    # Reserve locally
    for it in order.items.select_related("product", "variant"):
        if it.variant:
            it.variant.stock_qty -= it.qty
            it.variant.save(update_fields=["stock_qty"])
        else:
            it.product.stock_qty -= it.qty
            it.product.save(update_fields=["stock_qty"])

    order.status = "submitted"
    order.shipping_address = addr
    order.shipping_city = addr.city_name
    order.shipping_city_ref = addr.city_ref or ""
    order.shipping_warehouse = addr.warehouse_name
    order.shipping_warehouse_ref = addr.warehouse_ref or ""
    order.shipping_recipient = addr.recipient_name
    order.shipping_phone = addr.recipient_phone
    order.recalc()
    order.save(update_fields=[
        "status", "shipping_address",
        "shipping_city", "shipping_city_ref",
        "shipping_warehouse", "shipping_warehouse_ref",
        "shipping_recipient", "shipping_phone",
        "subtotal", "total",
    ])
    order.recalc()
    order.save(update_fields=["status", "shipping_address", "subtotal", "total"])

    # Push stock to Woo (best-effort)
    client = woo_sync.WooClient()
    for it in order.items.select_related("product", "variant"):
        try:
            if it.variant and it.product.woo_id:
                client.update_variation_stock(it.product.woo_id, it.variant.woo_variation_id, it.variant.stock_qty)
            elif it.product.woo_id:
                client.update_stock(it.product.woo_id, it.product.stock_qty)
        except Exception:
            pass

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
