"""A clarification question may not quote a price nobody calculated.

The question is the model's own words. That is deliberate and worth keeping -
"Which one, 1-Way 10A or 2-Way 16A?" is a better question than anything a
template would produce, and every digit in it belongs to a product.

What the model has no business writing is money. The option buttons beside the
question already carry real catalogue prices, so a reader has no way to tell a
figure the model invented from a figure the engine calculated. The check is
therefore on the currency marker, never on digits.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from conftest import CANONICAL_ORDER, canonical_quote_turn  # noqa: E402
from agent.grounding import collect_numbers, unsupported_prices  # noqa: E402
from agent.orchestrator import (  # noqa: E402
    CLARIFY_FALLBACK,
    STATUS_NEEDS_CLARIFICATION,
    STATUS_QUOTED,
    _safe_question,
    run_order_agent,
)

CANONICAL_ITEMS = [
    {"skuId": "SW-ANC-1W10A", "quantity": 20},
    {"skuId": "W-FIN-1.5-RED-90M", "quantity": 3},
    {"skuId": "MCB-HAV-SP-32A-C", "quantity": 2},
]

CANONICAL_TOTAL = 22306.48

# Two real options, with the prices the catalogue actually holds.
OPTIONS = [
    {"skuId": "W-FIN-1.5-RED-90M", "name": "Finolex 1.5 sqmm Red 90m",
     "value": "Red", "sellingPrice": 6608.0, "unit": "coil"},
    {"skuId": "W-FIN-1.5-BLU-90M", "name": "Finolex 1.5 sqmm Blue 90m",
     "value": "Blue", "sellingPrice": 6608.0, "unit": "coil"},
]


def clarification(question, options=None, requested="3 coils Finolex wire"):
    return {"requestedText": requested, "clarifyingAttribute": "colour",
            "question": question, "options": options if options is not None
            else OPTIONS}


class FakeBedrock:
    def __init__(self, turns):
        self._turns = list(turns)

    def converse(self, **kwargs):
        return {"output": {"message": self._turns.pop(0)}}


def tool_use(name, payload, use_id="t1"):
    return {"role": "assistant",
            "content": [{"toolUse": {"toolUseId": use_id, "name": name,
                                     "input": payload}}]}


# ---------------------------------------------------------------------------
# Specifications survive. This is the half that matters most.
# ---------------------------------------------------------------------------

def test_product_specifications_are_never_stripped():
    for question in (
        "Which one do you need - 1-Way 10A or 2-Way 16A?",
        "Please specify the size: 1.0 sqmm, 1.5 sqmm, 2.5 sqmm or 4.0 sqmm.",
        "Did you mean the SP 32A C-Curve or the DP 32A C-Curve?",
        "Is that the 90m coil or the 180m coil?",
        "How many pieces - 20 or 40?",
    ):
        assert _safe_question(clarification(question)) == question


def test_a_bare_number_is_not_treated_as_money():
    """Only a currency marker makes a number a price claim."""
    question = "Do you want 6608 of them?"   # absurd, but not a price claim
    assert _safe_question(clarification(question)) == question


# ---------------------------------------------------------------------------
# Fabricated prices do not survive
# ---------------------------------------------------------------------------

def test_an_invented_price_replaces_the_question():
    question = "Do you want the ₹5,000 one or the ₹9,000 one?"
    out = _safe_question(clarification(question))

    assert out != question
    assert "5,000" not in out and "9,000" not in out
    assert out == 'Please confirm which product is wanted for "3 coils Finolex wire".'


def test_invented_prices_in_other_notations_are_caught():
    for question in ("Is it the Rs 5,000 coil or the Rs. 9,000 coil?",
                     "INR 4200 or INR 7300?"):
        out = _safe_question(clarification(question))
        assert out != question, question
        for bad in ("5,000", "9,000", "4200", "7300"):
            assert bad not in out


def test_a_real_option_price_is_allowed_to_be_quoted():
    """The engine produced 6608.0, so the model may repeat it."""
    question = "Both are ₹6,608.00 - which colour do you need?"
    assert _safe_question(clarification(question)) == question


def test_the_fallback_is_used_when_there_is_no_requested_text():
    out = _safe_question(clarification("Pay ₹999 instead?", requested=""))
    assert out == CLARIFY_FALLBACK
    assert "999" not in out


def test_reasoning_is_still_stripped_from_the_question():
    out = _safe_question(clarification("<thinking>aside</thinking> Which colour?"))
    assert out == "Which colour?"


def test_a_question_that_is_only_reasoning_falls_back():
    assert _safe_question(clarification("<thinking>all of it</thinking>")) \
        == CLARIFY_FALLBACK


# ---------------------------------------------------------------------------
# The predicate itself
# ---------------------------------------------------------------------------

def test_unsupported_prices_reads_option_data_as_the_source_of_truth():
    allowed = collect_numbers(OPTIONS)
    assert unsupported_prices("₹6,608.00 each", allowed) == []
    assert unsupported_prices("₹1,234.00 each", allowed) == ["1,234.00"]
    # a specification is not a price
    assert unsupported_prices("1-Way 10A, 1.5 sqmm, 90m", allowed) == []


def test_no_options_means_no_price_may_be_quoted():
    """A NOT_FOUND line offers nothing, so it supports no figure at all."""
    out = _safe_question(clarification("It costs ₹450.", options=[]))
    assert "450" not in out


# ---------------------------------------------------------------------------
# End to end, and nothing else moved
# ---------------------------------------------------------------------------

def test_a_model_clarification_with_an_invented_price_is_replaced(seeded):
    fake = FakeBedrock([
        tool_use("search_catalog",
                 {"requestedText": "3 coils Finolex 1.5 sq mm wire"}),
        tool_use("request_clarification",
                 {"requestedText": "3 coils Finolex 1.5 sq mm wire",
                  "clarifyingAttribute": "colour",
                  "question": "The red is ₹1,000 and the blue is ₹2,000 "
                              "- which?"}, "t2"),
    ])
    result = run_order_agent(seeded, "order", client=fake)

    assert result.status == STATUS_NEEDS_CLARIFICATION
    assert "1,000" not in result.clarification["question"]
    assert "2,000" not in result.clarification["question"]
    assert result.summary == result.clarification["question"]
    # the real prices are still available, from the deterministic options
    prices = {o["sellingPrice"] for o in result.clarification["options"]}
    assert prices and all(isinstance(p, (int, float)) for p in prices)


def test_an_ordinary_clarification_is_untouched(seeded):
    fake = FakeBedrock([
        tool_use("search_catalog",
                 {"requestedText": "3 coils Finolex 1.5 sq mm wire"}),
        tool_use("request_clarification",
                 {"requestedText": "3 coils Finolex 1.5 sq mm wire",
                  "clarifyingAttribute": "colour",
                  "question": "Which colour - Red, Blue or Black?"}, "t2"),
    ])
    result = run_order_agent(seeded, "3 coils Finolex 1.5 sq mm wire",
                             client=fake)

    # An ordinary colour question survives as an ordinary colour question -
    # rebuilt from the real options rather than copied from the model, so the
    # colours listed are the ones the shop actually stocks.
    assert result.clarification["question"] == (
        'Which colour do you need for "3 coils Finolex 1.5 sq mm wire": '
        'Black, Blue or Red?')
    assert result.clarification["clarifyingAttribute"] == "colour"
    assert {o["skuId"] for o in result.clarification["options"]}


def test_the_canonical_order_is_unaffected(seeded):
    fake = FakeBedrock([canonical_quote_turn(CANONICAL_ITEMS)])
    result = run_order_agent(seeded, CANONICAL_ORDER, client=fake)

    assert result.status == STATUS_QUOTED
    assert result.quote["total"] == CANONICAL_TOTAL
    assert len(result.quote["lines"]) == 3
