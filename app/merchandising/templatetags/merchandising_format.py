from decimal import Decimal

from django import template


register = template.Library()


@register.filter
def percent100(value):
    if value is None:
        return None
    return Decimal(value) * Decimal("100")
