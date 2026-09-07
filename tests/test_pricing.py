from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given
from hypothesis import strategies as st

from model_router.pricing import PriceBook, cost_usd
from model_router.schemas import Usage
from tests.conftest import make_config, price


def test_cost_is_exact_decimal_arithmetic() -> None:
    # 1,000 prompt tokens at $0.15/M and 500 completion tokens at $0.60/M
    usage = Usage.of(1_000, 500)
    assert cost_usd(usage, price("0.15", "0.60")) == Decimal("0.00045000")


def test_zero_priced_model_costs_nothing() -> None:
    assert cost_usd(Usage.of(10_000, 10_000), price("0", "0")) == Decimal("0")


def test_a_single_token_of_a_cheap_model_is_not_rounded_away() -> None:
    # $0.15 per million is 1.5e-7 per token; eight places keep it.
    assert cost_usd(Usage.of(1, 0), price("0.15", "0.60")) == Decimal("0.00000015")


def test_pricebook_from_config_and_missing_price() -> None:
    book = PriceBook.from_config(make_config())
    assert book.price("a", "m1") == price()
    assert set(book.by_key()) == {"a/m1", "a/m2", "b/m1", "b/m3"}
    with pytest.raises(KeyError):
        book.price("a", "nope")


@given(
    prompt=st.integers(0, 10_000_000),
    completion=st.integers(0, 10_000_000),
    i=st.decimals(0, 100, places=4),
    o=st.decimals(0, 100, places=4),
)
def test_cost_is_never_negative_and_is_additive(
    prompt: int, completion: int, i: Decimal, o: Decimal
) -> None:
    p = price(str(i), str(o))
    total = cost_usd(Usage.of(prompt, completion), p)
    assert total >= 0
    # Summing the two halves separately lands within one rounding unit.
    halves = cost_usd(Usage.of(prompt, 0), p) + cost_usd(Usage.of(0, completion), p)
    assert abs(total - halves) <= Decimal("0.00000001")
