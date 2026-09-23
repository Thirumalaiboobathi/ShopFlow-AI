"""Telling the model why a search matched nothing.

Measured live, five runs of the documented canonical order in a row:

    run1: QUOTED              Rs 22,306.48
    run2: NEEDS_CLARIFICATION [NOT_FOUND, RESOLVED, NOT_FOUND]
    run3: NEEDS_CLARIFICATION [NOT_FOUND, RESOLVED, NOT_FOUND]
    run4: NEEDS_CLARIFICATION [NOT_FOUND, RESOLVED, NOT_FOUND]
    run5: NEEDS_CLARIFICATION [NOT_FOUND, RESOLVED, NOT_FOUND]

Four identical traces showed the cause: the model was sending the quantity as
the length. "20 Anchor modular switches" became `length="20"`, and since no
switch has a length, the filter excluded every real candidate. The matcher was
right every time, and `NOT_FOUND` told the model only "this product is not in
the catalogue" - which was true of the search and false of the product, and
gave it nothing to correct.

So the tool now says which stated attribute removed the last candidate. The
deterministic layer is unchanged: the diagnostic is computed by re-running the
same `search_catalog` with one attribute dropped at a time, and the verdict of
the search that counts still stands.

The dangerous version of this idea is the one these tests exist to prevent. A
diagnostic that suggested relaxing the brand would answer "no Siemens
contactor" with a Havells one, and a diagnostic that suggested dropping the
unit would sell a coil to someone who asked for a box. Neither is allowed to
happen, whatever it would do for the success rate.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from agent.tools import (  # noqa: E402
    _DIAGNOSABLE_FILTERS,
    _IDENTITY_FILTERS,
    run_tool,
)
from engine.matching import NOT_FOUND, RESOLVED  # noqa: E402


def search(seeded, **args):
    return run_tool(seeded, "search_catalog", args)


# The exact arguments from the four identical live failure traces.
LIVE_SWITCH = {"requestedText": "20 Anchor modular switches 1-Way 10A",
               "brand": "Anchor", "category": "Switch",
               "specification": "1-Way 10A", "length": "20", "uom": "PIECE"}
LIVE_MCB = {"requestedText": "2 Havells MCB SP 32A", "brand": "Havells",
            "category": "MCB", "specification": "SP 32A", "length": "2",
            "uom": "PIECE"}

# A length the line really does state, on a product that has none. The line
# guard leaves this one alone - the customer wrote "180m", so it is a fact
# about the request - and it is the diagnostic's job from here.
STATED_LENGTH_ON_A_SWITCH = {
    "requestedText": "20 Anchor modular switches 1-Way 10A 180m",
    "brand": "Anchor", "category": "Switch", "length": "180m"}


# ---------------------------------------------------------------------------
# The live failure no longer reaches the matcher at all
# ---------------------------------------------------------------------------
# These two cases are why `line_guard` exists. A quantity sent as a length is
# contradicted by the line's own words - "20 Anchor modular switches" states
# no length - so it is now removed before the search runs, and the search
# resolves. The diagnostic is what was built when the model had to notice its
# own mistake; the guard means it no longer has to. Both are kept: the guard
# handles what the line's words contradict, the diagnostic explains what they
# support.

@pytest.mark.parametrize("label,args,sku", [
    ("switch", LIVE_SWITCH, "SW-ANC-1W10A"),
    ("mcb", LIVE_MCB, "MCB-HAV-SP-32A-C"),
])
def test_the_quantity_in_the_length_field_is_removed_before_the_search(
        seeded, label, args, sku):
    payload = search(seeded, **args)

    assert payload["status"] == RESOLVED
    assert payload["skuId"] == sku

    dropped = [c for c in payload["lineIsolation"]["changes"]
               if c["filter"] == "length"]
    assert len(dropped) == 1
    assert dropped[0]["action"] == "dropped"
    assert dropped[0]["from"] == args["length"]
    assert dropped[0]["to"] is None


def test_a_length_the_line_really_states_is_still_diagnosed(seeded):
    """The guard removes what the line contradicts. The rest is explained."""
    payload = search(seeded, **STATED_LENGTH_ON_A_SWITCH)

    assert payload["status"] == NOT_FOUND
    assert payload["diagnostic"]["excludingFilters"] == ["length"]

    note = " ".join(payload["diagnostic"]["notes"])
    assert "length" in note
    assert "180m" in note
    assert "none of them has a length at all" in note
    assert "search_catalog again" in payload["instruction"]
    assert "Do not invent a skuId" in payload["instruction"]


def test_the_corrected_search_then_resolves(seeded):
    """The point of the diagnostic: the next search works."""
    corrected = {k: v for k, v in STATED_LENGTH_ON_A_SWITCH.items()
                 if k != "length"}
    assert search(seeded, **corrected)["status"] == RESOLVED


# ---------------------------------------------------------------------------
# What the diagnostic must never suggest
# ---------------------------------------------------------------------------

def test_brand_is_never_offered_as_something_to_relax(seeded):
    """"No Siemens contactor" must not become "here is a Havells one"."""
    payload = search(seeded, requestedText="5 Siemens 3-phase contactor 40A",
                     brand="Siemens")

    assert payload["status"] == NOT_FOUND
    assert payload["diagnostic"]["excludingFilters"] == []
    assert payload["instruction"] == (
        "This product is not in the catalogue. Do not invent a skuId.")


def test_category_is_never_offered_as_something_to_relax(seeded):
    """Asking for an MCB must not be answered with a switch."""
    payload = search(seeded, requestedText="3 Anchor MCB SP 32A",
                     brand="Anchor", category="MCB", specification="SP 32A")

    assert payload["status"] == NOT_FOUND
    assert "category" not in payload["diagnostic"]["excludingFilters"]
    assert "brand" not in payload["diagnostic"]["excludingFilters"]


def test_identity_filters_can_never_appear_in_excluding_filters(seeded):
    """Structural, not example-based: the two sets cannot overlap."""
    for args in (LIVE_SWITCH, LIVE_MCB, STATED_LENGTH_ON_A_SWITCH,
                 {"requestedText": "Polycab 1.5 sqmm wire", "brand": "Polycab",
                  "category": "Wire", "colour": "Pink"},
                 {"requestedText": "Anchor switch", "brand": "Anchor",
                  "category": "Switch", "colour": "Turquoise"}):
        payload = search(seeded, **args)
        excluding = payload.get("diagnostic", {}).get("excludingFilters", [])
        for identity in _IDENTITY_FILTERS:
            assert identity not in excluding, (identity, args)
        # And the line guard may not widen a brand either, by any route.
        for change in (payload.get("lineIsolation") or {}).get("changes", []):
            assert not (change["filter"] == "brand" and change["to"] is None)


def test_a_unit_mismatch_is_a_question_not_a_filter_to_drop(seeded):
    """The matcher refuses a box on purpose. The diagnostic must not undo it."""
    payload = search(seeded, requestedText="2 boxes of Finolex 1.5 sqmm red wire 90m",
                     brand="Finolex", category="Wire", colour="Red",
                     length="90m", uom="BOX")

    assert payload["status"] == NOT_FOUND
    assert "uom" not in payload["diagnostic"]["excludingFilters"]

    note = " ".join(payload["diagnostic"]["notes"])
    assert "stocked as coil" in note
    assert "do not search again without the unit" in note.lower()
    assert "request_clarification" in note
    assert "Do not convert the quantity" in note


def test_uom_is_not_a_diagnosable_filter():
    assert "uom" not in _DIAGNOSABLE_FILTERS
    assert set(_IDENTITY_FILTERS) == {"brand", "category"}


# ---------------------------------------------------------------------------
# The diagnostic hands over no business data
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("args", [
    LIVE_SWITCH, LIVE_MCB,
    {"requestedText": "5 Siemens contactor", "brand": "Siemens"},
    {"requestedText": "2 boxes of Finolex wire", "brand": "Finolex",
     "uom": "BOX"},
])
def test_no_sku_and_no_cost_ever_appears_in_a_diagnostic(seeded, args):
    """A count says a better search exists. Only a search may give an id."""
    blob = json.dumps(search(seeded, **args).get("diagnostic", {}))
    assert "skuId" not in blob
    for internal in ("costPrice", "supplierPrice", "marginPerUnit",
                     "marginPerRupee", "sellingPrice"):
        assert internal not in blob, internal


def test_the_diagnostic_is_bounded(seeded):
    """One note per stated attribute at most - never a dump of the catalogue."""
    diagnostic = search(seeded, **STATED_LENGTH_ON_A_SWITCH)["diagnostic"]
    assert len(diagnostic["notes"]) <= len(_DIAGNOSABLE_FILTERS) + 1
    assert len(json.dumps(diagnostic)) < 2000


# ---------------------------------------------------------------------------
# The matcher's own verdicts are untouched
# ---------------------------------------------------------------------------

def test_a_resolved_search_is_unchanged(seeded):
    payload = search(seeded, requestedText="20 Anchor modular switches 1-Way 10A White",
                     brand="Anchor", category="Switch",
                     specification="1-Way 10A", colour="White")
    assert payload["status"] == RESOLVED
    assert payload["skuId"] == "SW-ANC-1W10A"
    assert "diagnostic" not in payload        # only a miss is explained
    assert payload["instruction"].startswith("Use this skuId")


def test_an_ambiguous_search_is_unchanged(seeded):
    payload = search(seeded, requestedText="Anchor modular switches",
                     brand="Anchor", category="Switch")
    assert payload["status"] == "AMBIGUOUS"
    assert "diagnostic" not in payload
    assert {o["skuId"] for o in payload["options"]}


def test_the_diagnostic_changes_no_verdict(seeded):
    """Every search still returns exactly the status it returned before."""
    cases = [
        # LIVE_SWITCH and LIVE_MCB used to be NOT_FOUND here. What changed is
        # the arguments, not the matcher: the line guard removes a length the
        # line never stated, and the matcher then sees a search it has always
        # resolved. See test_the_quantity_in_the_length_field_is_removed...
        (STATED_LENGTH_ON_A_SWITCH, NOT_FOUND),
        ({"requestedText": "5 Siemens contactor", "brand": "Siemens"}, NOT_FOUND),
        ({"requestedText": "20 Anchor modular switches 1-Way 10A White",
          "brand": "Anchor", "category": "Switch", "specification": "1-Way 10A",
          "colour": "White"}, RESOLVED),
        ({"requestedText": "Anchor modular switches", "brand": "Anchor",
          "category": "Switch"}, "AMBIGUOUS"),
    ]
    for args, expected in cases:
        assert search(seeded, **args)["status"] == expected, args


def test_a_search_with_no_attributes_gets_no_advice(seeded):
    """Nothing was stated, so nothing can be questioned."""
    payload = search(seeded, requestedText="something we do not sell at all")
    assert payload["status"] == NOT_FOUND
    assert payload["diagnostic"]["excludingFilters"] == []
    assert payload["diagnostic"]["notes"] == []
