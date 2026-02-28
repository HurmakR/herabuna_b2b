import io
import os
import tempfile
from datetime import datetime

from django.contrib import messages
from django.contrib.auth.decorators import user_passes_test
from django.core.management import call_command
from django.db import transaction
from django.http import HttpResponse, HttpResponseBadRequest
from django.shortcuts import redirect, render
from django.views.decorators.http import require_GET, require_POST

from .forms import ConfirmActionForm, ImportBackupForm


def _parse_int_list(values):
    out = []
    for v in values:
        try:
            out.append(int(str(v).strip()))
        except Exception:
            continue
    return out


def _is_superuser(user):
    return user.is_authenticated and user.is_superuser


def _clear_warehouse():
    """Delete warehouse operational data and reset product stock to 0."""
    from warehouse.models import (
        InventoryReservation,
        InventoryMove,
        InventoryLot,
        InboundReceiptLine,
        InboundReceipt,
    )
    from b2b.models import Product

    InventoryReservation.objects.all().delete()
    InventoryMove.objects.all().delete()
    InventoryLot.objects.all().delete()
    InboundReceiptLine.objects.all().delete()
    InboundReceipt.objects.all().delete()
    Product.objects.all().update(stock_qty=0)


def _clear_orders_and_warehouse():
    """Delete orders (and related) plus warehouse data.

    Catalog (products/brands/categories) is preserved.
    """
    from b2b.models import OrderItem, Order
    from audit.models import LoginEvent

    # Orders
    OrderItem.objects.all().delete()
    Order.objects.all().delete()

    # Optional: audit log can be treated as disposable on reset
    LoginEvent.objects.all().delete()

    # Warehouse
    _clear_warehouse()


@user_passes_test(_is_superuser)
@require_GET
def service_dashboard(request):
    context = {
        "reset_form_warehouse": ConfirmActionForm(prefix="warehouse"),
        "reset_form_orders": ConfirmActionForm(prefix="orders"),
        "import_form": ImportBackupForm(),
    }
    return render(request, "reports/service_dashboard.html", context)


@user_passes_test(_is_superuser)
@require_GET
def export_backup(request):
    """Export data as JSON fixture.

    scope:
      - business (default): b2b + warehouse + audit
      - warehouse: only warehouse
    """
    scope = (request.GET.get("scope") or "business").strip().lower()

    if scope not in {"business", "warehouse"}:
        return HttpResponseBadRequest("Invalid scope")

    labels = ["b2b", "warehouse", "audit"] if scope == "business" else ["warehouse"]

    # Exclude Django internals; we keep custom user model (Dealer) as it's in b2b.
    excludes = ["contenttypes", "admin.logentry", "sessions.session", "auth.permission"]

    out = io.StringIO()
    call_command(
        "dumpdata",
        *labels,
        indent=2,
        stdout=out,
        exclude=excludes,
        use_natural_foreign_keys=True,
        use_natural_primary_keys=True,
    )

    payload = out.getvalue()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = f"backup_{scope}_{stamp}.json"

    resp = HttpResponse(payload, content_type="application/json; charset=utf-8")
    resp["Content-Disposition"] = f'attachment; filename="{filename}"'
    return resp


@user_passes_test(_is_superuser)
@require_POST
@transaction.atomic
def import_backup(request):
    form = ImportBackupForm(request.POST, request.FILES)
    if not form.is_valid():
        return render(
            request,
            "reports/service_dashboard.html",
            {
                "reset_form_warehouse": ConfirmActionForm(prefix="warehouse"),
                "reset_form_orders": ConfirmActionForm(prefix="orders"),
                "import_form": form,
            },
        )

    scope = form.cleaned_data["clear_scope"]
    uploaded = form.cleaned_data["file"]

    if scope == "warehouse":
        _clear_warehouse()
    elif scope == "orders":
        _clear_orders_and_warehouse()

    # Store upload in a temp file to feed loaddata
    with tempfile.NamedTemporaryFile(delete=False, suffix=".json") as tmp:
        for chunk in uploaded.chunks():
            tmp.write(chunk)
        tmp_path = tmp.name

    try:
        call_command("loaddata", tmp_path, verbosity=1)
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass

    messages.success(request, "Імпорт виконано успішно.")
    return redirect("reports:service_dashboard")


@user_passes_test(_is_superuser)
@require_POST
@transaction.atomic
def reset_warehouse(request):
    form = ConfirmActionForm(request.POST, prefix="warehouse")
    if not form.is_valid():
        return render(
            request,
            "reports/service_dashboard.html",
            {"reset_form_warehouse": form, "reset_form_orders": ConfirmActionForm(prefix="orders"), "import_form": ImportBackupForm()},
        )

    _clear_warehouse()
    messages.success(request, "Склад обнулено (лоти/рухи/резерви/накладні видалені, stock_qty=0).")
    return redirect("reports:service_dashboard")


@user_passes_test(_is_superuser)
@require_POST
@transaction.atomic
def reset_orders(request):
    form = ConfirmActionForm(request.POST, prefix="orders")
    if not form.is_valid():
        return render(
            request,
            "reports/service_dashboard.html",
            {"reset_form_warehouse": ConfirmActionForm(prefix="warehouse"), "reset_form_orders": form, "import_form": ImportBackupForm()},
        )

    _clear_orders_and_warehouse()
    messages.success(request, "Замовлення та склад очищені. Каталог збережено.")
    return redirect("reports:service_dashboard")


