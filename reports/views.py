from decimal import Decimal
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Sum, F, DecimalField, ExpressionWrapper
from django.db.models.functions import TruncDate
from django.shortcuts import render
from django.utils import timezone

from b2b.models import Order, Product, Dealer, Brand


def _is_staff(user):
    return user.is_staff


@staff_member_required
def sales_report(request):
    """Sales report with filters and chart by day."""
    qs = Order.objects.select_related("dealer").filter(
        status__in=["submitted", "pending_payment", "shipped"]
    )

    date_from = request.GET.get("date_from") or ""
    date_to = request.GET.get("date_to") or ""
    dealer_id = request.GET.get("dealer") or ""

    if date_from:
        qs = qs.filter(created_at__date__gte=date_from)
    if date_to:
        qs = qs.filter(created_at__date__lte=date_to)
    if dealer_id:
        qs = qs.filter(dealer_id=dealer_id)

    # Aggregate totals
    total_sales = qs.aggregate(total=Sum("total"))["total"] or Decimal("0")

    # If margin is already counted on items level you can plug it here later.
    total_margin = Decimal("0")

    # Group by day for chart
    by_day = (
        qs.annotate(day=TruncDate("created_at"))
          .values("day")
          .annotate(day_total=Sum("total"))
          .order_by("day")
    )

    chart_labels = [row["day"].strftime("%d.%m") for row in by_day]
    chart_totals = [float(row["day_total"] or 0) for row in by_day]

    # Group by dealer for table
    by_dealer = (
        qs.values("dealer__id", "dealer__username")
          .annotate(dealer_total=Sum("total"))
          .order_by("-dealer_total")
    )

    dealers = Dealer.objects.filter(is_dealer=True).order_by("username")

    context = {
        "orders": qs.select_related("dealer").order_by("-created_at")[:200],
        "date_from": date_from,
        "date_to": date_to,
        "dealer_id": dealer_id,
        "dealers": dealers,
        "total_sales": total_sales,
        "total_margin": total_margin,
        "by_dealer": by_dealer,
        "chart_labels": chart_labels,
        "chart_totals": chart_totals,
    }
    return render(request, "reports/sales_report.html", context)


@staff_member_required
def stock_report(request):
    """Stock report: quantities and values, with chart by brand."""
    qs = Product.objects.select_related("brand").all()

    brand_id = request.GET.get("brand") or ""
    only_in_stock = request.GET.get("only_in_stock") == "1"

    if brand_id:
        qs = qs.filter(brand_id=brand_id)
    if only_in_stock:
        qs = qs.filter(stock_qty__gt=0)

    # Per-row stock value expressions
    stock_cost_expr = ExpressionWrapper(
        F("stock_qty") * F("cost_price"),
        output_field=DecimalField(max_digits=14, decimal_places=2),
    )
    stock_wholesale_expr = ExpressionWrapper(
        F("stock_qty") * F("wholesale_price"),
        output_field=DecimalField(max_digits=14, decimal_places=2),
    )

    # Annotate every product once – so шаблон просто читає поля
    qs = qs.annotate(
        stock_cost=stock_cost_expr,
        stock_wholesale=stock_wholesale_expr,
    )

    # Totals
    total = qs.aggregate(
        total_qty=Sum("stock_qty"),
        total_cost=Sum("stock_cost"),
        total_wholesale=Sum("stock_wholesale"),
    )
    total_qty = total["total_qty"] or 0
    total_cost = total["total_cost"] or Decimal("0")
    total_wholesale = total["total_wholesale"] or Decimal("0")

    # Group by brand for chart
    by_brand = (
        qs.values("brand__name")
          .annotate(
              qty=Sum("stock_qty"),
              cost=Sum("stock_cost"),
              wholesale=Sum("stock_wholesale"),
          )
          .order_by("-cost")
    )

    chart_labels = [row["brand__name"] or "Без бренду" for row in by_brand]
    chart_values = [float(row["cost"] or 0) for row in by_brand]

    brands = Brand.objects.order_by("name")

    context = {
        "products": qs.order_by("brand__name", "name")[:500],
        "brand_id": brand_id,
        "only_in_stock": only_in_stock,
        "brands": brands,
        "total_qty": total_qty,
        "total_cost": total_cost,
        "total_wholesale": total_wholesale,
        "by_brand": by_brand,
        "chart_labels": chart_labels,
        "chart_values": chart_values,
    }
    return render(request, "reports/stock_report.html", context)