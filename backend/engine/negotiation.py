"""Supplier counter-offer: may the owner ask this supplier for a lower price,
at what figure, for how many - and is a worded draft of that ask safe to show?

WHAT THIS DECIDES
-----------------
Everything with a number in it. For one SKU:

    current supplier price   the owner's confirmed cost record
    previous supplier price  engine.margin.previous_supplier_cost
    increase and its %       engine.price_alerts.evaluate_price_change
    material or not          the same function (engine.pricing's one rule)
    walk-away price          the same function (engine.whatif.walk_away_price)
    target counter-offer     the walk-away price, unchanged
    quantity                 the purchase planner: the customer commitment for
                             the SKU, else its planned restock, else none
    supplier name            the catalogue's supplier for the SKU, else none

No formula is restated here. A draft is offered only when every eligibility
rule below holds; otherwise the answer is a fixed reason.

WHAT A LANGUAGE MODEL MAY DO
----------------------------
Word the message, from the figures above, and nothing else. Its text is kept
only if `validate_draft` passes it: every figure in it must be one of the
figures supplied, the target price, the current price and the quantity must
all be there, and it may not name another product, another supplier, a
discount, a decision or an agreement. Otherwise `fallback_draft` - a fixed
template filled from the same figures - is what the owner sees.

WHAT THIS IS NOT
----------------
It sends nothing and writes nothing. A draft is words for the owner to read,
copy and send themselves. Supplier document text never reaches the model:
only catalogue fields and engine figures do.
"""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Dict, Iterable, List, Optional

from .margin import previous_supplier_cost
from .messages import format_rupees
from .models import Dataset
from .price_alerts import AlertConfig, alert_config, evaluate_price_change
from .whatif import DEFAULT_BUDGET, _plan

ELIGIBLE = "ELIGIBLE"
UNKNOWN_SKU = "UNKNOWN_SKU"
PRODUCT_INCOMPLETE = "PRODUCT_INCOMPLETE"
NO_CONFIRMED_PRICE = "NO_CONFIRMED_PRICE"
CONFLICTING_PRICE = "CONFLICTING_PRICE"
NO_PREVIOUS_PRICE = "NO_PREVIOUS_PRICE"
NOT_AN_INCREASE = "NOT_AN_INCREASE"
NOT_MATERIAL = "NOT_MATERIAL"
NO_WALK_AWAY = "NO_WALK_AWAY"
WITHIN_WALK_AWAY = "WITHIN_WALK_AWAY"

_UNAVAILABLE = "Counter-offer unavailable because "
REASONS = {
    UNKNOWN_SKU: "this product is not a confirmed catalogue SKU.",
    PRODUCT_INCOMPLETE: "the catalogue entry has no product name or unit.",
    NO_CONFIRMED_PRICE: ("no supplier price for this product has been read "
                         "and confirmed by the owner."),
    CONFLICTING_PRICE: "supplier pricing is conflicting.",
    NO_PREVIOUS_PRICE: "there is no previous supplier price to compare with.",
    NOT_AN_INCREASE: "the supplier price has not increased.",
    NOT_MATERIAL: "the price increase is below the material-change threshold.",
    NO_WALK_AWAY: "no walk-away price can be funded for this product.",
    WITHIN_WALK_AWAY: ("the supplier price is within the current walk-away "
                       "threshold."),
}

QTY_COMMITMENT = "CUSTOMER_COMMITMENT"
QTY_RESTOCK = "PLANNED_RESTOCK"
QTY_NONE = "NONE"

# Document row states that make a supplier's price for the SKU untrustworthy.
_CONFLICT_STATES = frozenset({"CONFLICT"})

# The catalogue's placeholder for unbranded accessories. Not a brand, so the
# word in a draft names no other product.
GENERIC_BRAND = "generic"

MAX_DRAFT_CHARS = 700
MAX_LABEL_CHARS = 80


def _refuse(sku_id: str, code: str, **extra) -> dict:
    return {"eligible": False, "status": "UNAVAILABLE", "skuId": sku_id,
            "reason": code, "message": _UNAVAILABLE + REASONS[code],
            "terms": None, "stateChanged": False, **extra}


def _label(text) -> str:
    """Catalogue text as a one-line label: no control characters, bounded."""
    clean = " ".join(re.sub(r"[\x00-\x1f\x7f]", " ", str(text or "")).split())
    return clean[:MAX_LABEL_CHARS]


