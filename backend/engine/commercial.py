"""Commercial intelligence: what a price change and a shortage cost the shop.

WHY THIS EXISTS
---------------
The engines already know each fact separately. `shortage` knows 3 coils are
promised and 1 is on the shelf; `margin` knows the coil's margin fell from
₹708 to ₹308; the planner knows what the committed coils cost to buy within
the cash the owner has. What the owner asks next is one question across all
three - "how much money is this, and do I buy now or wait?" - and the answer
has to keep three different kinds of money apart:

    purchase cash       money that leaves the till to buy the shortfall.
                        It comes back when the customer pays. Not a loss.
    price exposure      the extra the shortfall costs because the supplier
                        price moved: shortfall x (current - previous cost).
    margin impact       what that does to profit. With the selling price
                        unchanged it is the SAME rupees as the price exposure,
                        seen from the sales side - so it is reported beside
                        the exposure and never added to it.

Three read-only views, all deterministic:

    money_at_risk            the three figures above, for one SKU
    buy_now_vs_wait          the facts of both choices; the owner chooses
    compare_supplier_quotes  the owner's offers, costed against the need

WHAT IT DOES NOT DO
-------------------
It decides nothing and writes nothing. There is no recommendation field, no
"best" offer and no forecast: waiting is described at today's price, because
no data here says what a price will be tomorrow. Every input is an existing
engine's output - shortage from `engine.shortage`, the previous cost from
`engine.margin.previous_supplier_cost`, the margin from `engine.margin`, the
allocation from the purchase planner - and nothing is re-derived here.
"""

from __future__ import annotations

import math
from typing import Dict, Iterable, List, Optional

from .margin import margin_view, previous_supplier_cost
from .models import Dataset, money
from .negotiation import safe_supplier_label
from .purchasing import parse_budget
from .shortage import shortages
from .supplier_prices import (MAX_SUPPLIER_UNIT_PRICE, PLAUSIBLE_MAX_RATIO,
                              PLAUSIBLE_MIN_RATIO)
from .whatif import DEFAULT_BUDGET, _plan

SOURCE = "engine.commercial"

AVAILABLE = "AVAILABLE"
UNAVAILABLE = "UNAVAILABLE"
UNKNOWN_SKU = "UNKNOWN_SKU"
NO_PREVIOUS_COST = "NO_PREVIOUS_COST"
INVALID_CONFIRMED_COST = "INVALID_CONFIRMED_COST"

# What a shortfall is, for buy-now-vs-wait.
SHORTAGE = "SHORTAGE"              # committed units are not on the shelf
NO_SHORTAGE = "NO_SHORTAGE"        # committed units are all in stock
NO_COMMITMENT = "NO_COMMITMENT"    # no customer has been promised this SKU

# Supplier quote comparison. One primary status per offer, plus flags.
FITS_REQUIREMENT = "FITS_REQUIREMENT"
MOQ_BLOCKED = "MOQ_BLOCKED"                    # must buy more than is needed
INSUFFICIENT_QUANTITY = "INSUFFICIENT_QUANTITY"  # cannot supply what is needed
NO_REQUIREMENT = "NO_REQUIREMENT"              # nothing is needed to compare
HIGHER_TOTAL_COST = "HIGHER_TOTAL_COST"
BETTER_UNIT_PRICE = "BETTER_UNIT_PRICE"
REVIEW_REQUIRED = "REVIEW_REQUIRED"            # price far from what the shop pays

MAX_OFFERS = 10
MAX_OFFER_QUANTITY = 100_000

OWNER_DECISION_NOTE = ("ShopFlow shows the facts of each choice. You decide; "
                       "nothing is bought or ordered.")
NO_FORECAST_NOTE = ("ShopFlow does not forecast supplier prices. Waiting is "
                    "shown at today's confirmed price.")
NOT_ADDED_NOTE = ("The margin lost is the same money as the price increase on "
                  "those units, so the two are not added together. Purchase "
                  "cash is money spent to fulfil the order, not money lost.")


class InvalidOfferError(ValueError):
    """An offer that cannot be compared. The whole request is refused."""


