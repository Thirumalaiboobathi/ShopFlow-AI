"""Business events, and the rule that they may never change a business result.

An event says something happened in the shop. It is published after the fact
is durable, it carries numbers a deterministic engine already calculated, and
it can fail without consequence. That last property is the one worth testing
hardest: EventBridge being down, throttled or simply not configured must leave
the quotation, the plan and the job row exactly as they would have been.

The second thing tested here is the width of the surface. An event goes to a
bus, a bus goes to SNS, and SNS goes to somebody's inbox - outside every
boundary this project enforces. So an event carries a handful of scalars from
a short list, and a customer's phone number, a credit limit and anything a
model wrote cannot travel on one whatever a caller passes.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from engine.loader import load_dataset  # noqa: E402
from engine.margin import margin_alerts  # noqa: E402
from engine.purchasing import build_purchase_plan  # noqa: E402
from engine.quote import calculate_quote  # noqa: E402
from engine.supplier_prices import compare_price  # noqa: E402
from observability import events, metrics  # noqa: E402

WIRE = "W-FIN-1.5-RED-90M"
SWITCH = "SW-ANC-1W10A"
MCB = "MCB-HAV-SP-32A-C"

CANONICAL_ITEMS = [
    {"skuId": SWITCH, "quantity": 20},
    {"skuId": WIRE, "quantity": 3, "uom": "COIL"},
    {"skuId": MCB, "quantity": 2},
]


class FakeBus:
    """Records what was put on the bus, or fails in a chosen way."""

    def __init__(self, error=None, failed_entries=0):
        self.entries = []
        self._error = error
        self._failed = failed_entries

    def put_events(self, Entries):  # noqa: N803 - boto3's own casing
        if self._error:
            raise self._error
        self.entries.extend(Entries)
        return {"FailedEntryCount": self._failed}


@pytest.fixture
def bus(monkeypatch):
    monkeypatch.setenv("EVENT_BUS_NAME", "shopflow-business-events")
    return FakeBus()


@pytest.fixture
def no_bus(monkeypatch):
    monkeypatch.delenv("EVENT_BUS_NAME", raising=False)


# ---------------------------------------------------------------------------
# 1. the right event, with the engine's own numbers
# ---------------------------------------------------------------------------

def test_a_supplier_price_change_carries_the_comparison_engine_figures(bus):
    data = load_dataset()
    comparison = compare_price(data, WIRE, 6300.0).as_dict()

    detail = events.publish(events.SUPPLIER_PRICE_CHANGED, client=bus,
                            **events.supplier_price_changed(comparison))

    assert detail["eventType"] == "SupplierPriceChanged"
    assert detail["skuId"] == WIRE
    assert detail["previousPrice"] == 5900.0
    assert detail["newPrice"] == 6300.0
    assert detail["changePercent"] == 6.78
    assert detail["materialChange"] is True
    # Byte for byte what the engine said, not a recomputation.
    assert detail["changePercent"] == comparison["percentageDelta"]


def test_a_stockout_carries_the_quotation_engine_figures(bus):
    data = load_dataset()
    quote = calculate_quote(data, CANONICAL_ITEMS).as_dict()
    line = next(l for l in quote["lines"] if l["skuId"] == SWITCH)

    detail = events.publish(events.STOCKOUT_DETECTED, client=bus,
                            **events.stockout_detected(line))

    assert detail["skuId"] == SWITCH
    assert detail["requested"] == 20
    assert detail["shortage"] == 6
    assert detail["shortage"] == line["shortageQty"]


def test_a_low_margin_event_carries_the_margin_engine_figures(bus):
    alert = margin_alerts(load_dataset(), {WIRE: 6300.0})[0]

    detail = events.publish(events.LOW_MARGIN_DETECTED, client=bus,
                            **events.low_margin_detected(alert))

    assert detail["previousMargin"] == 708.0
    assert detail["newMargin"] == 308.0
    assert detail["status"] == "LOW_MARGIN"


def test_a_purchase_plan_event_carries_the_planner_figures(bus):
    plan = build_purchase_plan(load_dataset(), 25000)

    detail = events.publish(events.PURCHASE_PLAN_GENERATED, client=bus,
                            **events.purchase_plan_generated(plan))

    assert detail["budget"] == 25000.0
    assert detail["plannedSpend"] == plan["totalSpend"]
    assert detail["plannedSpend"] == 24996.56


def test_the_event_is_addressed_to_the_shopflow_bus_and_source(bus):
    events.publish(events.STOCKOUT_DETECTED, client=bus, skuId=SWITCH)

    entry = bus.entries[0]
    assert entry["Source"] == "shopflow.business"
    assert entry["EventBusName"] == "shopflow-business-events"
    assert entry["DetailType"] == "StockoutDetected"
    assert json.loads(entry["Detail"])["shopId"] == "SHOP#demo"


def test_only_the_listed_event_types_exist():
    assert set(events.EVENT_TYPES) == {
        "SupplierPriceChanged", "StockoutDetected", "LowMarginDetected",
        "PurchasePlanGenerated", "OrderNeedsClarification",
        "OrderProcessingFailed", "DailyShopBriefGenerated"}
    assert set(events.EVENT_TYPES) == \
        set(metrics._ALLOWED_VALUES["EventType"])


def test_an_unknown_event_type_is_dropped_not_published(bus):
    assert events.publish("ShopBurnedDown", client=bus) is None
    assert bus.entries == []


# ---------------------------------------------------------------------------
# 2. failure changes nothing
# ---------------------------------------------------------------------------

def test_a_bus_that_raises_does_not_raise_at_the_caller(bus, monkeypatch):
    broken = FakeBus(error=RuntimeError("EventBridge is unavailable"))
    assert events.publish(events.STOCKOUT_DETECTED, client=broken,
                          skuId=SWITCH) is None


def test_a_rejected_entry_is_reported_as_a_failure(bus):
    rejecting = FakeBus(failed_entries=1)
    assert events.publish(events.STOCKOUT_DETECTED, client=rejecting,
                          skuId=SWITCH) is None


def test_no_bus_configured_is_a_supported_state_not_an_error(no_bus):
    """The demo runs without a bus and nothing about it is different."""
    assert events.bus_name() == ""
    assert events.publish(events.STOCKOUT_DETECTED, skuId=SWITCH) is None


def test_the_quotation_is_identical_whether_or_not_events_are_published():
    """The property that matters. Same order, two worlds, one answer."""
    data = load_dataset()
    quote = calculate_quote(data, CANONICAL_ITEMS).as_dict()

    broken = FakeBus(error=RuntimeError("down"))
    for line in quote["lines"]:
        if line["shortageQty"]:
            events.publish(events.STOCKOUT_DETECTED, client=broken,
                           **events.stockout_detected(line))

    again = calculate_quote(data, CANONICAL_ITEMS).as_dict()
    assert again == quote
    assert quote["total"] == 22306.48


def test_a_publish_failure_is_visible_as_a_metric(monkeypatch):
    monkeypatch.setenv("EVENT_BUS_NAME", "shopflow-business-events")
    emitted = []
    monkeypatch.setattr(metrics, "emit",
                        lambda name, *a, **k: emitted.append(name))

    events.publish(events.STOCKOUT_DETECTED,
                   client=FakeBus(error=RuntimeError("down")), skuId=SWITCH)

    assert metrics.EVENT_PUBLISH_FAILURES in emitted


# ---------------------------------------------------------------------------
# 3. idempotency where it is required
# ---------------------------------------------------------------------------

def test_one_sku_quoted_twice_is_one_stockout():
    """A subscriber that emails must not email twice for one order."""
    details = [
        events.stockout_detected({"skuId": SWITCH, "quantity": 20,
                                  "shortageQty": 6}),
        events.stockout_detected({"skuId": SWITCH, "quantity": 5,
                                  "shortageQty": 2}),
        events.stockout_detected({"skuId": WIRE, "quantity": 3,
                                  "shortageQty": 2}),
    ]
    assert [d["skuId"] for d in events.unique(details)] == [SWITCH, WIRE]


def test_deduplication_keeps_the_first_occurrence():
    details = events.unique([{"skuId": SWITCH, "shortage": 6},
                             {"skuId": SWITCH, "shortage": 99}])
    assert details == [{"skuId": SWITCH, "shortage": 6}]


# ---------------------------------------------------------------------------
# 4. what an event may never carry
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("field", [
    "phone", "customerName", "customerId", "creditLimit", "outstandingAmount",
    "thinking", "reasoning", "prompt", "systemPrompt", "trace", "orderText",
    "transcript", "apiKey", "token", "credentials", "summary", "question",
])
def test_a_forbidden_field_cannot_be_put_on_an_event(bus, field):
    detail = events.publish(events.STOCKOUT_DETECTED, client=bus,
                            skuId=SWITCH, **{field: "leaked"})
    assert field not in detail
    assert "leaked" not in json.dumps(detail)


def test_a_structure_cannot_be_smuggled_onto_an_event(bus):
    """A whole quotation, a match list or a raw trace does not fit."""
    detail = events.publish(
        events.STOCKOUT_DETECTED, client=bus, skuId=SWITCH,
        quote={"total": 22306.48, "lines": [1, 2, 3]},
        matches=[{"status": "RESOLVED"}])
    assert "quote" not in detail
    assert "matches" not in detail


def test_an_event_is_small(bus):
    detail = events.publish(events.SUPPLIER_PRICE_CHANGED, client=bus,
                            **events.supplier_price_changed(
                                compare_price(load_dataset(), WIRE,
                                              6300.0).as_dict()))
    assert len(detail) <= events.MAX_DETAIL_KEYS
    assert len(json.dumps(detail)) < 600


def test_a_long_string_is_truncated_rather_than_published_whole(bus):
    detail = events.publish(events.ORDER_PROCESSING_FAILED, client=bus,
                            reason="x" * 5000)
    assert len(detail["reason"]) == events.MAX_STRING_LENGTH


def test_every_event_says_which_shop_and_when(bus):
    for event_type in events.EVENT_TYPES:
        detail = events.publish(event_type, client=bus)
        assert detail["shopId"] == "SHOP#demo"
        assert isinstance(detail["timestamp"], int)


# ---------------------------------------------------------------------------
# 5. a clarification is not an alert
# ---------------------------------------------------------------------------

def test_a_clarification_is_an_event_but_not_an_alerting_one(bus):
    """It is published, so it can be counted. It is not routed to SNS.

    The routing itself is a CDK rule and is asserted in
    tests/test_queue.py::test_8m. This pins the other half: ShopFlow does
    emit the event, so the Operations tab can count clarifications, and the
    rule is what keeps it out of somebody's inbox.
    """
    detail = events.publish(events.ORDER_NEEDS_CLARIFICATION, client=bus,
                            jobId="abc", attribute="colour")
    assert detail["eventType"] == "OrderNeedsClarification"

    rule_source = (ROOT / "infrastructure" / "shopflow_stack.py").read_text(
        encoding="utf-8")
    alerting = rule_source.split("alerting_events = [")[1].split("]")[0]
    assert "OrderNeedsClarification" not in alerting
    for alerted in ("SupplierPriceChanged", "StockoutDetected",
                    "LowMarginDetected", "OrderProcessingFailed"):
        assert alerted in alerting
