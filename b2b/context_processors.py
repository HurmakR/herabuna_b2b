# Adds cart counters (lines, total quantity, total amount) to every template.

from decimal import Decimal
from .models import Order

def cart_badge(request):
    """Provide cart counters for the current user's draft order."""
    lines = 0
    qty_sum = 0
    total = Decimal("0")
    if getattr(request, "user", None) and request.user.is_authenticated and not request.user.is_staff:
        order = (
            Order.objects
            .filter(dealer=request.user, status="draft")
            .prefetch_related("items", "items__product", "items__variant")
            .first()
        )
        if order:
            items = list(order.items.all())
            lines = len(items)
            qty_sum = sum(i.qty for i in items)
            total = sum((i.price * i.qty for i in items), Decimal("0"))
    return {
        "cart_item_count": lines,   # number of distinct lines
        "cart_qty_sum": qty_sum,    # total quantity
        "cart_total": total,        # total amount (Decimal)
    }
