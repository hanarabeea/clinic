from decimal import Decimal

from django import template

register = template.Library()


@register.filter
def money(value, blank="") -> str:
    """`1,250.00`. An empty or zero amount prints `blank` (pass "0" to print zeros): ledger columns read better with
    empty cells than with rows of 0.00."""
    if value in (None, ""):
        return blank
    value = Decimal(value)
    if value == 0 and blank != "0":
        return blank
    return f"{value:,.2f}"


@register.filter
def money0(value) -> str:
    """Always a number, also for zero."""
    return f"{Decimal(value or 0):,.2f}"
