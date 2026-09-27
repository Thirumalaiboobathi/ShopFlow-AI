"""ShopFlow business events, published to EventBridge.

WHAT AN EVENT IS HERE
---------------------
A statement that something happened in the shop, carrying numbers that a
deterministic engine already calculated. Six of them exist and the list is
closed:

    SupplierPriceChanged     a confirmed document line moved a supplier cost
    StockoutDetected         a quoted line cannot be filled from stock
    LowMarginDetected        a SKU's margin fell past the configured threshold
    PurchasePlanGenerated    the planner produced a plan within a budget
    OrderNeedsClarification  an order ended as a question rather than a quote
    OrderProcessingFailed    an order failed inside the agent
    DailyShopBriefGenerated  the scheduled daily brief was built (counts only)

A SupplierPriceChanged event from a price list carries the price shock
alert's figures (engine.price_alerts): old and new cost, the deltas, the
margin before and after, the walk-away price and a severity.

An event is not a log line and not a trace. It is emitted when a business fact
changes, never once per function call, and it carries the minimum that a
subscriber needs to act.

WHERE THE NUMBERS COME FROM
---------------------------
From the engine output that is already in hand at the call site, copied
verbatim. Nothing in this module calculates, rounds, converts or derives a
business figure, and no model output may reach it: `build_*` takes the numbers
it is given and refuses anything that is not already a number. Bedrock may
later explain an event; it may never be the source of one.

FAILING SAFELY
--------------
Publishing is observability, and observability may not be able to change a
business result. `publish` never raises. If EventBridge is unreachable, is
throttled, or is not configured at all, the event is dropped, a metric is
emitted and the caller carries on with a transaction that is still correct.
The one thing that must never happen is an order failing because a bus did.
"""

from __future__ import annotations

import json
import os
import time
from typing import Dict, List, Optional

from . import metrics

# The closed list.
SUPPLIER_PRICE_CHANGED = "SupplierPriceChanged"
STOCKOUT_DETECTED = "StockoutDetected"
LOW_MARGIN_DETECTED = "LowMarginDetected"
PURCHASE_PLAN_GENERATED = "PurchasePlanGenerated"
ORDER_NEEDS_CLARIFICATION = "OrderNeedsClarification"
ORDER_PROCESSING_FAILED = "OrderProcessingFailed"
DAILY_SHOP_BRIEF_GENERATED = "DailyShopBriefGenerated"

EVENT_TYPES = (
    SUPPLIER_PRICE_CHANGED,
    STOCKOUT_DETECTED,
    LOW_MARGIN_DETECTED,
    PURCHASE_PLAN_GENERATED,
    ORDER_NEEDS_CLARIFICATION,
    ORDER_PROCESSING_FAILED,
    DAILY_SHOP_BRIEF_GENERATED,
)

# EventBridge's own field, used by rules to route. One source for this app.
SOURCE = "shopflow.business"

DEFAULT_SHOP_ID = "SHOP#demo"

# A detail is a handful of scalars. This bounds what can ever be put on a bus
# that other systems subscribe to - a whole quotation, a match list or a raw
# model trace cannot fit through it.
MAX_DETAIL_KEYS = 16
MAX_STRING_LENGTH = 200

# Keys that may never appear in an event, whatever a caller passes.
#
# An event is the widest surface in this application: EventBridge fans it out
# to SNS, and an SNS subscriber is an email in somebody's inbox, outside every
# boundary this project enforces. Supplier cost belongs in an owner alert and
# is allowed by name where the event is about a supplier price; a customer's
# phone number, a credit limit and any model text never are.
FORBIDDEN_KEYS = frozenset({
    "phone", "customerName", "customerId", "creditLimit", "outstandingAmount",
    "thinking", "reasoning", "prompt", "systemPrompt", "trace", "modelText",
    "summary", "question", "orderText", "transcript", "apiKey", "token",
    "credentials",
})


