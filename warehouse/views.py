from decimal import Decimal

from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Sum, F, DecimalField, ExpressionWrapper, Q, Count
from django.db.models.functions import Coalesce
from django.forms import modelformset_factory
from django.shortcuts import get_object_or_404, redirect, render
from django.contrib import messages

from b2b.models import Product
from .models import InventoryLot, InboundReceipt
from .forms import AdjustStockForm, ReceiptHeaderForm, ReceiptLineForm
from .services import receive_receipt
from . import services as wh

@staff_member_required
def dashboard(request):
    """Warehouse dashboard = inbound receipts journal + inventory totals."""

    q = (request.GET.get("q") or "").strip()

    # Inventory totals
    total_qty = (
        InventoryLot.objects.aggregate(
            total=Sum(F("qty_in") - F("qty_reserved") - F("qty_out"))
        ).get("total")
        or 0
    )

    total_cost = (
        InventoryLot.objects.aggregate(
            total=Sum(
                ExpressionWrapper(
                    (F("qty_in") - F("qty_reserved") - F("qty_out")) * F("unit_cost"),
                    output_field=DecimalField(max_digits=14, decimal_places=2),
                )
            )
        ).get("total")
        or Decimal("0")
    )

    total_wholesale = (
        Product.objects.aggregate(
            total=Sum(
                ExpressionWrapper(
                    F("stock_qty") * F("wholesale_price"),
                    output_field=DecimalField(max_digits=14, decimal_places=2),
                )
            )
        ).get("total")
        or Decimal("0")
    )

    # Receipts journal
    receipts = InboundReceipt.objects.all()
    if q:
        receipts = receipts.filter(
            Q(supplier__icontains=q) | Q(external_ref__icontains=q) | Q(note__icontains=q)
        )

    line_total_expr = ExpressionWrapper(
        F("lines__qty") * F("lines__unit_cost"),
        output_field=DecimalField(max_digits=14, decimal_places=2),
    )

    receipts = (
        receipts.annotate(
            lines_count=Count("lines", distinct=True),
            total_qty=Coalesce(Sum("lines__qty"), 0),
            total_sum=Coalesce(Sum(line_total_expr), Decimal("0")),
        )
        .order_by("-received_date", "-id")[:200]
    )

    context = {
        "q": q,
        "total_qty": int(total_qty),
        "total_cost": total_cost,
        "total_wholesale": total_wholesale,
        "receipts": receipts,
    }
    return render(request, "warehouse/dashboard.html", context)


@staff_member_required
def product_lots(request, product_id: int):
    product = get_object_or_404(Product, id=product_id)
    lots = InventoryLot.objects.filter(product=product).order_by("received_at", "id")

    return render(
        request,
        "warehouse/product_lots.html",
        {
            "product": product,
            "lots": lots,
        },
    )


@staff_member_required
def receive(request):
    """Deprecated: inbound stock is added only via inbound receipt."""
    messages.info(request, "Оприбуткування окремими лотами вимкнено. Використайте прихідну накладну.")
    return redirect("warehouse:receipt_new")


@staff_member_required
def adjust(request):
    initial = {}
    product_id = request.GET.get("product")
    if product_id:
        initial["product"] = product_id

    if request.method == "POST":
        form = AdjustStockForm(request.POST)
        if form.is_valid():
            try:
                wh.adjust_stock(
                    product=form.cleaned_data["product"],
                    qty_delta=form.cleaned_data["qty_delta"],
                    lot=form.cleaned_data.get("lot"),
                    note=form.cleaned_data.get("note") or "",
                    user=request.user,
                )
                messages.success(request, "Коригування застосовано.")
                return redirect("warehouse:dashboard")
            except Exception as e:
                messages.error(request, f"Помилка: {e}")
    else:
        form = AdjustStockForm(initial=initial)

    return render(request, "warehouse/adjust.html", {"form": form})

@staff_member_required
def receipt_detail(request, receipt_id: int):
    receipt = get_object_or_404(InboundReceipt, id=receipt_id)

    lines = receipt.lines.select_related("product").order_by("id")
    rows = []
    total_qty = 0
    total_sum = Decimal("0")
    for l in lines:
        qty = int(l.qty or 0)
        unit = (l.unit_cost or Decimal("0"))
        line_total = unit * qty
        rows.append({"line": l, "line_total": line_total})
        total_qty += qty
        total_sum += line_total

    return render(
        request,
        "warehouse/receipt_detail.html",
        {
            "receipt": receipt,
            "rows": rows,
            "total_qty": total_qty,
            "total_sum": total_sum,
        },
    )

@staff_member_required
def receive_receipt_view(request):
    LineFormSet = modelformset_factory(
        ReceiptLineForm._meta.model,
        form=ReceiptLineForm,
        extra=1,
        can_delete=True,
    )

    if request.method == "POST":
        header_form = ReceiptHeaderForm(request.POST)
        formset = LineFormSet(request.POST, queryset=ReceiptLineForm._meta.model.objects.none())

        if header_form.is_valid() and formset.is_valid():
            lines = []
            for f in formset.forms:
                if not f.cleaned_data or f.cleaned_data.get("DELETE"):
                    continue
                product = f.cleaned_data.get("product")
                qty = f.cleaned_data.get("qty")
                unit_cost = f.cleaned_data.get("unit_cost")
                if not product or not qty:
                    continue
                lines.append({"product": product, "qty": qty, "unit_cost": unit_cost})

            if not lines:
                messages.error(request, "Додайте хоча б один рядок товару.")
            else:
                receipt = receive_receipt(
                    created_by=request.user,
                    supplier=header_form.cleaned_data.get("supplier") or "",
                    external_ref=header_form.cleaned_data.get("external_ref") or "",
                    note=header_form.cleaned_data.get("note") or "",
                    currency=header_form.cleaned_data.get("currency") or "UAH",
                    received_date=header_form.cleaned_data.get("received_date"),
                    lines=lines,
                )
                messages.success(request, f"Оприбутковано накладну: {receipt}")
                return redirect("warehouse:receipt_detail", receipt_id=receipt.id)

    else:
        header_form = ReceiptHeaderForm()
        formset = LineFormSet(queryset=ReceiptLineForm._meta.model.objects.none())

    return render(
        request,
        "warehouse/receive_receipt.html",
        {"header_form": header_form, "formset": formset},
    )
