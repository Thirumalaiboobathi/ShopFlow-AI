"""Supplier reliability: a score only where verified history supports one.

WHY THIS IS STRICT
------------------
A reliability score is a claim about how a supplier has behaved. ShopFlow
does not record purchase orders or deliveries today, so for every supplier in
the shop it has no behaviour to score - and it says so. A plausible 72/100
built from a supplier's name, a price, a minimum order quantity, a single
quote, synthetic catalogue rows or a model's opinion would be worse than no
number at all, because an owner would act on it.

So the score is computed from one kind of evidence only: completed purchase
orders, each marked `verified`, carrying what was ordered, what arrived, when
it was promised, when it came and what was invoiced against what was quoted.
Anything less than the thresholds below returns INSUFFICIENT_DATA and a null
score. No input here comes from a request body or a model.

THE SCORE
---------
Four rates, each a plain count over the verified orders:

    completion      orders not cancelled            / orders
    quantity        delivered orders in full         / delivered orders
    on time         dated deliveries on/before date  / dated deliveries
    price stability invoiced price = quoted price    / priced deliveries

    score = round(100 x sum(weight x rate))

Every weight and threshold is in RELIABILITY_CONFIG. The weights are equal:
no evidence in this project says a late delivery matters more or less than a
short one to this shop, and equal weights are the choice that assumes least.
An owner who knows otherwise changes one line, and the tests say what moves.

`confidence` describes how much history the score rests on - a count band,
not a statistical interval.
"""

from __future__ import annotations

from datetime import date
from typing import Dict, Iterable, List, Optional

AVAILABLE = "AVAILABLE"
INSUFFICIENT_DATA = "INSUFFICIENT_DATA"
INVALID_HISTORY = "INVALID_HISTORY"

RELIABILITY_CONFIG = {
    # Fewer verified orders than this and no score is given: below ten, one
    # order moves a rate by more than ten points.
    "minVerifiedOrders": 10,
    # Each rate also needs this many orders of its own kind (delivered, dated,
    # priced). A rate over two deliveries is not a rate.
    "minComponentSamples": 5,
    "weights": {
        "completion": 0.25,
        "quantity": 0.25,
        "onTime": 0.25,
        "priceStability": 0.25,
    },
    # Price stability tolerance: invoiced within this many rupees of quoted.
    "priceToleranceRupees": 0.01,
    # Count bands for `confidence` - how much history, not how sure.
    "confidenceBands": ((50, "SUBSTANTIAL"), (20, "MODERATE"), (10, "LIMITED")),
}

INSUFFICIENT_REASON = "Not enough verified supplier performance history"
NOT_RECORDED_REASON = ("ShopFlow does not record purchase orders or deliveries "
                       "yet, so there is no verified supplier history to score.")
NOTE = ("ShopFlow does not invent supplier reliability scores. A score is "
        "given only from verified purchase-order history.")

_REQUIRED = ("orderId", "supplierId", "orderedQty", "verified")


class InvalidHistoryError(ValueError):
    pass


def _date(value, field: str, order_id: str) -> Optional[date]:
    if value in (None, ""):
        return None
    try:
        return date.fromisoformat(str(value))
    except ValueError:
        raise InvalidHistoryError(f"{order_id}: {field} is not a date") from None


def _count(value, field: str, order_id: str, *, allow_none=False) -> Optional[int]:
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise InvalidHistoryError(f"{order_id}: {field} must be a whole number")
    return value


def _price(value, field: str, order_id: str) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not value > 0 or value != value or value == float("inf"):
        raise InvalidHistoryError(f"{order_id}: {field} must be a positive number")
    return float(value)


def _record(raw, supplier_id: str) -> Optional[dict]:
    """One validated order, None when it is not verified or not this supplier."""
    if not isinstance(raw, dict) or any(k not in raw for k in _REQUIRED):
        raise InvalidHistoryError("each history record needs "
                                  + ", ".join(_REQUIRED))
    order_id = str(raw["orderId"])[:40]
    if raw["supplierId"] != supplier_id or raw["verified"] is not True:
        return None
    ordered = _count(raw["orderedQty"], "orderedQty", order_id)
    if ordered == 0:
        raise InvalidHistoryError(f"{order_id}: orderedQty must be at least 1")
    cancelled = raw.get("cancelled", False)
    if not isinstance(cancelled, bool):
        raise InvalidHistoryError(f"{order_id}: cancelled must be true or false")
    received = _count(raw.get("receivedQty"), "receivedQty", order_id,
                      allow_none=cancelled)
    return {
        "cancelled": cancelled,
        "ordered": ordered,
        "received": received,
        "agreed": _date(raw.get("agreedDeliveryDate"), "agreedDeliveryDate", order_id),
        "actual": _date(raw.get("actualDeliveryDate"), "actualDeliveryDate", order_id),
        "quoted": _price(raw.get("quotedUnitPrice"), "quotedUnitPrice", order_id),
        "invoiced": _price(raw.get("invoicedUnitPrice"), "invoicedUnitPrice", order_id),
    }


