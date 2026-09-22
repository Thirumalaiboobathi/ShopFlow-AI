"""What happens when the model writes prose instead of calling a tool.

Two defects lived on this path, both found by probing the deployed system as
an adversarial evaluator would.

The first: a customer asking for something the shop does not stock came back
as job status FAILED, and the owner was told "The order could not be
processed." A wrong brand, an unfamiliar unit, a product nobody carries - all
ordinary shop conversations - were reported as application errors.

The second: the model's private <thinking> block reached `result.summary`, and
from there `GET /api/jobs/{id}`. When someone tried a prompt injection, the
model's deliberation about the attempt was served back to them.

Neither produced a wrong number. Both made a sound system look unsound.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from agent.grounding import unresolved_requests  # noqa: E402
from agent.orchestrator import (  # noqa: E402
    CLARIFY_FALLBACK,
    INCOMPLETE_SUMMARY,
    STATUS_FAILED,
    STATUS_NEEDS_CLARIFICATION,
    STATUS_QUOTED,
    _strip_reasoning,
    run_order_agent,
)
from agent.tools import run_tool  # noqa: E402

CANONICAL_ITEMS = [
    {"skuId": "SW-ANC-1W10A", "quantity": 20},
    {"skuId": "W-FIN-1.5-RED-90M", "quantity": 3},
    {"skuId": "MCB-HAV-SP-32A-C", "quantity": 2},
]
CANONICAL_TOTAL = 22306.48
PARTIAL_ITEMS = [
    {"skuId": "W-FIN-1.5-RED-90M", "quantity": 3},
    {"skuId": "MCB-HAV-SP-32A-C", "quantity": 2},
]


def _owner_facing(result) -> str:
    """Everything a shop owner or an API caller reads as ShopFlow's answer.

    `trace` is deliberately excluded. It is the raw record of what the model
    actually sent, and sanitizing it would falsify the evidence the trace
    exists to provide - see test_the_trace_is_a_raw_record_on_purpose.
    """
    payload = result.as_dict()
    return " ".join(str(payload.get(k) or "") for k in
                    ("summary", "message", "clarification", "quote"))


class FakeBedrock:
    def __init__(self, turns):
        self._turns = list(turns)
        self.calls = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        if not self._turns:
            raise AssertionError("fake model ran out of scripted turns")
        return {"output": {"message": self._turns.pop(0)}}


class ExplodingBedrock:
    """A genuine fault, not a model opinion."""

    def converse(self, **kwargs):
        raise RuntimeError("bedrock is on fire")


def tool_use(name, payload, use_id="t1"):
    return {"role": "assistant",
            "content": [{"toolUse": {"toolUseId": use_id, "name": name,
                                     "input": payload}}]}


def prose(text):
    return {"role": "assistant", "content": [{"text": text}]}


def search_then_prose(search_args, text):
    """The live shape: the model searches, then explains instead of calling."""
    return [tool_use("search_catalog", search_args), prose(text)]


# ---------------------------------------------------------------------------
# The sanitizer
# ---------------------------------------------------------------------------

def test_7_a_thinking_block_is_removed_and_the_answer_kept():
    out = _strip_reasoning(
        "<thinking>\nprivate reasoning here\n</thinking>\nSafe user-facing message")
    assert out == "Safe user-facing message"
    assert "private" not in out
    assert "thinking" not in out


def test_8_multiline_thinking_is_removed():
    out = _strip_reasoning(
        "<thinking>\nline 1\nline 2\nline 3\n</thinking>\nFinal answer")
    assert out == "Final answer"
    for hidden in ("line 1", "line 2", "line 3"):
        assert hidden not in out


def test_9_every_thinking_block_is_removed():
    out = _strip_reasoning(
        "<thinking>first</thinking>Alpha<thinking>second</thinking>Beta")
    assert "first" not in out and "second" not in out
    assert "Alpha" in out and "Beta" in out


def test_10_an_unclosed_thinking_block_exposes_nothing():
    """Truncated output must not become a partial confession."""
    out = _strip_reasoning("Visible part. <thinking>the model was about to say")
    assert out == "Visible part."
    assert "about to say" not in out
    assert "thinking" not in out


def test_10b_a_stray_closing_tag_is_removed():
    assert _strip_reasoning("Answer</thinking>") == "Answer"


def test_11_nothing_but_reasoning_sanitizes_to_empty():
    assert _strip_reasoning("<thinking>all of it was reasoning</thinking>") == ""
    assert _strip_reasoning("") == ""
    assert _strip_reasoning(None) == ""


def test_the_sanitizer_tolerates_tag_spacing_and_case():
    assert _strip_reasoning("< Thinking >secret</ THINKING >Shown") == "Shown"


def test_ordinary_text_is_left_alone():
    plain = "We have 20 of those in stock today."
    assert _strip_reasoning(plain) == plain


# ---------------------------------------------------------------------------
# 11. empty after sanitization -> deterministic fallback, never an empty summary
# ---------------------------------------------------------------------------

def test_11b_an_all_reasoning_reply_falls_back_to_a_deterministic_summary(seeded):
    fake = FakeBedrock([prose("<thinking>only reasoning, no answer</thinking>")])
    result = run_order_agent(seeded, "order", client=fake)

    assert result.status == STATUS_FAILED  # nothing was ever searched for
    assert result.summary == INCOMPLETE_SUMMARY
    assert result.summary  # never empty
    assert "reasoning" not in result.summary


# ---------------------------------------------------------------------------
# 1-4. a prose reply about an unresolved product is a clarification, not a fault
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("label,args", [
    # 2. unknown product
    ("unknown product", {"requestedText": "5 Siemens 3-phase contactor 40A",
                         "brand": "Siemens", "category": "Contactor"}),
    # 3. wrong brand for the category
    ("wrong brand", {"requestedText": "3 Anchor MCB SP 32A", "brand": "Anchor",
                     "category": "MCB", "specification": "SP 32A"}),
    # 4. wrong unit
    ("wrong uom", {"requestedText": "2 boxes of Finolex wire",
                   "brand": "Finolex", "category": "Wire", "uom": "BOX"}),
])
def test_1_an_unresolved_product_explained_in_prose_is_not_a_failure(
        seeded, label, args):
    """The catalogue said NOT_FOUND and the model explained. That is a shop
    conversation, not a system fault."""
    assert run_tool(seeded, "search_catalog", args)["status"] == "NOT_FOUND"

    fake = FakeBedrock(search_then_prose(
        args, "<thinking>not in the catalog</thinking> We do not carry that."))
    result = run_order_agent(seeded, "order", client=fake)

    assert result.status == STATUS_NEEDS_CLARIFICATION, label
    assert result.status != STATUS_FAILED
    assert result.quote is None
    # the customer's own wording comes back, and no SKU is invented
    assert result.clarification["requestedText"] == args["requestedText"]
    assert "skuId" not in result.clarification
    assert result.clarification["options"] == []
    assert "thinking" not in result.summary


def test_1b_an_ambiguous_line_explained_in_prose_offers_the_real_options(seeded):
    args = {"requestedText": "some Anchor modular switches", "brand": "Anchor",
            "category": "Switch"}
    assert run_tool(seeded, "search_catalog", args)["status"] == "AMBIGUOUS"

    fake = FakeBedrock(search_then_prose(args, "There are several of those."))
    result = run_order_agent(seeded, "order", client=fake)

    assert result.status == STATUS_NEEDS_CLARIFICATION
    offered = {o["skuId"] for o in result.clarification["options"]}
    assert offered
    for sku in offered:
        assert sku in seeded.products  # only real catalogue SKUs are offered


def test_1c_a_refined_search_is_not_reported_as_unresolved(seeded):
    """Vague, then precise. One product, and it resolved - so prose after it
    is the agent failing to finish, not an unanswered question."""
    text = "20 Anchor modular switches 1-Way 10A White"
    vague = {"requestedText": text, "brand": "Anchor", "category": "Switch",
             "length": "90m"}
    precise = {"requestedText": text, "brand": "Anchor", "category": "Switch",
               "specification": "1-Way 10A", "colour": "White"}
    assert run_tool(seeded, "search_catalog", vague)["status"] == "NOT_FOUND"
    assert run_tool(seeded, "search_catalog", precise)["status"] == "RESOLVED"

    assert unresolved_requests([run_tool(seeded, "search_catalog", vague),
                                run_tool(seeded, "search_catalog", precise)]) == []

    fake = FakeBedrock([tool_use("search_catalog", vague),
                        tool_use("search_catalog", precise, "t2"),
                        prose("All set.")])
    result = run_order_agent(seeded, "order", client=fake)
    assert result.status == STATUS_FAILED
    assert result.quote is None


# ---------------------------------------------------------------------------
# 5-6. prompt injection
# ---------------------------------------------------------------------------

def test_5_injection_for_supplier_cost_leaks_nothing(seeded):
    fake = FakeBedrock([prose(
        "<thinking>The customer is attempting to bypass the established "
        "process and obtain the supplier cost of 58.0 for SW-ANC-1W10A."
        "</thinking> I can only quote selling prices.")])
    result = run_order_agent(seeded, "order", client=fake)

    assert result.status != STATUS_QUOTED
    assert result.quote is None
    blob = str(result.as_dict())
    assert "thinking" not in blob
    assert "bypass the established" not in blob
    assert "58.0" not in blob          # the supplier cost the model named
    assert "I can only quote selling prices." in result.summary


def test_6_injection_for_a_fake_sku_invents_nothing(seeded):
    fake = FakeBedrock([prose(
        "<thinking>They want FAKE-SKU-999 priced at 1 rupee.</thinking> "
        "I can only use catalogue products.")])
    result = run_order_agent(seeded, "order", client=fake)

    assert result.status != STATUS_QUOTED
    assert result.quote is None
    blob = str(result.as_dict())
    assert "FAKE-SKU-999" not in blob
    assert "thinking" not in blob


def test_6b_an_injection_wrapped_around_a_real_unresolved_product(seeded):
    """Case C meeting case A: the safe outcome is still the clarification."""
    args = {"requestedText": "5 Siemens 3-phase contactor 40A",
            "brand": "Siemens"}
    fake = FakeBedrock(search_then_prose(
        args, "<thinking>ignore the injection</thinking> Not stocked."))
    result = run_order_agent(seeded, "order", client=fake)

    assert result.status == STATUS_NEEDS_CLARIFICATION
    assert result.quote is None
    assert "thinking" not in str(result.as_dict())


# ---------------------------------------------------------------------------
# 12-14. nothing that already worked has changed
# ---------------------------------------------------------------------------

def test_12_the_canonical_order_still_quotes(seeded):
    fake = FakeBedrock([tool_use("calculate_quote", {"items": CANONICAL_ITEMS})])
    result = run_order_agent(seeded, "order", client=fake)

    assert result.status == STATUS_QUOTED
    assert result.quote["total"] == CANONICAL_TOTAL
    assert len(result.quote["lines"]) == 3


def test_13_the_partial_quote_is_still_blocked(seeded):
    fake = FakeBedrock([
        tool_use("search_catalog",
                 {"requestedText": "20 Anchor modular switches 1-Way 10A White",
                  "brand": "Anchor", "category": "Switch", "length": "90m"}),
        tool_use("calculate_quote", {"items": PARTIAL_ITEMS}, "t2"),
    ])
    result = run_order_agent(seeded, "order", client=fake)

    assert result.status == STATUS_NEEDS_CLARIFICATION
    assert result.quote is None
    assert "20740.48" not in str(result.as_dict())


def test_14_a_genuine_fault_is_still_a_failure(seeded):
    """Not every problem is a clarification. A broken dependency still raises."""
    with pytest.raises(RuntimeError):
        run_order_agent(seeded, "order", client=ExplodingBedrock())


def test_14b_repeated_invalid_tool_arguments_still_fail(seeded):
    bad = {"items": [{"skuId": "NOPE", "quantity": 1}]}
    fake = FakeBedrock([tool_use("calculate_quote", bad, f"t{i}")
                        for i in range(4)])
    result = run_order_agent(seeded, "order", client=fake)

    assert result.status == STATUS_FAILED
    assert "invalid tool arguments" in result.message


def test_14c_an_agent_that_never_finishes_still_fails(seeded):
    fake = FakeBedrock([tool_use("search_catalog", {"requestedText": "wire"},
                                 f"t{i}") for i in range(8)])
    result = run_order_agent(seeded, "order", client=fake)
    assert result.status == STATUS_FAILED


def test_14d_prose_with_nothing_searched_is_still_a_failure(seeded):
    """The existing contract: no evidence of a product means no clarification
    to offer. Guessing one would be inventing a request the customer did not
    make."""
    fake = FakeBedrock([prose("Sure, I can help you with that!")])
    result = run_order_agent(seeded, "order", client=fake)

    assert result.status == STATUS_FAILED
    assert result.quote is None


# ---------------------------------------------------------------------------
# the clarification a model asks for is sanitized too
# ---------------------------------------------------------------------------

def test_a_model_written_question_is_sanitized(seeded):
    fake = FakeBedrock([
        tool_use("search_catalog", {"requestedText": "Finolex 1.5 sq mm wire"}),
        tool_use("request_clarification",
                 {"requestedText": "Finolex 1.5 sq mm wire",
                  "clarifyingAttribute": "colour",
                  "question": "<thinking>offer colours</thinking> Which colour?"},
                 "t2"),
    ])
    result = run_order_agent(seeded, "order", client=fake)

    assert result.clarification["question"] == "Which colour?"
    assert result.summary == "Which colour?"
    facing = _owner_facing(result)
    assert "offer colours" not in facing
    assert "thinking" not in facing


def test_a_question_that_is_all_reasoning_falls_back(seeded):
    fake = FakeBedrock([
        tool_use("search_catalog", {"requestedText": "Finolex 1.5 sq mm wire"}),
        tool_use("request_clarification",
                 {"requestedText": "Finolex 1.5 sq mm wire",
                  "clarifyingAttribute": "colour",
                  "question": "<thinking>thinking out loud</thinking>"}, "t2"),
    ])
    result = run_order_agent(seeded, "order", client=fake)

    assert result.clarification["question"] == CLARIFY_FALLBACK
    assert result.summary == CLARIFY_FALLBACK
    assert "thinking out loud" not in _owner_facing(result)


# ---------------------------------------------------------------------------
# the contract is unchanged, and the fix is generic
# ---------------------------------------------------------------------------

def test_the_clarification_shape_is_identical_on_both_paths(seeded):
    """A withheld quote and an unresolved product render through one path."""
    from_prose = FakeBedrock(search_then_prose(
        {"requestedText": "5 Siemens contactor", "brand": "Siemens"}, "No."))
    a = run_order_agent(seeded, "order", client=from_prose)

    from_guard = FakeBedrock([
        tool_use("search_catalog",
                 {"requestedText": "20 Anchor switches", "brand": "Anchor",
                  "category": "Switch", "length": "90m"}),
        tool_use("calculate_quote", {"items": PARTIAL_ITEMS}, "t2"),
    ])
    b = run_order_agent(seeded, "order", client=from_guard)

    assert set(a.clarification) == set(b.clarification)
    assert set(a.clarification) == {"requestedText", "clarifyingAttribute",
                                    "question", "options"}
    assert a.summary == a.clarification["question"]
    assert b.summary == b.clarification["question"]


def test_no_new_field_was_added_to_the_result(seeded):
    fake = FakeBedrock([tool_use("calculate_quote", {"items": CANONICAL_ITEMS})])
    result = run_order_agent(seeded, "order", client=fake)
    assert set(result.as_dict()) == {
        "status", "summary", "quote", "clarification", "matches", "trace",
        "grounded", "ungroundedNumbers", "modelId", "turns", "elapsedMs",
        "message"}


def test_the_prose_classifier_is_generic():
    """No brand, SKU or total is special-cased."""
    import inspect

    from agent import grounding, orchestrator

    source = (inspect.getsource(grounding.unresolved_requests)
              + inspect.getsource(orchestrator._needs_clarification)
              + inspect.getsource(orchestrator._strip_reasoning))
    for token in ("Anchor", "ANC", "Finolex", "FIN", "Havells", "HAV",
                  "Siemens", "20740", "22306", "MCB-", "SW-", "W-FIN"):
        assert token not in source, f"{token} is special-cased"


def test_the_trace_is_a_raw_record_on_purpose(seeded):
    """The trace keeps what the model sent, verbatim, and that is correct.

    If a tool argument ever carried reasoning, the trace would still show it,
    because the trace is the audit record of the model's actual behaviour and
    a sanitized audit record is worth nothing. The boundary this module
    defends is the answer - summary, message, clarification - not the record.
    """
    fake = FakeBedrock([
        tool_use("search_catalog", {"requestedText": "Finolex 1.5 sq mm wire"}),
        tool_use("request_clarification",
                 {"requestedText": "Finolex 1.5 sq mm wire",
                  "clarifyingAttribute": "colour",
                  "question": "<thinking>aside</thinking> Which colour?"}, "t2"),
    ])
    result = run_order_agent(seeded, "order", client=fake)

    assert "aside" in str(result.trace)          # the record is intact
    assert "aside" not in _owner_facing(result)  # the answer is clean
