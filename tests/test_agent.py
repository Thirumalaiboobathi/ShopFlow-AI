"""The order agent.

Bedrock is replaced by a scripted fake so these tests are deterministic and run
offline. What is being tested is ShopFlow's half of the contract: that tools
reject invented SKUs, that ambiguity ends in a question, and that every number
in the result came from the engine.
"""

from __future__ import annotations

import pytest

from agent.grounding import (
    collect_numbers,
    deterministic_quote_summary,
    find_unsupported_numbers,
    validate_summary,
)
from agent.orchestrator import (
    MAX_ORDER_CHARS,
    MAX_TURNS,
    STATUS_FAILED,
    STATUS_NEEDS_CLARIFICATION,
    STATUS_QUOTED,
    OrderTooLongError,
    apply_summary,
    run_order_agent,
)
from agent.tools import ToolError, run_tool


class FakeBedrock:
    """Replays a scripted sequence of model turns and records what it saw."""

    def __init__(self, turns):
        self._turns = list(turns)
        self.calls = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        if not self._turns:
            raise AssertionError("fake model ran out of scripted turns")
        return {"output": {"message": self._turns.pop(0)}}


def tool_use(name, payload, use_id="t1"):
    return {"role": "assistant",
            "content": [{"toolUse": {"toolUseId": use_id, "name": name,
                                     "input": payload}}]}


def text_turn(text):
    return {"role": "assistant", "content": [{"text": text}]}


CANONICAL_ITEMS = [
    {"skuId": "SW-ANC-1W10A", "quantity": 20},
    {"skuId": "W-FIN-1.5-RED-90M", "quantity": 3},
    {"skuId": "MCB-HAV-SP-32A-C", "quantity": 2},
]


# ---- 1. order extraction and 9. end-to-end canonical order ----

def test_canonical_order_produces_the_engine_quote(seeded):
    fake = FakeBedrock([
        tool_use("search_catalog",
                 {"requestedText": "20 Anchor modular switches",
                  "brand": "Anchor", "category": "Switch"}),
        tool_use("calculate_quote", {"items": CANONICAL_ITEMS}, "t2"),
    ])
    result = run_order_agent(seeded, "20 switches, 3 coils, 2 MCB", client=fake)

    assert result.status == STATUS_QUOTED
    assert result.quote["total"] == 22306.48
    shortages = {l["skuId"]: l["shortageQty"] for l in result.quote["lines"]}
    assert shortages == {"SW-ANC-1W10A": 6, "W-FIN-1.5-RED-90M": 2,
                         "MCB-HAV-SP-32A-C": 0}


def test_agent_stops_as_soon_as_the_quote_is_produced(seeded):
    fake = FakeBedrock([tool_use("calculate_quote", {"items": CANONICAL_ITEMS})])
    result = run_order_agent(seeded, "order", client=fake)
    assert result.status == STATUS_QUOTED
    assert result.turns == 1
    assert len(fake.calls) == 1


def test_agent_is_given_the_strict_tool_config(seeded):
    fake = FakeBedrock([tool_use("calculate_quote", {"items": CANONICAL_ITEMS})])
    run_order_agent(seeded, "order", client=fake)
    names = {t["toolSpec"]["name"] for t in fake.calls[0]["toolConfig"]["tools"]}
    assert names == {"search_catalog", "get_inventory", "calculate_quote",
                     "request_clarification"}


# ---- 2. valid-SKU-only matching, 4. unknown SKU rejection ----

def test_invented_sku_is_rejected_by_the_quote_tool(seeded):
    with pytest.raises(ToolError) as exc:
        run_tool(seeded, "calculate_quote",
                 {"items": [{"skuId": "W-FIN-9.9-PINK-500M", "quantity": 1}]})
    assert exc.value.kind == "UNKNOWN_SKU"


def test_invented_sku_is_rejected_by_the_inventory_tool(seeded):
    with pytest.raises(ToolError) as exc:
        run_tool(seeded, "get_inventory", {"skuIds": ["NOT-A-REAL-SKU"]})
    assert exc.value.kind == "UNKNOWN_SKU"


