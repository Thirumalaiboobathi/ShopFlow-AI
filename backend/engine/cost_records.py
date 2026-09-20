"""The shop's current confirmed supplier purchase cost, per SKU.

WHAT THIS IS
------------
Stage 5 could only reach a confirmed price through the price-list job that
produced it. That made the confirmation a property of a document rather than of
the shop, and job records carry a 24-hour TTL - so the shop's own knowledge of
what it pays would quietly expire. This module defines the durable replacement.

One item per SKU, overwritten on each confirmation:

    PK  SHOP#<shopId>
    SK  COST#<skuId>

Overwriting is the supersede rule. The shop has exactly one current purchase
cost for a SKU at any moment, and the newest confirmation is it. The price
list that produced it is kept on the record (`sourceJobId`), so the trail back
to the document survives even though the previous cost does not.

DELIBERATELY NOT STORED HERE
----------------------------
No TTL. Job records expire because they are workings; this is shop state and
must not vanish overnight.

No selling price. A confirmed supplier cost says what the shop pays, never
what it charges. Repricing the shelf is a commercial decision the owner makes
separately, and nothing in this file touches `Product.sellingPrice`.

No stock. Confirming a price changes no quantity anywhere.

WHO MAY WRITE ONE
-----------------
Only the owner's explicit confirmation, via POST /api/price-decisions. An
extracted price is a reading of a document, not an agreement to pay it, so
extraction never writes here. A REJECTED decision writes nothing at all -
`build_cost_record` raises if asked.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional

from .models import Dataset, money

# The shop this demo represents. A single-tenant constant today; it is the
# partition key so that multi-tenancy later needs no data migration.
DEFAULT_SHOP_ID = "demo"

# Every price in this shop is rupees. Recorded explicitly rather than assumed,
# because a cost without a currency is not a cost.
CURRENCY = "INR"

CONFIRMED = "CONFIRMED"

# Where a purchase cost in a plan came from.
CONFIRMED_SUPPLIER_PRICE = "CONFIRMED_SUPPLIER_PRICE"
SEEDED_SUPPLIER_PRICE = "SEEDED_SUPPLIER_PRICE"


class InvalidCostRecordError(ValueError):
    """The confirmation cannot become a durable cost record."""


def cost_pk(shop_id: str = DEFAULT_SHOP_ID) -> str:
    return f"SHOP#{shop_id}"


def cost_sk(sku_id: str) -> str:
    return f"COST#{sku_id}"


def build_cost_record(
    data: Dataset,
    sku_id: str,
    cost: float,
    *,
    source_job_id: str,
    confirmed_at: int,
    effective_date: Optional[str] = None,
    shop_id: str = DEFAULT_SHOP_ID,
    decision: str = CONFIRMED,
) -> dict:
    """The shop's new current purchase cost for one SKU.

    Raises unless the decision is a confirmation: a rejection means the owner
    does not accept the price, and must leave no trace in the shop's costs.
    """
    if str(decision).upper() != CONFIRMED:
        raise InvalidCostRecordError(
            "only a CONFIRMED decision may set a purchase cost")
    if sku_id not in data.products:
        raise InvalidCostRecordError(f"unknown SKU: {sku_id}")

    try:
        amount = float(cost)
    except (TypeError, ValueError):
        raise InvalidCostRecordError("cost must be a number") from None
    if amount != amount or amount in (float("inf"), float("-inf")):
        raise InvalidCostRecordError("cost must be a finite number")
    if amount <= 0:
        raise InvalidCostRecordError("cost must be greater than zero")

    product = data.product(sku_id)
    return {
        "PK": cost_pk(shop_id),
        "SK": cost_sk(sku_id),
        "recordType": "CONFIRMED_SUPPLIER_COST",
        "shopId": shop_id,
        "skuId": sku_id,
        "productName": product.name,
        # The supplier the shop actually buys this SKU from. Taken from the
        # catalogue, not from the document, so a price list cannot silently
        # reassign a SKU to a different supplier.
        "supplierId": product.supplierId,
        "confirmedCost": money(amount),
        "currency": CURRENCY,
        "effectiveDate": effective_date or "",
        "sourceJobId": source_job_id,
        "confirmedAt": int(confirmed_at),
    }


def latest_confirmed_costs(records: Iterable[Dict]) -> Dict[str, dict]:
    """Index confirmed cost records by SKU, newest confirmation winning.

    The store holds one item per SKU so a conflict should not arise, but a
    caller may merge several sources. Resolving on `confirmedAt` here means the
    supersede rule lives in one place rather than at each call site.
    """
    latest: Dict[str, dict] = {}
    for row in records or []:
        if not isinstance(row, dict):
            continue
        sku_id = row.get("skuId")
        if not sku_id:
            continue
        try:
            cost = float(row.get("confirmedCost"))
        except (TypeError, ValueError):
            continue
        if cost <= 0:
            continue

        try:
            confirmed_at = int(row.get("confirmedAt") or 0)
        except (TypeError, ValueError):
            confirmed_at = 0

        entry = {
            "skuId": str(sku_id),
            "cost": money(cost),
            "currency": str(row.get("currency") or CURRENCY),
            "supplierId": row.get("supplierId"),
            "effectiveDate": row.get("effectiveDate") or "",
            "sourceJobId": row.get("sourceJobId") or "",
            "confirmedAt": confirmed_at,
        }
        existing = latest.get(entry["skuId"])
        if existing is None or confirmed_at >= existing["confirmedAt"]:
            latest[entry["skuId"]] = entry
    return latest


def to_plan_decisions(costs: Dict[str, dict]) -> List[dict]:
    """Confirmed cost records in the shape the planner already accepts.

    Keeps `build_purchase_plan`'s existing decision input as the single way a
    cost override enters the planner, so the durable store and a price-list job
    travel the same path.
    """
    return [
        {
            "skuId": sku_id,
            "decision": CONFIRMED,
            "currentPrice": entry["cost"],
            "sourceJobId": entry["sourceJobId"],
            "confirmedAt": entry["confirmedAt"],
        }
        for sku_id, entry in sorted(costs.items())
    ]