def _cost(value) -> Optional[float]:
    """A usable unit cost, or None: finite, positive and below the ceiling
    every supplier price in ShopFlow is held to."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    amount = float(value)
    if not math.isfinite(amount) or amount <= 0 or amount > MAX_SUPPLIER_UNIT_PRICE:
        return None
    return amount


def _unavailable(sku_id: str, reason: str) -> dict:
    return {"status": UNAVAILABLE, "skuId": sku_id, "reason": reason,
            "stateChanged": False, "source": SOURCE}


def _shortage(data: Dataset, sku_id: str) -> dict:
    """Committed, on hand and short for one SKU, from `engine.shortage`."""
    for entry in shortages(data):
        if entry.skuId == sku_id:
            return entry.as_evidence()
    return {"skuId": sku_id, "committedQty": 0, "onHand": data.onHand(sku_id),
            "shortageQty": 0, "earliestPromisedDate": ""}


def _costs(data: Dataset, sku_id: str, confirmed_costs) -> dict:
    """Previous and current unit cost, or the reason there are none.

    Current is the owner's confirmed cost when there is one, otherwise the
    previous cost - an unconfirmed document price never counts. A confirmed
    cost that is not a finite positive price is refused, not skipped.
    """
    previous = previous_supplier_cost(data, sku_id)
    if previous is None:
        return {"reason": NO_PREVIOUS_COST}
    confirmed = (confirmed_costs or {})
    if sku_id in confirmed:
        current = _cost(confirmed[sku_id])
        if current is None:
            return {"reason": INVALID_CONFIRMED_COST}
        return {"previous": money(previous), "current": money(current),
                "currentSource": "CONFIRMED_SUPPLIER_COST"}
    return {"previous": money(previous), "current": money(previous),
            "currentSource": "PREVIOUS_SUPPLIER_COST"}


# ---------------------------------------------------------------------------
# money at risk
# ---------------------------------------------------------------------------

def money_at_risk(data: Dataset, sku_id: str,
                  confirmed_costs: Optional[Dict[str, float]] = None) -> dict:
    """Purchase cash, price exposure and margin impact for one SKU's shortfall.

        purchaseQty            = shortageQty                (engine.shortage)
        purchaseCashRequired   = purchaseQty x currentCost
        unitPriceChange        = currentCost - previousCost
        supplierPriceExposure  = purchaseQty x max(0, unitPriceChange)
        marginImpact           = purchaseQty x marginReductionAmount (engine.margin)
        totalExposure          = supplierPriceExposure      (not + marginImpact)

    Only the shortfall is exposed: the units already on the shelf were bought
    at the old cost. A price fall is reported as a saving, never as negative
    exposure.
    """
    sku_id = str(sku_id or "")
    if sku_id not in data.products:
        return _unavailable(sku_id, UNKNOWN_SKU)
    costs = _costs(data, sku_id, confirmed_costs)
    if "reason" in costs:
        return _unavailable(sku_id, costs["reason"])

    short = _shortage(data, sku_id)
    qty = int(short["shortageQty"])
    previous, current = costs["previous"], costs["current"]
    change = money(current - previous)
    cash = money(qty * current)
    exposure = money(qty * max(0.0, change))
    saving = money(qty * max(0.0, -change))

    margin = margin_view(data, sku_id, current
                         if costs["currentSource"] == "CONFIRMED_SUPPLIER_COST"
                         else None)
    # Margin LOST on the shortfall. A price fall raises the margin; that is
    # reported as a saving above, never as a negative loss.
    reduction = max(0.0, margin.get("marginReductionAmount") or 0.0)
    margin_impact = money(qty * reduction)
    new_margin = (margin.get("newMarginAmount")
                  if margin.get("comparisonAvailable")
                  else margin.get("oldMarginAmount"))
    loss_per_unit = money(max(0.0, -(new_margin or 0.0)))

    product = data.product(sku_id)
    return {
        "status": AVAILABLE,
        "skuId": sku_id,
        "productName": product.name,
        "unit": product.unit,
        "committedQty": short["committedQty"],
        "onHand": short["onHand"],
        "shortageQty": qty,
        "purchaseQty": qty,
        "previousUnitCost": previous,
        "currentUnitCost": current,
        "currentCostSource": costs["currentSource"],
        "unitPriceChange": change,
        "purchaseCashRequired": cash,
        "supplierPriceExposure": exposure,
        "supplierPriceSaving": saving,
        "marginImpact": margin_impact,
        "marginPerUnitBefore": margin.get("oldMarginAmount"),
        "marginPerUnitNow": new_margin,
        "sellingPrice": margin.get("sellingPrice"),
        "sellingPriceChanged": False,
        "lossOnPurchase": money(qty * loss_per_unit),
        "totalExposure": exposure,
        "totalExposureDefinition": "supplierPriceExposure only",
        "explanation": _explain_risk(product.unit, qty, previous, current,
                                     change, cash, exposure, margin_impact),
        "notAddedTogether": NOT_ADDED_NOTE,
        "evidence": {
            "shortage": f"{short['committedQty']} committed - {short['onHand']} "
                        f"on hand = {qty} to buy",
            "purchaseCash": f"{qty} x {current} = {cash}",
            "priceExposure": f"{qty} x max(0, {current} - {previous}) = {exposure}",
            "marginImpact": f"{qty} x {money(reduction)} margin reduction = "
                            f"{margin_impact}",
            "sources": {
                "shortage": "engine.shortage.shortages",
                "previousUnitCost": "engine.margin.previous_supplier_cost",
                "currentUnitCost": ("confirmed supplier cost record"
                                    if costs["currentSource"] ==
                                    "CONFIRMED_SUPPLIER_COST"
                                    else "engine.margin.previous_supplier_cost"),
                "margin": "engine.margin.margin_view",
            },
        },
        "stateChanged": False,
        "source": SOURCE,
    }


def _explain_risk(unit, qty, previous, current, change, cash, exposure,
                  margin_impact) -> str:
    unit = str(unit or "unit")
    if qty == 0:
        return (f"No committed {unit} is short, so nothing has to be bought for "
                f"customer orders at ₹{current:,.2f}.")
    text = (f"{qty} committed {unit}(s) must be bought at ₹{current:,.2f}: "
            f"₹{cash:,.2f} of purchase cash.")
    if change > 0:
        text += (f" The supplier price rose ₹{change:,.2f} per {unit} from "
                 f"₹{previous:,.2f}, which adds ₹{exposure:,.2f} to that purchase"
                 f" and takes ₹{margin_impact:,.2f} off the margin on it.")
    elif change < 0:
        text += (f" The supplier price fell ₹{-change:,.2f} per {unit} from "
                 f"₹{previous:,.2f}.")
    return text


# ---------------------------------------------------------------------------
# buy now vs wait
# ---------------------------------------------------------------------------

def buy_now_vs_wait(data: Dataset, sku_id: str,
                    confirmed_costs: Optional[Dict[str, float]] = None, *,
                    budget=DEFAULT_BUDGET) -> dict:
    """Both choices for one SKU, as facts. The owner decides.

    BUY NOW prices the shortfall at today's cost and asks the purchase planner
    - commitments first, earliest promise first, at the confirmed costs - how
    many of those units the budget funds. WAIT spends nothing today and leaves
    the shortfall open. Neither side predicts a future price.
    """
    budget = parse_budget(budget)
    risk = money_at_risk(data, sku_id, confirmed_costs)
    if risk["status"] != AVAILABLE:
        return risk

    confirmed = {k: v for k, v in (confirmed_costs or {}).items()
                 if k in data.products and _cost(v) is not None}
    plan = _plan(data, budget, confirmed)
    line = next((c for c in plan["commitments"] if c["skuId"] == sku_id), None)
    funded = int(line["fundedQty"]) if line else 0
    planned_cost = money(line["lineCost"]) if line else 0.0
    qty, unit = risk["shortageQty"], risk["unit"]

    if risk["committedQty"] == 0:
        situation = NO_COMMITMENT
    elif qty == 0:
        situation = NO_SHORTAGE
    else:
        situation = SHORTAGE

    supplier = data.supplierFor(sku_id)
    buy_now = {
        "quantity": qty,
        "priceUsed": risk["currentUnitCost"],
        "cashRequired": risk["purchaseCashRequired"],
        "fundedQtyWithinBudget": min(funded, qty),
        "affordableWithinBudget": funded >= qty,
        "commitmentCovered": qty == 0 or funded >= qty,
        "marginPerUnit": risk["marginPerUnitNow"],
        "marginOnPurchase": money(qty * (risk["marginPerUnitNow"] or 0.0)),
        "supplierPriceExposure": risk["supplierPriceExposure"],
        "budgetLeftAfterAllCommitments": plan["budgetAfterCommitments"],
    }
    wait = {
        "cashRequiredNow": 0.0,
        "shortageRemaining": qty,
        "commitmentCovered": qty == 0,
        "currentPriceExposure": risk["supplierPriceExposure"],
        "purchasingCapacityPreserved": planned_cost,
        "earliestPromisedDate": _shortage(data, sku_id)["earliestPromisedDate"],
        "supplierLeadTimeDays": getattr(supplier, "leadTimeDays", None),
        "fulfilmentRisk": ("COMMITMENT_UNCOVERED" if qty > 0 else "NONE"),
    }
    return {
        "status": AVAILABLE,
        "situation": situation,
        "skuId": sku_id,
        "productName": risk["productName"],
        "unit": unit,
        "committedQty": risk["committedQty"],
        "onHand": risk["onHand"],
        "budget": money(budget),
        "buyNow": buy_now,
        "wait": wait,
        "facts": _facts(situation, risk, buy_now, wait, unit),
        "ownerDecisionRequired": True,
        "recommendation": None,
        "note": OWNER_DECISION_NOTE,
        "forecastNote": NO_FORECAST_NOTE,
        "sources": {
            "shortage": "engine.shortage.shortages",
            "cost": risk["evidence"]["sources"]["currentUnitCost"],
            "funding": "engine.purchasing.build_purchase_plan "
                       "(commitments first, earliest promise first)",
            "margin": "engine.margin.margin_view",
        },
        "stateChanged": False,
        "source": SOURCE,
    }


def _facts(situation, risk, buy_now, wait, unit) -> List[str]:
    price = f"₹{risk['currentUnitCost']:,.2f}"
    if situation == NO_COMMITMENT:
        return ["No customer order is waiting on this product. Restocking it is "
                "a purchase-plan decision, not a commitment."]
    if situation == NO_SHORTAGE:
        return [f"All {risk['committedQty']} committed {unit}(s) are in stock. "
                "Nothing needs to be bought for customer orders."]
    qty = risk["shortageQty"]
    facts = [f"Current supplier price is {price}.",
             f"Buying {qty} {unit}(s) now costs ₹{buy_now['cashRequired']:,.2f}."]
    if buy_now["affordableWithinBudget"]:
        facts.append("The budget funds it after earlier commitments, which covers "
                     "the committed order.")
    else:
        facts.append(f"Within the budget, after earlier commitments, "
                     f"{buy_now['fundedQtyWithinBudget']} of {qty} can be funded, "
                     "so buying now does not cover the whole order.")
    facts.append(f"Waiting leaves {qty} committed {unit}(s) uncovered.")
    if wait["supplierLeadTimeDays"] is not None:
        facts.append(f"The supplier's lead time is {wait['supplierLeadTimeDays']} "
                     "day(s).")
    return facts


# ---------------------------------------------------------------------------
# supplier quote comparison
# ---------------------------------------------------------------------------

# Figures an offer may not carry: each is worked out here.
OFFER_DERIVED_FIELDS = frozenset({
    "total", "totalCost", "purchaseCost", "purchaseQty", "requiredQty",
    "status", "flags", "rank", "best", "recommended", "score", "margin",
    "unitPriceDelta", "totalCostDelta", "excessQty", "verified",
})
OFFER_FIELDS = frozenset({"supplierName", "unitPrice", "moq", "availableQty"})


def _whole(value, name: str, *, minimum: int) -> Optional[int]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) \
            or not minimum <= value <= MAX_OFFER_QUANTITY:
        raise InvalidOfferError(f"{name} must be a whole number from {minimum} "
                                f"to {MAX_OFFER_QUANTITY}")
    return value


def parse_offers(raw) -> List[dict]:
    """Validate the owner's offers. Rejects rather than repairs."""
    if not isinstance(raw, list) or not raw:
        raise InvalidOfferError("offers must be a non-empty list")
    if len(raw) > MAX_OFFERS:
        raise InvalidOfferError(f"at most {MAX_OFFERS} offers can be compared")
    offers = []
    for i, offer in enumerate(raw, 1):
        if not isinstance(offer, dict):
            raise InvalidOfferError(f"offer {i} must be an object")
        derived = sorted(OFFER_DERIVED_FIELDS & set(offer))
        if derived:
            raise InvalidOfferError(
                f"offer {i}: these are worked out by ShopFlow and cannot be "
                f"supplied: {', '.join(derived)}")
        unknown = sorted(set(offer) - OFFER_FIELDS)
        if unknown:
            raise InvalidOfferError(f"offer {i}: unknown fields: {', '.join(unknown)}")
        name = safe_supplier_label(offer.get("supplierName"))
        if not name:
            raise InvalidOfferError(f"offer {i}: supplierName must be a plain "
                                    "supplier name")
        price = offer.get("unitPrice")
        if _cost(price) is None:
            raise InvalidOfferError(f"offer {i}: unitPrice must be a positive "
                                    "number")
        offers.append({
            "supplierName": name,
            "unitPrice": money(float(price)),
            "moq": _whole(offer.get("moq"), f"offer {i}: moq", minimum=1) or 1,
            "availableQty": _whole(offer.get("availableQty"),
                                   f"offer {i}: availableQty", minimum=0),
        })
    return offers