def _supplier_name(data: Dataset, sku_id: str) -> Optional[str]:
    """The catalogue's supplier for the SKU. Never the product's brand."""
    try:
        name = _label(data.supplierFor(sku_id).name)
    except (KeyError, AttributeError):
        return None
    return name or None


def _quantity(plan: dict, sku_id: str):
    """Customer commitment first, then planned restock, then none."""
    committed = sum(int(c.get("requestedQty") or 0)
                    for c in plan["commitments"] if c["skuId"] == sku_id)
    if committed > 0:
        return committed, QTY_COMMITMENT
    for r in plan["restockSelected"] + plan["restockDeferred"]:
        if r["skuId"] == sku_id and int(r.get("requestedQty") or 0) > 0:
            return int(r["requestedQty"]), QTY_RESTOCK
    return None, QTY_NONE


def negotiation_terms(data: Dataset, sku_id: str,
                      confirmed_costs: Optional[Dict[str, float]] = None, *,
                      budget: float = DEFAULT_BUDGET,
                      config: Optional[AlertConfig] = None,
                      document_rows: Iterable[dict] = ()) -> dict:
    """The deterministic terms of a counter-offer, or the reason there is none.

    `confirmed_costs` are the shop's confirmed purchase costs by SKU.
    `document_rows` are supplier price-list rows read after the confirmation;
    a CONFLICT row for this SKU there means the supplier's price is in doubt.
    Reads only; nothing is written and nothing passed in is modified.
    """
    sku_id = str(sku_id or "")
    if sku_id not in data.products:
        return _refuse(sku_id, UNKNOWN_SKU)
    product = data.product(sku_id)
    name, unit = _label(product.name), _label(product.unit)
    if not name or not unit:
        return _refuse(sku_id, PRODUCT_INCOMPLETE)

    confirmed = {k: float(v) for k, v in (confirmed_costs or {}).items()
                 if k in data.products}
    current = confirmed.get(sku_id)
    if current is None or not current > 0 or current != current:
        return _refuse(sku_id, NO_CONFIRMED_PRICE)

    if any(isinstance(r, dict) and r.get("skuId") == sku_id
           and r.get("status") in _CONFLICT_STATES for r in document_rows):
        return _refuse(sku_id, CONFLICTING_PRICE)

    previous = previous_supplier_cost(data, sku_id)
    if previous is None or not previous > 0:
        return _refuse(sku_id, NO_PREVIOUS_PRICE)

    cfg = config or alert_config()
    alert = evaluate_price_change(data, sku_id, previous, current,
                                  confirmed_costs=confirmed, budget=budget,
                                  config=cfg)
    facts = {"previousSupplierPrice": alert["oldCost"],
             "currentSupplierPrice": alert["newCost"],
             "priceIncreasePercent": alert["percentageDelta"],
             "walkAwayPrice": alert["walkAway"]["price"]}
    if alert["direction"] != "INCREASE":
        return _refuse(sku_id, NOT_AN_INCREASE, facts=facts)
    if not alert["materialChange"]:
        return _refuse(sku_id, NOT_MATERIAL, facts=facts,
                       thresholdPercent=cfg.percentThreshold)
    walk = alert["walkAway"]
    if not walk["price"] or walk["price"] <= 0:
        return _refuse(sku_id, NO_WALK_AWAY, facts=facts)
    if not walk["currentCostAboveWalkAway"]:
        return _refuse(sku_id, WITHIN_WALK_AWAY, facts=facts)

    quantity, quantity_source = _quantity(_plan(data, budget, confirmed), sku_id)
    terms = {
        "skuId": sku_id,
        "productName": name,
        "brand": _label(product.brand),
        "uom": unit,
        "previousSupplierPrice": alert["oldCost"],
        "currentSupplierPrice": alert["newCost"],
        "absoluteIncrease": alert["absoluteDelta"],
        "priceIncreasePercent": alert["percentageDelta"],
        "walkAwayPrice": walk["price"],
        "bindingLimit": walk["bindingLimit"],
        # The ask is the walk-away price itself. No discount rule exists in
        # this shop, so none is invented here.
        "targetCounterOffer": walk["price"],
        "priceGap": walk["differenceFromCurrentCost"],
        "quantity": quantity,
        "quantitySource": quantity_source,
        "supplierName": _supplier_name(data, sku_id),
        "supplierSource": "CATALOGUE",
        "budget": float(budget),
        "materialThresholdPercent": cfg.percentThreshold,
        "sources": {
            "currentSupplierPrice": "confirmed supplier cost record",
            "previousSupplierPrice": "engine.margin.previous_supplier_cost",
            "priceIncreasePercent": "engine.price_alerts.evaluate_price_change",
            "walkAwayPrice": "engine.whatif.walk_away_price",
            "quantity": "engine.purchasing (via engine.whatif._plan)",
            "supplierName": "catalogue supplier for the SKU",
        },
    }
    return {"eligible": True, "status": ELIGIBLE, "skuId": sku_id,
            "reason": ELIGIBLE, "message": "", "terms": terms,
            "stateChanged": False}