@user_passes_test(_is_superuser)
@require_GET
def service_woo_import(request):
    """Show Woo products that are missing in local catalog (by SKU)."""
    from b2b.services.woo_sync import list_missing_products_from_woo

    missing = list_missing_products_from_woo()
    return render(request, "reports/service_woo_import.html", {"missing": missing})


@user_passes_test(_is_superuser)
@require_POST
def service_woo_import_apply(request):
    """Import selected Woo products into local catalog (catalog only)."""
    from b2b.services.woo_sync import import_missing_products_from_woo

    woo_ids = _parse_int_list(request.POST.getlist("woo_ids"))
    if not woo_ids:
        messages.warning(request, "Нічого не вибрано для імпорту.")
        return redirect("reports:service_woo_import")

    res = import_missing_products_from_woo(woo_ids=woo_ids)
    messages.success(
        request,
        (
            f"Імпорт виконано: створено {res.created}, "
            f"прив'язано по SKU {res.linked_by_sku}, "
            f"пропущено {res.skipped_existing}. "
            f"Категорій створено {res.categories_created}, брендів {res.brands_created}."
        ),
    )
    return redirect("reports:service_woo_import")


@user_passes_test(_is_superuser)
@require_GET
def service_marketplace_orders(request):
    from django.db.models import Sum
    from b2b.models import Order
    from warehouse.models import InventoryReservation

    channel = (request.GET.get("channel") or "").strip().lower()
    days = int(request.GET.get("days") or 14)

    qs = Order.objects.filter(channel__in=["woo", "rozetka"]).order_by("-created_at").select_related("dealer")
    from datetime import timedelta
    from django.utils import timezone as dj_timezone
    if days and days > 0:
        qs = qs.filter(created_at__gte=dj_timezone.now() - timedelta(days=days))
    if channel in {"woo", "rozetka"}:
        qs = qs.filter(channel=channel)

    orders = list(qs[:200])

    # Precompute reserved qty per order (single query)
    res_rows = (
        InventoryReservation.objects
        .filter(order_item__order_id__in=[o.id for o in orders])
        .values("order_item__order_id")
        .annotate(qty=Sum("qty"))
    )
    reserved_map = {int(r["order_item__order_id"]): int(r["qty"] or 0) for r in res_rows}

    for o in orders:
        payload = o.external_payload or {}
        # Django templates disallow access to attributes starting with underscores.
        # Attach computed values using safe attribute names.
        o.reserved_qty = reserved_map.get(o.id, 0)
        o.unmatched_count = len(payload.get("unmatched_items") or [])
        o.sync_error = payload.get("sync_error") or ""

    context = {
        "orders": orders,
        "channel": channel,
        "days": days,
    }
    return render(request, "reports/service_marketplace_orders.html", context)


@user_passes_test(_is_superuser)
@require_POST
def service_marketplace_orders_sync(request):
    from b2b.services.marketplace_sync import sync_rozetka_orders, sync_woo_orders

    source = (request.POST.get("source") or "all").strip().lower()
    days = int(request.POST.get("days") or 14)
    auto_apply = bool(request.POST.get("auto_apply") == "1")

    if source in {"woo", "all"}:
        try:
            res = sync_woo_orders(days=days, auto_apply=auto_apply)
            messages.success(
                request,
                (
                    f"Woo: created={res.created}, updated={res.updated}, "
                    f"reserved={res.reserved}, released={res.released}, "
                    f"skipped_unmapped={res.skipped_unmapped}, errors={len(res.errors)}"
                ),
            )
            for e in res.errors[:5]:
                messages.warning(request, f"Woo: {e}")
        except Exception as e:
            messages.error(request, f"Woo sync error: {e}")

    if source in {"rozetka", "all"}:
        try:
            res = sync_rozetka_orders(days=days, auto_apply=auto_apply, types=1)
            messages.success(
                request,
                (
                    f"Rozetka: created={res.created}, updated={res.updated}, "
                    f"reserved={res.reserved}, released={res.released}, "
                    f"skipped_unmapped={res.skipped_unmapped}, errors={len(res.errors)}"
                ),
            )
            for e in res.errors[:5]:
                messages.warning(request, f"Rozetka: {e}")
        except Exception as e:
            messages.error(request, f"Rozetka sync error: {e}")

    return redirect("reports:service_marketplace_orders")


@user_passes_test(_is_superuser)
@require_POST
def service_marketplace_orders_apply(request):
    from b2b.models import Order
    from b2b.services.marketplace_orders import apply_stock_action

    action = (request.POST.get("action") or "").strip().lower()
    order_ids = _parse_int_list(request.POST.getlist("order_ids"))

    if action not in {"reserve", "release", "ship"}:
        messages.error(request, "Невідома дія.")
        return redirect("reports:service_marketplace_orders")

    if not order_ids:
        messages.warning(request, "Не вибрано жодного замовлення.")
        return redirect("reports:service_marketplace_orders")

    ok = 0
    failed = 0
    for oid in order_ids:
        order = Order.objects.filter(id=oid, channel__in=["woo", "rozetka"]).first()
        if not order:
            continue
        try:
            apply_stock_action(order=order, action=action)
            ok += 1
        except Exception as e:
            failed += 1
            messages.warning(request, f"{order.channel}:{order.external_id} — {e}")

    if ok:
        messages.success(request, f"Готово: {ok} шт.")
    if failed:
        messages.warning(request, f"Помилки: {failed} шт.")

    return redirect("reports:service_marketplace_orders")