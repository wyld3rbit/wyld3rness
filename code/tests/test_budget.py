"""Tests for wyld3rness/loop/budget.py: the worst case reserved before a call, refusals, and settling."""

import pytest

from wyld3rness.loop.budget import TEMPLATE_TOKENS, Budget, BudgetExceeded
from wyld3rness.trace.prices import ModelPrice

PRICE = ModelPrice("qwen3:4b-instruct", 0.00000005, 0.0000004, "gpt-5-nano")


def test_the_worst_case_bounds_input_by_bytes_and_output_by_the_cap() -> None:
    budget = Budget(1.0, [PRICE])
    prompt = "héllo"  # 6 bytes in UTF-8: a multi-byte character counts as its bytes
    expected = (6 + TEMPLATE_TOKENS) * PRICE.input_per_token + 400 * PRICE.output_per_token
    assert budget.worst_case("qwen3:4b-instruct", prompt, 400) == pytest.approx(expected)


def test_the_byte_bound_holds_on_the_measured_prompts() -> None:
    """Research log entry 014: 10 live decider prompts, (bytes, input tokens) as Ollama reported them."""
    measured = [(1887, 590), (1998, 614), (2091, 653), (1891, 594), (2002, 618)]
    measured += [(2091, 653), (1891, 594), (2005, 621), (2094, 656), (1894, 597)]
    assert all(b + TEMPLATE_TOKENS >= tokens + 8 for b, tokens in measured)  # 8: the template's own tokens


def test_a_call_that_does_not_fit_is_refused_before_it_runs() -> None:
    budget = Budget(0.0002, [PRICE])
    budget.reserve("qwen3:4b-instruct", "x" * 100, 400)  # about $0.000165 held
    with pytest.raises(BudgetExceeded, match="does not fit"):
        budget.reserve("qwen3:4b-instruct", "x" * 100, 400)


def test_an_unpriced_model_is_refused() -> None:
    with pytest.raises(BudgetExceeded, match="no reference price"):
        Budget(1.0, [PRICE]).reserve("mystery:70b", "x", 10)


def test_settling_replaces_the_reservation_with_the_real_cost() -> None:
    budget = Budget(1.0, [PRICE])
    r = budget.reserve("QWEN3:4b-instruct", "x" * 2000, 400)  # names match case-insensitively
    charge = budget.settle(r, 600, 150)
    assert budget.held_usd == pytest.approx(0.0)
    assert charge.cost_usd == pytest.approx(600 * PRICE.input_per_token + 150 * PRICE.output_per_token)
    assert budget.spent_usd == pytest.approx(charge.cost_usd) and charge.cost_usd < charge.reserved_usd


def test_unknown_token_counts_are_charged_the_reservation() -> None:
    budget = Budget(1.0, [PRICE])
    r = budget.reserve("qwen3:4b-instruct", "x", 10)
    assert budget.settle(r, None, 5).cost_usd == r.usd and budget.spent_usd == r.usd
