"""Stage 5.1 - the shop's durable confirmed supplier purchase cost.

The engine half: what a cost record is, who may create one, and how a newer
confirmation supersedes an older one. The API half - that confirming persists
and that the planner reads it back with no job id - lives in test_api.py.
"""

from __future__ import annotations

import pytest

from engine.cost_records import (
    CONFIRMED_SUPPLIER_PRICE,
    CURRENCY,
    DEFAULT_SHOP_ID,
    InvalidCostRecordError,
    build_cost_record,
    cost_pk,
    cost_sk,
    latest_confirmed_costs,
    to_plan_decisions,
)
from engine.purchasing import build_purchase_plan

WIRE = "W-FIN-1.5-RED-90M"
JOB = "a" * 32


def record(seeded, **overrides):
    kwargs = dict(
        sku_id=WIRE, cost=6300.0, source_job_id=JOB, confirmed_at=1_700_000_000,
        effective_date="15-09-2026",
    )
    kwargs.update(overrides)
    return build_cost_record(seeded, **kwargs)


# ---------------------------------------------------------------------------
# 1  the record itself
# ---------------------------------------------------------------------------

def test_1_a_confirmation_builds_a_complete_cost_record(seeded):
    r = record(seeded)

    assert r["PK"] == cost_pk(DEFAULT_SHOP_ID) == "SHOP#demo"
    assert r["SK"] == cost_sk(WIRE) == f"COST#{WIRE}"
    assert r["shopId"] == DEFAULT_SHOP_ID
    assert r["skuId"] == WIRE
    assert r["supplierId"] == seeded.product(WIRE).supplierId
    assert r["confirmedCost"] == 6300.0
    assert r["currency"] == CURRENCY == "INR"
    assert r["effectiveDate"] == "15-09-2026"
    assert r["sourceJobId"] == JOB
    assert r["confirmedAt"] == 1_700_000_000


def test_1b_a_cost_record_never_expires(seeded):
    """Job records carry a TTL. Shop state must not."""
    assert "expiresAt" not in record(seeded)


def test_1c_a_cost_record_carries_no_selling_price_and_no_stock(seeded):
    r = record(seeded)
    assert "sellingPrice" not in r
    assert "onHand" not in r
    assert "quantity" not in r


def test_1d_the_supplier_comes_from_the_catalogue_not_the_document(seeded):
    """A price list must not be able to reassign a SKU to another supplier."""
    r = record(seeded)
    assert r["supplierId"] == seeded.product(WIRE).supplierId


# ---------------------------------------------------------------------------
# 5  only a confirmation may write one
# ---------------------------------------------------------------------------

def test_5_a_rejected_decision_cannot_build_a_cost_record(seeded):
    with pytest.raises(InvalidCostRecordError, match="CONFIRMED"):
        record(seeded, decision="REJECTED")


def test_5b_an_unknown_sku_is_refused(seeded):
    with pytest.raises(InvalidCostRecordError, match="unknown SKU"):
        record(seeded, sku_id="NO-SUCH-SKU")


@pytest.mark.parametrize("bad", [0, -1, "lots", None, float("nan"), float("inf")])
def test_5c_an_unusable_cost_is_refused(seeded, bad):
    with pytest.raises(InvalidCostRecordError):
        record(seeded, cost=bad)


# ---------------------------------------------------------------------------
# 6-7  supersede, and keep the trail back to the document
# ---------------------------------------------------------------------------

def test_6_the_newer_confirmation_supersedes_the_older():
    rows = [
        {"skuId": WIRE, "confirmedCost": 6300.0, "confirmedAt": 100,
         "sourceJobId": "old"},
        {"skuId": WIRE, "confirmedCost": 6500.0, "confirmedAt": 200,
         "sourceJobId": "new"},
    ]
    latest = latest_confirmed_costs(rows)
    assert latest[WIRE]["cost"] == 6500.0
    assert latest[WIRE]["sourceJobId"] == "new"


