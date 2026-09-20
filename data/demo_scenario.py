"""Build the canonical ShopFlow demo scenario from the seeded dataset.

Every figure here is computed by the engine. Nothing is typed in. This module
is the single source the API, the UI, the tests and the demo video all read
from, so a change to the seed propagates everywhere at once.

Run `python data/demo_scenario.py` to print the scenario and write the JSON.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from data.generator import DEMO_ORDER_SKUS, build_dataset  # noqa: E402
from engine.budget import allocate_budget  # noqa: E402
from engine.models import Dataset  # noqa: E402
from engine.pricing import (  # noqa: E402
    PRICE_ALERT_THRESHOLD_PERCENT,
    cheaper_alternatives,
    current_cost,
    price_delta,
)
from engine.scenarios import scenario_report  # noqa: E402
from engine.shortage import shortages  # noqa: E402
from engine.velocity import coverage_for, velocity_for  # noqa: E402

# The cash the owner says is available in the demo. Chosen because the engine
# shows it funding every commitment while leaving restocking genuinely
# contested - see `assert_budget_binds`.
DEMO_BUDGET = 25000.0


def build_scenario(data: Dataset | None = None, budget: float = DEMO_BUDGET) -> dict:
    data = data or build_dataset()
    order = data.orders[0]

    order_view = []
    for line in order.lines:
        p = data.product(line.skuId)
        order_view.append({
            "skuId": p.skuId,
            "name": p.name,
            "quantity": line.quantity,
            "onHand": data.onHand(p.skuId),
            "sellingPrice": p.sellingPrice,
            "lineTotal": round(p.sellingPrice * line.quantity, 2),
            "weeklyVelocity": round(velocity_for(data, p.skuId).weeklyVelocity, 2),
            "coverageWeeks": round(coverage_for(data, p.skuId), 2),
        })

    quote_total = round(sum(l["lineTotal"] for l in order_view), 2)
    plan = allocate_budget(data, budget)

    # Every movement on an ordered item is reported, but only those above the
    # alert threshold are flagged - dealer rates drift constantly and a 0.6%
    # move is not something to interrupt the owner about.
    deltas = []
    for line in order.lines:
        d = price_delta(data, line.skuId)
        if d and d.percentChange:
            deltas.append({
                **d.as_evidence(),
                "isAlert": d.percentChange > PRICE_ALERT_THRESHOLD_PERCENT,
            })

    return {
        "shopId": "demo",
        "order": {
            "orderId": order.orderId,
            "customerName": order.customerName,
            "placedDate": order.placedDate,
            "promisedDate": order.promisedDate,
            "lines": order_view,
            "quoteTotal": quote_total,
        },
        "shortages": [s.as_evidence() for s in shortages(data)],
        "priceChanges": deltas,
        "cheaperAlternatives": [
            a.as_evidence()
            for line in order.lines
            for a in cheaper_alternatives(data, line.skuId)
        ],
        "budgetPlan": plan.as_dict(),
        "scenarioCounts": scenario_report(data),
    }


def assert_budget_binds(scenario: dict) -> None:
    """The demo is only honest if the budget forces a real tradeoff.

    Two conditions must hold: every customer commitment is funded, and at least
    one discretionary restock is turned down for lack of cash. If either fails
    the budget is theatre and the number must be re-chosen.
    """
    plan = scenario["budgetPlan"]
    if not plan["allCommitmentsFunded"]:
        raise AssertionError("Tier 1 commitments are not fully funded")
    refused = [d for d in plan["deferred"] if d["tier"] == "RESTOCK"]
    if not refused:
        raise AssertionError("budget does not bind - nothing was deferred")


SCENARIO_PATH = (
    Path(__file__).resolve().parents[1] / "backend" / "seed_data" / "demo_scenario.json"
)


def main() -> None:
    scenario = build_scenario()
    assert_budget_binds(scenario)
    SCENARIO_PATH.write_text(json.dumps(scenario, indent=2), encoding="utf-8")

    order = scenario["order"]
    plan = scenario["budgetPlan"]
    print(f"ORDER {order['orderId']} - {order['customerName']}")
    for l in order["lines"]:
        print(f"  {l['quantity']:>3} x {l['name']}")
        print(f"      on hand {l['onHand']}, velocity {l['weeklyVelocity']}/wk, "
              f"cover {l['coverageWeeks']} wk")
    print(f"  QUOTE TOTAL  Rs {order['quoteTotal']:,.2f}\n")

    print("SHORTAGES")
    for s in scenario["shortages"]:
        print(f"  {s['skuId']:<24} need {s['committedQty']:>3}  have {s['onHand']:>3}"
              f"  short {s['shortageQty']:>3}")

    print("\nPRICE CHANGES ON ORDERED ITEMS")
    for d in scenario["priceChanges"]:
        print(f"  {d['skuId']:<24} {d['previousCost']} -> {d['currentCost']} "
              f"({d['percentChange']:+}%)")

    print("\nCHEAPER SUPPLIER OPTIONS")
    for a in scenario["cheaperAlternatives"]:
        print(f"  {a['skuId']:<24} {a['supplierName']} Rs {a['unitCost']} "
              f"vs Rs {a['incumbentCost']} (save {a['savingPercent']}%)")

    print(f"\nBUDGET Rs {plan['budget']:,.2f}")
    print(f"  Tier 1 committed orders  Rs {plan['tier1Spend']:,.2f}")
    print(f"  Tier 2 restocking        Rs {plan['tier2Spend']:,.2f}")
    print(f"  Total spend              Rs {plan['totalSpend']:,.2f}")
    print(f"  Remaining                Rs {plan['remaining']:,.2f}")

    print(f"\nPURCHASED ({len(plan['purchased'])} lines)")
    for l in plan["purchased"][:10]:
        print(f"  [{l['tier']:<16}] {l['decision']:<7} {l['skuId']:<24} "
              f"x{l['fundedQty']:<4} Rs {l['lineCost']:>10,.2f}")

    deferred = [d for d in plan["deferred"] if d["fundedQty"] == 0]
    print(f"\nDEFERRED ({len(deferred)} lines) - first 5")
    for l in deferred[:5]:
        print(f"  {l['skuId']:<24} wanted x{l['requestedQty']}  "
              f"Rs {l['evidence']['fullLineCost']:,.2f}")
        print(f"      risk: {l['risk']}")

    print(f"\nSCENARIO COUNTS {json.dumps(scenario['scenarioCounts'])}")
    print(f"\nwritten to {SCENARIO_PATH}")


if __name__ == "__main__":
    main()
