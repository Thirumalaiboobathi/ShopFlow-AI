"""Clear the shop's confirmed supplier purchase costs, for a repeatable demo.

WHY THIS EXISTS
---------------
Stage 5.1 made a confirmed price durable, which is the point: a cost the owner
agreed to last week is what they pay today. The side effect is that the demo
became one-shot. Once the ₹6,300 wire price is confirmed, the "before" state -
planning at the seeded ₹5,900 - is gone, and the price list no longer shows a
+6.78% change against it either.

This puts the shop back to seeded state so the story can be told again.

It is a deliberate, explicit operator action and lives nowhere near the API.
There is no route, no button and no UI for it: erasing what the owner agreed to
pay is not something a web request should be able to do.

    python scripts/reset_confirmed_costs.py            # show what would go
    python scripts/reset_confirmed_costs.py --confirm  # actually delete

Only `SHOP#<shopId>` / `COST#...` items are touched. Jobs, decisions, orders
and inventory are left alone.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import boto3  # noqa: E402
from boto3.dynamodb.conditions import Key  # noqa: E402

from engine.cost_records import DEFAULT_SHOP_ID, cost_pk  # noqa: E402

TABLE_NAME = "shopflow-demo"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm", action="store_true",
                        help="actually delete; without it, only list")
    parser.add_argument("--shop", default=DEFAULT_SHOP_ID)
    parser.add_argument("--table", default=TABLE_NAME)
    args = parser.parse_args()

    table = boto3.resource("dynamodb").Table(args.table)
    pk = cost_pk(args.shop)

    rows = table.query(
        KeyConditionExpression=Key("PK").eq(pk) & Key("SK").begins_with("COST#")
    ).get("Items", [])

    if not rows:
        print(f"No confirmed supplier costs for {pk}. Already at seeded state.")
        return 0

    print(f"{len(rows)} confirmed supplier cost(s) under {pk}:\n")
    for row in rows:
        print(f"  {row.get('skuId'):<24} Rs {row.get('confirmedCost')} "
              f"{row.get('currency')}  from job {row.get('sourceJobId')}")

    if not args.confirm:
        print("\nDry run. Re-run with --confirm to delete these.")
        return 0

    for row in rows:
        table.delete_item(Key={"PK": row["PK"], "SK": row["SK"]})

    left = table.query(
        KeyConditionExpression=Key("PK").eq(pk) & Key("SK").begins_with("COST#")
    ).get("Items", [])
    if left:
        print(f"\nFAILED: {len(left)} record(s) remain.")
        return 1

    print(f"\nDeleted {len(rows)} record(s). The shop is back to seeded costs.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
