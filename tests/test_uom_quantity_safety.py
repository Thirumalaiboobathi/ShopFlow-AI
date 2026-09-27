"""A count in a unit the shop never sells in is a question, not a quotation.

A live evaluation sent "3 kg Havells MCB SP 32A C-curve" and got a quotation
for three breakers. "kg" was not a unit ShopFlow knew, so the line read as "3"
with no unit, and a count with no unit is a count of the catalogue unit. The
customer's unit was silently replaced.

The rule these tests hold: a weight or volume unit on a count withholds the
quotation and asks how many of the catalogue unit are wanted. Nothing is
converted and nothing is assumed.
"""

from __future__ import annotations

import pytest

from agent.orchestrator import (
    STATUS_NEEDS_CLARIFICATION,
    STATUS_QUOTED,
    run_order_agent,
)
from agent.quantity_guard import (
    UNIT_NOT_SOLD,
    VERIFIED,
    check_quantities,
    question_for,
)
from test_quantity_integrity import (
    MCB,
    MCB_SEARCH,
    FakeBedrock,
    assert_not_quoted,
    search_then_quote,
)

RESOLVED_MCB = [{"skuId": MCB, "status": "RESOLVED",
                 "requestedText": "Havells MCB SP 32A C-curve"}]


def _status(seeded, text, qty):
    quote = {"lines": [{"skuId": MCB, "quantity": qty}]}
    return check_quantities(seeded, text, RESOLVED_MCB, quote)[0]


@pytest.mark.parametrize("text", [
    "3 kg Havells MCB SP 32A C-curve",
    "3kg Havells MCB SP 32A C-curve",
    "3 kgs Havells MCB SP 32A C-curve",
    "3 litre Havells MCB SP 32A C-curve",
    "moonu kg Havells MCB SP 32A C-curve venum",
    "3 kg MCB Havells SP 32A C-curve venum",
])
def test_weight_or_volume_on_a_count_is_never_verified(seeded, text):
    entry = _status(seeded, text, 3)
    assert entry["status"] == UNIT_NOT_SOLD
    assert entry["catalogueUom"] == "PIECE"


@pytest.mark.parametrize("text", [
    "3 Havells MCB SP 32A C-curve",
    "3 MCBs Havells SP 32A C-curve",
    "3 pcs Havells MCB SP 32A C-curve",
    "moonu Havells MCB SP 32A C-curve venum",
])
def test_a_plain_count_is_still_verified(seeded, text):
    assert _status(seeded, text, 3)["status"] == VERIFIED


def test_a_length_specification_is_not_a_unit_problem(seeded):
    quote = {"lines": [{"skuId": "ACC-CLIP-CLAMP", "quantity": 10}]}
    matches = [{"skuId": "ACC-CLIP-CLAMP", "status": "RESOLVED",
                "requestedText": "cable clip clamp 20mm"}]
    entry = check_quantities(seeded, "10 cable clip clamp 20mm", matches,
                             quote)[0]
    assert entry["status"] == VERIFIED


def test_the_question_names_the_customer_unit_and_the_shop_unit(seeded):
    entry = _status(seeded, "3 kg Havells MCB SP 32A C-curve", 3)
    question = question_for(entry, "Havells MCB SP 32A C-Curve")
    assert "3 kg" in question
    assert "sold by the piece" in question
    assert "Nothing has been quoted" in question


def test_three_kg_mcb_is_never_quoted_as_three_pieces(seeded):
    """The live failure, end to end through the agent loop."""
    result = run_order_agent(
        seeded, "3 kg Havells MCB SP 32A C-curve",
        client=FakeBedrock(search_then_quote((MCB, 3), search=MCB_SEARCH)))
    assert_not_quoted(result)
    assert result.status == STATUS_NEEDS_CLARIFICATION
    assert result.clarification["clarifyingAttribute"] == "uom"
    assert result.clarification["options"] == []


def test_three_mcbs_is_still_quoted(seeded):
    result = run_order_agent(
        seeded, "3 Havells MCB SP 32A C-curve",
        client=FakeBedrock(search_then_quote((MCB, 3), search=MCB_SEARCH)))
    assert result.status == STATUS_QUOTED
    assert result.quote["lines"][0]["quantity"] == 3


def test_model_question_about_an_unmatched_kg_line_becomes_the_unit_question(seeded):
    """The live path: the model searched "3 kg Havells MCB...", matched
    nothing, and asked its own question. The template now asks about the unit,
    which is the actual problem, instead of saying the product is unknown."""
    from test_quantity_integrity import turn
    search = {"requestedText": "3 kg Havells MCB SP 32A C-curve",
              "brand": "Havells", "category": "MCB", "specification": "32A kg"}
    ask = ("request_clarification", {
        "requestedText": "3 kg Havells MCB SP 32A C-curve",
        "clarifyingAttribute": "specification",
        "question": "Did you mean 3 kg of MCBs? We will quote 3.", "options": []})
    result = run_order_agent(seeded, "3 kg Havells MCB SP 32A C-curve",
                             client=FakeBedrock([turn(("search_catalog", search)),
                                                 turn(ask)]))
    assert_not_quoted(result)
    assert result.status == STATUS_NEEDS_CLARIFICATION
    assert result.clarification["clarifyingAttribute"] == "uom"
    assert "3 kg" in result.clarification["question"]
    assert "not by weight or volume" in result.clarification["question"]
    assert "We will quote" not in result.summary