def compare_supplier_quotes(data: Dataset, sku_id: str, raw_offers,
                            confirmed_costs: Optional[Dict[str, float]] = None
                            ) -> dict:
    """Each offer costed against what the shop actually needs to buy.

    Required quantity is the committed shortfall from `engine.shortage`, never
    the caller's figure. For each offer:

        purchaseQty   = max(requiredQty, moq)
        purchaseCost  = purchaseQty x unitPrice
        excessQty     = purchaseQty - requiredQty

    Offers are returned in the order given. Nothing is ranked or chosen, and
    no offer is stored: an offer is a claim to compare, not a record of how a
    supplier has performed.
    """
    sku_id = str(sku_id or "")
    if sku_id not in data.products:
        return _unavailable(sku_id, UNKNOWN_SKU)
    offers = parse_offers(raw_offers)
    risk = money_at_risk(data, sku_id, confirmed_costs)
    if risk["status"] != AVAILABLE:
        return risk
    required = risk["shortageQty"]
    reference_price = risk["currentUnitCost"]
    reference = {
        "supplierName": getattr(data.supplierFor(sku_id), "name", ""),
        "unitPrice": reference_price,
        "source": risk["currentCostSource"],
        "purchaseCost": money(required * reference_price),
    }

    rows = []
    for offer in offers:
        price, moq, available = offer["unitPrice"], offer["moq"], offer["availableQty"]
        row = {**offer, "requiredQty": required, "flags": []}
        ratio = price / reference_price
        if not PLAUSIBLE_MIN_RATIO <= ratio <= PLAUSIBLE_MAX_RATIO:
            row["flags"].append(REVIEW_REQUIRED)
        if price < reference_price:
            row["flags"].append(BETTER_UNIT_PRICE)
        row["unitPriceDelta"] = money(price - reference_price)
        margin = margin_view(data, sku_id, price)
        row["marginPerUnit"] = margin.get("newMarginAmount")
        if required == 0:
            row.update(status=NO_REQUIREMENT, purchaseQty=0, purchaseCost=0.0,
                       excessQty=0, totalCostDelta=0.0)
        elif available is not None and available < required:
            row.update(status=INSUFFICIENT_QUANTITY, purchaseQty=None,
                       purchaseCost=None, excessQty=None, totalCostDelta=None)
        else:
            qty = max(required, moq)
            cost = money(qty * price)
            row.update(status=MOQ_BLOCKED if moq > required else FITS_REQUIREMENT,
                       purchaseQty=qty, purchaseCost=cost,
                       excessQty=qty - required,
                       totalCostDelta=money(cost - reference["purchaseCost"]))
        rows.append(row)

    costed = [r["purchaseCost"] for r in rows if r["purchaseCost"]]
    lowest = min(costed) if costed else None
    for row in rows:
        if lowest is not None and row["purchaseCost"] and row["purchaseCost"] > lowest:
            row["flags"].append(HIGHER_TOTAL_COST)
        row["explanation"] = _explain_offer(row, risk["unit"])

    return {
        "status": AVAILABLE,
        "skuId": sku_id,
        "productName": risk["productName"],
        "unit": risk["unit"],
        "requiredQty": required,
        "requiredQtySource": "engine.shortage (committed minus on hand)",
        "reference": reference,
        "offers": rows,
        "ranked": False,
        "ownerDecisionRequired": True,
        "note": ("Offers are compared on their stated terms. ShopFlow does not "
                 "pick a supplier, and an offer is not a record of past "
                 "performance."),
        "stateChanged": False,
        "source": SOURCE,
    }


def _explain_offer(row: dict, unit: str) -> str:
    name, price = row["supplierName"], f"₹{row['unitPrice']:,.2f}"
    if row["status"] == NO_REQUIREMENT:
        return f"{name}: {price} per {unit}. Nothing needs to be bought for orders."
    if row["status"] == INSUFFICIENT_QUANTITY:
        return (f"{name}: {price} per {unit}, but only {row['availableQty']} "
                f"available of the {row['requiredQty']} needed.")
    text = (f"{name}: {row['purchaseQty']} x {price} = "
            f"₹{row['purchaseCost']:,.2f}.")
    if row["status"] == MOQ_BLOCKED:
        text += (f" Minimum order {row['moq']} is more than the "
                 f"{row['requiredQty']} needed: {row['excessQty']} extra.")
    if REVIEW_REQUIRED in row["flags"]:
        text += " The price is far from what the shop pays - check the quote."
    return text


def committed_skus(data: Dataset) -> List[str]:
    """SKUs a customer has been promised, for the page's product picker."""
    return [s.skuId for s in shortages(data)]


__all__: Iterable[str] = (
    "money_at_risk", "buy_now_vs_wait", "compare_supplier_quotes",
    "parse_offers", "InvalidOfferError", "committed_skus",
)
