"""Smoke test: the planner's data flow against REAL DynamoDB.

WHY THIS EXISTS
---------------
Stage 4 shipped a bug the unit suite could not have caught. The tests replace
DynamoDB with an in-memory `FakeTable` that accepts anything a Python dict
accepts. The real table does not: it rejects float, and a decision record
carries three of them. Every test passed and the deployed endpoint returned
500.

The Stage 5 planner depends on that same path - it reads confirmed supplier
prices back out of DynamoDB and feeds them to the engine - so the same class of
bug would land the same way. This script closes that gap by exercising the real
store:

    write a decision record (Decimal)  ->  read it back  ->  run the engine
                                       ->  assert the expected allocation

It is NOT part of the pytest suite, on purpose. The unit tests must stay fast
and runnable with no AWS account. This is run explicitly, before a deploy:

    python scripts/smoke_test_planner.py

Requires AWS credentials for the account holding ShopFlowStack, and writes only
to keys prefixed SMOKE# in the shopflow-demo table, which it deletes on the way
out.
"""

from __future__ import annotations

import sys
import uuid
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import boto3  # noqa: E402

from engine.loader import cached_dataset  # noqa: E402
from engine.purchasing import build_purchase_plan  # noqa: E402

TABLE_NAME = "shopflow-demo"
WIRE = "W-FIN-1.5-RED-90M"
BUDGET = 25000.0

# What the owner confirmed in the Stage 4 flow: the dealer rate moved 5,900 ->
# 6,300, a 6.78% rise. Two coils are on committed order, so confirming it must
# cost the purchase plan exactly 2 x 400 = 800 more in Tier 1.
PREVIOUS_PRICE = Decimal("5900.0")
CONFIRMED_PRICE = Decimal("6300.0")
EXPECTED_EXTRA_TIER1 = 800.0

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {label}" + (f"  {detail}" if detail else ""))
    if not condition:
        failures.append(label)


def main() -> int:
    table = boto3.resource("dynamodb").Table(TABLE_NAME)
    job_id = uuid.uuid4().hex
    pk = f"SMOKE#DECISION#{job_id}"
    sk = f"SKU#{WIRE}"

    print(f"ShopFlow planner smoke test")
    print(f"  table   {TABLE_NAME}")
    print(f"  sku     {WIRE}")
    print(f"  budget  Rs {BUDGET:,.2f}\n")

    data = cached_dataset()
    baseline = build_purchase_plan(data, BUDGET)
    print("baseline plan (no confirmed price change)")
    print(f"  tier1={baseline['commitmentCost']:,.2f} "
          f"tier2={baseline['restockCost']:,.2f} "
          f"total={baseline['totalSpend']:,.2f} "
          f"remaining={baseline['remaining']:,.2f}\n")

    try:
        # ---- 1. write, the way the API really writes it ----
        print("1. write a decision record to real DynamoDB")
        table.put_item(Item={
            "PK": pk,
            "SK": sk,
            "jobId": job_id,
            "skuId": WIRE,
            "decision": "CONFIRMED",
            "previousPrice": PREVIOUS_PRICE,
            "currentPrice": CONFIRMED_PRICE,
            "catalogPriceChanged": False,
        })
        check("DynamoDB accepted the record", True)

        # ---- 2. read it back ----
        print("2. read it back")
        item = table.get_item(Key={"PK": pk, "SK": sk}).get("Item")
        check("record round-tripped", item is not None)
        if item is None:
            return 1
        check("price came back as Decimal, not float",
              isinstance(item["currentPrice"], Decimal),
              f"type={type(item['currentPrice']).__name__}")
        check("stored value is exact", item["currentPrice"] == CONFIRMED_PRICE,
              f"{item['currentPrice']}")

        # ---- 3. feed the stored row to the engine ----
        print("3. run the deterministic engine on the stored row")
        decisions = [{
            "skuId": item["skuId"],
            "decision": item["decision"],
            "currentPrice": float(item["currentPrice"]),
            "previousPrice": float(item["previousPrice"]),
        }]
        plan = build_purchase_plan(data, BUDGET, decisions)
        print(f"  tier1={plan['commitmentCost']:,.2f} "
              f"tier2={plan['restockCost']:,.2f} "
              f"total={plan['totalSpend']:,.2f} "
              f"remaining={plan['remaining']:,.2f}")

        # ---- 4. the expected result ----
        print("4. assert the expected allocation")
        wire = next((l for l in plan["commitments"] if l["skuId"] == WIRE), None)
        check("the wire is priced at the confirmed cost",
              wire is not None and wire["unitCost"] == 6300.0,
              f"unitCost={wire['unitCost'] if wire else 'missing'}")
        check("selling price was NOT rewritten by the confirmed cost",
              wire is not None
              and wire["sellingPrice"] == data.product(WIRE).sellingPrice,
              f"sellingPrice={wire['sellingPrice'] if wire else 'missing'}")

        extra = round(plan["commitmentCost"] - baseline["commitmentCost"], 2)
        check(f"confirming the rise costs exactly Rs {EXPECTED_EXTRA_TIER1:,.2f} more",
              extra == EXPECTED_EXTRA_TIER1, f"actual Rs {extra:,.2f}")
        check("that money came out of restocking",
              plan["restockCost"] < baseline["restockCost"],
              f"{baseline['restockCost']:,.2f} -> {plan['restockCost']:,.2f}")

        check("spend never exceeds the budget",
              plan["totalSpend"] <= BUDGET, f"Rs {plan['totalSpend']:,.2f}")
        check("remaining is never negative",
              plan["remaining"] >= 0, f"Rs {plan['remaining']:,.2f}")
        check("every customer commitment is still funded",
              plan["allCommitmentsFunded"] is True)
        check("the budget still binds",
              plan["budgetIsBinding"] is True,
              f"{plan['counts']['restockDeferred']} deferred")

    finally:
        # ---- 5. leave nothing behind ----
        print("5. clean up")
        table.delete_item(Key={"PK": pk, "SK": sk})
        gone = table.get_item(Key={"PK": pk, "SK": sk}).get("Item") is None
        check("smoke record removed", gone)

    print()
    if failures:
        print(f"SMOKE TEST FAILED - {len(failures)} check(s):")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
