from decimal import Decimal
from .models import Order

def cart_badge(request):
    """Provide cart counters and order badges to every template."""
    lines = 0
    qty_sum = 0
    total = Decimal("0")
    admin_new_orders = 0
    client_unpaid = 0

    if getattr(request, "user", None) and request.user.is_authenticated:
        if not request.user.is_staff:
            order = (
                Order.objects
                .filter(dealer=request.user, status="draft")
                .prefetch_related("items")
                .first()
            )
            if order:
                items = list(order.items.all())
                lines = len(items)
                qty_sum = sum(i.qty for i in items)
                total = sum((i.price * i.qty for i in items), Decimal("0"))
            client_unpaid = Order.objects.filter(dealer=request.user, status="pending_payment").count()
        else:
            admin_new_orders = Order.objects.filter(status="submitted").count()

    return {
        "cart_item_count": lines,
        "cart_qty_sum": qty_sum,
        "cart_total": total,
        "admin_new_orders": admin_new_orders,
        "client_unpaid_count": client_unpaid,
    }