class _NullClient:
    """Stands in when there is no bus configured, so callers need no branch."""

    def put_events(self, **_kwargs):
        raise RuntimeError("no EventBridge bus is configured")


_client = None


def _events_client():
    global _client
    if _client is None:
        import boto3  # imported lazily so the engine stays importable

        _client = boto3.client("events")
    return _client


def bus_name() -> str:
    """The configured bus, or empty when events are switched off."""
    return (os.environ.get("EVENT_BUS_NAME") or "").strip()


def _scalar(value):
    """Events carry scalars. Anything else is a structure and is refused."""
    if isinstance(value, bool) or value is None:
        return value
    if isinstance(value, (int, float)):
        return value
    if isinstance(value, str):
        return value[:MAX_STRING_LENGTH]
    return None


def build_detail(event_type: str, shop_id: str = DEFAULT_SHOP_ID,
                 **fields) -> Dict:
    """One event body: the shop, the time, and the caller's own numbers.

    Raises on an unknown event type, because an event nobody routes is a
    silent one. Everything else is filtered rather than rejected: a caller
    passing a structure gets it dropped, not an exception thrown into the
    middle of a business transaction.
    """
    if event_type not in EVENT_TYPES:
        raise ValueError(f"unknown event type: {event_type!r}")

    detail = {
        "eventType": event_type,
        "shopId": shop_id or DEFAULT_SHOP_ID,
        "timestamp": int(time.time()),
    }
    for key, value in fields.items():
        if key in detail or key in FORBIDDEN_KEYS:
            continue
        scalar = _scalar(value)
        if scalar is None and value is not None:
            continue  # a structure, not a fact about the event
        detail[key] = scalar
        if len(detail) >= MAX_DETAIL_KEYS:
            break
    return detail


def publish(event_type: str, shop_id: str = DEFAULT_SHOP_ID, *,
            client=None, job_id: str = "", **fields) -> Optional[Dict]:
    """Publish one business event. Returns the detail sent, or None.

    Never raises. Returning None means the event did not go out - because no
    bus is configured, because the call failed, or because the event type was
    not one of the six - and in every one of those cases the caller's own
    work is unaffected and already correct.
    """
    try:
        detail = build_detail(event_type, shop_id, **fields)
    except ValueError:
        metrics.emit(metrics.EVENT_PUBLISH_FAILURES, reason="unknown_event_type")
        return None

    bus = bus_name()
    if not bus:
        # Events are switched off. This is a supported configuration, not a
        # failure: the demo runs without a bus and nothing about the order,
        # the quotation or the plan is different.
        return None

    try:
        response = (client or _events_client()).put_events(Entries=[{
            "Source": SOURCE,
            "DetailType": event_type,
            "EventBusName": bus,
            "Detail": json.dumps(detail),
        }])
        failed = int((response or {}).get("FailedEntryCount") or 0)
        if failed:
            # EventBridge accepted the call and rejected the entry. Same
            # consequence as a raised error, and the same response.
            print(json.dumps({"event": "business_event_rejected",
                              "eventType": event_type, "jobId": job_id}))
            metrics.emit(metrics.EVENT_PUBLISH_FAILURES, job_id=job_id,
                         dimensions={"EventType": event_type})
            return None
    except Exception as exc:  # noqa: BLE001 - observability may not break work
        print(json.dumps({"event": "business_event_failed",
                          "eventType": event_type, "jobId": job_id,
                          "error": f"{type(exc).__name__}: {str(exc)[:200]}"}))
        metrics.emit(metrics.EVENT_PUBLISH_FAILURES, job_id=job_id,
                     dimensions={"EventType": event_type})
        return None

    metrics.emit(metrics.BUSINESS_EVENTS, job_id=job_id,
                 dimensions={"EventType": event_type})
    return detail


# ---------------------------------------------------------------------------
# The callers' helpers
# ---------------------------------------------------------------------------
# Each of these takes engine output and copies the fields out of it. They exist
# so that no call site has to decide what goes in an event, and so that the
# mapping from an engine's own dictionary to an event body is in one readable
# place rather than spread across two Lambdas.


