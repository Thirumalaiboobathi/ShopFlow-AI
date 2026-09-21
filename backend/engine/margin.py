"""Margin protection: what a confirmed supplier increase does to the shop's profit.

WHY THIS EXISTS
---------------
Stage 5.1 gave the shop a durable record of what it pays. That answers "my cost
went up" but not the question the owner actually asks next, which is "so am I
still making anything on it?". A supplier increase of 6.78% on a coil that
carried a 10.71% margin removes more than half the profit on every coil sold,
and nothing in the workflow said so.

This module says so. It is the third job in the product:

    SELL      messy order      -> verified quotation
    PROTECT   price change     -> margin protection      <- here
    BUY       limited cash     -> prioritised purchase plan

WHAT IT DOES NOT DO
-------------------
It changes nothing. It is a read-only view over three numbers the shop already
holds, and it writes to no store, mutates no dataset and touches no allocation.
In particular `Product.sellingPrice` is never assigned anywhere in this file -
`suggested_selling_price` returns a number for a human to look at, and that is
the entire extent of it. Repricing the shelf stays a decision the owner makes
deliberately, on screen, in a step this module does not implement.

It also does not re-derive any of its inputs. The selling price comes from the
catalogue, the previous supplier cost from `pricing.current_cost` over the
un-repriced dataset, and the confirmed cost from the shop's confirmed-cost
records. There is no fourth source, and no inference: a cost that is not
recorded produces an explicit unavailable state rather than a guess.

NUMERIC CONVENTION
------------------
Rupee amounts go through `models.money`, the rounding every other engine
module already uses, and percentages through `round(x, 2)`, matching
`pricing.PriceDelta.percentChange`. Decimal appears only where this project
already uses it - at the DynamoDB boundary in the API handler - and nothing
here is persisted, so no float ever reaches a store through this path.
"""

from __future__ import annotations

from typing import Dict, Iterable, List, Optional

from .models import Dataset, money
from .pricing import current_cost

# The margin below which a line is called out as too thin to be worth stocking.
#
# Chosen from this catalogue rather than from a textbook: the narrowest margin
# anywhere in the seeded 147-SKU shop today is 10.71%, and the median is
# 21.88%. A 10% line therefore flags nothing in a healthy shop and lights up
# only when a supplier increase has genuinely eroded a product. It is a
# configurable warning threshold, not a pricing policy - every caller may pass
# its own, and no purchasing or quotation decision depends on it.
MARGIN_WARNING_PERCENT = 10.0

# Status of one product's margin.
HEALTHY = "HEALTHY"                  # nothing to report
MARGIN_REDUCED = "MARGIN_REDUCED"    # a confirmed increase cut the margin
LOW_MARGIN = "LOW_MARGIN"            # margin is below the warning threshold
NEGATIVE_MARGIN = "NEGATIVE_MARGIN"  # the shop now pays more than it charges
UNAVAILABLE = "UNAVAILABLE"          # not enough recorded data to say anything

# Why no margin could be reported.
NO_SUCH_SKU = "NO_SUCH_SKU"
NO_SELLING_PRICE = "NO_SELLING_PRICE"
NO_PREVIOUS_COST = "NO_PREVIOUS_COST"
# Not a failure: the margin IS reported, but there is no confirmed increase to
# compare it against, so the before/after half of the view is absent.
NO_CONFIRMED_COST = "NO_CONFIRMED_COST"

SELLING_PRICE_NOTE = (
    "ShopFlow does not automatically change your selling price."
)
SUGGESTION_NOTE = (
    "Review and confirm before applying any price change."
)


