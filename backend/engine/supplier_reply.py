"""Supplier reply: what did the supplier offer, and how does it compare with
what the shop can pay?

After a counter-offer, the supplier answers in their own words - "6100 final,
5 coils min". A language model reads those words into terms
(agent.supplier_reply). Everything after that is here, and none of it is the
model's:

    check_extraction   every extracted figure must be in the supplier's own
                       text, positive, inside a plausible range and in the
                       product's own unit; a reply that names more than one
                       possible price is a question, not a choice
    evaluate_offer     the offer against the walk-away price
                       (engine.whatif.walk_away_price), the customer
                       commitment and planned restock (the purchase planner)
                       and the cash left after other committed purchases

The result is information for the owner. Nothing here accepts, counters,
buys, records or sends anything, and nothing is written.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Dict, List, Optional

from . import gst
from .messages import format_rupees
from .models import Dataset
from .negotiation import safe_product_label
from .price_alerts import alert_config
from .uom import normalize_uom, product_uom
from .whatif import DEFAULT_BUDGET, _plan, walk_away_price

MAX_REPLY_CHARS = 500

# Price states, against the walk-away price.
BELOW_WALK_AWAY = "BELOW_WALK_AWAY"
AT_WALK_AWAY = "AT_WALK_AWAY"
ABOVE_WALK_AWAY = "ABOVE_WALK_AWAY"
# Flags beside the price state.
CASH_CONSTRAINED = "CASH_CONSTRAINED"
BEYOND_COMMITMENTS = "BEYOND_COMMITMENTS"
BEYOND_PLANNED_NEED = "BEYOND_PLANNED_NEED"
# When the reply cannot be evaluated.
AMBIGUOUS = "AMBIGUOUS"
INVALID = "INVALID"
UNAVAILABLE = "UNAVAILABLE"

# An offered price outside this band of the current supplier price is not
# read as an offer: "accept ₹1" is not a price for a ₹6,300 coil. The same
# bounds the What-If simulator uses for a supplier cost.
MIN_PRICE_FACTOR = Decimal("0.5")
MAX_PRICE_FACTOR = Decimal("3")
MAX_QUANTITY = 10_000
MAX_LEAD_DAYS = 365

# Instruction-shaped text in a supplier's reply. It never changes what
# ShopFlow does - the reply is data - but the owner is told it was there.
_INSTRUCTION_LIKE = re.compile(
    r"\bignore\b|\binstructions?\b|\bprompt\b|\bsystem\b|\bapprove|"
    r"\bauto(?:matic|matically)?\b|\bsend\b|\btell (?:the )?owner\b|"
    r"\bset (?:the )?price\b",
    re.IGNORECASE)
_NUMBER = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?)")
_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "twelve": 12,
    "fifteen": 15, "twenty": 20, "fifty": 50, "hundred": 100,
}


def _dec(value) -> Optional[Decimal]:
    try:
        if isinstance(value, bool) or value is None:
            return None
        d = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, ValueError):
        return None
    return d if d.is_finite() else None


def _numbers_in(text: str) -> List[Decimal]:
    out = []
    for m in _NUMBER.finditer(text or ""):
        d = _dec(m.group(1))
        if d is not None:
            out.append(d)
    for word in re.findall(r"[a-z]+", (text or "").lower()):
        if word in _NUMBER_WORDS:
            out.append(Decimal(_NUMBER_WORDS[word]))
    return out


def clean_reply(text) -> str:
    """The supplier's reply as one bounded line of text."""
    clean = " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", str(text or "")).split())
    return clean[:MAX_REPLY_CHARS]


def instruction_like(text: str) -> bool:
    return bool(_INSTRUCTION_LIKE.search(text or ""))