def supplier_price_changed(comparison: Dict, shop_id: str = DEFAULT_SHOP_ID,
                           **extra) -> Dict:
    """From `engine.supplier_prices.PriceComparison.as_dict()`."""
    return {
        "skuId": comparison.get("skuId"),
        "previousPrice": comparison.get("previousPrice"),
        "newPrice": comparison.get("currentPrice"),
        "changePercent": comparison.get("percentageDelta"),
        "direction": comparison.get("direction"),
        "materialChange": bool(comparison.get("materialChange")),
        **extra,
    }


def supplier_price_alert(alert: Dict, **extra) -> Dict:
    """From `engine.price_alerts.evaluate_price_change(...)`.

    The alert's own figures, copied. Supplier cost is owner data and is
    allowed here because this event goes to the owner's alert topic; no
    customer, no model text and no prompt is ever part of it.
    """
    return {
        "skuId": alert.get("skuId"),
        "product": alert.get("product"),
        "supplier": alert.get("supplier"),
        "oldCost": alert.get("oldCost"),
        "newCost": alert.get("newCost"),
        "absoluteDelta": alert.get("absoluteDelta"),
        "percentageDelta": alert.get("percentageDelta"),
        "oldMargin": alert.get("oldMargin"),
        "newMargin": alert.get("newMargin"),
        "marginDelta": alert.get("marginDelta"),
        "severity": alert.get("severity"),
        "walkAwayPrice": (alert.get("walkAway") or {}).get("price"),
        "alertId": alert.get("alertId"),
        **extra,
    }


def daily_shop_brief_generated(brief: Dict, **extra) -> Dict:
    """From `engine.brief.build_brief(...)`. Counts and the plan's totals."""
    counts = brief.get("counts") or {}
    planner = brief.get("planner") or {}
    return {
        "briefDate": brief.get("date"),
        "shortages": counts.get("shortages"),
        "lowStock": counts.get("lowStock"),
        "supplierAlerts": counts.get("supplierAlerts"),
        "criticalAlerts": counts.get("criticalAlerts"),
        "marginRisks": counts.get("marginRisks"),
        "plannedSpend": planner.get("totalSpend"),
        "remaining": planner.get("remaining"),
        **extra,
    }


def stockout_detected(line: Dict, **extra) -> Dict:
    """From one line of `engine.quote.calculate_quote(...).as_dict()`."""
    return {
        "skuId": line.get("skuId"),
        "requested": line.get("quantity"),
        "available": line.get("availableQty"),
        "shortage": line.get("shortageQty"),
        **extra,
    }


def low_margin_detected(alert: Dict, **extra) -> Dict:
    """From one entry of `engine.margin.margin_alerts(...)`."""
    return {
        "skuId": alert.get("skuId"),
        "previousMargin": alert.get("oldMarginAmount"),
        "newMargin": alert.get("newMarginAmount"),
        "marginPercent": alert.get("newMarginPercent"),
        "status": alert.get("status"),
        **extra,
    }


def purchase_plan_generated(plan: Dict, **extra) -> Dict:
    """From `engine.purchasing.build_purchase_plan(...)`."""
    return {
        "budget": plan.get("budget"),
        "plannedSpend": plan.get("totalSpend"),
        "restockCost": plan.get("restockCost"),
        "remaining": plan.get("remaining"),
        **extra,
    }


def unique(details: List[Dict], *keys: str) -> List[Dict]:
    """Drop repeats, so one order cannot emit the same fact twice.

    Idempotency where it is cheap and obvious: two quoted lines for the same
    SKU are one stockout, not two, and a subscriber that sends an email should
    not send it twice because a document listed a product on two rows.
    """
    seen, out = set(), []
    for detail in details:
        signature = tuple(detail.get(k) for k in (keys or ("skuId",)))
        if signature in seen:
            continue
        seen.add(signature)
        out.append(detail)
    return out
