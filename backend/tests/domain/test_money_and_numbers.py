"""Money is Decimal end to end; order numbers parse leniently but never cross prefixes."""

from decimal import Decimal

import pytest

from app.domain.money import format_money, to_money
from app.domain.orders.numbers import display_number, parse_order_number


@pytest.mark.parametrize(
    ("amount", "shown"),
    [(Decimal("1250"), "NPR 1,250.00"), (Decimal("0"), "NPR 0.00"), (Decimal("1234567.5"), "NPR 1,234,567.50"),
     (Decimal("450.005"), "NPR 450.01")],
)  # fmt: skip
def test_format_money(amount: Decimal, shown: str) -> None:
    assert format_money(amount, "NPR") == shown


def test_floats_are_rejected() -> None:
    with pytest.raises(TypeError):
        format_money(12.5, "NPR")  # type: ignore[arg-type]
    with pytest.raises(TypeError):
        to_money(0.1)  # type: ignore[arg-type]


def test_to_money_quantizes() -> None:
    assert to_money(Decimal("3")) == Decimal("3.00")
    assert to_money("19.999") == Decimal("20.00")


@pytest.mark.parametrize(
    "raw", ["HT-1042", "ht-1042", "HT 1042", "ht1042", "#1042", "1042", " HT-1042 ", "order HT-1042"]
)
def test_parse_accepts_friendly_forms(raw: str) -> None:
    assert parse_order_number(raw, "HT") == 1042


@pytest.mark.parametrize("raw", ["XY-1042", "", "HT-", "abc", "HT-10x2", "HT-0", "-5", "99999999999"])
def test_parse_rejects_other_prefixes_and_junk(raw: str) -> None:
    assert parse_order_number(raw, "HT") is None


def test_display() -> None:
    assert display_number("HT", 1042) == "HT-1042"
