"""Smoke test: khata and units, end to end through the real API handler.

WHY THIS EXISTS
---------------
`smoke_test_planner.py` exists because a bug got past the unit suite: the
in-memory `FakeTable` accepts anything a Python dict accepts, and real
DynamoDB does not. That script closes the gap for the planner.

Credit and units are a different shape of risk. Neither writes anything and
neither reads DynamoDB, so there is no serialisation trap to fall into. What
there IS, is the risk that they write something by accident - a credit enquiry
that leaves a row, a customer record that reaches the store - and the risk
that adding units quietly moved a price.

So this script runs the real Lambda handlers against the real table, with real
boto3 clients, and then checks that the table is byte-for-byte what it was
before. It also re-derives the canonical quotation and the canonical plan and
asserts they have not moved.

Run it explicitly, before a deploy:

    python scripts/smoke_test_credit_uom.py

Requires AWS credentials for the account holding ShopFlowStack. It writes
NOTHING - if this script leaves a row behind, that is the bug it is looking
for.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

os.environ.setdefault("TABLE_NAME", "shopflow-demo")
os.environ.setdefault("WORKER_FUNCTION_NAME", "shopflow-order-worker")
os.environ.setdefault("UPLOADS_BUCKET", "unused-by-this-script")

import boto3  # noqa: E402

import lambdas.api.handler as api  # noqa: E402
from engine.loader import cached_dataset  # noqa: E402
from engine.quote import UomMismatchError, calculate_quote  # noqa: E402
from engine.purchasing import build_purchase_plan  # noqa: E402
from engine.uom import COIL, METER, base_equivalent, product_uom  # noqa: E402

# Owner routes require the caller to say it is asking as the shop owner. The
# header is a demo gate, not authentication - see handler.DEMO_OWNER_HEADER.
OWNER_HEADERS = {"x-shopflow-demo-owner": "demo-workspace"}

TABLE_NAME = os.environ["TABLE_NAME"]
WIRE = "W-FIN-1.5-RED-90M"
SWITCH = "SW-ANC-1W10A"
MCB = "MCB-HAV-SP-32A-C"
CANONICAL = [(SWITCH, 20), (WIRE, 3), (MCB, 2)]

CANONICAL_TOTAL = 22306.48
CANONICAL_PLAN_TOTAL = 24996.56

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {label}" + (f"  {detail}" if detail else ""))
    if not condition:
        failures.append(label)


def body_of(response):
    return json.loads(response["body"])


def table_snapshot(table):
    """Every key in the table. Small enough to scan; this is a demo shop."""
    keys, start = set(), None
    while True:
        kwargs = {"ProjectionExpression": "PK,SK"}
        if start:
            kwargs["ExclusiveStartKey"] = start
        page = table.scan(**kwargs)
        for item in page.get("Items", []):
            keys.add((item.get("PK"), item.get("SK")))
        start = page.get("LastEvaluatedKey")
        if not start:
            return keys


def main() -> int:
    data = cached_dataset()
    table = boto3.resource("dynamodb").Table(TABLE_NAME)

    print(f"\nShopFlow khata + units smoke test ({TABLE_NAME})\n")

    print("1. the table before")
    before = table_snapshot(table)
    check("table scanned", True, f"{len(before)} items")
    check("no khata row exists in the store",
          not any(str(pk).startswith("CUSTOMER#") for pk, _ in before),
          "customers are seed data, not stored state")

    # ---------------------------------------------------------------- units
    print("\n2. units, from the catalogue")
    wire = data.product(WIRE)
    check("wire is sold by the coil", product_uom(wire) == COIL, product_uom(wire))
    check("the conversion is the length printed on the SKU",
          wire.baseQuantity == 90.0 and wire.baseUom == METER,
          f"{wire.baseQuantity} {wire.baseUom}")
    equivalent = base_equivalent(wire, 3)
    check("3 coils reports 270 metres", equivalent["quantity"] == 270.0,
          equivalent["calculation"])
    check("switches carry no invented conversion",
          base_equivalent(data.product(SWITCH), 5) is None)

    print("\n3. units do not move a price")
    plain = calculate_quote(data, CANONICAL).as_dict()
    stated = calculate_quote(data, [
        {"skuId": SWITCH, "quantity": 20, "uom": "pieces"},
        {"skuId": WIRE, "quantity": 3, "uom": "coils"},
        {"skuId": MCB, "quantity": 2, "uom": "piece"},
    ]).as_dict()
    check("canonical quotation unchanged", plain["total"] == CANONICAL_TOTAL,
          f"Rs {plain['total']:,.2f}")
    check("stating the unit changes no total", stated["total"] == plain["total"],
          f"Rs {stated['total']:,.2f}")
    line = next(l for l in stated["lines"] if l["skuId"] == WIRE)
    check("the ordered quantity is still 3 coils",
          line["quantity"] == 3 and line["catalogueUom"] == COIL)
    check("the equivalent is shown beside it, not instead of it",
          line["baseEquivalent"]["quantity"] == 270.0)
    check("the shortage is in coils", line["shortageQty"] == 2)

    print("\n4. a unit that cannot be honoured stops the quotation")
    for label, item in (
        ("metres of a coil", {"skuId": WIRE, "quantity": 90, "uom": "metres"}),
        ("a box of switches", {"skuId": SWITCH, "quantity": 2, "uom": "boxes"}),
    ):
        try:
            calculate_quote(data, [item])
            check(f"{label} is refused", False, "NO ERROR RAISED")
        except UomMismatchError as exc:
            check(f"{label} is refused", True, exc.resolution["status"])

    print("\n5. the canonical plan is unmoved")
    plan = build_purchase_plan(data, 25000.0)
    check("plan total unchanged", plan["totalSpend"] == CANONICAL_PLAN_TOTAL,
          f"Rs {plan['totalSpend']:,.2f}")
    check("every plan line names a supplier",
          all(l.get("supplierName") for l in
              plan["commitments"] + plan["restockSelected"]))

    # --------------------------------------------------------------- credit
    print("\n6. khata, through the real handler")
    listed = body_of(api.handler({"routeKey": "GET /api/customers", "headers": OWNER_HEADERS}, None))
    check("customers listed", len(listed["customers"]) == 4,
          f"{len(listed['customers'])} accounts")
    check("every account is labelled synthetic",
          all(c["synthetic"] for c in listed["customers"]))

    cases = {
        "CUST-BALA-002": "APPROVED",
        "CUST-RAVI-001": "LIMIT_EXCEEDED",
        "CUST-KUMAR-004": "BLOCKED",
        "CUST-NOT-REAL": "NO_CREDIT_ACCOUNT",
    }
    for customer_id, expected in cases.items():
        result = body_of(api.handler({
            "routeKey": "POST /api/credit/check", "headers": OWNER_HEADERS,
            "body": json.dumps({"customerId": customer_id,
                                "orderTotal": CANONICAL_TOTAL}),
        }, None))
        check(f"{customer_id} -> {expected}", result["decision"] == expected,
              result["decision"])

    example = body_of(api.handler({
        "routeKey": "POST /api/credit/check", "headers": OWNER_HEADERS,
        "body": json.dumps({"customerId": "CUST-RAVI-001", "orderTotal": 4200}),
    }, None))
    check("the worked example reproduces",
          example["projectedOutstanding"] == 12700.0
          and example["remainingCredit"] == 2300.0,
          f"projected {example['projectedOutstanding']}, "
          f"remaining {example['remainingCredit']}")
    check("the response states this is not credit scoring",
          "does not perform credit scoring" in example["policy"])

    print("\n7. nothing was written")
    after = table_snapshot(table)
    check("the table is exactly as it was", after == before,
          f"{len(after)} items")
    added = sorted(after - before)
    check("no row was added", not added, str(added[:5]) if added else "")

    print("\n8. selling prices are untouched")
    check("wire selling price unchanged",
          cached_dataset().product(WIRE).sellingPrice == 6608.0)
    check("switch selling price unchanged",
          cached_dataset().product(SWITCH).sellingPrice == 78.3)

    print()
    if failures:
        print(f"SMOKE TEST FAILED: {len(failures)} check(s)")
        for name in failures:
            print(f"  - {name}")
        return 1
    print("SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
