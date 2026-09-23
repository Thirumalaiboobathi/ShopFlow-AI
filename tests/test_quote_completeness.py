"""A quotation must cover every product the customer asked for.

These tests exist because of a live run, not a hypothetical. The model leaked
`length: "90m"` from the wire line into the Anchor switch search; the matcher
correctly returned NOT_FOUND and correctly refused to invent a skuId; the
model then quoted the two lines that had resolved. The arithmetic was right
and the result came back QUOTED at Rs 20,740.48 - a bill for two of the three
products the customer asked for, with the third mentioned nowhere but the
match list.

The engines were right. The boundary was not guarded. These tests guard it.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from agent.grounding import unsatisfied_lines  # noqa: E402
from agent.orchestrator import (  # noqa: E402
    STATUS_NEEDS_CLARIFICATION,
    STATUS_QUOTED,
    run_order_agent,
)
from agent.tools import run_tool  # noqa: E402
from engine.quote import calculate_quote  # noqa: E402


# The three products of the canonical order, in the wordings that make each
# of them resolve, stay ambiguous, or fall out of the catalogue entirely.
SWITCH = "20 Anchor modular switches 1-Way 10A White"
WIRE = "3 coils Finolex 1.5 sq mm FR wire red 90m"
MCB = "2 Havells MCB SP 32A C-curve"

CANONICAL_ITEMS = [
    {"skuId": "SW-ANC-1W10A", "quantity": 20},
    {"skuId": "W-FIN-1.5-RED-90M", "quantity": 3},
    {"skuId": "MCB-HAV-SP-32A-C", "quantity": 2},
]
PARTIAL_ITEMS = [
    {"skuId": "W-FIN-1.5-RED-90M", "quantity": 3},
    {"skuId": "MCB-HAV-SP-32A-C", "quantity": 2},
]

CANONICAL_TOTAL = 22306.48
PARTIAL_TOTAL = 20740.48


class FakeBedrock:
    """Replays a scripted sequence of model turns."""

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


def tool_uses(*calls):
    """One assistant turn containing several tool calls, as the live run had."""
    return {"role": "assistant", "content": [
        {"toolUse": {"toolUseId": f"t{i}", "name": name, "input": args}}
        for i, (name, args) in enumerate(calls, start=1)
    ]}


def search(seeded, **args):
    """A real search payload, exactly as the agent loop records it."""
    return run_tool(seeded, "search_catalog", args)


def quote_of(seeded, items):
    return calculate_quote(seeded, items).as_dict()


# The leaked attribute that caused the live failure: a switch has no length.
# A switch line that cannot resolve, because another line's specification
# landed on it.
#
# This was the original leak - length="90m" from the wire line - and it is
# now repaired before the matcher ever sees it: `line_guard` removes a length
# the line's own words never state. See HISTORIC_LENGTH_LEAK below, which
# asserts exactly that. The completeness guard still has to work when a line
# fails for a reason the line guard cannot see, and a specification from
# another line is one: "SP 32A" and "1-Way 10A" are both legitimate ways to
# write a specification, and nothing deterministic can tell which line a
# specification came from. So that is what is used here.
LEAKED_SWITCH_SEARCH = {"requestedText": SWITCH, "brand": "Anchor",
                        "category": "Switch", "specification": "SP 32A",
                        "uom": "PIECE"}
HISTORIC_LENGTH_LEAK = {"requestedText": SWITCH, "brand": "Anchor",
                        "category": "Switch", "specification": "1-Way 10A",
                        "length": "90m", "uom": "PIECE"}
GOOD_SWITCH_SEARCH = {"requestedText": SWITCH, "brand": "Anchor",
                      "category": "Switch", "specification": "1-Way 10A",
                      "colour": "White"}
WIRE_SEARCH = {"requestedText": WIRE, "brand": "Finolex", "category": "Wire",
               "colour": "Red", "specification": "1.5 sq mm",
               "length": "90m", "uom": "COIL"}
MCB_SEARCH = {"requestedText": MCB, "brand": "Havells", "category": "MCB",
              "specification": "SP 32A C-curve"}


# ---------------------------------------------------------------------------
# The live failure, reproduced
# ---------------------------------------------------------------------------

def test_the_live_partial_quotation_is_not_returned_as_quoted(seeded):
    """The exact observed run: Anchor NOT_FOUND, wire and MCB resolved."""
    fake = FakeBedrock([
        tool_uses(("search_catalog", LEAKED_SWITCH_SEARCH),
                  ("search_catalog", WIRE_SEARCH),
                  ("search_catalog", MCB_SEARCH)),
        tool_use("calculate_quote", {"items": PARTIAL_ITEMS}, "t4"),
    ])
    result = run_order_agent(seeded, f"{SWITCH}, {WIRE}, {MCB}", client=fake)

    assert result.status != STATUS_QUOTED
    assert result.status == STATUS_NEEDS_CLARIFICATION
    assert result.quote is None


def test_the_partial_total_is_nowhere_in_the_result(seeded):
    """20,740.48 must not reach the owner as a figure, in any field."""
    fake = FakeBedrock([
        tool_use("search_catalog", LEAKED_SWITCH_SEARCH),
        tool_use("calculate_quote", {"items": PARTIAL_ITEMS}, "t2"),
    ])
    result = run_order_agent(seeded, "order", client=fake)

    assert result.quote is None
    payload = result.as_dict()
    for field in ("summary", "quote", "clarification", "message"):
        rendered = str(payload.get(field))
        assert "20740.48" not in rendered
        assert "20,740.48" not in rendered


def test_the_unresolved_line_stays_visible(seeded):
    """The owner is told which product was not quoted, in their own words."""
    fake = FakeBedrock([
        tool_use("search_catalog", LEAKED_SWITCH_SEARCH),
        tool_use("calculate_quote", {"items": PARTIAL_ITEMS}, "t2"),
    ])
    result = run_order_agent(seeded, "order", client=fake)

    assert result.clarification["requestedText"] == SWITCH
    assert result.clarification["question"]
    # The NOT_FOUND match is still in the record, unaltered.
    not_found = [m for m in result.matches if m["status"] == "NOT_FOUND"]
    assert len(not_found) == 1
    assert not_found[0]["requestedText"] == SWITCH
    assert not_found[0]["skuId"] is None


def test_the_guard_invents_no_sku(seeded):
    """Blocking a quote must never produce a skuId nobody searched for."""
    fake = FakeBedrock([
        tool_use("search_catalog", LEAKED_SWITCH_SEARCH),
        tool_use("calculate_quote", {"items": PARTIAL_ITEMS}, "t2"),
    ])
    result = run_order_agent(seeded, "order", client=fake)

    assert "skuId" not in result.clarification
    # A NOT_FOUND line has nothing to offer, and nothing is offered.
    assert result.clarification["options"] == []


# ---------------------------------------------------------------------------
# A. NOT_FOUND + resolved items -> cannot be QUOTED
# ---------------------------------------------------------------------------

def test_the_historic_length_leak_no_longer_reaches_the_matcher(seeded):
    """The ₹20,740.48 quote began here. The leak is now caught one step earlier.

    This does not replace the completeness guard and does not make it
    optional - every other test in this file still holds. It records that the
    specific contamination that produced an invalid partial quotation is now
    removed from the search before it runs, so the guard is no longer the only
    thing standing between it and a customer.
    """
    payload = search(seeded, **HISTORIC_LENGTH_LEAK)

    assert payload["status"] == "RESOLVED"
    assert payload["skuId"] == "SW-ANC-1W10A"
    assert [c["filter"] for c in payload["lineIsolation"]["changes"]] == ["length"]
    assert payload["lineIsolation"]["changes"][0]["to"] is None


def test_a_not_found_line_blocks_the_quote(seeded):
    matches = [search(seeded, **LEAKED_SWITCH_SEARCH),
               search(seeded, **WIRE_SEARCH)]
    assert matches[0]["status"] == "NOT_FOUND"
    assert matches[1]["status"] == "RESOLVED"

    missing = unsatisfied_lines(matches, quote_of(seeded, PARTIAL_ITEMS))
    assert [m["requestedText"] for m in missing] == [SWITCH]


# ---------------------------------------------------------------------------
# B. AMBIGUOUS + resolved items -> cannot be QUOTED
# ---------------------------------------------------------------------------

def test_an_ambiguous_line_left_out_of_the_quote_blocks_it(seeded):
    """Ambiguous, and none of its options were quoted: still unanswered."""
    ambiguous = search(seeded, requestedText="3 coils Finolex 1.5 sq mm wire")
    assert ambiguous["status"] == "AMBIGUOUS"

    # A quote containing only the MCB - the wire question was never answered.
    quote = quote_of(seeded, [{"skuId": "MCB-HAV-SP-32A-C", "quantity": 2}])
    missing = unsatisfied_lines([ambiguous], quote)
    assert len(missing) == 1
    assert missing[0]["status"] == "AMBIGUOUS"


def test_an_ambiguous_line_blocks_the_whole_agent_result(seeded):
    fake = FakeBedrock([
        tool_use("search_catalog",
                 {"requestedText": "3 coils Finolex 1.5 sq mm wire"}),
        tool_use("calculate_quote",
                 {"items": [{"skuId": "MCB-HAV-SP-32A-C", "quantity": 2}]},
                 "t2"),
    ])
    result = run_order_agent(seeded, "order", client=fake)

    assert result.status == STATUS_NEEDS_CLARIFICATION
    assert result.quote is None
    # The matcher's own options are offered - not re-derived, not invented.
    offered = {o["skuId"] for o in result.clarification["options"]}
    assert offered
    for sku in offered:
        assert sku in seeded.products


def test_an_ambiguous_line_answered_by_the_quote_is_satisfied(seeded):
    """The clarification round. The owner picked an option; it was quoted.

    This is why the guard consults the matcher's offered options and not only
    RESOLVED. Refusing this case would make the existing confirmation flow
    impossible to complete.
    """
    ambiguous = search(seeded, requestedText="20 Anchor modular switches",
                       brand="Anchor", category="Switch")
    assert ambiguous["status"] == "AMBIGUOUS"
    assert "SW-ANC-1W10A" in {o["skuId"] for o in ambiguous["options"]}

    assert unsatisfied_lines([ambiguous], quote_of(seeded, CANONICAL_ITEMS)) == []


# ---------------------------------------------------------------------------
# C. all requested lines RESOLVED -> QUOTED remains allowed
# ---------------------------------------------------------------------------

def test_every_line_resolved_and_quoted_is_allowed(seeded):
    matches = [search(seeded, **GOOD_SWITCH_SEARCH),
               search(seeded, **WIRE_SEARCH),
               search(seeded, **MCB_SEARCH)]
    assert [m["status"] for m in matches] == ["RESOLVED"] * 3
    assert unsatisfied_lines(matches, quote_of(seeded, CANONICAL_ITEMS)) == []


def test_the_canonical_order_still_quotes(seeded):
    """The canonical flow, unchanged: three lines, Rs 22,306.48."""
    fake = FakeBedrock([
        tool_uses(("search_catalog", GOOD_SWITCH_SEARCH),
                  ("search_catalog", WIRE_SEARCH),
                  ("search_catalog", MCB_SEARCH)),
        tool_use("calculate_quote", {"items": CANONICAL_ITEMS}, "t4"),
    ])
    result = run_order_agent(seeded, f"{SWITCH}, {WIRE}, {MCB}", client=fake)

    assert result.status == STATUS_QUOTED
    assert result.quote["total"] == CANONICAL_TOTAL
    assert len(result.quote["lines"]) == 3


def test_a_quote_with_no_searches_is_untouched(seeded):
    """Nothing was searched, so nothing can be shown to be missing.

    The confirmation round can reach this: the owner's choice is folded into
    the prompt and the model quotes it directly. calculate_quote already
    refuses a skuId that is not in the catalogue, so the SKU is still real.
    """
    quote = calculate_quote(seeded, CANONICAL_ITEMS).as_dict()
    assert unsatisfied_lines([], quote) == []


def test_a_quote_no_order_text_supports_is_held_for_its_quantity(seeded):
    """The same no-search quote, run end to end against an order text that
    states no quantities. This used to be QUOTED. Nothing in "order" says 20,
    3 or 2, so those numbers came from the model alone, and a quantity only
    the model vouches for is not one a customer is billed for."""
    fake = FakeBedrock([tool_use("calculate_quote", {"items": CANONICAL_ITEMS})])
    result = run_order_agent(seeded, "order", client=fake)

    assert result.status == STATUS_NEEDS_CLARIFICATION
    assert result.quote is None
    assert result.clarification["clarifyingAttribute"] == "quantity"


# ---------------------------------------------------------------------------
# D. the quote does not cover every requested line -> cannot be QUOTED
# ---------------------------------------------------------------------------

def test_a_resolved_line_dropped_from_the_quote_blocks_it(seeded):
    """Resolved perfectly, then simply left out. Still not a quotation.

    This is the coverage half of the invariant, and it is not the same defect
    as NOT_FOUND: here the matcher succeeded and the model silently omitted
    the line from calculate_quote.
    """
    matches = [search(seeded, **GOOD_SWITCH_SEARCH), search(seeded, **MCB_SEARCH)]
    assert [m["status"] for m in matches] == ["RESOLVED", "RESOLVED"]

    quote = quote_of(seeded, [{"skuId": "MCB-HAV-SP-32A-C", "quantity": 2}])
    missing = unsatisfied_lines(matches, quote)
    assert [m["requestedText"] for m in missing] == [SWITCH]


def test_a_dropped_resolved_line_blocks_the_agent_result(seeded):
    fake = FakeBedrock([
        tool_use("search_catalog", GOOD_SWITCH_SEARCH),
        tool_use("calculate_quote",
                 {"items": [{"skuId": "MCB-HAV-SP-32A-C", "quantity": 2}]},
                 "t2"),
    ])
    result = run_order_agent(seeded, "order", client=fake)

    assert result.status == STATUS_NEEDS_CLARIFICATION
    assert result.quote is None
    assert result.clarification["requestedText"] == SWITCH


# ---------------------------------------------------------------------------
# E. a line-count mismatch is not silently accepted
# ---------------------------------------------------------------------------

def test_more_requested_lines_than_quote_lines_is_caught(seeded):
    """Three products searched, two priced. The difference is not accepted."""
    matches = [search(seeded, **LEAKED_SWITCH_SEARCH),
               search(seeded, **WIRE_SEARCH),
               search(seeded, **MCB_SEARCH)]
    quote = quote_of(seeded, PARTIAL_ITEMS)

    assert len(quote["lines"]) == 2
    assert len({m["requestedText"] for m in matches}) == 3
    assert len(unsatisfied_lines(matches, quote)) == 1


def test_repeated_searches_for_one_line_count_once(seeded):
    """Refining a search is not two products. Distinct wording is the unit.

    len(matches) is not the requested line count: the model searches the same
    line again when the first attempt was too vague.
    """
    vague = search(seeded, **LEAKED_SWITCH_SEARCH)
    refined = search(seeded, **GOOD_SWITCH_SEARCH)
    assert vague["status"] == "NOT_FOUND"
    assert refined["status"] == "RESOLVED"

    assert unsatisfied_lines([vague, refined],
                             quote_of(seeded, CANONICAL_ITEMS)) == []


def test_whitespace_and_case_do_not_split_one_line_in_two(seeded):
    a = search(seeded, requestedText="  Finolex 1.5 sq mm RED wire 90m  ",
               uom="COIL")
    b = search(seeded, requestedText="finolex 1.5 sq mm red wire 90m",
               uom="COIL")
    quote = quote_of(seeded, [{"skuId": "W-FIN-1.5-RED-90M", "quantity": 3,
                               "uom": "COIL"}])
    assert unsatisfied_lines([a, b], quote) == []


# ---------------------------------------------------------------------------
# F. the deterministic quotation calculation is unchanged
# ---------------------------------------------------------------------------

def test_the_quotation_engine_is_not_touched_by_the_guard(seeded):
    """The guard withholds a quote. It never recalculates one."""
    quote = quote_of(seeded, CANONICAL_ITEMS)
    partial = quote_of(seeded, PARTIAL_ITEMS)
    assert quote["total"] == CANONICAL_TOTAL
    assert partial["total"] == PARTIAL_TOTAL  # the arithmetic was never wrong

    unsatisfied_lines([], quote)
    unsatisfied_lines([], partial)
    assert quote["total"] == CANONICAL_TOTAL
    assert partial["total"] == PARTIAL_TOTAL


# ---------------------------------------------------------------------------
# G. the existing clarification flow still works
# ---------------------------------------------------------------------------

def test_a_model_requested_clarification_is_left_alone(seeded):
    """The guard only ever inspects a QUOTED result."""
    fake = FakeBedrock([
        tool_use("search_catalog",
                 {"requestedText": "3 coils Finolex 1.5 sq mm wire"}),
        tool_use("request_clarification",
                 {"requestedText": "3 coils Finolex 1.5 sq mm wire",
                  "clarifyingAttribute": "colour",
                  "question": "Which colour?"}, "t2"),
    ])
    result = run_order_agent(seeded, "order", client=fake)

    assert result.status == STATUS_NEEDS_CLARIFICATION
    assert result.clarification["question"] == "Which colour?"
    assert result.summary == "Which colour?"
    assert {o["skuId"] for o in result.clarification["options"]}


def test_a_blocked_quote_looks_like_any_other_clarification(seeded):
    """Same shape, so the API, the frontend and the language layer need no
    change to render it."""
    fake = FakeBedrock([
        tool_use("search_catalog",
                 {"requestedText": "3 coils Finolex 1.5 sq mm wire"}),
        tool_use("calculate_quote",
                 {"items": [{"skuId": "MCB-HAV-SP-32A-C", "quantity": 2}]},
                 "t2"),
    ])
    result = run_order_agent(seeded, "order", client=fake)

    assert set(result.clarification) == {"requestedText", "clarifyingAttribute",
                                         "question", "options"}
    assert result.summary == result.clarification["question"]
    assert isinstance(result.clarification["clarifyingAttribute"], str)


def test_the_trace_still_records_what_happened(seeded):
    """Withholding the quote must not erase the evidence of the bad call."""
    fake = FakeBedrock([
        tool_use("search_catalog", LEAKED_SWITCH_SEARCH),
        tool_use("calculate_quote", {"items": PARTIAL_ITEMS}, "t2"),
    ])
    result = run_order_agent(seeded, "order", client=fake)

    assert [t["tool"] for t in result.trace] == ["search_catalog",
                                                 "calculate_quote"]


# ---------------------------------------------------------------------------
# The guard is generic
# ---------------------------------------------------------------------------

def test_the_guard_knows_nothing_about_anchor_or_the_canonical_order(seeded):
    """No brand, SKU, total or order text is special-cased."""
    import inspect

    from agent import grounding, orchestrator

    source = (inspect.getsource(grounding.unsatisfied_lines)
              + inspect.getsource(orchestrator._require_complete_quote))
    for token in ("Anchor", "ANC", "Finolex", "FIN", "Havells", "HAV",
                  "20740", "22306", "MCB-", "SW-", "W-FIN"):
        assert token not in source, f"{token} is special-cased in the guard"


def test_the_guard_works_on_an_unrelated_product(seeded):
    """Nothing about the canonical three products is required."""
    bell = search(seeded, requestedText="one Anchor bell push",
                  brand="Anchor", category="Switch", specification="Bell Push")
    socket_quote = quote_of(seeded, [{"skuId": "SW-ANC-SKT6A", "quantity": 1}])

    assert bell["status"] == "RESOLVED"
    assert bell["skuId"] not in {l["skuId"] for l in socket_quote["lines"]}
    assert len(unsatisfied_lines([bell], socket_quote)) == 1


# ---------------------------------------------------------------------------
# G. a line the model never searched for at all
# ---------------------------------------------------------------------------
# The hole this file did not cover. Every check above reads `matches`, which
# is the model's own account of the order, so a line it simply skipped is
# invisible to all of them and they all agree the quote is complete.
#
# Found live: a six-line order came back QUOTED with five lines on it. The
# arithmetic was right, no SKU was invented and nothing was ambiguous - the
# customer would simply have been billed for five of the six things they
# asked for. That is the Rs 20,740.48 defect arriving by a different road.

def test_a_product_named_in_the_order_but_never_searched_blocks_the_quote(seeded):
    fake = FakeBedrock([
        tool_uses(("search_catalog", GOOD_SWITCH_SEARCH),
                  ("search_catalog", WIRE_SEARCH)),
        tool_uses(("calculate_quote", {"items": [
            {"skuId": "SW-ANC-1W10A", "quantity": 20},
            {"skuId": "W-FIN-1.5-RED-90M", "quantity": 3, "uom": "COIL"},
        ]})),
    ])
    order = f"Anna, {SWITCH}, {WIRE}, {MCB}"
    result = run_order_agent(seeded, order, client=fake)

    assert result.status == STATUS_NEEDS_CLARIFICATION
    assert result.quote is None
    assert "Havells" in result.clarification["question"]


def test_the_question_does_not_say_the_product_is_unstocked(seeded):
    """Havells is on the shelf. Nobody looked for it - that is a different thing."""
    fake = FakeBedrock([
        tool_uses(("search_catalog", GOOD_SWITCH_SEARCH),
                  ("search_catalog", WIRE_SEARCH)),
        tool_uses(("calculate_quote", {"items": [
            {"skuId": "SW-ANC-1W10A", "quantity": 20},
            {"skuId": "W-FIN-1.5-RED-90M", "quantity": 3, "uom": "COIL"},
        ]})),
    ])
    question = run_order_agent(
        seeded, f"Anna, {SWITCH}, {WIRE}, {MCB}",
        client=fake).clarification["question"]

    assert "not in the catalogue" not in question
    assert "no product was looked up" in question


def test_a_fully_searched_order_is_not_blocked_by_the_coverage_check(seeded):
    """The check may only ever fire on something that was genuinely skipped."""
    fake = FakeBedrock([
        tool_uses(("search_catalog", GOOD_SWITCH_SEARCH),
                  ("search_catalog", WIRE_SEARCH),
                  ("search_catalog", {"requestedText": MCB, "brand": "Havells",
                                      "category": "MCB",
                                      "specification": "SP 32A"})),
        tool_uses(("calculate_quote", {"items": CANONICAL_ITEMS})),
    ])
    result = run_order_agent(seeded, f"Anna, {SWITCH}, {WIRE}, {MCB}",
                             client=fake)

    assert result.status == STATUS_QUOTED
    assert result.quote["total"] == CANONICAL_TOTAL
    assert len(result.quote["lines"]) == 3


def test_the_coverage_check_reads_only_real_catalogue_words(seeded):
    """A greeting or an adjective may not become a missing product."""
    from agent.line_guard import uncovered_terms

    matches = [search(seeded, **GOOD_SWITCH_SEARCH)]
    order = ("Anna, please, urgently, best quality, 20 Anchor modular "
             "switches 1-Way 10A White for the new site")
    assert uncovered_terms(seeded, order, matches) == []
