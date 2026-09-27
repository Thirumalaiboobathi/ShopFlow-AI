"""Business What-If: test a decision before making it.

WHAT THIS IS
------------
A shop owner's "what happens if...?" answered by the same deterministic
engines that price quotations and plan purchases, run on a copy of the facts.

    "What if Finolex wire price increases by Rs 300?"
    "What if I give this customer 2% discount?"
    "What if the customer takes 5 instead of 3?"
    "What if I have only Rs 20,000 to restock?"
    "What if I increase the selling price by Rs 200?"
    "What if I include GST in the selling price?"
    "What if I don't restock the shortage?"
    "What if supplier price increases 10%?"
    "What is the most I can pay for Finolex wire?"   (walk-away price)

Every answer has the same four parts - CURRENT, SCENARIO, CHANGE, IMPACT - and
one DECISION. Each part is a figure an engine produced: the margin engine's
status, the planner's allocation, the quotation's arithmetic, the GST
calculator's tax. The decision is chosen by a fixed rule from those figures.

WHAT IT NEVER DOES
------------------
It changes nothing. There is no write in this module and no function here
receives a table, a client or anything that could reach one. The dataset is
read, never assigned: prices are `frozen` dataclasses, and the planner runs
over `purchasing.apply_confirmed_costs`, which copies before it reprices.
Stock, supplier costs, confirmed prices, orders, khata accounts and the
purchase plan are exactly what they were before the question was asked. Every
response says so in `stateChanged: false`, and the tests prove it.

WHO READS THE SENTENCE
----------------------
Not a language model. The owner's sentence is read by `parse_scenario`, a
closed set of patterns, and turned into a scenario TYPE and bounded
PARAMETERS. That is deliberate: the model is where a number can be invented,
and every number here must be one the owner typed or one an engine computed.
A sentence that fits no pattern is refused with examples of what does, and a
sentence that fits one but leaves a number or a product open is answered with
a question, never with a guess.

Some sentences are refused on purpose, whatever else they contain:

    "Use 18% GST" / "GST is zero"   a tax rate is configuration, not a what-if
    "Make my margin 20%"            margin is a result of price and cost
    "Give me a 50% discount"        above the configured discount ceiling
    "Supplier price is Rs 1"        outside the plausible range for a cost
    "Show me khata / phone numbers" not something this simulator reads
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Dict, List, Optional

from . import gst
from .margin import MARGIN_WARNING_PERCENT, _status as margin_status
from .matching import AMBIGUOUS, RESOLVED, resolve_product
from .messages import format_rupees
from .models import Dataset
from .pricing import current_cost
from .purchasing import InvalidBudgetError, build_purchase_plan, parse_budget
from .quote import calculate_quote
from .voice import extract_attributes

# --- scenario types ---------------------------------------------------------
SUPPLIER_COST_CHANGE = "SUPPLIER_COST_CHANGE"
SELLING_PRICE_CHANGE = "SELLING_PRICE_CHANGE"
DISCOUNT = "DISCOUNT"
QUANTITY_CHANGE = "QUANTITY_CHANGE"
BUDGET_CHANGE = "BUDGET_CHANGE"
GST_INCLUSIVE_PRICE = "GST_INCLUSIVE_PRICE"
SKIP_RESTOCK = "SKIP_RESTOCK"
GST_VIEW = "GST_VIEW"
WALK_AWAY_PRICE = "WALK_AWAY_PRICE"

SCENARIO_TYPES = (SUPPLIER_COST_CHANGE, SELLING_PRICE_CHANGE, DISCOUNT,
                  QUANTITY_CHANGE, BUDGET_CHANGE, GST_INCLUSIVE_PRICE,
                  SKIP_RESTOCK, GST_VIEW, WALK_AWAY_PRICE)

# --- outcomes ---------------------------------------------------------------
SIMULATED = "SIMULATED"
NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
REFUSED = "REFUSED"

# --- refusal reasons --------------------------------------------------------
GST_RATE_NOT_SETTABLE = "GST_RATE_NOT_SETTABLE"
MARGIN_IS_A_RESULT = "MARGIN_IS_A_RESULT"
OUT_OF_SCOPE = "OUT_OF_SCOPE"
DISCOUNT_ABOVE_LIMIT = "DISCOUNT_ABOVE_LIMIT"
OUTSIDE_PLAUSIBLE_RANGE = "OUTSIDE_PLAUSIBLE_RANGE"
INVALID_VALUE = "INVALID_VALUE"
NOT_UNDERSTOOD = "NOT_UNDERSTOOD"

# --- guardrails, configured here and nowhere else ---------------------------
# A counter discount above this is a pricing decision, not a what-if.
MAX_DISCOUNT_PERCENT = Decimal("20")
# A supplier cost may be simulated from half to three times what it is now.
# Outside that, the figure is almost certainly a typo or an injection
# ("supplier price is Rs 1"), and simulating it would teach nothing.
MIN_COST_FACTOR = Decimal("0.5")
MAX_COST_FACTOR = Decimal("3")
# A selling price may be simulated within +/- 50% of the shelf price.
MAX_PRICE_CHANGE_PERCENT = Decimal("50")
MAX_QUANTITY = 10_000
# The margin floor a walk-away price may be asked for. Above this the "floor"
# is a pricing strategy, not a limit, and at or below zero it is no limit.
MAX_MARGIN_FLOOR_PERCENT = Decimal("50")
MAX_TEXT_CHARS = 500
DEFAULT_BUDGET = 25000.0

INTERPRETER = "deterministic parser (engine.whatif.parse_scenario)"
SIMULATION_NOTICE = ("Simulation only. Nothing was changed: stock, supplier "
                     "costs, confirmed prices, orders, customer accounts and "
                     "the purchase plan are exactly as they were.")

EXAMPLES = (
    "What if Finolex wire price increases by ₹300?",
    "What if supplier price increases 10%?",
    "What if I give this customer 2% discount?",
    "What if the customer takes 5 instead of 3?",
    "What if I have only ₹20,000 to restock?",
    "What if I increase the selling price by ₹200?",
    "What if I include GST in the selling price?",
    "What if I don't restock the shortage?",
    "What is the most I can pay for Finolex wire?",
)

HUNDRED = Decimal("100")
ZERO = Decimal("0")


class ScenarioError(ValueError):
    """A scenario that cannot be simulated, with the reason code to show."""

    def __init__(self, reason: str, message: str, details: Optional[dict] = None):
        self.reason = reason
        # The figures a refusal quotes, carried as data so the words stay
        # grounded: a limit named in a sentence is a limit in the response.
        self.details = details or {}
        super().__init__(message)


# ---------------------------------------------------------------------------
# reading the sentence
# ---------------------------------------------------------------------------

_NUMBER = r"(\d{1,3}(?:,\d{2,3})+(?:\.\d+)?|\d+(?:\.\d+)?)"
_MONEY = re.compile(
    r"(?:₹|\brs\.?|\binr)\s*" + _NUMBER + r"|" + _NUMBER + r"\s*(?:rupees?|\brs\b)",
    re.IGNORECASE)
_PERCENT = re.compile(_NUMBER + r"\s*(?:%|percent\b|per\s*cent\b)", re.IGNORECASE)
_BARE = re.compile(r"(?<![\w.])" + _NUMBER + r"(?![\w%])")

_UP = re.compile(r"\b(increase[sd]?|rise[sn]?|rising|goes\s+up|go\s+up|up\s+by|"
                 r"hike[sd]?|higher|raise[sd]?|costlier|more\s+expensive|"
                 r"jump[sd]?)\b|\+", re.IGNORECASE)
_DOWN = re.compile(r"\b(decrease[sd]?|drop[sp]?|dropped|fall[s]?|fell|reduce[sd]?|"
                   r"lower(?:ed)?|cheaper|down\s+by|cut[s]?)\b", re.IGNORECASE)

_OUT_OF_SCOPE = re.compile(
    r"\b(khata|credit\s+limit|outstanding|phone|mobile\s+number|"
    r"contact\s+number|customer\s+list|all\s+customers|reveal|system\s+prompt|"
    r"ignore\s+(?:all\s+)?(?:the\s+)?previous|instructions)\b"
    # A role marker is somebody trying to speak for the system. A shop
    # owner's question has no reason to contain one.
    r"|\b(?:system|assistant|developer)\s*(?::|says\b)",
    re.IGNORECASE)
_SET_MARGIN = re.compile(
    r"\b(make|set|keep|fix|force)\b[^.?!]*\bmargin\b|\bmargin\s+(?:to|=|of|at)\s*\d",
    re.IGNORECASE)
_SET_GST = re.compile(
    r"\bgst\b[^.?!]*?(?:\bis\b|=|\bat\b|\bof\b|\brate\b|\bto\b)\s*(?:zero|nil|none|"
    r"\d+(?:\.\d+)?\s*%?)|\d+(?:\.\d+)?\s*%\s*gst\b|\b(?:no|zero|nil)\s+gst\b",
    re.IGNORECASE)

_INCLUDE_GST = re.compile(
    r"\binclud\w*\s+gst\b[^.?!]*\b(price|mrp|shelf)|\bgst[\s-]+inclusive\s+"
    r"(?:selling\s+)?price|\bprice\s+(?:already\s+)?includ\w*\s+gst|\babsorb\w*\s+"
    r"(?:the\s+)?gst", re.IGNORECASE)
_DISCOUNT = re.compile(r"\bdiscount\b|\boff\b", re.IGNORECASE)
_INSTEAD = re.compile(r"(\d+)\s*(?:\w+\s+){0,3}?instead\s+of\s+(\d+)", re.IGNORECASE)
_TAKES = re.compile(r"\b(?:takes?|buys?|orders?|wants?|quantity\s+(?:to|of|is)|"
                    r"make\s+it)\s+(\d+)\b", re.IGNORECASE)
_SKIP_RESTOCK = re.compile(
    r"\b(?:don'?t|do\s+not|not|never|skip|no)\s+(?:re-?stock|restocking|buy|"
    r"purchase|reorder)", re.IGNORECASE)
_BUDGET = re.compile(r"\b(budget|cash|restock|to\s+spend|to\s+buy|money|"
                     r"only\s+have|have\s+only|i\s+have)\b", re.IGNORECASE)
_SELLING = re.compile(r"\b(selling|sell|shelf|my\s+price|our\s+price|mrp|"
                      r"retail\s+price)\b", re.IGNORECASE)
_SUPPLIER = re.compile(r"\b(supplier|dealer|purchase\s+price|buying\s+price|"
                       r"cost|price)\b", re.IGNORECASE)
# Words that can only mean what the shop PAYS. "price" alone is ambiguous and
# is not one of them.
_SUPPLIER_EXPLICIT = re.compile(r"\b(supplier|dealer|purchase\s+price|"
                                r"buying\s+price|cost)\b", re.IGNORECASE)
# "What is the most I can pay?" - the walk-away price. Read before the
# set-margin refusal, because "keeping a 10% margin" is the floor the owner is
# asking about, not an attempt to set the margin.
_WALK_AWAY = re.compile(
    r"\bwalk[\s-]*away\b|\bmost\s+(?:i|we)\s+(?:can|could|should)\s+"
    r"(?:pay|afford|accept)|\bmax(?:imum)?\s+(?:i|we)\s+(?:can|could)\s+pay"
    r"|\bmax(?:imum)?\s+(?:supplier\s+|purchase\s+|buying\s+)?(?:price|cost)"
    r"\s+(?:i|we)\s+can|\bhighest\s+(?:supplier\s+)?(?:price|cost)\s+(?:i|we)"
    r"\s+can|\bhow\s+much\s+can\s+(?:i|we)\s+pay\b", re.IGNORECASE)
_GST_VIEW = re.compile(
    r"\b(gst|tax|igst|cgst|sgst)\b|customer\s+(?:will\s+)?pay|final\s+"
    r"(?:customer\s+)?amount|another\s+state|other\s+state|inter[\s-]?state",
    re.IGNORECASE)


# An amount written with a minus sign, or "negative"/"minus" in front of it:
# "-₹500", "₹-500", "-500", "negative ₹500", "minus 5%". A hyphen glued to a
# word before it ("W-FIN-1.5-RED") is part of a name, not a sign.
_SIGNED_AMOUNT = re.compile(
    r"(?:(?<![\w.])[-−–]\s*(?:₹|rs\.?\s*|inr\s*)?"
    r"|(?:₹|\brs\.?|\binr)\s*[-−–]\s*"
    r"|\b(?:negative|minus)\s+(?:₹|rs\.?\s*|inr\s*)?)"
    + _NUMBER + r"(\s*(?:%|percent\b|per\s*cent\b))?", re.IGNORECASE)
_ADD = re.compile(r"\b(add|adds|added|plus)\b", re.IGNORECASE)


def _signed_amount_question(text: str) -> Optional[dict]:
    """A stated change whose amount carries a minus sign is a question.

    "increases by -₹500" was simulated as an increase of ₹500: the direction
    word was read, the sign was not. It is not "obviously" a decrease either -
    the owner may have typed the sign by mistake. Neither reading is chosen;
    both are offered.
    """
    match = _SIGNED_AMOUNT.search(text)
    if not match:
        return None
    value = _num(match.group(1))
    percent = bool(match.group(2))
    amount = (f"{value:g}%" if percent
              else format_rupees(float(value)).replace(".00", ""))
    up = bool(_UP.search(text) or _ADD.search(text))
    down = bool(_DOWN.search(text))
    if up and not down:
        lead = "The increase amount cannot be negative. "
    elif down and not up:
        lead = "The decrease amount cannot be negative. "
    else:
        lead = "The amount in the question is negative. "
    question = (f"{lead}Did you mean an increase of {amount} or a decrease "
                f"of {amount}?")
    unit = "PERCENT" if percent else "AMOUNT"
    return {"status": NEEDS_CLARIFICATION, "type": None, "params": {},
            "question": question, "attribute": "direction",
            "reason": INVALID_VALUE,
            "options": [
                {"value": "INCREASE", "kind": unit, "amount": float(value)},
                {"value": "DECREASE", "kind": unit, "amount": float(value)},
            ]}


def _num(text: str) -> Decimal:
    return Decimal(text.replace(",", ""))


def _money_amounts(text: str) -> List[Decimal]:
    out = []
    for match in _MONEY.finditer(text):
        raw = match.group(1) or match.group(2)
        out.append(_num(raw))
    return out


def _percent_amounts(text: str) -> List[Decimal]:
    return [_num(m.group(1)) for m in _PERCENT.finditer(text)]


def _direction(text: str) -> Optional[int]:
    up, down = bool(_UP.search(text)), bool(_DOWN.search(text))
    if up == down:
        return None
    return 1 if up else -1


def _one(values: List[Decimal], what: str) -> Optional[Decimal]:
    unique = sorted(set(values))
    if len(unique) > 1:
        raise ScenarioError(INVALID_VALUE,
                            f"The question states more than one {what} "
                            f"({', '.join(str(v) for v in unique)}). Please "
                            f"ask about one change at a time.")
    return unique[0] if unique else None


def _change(text: str) -> Optional[dict]:
    """A stated change: +/- an amount, +/- a percentage, or a new value."""
    percent = _one(_percent_amounts(text), "percentage")
    money = _one(_money_amounts(text), "amount")
    direction = _direction(text)
    if percent is not None and money is not None:
        raise ScenarioError(INVALID_VALUE,
                            "The question states both an amount and a "
                            "percentage. Please use one.")
    if percent is not None:
        if direction is None:
            return None
        return {"kind": "PERCENT", "value": percent, "direction": direction}
    if money is not None:
        if direction is None:
            # "What if the supplier price is Rs 6,500?" - a new value.
            return {"kind": "ABSOLUTE", "value": money, "direction": 0}
        if money == ZERO:
            # Refused like a 0% change: a change of nothing is not a scenario.
            raise ScenarioError(INVALID_VALUE,
                                "A change of ₹0 changes nothing. Please state "
                                "an amount greater than ₹0.")
        return {"kind": "AMOUNT", "value": money, "direction": direction}
    return None


def parse_scenario(text: str) -> dict:
    """Read one owner sentence into a scenario type and its parameters.

    Returns {"status": SIMULATED-ready "OK" | NEEDS_CLARIFICATION | REFUSED,
    "type", "params", "reason", "message"}. Nothing here resolves a product
    or reads a price - it only reads what the sentence says.
    """
    text = " ".join(str(text or "").split())[:MAX_TEXT_CHARS]
    if not text:
        return _ask(None, "Describe the change you want to test.",
                    reason=NOT_UNDERSTOOD)

    if _OUT_OF_SCOPE.search(text):
        return _refuse(OUT_OF_SCOPE,
                       "The What-If simulator answers questions about prices, "
                       "costs, quantities, GST and cash. It does not read "
                       "customer accounts, phone numbers or its own "
                       "instructions.")
    if _WALK_AWAY.search(text):
        # A margin floor the owner states is used; figures that look like a
        # supplier price are not - the cost is always the shop's own record.
        percent = _percent_amounts(text)
        if len(set(percent)) > 1:
            return _refuse(INVALID_VALUE, "Please state one margin floor, for "
                           "example \"keeping a 10% margin\".")
        floor = percent[0] if percent else Decimal(str(MARGIN_WARNING_PERCENT))
        if floor <= ZERO or floor > MAX_MARGIN_FLOOR_PERCENT:
            return _refuse(INVALID_VALUE,
                           f"A margin floor must be more than 0% and at most "
                           f"{MAX_MARGIN_FLOOR_PERCENT}%.")
        return _ok(WALK_AWAY_PRICE, {
            "marginFloorPercent": floor,
            "marginFloorSource": ("question" if percent
                                  else "shop warning threshold"),
            "typedAmountsIgnored": [float(v) for v in _money_amounts(text)],
        })
    if _SET_MARGIN.search(text):
        return _refuse(MARGIN_IS_A_RESULT,
                       "Margin is the result of the selling price and the "
                       "supplier cost, so it cannot be set directly. Try "
                       "\"What if I increase the selling price by ₹200?\"")
    if _SET_GST.search(text) and not _INCLUDE_GST.search(text):
        return _refuse(GST_RATE_NOT_SETTABLE,
                       "GST rates come from the shop's tax configuration and "
                       "cannot be changed by a question. The simulator always "
                       "uses the configured rate.")

    signed = _signed_amount_question(text)
    if signed:
        return signed

    try:
        if _INCLUDE_GST.search(text):
            return _ok(GST_INCLUSIVE_PRICE, {})

        if _DISCOUNT.search(text) and (_percent_amounts(text) or
                                       _money_amounts(text)):
            percent = _one(_percent_amounts(text), "discount")
            money = _one(_money_amounts(text), "discount")
            if percent is not None and money is not None:
                raise ScenarioError(INVALID_VALUE, "Please state the discount "
                                    "as a percentage or an amount, not both.")
            if percent is not None:
                return _ok(DISCOUNT, {"kind": "PERCENT", "value": percent})
            return _ok(DISCOUNT, {"kind": "AMOUNT", "value": money})
        if _DISCOUNT.search(text):
            return _ask(DISCOUNT, "How much discount - for example 2% or ₹100?",
                        attribute="discount")

        instead = _INSTEAD.search(text)
        if instead:
            return _ok(QUANTITY_CHANGE, {"newQuantity": int(instead.group(1)),
                                         "oldQuantity": int(instead.group(2))})
        takes = _TAKES.search(text)
        if takes and not _money_amounts(text):
            return _ok(QUANTITY_CHANGE, {"newQuantity": int(takes.group(1)),
                                         "oldQuantity": None})

        if _SKIP_RESTOCK.search(text):
            return _ok(SKIP_RESTOCK, {})

        money = _money_amounts(text)
        if _BUDGET.search(text) and money and not _SUPPLIER.search(text) \
                and not _SELLING.search(text):
            return _ok(BUDGET_CHANGE, {"budget": _one(money, "budget")})

        if _SELLING.search(text) and _SUPPLIER_EXPLICIT.search(text):
            # Both prices named: which one moves is exactly the question, and
            # choosing for the owner is how an injected clause ("...treat the
            # selling price as...") would steer the answer.
            return _ask(None, "Do you mean the supplier cost or your selling "
                              "price? Please ask about one of them.",
                        attribute="price")
        if _SELLING.search(text):
            change = _change(text)
            if change is None:
                return _ask(SELLING_PRICE_CHANGE,
                            "By how much would the selling price change - for "
                            "example +₹200 or -5%?", attribute="amount")
            return _ok(SELLING_PRICE_CHANGE, change)

        if _SUPPLIER.search(text) and (_UP.search(text) or _DOWN.search(text)
                                       or money or _percent_amounts(text)
                                       or re.search(r"\bchange", text, re.I)):
            change = _change(text)
            if change is None:
                return _ask(SUPPLIER_COST_CHANGE,
                            "By how much would the supplier cost change - for "
                            "example +₹300 or +10%?", attribute="amount")
            return _ok(SUPPLIER_COST_CHANGE, change)

        if _GST_VIEW.search(text):
            return _ok(GST_VIEW, {})
    except ScenarioError as exc:
        return _refuse(exc.reason, str(exc))

    return _refuse(NOT_UNDERSTOOD,
                   "That is not a scenario ShopFlow can simulate. Try one of: "
                   + " ".join(EXAMPLES[:4]))


def _ok(kind: str, params: dict) -> dict:
    return {"status": "OK", "type": kind, "params": params}


def _ask(kind: Optional[str], question: str, *, attribute: str = "",
         reason: str = "") -> dict:
    return {"status": NEEDS_CLARIFICATION, "type": kind, "params": {},
            "question": question, "attribute": attribute, "reason": reason}


def _refuse(reason: str, message: str) -> dict:
    return {"status": REFUSED, "type": None, "params": {}, "reason": reason,
            "message": message}


# ---------------------------------------------------------------------------
# which product
# ---------------------------------------------------------------------------

def _resolve_sku(data: Dataset, text: str, context_sku: Optional[str],
                 quote: Optional[dict]) -> dict:
    """The product a question is about, or the question to ask about it.

    The sentence wins when it names one product. When it names a family
    ("Finolex wire") the product the owner has selected is used if it belongs
    to that family, and otherwise the owner is asked which one. A product is
    never picked because it was first in a list.
    """
    attrs = extract_attributes(data, text)
    context_ok = bool(context_sku) and context_sku in data.products
    quote_skus = [l.get("skuId") for l in (quote or {}).get("lines") or []
                  if l.get("skuId") in data.products]

    if attrs.get("brand") or attrs.get("category"):
        found = resolve_product(data, requested_text=text, **attrs)
        if found.status == RESOLVED:
            return {"skuId": found.skuId, "source": "question text"}
        if found.status == AMBIGUOUS:
            options = [o["skuId"] for o in found.options]
            for candidate in ([context_sku] if context_ok else []) + quote_skus:
                if candidate in options:
                    return {"skuId": candidate,
                            "source": "selected product within the family "
                                      "the question names"}
            return {"clarify": {
                "question": "Which product do you mean?",
                "attribute": found.clarifyingAttribute or "product",
                "options": [{"skuId": s, "name": data.product(s).name}
                            for s in options[:8]],
            }}
        # The sentence named something the catalogue does not carry.
        return {"refuse": (INVALID_VALUE,
                           "The product in that question is not in the "
                           "catalogue.")}

    if context_ok:
        return {"skuId": context_sku, "source": "selected product"}
    if len(set(quote_skus)) == 1:
        return {"skuId": quote_skus[0], "source": "the only line on the quotation"}
    options = sorted(set(quote_skus))
    return {"clarify": {
        "question": "Which product is this about?",
        "attribute": "product",
        "options": [{"skuId": s, "name": data.product(s).name} for s in options[:8]],
    }}


# ---------------------------------------------------------------------------
# small arithmetic helpers - Decimal throughout
# ---------------------------------------------------------------------------

def _d(value) -> Decimal:
    return gst.to_decimal(value)


def _p(value: Decimal) -> float:
    return gst.out(value)


def _pct(part: Decimal, whole: Decimal) -> Optional[float]:
    if whole <= ZERO:
        return None
    return float(gst.paise(part / whole * HUNDRED))


def _margin(price: Decimal, cost: Decimal) -> dict:
    amount = price - cost
    percent = _pct(amount, price)
    status = margin_status(float(gst.paise(amount)),
                           percent if percent is not None else -100.0,
                           None, MARGIN_WARNING_PERCENT)
    return {"amount": _p(amount), "percent": percent, "status": status}


def _row(label: str, current, scenario, *, unit: str = "INR") -> dict:
    change = None
    if isinstance(current, (int, float)) and isinstance(scenario, (int, float)) \
            and not isinstance(current, bool) and not isinstance(scenario, bool):
        change = _p(_d(scenario) - _d(current)) if unit == "INR" else \
            round(float(_d(scenario) - _d(current)), 2)
    return {"label": label, "current": current, "scenario": scenario,
            "change": change, "unit": unit}


def _impact(label: str, value, *, unit: str = "INR") -> dict:
    return {"label": label, "value": value, "unit": unit}


def _cost_now(data: Dataset, sku: str, confirmed: Dict[str, float]) -> dict:
    if sku in confirmed:
        return {"cost": _d(confirmed[sku]), "source": "CONFIRMED_SUPPLIER_PRICE"}
    return {"cost": _d(current_cost(data, sku)), "source": "SEEDED_SUPPLIER_PRICE"}


def _decisions(confirmed: Dict[str, float], override: Optional[dict] = None
               ) -> List[dict]:
    costs = dict(confirmed)
    if override:
        costs.update(override)
    return [{"skuId": sku, "decision": "CONFIRMED", "currentPrice": float(cost)}
            for sku, cost in sorted(costs.items())]


def _plan(data: Dataset, budget: float, confirmed: Dict[str, float],
          override: Optional[dict] = None) -> dict:
    return build_purchase_plan(data, budget, _decisions(confirmed, override),
                               include_impact=False)


def _plan_summary(plan: dict) -> dict:
    return {
        "commitmentCost": plan["commitmentCost"],
        "restockCost": plan["restockCost"],
        "totalSpend": plan["totalSpend"],
        "remaining": plan["remaining"],
        "allCommitmentsFunded": plan["allCommitmentsFunded"],
        "restockSelectedCount": plan["counts"]["restockSelected"],
        "restockDeferredCount": plan["counts"]["restockDeferred"],
    }


def _rate(data: Dataset, sku: str) -> Decimal:
    treatment = gst.rate_for(data.product(sku))
    if treatment is None:
        raise ScenarioError(INVALID_VALUE, "No GST rate is configured for this "
                            "product, so the simulation cannot include GST.")
    return treatment["rate"]


def _quote_items(quote: Optional[dict]) -> List[dict]:
    return [{"skuId": l["skuId"], "quantity": l["quantity"]}
            for l in (quote or {}).get("lines") or []
            if l.get("skuId") and isinstance(l.get("quantity"), int)]


# ---------------------------------------------------------------------------
# the scenarios
# ---------------------------------------------------------------------------

def _supplier_cost(data, sku, change, confirmed, budget) -> dict:
    now = _cost_now(data, sku, confirmed)
    cost = now["cost"]
    if change["kind"] == "PERCENT":
        if change["value"] <= ZERO or change["value"] > HUNDRED * 2:
            raise ScenarioError(OUTSIDE_PLAUSIBLE_RANGE,
                                "A supplier cost change must be more than 0% "
                                "and at most 200%.")
        new = gst.paise(cost * (HUNDRED + change["direction"] * change["value"])
                        / HUNDRED)
    elif change["kind"] == "AMOUNT":
        new = cost + change["direction"] * change["value"]
    else:
        new = change["value"]
    if new <= ZERO or new < cost * MIN_COST_FACTOR or new > cost * MAX_COST_FACTOR:
        raise ScenarioError(
            OUTSIDE_PLAUSIBLE_RANGE,
            f"A supplier cost of {format_rupees(float(gst.paise(new)))} is "
            f"outside the range this simulator accepts "
            f"({format_rupees(float(gst.paise(cost * MIN_COST_FACTOR)))} to "
            f"{format_rupees(float(gst.paise(cost * MAX_COST_FACTOR)))}) for a "
            f"product that costs {format_rupees(float(gst.paise(cost)))} now.",
            {"requestedCost": _p(new), "currentCost": _p(cost),
             "minimumCost": _p(cost * MIN_COST_FACTOR),
             "maximumCost": _p(cost * MAX_COST_FACTOR)})

    product = data.product(sku)
    price = _d(product.sellingPrice)
    before, after = _margin(price, cost), _margin(price, new)
    base_plan = _plan(data, budget, confirmed)
    new_plan = _plan(data, budget, confirmed, {sku: float(gst.paise(new))})
    b, n = _plan_summary(base_plan), _plan_summary(new_plan)

    rows = [
        _row("Selling price", _p(price), _p(price)),
        _row("Supplier cost", _p(cost), _p(new)),
        _row("Margin per unit (taxable)", before["amount"], after["amount"]),
        _row("Margin %", before["percent"], after["percent"], unit="PERCENT"),
    ]
    impact = [
        _impact("Committed customer orders cost",
                _p(_d(n["commitmentCost"]) - _d(b["commitmentCost"]))),
        _impact("Restocking capacity change",
                _p(_d(n["restockCost"]) - _d(b["restockCost"]))),
        _impact("Restock lines funded", n["restockSelectedCount"], unit="COUNT"),
        _impact("Margin status", after["status"], unit="STATUS"),
    ]
    if after["status"] == "NEGATIVE_MARGIN":
        code, text = "DO_NOT_RESTOCK_AT_THIS_COST", (
            "At this cost every unit sells below cost. Review the selling "
            "price before buying more.")
    elif after["status"] == "LOW_MARGIN":
        code, text = "REVIEW_BEFORE_INCREASE", (
            f"Margin would fall below {MARGIN_WARNING_PERCENT:g}%. Consider "
            f"confirming stock at the current supplier price before the "
            f"increase, and review the selling price.")
    elif new > cost:
        code, text = "MONITOR", ("Margin falls but stays above the warning "
                                 "threshold.")
    else:
        code, text = "NO_ACTION_NEEDED", "The cost falls, so the margin improves."
    if not n["allCommitmentsFunded"] and b["allCommitmentsFunded"]:
        code, text = "COMMITMENTS_AT_RISK", (
            "At this cost the budget no longer funds every committed customer "
            "order.")
    return {
        "subject": {"skuId": sku, "name": product.name},
        "baseline": {"supplierCost": _p(cost), "costSource": now["source"],
                     "sellingPrice": _p(price), "budget": budget, "plan": b},
        "scenarioValues": {"supplierCost": _p(new), "plan": n},
        "rows": rows, "impact": impact,
        "decision": {"code": code, "text": text},
        "explanation": (
            f"{product.name}: supplier cost {format_rupees(_p(cost))} → "
            f"{format_rupees(_p(new))}. Margin per unit "
            f"{format_rupees(before['amount'])} → "
            f"{format_rupees(after['amount'])} at a selling price of "
            f"{format_rupees(_p(price))}, which is not changed."),
    }


def _selling_price(data, sku, change, confirmed) -> dict:
    product = data.product(sku)
    price = _d(product.sellingPrice)
    if change["kind"] == "PERCENT":
        if change["value"] <= ZERO or change["value"] > MAX_PRICE_CHANGE_PERCENT:
            raise ScenarioError(OUTSIDE_PLAUSIBLE_RANGE,
                                f"A selling price change must be more than 0% "
                                f"and at most {MAX_PRICE_CHANGE_PERCENT}%.")
        new = gst.paise(price * (HUNDRED + change["direction"] * change["value"])
                        / HUNDRED)
    elif change["kind"] == "AMOUNT":
        new = price + change["direction"] * change["value"]
    else:
        new = change["value"]
    limit = price * MAX_PRICE_CHANGE_PERCENT / HUNDRED
    if new <= ZERO or abs(new - price) > limit:
        raise ScenarioError(OUTSIDE_PLAUSIBLE_RANGE,
                            f"The simulator accepts a selling price within "
                            f"{MAX_PRICE_CHANGE_PERCENT}% of the current "
                            f"{format_rupees(_p(price))}.")
    cost = _cost_now(data, sku, confirmed)["cost"]
    rate = _rate(data, sku)
    before, after = _margin(price, cost), _margin(new, cost)
    tax_before = gst.tax_from_exclusive(price, rate)
    tax_after = gst.tax_from_exclusive(new, rate)
    rows = [
        _row("Selling price (taxable)", _p(price), _p(new)),
        _row("Customer pays incl. GST", tax_before["grandTotal"],
             tax_after["grandTotal"]),
        _row("Supplier cost", _p(cost), _p(cost)),
        _row("Margin per unit (taxable)", before["amount"], after["amount"]),
        _row("Margin %", before["percent"], after["percent"], unit="PERCENT"),
    ]
    impact = [_impact("Margin status", after["status"], unit="STATUS"),
              _impact("GST per unit", tax_after["totalTax"])]
    if after["status"] == "NEGATIVE_MARGIN":
        code, text = "BELOW_COST", "This price sells below supplier cost."
    elif after["status"] == "LOW_MARGIN":
        code, text = "LOW_MARGIN", (f"Margin would be below "
                                    f"{MARGIN_WARNING_PERCENT:g}%.")
    else:
        code, text = "MARGIN_ABOVE_THRESHOLD", (
            "Margin stays above the warning threshold. The shelf price is not "
            "changed by this simulation.")
    return {
        "subject": {"skuId": sku, "name": product.name},
        "baseline": {"sellingPrice": _p(price), "supplierCost": _p(cost)},
        "scenarioValues": {"sellingPrice": _p(new)},
        "rows": rows, "impact": impact,
        "decision": {"code": code, "text": text},
        "explanation": (
            f"{product.name}: selling price {format_rupees(_p(price))} → "
            f"{format_rupees(_p(new))}; margin per unit "
            f"{format_rupees(before['amount'])} → "
            f"{format_rupees(after['amount'])}."),
    }


def _gst_inclusive(data, sku, confirmed) -> dict:
    product = data.product(sku)
    price = _d(product.sellingPrice)
    cost = _cost_now(data, sku, confirmed)["cost"]
    rate = _rate(data, sku)
    on_top = gst.margin_after_gst(price, cost, rate, price_basis=gst.EXCLUSIVE)
    inside = gst.margin_after_gst(price, cost, rate, price_basis=gst.INCLUSIVE)
    rows = [
        _row("Customer pays", on_top["customerPays"], inside["customerPays"]),
        _row("Taxable sales value", on_top["taxableSales"], inside["taxableSales"]),
        _row("GST collected (not margin)", on_top["gstCollected"],
             inside["gstCollected"]),
        _row("Margin per unit", on_top["margin"], inside["margin"]),
        _row("Margin %", on_top["marginPercent"], inside["marginPercent"],
             unit="PERCENT"),
    ]
    status = _margin(_d(inside["taxableSales"]), cost)["status"]
    if inside["margin"] < 0:
        code, text = "KEEP_GST_ON_TOP", (
            "Including GST in the current price would sell below cost. Keep "
            "GST on top of the selling price.")
    elif status == "LOW_MARGIN":
        code, text = "KEEP_GST_ON_TOP", (
            f"Including GST in the current price leaves a margin below "
            f"{MARGIN_WARNING_PERCENT:g}%.")
    else:
        code, text = "MARGIN_ABOVE_THRESHOLD", (
            "Including GST in the price still leaves a margin above the "
            "warning threshold.")
    return {
        "subject": {"skuId": sku, "name": product.name},
        "baseline": {"sellingPrice": _p(price), "priceBasis": gst.EXCLUSIVE,
                     "supplierCost": _p(cost), "gstRate": float(rate)},
        "scenarioValues": {"priceBasis": gst.INCLUSIVE},
        "rows": rows,
        "impact": [_impact("Margin change from absorbing GST",
                           _p(_d(inside["margin"]) - _d(on_top["margin"]))),
                   _impact("Margin status", status, unit="STATUS")],
        "decision": {"code": code, "text": text},
        "explanation": (
            f"{product.name}: with GST on top the customer pays "
            f"{format_rupees(on_top['customerPays'])} and the margin is "
            f"{format_rupees(on_top['margin'])}. If "
            f"{format_rupees(_p(price))} already included GST, the taxable "
            f"value would be {format_rupees(inside['taxableSales'])} and the "
            f"margin {format_rupees(inside['margin'])}."),
    }


def _quote_margin(data, quote_dict: dict, confirmed) -> Decimal:
    total = ZERO
    for line in quote_dict["lines"]:
        cost = _cost_now(data, line["skuId"], confirmed)["cost"]
        total += _d(line["lineTotal"]) - cost * line["quantity"]
    return total


def _discount(data, params, quote, sku_info, confirmed) -> dict:
    kind, value = params["kind"], params["value"]
    if value <= ZERO:
        raise ScenarioError(INVALID_VALUE, "A discount must be more than zero.")
    items = _quote_items(quote)
    if not items:
        if not sku_info.get("skuId"):
            raise ScenarioError(INVALID_VALUE, "There is no quotation or "
                                "product selected to discount.")
        items = [{"skuId": sku_info["skuId"], "quantity": 1}]
    base = calculate_quote(data, items).as_dict()
    subtotal = _d(base["total"])
    discount = (gst.paise(subtotal * value / HUNDRED) if kind == "PERCENT"
                else gst.paise(value))
    percent = value if kind == "PERCENT" else _d(_pct(discount, subtotal) or 0)
    if percent > MAX_DISCOUNT_PERCENT:
        raise ScenarioError(DISCOUNT_ABOVE_LIMIT,
                            f"A {percent}% discount is above the configured "
                            f"limit of {MAX_DISCOUNT_PERCENT}%. The simulator "
                            f"will not model it.")
    if discount >= subtotal:
        raise ScenarioError(INVALID_VALUE, "The discount is larger than the "
                            "order value.")
    new_subtotal = subtotal - discount
    rate_set = {_rate(data, i["skuId"]) for i in items}
    if len(rate_set) != 1:
        raise ScenarioError(INVALID_VALUE, "These products carry different GST "
                            "rates, so a whole-order discount cannot be "
                            "simulated here.")
    rate = rate_set.pop()
    tax_before = gst.tax_from_exclusive(subtotal, rate)
    tax_after = gst.tax_from_exclusive(new_subtotal, rate)
    margin_before = _quote_margin(data, base, confirmed)
    margin_after = margin_before - discount
    rows = [
        _row("Taxable value", _p(subtotal), _p(new_subtotal)),
        _row("GST", tax_before["totalTax"], tax_after["totalTax"]),
        _row("Customer pays incl. GST", tax_before["grandTotal"],
             tax_after["grandTotal"]),
        _row("Shop margin (taxable)", _p(margin_before), _p(margin_after)),
    ]
    status = _margin(new_subtotal, new_subtotal - margin_after)["status"]
    if margin_after < ZERO:
        code, text = "BELOW_COST", "This discount sells the order below cost."
    elif status == "LOW_MARGIN":
        code, text = "LOW_MARGIN", (f"After this discount the order margin is "
                                    f"below {MARGIN_WARNING_PERCENT:g}%.")
    else:
        code, text = "MARGIN_ABOVE_THRESHOLD", ("The order keeps a margin above "
                                                "the warning threshold.")
    return {
        "subject": {"lines": len(items)},
        "baseline": {"taxableValue": _p(subtotal)},
        "scenarioValues": {"discount": _p(discount),
                           "discountPercent": float(gst.paise(percent))},
        "rows": rows,
        "impact": [_impact("Discount given", _p(discount)),
                   _impact("Margin given up", _p(discount)),
                   _impact("Margin status", status, unit="STATUS")],
        "decision": {"code": code, "text": text},
        "explanation": (
            f"A discount of {format_rupees(_p(discount))} takes the taxable "
            f"value from {format_rupees(_p(subtotal))} to "
            f"{format_rupees(_p(new_subtotal))}; the customer pays "
            f"{format_rupees(tax_after['grandTotal'])} including GST, and the "
            f"shop's margin falls by the full {format_rupees(_p(discount))}."),
    }


def _quantity(data, params, quote, sku_info, confirmed) -> dict:
    new_qty, old_qty = params["newQuantity"], params.get("oldQuantity")
    if not 0 < new_qty <= MAX_QUANTITY:
        raise ScenarioError(INVALID_VALUE, f"A quantity must be between 1 and "
                            f"{MAX_QUANTITY}.")
    items = _quote_items(quote)
    target = None
    if items and old_qty is not None:
        hits = [i for i in items if i["quantity"] == old_qty]
        if len(hits) == 1:
            target = hits[0]["skuId"]
    if target is None and sku_info.get("skuId"):
        target = sku_info["skuId"]
        if items and target not in {i["skuId"] for i in items}:
            items = items + [{"skuId": target, "quantity": old_qty or 1}]
    if target is None:
        raise ScenarioError(INVALID_VALUE, "Which line should change? Select a "
                            "product or open a quotation first.")
    if not items:
        items = [{"skuId": target, "quantity": old_qty or 1}]
    before_items = [dict(i) for i in items]
    if old_qty is not None:
        for i in before_items:
            if i["skuId"] == target:
                i["quantity"] = old_qty
    after_items = [dict(i, quantity=new_qty) if i["skuId"] == target else dict(i)
                   for i in before_items]
    before = calculate_quote(data, before_items).as_dict()
    after = calculate_quote(data, after_items).as_dict()
    gst_before = gst.quote_gst(data, before)
    gst_after = gst.quote_gst(data, after)
    line_b = next(l for l in before["lines"] if l["skuId"] == target)
    line_a = next(l for l in after["lines"] if l["skuId"] == target)
    margin_b = _quote_margin(data, before, confirmed)
    margin_a = _quote_margin(data, after, confirmed)
    rows = [
        _row("Quantity", line_b["quantity"], line_a["quantity"], unit="COUNT"),
        _row("Line value (taxable)", line_b["lineTotal"], line_a["lineTotal"]),
        _row("Quotation taxable total", before["total"], after["total"]),
        _row("Grand total incl. GST", gst_before.get("grandTotal"),
             gst_after.get("grandTotal")),
        _row("Shop margin (taxable)", _p(margin_b), _p(margin_a)),
        _row("Short of stock", line_b["shortageQty"], line_a["shortageQty"],
             unit="COUNT"),
    ]
    if line_a["shortageQty"] > 0:
        code, text = "BUY_BEFORE_DELIVERY", (
            f"Stock covers {line_a['onHand']}; {line_a['shortageQty']} more "
            f"must be bought before this can be delivered.")
    else:
        code, text = "IN_STOCK", "The new quantity is covered by stock on hand."
    name = data.product(target).name
    return {
        "subject": {"skuId": target, "name": name},
        "baseline": {"quantity": line_b["quantity"], "total": before["total"]},
        "scenarioValues": {"quantity": line_a["quantity"],
                           "total": after["total"]},
        "rows": rows,
        "impact": [_impact("Stock on hand", line_a["onHand"], unit="COUNT")],
        "decision": {"code": code, "text": text},
        "explanation": (
            f"{name}: {line_b['quantity']} → {line_a['quantity']}. The "
            f"quotation's taxable total moves from "
            f"{format_rupees(before['total'])} to "
            f"{format_rupees(after['total'])}."),
    }


def _budget(data, params, confirmed, budget) -> dict:
    try:
        new_budget = parse_budget(float(params["budget"]))
    except (InvalidBudgetError, ValueError) as exc:
        raise ScenarioError(INVALID_VALUE, str(exc)) from None
    base = _plan_summary(_plan(data, budget, confirmed))
    new_plan = _plan(data, new_budget, confirmed)
    new = _plan_summary(new_plan)
    cash = gst.plan_gst_view(data, new_plan)
    rows = [
        _row("Budget", budget, new_budget),
        _row("Committed customer orders", base["commitmentCost"],
             new["commitmentCost"]),
        _row("Restocking", base["restockCost"], new["restockCost"]),
        _row("Total spend (ex-GST)", base["totalSpend"], new["totalSpend"]),
        _row("Unspent", base["remaining"], new["remaining"]),
        _row("Restock lines funded", base["restockSelectedCount"],
             new["restockSelectedCount"], unit="COUNT"),
    ]
    impact = [_impact("Lines deferred", new["restockDeferredCount"], unit="COUNT")]
    if cash.get("available"):
        impact.append(_impact("Cash out incl. supplier GST",
                              cash["cashOutInclGst"]))
    if not new["allCommitmentsFunded"]:
        code, text = "COMMITMENTS_AT_RISK", (
            "This budget does not fund every committed customer order.")
    elif new["restockSelectedCount"] < base["restockSelectedCount"]:
        code, text = "FEWER_RESTOCKS", (
            "Every committed customer order is still funded; fewer restock "
            "lines fit.")
    else:
        code, text = "COMMITMENTS_FUNDED", ("Every committed customer order is "
                                            "funded.")
    return {
        "subject": {"budget": new_budget},
        "baseline": {"budget": budget, "plan": base},
        "scenarioValues": {"budget": new_budget, "plan": new},
        "rows": rows, "impact": impact,
        "decision": {"code": code, "text": text},
        "explanation": (
            f"With {format_rupees(new_budget)} instead of "
            f"{format_rupees(budget)}, the plan spends "
            f"{format_rupees(new['totalSpend'])} before GST and leaves "
            f"{format_rupees(new['remaining'])} unspent."),
    }


def _skip_restock(data, quote, sku_info, confirmed) -> dict:
    lines = [l for l in (quote or {}).get("lines") or []
             if (l.get("shortageQty") or 0) > 0]
    if not lines and sku_info.get("skuId"):
        sku = sku_info["skuId"]
        quote_lines = [l for l in (quote or {}).get("lines") or []
                       if l.get("skuId") == sku]
        lines = [l for l in quote_lines if (l.get("shortageQty") or 0) > 0]
    if not lines:
        raise ScenarioError(INVALID_VALUE, "There is no shortage on the current "
                            "quotation to skip restocking.")
    units = ZERO
    sales = ZERO
    margin = ZERO
    saved = ZERO
    for line in lines:
        short = _d(line["shortageQty"])
        price = _d(line["sellingPrice"])
        cost = _cost_now(data, line["skuId"], confirmed)["cost"]
        units += short
        sales += short * price
        margin += short * (price - cost)
        saved += short * cost
    rows = [_row("Units that cannot be delivered", 0, int(units), unit="COUNT"),
            _row("Sales at risk (taxable)", 0.0, _p(sales)),
            _row("Margin at risk", 0.0, _p(margin)),
            _row("Cash not spent on restocking", 0.0, _p(saved))]
    return {
        "subject": {"lines": [l["skuId"] for l in lines]},
        "baseline": {"shortageLines": len(lines)},
        "scenarioValues": {"restock": False},
        "rows": rows,
        "impact": [_impact("Lines short", len(lines), unit="COUNT")],
        "decision": {"code": "CUSTOMER_ORDER_INCOMPLETE", "text": (
            "Without restocking, this quotation cannot be delivered in full. "
            "Tell the customer before promising it.")},
        "explanation": (
            f"Skipping the restock saves {format_rupees(_p(saved))} of cash now "
            f"and puts {format_rupees(_p(sales))} of sales and "
            f"{format_rupees(_p(margin))} of margin at risk."),
    }


def _gst_view(data, text, quote, sku_info, confirmed) -> dict:
    mode = gst.detect_tax_mode(text)
    items = _quote_items(quote)
    if not items:
        if not sku_info.get("skuId"):
            raise ScenarioError(INVALID_VALUE, "Open a quotation or select a "
                                "product to see its GST.")
        items = [{"skuId": sku_info["skuId"], "quantity": 1}]
    q = calculate_quote(data, items).as_dict()
    tax = gst.quote_gst(data, q, mode["taxMode"], tax_mode_source=mode["source"])
    if not tax.get("available"):
        raise ScenarioError(INVALID_VALUE, "A GST rate is not configured for "
                            "every product here.")
    margin = _quote_margin(data, q, confirmed)
    rows = [
        _row("Taxable value", q["total"], tax["totalTaxableValue"]),
        _row("CGST", 0.0, tax["cgst"]),
        _row("SGST", 0.0, tax["sgst"]),
        _row("IGST", 0.0, tax["igst"]),
        _row("Customer pays", q["total"], tax["grandTotal"]),
        _row("Shop margin (taxable)", _p(margin), _p(margin)),
    ]
    return {
        "subject": {"lines": len(items), "taxMode": tax["taxMode"]},
        "baseline": {"taxableValue": q["total"]},
        "scenarioValues": {"taxMode": tax["taxMode"],
                           "grandTotal": tax["grandTotal"]},
        "rows": rows,
        "impact": [_impact("GST (not margin)", tax["totalGst"]),
                   _impact("Place of supply", tax["taxMode"], unit="STATUS")],
        "decision": {"code": "GST_SHOWN", "text": (
            "GST is collected for the government. The shop's margin is the "
            "same with or without it.")},
        "explanation": (
            f"Taxable value {format_rupees(q['total'])} plus GST "
            f"{format_rupees(tax['totalGst'])} = "
            f"{format_rupees(tax['grandTotal'])} for the customer. Margin stays "
            f"{format_rupees(_p(margin))}."),
        "gst": tax,
    }


# ---------------------------------------------------------------------------
# the entry point
# ---------------------------------------------------------------------------

NEEDS_PRODUCT = (SUPPLIER_COST_CHANGE, SELLING_PRICE_CHANGE, GST_INCLUSIVE_PRICE,
                 WALK_AWAY_PRICE)


def simulate(data: Dataset, text: str, *,
             confirmed_costs: Optional[Dict[str, float]] = None,
             quote: Optional[dict] = None,
             sku_id: Optional[str] = None,
             budget=None) -> dict:
    """Answer one what-if question. Pure: reads its inputs and returns a dict.

    `confirmed_costs` are the shop's confirmed supplier prices (SKU -> cost).
    `quote` is an existing quotation to reason about, and `sku_id` the product
    the owner has selected. `budget` is the cash the planner compares against.
    None of them is modified.
    """
    confirmed = {k: float(v) for k, v in (confirmed_costs or {}).items()
                 if k in data.products}
    try:
        base_budget = parse_budget(float(DEFAULT_BUDGET if budget in (None, "")
                                         else budget))
    except (InvalidBudgetError, TypeError, ValueError) as exc:
        return _finish(text, {"status": REFUSED, "reason": INVALID_VALUE,
                              "message": f"budget: {exc}"}, None)

    parsed = parse_scenario(text)
    if parsed["status"] != "OK":
        return _finish(text, parsed, None)

    kind, params = parsed["type"], parsed["params"]
    try:
        sku_info: dict = {}
        if kind in NEEDS_PRODUCT or kind in (DISCOUNT, QUANTITY_CHANGE,
                                             SKIP_RESTOCK, GST_VIEW):
            wants = kind in NEEDS_PRODUCT or not _quote_items(quote)
            if wants or kind == QUANTITY_CHANGE:
                sku_info = _resolve_sku(data, text, sku_id, quote)
                if "clarify" in sku_info and (kind in NEEDS_PRODUCT or
                                              not _quote_items(quote)):
                    c = sku_info["clarify"]
                    return _finish(text, {**parsed, "status": NEEDS_CLARIFICATION,
                                          "question": c["question"],
                                          "attribute": c["attribute"],
                                          "options": c["options"]}, None)
                if "refuse" in sku_info:
                    reason, message = sku_info["refuse"]
                    return _finish(text, {**parsed, "status": REFUSED,
                                          "reason": reason, "message": message},
                                   None)

        if kind == SUPPLIER_COST_CHANGE:
            body = _supplier_cost(data, sku_info["skuId"], params, confirmed,
                                  base_budget)
        elif kind == SELLING_PRICE_CHANGE:
            body = _selling_price(data, sku_info["skuId"], params, confirmed)
        elif kind == GST_INCLUSIVE_PRICE:
            body = _gst_inclusive(data, sku_info["skuId"], confirmed)
        elif kind == DISCOUNT:
            body = _discount(data, params, quote, sku_info, confirmed)
        elif kind == QUANTITY_CHANGE:
            body = _quantity(data, params, quote, sku_info, confirmed)
        elif kind == BUDGET_CHANGE:
            body = _budget(data, params, confirmed, base_budget)
        elif kind == SKIP_RESTOCK:
            body = _skip_restock(data, quote, sku_info, confirmed)
        elif kind == WALK_AWAY_PRICE:
            body = walk_away_price(
                data, sku_info["skuId"], params, confirmed, base_budget,
                budget_source="owner" if budget not in (None, "") else "default")
        else:
            body = _gst_view(data, text, quote, sku_info, confirmed)
    except ScenarioError as exc:
        return _finish(text, {**parsed, "status": REFUSED, "reason": exc.reason,
                              "message": str(exc), "details": exc.details},
                       None)

    if sku_info.get("source"):
        body["subject"]["selectedBy"] = sku_info["source"]
    return _finish(text, {**parsed, "status": SIMULATED}, body)


# ---------------------------------------------------------------------------
# walk-away price: the most the shop can pay the supplier
# ---------------------------------------------------------------------------

MARGIN_LIMIT = "MARGIN_LIMIT"
CASH_LIMIT = "CASH_LIMIT"


def walk_away_price(data, sku, params, confirmed, budget, *,
                    budget_source: str = "owner") -> dict:
    """The highest supplier cost the shop can accept for one product.

    Two ceilings, each a plain formula over the shop's own figures:

        margin ceiling = selling price x (1 - margin floor)
        cash ceiling   = (budget - other committed purchases)
                         / units still to buy for committed customer orders

    The lower one is the walk-away price, and whichever it is names the
    reason. The cash ceiling exists only when customer orders already commit
    the shop to buying this product; otherwise the margin decides alone. The
    supplier cost compared against it is the confirmed or seeded cost on
    record - a figure typed into the question is never used. Nothing is
    written: the planner runs on a copy, like every other scenario here.
    """
    product = data.product(sku)
    if product.sellingPrice is None or _d(product.sellingPrice) <= ZERO:
        raise ScenarioError(INVALID_VALUE, "This product has no selling price, "
                            "so no margin-based walk-away price exists.")
    floor = _d(params["marginFloorPercent"])
    if floor <= ZERO or floor > MAX_MARGIN_FLOOR_PERCENT:
        raise ScenarioError(INVALID_VALUE, "A margin floor must be more than "
                            f"0% and at most {MAX_MARGIN_FLOOR_PERCENT}%.")
    price = _d(product.sellingPrice)
    now = _cost_now(data, sku, confirmed)
    cost = now["cost"]

    margin_ceiling = gst.paise(price * (HUNDRED - floor) / HUNDRED)

    plan = _plan(data, budget, confirmed)
    mine = [c for c in plan["commitments"] if c["skuId"] == sku]
    committed_qty = sum(int(c.get("requestedQty") or 0) for c in mine)
    other = sum((_d(c.get("fullLineCost") or 0) for c in plan["commitments"]
                 if c["skuId"] != sku), ZERO)
    cash_ceiling = None
    if committed_qty > 0:
        spare = _d(budget) - other
        cash_ceiling = gst.paise(spare / committed_qty) if spare > ZERO else ZERO

    if cash_ceiling is not None and cash_ceiling < margin_ceiling:
        walk_away, binding = cash_ceiling, CASH_LIMIT
    else:
        walk_away, binding = margin_ceiling, MARGIN_LIMIT
    gap = cost - walk_away           # positive: current cost is above it
    floor_text = f"{float(floor):g}%"
    limit_words = (f"your {floor_text} margin limit" if binding == MARGIN_LIMIT
                   else f"what {format_rupees(_p(_d(budget)))} of cash can "
                        f"fund for committed orders")

    if walk_away <= ZERO:
        code, text = "CANNOT_FUND", (
            "The budget is already spent on other committed orders, so no "
            "supplier price for this product can be funded. Add cash or defer "
            "another purchase first.")
    elif gap > ZERO:
        code, text = "ABOVE_WALK_AWAY", (
            f"Current cost is {format_rupees(_p(gap))} above {limit_words}. "
            f"Negotiate down to {format_rupees(_p(walk_away))} or "
            + ("review the selling price before buying more."
               if binding == MARGIN_LIMIT else
               "add cash before buying more."))
    else:
        code, text = "WITHIN_WALK_AWAY", (
            f"Current cost is {format_rupees(_p(-gap))} below the walk-away "
            f"price. Do not accept a rise above {format_rupees(_p(walk_away))}.")

    rows = [
        _row("Selling price", _p(price), _p(price)),
        _row("Supplier cost now", _p(cost), _p(cost)),
        _row("Margin ceiling", None, _p(margin_ceiling)),
        _row("Cash ceiling", None,
             _p(cash_ceiling) if cash_ceiling is not None else None),
        _row("Walk-away price", None, _p(walk_away)),
    ]
    impact = [
        _impact("Walk-away price", _p(walk_away)),
        _impact("Binding limit", binding, unit="STATUS"),
        _impact("Current cost above walk-away" if gap > ZERO
                else "Headroom below walk-away", _p(abs(gap))),
        _impact("Margin at walk-away price", _p(price - walk_away)),
    ]
    return {
        "subject": {"skuId": sku, "name": product.name},
        "baseline": {"supplierCost": _p(cost), "costSource": now["source"],
                     "sellingPrice": _p(price), "budget": float(budget),
                     "budgetSource": budget_source,
                     "marginFloorPercent": float(floor),
                     "marginFloorSource": params.get("marginFloorSource"),
                     "committedQty": committed_qty,
                     "otherCommittedCost": _p(other)},
        "scenarioValues": {
            "walkAwayPrice": _p(walk_away),
            "bindingLimit": binding,
            "marginCeiling": _p(margin_ceiling),
            "cashCeiling": (_p(cash_ceiling) if cash_ceiling is not None
                            else None),
            "differenceFromCurrentCost": _p(gap),
            "currentCostAboveWalkAway": gap > ZERO,
            "formulas": {
                "marginCeiling": (f"{_p(price)} x (1 - {float(floor):g}/100) "
                                  f"= {_p(margin_ceiling)}"),
                "cashCeiling": (None if cash_ceiling is None else
                                f"({float(budget):g} - {_p(other)}) / "
                                f"{committed_qty} = {_p(cash_ceiling)}"),
            },
        },
        "rows": rows, "impact": impact,
        "decision": {"code": code, "text": text},
        "explanation": (
            f"Walk-away price: {format_rupees(_p(walk_away))} "
            f"({'margin' if binding == MARGIN_LIMIT else 'cash'} limit). "
            f"Current supplier cost: {format_rupees(_p(cost))}."),
    }


def _json_params(params: dict) -> dict:
    out = {}
    for key, value in (params or {}).items():
        out[key] = float(value) if isinstance(value, Decimal) else value
    return out


def _finish(text: str, parsed: dict, body: Optional[dict]) -> dict:
    status = parsed["status"] if parsed["status"] != "OK" else SIMULATED
    result = {
        "status": status,
        "scenarioType": parsed.get("type"),
        "interpretation": {
            "customerInput": " ".join(str(text or "").split())[:MAX_TEXT_CHARS],
            "interpretedBy": INTERPRETER,
            "modelUsed": False,
            "scenarioType": parsed.get("type"),
            "parameters": _json_params(parsed.get("params")),
        },
        "simulationOnly": True,
        "stateChanged": False,
        "notice": SIMULATION_NOTICE,
    }
    if status == NEEDS_CLARIFICATION:
        result["clarification"] = {
            "question": parsed.get("question"),
            "attribute": parsed.get("attribute") or None,
            "options": parsed.get("options") or [],
        }
        result["explanation"] = parsed.get("question")
    elif status == REFUSED:
        result["reason"] = parsed.get("reason")
        result["explanation"] = parsed.get("message")
        result["examples"] = list(EXAMPLES)
        if parsed.get("details"):
            result["limits"] = parsed["details"]
    else:
        result.update({
            "subject": body["subject"],
            "baseline": body["baseline"],
            "scenario": body["scenarioValues"],
            "rows": body["rows"],
            "impact": body["impact"],
            "decision": body["decision"],
            "explanation": body["explanation"],
        })
        if body.get("gst"):
            result["gst"] = body["gst"]
    names = [((body or {}).get("subject") or {}).get("name") or ""]
    grounded, unsupported = check_grounding(result, ignore=names)
    result["grounded"] = grounded
    result["ungroundedNumbers"] = unsupported
    result["decisionTrace"] = trace(result)
    return result


# ---------------------------------------------------------------------------
# grounding: every number in the words is a number in the result
# ---------------------------------------------------------------------------

_TEXT_NUMBER = re.compile(r"(?<![A-Za-z0-9])-?\d[\d,]*(?:\.\d+)?")


def _collect(value, bucket: set) -> None:
    if isinstance(value, bool):
        return
    if isinstance(value, (int, float, Decimal)):
        bucket.add(round(float(value), 2))
        bucket.add(round(abs(float(value)), 2))
    elif isinstance(value, dict):
        for v in value.values():
            _collect(v, bucket)
    elif isinstance(value, (list, tuple)):
        for v in value:
            _collect(v, bucket)
    elif isinstance(value, str):
        for m in _TEXT_NUMBER.finditer(value):
            try:
                bucket.add(round(abs(float(m.group().replace(",", ""))), 2))
            except ValueError:
                pass


def check_grounding(result: dict, ignore: List[str] = ()) -> tuple:
    """Does every figure in the explanation and decision appear in the result?

    The words are built from the figures, so this should always hold. It is
    checked anyway, on every response, so that a future edit which writes a
    number into a sentence without carrying it in the data fails loudly.
    Configured constants (the warning threshold, the discount ceiling) and the
    owner's own question count as grounded; product names are skipped because
    "1.5 sqmm" and "90m" are names, not figures.
    """
    known: set = set()
    for key in ("rows", "impact", "baseline", "scenario", "subject",
                "interpretation", "gst", "clarification", "examples",
                "limits"):
        _collect(result.get(key), known)
    for constant in (MARGIN_WARNING_PERCENT, MAX_DISCOUNT_PERCENT,
                     MAX_PRICE_CHANGE_PERCENT, MAX_QUANTITY,
                     MAX_MARGIN_FLOOR_PERCENT, 200, 0):
        _collect(constant, known)
    words = " ".join(str(result.get(k) or "") for k in ("explanation",))
    words += " " + str((result.get("decision") or {}).get("text") or "")
    for name in ignore or []:
        if name:
            words = words.replace(name, " ")
    unsupported = []
    for match in _TEXT_NUMBER.finditer(words):
        try:
            value = round(abs(float(match.group().replace(",", ""))), 2)
        except ValueError:
            continue
        if value not in known:
            unsupported.append(match.group())
    return (not unsupported, unsupported)


# ---------------------------------------------------------------------------
# the decision trace for a simulation (owner audience)
# ---------------------------------------------------------------------------

def trace(result: dict) -> dict:
    """CUSTOMER INPUT -> INTERPRETATION -> BASELINE -> SCENARIO -> IMPACT -> WORDS.

    Built from the result's own structured fields. There is no model prose to
    leave out, because no model was involved; the interpretation step says so.
    """
    interp = result.get("interpretation") or {}
    steps = [
        {"step": "CUSTOMER_INPUT", "text": interp.get("customerInput")},
        {"step": "SCENARIO_INTERPRETATION",
         "interpretedBy": interp.get("interpretedBy"),
         "modelUsed": False,
         "scenarioType": interp.get("scenarioType"),
         "parameters": interp.get("parameters"),
         "status": result.get("status")},
    ]
    if result.get("status") == SIMULATED:
        steps += [
            {"step": "BASELINE", "values": result.get("baseline")},
            {"step": "SCENARIO", "values": result.get("scenario")},
            {"step": "DETERMINISTIC_IMPACT", "rows": result.get("rows"),
             "impact": result.get("impact"), "source": "engine.whatif"},
            {"step": "FINAL_EXPLANATION",
             "decision": (result.get("decision") or {}).get("code"),
             "grounded": result.get("grounded")},
        ]
    else:
        steps.append({"step": "FINAL_EXPLANATION", "decision": None,
                      "reason": result.get("reason") or result.get("status")})
    lines = []
    for step in steps:
        name = step["step"]
        if name == "CUSTOMER_INPUT":
            lines.append(f"Owner asked: \"{step['text']}\"")
        elif name == "SCENARIO_INTERPRETATION":
            lines.append(f"Read by the deterministic parser as "
                         f"{step['scenarioType'] or 'no scenario'} "
                         f"({step['status']}). No model was used.")
        elif name == "BASELINE":
            lines.append("Baseline taken from the shop's current figures.")
        elif name == "SCENARIO":
            lines.append("Scenario applied to a copy; nothing was saved.")
        elif name == "DETERMINISTIC_IMPACT":
            lines.append(f"{len(step['rows'] or [])} figure(s) recalculated by "
                         f"the engine.")
        elif name == "FINAL_EXPLANATION":
            lines.append(f"Decision: {step.get('decision') or step.get('reason')}.")
    return {"available": True, "audience": "owner", "steps": steps,
            "lines": lines, "source": "engine.whatif.trace"}