# ---------------------------------------------------------------------------
# the words
# ---------------------------------------------------------------------------

def _units(uom: str, quantity) -> str:
    if quantity == 1 or uom.endswith("s"):
        return uom
    return uom + ("es" if uom.endswith(("x", "ch", "sh")) else "s")


def greeting(terms: dict) -> str:
    return f"Hi {terms['supplierName']}," if terms.get("supplierName") else "Hi,"


def fallback_draft(terms: dict) -> str:
    """The fixed template. Every placeholder is a figure in `terms`."""
    ask = (f"for our next {terms['quantity']} "
           f"{_units(terms['uom'], terms['quantity'])}"
           if terms.get("quantity") else "on our next purchase")
    return (f"{greeting(terms)} we regularly purchase {terms['productName']}. "
            f"The latest price is {format_rupees(terms['currentSupplierPrice'])} "
            f"per {terms['uom']}. At this price our purchase economics become "
            f"difficult. Can you offer "
            f"{format_rupees(terms['targetCounterOffer'])} or better {ask}?")


def model_payload(terms: dict) -> dict:
    """What the model is given: figures and catalogue labels, pre-formatted."""
    return {
        "productName": terms["productName"],
        "sku": terms["skuId"],
        "uom": terms["uom"],
        "previousSupplierPrice": format_rupees(terms["previousSupplierPrice"]),
        "currentSupplierPrice": format_rupees(terms["currentSupplierPrice"]),
        "priceIncreasePercent": f"{terms['priceIncreasePercent']:.2f}%",
        "walkAwayPrice": format_rupees(terms["walkAwayPrice"]),
        "targetCounterOffer": format_rupees(terms["targetCounterOffer"]),
        "quantity": terms["quantity"],
        "supplierName": terms.get("supplierName"),
        "greeting": greeting(terms),
    }


# ---------------------------------------------------------------------------
# the check on the words
# ---------------------------------------------------------------------------

_NUMBER = re.compile(r"(?<![\w.])(\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?)")
_CURRENCY_BEFORE = re.compile(r"(?:₹|\brs\.?|\binr)\s*$", re.I)
_GREETING = re.compile(r"^\s*(?:hi|hello|dear|namaste)\b[ \t]*([^,!.\n]*)",
                       re.I)
_GENERIC_ADDRESSEE = {"", "team", "sir", "sir/madam", "madam", "there", "all"}

# Phrases a draft may not contain, by what they would wrongly claim or do.
_FORBIDDEN = [
    ("UNSUPPORTED_DISCOUNT", r"\bdiscount|\brebate|\bcash ?back|% off\b"),
    ("PURCHASE_DECISION",
     r"\bwe (?:will|shall|'ll|are going to) (?:buy|purchase|order|stop|switch|"
     r"cancel|place)|\bwe have decided\b|\bwe are placing\b|"
     r"\bplease (?:ship|dispatch|deliver|book)\b"),
    ("CLAIMS_APPROVAL",
     r"\bwe (?:accept|agree|approve|confirm)\b|\b(?:approved|confirmed)\b|"
     r"\balready (?:agreed|approved|accepted)\b"),
    ("CLAIMS_SUPPLIER_AGREED",
     r"\bas agreed\b|\byou (?:have )?(?:agreed|accepted|confirmed)\b|"
     r"\bthank(?:s| you) for agreeing\b|\bas discussed\b"),
    ("AUTO_SEND",
     r"\bsen[dt] automatically\b|\bauto-?(?:send|sent|matic)|"
     r"\bautomated message\b|\bthis message (?:was|has been|is) sent\b|"
     r"\bsent (?:by|via|from) shopflow\b"),
    ("LINK_OR_CONTACT", r"https?://|www\.|\S+@\S+\.\w+"),
    # The shop's own negotiating terms are not for the supplier's eyes: the
    # walk-away price is the owner's ceiling, and a margin is private.
    ("INTERNAL_TERMS",
     r"walk-?\s?away|\bmargin|\btarget\b|\bcounter-?\s?offer\b|\bsku\b|"
     r"\bceiling\b"),
]