def test_invented_sku_cannot_be_offered_as_a_clarification_option(seeded):
    with pytest.raises(ToolError) as exc:
        run_tool(seeded, "request_clarification", {
            "clarifyingAttribute": "colour", "question": "Which colour?",
            "skuIdOptions": ["MADE-UP-SKU"]})
    assert exc.value.kind == "UNKNOWN_SKU"


def test_agent_recovers_when_the_model_invents_a_sku(seeded):
    """The tool refuses, the model is told why, and the real order still lands."""
    fake = FakeBedrock([
        tool_use("calculate_quote",
                 {"items": [{"skuId": "INVENTED-SKU", "quantity": 5}]}),
        tool_use("calculate_quote", {"items": CANONICAL_ITEMS}, "t2"),
    ])
    result = run_order_agent(seeded, "order", client=fake)

    assert result.status == STATUS_QUOTED
    rejected = [t for t in result.trace if not t.get("ok")]
    assert len(rejected) == 1
    assert rejected[0]["errorKind"] == "UNKNOWN_SKU"


def test_repeated_invalid_arguments_stop_the_agent(seeded):
    bad = {"items": [{"skuId": "NOPE", "quantity": 1}]}
    fake = FakeBedrock([tool_use("calculate_quote", bad, f"t{i}") for i in range(4)])
    result = run_order_agent(seeded, "order", client=fake)
    assert result.status == STATUS_FAILED
    assert "invalid tool arguments" in result.message


def test_search_tool_only_ever_returns_catalog_skus(seeded):
    payload = run_tool(seeded, "search_catalog",
                       {"requestedText": "Finolex 1.5 sq mm wire",
                        "brand": "Finolex", "category": "Wire"})
    for candidate in payload.get("candidates", []):
        assert candidate["skuId"] in seeded.products


# ---- 3. ambiguity produces a clarification ----

def test_ambiguous_product_is_reported_as_ambiguous_to_the_model(seeded):
    payload = run_tool(seeded, "search_catalog", {
        "requestedText": "Finolex 1.5 sq mm wire", "brand": "Finolex",
        "category": "Wire", "specification": "1.5 sqmm"})
    assert payload["status"] == "AMBIGUOUS"
    assert payload["skuId"] is None
    assert payload["clarifyingAttribute"] == "colour"
    assert "Do not choose" in payload["instruction"]


def test_clarification_ends_the_run_with_a_question(seeded):
    fake = FakeBedrock([
        tool_use("search_catalog", {"requestedText": "Finolex 1.5 sq mm wire",
                                    "brand": "Finolex", "category": "Wire",
                                    "specification": "1.5 sqmm"}),
        tool_use("request_clarification", {
            "requestedText": "Finolex 1.5 sq mm wire",
            "clarifyingAttribute": "colour",
            "question": "Which colour do you need?"}, "t2"),
    ])
    result = run_order_agent(seeded, "3 coils Finolex 1.5 sq mm wire", client=fake)

    assert result.status == STATUS_NEEDS_CLARIFICATION
    assert result.quote is None
    assert result.clarification["question"] == "Which colour do you need?"


def test_clarification_options_are_rebuilt_when_the_model_omits_them(seeded):
    """The owner must always get real choices to pick from."""
    payload = run_tool(seeded, "request_clarification", {
        "requestedText": "Finolex 1.5 sq mm wire",
        "clarifyingAttribute": "colour",
        "question": "Which colour?"})
    options = payload["clarification"]["options"]
    assert {o["value"] for o in options} == {"Red", "Blue", "Black"}
    assert all(o["skuId"] in seeded.products for o in options)


# ---- 8. no invented numeric values ----

def test_grounding_accepts_numbers_that_came_from_the_engine():
    payload = {"quote": {"total": 22306.48, "lines": [{"shortageQty": 6}]}}
    ok, unsupported = validate_summary("Total Rs 22,306.48, short by 6.", payload)
    assert ok and unsupported == []


