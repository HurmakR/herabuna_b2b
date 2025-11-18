from datetime import datetime, timedelta

from django.contrib.auth.decorators import user_passes_test
from django.db.models import Sum, F, DecimalField, ExpressionWrapper
from django.shortcuts import render
from django.utils import timezone

from b2b.models import Order, OrderItem, Dealer, Product, Brand, Category


def _is_staff(user):
    return user.is_staff


@user_passes_test(_is_staff)
def sales_report(request):
    """
    Simple sales analytics:
    - filter by date range and dealer
    - aggregate totals per day and per dealer
    - provide data for charts (Chart.js)
    """
    # Default period: last 30 days
    today = timezone.localdate()
    default_from = today - timedelta(days=30)

    date_from_str = request.GET.get("date_from") or default_from.isoformat()
    date_to_str = request.GET.get("date_to") or today.isoformat()
    dealer_id = request.GET.get("dealer") or ""

    try:
        date_from = datetime.fromisoformat(date_from_str).date()
    except Exception:
        date_from = default_from

    try:
        date_to = datetime.fromisoformat(date_to_str).date()
    except Exception:
        date_to = today

    # Limit to end of day
    date_from_dt = datetime.combine(date_from, datetime.min.time(), tzinfo=timezone.get_current_timezone())
    date_to_dt = datetime.combine(date_to, datetime.max.time(), tzinfo=timezone.get_current_timezone())

    # Statuses that we consider as real sales (exclude draft, cancelled)
    valid_statuses = ["submitted", "pending_payment", "shipped"]

    qs = (
        Order.objects
        .filter(created_at__range=(date_from_dt, date_to_dt), status__in=valid_statuses)
        .select_related("dealer")
        .prefetch_related("items")
    )
    if dealer_id:
        qs = qs.filter(dealer_id=dealer_id)

    # Aggregate per day
    # We assume Order.subtotal / total are already calculated
    daily = (
        qs.values("created_at__date")
        .annotate(total_sum=Sum("total"))
        .order_by("created_at__date")
    )

    # Prepare data for chart (labels + values)
    labels = []
    values = []
    for row in daily:
        d = row["created_at__date"]
        labels.append(d.strftime("%Y-%m-%d"))
        values.append(float(row["total_sum"] or 0))

    # Aggregate per dealer
    per_dealer = (
        qs.values("dealer__id", "dealer__company_name", "dealer__username")
        .annotate(total_sum=Sum("total"))
        .order_by("-total_sum")
    )

    context = {
        "orders": qs.order_by("-created_at")[:200],  # last 200 for table
        "dealers": Dealer.objects.filter(is_dealer=True, is_active=True).order_by("company_name", "username"),
        "selected_dealer": int(dealer_id) if dealer_id else "",
        "date_from": date_from_str,
        "date_to": date_to_str,
        "chart_labels": labels,
        "chart_values": values,
        "per_dealer": per_dealer,
    }
    return render(request, "reports/sales_report.html", context)


@user_passes_test(_is_staff)
def stock_report(request):
    """
    Stock analytics:
    - filter by brand and category
    - current stock per product
    - cost value (cost_price * stock_qty)
    - wholesale value (wholesale_price * stock_qty)
    - bar chart with top products by cost and wholesale value
    """
    brand_id = request.GET.get("brand") or ""
    category_id = request.GET.get("category") or ""

    products = Product.objects.select_related("brand").prefetch_related("categories")

    if brand_id:
        products = products.filter(brand_id=brand_id)
    if category_id:
        products = products.filter(categories__id=category_id)

    # Annotate cost_value and wholesale_value
    cost_expr = ExpressionWrapper(
        F("cost_price") * F("stock_qty"),
        output_field=DecimalField(max_digits=14, decimal_places=2),
    )
    wholesale_expr = ExpressionWrapper(
        F("wholesale_price") * F("stock_qty"),
        output_field=DecimalField(max_digits=14, decimal_places=2),
    )

    products = products.annotate(
        cost_value=cost_expr,
        wholesale_value=wholesale_expr,
    )

    total_cost = products.aggregate(s=Sum("cost_value"))["s"] or 0
    total_wholesale = products.aggregate(s=Sum("wholesale_value"))["s"] or 0

    # Chart: show top N by cost_value
    top = (
        products.filter(stock_qty__gt=0)
        .order_by("-cost_value")[:20]
    )

    chart_labels = [p.name_with_weight for p in top]
    chart_cost_values = [float(p.cost_value or 0) for p in top]
    chart_wh_values = [float(p.wholesale_value or 0) for p in top]

    brands = Brand.objects.order_by("name")
    categories = Category.objects.order_by("name")

    context = {
        "products": products.order_by("-stock_qty", "name"),
        "brand_id": brand_id,
        "category_id": category_id,
        "brands": brands,
        "categories": categories,
        "total_cost": float(total_cost),
        "total_wholesale": float(total_wholesale),
        "chart_labels": chart_labels,
        "chart_cost_values": chart_cost_values,
        "chart_wh_values": chart_wh_values,
    }
    return render(request, "reports/stock_report.html", context)