def reply_context(data: Dataset, sku_id: str,
                  confirmed_costs: Optional[Dict[str, float]] = None, *,
                  budget: float = DEFAULT_BUDGET) -> dict:
    """The shop's side of the comparison, from the engines. Reads only."""
    sku_id = str(sku_id or "")
    if sku_id not in data.products:
        return {"available": False, "reason": "UNKNOWN_SKU",
                "message": "This product is not a catalogue SKU."}
    confirmed = {k: float(v) for k, v in (confirmed_costs or {}).items()
                 if k in data.products}
    current = confirmed.get(sku_id)
    if current is None or not current > 0:
        return {"available": False, "reason": "NO_CONFIRMED_PRICE",
                "message": ("There is no confirmed supplier price for this "
                            "product to compare a reply with.")}
    cfg = alert_config()
    walk = walk_away_price(
        data, sku_id, {"marginFloorPercent": Decimal(str(cfg.marginFloorPercent))},
        confirmed, budget)
    values, base = walk["scenarioValues"], walk["baseline"]
    if not values["walkAwayPrice"] or values["walkAwayPrice"] <= 0:
        return {"available": False, "reason": "NO_WALK_AWAY",
                "message": "No walk-away price can be funded for this product."}
    plan = _plan(data, budget, confirmed)
    restock = next((int(r.get("requestedQty") or 0)
                    for r in plan["restockSelected"] + plan["restockDeferred"]
                    if r["skuId"] == sku_id), 0)
    product = data.product(sku_id)
    cash = gst.paise(gst.to_decimal(budget)
                     - gst.to_decimal(base["otherCommittedCost"]))
    return {
        "available": True,
        "skuId": sku_id,
        "productName": safe_product_label(product.name),
        "uom": product_uom(product),
        "unit": str(product.unit or "unit").lower(),
        "currentSupplierPrice": float(current),
        "walkAwayPrice": values["walkAwayPrice"],
        "bindingLimit": values["bindingLimit"],
        "committedQty": int(base["committedQty"]),
        "plannedRestockQty": restock,
        "budget": float(budget),
        "otherCommittedCost": base["otherCommittedCost"],
        "cashAvailable": gst.out(cash if cash > 0 else Decimal("0")),
        "sources": {
            "walkAwayPrice": "engine.whatif.walk_away_price",
            "committedQty": "engine.whatif.walk_away_price (planner commitments)",
            "plannedRestockQty": "engine.purchasing (via engine.whatif._plan)",
            "cashAvailable": "budget - other committed purchases",
        },
    }


def check_extraction(reply: str, extracted, context: dict) -> dict:
    """Hold the model's reading of the reply to the reply itself.

    Returns {"status": "OK" | AMBIGUOUS | INVALID, "problems": [...],
    "terms": {...}}. Only OK terms are evaluated.
    """
    problems: List[str] = []
    if not isinstance(extracted, dict):
        return {"status": AMBIGUOUS, "problems": ["NOT_READ"], "terms": None}
    in_text = set(_numbers_in(reply))
    current = Decimal(str(context["currentSupplierPrice"]))

    price = _dec(extracted.get("offeredPrice"))
    if price is None:
        problems.append("NO_PRICE")
    elif price <= 0:
        return {"status": INVALID, "problems": ["PRICE_NOT_POSITIVE"],
                "terms": None}
    elif price not in in_text:
        # The model produced a figure the supplier did not write - divided a
        # total by a quantity, say. Not the supplier's offer: a question.
        return {"status": AMBIGUOUS, "problems": ["PRICE_NOT_IN_REPLY"],
                "terms": None}
    else:
        if not (current * MIN_PRICE_FACTOR <= price <= current * MAX_PRICE_FACTOR):
            return {"status": INVALID, "problems": ["PRICE_OUT_OF_RANGE"],
                    "terms": {"offeredPrice": float(price)}}
        # More than one number that could be the price: the owner says which.
        plausible = {n for n in in_text
                     if current * MIN_PRICE_FACTOR <= n <= current * MAX_PRICE_FACTOR}
        if len(plausible) > 1:
            problems.append("SEVERAL_PRICES")

    qty_raw = extracted.get("minimumQuantity")
    qty = None
    if qty_raw is not None:
        q = _dec(qty_raw)
        if q is None or q != q.to_integral_value():
            problems.append("QUANTITY_NOT_WHOLE")
        elif q <= 0:
            return {"status": INVALID, "problems": ["QUANTITY_NOT_POSITIVE"],
                    "terms": None}
        elif q > MAX_QUANTITY:
            return {"status": INVALID, "problems": ["QUANTITY_OUT_OF_RANGE"],
                    "terms": None}
        elif q not in in_text:
            problems.append("QUANTITY_NOT_IN_REPLY")
        else:
            qty = int(q)

    uom = extracted.get("uom")
    if uom not in (None, ""):
        if normalize_uom(str(uom)) != context["uom"]:
            problems.append("UNIT_NOT_THE_PRODUCTS")

    lead = extracted.get("leadTimeDays")
    lead_days = None
    if lead is not None:
        ld = _dec(lead)
        if ld is None or ld < 0 or ld > MAX_LEAD_DAYS \
                or ld != ld.to_integral_value() or ld not in in_text:
            problems.append("LEAD_TIME_UNREADABLE")
        else:
            lead_days = int(ld)

    valid_until = extracted.get("validUntil")
    if valid_until is not None:
        valid_until = clean_reply(valid_until)[:40]
        if not valid_until or valid_until.lower() not in reply.lower():
            problems.append("VALIDITY_NOT_IN_REPLY")

    terms = {"offeredPrice": float(price) if price is not None else None,
             "minimumQuantity": qty, "leadTimeDays": lead_days,
             "validUntil": valid_until}
    return {"status": AMBIGUOUS if problems else "OK", "problems": problems,
            "terms": terms}


