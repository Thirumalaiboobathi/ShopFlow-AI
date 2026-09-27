"""Two-tier deterministic budget allocation.

This is a greedy allocator, not an optimiser. It is deliberately simple so that
every rupee it spends can be explained to a shop owner in one sentence and
reproduced exactly by a test.

POLICY
------
Tier 1 - committed customer orders.
    Any shortage created by a committed order is funded first, in order of the
    earliest promised delivery date. A customer promise outranks every
    discretionary restock regardless of how profitable that restock would be.

Tier 2 - discretionary restocking.
    Whatever budget survives Tier 1 is allocated by a transparent priority
    score, highest first:

        coverageWeeks   = availableAfterCommitments / weeklyVelocity
        riskHorizon     = supplierLeadTimeWeeks + SAFETY_WEEKS
        stockoutRisk    = clamp(1 - coverageWeeks / riskHorizon, 0, 1)
        marginPerRupee  = (sellingPrice - currentSupplierCost) / currentSupplierCost

        priority        = stockoutRisk * marginPerRupee

    stockoutRisk already carries sales velocity, current stock and supplier
    lead time, which is why they do not appear again as separate multipliers.
    Multiplying by marginPerRupee ranks by return on the cash actually spent,
    so a cheap fast-moving item can outrank an expensive one.

    Dead stock has zero velocity, therefore infinite coverage, therefore zero
    risk and zero priority - it is never restocked.

Both tiers allow partial fills: if the budget cannot cover a whole line, it
buys the units it can afford and reports the line as PARTIAL.

Tier 2 is funded only when EVERY Tier 1 line is bought in full. A live
evaluation found that at Rs 12,947.99 - one paisa short of the Rs 12,948
the commitments cost - the second Finolex coil for a customer went unbought
and the Rs 6,299.99 left over was spent on discretionary restock. Money is
never spent on the shelf while a customer promise is short, by any amount.
Affordability is counted in whole paise, so a one-paisa shortfall is a
shortfall and not a rounding error.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from decimal import ROUND_HALF_UP, Decimal
from typing import Dict, List, Optional

from .models import ALL_OR_NOTHING, Dataset, money
from .pricing import current_cost, margin_per_rupee
from .shortage import committed_demand, shortages, uncommitted_stock
from .velocity import INFINITE_COVERAGE, coverage_weeks, velocity_for

# Weeks of buffer stock held on top of the supplier lead time.
SAFETY_WEEKS = 2.0
# Weeks of forward demand a restock aims to cover, beyond lead time + safety.
REORDER_COVER_WEEKS = 4.0
# Float tolerance for rupee comparisons.
EPSILON = 1e-6


def _paise(rupees: float) -> int:
    """Rupees as a whole number of paise, rounded half-up."""
    return int((Decimal(str(rupees)) * 100).quantize(Decimal("1"),
                                                     rounding=ROUND_HALF_UP))


def _affordable(requested: int, remaining_paise: int, unit_cost: float) -> int:
    """Whole units the remaining paise can buy, never more than requested."""
    unit = _paise(unit_cost)
    if unit <= 0:
        return requested
    return max(0, min(requested, remaining_paise // unit))

BUY = "BUY"
PARTIAL = "PARTIAL"
DEFER = "DEFER"

TIER1 = "COMMITTED_ORDER"
TIER2 = "RESTOCK"


@dataclass
class PlanLine:
    skuId: str
    tier: str
    decision: str
    requestedQty: int
    fundedQty: int
    unitCost: float
    lineCost: float
    reason: str
    risk: Optional[str]
    evidence: Dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        return {
            "skuId": self.skuId,
            "tier": self.tier,
            "decision": self.decision,
            "requestedQty": self.requestedQty,
            "fundedQty": self.fundedQty,
            "unitCost": self.unitCost,
            "lineCost": self.lineCost,
            "reason": self.reason,
            "risk": self.risk,
            "evidence": self.evidence,
        }


@dataclass
class BudgetPlan:
    budget: float
    lines: List[PlanLine]

    @property
    def purchased(self) -> List[PlanLine]:
        return [l for l in self.lines if l.fundedQty > 0]

    @property
    def deferred(self) -> List[PlanLine]:
        return [l for l in self.lines if l.decision in (DEFER, PARTIAL)]

    @property
    def tier1Spend(self) -> float:
        return money(sum(l.lineCost for l in self.lines if l.tier == TIER1))

    @property
    def tier2Spend(self) -> float:
        return money(sum(l.lineCost for l in self.lines if l.tier == TIER2))

    @property
    def totalSpend(self) -> float:
        return money(sum(l.lineCost for l in self.lines))

    @property
    def remaining(self) -> float:
        return money(self.budget - self.totalSpend)

    @property
    def allCommitmentsFunded(self) -> bool:
        return all(
            l.decision == BUY for l in self.lines if l.tier == TIER1
        )

    def merged_lines(self) -> List[dict]:
        """One row per SKU for display, with the tier split kept underneath.

        A SKU can legitimately be funded twice - once to honour a customer
        order and again to restock the shelf - which reads as a duplicate on
        screen. The UI shows the combined quantity; the per-tier allocations
        remain intact here so the decision stays auditable.
        """
        order: List[str] = []
        grouped: Dict[str, List[PlanLine]] = {}
        for line in self.lines:
            if line.skuId not in grouped:
                grouped[line.skuId] = []
                order.append(line.skuId)
            grouped[line.skuId].append(line)

        merged = []
        for skuId in order:
            parts = grouped[skuId]
            merged.append({
                "skuId": skuId,
                "totalFundedQty": sum(p.fundedQty for p in parts),
                "totalRequestedQty": sum(p.requestedQty for p in parts),
                "totalCost": money(sum(p.lineCost for p in parts)),
                "tiers": [p.as_dict() for p in parts],
            })
        return merged

    def as_dict(self) -> dict:
        return {
            "budget": money(self.budget),
            "tier1Spend": self.tier1Spend,
            "tier2Spend": self.tier2Spend,
            "totalSpend": self.totalSpend,
            "remaining": self.remaining,
            "allCommitmentsFunded": self.allCommitmentsFunded,
            "purchased": [l.as_dict() for l in self.purchased],
            "deferred": [l.as_dict() for l in self.deferred],
            "lines": [l.as_dict() for l in self.lines],
            "mergedLines": self.merged_lines(),
        }


@dataclass(frozen=True)
class RestockCandidate:
    skuId: str
    reorderQty: int
    unitCost: float
    weeklyVelocity: float
    availableAfterCommitments: int
    coverageWeeks: float
    leadTimeDays: int
    riskHorizonWeeks: float
    stockoutRisk: float
    marginPerRupee: float
    priority: float

    def as_evidence(self) -> dict:
        return {
            "skuId": self.skuId,
            "reorderQty": self.reorderQty,
            "unitCost": self.unitCost,
            "weeklyVelocity": round(self.weeklyVelocity, 2),
            "availableAfterCommitments": self.availableAfterCommitments,
            "coverageWeeks": (
                None
                if self.coverageWeeks == INFINITE_COVERAGE
                else round(self.coverageWeeks, 2)
            ),
            "leadTimeDays": self.leadTimeDays,
            "riskHorizonWeeks": round(self.riskHorizonWeeks, 2),
            "stockoutRisk": round(self.stockoutRisk, 4),
            "marginPerRupee": round(self.marginPerRupee, 4),
            "priority": round(self.priority, 6),
            "formula": "priority = stockoutRisk * marginPerRupee",
        }


def _clamp01(x: float) -> float:
    return max(0.0, min(1.0, x))


def restock_candidates(data: Dataset) -> List[RestockCandidate]:
    """Every SKU that is below its reorder point, ranked by priority."""
    demand = committed_demand(data.committedOrders())
    candidates: List[RestockCandidate] = []

    for skuId in sorted(data.products):
        velocity = velocity_for(data, skuId).weeklyVelocity
        if velocity <= 0:
            continue  # dead stock - never urgent

        supplier = data.supplierFor(skuId)
        lead_weeks = supplier.leadTimeWeeks
        risk_horizon = lead_weeks + SAFETY_WEEKS
        if risk_horizon <= 0:
            continue

        available = uncommitted_stock(data, skuId, demand)
        cover = coverage_weeks(available, velocity)
        risk = _clamp01(1.0 - cover / risk_horizon)
        if risk <= 0:
            continue  # comfortably stocked

        target = velocity * (lead_weeks + SAFETY_WEEKS + REORDER_COVER_WEEKS)
        reorder_qty = int(math.ceil(target - available))
        if reorder_qty <= 0:
            continue

        unit_cost = current_cost(data, skuId)
        if unit_cost <= 0:
            continue

        mpr = margin_per_rupee(data, skuId)
        candidates.append(
            RestockCandidate(
                skuId=skuId,
                reorderQty=reorder_qty,
                unitCost=unit_cost,
                weeklyVelocity=velocity,
                availableAfterCommitments=available,
                coverageWeeks=cover,
                leadTimeDays=supplier.leadTimeDays,
                riskHorizonWeeks=risk_horizon,
                stockoutRisk=risk,
                marginPerRupee=mpr,
                priority=risk * mpr,
            )
        )

    # Descending priority, skuId as a deterministic tie-break.
    return sorted(candidates, key=lambda c: (-c.priority, c.skuId))


def _deferral_risk(c: RestockCandidate) -> str:
    cover = c.coverageWeeks
    lead_weeks = c.leadTimeDays / 7.0
    if cover == INFINITE_COVERAGE:
        return "No stockout risk - this product has no recent sales."
    base = (
        f"Stock covers about {cover:.1f} weeks at {c.weeklyVelocity:.1f} units/week; "
        f"supplier lead time is {c.leadTimeDays} days."
    )
    if cover < lead_weeks:
        return base + " Stock will run out before a replacement order could arrive."
    return base


def allocate_budget(data: Dataset, budget: float) -> BudgetPlan:
    """Split `budget` across committed shortages, then discretionary restocking."""
    if budget < 0:
        raise ValueError("budget must not be negative")

    lines: List[PlanLine] = []
    remaining = float(budget)
    remaining_paise = _paise(budget)

    # ---- Tier 1: committed customer orders ----
    short_list = [s for s in shortages(data) if s.isShort]
    short_list.sort(key=lambda s: (s.earliestPromisedDate, s.skuId))

    for s in short_list:
        unit_cost = current_cost(data, s.skuId)
        requested = s.shortageQty
        full_cost = unit_cost * requested

        affordable = _affordable(requested, remaining_paise, unit_cost)

        # A product that cannot be usefully part-delivered is funded in full or
        # not at all - buying half a matched set helps nobody.
        policy = data.product(s.skuId).fulfilmentPolicy
        if policy == ALL_OR_NOTHING and affordable < requested:
            affordable = 0

        line_cost = money(unit_cost * affordable)
        evidence = {
            **s.as_evidence(),
            "unitCost": unit_cost,
            "fullLineCost": money(full_cost),
            "budgetRemainingBefore": money(remaining),
            "fulfilmentPolicy": policy,
            "policy": "Tier 1 - committed customer orders are funded first, "
                      "earliest promised date first.",
        }

        if affordable == requested:
            decision, risk = BUY, None
            reason = (
                f"Committed order needs {s.committedQty} units, {s.onHand} in stock. "
                f"Buying the {requested} unit shortfall."
            )
        elif affordable > 0:
            decision = PARTIAL
            reason = (
                f"Budget covers only {affordable} of the {requested} units needed "
                f"for the committed order."
            )
            risk = (
                f"{requested - affordable} units still short - the promised order "
                f"for {s.committedQty} units cannot be fully delivered."
            )
        else:
            decision = DEFER
            if policy == ALL_OR_NOTHING:
                reason = (
                    f"Budget cannot cover all {requested} units, and this product "
                    f"cannot be part-delivered."
                )
            else:
                reason = "Budget exhausted before this committed shortage could be funded."
            risk = (
                f"All {requested} units still short - the promised order for "
                f"{s.committedQty} units cannot be delivered."
            )

        remaining_paise -= _paise(unit_cost) * affordable
        remaining = remaining_paise / 100
        lines.append(
            PlanLine(
                skuId=s.skuId,
                tier=TIER1,
                decision=decision,
                requestedQty=requested,
                fundedQty=affordable,
                unitCost=unit_cost,
                lineCost=line_cost,
                reason=reason,
                risk=risk,
                evidence=evidence,
            )
        )

    # ---- Tier 2: discretionary restocking ----
    # Only once every customer commitment is bought in full. While any is
    # short, the money that is left belongs to that commitment, and no
    # restock is bought - it is deferred, and says why.
    commitments_short = any(l.decision != BUY for l in lines)
    for c in restock_candidates(data):
        requested = c.reorderQty
        affordable = (0 if commitments_short
                      else _affordable(requested, remaining_paise, c.unitCost))
        line_cost = money(c.unitCost * affordable)

        evidence = {
            **c.as_evidence(),
            "fullLineCost": money(c.unitCost * requested),
            "budgetRemainingBefore": money(remaining),
            "policy": "Tier 2 - remaining budget allocated by "
                      "priority = stockoutRisk * marginPerRupee, highest first.",
        }

        if affordable == requested:
            decision, risk = BUY, None
            reason = (
                f"Restock: {c.availableAfterCommitments} units uncommitted cover "
                f"{c.coverageWeeks:.1f} weeks at {c.weeklyVelocity:.1f}/week, "
                f"below the {c.riskHorizonWeeks:.1f} week reorder point."
            )
        elif affordable > 0:
            decision = PARTIAL
            reason = (
                f"Partial restock: budget covers {affordable} of {requested} units."
            )
            risk = _deferral_risk(c)
        elif commitments_short:
            decision = DEFER
            reason = ("Deferred: a customer commitment is not fully funded, so "
                      "no discretionary restock is bought.")
            risk = _deferral_risk(c)
        else:
            decision = DEFER
            reason = "Deferred: remaining budget cannot fund this restock."
            risk = _deferral_risk(c)

        remaining_paise -= _paise(c.unitCost) * affordable
        remaining = remaining_paise / 100
        lines.append(
            PlanLine(
                skuId=c.skuId,
                tier=TIER2,
                decision=decision,
                requestedQty=requested,
                fundedQty=affordable,
                unitCost=c.unitCost,
                lineCost=line_cost,
                reason=reason,
                risk=risk,
                evidence=evidence,
            )
        )

    return BudgetPlan(budget=money(budget), lines=lines)
