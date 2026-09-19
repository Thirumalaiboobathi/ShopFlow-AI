"""Sales velocity and coverage."""

from __future__ import annotations

import math

import pytest

from conftest import make_dataset, make_product, weeks
from engine.models import WeeklySales
from engine.velocity import (
    INFINITE_COVERAGE,
    coverage_for,
    coverage_weeks,
    sales_velocity,
    velocity_for,
)


def rows(units):
    return [WeeklySales("SKU", w, u) for w, u in zip(weeks(len(units)), units)]


def test_velocity_is_mean_over_window():
    assert sales_velocity(rows([4, 5, 4, 4, 3, 5, 4, 5])) == pytest.approx(4.25)


def test_velocity_uses_only_trailing_window():
    # Older weeks of heavy sales must not inflate a recently quiet product.
    history = rows([100] * 10 + [2] * 8)
    assert sales_velocity(history, window_weeks=8) == pytest.approx(2.0)


def test_velocity_counts_zero_weeks_as_zero_not_missing():
    # Dropping empty weeks would make a slow mover look healthy.
    assert sales_velocity(rows([4, 0, 0, 0, 4, 0, 0, 0])) == pytest.approx(1.0)


def test_velocity_of_empty_history_is_zero():
    assert sales_velocity([]) == 0.0


def test_velocity_window_shorter_than_history_available():
    # Only 3 weeks exist; the mean is over those 3, not over the 8-week window.
    assert sales_velocity(rows([3, 6, 3])) == pytest.approx(4.0)


def test_velocity_rejects_non_positive_window():
    with pytest.raises(ValueError):
        sales_velocity(rows([1, 2]), window_weeks=0)


def test_negative_units_are_floored_at_zero():
    # A return recorded as negative must not offset real demand.
    assert sales_velocity(rows([-5, 5, 5, 5])) == pytest.approx(3.75)


def test_coverage_is_stock_divided_by_velocity():
    assert coverage_weeks(17, 4.25) == pytest.approx(4.0)


def test_zero_velocity_gives_infinite_coverage():
    # Dead stock must never look urgent to the allocator.
    assert coverage_weeks(50, 0.0) == INFINITE_COVERAGE
    assert math.isinf(coverage_weeks(50, 0.0))


def test_zero_stock_gives_zero_coverage():
    assert coverage_weeks(0, 4.0) == 0.0


def test_negative_stock_is_treated_as_zero():
    assert coverage_weeks(-12, 4.0) == 0.0


def test_velocity_and_coverage_over_a_dataset():
    p = make_product("A", cost=100, selling=150)
    data = make_dataset([p], inventory={"A": 21}, velocity={"A": 7})
    assert velocity_for(data, "A").weeklyVelocity == pytest.approx(7.0)
    assert coverage_for(data, "A") == pytest.approx(3.0)


def test_velocity_evidence_reports_window_and_units():
    p = make_product("A", cost=100, selling=150)
    data = make_dataset([p], velocity={"A": 3}, history_weeks=8)
    ev = velocity_for(data, "A").as_evidence()
    assert ev == {"skuId": "A", "weeklyVelocity": 3.0, "windowWeeks": 8,
                  "unitsInWindow": 24}


def test_seeded_demo_wire_velocity_is_stable(seeded):
    # The demo narrates this number out loud, so it is pinned.
    v = velocity_for(seeded, "W-FIN-1.5-RED-90M").weeklyVelocity
    assert v == pytest.approx(4.25)
