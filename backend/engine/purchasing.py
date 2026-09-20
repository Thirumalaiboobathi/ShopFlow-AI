"""Cash-constrained purchasing: the owner-facing view of the budget allocator.

Stage 5 adds no new decision logic. The allocation is still `budget.allocate_budget`,
which was written in Stage 1 and has not changed. What this module adds is the
two things that allocator deliberately left out:

1.  CONFIRMED SUPPLIER COSTS.
    `supplier_prices.build_decision_record` records an owner's ruling on a
    detected price change and explicitly does NOT rewrite the catalogue, noting
    that applying it to purchasing is "a separate, later step that they trigger
    knowingly". Asking for a purchase plan is that step. `apply_confirmed_costs`
    folds confirmed changes into a COPY of the dataset before planning, so the
    plan is priced at what the supplier will actually charge.

2.  PRESENTATION.
    The allocator emits SKU ids and raw figures. A shop owner needs product
    names, the stock position, and the arithmetic written out. Nothing here
    computes a new number - every figure is lifted from the allocator's own
    evidence.

SELLING PRICE vs SUPPLIER COST
------------------------------
These are two different numbers and this module never lets them merge:

    sellingPrice  - what the customer pays. Lives on the Product. A confirmed
                    supplier price change NEVER touches it; repricing the shelf
                    is a commercial decision, not an arithmetic consequence.
    unitCost      - what the shop pays the supplier. Comes from the supplier
                    price history, and is the only number the budget is spent in.

A purchase plan is denominated entirely in unitCost. sellingPrice appears only
inside marginPerRupee, where the gap between the two is the whole point.
"""

from __future__ import annotations

import copy
from typing import Dict, Iterable, List, Optional

from .budget import (
    DEFER,
    PARTIAL,
    TIER1,
    TIER2,
    BudgetPlan,
    allocate_budget,
)
from .cost_records import (
    CONFIRMED_SUPPLIER_PRICE,
    SEEDED_SUPPLIER_PRICE,
)
from .models import Dataset, SupplierPrice, money
from .pricing import current_cost

# What-if budgets offered beside the owner's own figure. Two points either side
# of the demo budget, enough to show the allocation genuinely moving without
# turning the UI into a general scenario explorer.
WHATIF_BUDGETS = (20000.0, 30000.0)

# An owner's ruling that means "yes, that is the new price".
CONFIRMED = "CONFIRMED"

MAX_BUDGET = 10_000_000.0


class InvalidBudgetError(ValueError):
    """The budget is missing, negative, or not a number."""


