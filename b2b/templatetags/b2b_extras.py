from django import template
register = template.Library()

@register.filter
def mul(a, b):
    """Multiply two numbers for template usage."""
    try:
        return float(a) * float(b)
    except (TypeError, ValueError):
        return ''
