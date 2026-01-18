from decimal import Decimal
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Sum, F, DecimalField, ExpressionWrapper, Value, OuterRef, Subquery
from django.db.models.functions import TruncDate, Coalesce
from django.shortcuts import render
from b2b.models import Order, Product, Dealer, Brand
from warehouse.models import InventoryLot


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

    # Margin is calculated from warehouse FIFO cost captured on shipping.
    shipped_qs = qs.filter(status="shipped")
    line_rev_expr = ExpressionWrapper(
        F("items__qty") * F("items__price"),
        output_field=DecimalField(max_digits=14, decimal_places=2),
    )
    line_cost_expr = Coalesce(
        F("items__cost_total"),
        Value(0),
        output_field=DecimalField(max_digits=14, decimal_places=2),
    )
    line_margin_expr = ExpressionWrapper(
        line_rev_expr - line_cost_expr,
        output_field=DecimalField(max_digits=14, decimal_places=2),
    )
    total_margin = shipped_qs.aggregate(total=Sum(line_margin_expr))["total"] or Decimal("0")

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
    """Stock report: quantities and values, with chart by brand.

    Notes:
      - Quantity and wholesale totals are computed from Product fields.
      - Cost totals are computed from InventoryLot remaining quantities * unit_cost.
      - Per-product cost is computed via Subquery, to avoid aggregate-on-aggregate issues.
    """

    base_qs = Product.objects.select_related("brand").all()

    brand_id = request.GET.get("brand") or ""
    only_in_stock = request.GET.get("only_in_stock") == "1"

    if brand_id:
        base_qs = base_qs.filter(brand_id=brand_id)
    if only_in_stock:
        base_qs = base_qs.filter(stock_qty__gt=0)

    stock_wholesale_expr = ExpressionWrapper(
        F("stock_qty") * F("wholesale_price"),
        output_field=DecimalField(max_digits=14, decimal_places=2),
    )

    lot_available_expr = F("qty_in") - F("qty_reserved") - F("qty_out")
    lot_cost_expr = ExpressionWrapper(
        lot_available_expr * F("unit_cost"),
        output_field=DecimalField(max_digits=14, decimal_places=2),
    )

    last_unit_cost_sq = Subquery(
        InventoryLot.objects.filter(product_id=OuterRef("pk"))
        .order_by("-received_at", "-id")
        .values("unit_cost")[:1]
    )

    stock_cost_sq = Subquery(
        InventoryLot.objects.filter(product_id=OuterRef("pk"))
        .values("product_id")
        .annotate(cost=Sum(lot_cost_expr))
        .values("cost")[:1]
    )

    products_qs = base_qs.annotate(
        last_unit_cost=last_unit_cost_sq,
        stock_cost=Coalesce(
            stock_cost_sq,
            Value(0),
            output_field=DecimalField(max_digits=14, decimal_places=2),
        ),
        stock_wholesale=stock_wholesale_expr,
    )

    # Totals
    total_qty = base_qs.aggregate(total=Sum("stock_qty"))["total"] or 0
    total_wholesale = base_qs.aggregate(total=Sum(stock_wholesale_expr))["total"] or Decimal("0")

    lot_total_cost = (
        InventoryLot.objects.filter(product_id__in=base_qs.values("id"))
        .aggregate(total=Sum(lot_cost_expr))["total"]
        or Decimal("0")
    )

    # By brand (avoid join-duplication for qty/wholesale by using Product-only query,
    # and compute cost by brand from lots).
    by_brand_map = {}

    for row in base_qs.values("brand__name").annotate(
        qty=Sum("stock_qty"),
        wholesale=Sum(stock_wholesale_expr),
    ):
        name = row["brand__name"] or "Без бренду"
        by_brand_map[name] = {
            "brand__name": name,
            "qty": row["qty"] or 0,
            "wholesale": row["wholesale"] or Decimal("0"),
            "cost": Decimal("0"),
        }

    for row in (
        InventoryLot.objects.filter(product_id__in=base_qs.values("id"))
        .values("product__brand__name")
        .annotate(cost=Sum(lot_cost_expr))
    ):
        name = row["product__brand__name"] or "Без бренду"
        by_brand_map.setdefault(
            name,
            {"brand__name": name, "qty": 0, "wholesale": Decimal("0"), "cost": Decimal("0")},
        )
        by_brand_map[name]["cost"] = row["cost"] or Decimal("0")

    by_brand = sorted(by_brand_map.values(), key=lambda r: r["cost"], reverse=True)

    chart_labels = [row["brand__name"] for row in by_brand]
    chart_values = [float(row["cost"] or 0) for row in by_brand]

    brands = Brand.objects.order_by("name")

    context = {
        "products": products_qs.order_by("brand__name", "name")[:500],
        "brand_id": brand_id,
        "only_in_stock": only_in_stock,
        "brands": brands,
        "total_qty": total_qty,
        "total_cost": lot_total_cost,
        "total_wholesale": total_wholesale,
        "by_brand": by_brand,
        "chart_labels": chart_labels,
        "chart_values": chart_values,
    }
    return render(request, "reports/stock_report.html", context)