def parse_budget(value) -> float:
    """Validate an owner-supplied budget.

    Zero is a legitimate answer - it means "I have no cash", and the planner
    should say so rather than error. Negative is not: there is no such thing as
    negative cash to allocate, and silently clamping it to zero would hide a
    caller's bug.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise InvalidBudgetError("budget must be a number")
    amount = float(value)
    if amount != amount or amount in (float("inf"), float("-inf")):
        raise InvalidBudgetError("budget must be a finite number")
    if amount < 0:
        raise InvalidBudgetError("budget must not be negative")
    if amount > MAX_BUDGET:
        raise InvalidBudgetError(f"budget must be {MAX_BUDGET:,.0f} or less")
    return money(amount)


def confirmed_cost_details(decisions: Iterable[Dict]) -> Dict[str, dict]:
    """Confirmed price changes, with where each one came from.

    Rejected changes are ignored on purpose: the owner saying "that is not the
    price I agreed" means the shop keeps planning at the cost it already knows.

    The provenance travels with the price so the plan can say which costs the
    owner agreed to and which are still the seeded default, rather than the UI
    guessing from the numbers.
    """
    details: Dict[str, dict] = {}
    for row in decisions or []:
        if not isinstance(row, dict):
            continue
        if str(row.get("decision") or "").upper() != CONFIRMED:
            continue
        sku_id = row.get("skuId")
        price = row.get("currentPrice")
        if not sku_id or price is None:
            continue
        try:
            value = float(price)
        except (TypeError, ValueError):
            continue
        if value <= 0:
            continue
        try:
            confirmed_at = int(row.get("confirmedAt") or 0)
        except (TypeError, ValueError):
            confirmed_at = 0
        details[str(sku_id)] = {
            "cost": money(value),
            "sourceJobId": row.get("sourceJobId") or "",
            "confirmedAt": confirmed_at,
        }
    return details


def confirmed_costs(decisions: Iterable[Dict]) -> Dict[str, float]:
    """Just the prices, for callers that do not need the provenance."""
    return {sku: d["cost"] for sku, d in confirmed_cost_details(decisions).items()}


def apply_confirmed_costs(data: Dataset, costs: Dict[str, float]) -> Dataset:
    """Return a copy of `data` priced at the owner's confirmed supplier costs.

    The confirmed price is appended to the SKU's supplier price history rather
    than overwriting anything. Two reasons: `current_cost` already reads the
    latest entry from the SKU's own supplier, so every downstream figure -
    margin, priority, line cost - picks the change up with no further plumbing;
    and the prior price survives, so the evidence can still show what changed.

    The input dataset is never mutated - a plan request must not alter the
    shop's seeded record - and `Product.sellingPrice` is not touched at all.
    """
    if not costs:
        return data

    updated = copy.copy(data)
    updated.priceHistory = dict(data.priceHistory)

    for sku_id, price in costs.items():
        if sku_id not in data.products:
            continue  # an unknown SKU cannot reprice anything
        product = data.products[sku_id]
        history = list(data.prices(sku_id))
        if history and money(history[-1].unitCost) == money(price):
            continue  # already the live cost, nothing to append

        # Dated after every known entry so it sorts last and wins.
        latest_date = max((p.effectiveDate for p in history), default="")
        effective = _day_after(latest_date) if latest_date else "9999-12-31"
        history.append(
            SupplierPrice(
                skuId=sku_id,
                supplierId=product.supplierId,
                effectiveDate=effective,
                unitCost=money(price),
            )
        )
        updated.priceHistory[sku_id] = history

    return updated


def _day_after(iso_date: str) -> str:
    from datetime import date, timedelta

    try:
        parsed = date.fromisoformat(iso_date)
    except ValueError:
        return "9999-12-31"
    return (parsed + timedelta(days=1)).isoformat()


def _line_view(data: Dataset, line, baseline: Dataset,
               confirmed: Dict[str, dict]) -> dict:
    """One allocator line, enriched for display. Adds no arithmetic."""
    product = data.product(line.skuId)
    evidence = dict(line.evidence)

    view = {
        "skuId": line.skuId,
        "productName": product.name,
        "tier": line.tier,
        "decision": line.decision,
        "selected": line.fundedQty > 0,
        "requestedQty": line.requestedQty,
        "fundedQty": line.fundedQty,
        # The budget is spent in supplier cost, never in selling price.
        "unitCost": line.unitCost,
        "lineCost": line.lineCost,
        "fullLineCost": evidence.get("fullLineCost"),
        # Carried alongside purely so the UI can show they are different
        # numbers. It is not part of any allocation arithmetic.
        "sellingPrice": product.sellingPrice,
        "currentStock": data.onHand(line.skuId),
        "reason": line.reason,
        "risk": line.risk,
        "evidence": evidence,
    }

    if line.tier == TIER2:
        view.update({
            "weeklyVelocity": evidence.get("weeklyVelocity"),
            "availableAfterCommitments": evidence.get("availableAfterCommitments"),
            "coverageWeeks": evidence.get("coverageWeeks"),
            "stockoutRisk": evidence.get("stockoutRisk"),
            "marginPerRupee": evidence.get("marginPerRupee"),
            "priority": evidence.get("priority"),
            "priorityCalculation": _priority_calculation(evidence),
        })
    else:
        view.update({
            "committedQty": evidence.get("committedQty"),
            "shortageQty": evidence.get("shortageQty"),
            "earliestPromisedDate": evidence.get("earliestPromisedDate"),
            "fulfilmentPolicy": evidence.get("fulfilmentPolicy"),
        })

    # Where this purchase cost came from. Never inferred from the figures -
    # a confirmed price that happens to equal the seeded one is still
    # confirmed, and a plan must not claim otherwise.
    entry = confirmed.get(line.skuId)
    if entry:
        view["costSource"] = CONFIRMED_SUPPLIER_PRICE
        view["costProvenance"] = {
            "source": CONFIRMED_SUPPLIER_PRICE,
            "sourceJobId": entry.get("sourceJobId") or None,
            "confirmedAt": entry.get("confirmedAt") or None,
            "note": "You confirmed this supplier price.",
        }
    else:
        view["costSource"] = SEEDED_SUPPLIER_PRICE
        view["costProvenance"] = {
            "source": SEEDED_SUPPLIER_PRICE,
            "sourceJobId": None,
            "confirmedAt": None,
            "note": "The shop's existing supplier cost - no newer price confirmed.",
        }

    baseline_cost = current_cost(baseline, line.skuId)
    if money(baseline_cost) != money(line.unitCost):
        view["costBasis"] = {
            "previousCost": money(baseline_cost),
            "confirmedCost": line.unitCost,
            "note": "Priced at the supplier increase the owner confirmed.",
        }
    return view


def _priority_calculation(evidence: Dict) -> Optional[str]:
    """The ranking arithmetic written out, using the allocator's own figures."""
    risk = evidence.get("stockoutRisk")
    mpr = evidence.get("marginPerRupee")
    priority = evidence.get("priority")
    if risk is None or mpr is None or priority is None:
        return None
    return f"priority = {risk} x {mpr} = {priority}"