def test_6b_order_of_arrival_does_not_decide_the_winner():
    rows = [
        {"skuId": WIRE, "confirmedCost": 6500.0, "confirmedAt": 200},
        {"skuId": WIRE, "confirmedCost": 6300.0, "confirmedAt": 100},
    ]
    assert latest_confirmed_costs(rows)[WIRE]["cost"] == 6500.0


def test_7_the_source_job_id_is_retained_through_to_the_planner(seeded):
    latest = latest_confirmed_costs([
        {"skuId": WIRE, "confirmedCost": 6300.0, "confirmedAt": 100,
         "sourceJobId": JOB, "currency": "INR"},
    ])
    assert latest[WIRE]["sourceJobId"] == JOB

    decisions = to_plan_decisions(latest)
    assert decisions == [{
        "skuId": WIRE, "decision": "CONFIRMED", "currentPrice": 6300.0,
        "sourceJobId": JOB, "confirmedAt": 100,
    }]

    plan = build_purchase_plan(seeded, 25000.0, decisions)
    line = next(l for l in plan["commitments"] if l["skuId"] == WIRE)
    assert line["costProvenance"]["sourceJobId"] == JOB
    assert plan["confirmedCosts"][0]["sourceJobId"] == JOB


def test_latest_confirmed_costs_ignores_unusable_rows():
    rows = [
        {"skuId": "A", "confirmedCost": 10.0, "confirmedAt": 1},
        {"skuId": "B", "confirmedCost": 0, "confirmedAt": 1},
        {"skuId": "C", "confirmedCost": "abc", "confirmedAt": 1},
        {"skuId": None, "confirmedCost": 5.0, "confirmedAt": 1},
        "not a dict",
    ]
    assert list(latest_confirmed_costs(rows)) == ["A"]


# ---------------------------------------------------------------------------
# provenance in the plan
# ---------------------------------------------------------------------------

def test_provenance_says_confirmed_for_a_confirmed_sku_and_seeded_otherwise(seeded):
    decisions = to_plan_decisions(latest_confirmed_costs([
        {"skuId": WIRE, "confirmedCost": 6300.0, "confirmedAt": 100,
         "sourceJobId": JOB},
    ]))
    plan = build_purchase_plan(seeded, 25000.0, decisions)

    wire = next(l for l in plan["commitments"] if l["skuId"] == WIRE)
    assert wire["costSource"] == CONFIRMED_SUPPLIER_PRICE

    others = [l for l in plan["commitments"] + plan["restockSelected"]
              if l["skuId"] != WIRE]
    assert others
    for line in others:
        assert line["costSource"] == "SEEDED_SUPPLIER_PRICE"
        assert line["costProvenance"]["sourceJobId"] is None


def test_provenance_is_not_inferred_from_the_figures(seeded):
    """A confirmed price equal to the seeded one is still confirmed.

    Guards against a tempting shortcut - deciding provenance by comparing the
    plan's cost against the seed - which would mislabel exactly the case where
    the owner agreed the supplier had NOT moved.
    """
    from engine.pricing import current_cost

    same = current_cost(seeded, WIRE)
    decisions = to_plan_decisions(latest_confirmed_costs([
        {"skuId": WIRE, "confirmedCost": same, "confirmedAt": 100,
         "sourceJobId": JOB},
    ]))
    plan = build_purchase_plan(seeded, 25000.0, decisions)

    wire = next(l for l in plan["commitments"] if l["skuId"] == WIRE)
    assert wire["unitCost"] == same
    assert wire["costSource"] == CONFIRMED_SUPPLIER_PRICE
    assert "costBasis" not in wire  # nothing changed, so nothing to report


def test_4_confirmed_cost_never_touches_selling_price(seeded):
    before = seeded.product(WIRE).sellingPrice
    decisions = to_plan_decisions(latest_confirmed_costs([
        {"skuId": WIRE, "confirmedCost": 6300.0, "confirmedAt": 100,
         "sourceJobId": JOB},
    ]))
    plan = build_purchase_plan(seeded, 25000.0, decisions)

    wire = next(l for l in plan["commitments"] if l["skuId"] == WIRE)
    assert wire["unitCost"] == 6300.0
    assert wire["sellingPrice"] == before
    assert seeded.product(WIRE).sellingPrice == before
