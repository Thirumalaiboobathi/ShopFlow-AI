"""Sales velocity and stock coverage.

These two numbers drive every restocking decision, so they are deliberately
simple and auditable: a trailing mean, and a division.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import List

from .models import Dataset, WeeklySales

# Trailing window used for velocity. Six months of history is seeded, but a
# shorter window responds to recent demand instead of averaging it away.
VELOCITY_WINDOW_WEEKS = 8

INFINITE_COVERAGE = math.inf


@dataclass(frozen=True)
class VelocityResult:
    skuId: str
    weeklyVelocity: float
    windowWeeks: int
    unitsInWindow: int

    def as_evidence(self) -> dict:
        return {
            "skuId": self.skuId,
            "weeklyVelocity": round(self.weeklyVelocity, 2),
            "windowWeeks": self.windowWeeks,
            "unitsInWindow": self.unitsInWindow,
        }


def sales_velocity(
    weekly: List[WeeklySales], window_weeks: int = VELOCITY_WINDOW_WEEKS
) -> float:
    """Mean units sold per week over the trailing window.

    Weeks with zero sales count as zero, not as missing data - a product that
    did not sell is genuinely slow-moving, and dropping those weeks would
    flatter dead stock.
    """
    if window_weeks <= 0:
        raise ValueError("window_weeks must be positive")
    if not weekly:
        return 0.0
    ordered = sorted(weekly, key=lambda w: w.weekStart)
    window = ordered[-window_weeks:]
    total = sum(max(0, w.unitsSold) for w in window)
    return total / float(len(window))


def velocity_for(
    data: Dataset, skuId: str, window_weeks: int = VELOCITY_WINDOW_WEEKS
) -> VelocityResult:
    weekly = data.weeklySales(skuId)
    ordered = sorted(weekly, key=lambda w: w.weekStart)[-window_weeks:]
    v = sales_velocity(weekly, window_weeks)
    return VelocityResult(
        skuId=skuId,
        weeklyVelocity=v,
        windowWeeks=len(ordered) if ordered else 0,
        unitsInWindow=sum(max(0, w.unitsSold) for w in ordered),
    )


def coverage_weeks(available: int, weekly_velocity: float) -> float:
    """How many weeks the stock on hand will last at the current rate.

    Zero velocity means the stock never depletes through sales, so coverage is
    infinite. That is the correct signal for dead stock: it must never look
    urgent to the restocking allocator.
    """
    stock = max(0, available)
    if weekly_velocity <= 0:
        return INFINITE_COVERAGE
    return stock / weekly_velocity


def coverage_for(
    data: Dataset, skuId: str, available: int | None = None
) -> float:
    stock = data.onHand(skuId) if available is None else available
    return coverage_weeks(stock, velocity_for(data, skuId).weeklyVelocity)
