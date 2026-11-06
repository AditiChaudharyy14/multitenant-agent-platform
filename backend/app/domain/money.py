"""Money is Decimal end to end: Postgres numeric(10,2) in, Decimal through the code, a
string in cards ("1600.00") and "NPR 1,600.00" for people. A float anywhere is a bug,
so both helpers refuse one instead of silently rounding it.
"""

from decimal import ROUND_HALF_UP, Decimal

CENT = Decimal("0.01")


def to_money(value: Decimal | int | str) -> Decimal:
    """Quantize to cents (half up). Accepts Decimal, int or a numeric string; never float."""
    if isinstance(value, float | bool):
        raise TypeError(f"money must not be a {type(value).__name__}: {value!r}")
    return Decimal(value).quantize(CENT, rounding=ROUND_HALF_UP)


def money_str(value: Decimal) -> str:
    """Machine form for cards and proposal payloads: '1600.00'."""
    return f"{to_money(value):.2f}"


def format_money(value: Decimal, currency: str) -> str:
    """Human form: 'NPR 1,250.00'."""
    if not isinstance(value, Decimal):
        raise TypeError(f"format_money needs a Decimal, got {type(value).__name__}")
    return f"{currency} {to_money(value):,.2f}"
