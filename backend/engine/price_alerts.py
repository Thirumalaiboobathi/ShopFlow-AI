"""Supplier price shock alerts: is this supplier price change worth telling
the owner about, and how bad is it?

WHAT THIS DECIDES
-----------------
For one product whose supplier cost moved from OLD to NEW:

    absolute delta      NEW - OLD
    percentage delta    (NEW - OLD) / OLD x 100
    margin before/after selling price - OLD, selling price - NEW
    walk-away price     engine.whatif.walk_away_price - reused, not redone
    planner impact      the purchase planner run at OLD and at NEW

and, from those figures and the configuration below, whether it is an alert
at all and at what severity. Every figure is an engine's; no model is
involved, and nothing here is written anywhere.

WHEN IT IS AN ALERT
-------------------
Only a cost INCREASE can alert, and only when at least one trigger fires:

    PERCENT_THRESHOLD     percentage delta >= the price alert threshold
    ABSOLUTE_THRESHOLD    absolute delta   >= the INR threshold
    MARGIN_FLOOR_BREACH   the margin was at or above the floor and is now below
    MARGIN_NEAR_FLOOR     the margin has just come within MARGIN_APPROACH_POINTS
                          of the floor (crossing into that zone, not sitting
                          in it - a margin already there does not re-alert)
    PURCHASING_CAPACITY   the planner can fund at least this much less restock

A small move with none of these is INFO and is not an alert - the owner is
not emailed about a two-rupee change.

SEVERITY
--------
    CRITICAL  an alert where the new margin is below the floor, or the new
              cost is above the walk-away price
    WARNING   any other alert
    INFO      not an alert

CONFIGURATION
-------------
One place. The percentage threshold is the one the supplier price engine
already uses (`engine.pricing.PRICE_ALERT_THRESHOLD_PERCENT`) and the margin
floor is the margin engine's warning threshold; neither is restated here.
Each may be overridden by an environment variable so a deployment can tune
them without a code change.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, Optional

from . import gst
from .margin import MARGIN_WARNING_PERCENT
from .messages import format_rupees
from .models import Dataset
from .pricing import PRICE_ALERT_THRESHOLD_PERCENT, is_material_change
from .whatif import (DEFAULT_BUDGET, _margin, _plan, _plan_summary,
                     walk_away_price)

INFO = "INFO"
WARNING = "WARNING"
CRITICAL = "CRITICAL"
SEVERITIES = (INFO, WARNING, CRITICAL)

PERCENT_THRESHOLD = "PERCENT_THRESHOLD"
ABSOLUTE_THRESHOLD = "ABSOLUTE_THRESHOLD"
MARGIN_FLOOR_BREACH = "MARGIN_FLOOR_BREACH"
MARGIN_NEAR_FLOOR = "MARGIN_NEAR_FLOOR"
PURCHASING_CAPACITY = "PURCHASING_CAPACITY"

# Defaults. The first two are other engines' own constants, read here rather
# than copied, so there is one number for each idea in the codebase.
DEFAULT_PERCENT_THRESHOLD = PRICE_ALERT_THRESHOLD_PERCENT     # 5%
DEFAULT_MARGIN_FLOOR_PERCENT = MARGIN_WARNING_PERCENT          # 10%
DEFAULT_ABSOLUTE_THRESHOLD_INR = 250.0
DEFAULT_MARGIN_APPROACH_POINTS = 2.0
DEFAULT_CAPACITY_REDUCTION_INR = 500.0

HUNDRED = Decimal("100")
ZERO = Decimal("0")


@dataclass(frozen=True)
class AlertConfig:
    percentThreshold: float = DEFAULT_PERCENT_THRESHOLD
    absoluteThresholdInr: float = DEFAULT_ABSOLUTE_THRESHOLD_INR
    marginFloorPercent: float = DEFAULT_MARGIN_FLOOR_PERCENT
    marginApproachPoints: float = DEFAULT_MARGIN_APPROACH_POINTS
    capacityReductionInr: float = DEFAULT_CAPACITY_REDUCTION_INR

    def as_dict(self) -> dict:
        return dict(self.__dict__)


_ENV = {
    "percentThreshold": "SHOPFLOW_PRICE_ALERT_PERCENT",
    "absoluteThresholdInr": "SHOPFLOW_PRICE_ALERT_ABSOLUTE_INR",
    "marginFloorPercent": "SHOPFLOW_MARGIN_FLOOR_PERCENT",
    "marginApproachPoints": "SHOPFLOW_MARGIN_APPROACH_POINTS",
    "capacityReductionInr": "SHOPFLOW_CAPACITY_REDUCTION_INR",
}


def alert_config(environ=None) -> AlertConfig:
    """The configuration, with any valid environment override applied.

    An override that is not a positive number is ignored rather than trusted:
    a typo in a Lambda variable must not switch alerting off.
    """
    environ = os.environ if environ is None else environ
    values = {}
    for field, var in _ENV.items():
        raw = environ.get(var)
        if raw in (None, ""):
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if value > 0 and value < 1_000_000:
            values[field] = value
    return AlertConfig(**values)


def alert_id(sku_id: str, old_cost, new_cost) -> str:
    """One id per distinct price move, so the same move alerts once."""
    return (f"PRICE#{sku_id}#{gst.out(gst.paise(gst.to_decimal(old_cost))):.2f}"
            f"#{gst.out(gst.paise(gst.to_decimal(new_cost))):.2f}")


def evaluate_price_change(data: Dataset, sku_id: str, old_cost, new_cost, *,
                          supplier: str = "",
                          confirmed_costs: Optional[Dict[str, float]] = None,
                          budget: float = DEFAULT_BUDGET,
                          config: Optional[AlertConfig] = None,
                          now: Optional[int] = None) -> dict:
    """Everything an owner needs to judge one supplier price move.

    `confirmed_costs` are the shop's other confirmed costs; this SKU's entry,
    if any, is replaced by OLD for the baseline and by NEW for the scenario.
    Raises ValueError for an unknown SKU or a cost that is not positive.
    """
    if sku_id not in data.products:
        raise ValueError(f"unknown SKU: {sku_id}")
    old, new = gst.to_decimal(old_cost), gst.to_decimal(new_cost)
    if old <= ZERO or new <= ZERO:
        raise ValueError("supplier costs must be positive")
    cfg = config or alert_config()
    product = data.product(sku_id)
    price = gst.to_decimal(product.sellingPrice)

    delta = gst.paise(new - old)
    percent = gst.paise(delta / old * HUNDRED)
    before, after = _margin(price, old), _margin(price, new)

    others = {k: float(v) for k, v in (confirmed_costs or {}).items()
              if k != sku_id and k in data.products}
    base_plan = _plan_summary(_plan(data, budget, others, {sku_id: float(old)}))
    new_plan = _plan_summary(_plan(data, budget, others, {sku_id: float(new)}))
    capacity_loss = gst.paise(gst.to_decimal(base_plan["restockCost"])
                              - gst.to_decimal(new_plan["restockCost"]))

    walk = walk_away_price(
        data, sku_id,
        {"marginFloorPercent": Decimal(str(cfg.marginFloorPercent))},
        {**others, sku_id: float(new)}, budget)["scenarioValues"]

    floor = cfg.marginFloorPercent
    new_pct = after["percent"] if after["percent"] is not None else -100.0
    old_pct = before["percent"] if before["percent"] is not None else -100.0

    triggers = []
    if delta > ZERO:
        # The same test the supplier price review applies - one boundary.
        if is_material_change(old, new, cfg.percentThreshold):
            triggers.append(PERCENT_THRESHOLD)
        if float(delta) >= cfg.absoluteThresholdInr:
            triggers.append(ABSOLUTE_THRESHOLD)
        if old_pct >= floor > new_pct:
            triggers.append(MARGIN_FLOOR_BREACH)
        near = floor + cfg.marginApproachPoints
        if old_pct >= near > new_pct >= floor:
            triggers.append(MARGIN_NEAR_FLOOR)
        if float(capacity_loss) >= cfg.capacityReductionInr:
            triggers.append(PURCHASING_CAPACITY)
    is_alert = bool(triggers)
    above_walk_away = bool(walk["currentCostAboveWalkAway"])

    if is_alert and (new_pct < floor or above_walk_away):
        severity = CRITICAL
    elif is_alert:
        severity = WARNING
    else:
        severity = INFO

    return {
        "alertId": alert_id(sku_id, old, new),
        "alert": is_alert,
        "severity": severity,
        "triggers": triggers,
        "skuId": sku_id,
        "product": product.name,
        "supplier": supplier or "",
        "oldCost": gst.out(old),
        "newCost": gst.out(new),
        "absoluteDelta": gst.out(delta),
        "percentageDelta": gst.out(percent),
        # The review's own verdict on this move, from the same function.
        "materialChange": is_material_change(old, new, cfg.percentThreshold),
        "direction": ("INCREASE" if delta > ZERO else
                      "DECREASE" if delta < ZERO else "UNCHANGED"),
        "sellingPrice": gst.out(price),
        "oldMargin": before["amount"],
        "newMargin": after["amount"],
        "marginDelta": gst.out(gst.to_decimal(after["amount"])
                               - gst.to_decimal(before["amount"])),
        "oldMarginPercent": before["percent"],
        "newMarginPercent": after["percent"],
        "marginStatus": after["status"],
        "walkAway": {
            "price": walk["walkAwayPrice"],
            "bindingLimit": walk["bindingLimit"],
            "differenceFromCurrentCost": walk["differenceFromCurrentCost"],
            "currentCostAboveWalkAway": above_walk_away,
        },
        "plannerImpact": {
            "budget": float(budget),
            "restockCostBefore": base_plan["restockCost"],
            "restockCostAfter": new_plan["restockCost"],
            "restockCapacityReduction": gst.out(capacity_loss),
            "commitmentCostChange": gst.out(
                gst.to_decimal(new_plan["commitmentCost"])
                - gst.to_decimal(base_plan["commitmentCost"])),
            "allCommitmentsFunded": new_plan["allCommitmentsFunded"],
        },
        "config": cfg.as_dict(),
        "timestamp": int(now) if now is not None else None,
        "source": "engine.price_alerts",
    }


def alert_lines(alert: dict) -> list:
    """The alert as plain lines, built only from the alert's own figures."""
    rupee = format_rupees
    lines = [
        f"{alert['severity']}: {alert['product']}",
        f"Supplier cost {rupee(alert['oldCost'])} -> {rupee(alert['newCost'])} "
        f"({'+' if alert['absoluteDelta'] >= 0 else ''}"
        f"{rupee(alert['absoluteDelta'])}, "
        f"{'+' if alert['percentageDelta'] >= 0 else ''}"
        f"{alert['percentageDelta']:.2f}%)",
        f"Margin {rupee(alert['oldMargin'])} -> {rupee(alert['newMargin'])}",
    ]
    walk = alert["walkAway"]
    if walk["currentCostAboveWalkAway"]:
        lines.append(f"Walk-away price {rupee(walk['price'])}: current cost is "
                     f"{rupee(walk['differenceFromCurrentCost'])} above it.")
    else:
        lines.append(f"Walk-away price {rupee(walk['price'])}.")
    return lines
