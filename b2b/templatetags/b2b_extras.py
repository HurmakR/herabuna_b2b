from django import template
register = template.Library()

@register.filter
def mul(a, b):
    """Multiply two numbers for template usage."""
    try:
        return float(a) * float(b)
    except (TypeError, ValueError):
        return ''

@register.filter
def get_item(dictionary, key):
    """Get item from dict by key: {{ mydict|get_item:key }}"""
    if not isinstance(dictionary, dict):
        return ""
    return dictionary.get(key, "")

@register.filter
def margin_color(wholesale_price, cost_price):
    """Return inline style string based on margin ratio.
    Usage: {{ p.wholesale_price|margin_color:p.last_unit_cost }}
    """
    try:
        price = float(wholesale_price or 0)
        cost = float(cost_price or 0)
        if cost <= 0 or price <= 0:
            return ""
        ratio = price / cost
        if ratio >= 2.0:
            return "color:#1a6b2a;font-weight:600;background:#f0fdf4;border-color:#86efac"
        elif ratio >= 1.7:
            return "color:#92400e;font-weight:600;background:#fffbeb;border-color:#fcd34d"
        else:
            return "color:#991b1b;font-weight:600;background:#fef2f2;border-color:#fca5a5"
    except (TypeError, ValueError):
        return ""

@register.filter
def margin_ratio(wholesale_price, cost_price):
    """Return margin ratio as float."""
    try:
        price = float(wholesale_price or 0)
        cost = float(cost_price or 0)
        if cost <= 0 or price <= 0:
            return None
        return round(price / cost, 2)
    except (TypeError, ValueError):
        return None