def _unique_orders(raw: List[dict], supplier_id: str) -> None:
    """Refuse a history that lists one of this supplier's orders twice.

    A verified order is one piece of evidence. Ten copies of it are still one
    order, and counting them as ten would let a single delivery clear the
    evidence threshold. Not de-duplicated either: a repeated id means the
    history itself cannot be trusted as a record, so none of it is scored.
    Uniqueness is per supplier, the scope of the history being scored.
    """
    seen = set()
    for record in raw:
        if record["supplierId"] != supplier_id:
            continue
        order_id = str(record["orderId"])
        if order_id in seen:
            raise InvalidHistoryError(f"{order_id[:40]}: orderId appears more "
                                      "than once; each order is evidence once")
        seen.add(order_id)


def _insufficient(supplier_id: str, reason: str, evidence: dict) -> dict:
    return {"status": INSUFFICIENT_DATA, "supplierId": supplier_id,
            "score": None, "confidence": None, "reason": reason,
            "evidence": evidence, "note": NOTE, "stateChanged": False}


def score_supplier(supplier_id: str, history: Iterable[dict],
                   config: Optional[Dict] = None) -> dict:
    """A deterministic reliability score from verified history, or none.

    `history` is purchase-order records from the shop's own store. Unverified
    records and other suppliers' records are counted and ignored. A malformed
    record refuses the whole history rather than scoring around it.
    """
    cfg = config or RELIABILITY_CONFIG
    supplier_id = str(supplier_id or "")
    try:
        raw = list(history or [])
        records = [_record(r, supplier_id) for r in raw]
        _unique_orders(raw, supplier_id)
    except (InvalidHistoryError, TypeError) as exc:
        return {"status": INVALID_HISTORY, "supplierId": supplier_id,
                "score": None, "confidence": None, "reason": str(exc),
                "note": NOTE, "stateChanged": False}
    orders = [r for r in records if r is not None]

    delivered = [r for r in orders if not r["cancelled"]]
    dated = [r for r in delivered if r["agreed"] and r["actual"]]
    priced = [r for r in delivered if r["quoted"] and r["invoiced"]]
    counts = {
        "recordsSupplied": len(raw),
        "verifiedOrders": len(orders),
        "unverifiedOrIgnored": len(raw) - len(orders),
        "cancelled": sum(r["cancelled"] for r in orders),
        "delivered": len(delivered),
        "deliveredInFull": sum(r["received"] >= r["ordered"] for r in delivered),
        "quantityShortfalls": sum(r["received"] < r["ordered"] for r in delivered),
        "datedDeliveries": len(dated),
        "onOrBeforeAgreedDate": sum(r["actual"] <= r["agreed"] for r in dated),
        "lateDeliveries": sum(r["actual"] > r["agreed"] for r in dated),
        "pricedDeliveries": len(priced),
        "invoicedAtQuotedPrice": sum(
            abs(r["invoiced"] - r["quoted"]) <= cfg["priceToleranceRupees"]
            for r in priced),
    }
    counts["priceChangesAfterQuote"] = counts["pricedDeliveries"] - \
        counts["invoicedAtQuotedPrice"]

    if len(orders) < cfg["minVerifiedOrders"]:
        return _insufficient(supplier_id, INSUFFICIENT_REASON
                             + f": {len(orders)} verified order(s), at least "
                             f"{cfg['minVerifiedOrders']} needed.", counts)
    minimum = cfg["minComponentSamples"]
    short = [name for name, n in (("delivered", len(delivered)),
                                  ("dated deliveries", len(dated)),
                                  ("priced deliveries", len(priced))) if n < minimum]
    if short:
        return _insufficient(supplier_id, INSUFFICIENT_REASON + ": fewer than "
                             f"{minimum} {', '.join(short)}.", counts)

    rates = {
        "completion": counts["delivered"] / counts["verifiedOrders"],
        "quantity": counts["deliveredInFull"] / counts["delivered"],
        "onTime": counts["onOrBeforeAgreedDate"] / counts["datedDeliveries"],
        "priceStability": counts["invoicedAtQuotedPrice"] / counts["pricedDeliveries"],
    }
    weights = cfg["weights"]
    score = round(100 * sum(weights[k] * rates[k] for k in weights))
    level = next(label for floor, label in cfg["confidenceBands"]
                 if len(orders) >= floor)
    return {
        "status": AVAILABLE,
        "supplierId": supplier_id,
        "score": score,
        "confidence": {"level": level,
                       "basis": f"{len(orders)} verified orders",
                       "meaning": "how much history the score rests on"},
        "components": {k: {"rate": round(rates[k], 4), "weight": weights[k],
                           "points": round(100 * weights[k] * rates[k], 2)}
                       for k in weights},
        "evidence": counts,
        "summary": _summary(score, counts),
        "note": NOTE,
        "stateChanged": False,
    }


def _summary(score: int, c: dict) -> List[str]:
    return [f"Reliability score: {score}/100",
            f"Based on {c['verifiedOrders']} verified historical orders.",
            f"{c['deliveredInFull']} delivered in full, {c['quantityShortfalls']} "
            "with a quantity shortfall.",
            f"{c['onOrBeforeAgreedDate']} on or before the agreed date, "
            f"{c['lateDeliveries']} late.",
            f"{c['cancelled']} cancelled.",
            f"{c['priceChangesAfterQuote']} invoiced at a price different from "
            "the quote."]


def shop_history(supplier_id: str) -> List[dict]:
    """The shop's verified purchase-order history for one supplier.

    Empty, and deliberately so: no workflow in ShopFlow records a purchase
    order or a delivery, and nothing is inferred in its place. This is the one
    place such a record source would be read.
    """
    return []
