"""Money arithmetic for the books: `Decimal` only, rounded once, and splits that always add up.

Rules (plan 14 §14.5): an amount is rounded once, at the line, with ROUND_HALF_UP to two places; totals are sums of
rounded lines; anything that divides an amount (a discount over lines, a tax, doctor shares) goes through `allocate`,
so the parts sum to the whole to the piaster.
"""

from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")
ZERO = Decimal("0.00")


def money(value) -> Decimal:
    """`value` as an amount with two places. Floats are refused: they are binary and would bring their error in."""
    if isinstance(value, float):
        raise TypeError("money() takes Decimal, int or str, never float")
    if not isinstance(value, Decimal):
        value = Decimal(str(value))
    if not value.is_finite():
        raise ValueError("not a finite amount")
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def allocate(total, weights) -> list[Decimal]:
    """Split `total` in proportion to `weights` (largest-remainder method). The parts are amounts with two places and
    sum to `total` exactly; the piasters left over by rounding down go to the parts with the largest remainders
    (the earlier part on a tie), so the result is the same every time."""
    total = money(total)
    weights = [Decimal(str(w)) if not isinstance(w, Decimal) else w for w in weights]
    if not weights:
        raise ValueError("nothing to allocate to")
    if any(w < 0 for w in weights):
        raise ValueError("weights cannot be negative")
    whole = sum(weights, Decimal(0))
    if whole == 0:
        if total == 0:
            return [ZERO for _ in weights]
        raise ValueError("cannot allocate an amount over weights that are all zero")
    sign = -1 if total < 0 else 1
    amount = abs(total)
    exact = [amount * w / whole for w in weights]
    parts = [e.quantize(CENT, rounding=ROUND_DOWN) for e in exact]
    left = int((amount - sum(parts, Decimal(0))) / CENT)
    by_remainder = sorted(range(len(weights)), key=lambda i: (-(exact[i] - parts[i]), i))
    for i in by_remainder[:left]:
        parts[i] += CENT
    return [p * sign for p in parts]


def percent_of(amount, percent) -> Decimal:
    """`percent` % of `amount`, rounded once."""
    return money(money(amount) * Decimal(str(percent)) / Decimal(100))