def test_grounding_rejects_a_number_no_tool_produced():
    payload = {"quote": {"total": 22306.48}}
    ok, unsupported = validate_summary(
        "Total Rs 22,306.48 and you save Rs 4,200.", payload)
    assert not ok
    assert "4,200" in unsupported


def test_ungrounded_summary_is_replaced_by_the_engines_own_words(seeded):
    from engine.quote import calculate_quote

    quote = calculate_quote(seeded, CANONICAL_ITEMS).as_dict()
    fake = FakeBedrock([tool_use("calculate_quote", {"items": CANONICAL_ITEMS})])
    result = run_order_agent(seeded, "order", client=fake)

    honest = result.summary
    result = apply_summary(result, "You will save Rs 9,999 on this order.")
    assert result.grounded is False
    assert result.summary == honest          # the invented figure is discarded
    assert "9,999" in result.ungroundedNumbers


def test_grounded_model_summary_is_allowed_through(seeded):
    fake = FakeBedrock([tool_use("calculate_quote", {"items": CANONICAL_ITEMS})])
    result = run_order_agent(seeded, "order", client=fake)
    result = apply_summary(result, "The quotation comes to Rs 22306.48.")
    assert result.grounded is True
    assert result.summary == "The quotation comes to Rs 22306.48."


def test_collect_numbers_reads_values_inside_evidence_strings():
    found = collect_numbers({"evidence": {"line": "20 x 78.3 = 1566.0"}})
    assert {20.0, 78.3, 1566.0} <= found


def test_find_unsupported_numbers_tolerates_formatting():
    assert find_unsupported_numbers("Rs 22,306.48", {22306.48}) == []


def test_deterministic_summary_is_built_from_engine_figures(seeded):
    from engine.quote import calculate_quote

    quote = calculate_quote(seeded, CANONICAL_ITEMS).as_dict()
    summary = deterministic_quote_summary(quote)
    ok, _ = validate_summary(summary, {"quote": quote})
    assert ok
    assert "22306.48" in summary


# ---- bounds and safety ----

def test_agent_loop_is_bounded(seeded):
    fake = FakeBedrock([
        tool_use("search_catalog", {"requestedText": "wire"}, f"t{i}")
        for i in range(MAX_TURNS + 2)
    ])
    result = run_order_agent(seeded, "order", client=fake)
    assert result.status == STATUS_FAILED
    assert result.turns == MAX_TURNS
    assert len(fake.calls) == MAX_TURNS


def test_prose_only_reply_is_not_treated_as_a_completed_order(seeded):
    fake = FakeBedrock([text_turn("Sure, I can help you with that!")])
    result = run_order_agent(seeded, "order", client=fake)
    assert result.status == STATUS_FAILED
    assert result.quote is None


def test_oversized_order_is_rejected_before_reaching_the_model(seeded):
    fake = FakeBedrock([])
    with pytest.raises(OrderTooLongError):
        run_order_agent(seeded, "x" * (MAX_ORDER_CHARS + 1), client=fake)
    assert fake.calls == []


def test_empty_order_is_rejected(seeded):
    with pytest.raises(ValueError):
        run_order_agent(seeded, "   ", client=FakeBedrock([]))


def test_oversized_quantity_is_rejected(seeded):
    with pytest.raises(ToolError):
        run_tool(seeded, "calculate_quote",
                 {"items": [{"skuId": "SW-ANC-1W10A", "quantity": 999_999}]})


def test_too_many_lines_are_rejected(seeded):
    items = [{"skuId": "SW-ANC-1W10A", "quantity": 1} for _ in range(26)]
    with pytest.raises(ToolError):
        run_tool(seeded, "calculate_quote", {"items": items})


def test_unknown_tool_name_is_rejected(seeded):
    with pytest.raises(ToolError) as exc:
        run_tool(seeded, "delete_everything", {})
    assert exc.value.kind == "UNKNOWN_TOOL"
