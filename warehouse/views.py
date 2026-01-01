from decimal import Decimal

from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Sum, F, DecimalField, ExpressionWrapper, Q
from django.shortcuts import get_object_or_404, redirect, render
from django.contrib import messages

from b2b.models import Product
from .models import InventoryLot
from .forms import ReceiveLotForm, AdjustStockForm
from . import services as wh


@staff_member_required
def dashboard(request):
    q = (request.GET.get("q") or "").strip()

    products = Product.objects.all()
    if q:
        products = products.filter(Q(sku__icontains=q) | Q(name__icontains=q))

    stock_cost_expr = ExpressionWrapper(
        F("stock_qty") * F("cost_price"),
        output_field=DecimalField(max_digits=14, decimal_places=2),
    )
    stock_wholesale_expr = ExpressionWrapper(
        F("stock_qty") * F("wholesale_price"),
        output_field=DecimalField(max_digits=14, decimal_places=2),
    )

    products = products.annotate(
        stock_cost=stock_cost_expr,
        stock_wholesale=stock_wholesale_expr,
    ).order_by("brand__name", "name")[:500]

    totals = products.aggregate(
        total_qty=Sum("stock_qty"),
        total_cost=Sum("stock_cost"),
        total_wholesale=Sum("stock_wholesale"),
    )
    totals = {
        "total_qty": totals["total_qty"] or 0,
        "total_cost": totals["total_cost"] or Decimal("0"),
        "total_wholesale": totals["total_wholesale"] or Decimal("0"),
    }

    return render(
        request,
        "warehouse/dashboard.html",
        {
            "q": q,
            "products": products,
            "totals": totals,
        },
    )


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
    initial = {}
    product_id = request.GET.get("product")
    if product_id:
        initial["product"] = product_id

    if request.method == "POST":
        form = ReceiveLotForm(request.POST)
        if form.is_valid():
            try:
                wh.receive_lot(
                    product=form.cleaned_data["product"],
                    qty=form.cleaned_data["qty"],
                    unit_cost=form.cleaned_data["unit_cost"],
                    reference=form.cleaned_data.get("reference") or "",
                    note=form.cleaned_data.get("note") or "",
                    user=request.user,
                )
                messages.success(request, "Оприбуткування виконано.")
                return redirect("warehouse:dashboard")
            except Exception as e:
                messages.error(request, f"Помилка: {e}")
    else:
        form = ReceiveLotForm(initial=initial)

    return render(request, "warehouse/receive.html", {"form": form})


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
