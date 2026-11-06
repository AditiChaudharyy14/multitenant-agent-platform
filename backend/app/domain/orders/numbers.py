"""Order numbers (HT-1042). The parsing and display rules are shared with booking numbers
and live in app/domain/numbers.py; these names stay for the store code."""

from app.domain.numbers import display_number, parse_number

parse_order_number = parse_number

__all__ = ["display_number", "parse_order_number"]