def _decimal(text: str) -> Optional[Decimal]:
    try:
        return Decimal(text.replace(",", ""))
    except InvalidOperation:
        return None


def _figures(text: str) -> List[tuple]:
    """(value, is_money) for every number in the text."""
    out = []
    for m in _NUMBER.finditer(text):
        value = _decimal(m.group(1))
        if value is not None:
            out.append((value, bool(_CURRENCY_BEFORE.search(text[:m.start()]))))
    return out


def _d(value) -> Decimal:
    return Decimal(str(value)).quantize(Decimal("0.01"))


def validate_draft(text, terms: dict, data: Dataset) -> dict:
    """Is this worded draft faithful to the terms? Deterministic; no model.

    Returns {"valid": bool, "problems": [codes...]}.
    """
    problems: List[str] = []
    if not isinstance(text, str) or not text.strip():
        return {"valid": False, "problems": ["EMPTY"]}
    if len(text) > MAX_DRAFT_CHARS:
        problems.append("TOO_LONG")
    if "```" in text or re.search(r"^\s*[#*>-]\s", text, re.M):
        problems.append("FORMATTING")

    # Catalogue labels are data and may be quoted verbatim; their own digits
    # are not figures of the model's. Everything else is checked.
    body = text
    for label in (terms.get("productName"), terms.get("supplierName"),
                  terms.get("skuId")):
        if label:
            body = re.sub(re.escape(label), " ", body, flags=re.I)
    lowered = body.lower()

    money = {_d(terms[k]) for k in ("previousSupplierPrice",
                                    "currentSupplierPrice",
                                    "targetCounterOffer", "walkAwayPrice",
                                    "priceGap", "absoluteIncrease")
             if terms.get(k) is not None}
    plain = set(money) | {_d(terms["priceIncreasePercent"])}
    if terms.get("quantity"):
        plain.add(_d(terms["quantity"]))
    product = data.products.get(terms.get("skuId"))
    for field in ("specification", "length", "colour", "category"):
        for m in _NUMBER.finditer(str(getattr(product, field, "") or "")):
            value = _decimal(m.group(1))
            if value is not None:
                plain.add(_d(value))

    figures = _figures(body)
    for value, is_money in figures:
        allowed = money if is_money else plain
        if _d(value) not in allowed:
            problems.append("UNSUPPORTED_NUMBER")
            break
    present = {_d(v) for v, _ in figures}
    if _d(terms["targetCounterOffer"]) not in present:
        problems.append("TARGET_MISSING")
    if _d(terms["currentSupplierPrice"]) not in present:
        problems.append("CURRENT_PRICE_MISSING")
    if terms.get("quantity") and _d(terms["quantity"]) not in present:
        problems.append("QUANTITY_MISSING")

    # The product must still be recognisably this one: its brand when the
    # catalogue name carries it, otherwise most of the name's own words.
    brand = (terms.get("brand") or "").lower()
    name = (terms.get("productName") or "").lower()
    if brand and brand in name:
        if brand not in text.lower():
            problems.append("PRODUCT_CHANGED")
    else:
        words = set(re.findall(r"[a-z]{3,}", name))
        if words and sum(w in text.lower() for w in words) * 2 < len(words):
            problems.append("PRODUCT_CHANGED")
    others = {p.brand.lower() for p in data.products.values()
              if p.brand and p.brand.lower() not in (brand, GENERIC_BRAND)
              and p.brand.lower() not in brand}
    if any(re.search(rf"\b{re.escape(b)}\b", lowered) for b in others):
        problems.append("OTHER_PRODUCT")
    if any(s != terms.get("skuId") and s.lower() in lowered
           for s in data.products):
        problems.append("OTHER_SKU")

    supplier = (terms.get("supplierName") or "").lower()
    names = {s.name.lower() for s in data.suppliers.values() if s.name}
    if any(n != supplier and n in text.lower() for n in names):
        problems.append("INVENTED_SUPPLIER")
    m = _GREETING.match(text)
    if m:
        addressee = " ".join(m.group(1).split()).lower()
        if addressee not in _GENERIC_ADDRESSEE and addressee != supplier:
            problems.append("INVENTED_SUPPLIER")

    for code, pattern in _FORBIDDEN:
        if re.search(pattern, lowered, re.I):
            problems.append(code)

    problems = list(dict.fromkeys(problems))
    return {"valid": not problems, "problems": problems}
