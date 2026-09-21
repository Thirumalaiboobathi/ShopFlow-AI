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
import time
import uuid
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import boto3  # noqa: E402

from boto3.dynamodb.conditions import Key  # noqa: E402

from engine.cost_records import (  # noqa: E402
    DEFAULT_SHOP_ID,
    build_cost_record,
    cost_pk,
    cost_sk,
    latest_confirmed_costs,
    to_plan_decisions,
)
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
    # The real shop key is SHOP#demo. The smoke test deliberately writes to a
    # SMOKE# partition instead, so a failed run can never leave a bogus cost
    # sitting in the shop's live state.
    smoke_cost_pk = f"SMOKE#{cost_pk(DEFAULT_SHOP_ID)}"

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

        # ---- 5. Stage 5.1: the durable confirmed purchase cost ----
        #
        # This is the path the planner now takes with no job id at all, so it
        # is the one that matters most. It also exercises a composite key
        # condition against the real table - the in-memory fake once parsed
        # that wrongly and silently matched nothing, which no unit test caught.
        print("5. persist a confirmed purchase cost and read it back")
        record = build_cost_record(
            data, WIRE, float(CONFIRMED_PRICE),
            source_job_id=job_id,
            confirmed_at=int(time.time()),
            effective_date="15-09-2026",
        )
        # Written the way the API writes it: Decimal, not float.
        record["confirmedCost"] = CONFIRMED_PRICE
        record["PK"] = smoke_cost_pk  # keep the smoke data out of shop state
        table.put_item(Item=record)
        check("DynamoDB accepted the cost record", True)
        check("cost record carries no TTL", "expiresAt" not in record)

        rows = table.query(
            KeyConditionExpression=Key("PK").eq(smoke_cost_pk)
            & Key("SK").begins_with("COST#")
        ).get("Items", [])
        check("composite query returned the cost row", len(rows) == 1,
              f"{len(rows)} row(s)")

        stored = rows[0] if rows else {}
        check("cost came back as Decimal",
              isinstance(stored.get("confirmedCost"), Decimal),
              f"type={type(stored.get('confirmedCost')).__name__}")
        check("currency recorded", stored.get("currency") == "INR",
              str(stored.get("currency")))
        check("source job id retained", stored.get("sourceJobId") == job_id)
        check("supplier recorded",
              stored.get("supplierId") == data.product(WIRE).supplierId,
              str(stored.get("supplierId")))
        check("effective date retained", stored.get("effectiveDate") == "15-09-2026")
        check("no selling price on the record", "sellingPrice" not in stored)

        # ---- 6. stored record -> engine, with no job id involved ----
        print("6. plan from the stored cost alone")
        latest = latest_confirmed_costs([{
            "skuId": stored.get("skuId"),
            "confirmedCost": float(stored.get("confirmedCost")),
            "currency": stored.get("currency"),
            "supplierId": stored.get("supplierId"),
            "effectiveDate": stored.get("effectiveDate"),
            "sourceJobId": stored.get("sourceJobId"),
            "confirmedAt": stored.get("confirmedAt"),
        }])
        durable = build_purchase_plan(data, BUDGET, to_plan_decisions(latest))
        print(f"  tier1={durable['commitmentCost']:,.2f} "
              f"tier2={durable['restockCost']:,.2f} "
              f"total={durable['totalSpend']:,.2f} "
              f"remaining={durable['remaining']:,.2f}")

        dline = next((l for l in durable["commitments"] if l["skuId"] == WIRE), None)
        check("planner used the stored confirmed cost",
              dline is not None and dline["unitCost"] == 6300.0,
              f"unitCost={dline['unitCost'] if dline else 'missing'}")
        check("evidence says CONFIRMED_SUPPLIER_PRICE",
              dline is not None
              and dline["costSource"] == "CONFIRMED_SUPPLIER_PRICE",
              str(dline["costSource"]) if dline else "missing")
        check("provenance names the source price list",
              dline is not None
              and dline["costProvenance"]["sourceJobId"] == job_id)
        check("selling price still untouched",
              dline is not None
              and dline["sellingPrice"] == data.product(WIRE).sellingPrice)
        check("same result as the job-id path",
              durable["commitmentCost"] == plan["commitmentCost"],
              f"Rs {durable['commitmentCost']:,.2f}")

        unconfirmed = [l for l in durable["commitments"] if l["skuId"] != WIRE]
        check("unconfirmed SKUs are labelled SEEDED_SUPPLIER_PRICE",
              all(l["costSource"] == "SEEDED_SUPPLIER_PRICE" for l in unconfirmed),
              f"{len(unconfirmed)} line(s)")

        # ---- 7. the confirmed-cost impact, from the stored record ----
        #
        # Reported by running the same allocator twice - once with the stored
        # confirmed cost applied, once without - and subtracting. Verified here
        # against the real persisted row rather than an in-memory fixture.
        print("7. confirmed-cost impact")
        check("baseline plan omits the impact block",
              "confirmedCostImpact" not in baseline)

        impact = durable.get("confirmedCostImpact")
        check("confirmed plan returns the impact block", impact is not None)
        if impact:
            print(f"  directCommitmentIncrease    Rs {impact['directCommitmentIncrease']:,.2f}")
            print(f"  restockingCapacityReduction Rs {impact['restockingCapacityReduction']:,.2f}")
            print(f"  restockCostWithout          Rs {impact['restockCostWithout']:,.2f}")
            print(f"  restockCostWith             Rs {impact['restockCostWith']:,.2f}")

            check("direct commitment increase is the expected Rs 800.00",
                  impact["directCommitmentIncrease"] == EXPECTED_EXTRA_TIER1,
                  f"Rs {impact['directCommitmentIncrease']:,.2f}")
            check("restocking capacity reduction is a DIFFERENT figure",
                  impact["restockingCapacityReduction"]
                  != impact["directCommitmentIncrease"],
                  f"Rs {impact['restockingCapacityReduction']:,.2f}")
            check("impact describes the plan it came with",
                  impact["restockCostWith"] == durable["restockCost"])
            check("impact baseline matches the unconfirmed plan",
                  impact["restockCostWithout"] == baseline["restockCost"],
                  f"Rs {baseline['restockCost']:,.2f}")
            check("the two figures reconcile",
                  round(impact["restockCostWithout"] - impact["restockCostWith"], 2)
                  == impact["restockingCapacityReduction"])
            check("impact did not change the plan's own totals",
                  durable["totalSpend"] <= BUDGET and durable["remaining"] >= 0,
                  f"total Rs {durable['totalSpend']:,.2f}")

    finally:
        # ---- 8. leave nothing behind ----
        print("8. clean up")
        table.delete_item(Key={"PK": pk, "SK": sk})
        table.delete_item(Key={"PK": smoke_cost_pk, "SK": cost_sk(WIRE)})
        gone = (table.get_item(Key={"PK": pk, "SK": sk}).get("Item") is None
                and table.get_item(
                    Key={"PK": smoke_cost_pk, "SK": cost_sk(WIRE)}
                ).get("Item") is None)
        check("smoke records removed", gone)

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