def budget_is_binding(plan: BudgetPlan) -> bool:
    """True when the budget forces a genuine tradeoff.

    Both halves matter. If commitments are unfunded the shop is failing
    customers, and if nothing is turned down the cash was never actually
    constrained - a plan that buys everything is not a decision.
    """
    if not plan.allCommitmentsFunded:
        return False
    return any(
        l.tier == TIER2 and l.decision in (DEFER, PARTIAL) for l in plan.lines
    )


def build_purchase_plan(
    data: Dataset,
    budget: float,
    decisions: Optional[Iterable[Dict]] = None,
) -> dict:
    """The owner-facing purchase plan for one budget.

    `decisions` are owner rulings from a supplier price review; confirmed ones
    reprice the plan. Everything else is the Stage 1 allocator unchanged.
    """
    amount = parse_budget(budget)
    details = confirmed_cost_details(decisions or [])
    costs = {sku: d["cost"] for sku, d in details.items()}
    priced = apply_confirmed_costs(data, costs)

    plan = allocate_budget(priced, amount)
    lines = [_line_view(priced, l, data, details) for l in plan.lines]

    commitments = [l for l in lines if l["tier"] == TIER1]
    restock = [l for l in lines if l["tier"] == TIER2]
    selected = [l for l in restock if l["selected"]]
    deferred = [l for l in restock if not l["selected"]]

    commitment_full_cost = money(
        sum(l["fullLineCost"] or 0.0 for l in commitments)
    )

    return {
        "budget": amount,
        "commitments": commitments,
        "commitmentCost": plan.tier1Spend,
        "commitmentFullCost": commitment_full_cost,
        "allCommitmentsFunded": plan.allCommitmentsFunded,
        "budgetAfterCommitments": money(amount - plan.tier1Spend),
        "restockSelected": selected,
        "restockDeferred": deferred,
        "restockCost": plan.tier2Spend,
        "totalSpend": plan.totalSpend,
        "remaining": plan.remaining,
        "budgetIsBinding": budget_is_binding(plan),
        "confirmedCosts": [
            {
                "skuId": sku,
                "productName": data.product(sku).name,
                "previousCost": money(current_cost(data, sku)),
                "confirmedCost": details[sku]["cost"],
                "sourceJobId": details[sku]["sourceJobId"] or None,
                "confirmedAt": details[sku]["confirmedAt"] or None,
                "source": CONFIRMED_SUPPLIER_PRICE,
            }
            for sku in sorted(details)
            if sku in data.products
        ],
        "counts": {
            "commitments": len(commitments),
            "restockSelected": len(selected),
            "restockDeferred": len(deferred),
        },
        "policy": {
            "tier1": "Committed customer orders are funded first, "
                     "earliest promised date first.",
            "tier2": "Remaining cash is allocated by "
                     "priority = stockoutRisk x marginPerRupee, highest first.",
            "costBasis": "All spend is in supplier purchase cost. Selling price "
                         "is never changed by a purchase plan.",
            "costSource": "Each line states whether its purchase cost is one "
                          "you confirmed or the shop's existing supplier cost.",
        },
    }


def what_if(
    data: Dataset,
    budget: float,
    decisions: Optional[Iterable[Dict]] = None,
    budgets: Iterable[float] = WHATIF_BUDGETS,
) -> List[dict]:
    """The same plan at two other budgets, summarised for comparison.

    Deliberately a fixed pair of totals rather than a general scenario engine:
    the question a shop owner actually asks is "what if I had a bit more, or a
    bit less", and two concrete answers settle it.
    """
    summaries = []
    for alternative in budgets:
        if money(alternative) == money(budget):
            continue
        plan = build_purchase_plan(data, alternative, decisions)
        summaries.append({
            "budget": plan["budget"],
            "commitmentCost": plan["commitmentCost"],
            "restockCost": plan["restockCost"],
            "totalSpend": plan["totalSpend"],
            "remaining": plan["remaining"],
            "allCommitmentsFunded": plan["allCommitmentsFunded"],
            "restockSelectedCount": plan["counts"]["restockSelected"],
            "restockDeferredCount": plan["counts"]["restockDeferred"],
            "budgetIsBinding": plan["budgetIsBinding"],
        })
    return summaries