QUESTIONS = {
    "NOT_READ": "ShopFlow could not read this reply.",
    "NO_PRICE": "The reply does not state a price.",
    "PRICE_NOT_IN_REPLY": "The price read from the reply is not written in it.",
    "SEVERAL_PRICES": "The reply mentions more than one possible price.",
    "QUANTITY_NOT_WHOLE": "The minimum quantity is not a whole number.",
    "QUANTITY_NOT_IN_REPLY": ("The minimum quantity read from the reply is not "
                              "written in it."),
    "UNIT_NOT_THE_PRODUCTS": ("The reply names a unit this product is not "
                              "sold in."),
    "LEAD_TIME_UNREADABLE": "The delivery time could not be read.",
    "VALIDITY_NOT_IN_REPLY": "The validity period could not be read.",
    "PRICE_NOT_POSITIVE": "The price in the reply is zero or negative.",
    "PRICE_OUT_OF_RANGE": ("The price in the reply is not a plausible price "
                           "for this product (it must be between half and "
                           "three times the current supplier price)."),
    "QUANTITY_NOT_POSITIVE": "The quantity in the reply is zero or negative.",
    "QUANTITY_OUT_OF_RANGE": "The quantity in the reply is not plausible.",
}


def question(check: dict) -> str:
    lines = [QUESTIONS.get(p, p) for p in check["problems"]]
    return (" ".join(lines) + " Nothing has been decided. Please read the "
            "reply and enter the terms yourself, or ask the supplier.")


def evaluate_offer(context: dict, terms: dict) -> dict:
    """The offer against the shop's own limits. Deterministic; no model."""
    price = gst.paise(gst.to_decimal(terms["offeredPrice"]))
    walk = gst.paise(gst.to_decimal(context["walkAwayPrice"]))
    current = gst.paise(gst.to_decimal(context["currentSupplierPrice"]))
    diff = price - walk
    status = (ABOVE_WALK_AWAY if diff > 0 else
              AT_WALK_AWAY if diff == 0 else BELOW_WALK_AWAY)

    committed = int(context["committedQty"])
    planned = int(context["plannedRestockQty"])
    minimum = terms.get("minimumQuantity")
    units = max(minimum or 0, committed) or None
    flags: List[str] = []
    cost = cash_short = None
    cash = gst.to_decimal(context["cashAvailable"])
    if units:
        cost = gst.paise(price * units)
        if cost > cash:
            flags.append(CASH_CONSTRAINED)
            cash_short = gst.paise(cost - cash)
    if minimum and minimum > committed:
        flags.append(BEYOND_COMMITMENTS)
    if minimum and minimum > committed + planned:
        flags.append(BEYOND_PLANNED_NEED)

    unit = context.get("unit") or "unit"
    plural = unit if (minimum == 1 or unit.endswith("s")) else unit + "s"
    parts = [f"Supplier offered {format_rupees(gst.out(price))}"
             + (f" for a minimum of {minimum} {plural}." if minimum else ".")]
    if status == ABOVE_WALK_AWAY:
        parts.append(f"This is {format_rupees(gst.out(diff))} above your "
                     f"walk-away price of {format_rupees(gst.out(walk))}.")
    elif status == AT_WALK_AWAY:
        parts.append(f"This is exactly your walk-away price of "
                     f"{format_rupees(gst.out(walk))}.")
    else:
        parts.append(f"This is {format_rupees(gst.out(-diff))} below your "
                     f"walk-away price of {format_rupees(gst.out(walk))}.")
    if CASH_CONSTRAINED in flags:
        parts.append(f"{units} x {format_rupees(gst.out(price))} = "
                     f"{format_rupees(gst.out(cost))}, "
                     f"{format_rupees(gst.out(cash_short))} more than the "
                     f"{format_rupees(gst.out(cash))} available after other "
                     f"committed purchases.")
    if BEYOND_COMMITMENTS in flags:
        parts.append(f"The minimum is {minimum - committed} more than the "
                     f"{committed} committed to customers.")
    return {
        "status": status,
        "flags": flags,
        "offeredPrice": gst.out(price),
        "currentSupplierPrice": gst.out(current),
        "walkAwayPrice": gst.out(walk),
        "differenceFromWalkAway": gst.out(diff),
        "changeFromCurrentPrice": gst.out(price - current),
        "minimumQuantity": minimum,
        "committedQty": committed,
        "plannedRestockQty": planned,
        "unitsPriced": units,
        "purchaseCost": gst.out(cost) if cost is not None else None,
        "cashAvailable": gst.out(cash),
        "cashShortfall": gst.out(cash_short) if cash_short is not None else None,
        "explanation": " ".join(parts),
        "ownerDecisionRequired": True,
        "decisionMadeBy": "owner",
        "stateChanged": False,
        "sent": False,
    }