def _finite(value) -> Optional[float]:
    """A usable positive amount, or None. Never raises, never invents.

    Malformed input is the normal case here, not an exception: a cost may
    arrive from DynamoDB as a Decimal, from a request as a string, or not at
    all. Anything that is not a finite positive number is simply absent.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return None
    if amount != amount or amount in (float("inf"), float("-inf")):
        return None
    if amount <= 0:
        return None
    return amount


def previous_supplier_cost(data: Dataset, sku_id: str) -> Optional[float]:
    """What the shop paid before any confirmed increase, or None.

    `pricing.current_cost` is the single definition of "what this SKU costs
    from its own supplier" and is reused unchanged. Called with the ORIGINAL
    dataset - not one already repriced by `purchasing.apply_confirmed_costs` -
    it is the before figure by construction.
    """
    if sku_id not in data.products:
        return None
    return _finite(current_cost(data, sku_id))


def _percent(amount: float, selling_price: float) -> float:
    """Margin as a percentage of the selling price."""
    return round(amount / selling_price * 100.0, 2)


def _unavailable(sku_id: str, reason: str, *, name: str = "") -> dict:
    return {
        "skuId": sku_id,
        "productName": name,
        "available": False,
        "comparisonAvailable": False,
        "status": UNAVAILABLE,
        "unavailableReason": reason,
        "sellingPrice": None,
        "previousSupplierCost": None,
        "confirmedSupplierCost": None,
        "oldMarginAmount": None,
        "newMarginAmount": None,
        "oldMarginPercent": None,
        "newMarginPercent": None,
        "marginReductionAmount": None,
        "marginReductionPercent": None,
        "suggestedSellingPrice": None,
        "sellingPriceChanged": False,
        "note": SELLING_PRICE_NOTE,
    }


def suggested_selling_price(
    confirmed_cost: float, old_margin_percent: float
) -> Optional[float]:
    """The price that would restore the margin percentage the shop used to earn.

        target = confirmedCost / (1 - oldMarginPercent / 100)

    Returned only when that is a meaningful thing to compute. A product that
    was already being sold at or below cost has no healthy margin to restore,
    and extrapolating one would be inventing a commercial position the shop
    never held.

    `old_margin_percent` is the REPORTED percentage, already rounded to two
    places. Using it rather than the unrounded ratio keeps the panel checkable:
    an owner who reaches for a calculator and divides the confirmed cost by
    (1 - the percentage shown) gets the price shown. The cost of that choice is
    a few paise of imprecision in the restored margin, which is well inside the
    rounding a shop prices at anyway.

    This is a recommendation and nothing else. No caller applies it, and
    `Product.sellingPrice` is never written from it.
    """
    cost = _finite(confirmed_cost)
    if cost is None or old_margin_percent is None:
        return None
    if old_margin_percent <= 0 or old_margin_percent >= 100:
        return None
    return money(cost / (1.0 - old_margin_percent / 100.0))


def margin_view(
    data: Dataset,
    sku_id: str,
    confirmed_cost=None,
    *,
    warning_percent: float = MARGIN_WARNING_PERCENT,
) -> dict:
    """One product's margin, before and after a confirmed supplier cost.

    Three inputs, each from its existing owner and none of them duplicated
    here: the catalogue selling price, the previous supplier cost, and the
    confirmed supplier cost the caller passes in.

    With no confirmed cost the current margin is still reported - it is a fact
    about the shop today - but nothing is compared and no suggestion is made,
    because there is no change to respond to.
    """
    if sku_id not in data.products:
        return _unavailable(sku_id, NO_SUCH_SKU)

    product = data.product(sku_id)
    selling = _finite(product.sellingPrice)
    if selling is None:
        return _unavailable(sku_id, NO_SELLING_PRICE, name=product.name)

    previous = previous_supplier_cost(data, sku_id)
    if previous is None:
        # Deliberately not inferred from the catalogue cost or from the
        # confirmed price. An unknown before-figure makes every comparison
        # below meaningless, and a plausible-looking one would be worse than
        # none at all.
        return _unavailable(sku_id, NO_PREVIOUS_COST, name=product.name)

    old_amount = money(selling - previous)
    old_percent = _percent(old_amount, selling)

    view = {
        "skuId": sku_id,
        "productName": product.name,
        "unit": product.unit,
        "available": True,
        "sellingPrice": money(selling),
        "previousSupplierCost": money(previous),
        "oldMarginAmount": old_amount,
        "oldMarginPercent": old_percent,
        "marginWarningPercent": round(float(warning_percent), 2),
        # Stated on every view, and asserted in the tests, because the whole
        # feature is only safe if this stays true.
        "sellingPriceChanged": False,
        "note": SELLING_PRICE_NOTE,
    }

    confirmed = _finite(confirmed_cost)
    if confirmed is None:
        view.update({
            "comparisonAvailable": False,
            "unavailableReason": NO_CONFIRMED_COST,
            "confirmedSupplierCost": None,
            "newMarginAmount": None,
            "newMarginPercent": None,
            "marginReductionAmount": None,
            "marginReductionPercent": None,
            "suggestedSellingPrice": None,
            "status": _status(old_amount, old_percent, None, warning_percent),
            "evidence": {
                "oldMargin": f"{view['sellingPrice']} - {view['previousSupplierCost']} "
                             f"= {old_amount}",
                "oldMarginPercent": f"{old_amount} / {view['sellingPrice']} x 100 "
                                    f"= {old_percent}%",
                "comparison": "No confirmed supplier price change for this product.",
                "source": "engine.margin.margin_view",
            },
        })
        return view

    new_amount = money(selling - confirmed)
    new_percent = _percent(new_amount, selling)
    # Subtracting the ROUNDED percentages, so the three figures the owner is
    # shown always agree with each other on screen. Subtracting the unrounded
    # ratios can leave a panel reading 10.71 - 4.66 = 6.06.
    reduction_percent = round(old_percent - new_percent, 2)
    reduction_amount = money(old_amount - new_amount)

    view.update({
        "comparisonAvailable": True,
        "confirmedSupplierCost": money(confirmed),
        "newMarginAmount": new_amount,
        "newMarginPercent": new_percent,
        "marginReductionAmount": reduction_amount,
        "marginReductionPercent": reduction_percent,
        "status": _status(new_amount, new_percent, reduction_amount, warning_percent),
        "suggestedSellingPrice": (
            suggested_selling_price(confirmed, old_percent)
            if reduction_amount > 0 else None
        ),
        "suggestionNote": SUGGESTION_NOTE,
        "evidence": {
            "oldMargin": f"{view['sellingPrice']} - {view['previousSupplierCost']} "
                         f"= {old_amount}",
            "newMargin": f"{view['sellingPrice']} - {money(confirmed)} = {new_amount}",
            "oldMarginPercent": f"{old_amount} / {view['sellingPrice']} x 100 "
                                f"= {old_percent}%",
            "newMarginPercent": f"{new_amount} / {view['sellingPrice']} x 100 "
                                f"= {new_percent}%",
            "reduction": f"{old_amount} - {new_amount} = {reduction_amount}",
            "threshold": f"warning below {round(float(warning_percent), 2)}% margin",
            "source": "engine.margin.margin_view",
        },
    })
    return view


def _status(
    amount: float,
    percent: float,
    reduction: Optional[float],
    warning_percent: float,
) -> str:
    """Deterministic classification of the margin now being earned.

    Order matters and reflects severity: losing money on every sale outranks a
    thin margin, which outranks a margin that merely fell. A price DECREASE
    leaves reduction negative and is never reported as a reduction.
    """
    if amount < 0:
        return NEGATIVE_MARGIN
    if percent < warning_percent:
        return LOW_MARGIN
    if reduction is not None and reduction > 0:
        return MARGIN_REDUCED
    return HEALTHY


# Statuses that the owner should be shown rather than left to find.
WARNING_STATUSES = (MARGIN_REDUCED, LOW_MARGIN, NEGATIVE_MARGIN)


def margin_alerts(
    data: Dataset,
    confirmed_costs: Dict[str, float],
    *,
    warning_percent: float = MARGIN_WARNING_PERCENT,
) -> List[dict]:
    """A margin view for every SKU with a confirmed supplier cost.

    Ordered by how much margin was lost, largest first, so the product that
    hurts most is the one the owner reads. Unavailable views are kept rather
    than dropped: "we cannot tell you the margin on this" is information, and
    silently omitting a product would look like an all-clear.
    """
    views = [
        margin_view(data, sku_id, cost, warning_percent=warning_percent)
        for sku_id, cost in sorted((confirmed_costs or {}).items())
    ]
    return sorted(
        views,
        key=lambda v: (v.get("marginReductionAmount") or 0.0),
        reverse=True,
    )


def quotation_margin_impact(quote: dict, alerts: Iterable[dict]) -> dict:
    """Which confirmed price changes touch the quotation on screen.

    A join, not a calculation. Every figure in the result is carried straight
    from the margin views passed in; the quotation supplies only the set of
    SKUs it contains.

    The quotation total is deliberately absent from the output. A supplier
    cost change does not reprice a quotation the customer was already given,
    and returning the total here would invite a UI that redisplays it as
    though something about it had changed.
    """
    sku_ids = {
        line.get("skuId")
        for line in (quote or {}).get("lines", [])
        if line.get("skuId")
    }
    affected = [
        view for view in (alerts or [])
        if view.get("skuId") in sku_ids and view.get("comparisonAvailable")
    ]
    return {
        "affected": affected,
        "affectedCount": len(affected),
        "quotationTotalChanged": False,
        "note": (
            "Supplier price change affects this quotation."
            if affected else
            "No confirmed supplier price change affects this quotation."
        ),
        "explanation": (
            "The quotation keeps the selling prices it was priced at. This "
            "shows what the change did to your margin, not to the customer's "
            "total."
        ),
    }
