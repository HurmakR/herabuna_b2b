from django import template

register = template.Library()

@register.filter(name="add_class")
def add_class(field, css):
    # не ламаємо існуючі attrs, просто додаємо class
    attrs = field.field.widget.attrs.copy()
    existing = attrs.get("class", "")
    attrs["class"] = (existing + " " + css).strip()
    return field.as_widget(attrs=attrs)

@register.filter(name="attr")
def set_attr(field, arg):
    """
    Generic attribute setter from templates.
    Usage: {{ field|attr:"placeholder:Ваш email" }}
    """
    try:
        k, v = str(arg).split(":", 1)
    except ValueError:
        return field
    attrs = field.field.widget.attrs.copy()
    attrs[k] = v
    return field.as_widget(attrs=attrs)
