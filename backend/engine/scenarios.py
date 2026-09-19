"""Detectors for the business conditions ShopFlow is built to catch.

Each detector is a pure query over the dataset. The seed generator plants these
conditions deliberately; the test suite asserts the detectors find them. That
pairing is what stops the demo from drifting away from the data behind it.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, List

from .models import Dataset
from .pricing import cheaper_alternatives, detect_price_increases
from .shortage import shortages
from .velocity import INFINITE_COVERAGE, coverage_weeks, velocity_for

# A product with more than this many weeks of cover is not selling through.
SLOW_MOVING_WEEKS = 12.0
# Weeks of zero sales before stock is considered dead.
DEAD_STOCK_WINDOW_WEEKS = 12


@dataclass(frozen=True)
class SkuFinding:
    skuId: str
    detail: Dict

    def as_dict(self) -> dict:
        return {"skuId": self.skuId, **self.detail}


def imminent_stockouts(data: Dataset) -> List[SkuFinding]:
    """Stock that will run out before a replacement order could arrive."""
    found = []
    for skuId in sorted(data.products):
        v = velocity_for(data, skuId).weeklyVelocity
        if v <= 0:
            continue
        cover = coverage_weeks(data.onHand(skuId), v)
        lead_weeks = data.supplierFor(skuId).leadTimeWeeks
        if cover < lead_weeks:
            found.append(
                SkuFinding(
                    skuId,
                    {
                        "coverageWeeks": round(cover, 2),
                        "leadTimeWeeks": round(lead_weeks, 2),
                        "weeklyVelocity": round(v, 2),
                        "onHand": data.onHand(skuId),
                    },
                )
            )
    return found


def dead_stock(data: Dataset) -> List[SkuFinding]:
    """Stock on hand with no sales at all in the recent window."""
    found = []
    for skuId in sorted(data.products):
        on_hand = data.onHand(skuId)
        if on_hand <= 0:
            continue
        recent = data.weeklySales(skuId)[-DEAD_STOCK_WINDOW_WEEKS:]
        if recent and sum(w.unitsSold for w in recent) == 0:
            found.append(
                SkuFinding(
                    skuId,
                    {
                        "onHand": on_hand,
                        "weeksWithoutSale": len(recent),
                        "capitalTied": round(
                            on_hand * data.product(skuId).costPrice, 2
                        ),
                    },
                )
            )
    return found


def slow_moving(data: Dataset) -> List[SkuFinding]:
    """Selling, but far too slowly for the stock held."""
    found = []
    for skuId in sorted(data.products):
        v = velocity_for(data, skuId).weeklyVelocity
        if v <= 0:
            continue  # that is dead stock, reported separately
        cover = coverage_weeks(data.onHand(skuId), v)
        if cover != INFINITE_COVERAGE and cover > SLOW_MOVING_WEEKS:
            found.append(
                SkuFinding(
                    skuId,
                    {"coverageWeeks": round(cover, 2), "weeklyVelocity": round(v, 2)},
                )
            )
    return found


@dataclass(frozen=True)
class AmbiguityGroup:
    distinguishingAttribute: str
    shared: Dict
    skuIds: List[str]

    def as_dict(self) -> dict:
        return {
            "distinguishingAttribute": self.distinguishingAttribute,
            "shared": self.shared,
            "skuIds": self.skuIds,
        }


# Attributes checked for "these differ only by X". Ordered by how often a
# customer leaves them unsaid in a spoken order.
_AMBIGUITY_ATTRS = ("colour", "length", "specification", "brand")


def ambiguity_groups(data: Dataset) -> List[AmbiguityGroup]:
    """Sets of SKUs identical except for exactly one attribute.

    These are the orders the agent must refuse to guess at. "Finolex 1.5 sq mm
    wire" maps to three SKUs that differ only in colour, so the correct
    behaviour is a clarification request, not a coin flip.
    """
    groups: List[AmbiguityGroup] = []

    for attr in _AMBIGUITY_ATTRS:
        buckets: Dict[tuple, List[str]] = defaultdict(list)
        for skuId, p in data.products.items():
            key = (
                p.brand if attr != "brand" else None,
                p.category,
                p.specification if attr != "specification" else None,
                p.colour if attr != "colour" else None,
                p.length if attr != "length" else None,
            )
            buckets[key].append(skuId)

        for key, skus in buckets.items():
            if len(skus) < 2:
                continue
            # Only a real ambiguity if the varying attribute actually differs.
            values = {getattr(data.product(s), attr) for s in skus}
            if len(values) < 2:
                continue
            brand, category, spec, colour, length = key
            shared = {
                "category": category,
                "brand": brand,
                "specification": spec,
                "colour": colour,
                "length": length,
            }
            groups.append(
                AmbiguityGroup(
                    distinguishingAttribute=attr,
                    shared={k: v for k, v in shared.items() if v is not None},
                    skuIds=sorted(skus),
                )
            )

    return sorted(groups, key=lambda g: (g.distinguishingAttribute, g.skuIds))


def supplier_price_increases(data: Dataset, threshold_percent: float = 1.0):
    return detect_price_increases(data, threshold_percent)


def committed_shortages(data: Dataset):
    return [s for s in shortages(data) if s.isShort]


def cheaper_supplier_options(data: Dataset) -> List[SkuFinding]:
    found = []
    for skuId in sorted(data.products):
        alts = cheaper_alternatives(data, skuId)
        if alts:
            found.append(SkuFinding(skuId, {"alternatives": [a.as_evidence() for a in alts]}))
    return found


def scenario_report(data: Dataset) -> Dict[str, int]:
    """Counts of every planted condition - a quick health check on the seed."""
    return {
        "imminentStockouts": len(imminent_stockouts(data)),
        "deadStock": len(dead_stock(data)),
        "slowMoving": len(slow_moving(data)),
        "ambiguityGroups": len(ambiguity_groups(data)),
        "supplierPriceIncreases": len(supplier_price_increases(data)),
        "committedShortages": len(committed_shortages(data)),
        "cheaperSupplierOptions": len(cheaper_supplier_options(data)),
    }
