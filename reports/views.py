from decimal import Decimal
from django.contrib.admin.views.decorators import staff_member_required
from django.db.models import Sum, F, DecimalField, ExpressionWrapper, Value, OuterRef, Subquery, Count, Avg
from django.db.models.functions import TruncDate, Coalesce
from django.shortcuts import render
from b2b.models import Order, Product, Dealer, Brand
from warehouse.models import InventoryLot


def _is_staff(user):
    return user.is_staff


@staff_member_required
def sales_report(request):
    """Sales report.

    The report is split into:
      - Orders created in the selected period (for operational view)
      - Orders shipped in the selected period (for financial view: revenue/COGS/margin)
    """

    created_qs = Order.objects.select_related("dealer").filter(
        status__in=["submitted", "pending_payment", "shipped"]
    )

    date_from = request.GET.get("date_from") or ""
    date_to = request.GET.get("date_to") or ""
    dealer_id = request.GET.get("dealer") or ""

    if date_from:
        created_qs = created_qs.filter(created_at__date__gte=date_from)
    if date_to:
        created_qs = created_qs.filter(created_at__date__lte=date_to)
    if dealer_id:
        created_qs = created_qs.filter(dealer_id=dealer_id)

    shipped_qs = Order.objects.select_related("dealer").filter(status="shipped")
    if date_from:
        shipped_qs = shipped_qs.filter(shipped_at__date__gte=date_from)
    if date_to:
        shipped_qs = shipped_qs.filter(shipped_at__date__lte=date_to)
    if dealer_id:
        shipped_qs = shipped_qs.filter(dealer_id=dealer_id)

    # Created orders totals (operational)
    created_total_sales = created_qs.aggregate(total=Sum("total"))["total"] or Decimal("0")
    created_count = created_qs.count()
    created_avg_order = created_qs.aggregate(avg=Avg("total"))["avg"] or Decimal("0")

    created_by_status = (
        created_qs.values("status")
        .annotate(cnt=Count("id"), total=Sum("total"))
        .order_by("status")
    )

    # Shipped orders totals (financial)
    shipped_total_sales = shipped_qs.aggregate(total=Sum("total"))["total"] or Decimal("0")
    shipped_count = shipped_qs.count()
    shipped_avg_order = shipped_qs.aggregate(avg=Avg("total"))["avg"] or Decimal("0")

    # Items (use stored snapshot price + captured FIFO cost on shipping)
    from b2b.models import OrderItem

    shipped_items = (
        OrderItem.objects
        .select_related("product", "order", "order__dealer")
        .filter(order__in=shipped_qs)
    )

    # NOTE:
    # Do NOT reuse the same Expression instance across multiple ORM operations.
    # Django may mutate expressions during resolve/aggregation (summarize=True),
    # and reusing them can trigger "... is an aggregate" FieldError.
    dec_out = DecimalField(max_digits=14, decimal_places=2)

    def rev_expr():
        return ExpressionWrapper(
            F("qty") * F("price"),
            output_field=dec_out,
        )

    def cogs_expr():
        return Coalesce(
            F("cost_total"),
            Value(0, output_field=dec_out),
            output_field=dec_out,
        )

    def margin_expr():
        return ExpressionWrapper(
            rev_expr() - cogs_expr(),
            output_field=dec_out,
        )

    shipped_totals = shipped_items.aggregate(
        revenue=Coalesce(Sum(rev_expr()), Value(0, output_field=dec_out), output_field=dec_out),
        cogs=Coalesce(Sum(cogs_expr()), Value(0, output_field=dec_out), output_field=dec_out),
        margin=Coalesce(Sum(margin_expr()), Value(0, output_field=dec_out), output_field=dec_out),
        qty=Coalesce(Sum("qty"), Value(0)),
    )

    shipped_revenue = shipped_totals["revenue"] or Decimal("0")
    shipped_cogs = shipped_totals["cogs"] or Decimal("0")
    shipped_margin = shipped_totals["margin"] or Decimal("0")
    shipped_qty = int(shipped_totals["qty"] or 0)
    shipped_margin_pct = (shipped_margin / shipped_revenue * Decimal("100")) if shipped_revenue else Decimal("0")

    # Chart (shipped revenue + margin) by day
    by_day = (
        shipped_items
        .annotate(day=TruncDate(Coalesce(F("order__shipped_at"), F("order__created_at"))))
        .values("day")
        .annotate(
            revenue=Coalesce(Sum(rev_expr()), Value(0, output_field=dec_out), output_field=dec_out),
            margin=Coalesce(Sum(margin_expr()), Value(0, output_field=dec_out), output_field=dec_out),
        )
        .order_by("day")
    )

    chart_labels = [row["day"].strftime("%d.%m") for row in by_day]
    chart_revenue = [float(row["revenue"] or 0) for row in by_day]
    chart_margin = [float(row["margin"] or 0) for row in by_day]

    # By dealer (shipped)
    by_dealer = (
        shipped_items.values("order__dealer__id", "order__dealer__username")
        .annotate(
            orders=Count("order", distinct=True),
            revenue=Coalesce(Sum(rev_expr()), Value(0, output_field=dec_out), output_field=dec_out),
            cogs=Coalesce(Sum(cogs_expr()), Value(0, output_field=dec_out), output_field=dec_out),
            margin=Coalesce(Sum(margin_expr()), Value(0, output_field=dec_out), output_field=dec_out),
        )
        .order_by("-revenue")
    )

    # Top products (shipped)
    # IMPORTANT:
    # Do not define an annotation named "qty" in the same annotate() call where we
    # also build revenue expressions based on F("qty"). If we do, F("qty") can start
    # referring to the *aggregate annotation* instead of the model field, and Django
    # will raise: "... ExpressionWrapper(F(qty) * F(price)) is an aggregate".
    #
    # We avoid that by calculating revenue/cogs/margin first, and only then adding qty.
    top_products_base = shipped_items.values("product__sku", "product__name")

    top_products_by_rev = (
        top_products_base
        .annotate(
            revenue=Coalesce(Sum(rev_expr()), Value(0, output_field=dec_out), output_field=dec_out),
            cogs=Coalesce(Sum(cogs_expr()), Value(0, output_field=dec_out), output_field=dec_out),
            margin=Coalesce(Sum(margin_expr()), Value(0, output_field=dec_out), output_field=dec_out),
        )
        .annotate(qty=Sum("qty"))
        .order_by("-revenue", "product__sku")[:20]
    )

    top_products_by_margin = (
        top_products_base
        .annotate(
            revenue=Coalesce(Sum(rev_expr()), Value(0, output_field=dec_out), output_field=dec_out),
            cogs=Coalesce(Sum(cogs_expr()), Value(0, output_field=dec_out), output_field=dec_out),
            margin=Coalesce(Sum(margin_expr()), Value(0, output_field=dec_out), output_field=dec_out),
        )
        .annotate(qty=Sum("qty"))
        .order_by("-margin", "product__sku")[:20]
    )

    # Line items table (latest shipped)
    items_table = (
        shipped_items
        .annotate(line_revenue=rev_expr(), line_margin=margin_expr())
        .order_by("-order__shipped_at", "-order__id", "-id")[:300]
    )

    dealers = Dealer.objects.filter(is_dealer=True).order_by("username")

    context = {
        "created_orders": created_qs.order_by("-created_at")[:200],
        "shipped_orders": shipped_qs.order_by("-shipped_at", "-id")[:200],
        "date_from": date_from,
        "date_to": date_to,
        "dealer_id": dealer_id,
        "dealers": dealers,
        "created_total_sales": created_total_sales,
        "created_count": created_count,
        "created_avg_order": created_avg_order,
        "created_by_status": created_by_status,

        "shipped_total_sales": shipped_total_sales,
        "shipped_count": shipped_count,
        "shipped_avg_order": shipped_avg_order,
        "shipped_revenue": shipped_revenue,
        "shipped_cogs": shipped_cogs,
        "shipped_margin": shipped_margin,
        "shipped_margin_pct": shipped_margin_pct,
        "shipped_qty": shipped_qty,

        "by_dealer": by_dealer,
        "chart_labels": chart_labels,
        "chart_revenue": chart_revenue,
        "chart_margin": chart_margin,
        "top_products_by_rev": top_products_by_rev,
        "top_products_by_margin": top_products_by_margin,
        "items_table": items_table,
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
