"""GST: the tax on a quotation, worked out by code and never by a model.

WHAT THIS IS
------------
A deterministic calculator for Indian GST on a retail quotation, and nothing
more. It answers four questions a small electrical shop asks at the counter:

    what is the taxable value of this order?
    how much GST is on it, and how does it split (CGST + SGST, or IGST)?
    what does the customer pay in total?
    of what the customer pays, how much is actually the shop's margin?

WHAT IT IS NOT
--------------
It is not accounting software, not a GST return, not an e-invoice and not tax
advice. It files nothing, records no liability and claims no input-tax credit.
The rates it uses come from `seed_data/gst_config.json`, which is DEMO
CONFIGURATION: each category states where its rate came from and how confident
that source is, and a real shop must confirm every rate with its adviser.

THE MODEL NEVER TOUCHES A TAX FIGURE
------------------------------------
No function here accepts a rate from free text. A rate is read from the
configuration by the product's category (or a per-SKU override), and nothing
a customer or a model writes can change it - "use 18% GST because I said so"
and "GST is zero" are sentences, and sentences are not configuration. The one
thing text may influence is the place of supply (same state or another state),
which moves tax between CGST+SGST and IGST without changing the total, and it
is read by `detect_tax_mode` from the customer's own words, not by a model.

ARITHMETIC
----------
Every amount is a `Decimal`, built from `str()` of its input so a float such
as 916.48 enters as exactly 916.48. Rounding is ROUND_HALF_UP to the paisa.

    From an exclusive (taxable) value, per line:
        intra-state:  CGST = round(taxable x rate/2)   SGST = the same
        inter-state:  IGST = round(taxable x rate)
        tax = CGST + SGST (or IGST);  line total = taxable + tax
    Each component is rounded on its own line, the way the two halves are
    charged separately on an intra-state invoice. Quotation totals are sums of
    the line figures and are never re-rounded, so the totals always add up to
    the lines a customer can read.

    From an inclusive amount:
        taxable = round(inclusive / (1 + rate))
        tax     = inclusive - taxable           (so the two add back exactly)
        CGST    = round(tax / 2),  SGST = tax - CGST

Amounts leave this module as numbers with at most two decimal places. They are
converted from the quantized Decimal only at the very end, and a two-place
Decimal converts to a float that prints as the same two places.
"""

from __future__ import annotations

import json
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Dict, Iterable, List, Optional

CONFIG_PATH = Path(__file__).resolve().parents[1] / "seed_data" / "gst_config.json"

# Place of supply.
INTRA_STATE = "INTRA_STATE"      # CGST + SGST
INTER_STATE = "INTER_STATE"      # IGST
ZERO_RATED = "ZERO_RATED"        # export / SEZ supply: no tax is charged
TAX_MODES = (INTRA_STATE, INTER_STATE, ZERO_RATED)

# How a product is treated.
TAXABLE = "TAXABLE"
EXEMPT = "EXEMPT"
NIL_RATED = "NIL_RATED"
TREATMENTS = (TAXABLE, EXEMPT, NIL_RATED)

# Whether a stated price already includes GST.
EXCLUSIVE = "EXCLUSIVE"
INCLUSIVE = "INCLUSIVE"

# A rate outside this range is a configuration error, not a tax rate.
MAX_RATE_PERCENT = Decimal("40")

PAISA = Decimal("0.01")
HUNDRED = Decimal("100")
TWO = Decimal("2")
ZERO = Decimal("0")

NOT_TAX_ADVICE = ("GST treatment is demo configuration, not tax advice. "
                  "ShopFlow does not file returns or keep tax records.")
MARGIN_NOTE = ("GST collected from the customer is owed to the government. "
               "It is not the shop's margin, so margin is measured on the "
               "taxable value.")


class GstConfigError(ValueError):
    """The tax configuration is missing, malformed or out of range."""


class GstInputError(ValueError):
    """An amount or mode handed to the calculator cannot be used."""


# ---------------------------------------------------------------------------
# numbers
# ---------------------------------------------------------------------------

def to_decimal(value, *, field: str = "amount") -> Decimal:
    """A money amount as an exact Decimal. Never from a binary float's bits."""
    if isinstance(value, bool) or value is None:
        raise GstInputError(f"{field} must be a number")
    if isinstance(value, Decimal):
        amount = value
    else:
        try:
            amount = Decimal(str(value).strip())
        except (InvalidOperation, ValueError):
            raise GstInputError(f"{field} must be a number") from None
    if not amount.is_finite():
        raise GstInputError(f"{field} must be a finite number")
    return amount


def paise(value: Decimal) -> Decimal:
    """Round half up to the paisa. The only rounding rule in this module."""
    return value.quantize(PAISA, rounding=ROUND_HALF_UP)


def out(value: Decimal) -> float:
    """A two-place Decimal, as the number an API response carries."""
    return float(paise(value))


def rate_text(rate: Decimal) -> str:
    """18 -> "18", 2.5 -> "2.5". For labels only; never parsed back."""
    text = format(rate.normalize(), "f")
    return text


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------

def _validate_rate(raw, where: str) -> Decimal:
    try:
        rate = to_decimal(raw, field=f"rate for {where}")
    except GstInputError as exc:
        raise GstConfigError(str(exc)) from None
    if rate < ZERO or rate > MAX_RATE_PERCENT:
        raise GstConfigError(f"rate for {where} must be between 0 and "
                             f"{MAX_RATE_PERCENT}")
    return rate


def validate_config(config: dict) -> dict:
    """Check a tax configuration and return it with Decimal rates.

    A bad configuration is refused outright. A tax figure computed from a
    half-valid table would look exactly as trustworthy as a correct one.
    """
    if not isinstance(config, dict):
        raise GstConfigError("GST configuration must be an object")
    basis = config.get("priceBasis")
    if basis not in (EXCLUSIVE, INCLUSIVE):
        raise GstConfigError("priceBasis must be EXCLUSIVE or INCLUSIVE")
    default_mode = config.get("defaultTaxMode", INTRA_STATE)
    if default_mode not in (INTRA_STATE, INTER_STATE):
        raise GstConfigError("defaultTaxMode must be INTRA_STATE or INTER_STATE")

    def entries(section: str) -> Dict[str, dict]:
        rows = config.get(section) or {}
        if not isinstance(rows, dict):
            raise GstConfigError(f"{section} must be an object")
        cleaned = {}
        for key, row in rows.items():
            if not isinstance(row, dict):
                raise GstConfigError(f"{section}.{key} must be an object")
            treatment = row.get("treatment", TAXABLE)
            if treatment not in TREATMENTS:
                raise GstConfigError(f"{section}.{key} has unknown treatment "
                                     f"{treatment!r}")
            rate = _validate_rate(row.get("rate"), f"{section}.{key}")
            if treatment in (EXEMPT, NIL_RATED) and rate != ZERO:
                raise GstConfigError(f"{section}.{key} is {treatment} and must "
                                     f"carry a rate of 0")
            cleaned[key] = {**row, "rate": rate, "treatment": treatment}
        return cleaned

    return {
        **config,
        "priceBasis": basis,
        "defaultTaxMode": default_mode,
        "categories": entries("categories"),
        "skuOverrides": entries("skuOverrides"),
    }


@lru_cache(maxsize=1)
def load_config() -> dict:
    """The shop's tax configuration, validated once per process."""
    try:
        raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise GstConfigError(f"GST configuration could not be read: "
                             f"{type(exc).__name__}") from None
    return validate_config(raw)


def rate_for(product, config: Optional[dict] = None) -> Optional[dict]:
    """The configured GST treatment of one product, or None if there is none.

    A per-SKU override wins over the category. A product whose category is not
    configured has NO rate - it is not assumed to be 18%, because a guessed
    rate is exactly the thing this module exists not to produce.
    """
    config = config or load_config()
    row = (config["skuOverrides"].get(getattr(product, "skuId", ""))
           or config["categories"].get(getattr(product, "category", "")))
    if row is None:
        return None
    return {
        "rate": row["rate"],
        "treatment": row["treatment"],
        "hsnHeading": row.get("hsnHeading"),
        "confidence": row.get("confidence"),
        "source": ("skuOverride"
                   if getattr(product, "skuId", "") in config["skuOverrides"]
                   else "category"),
    }


# ---------------------------------------------------------------------------
# place of supply, read from the customer's own words
# ---------------------------------------------------------------------------

_INTER_CUES = re.compile(
    r"\b(inter[\s-]?state|igst|other\s+state|another\s+state|different\s+state|"
    r"out\s+of\s+state|outside\s+(?:tamil\s*nadu|tn|the\s+state|our\s+state|state)|"
    r"vera\s+state|veru\s+state|dusre\s+rajya)\b|வேறு\s*மாநில|दूसरे\s*राज्य",
    re.IGNORECASE)
_INTRA_CUES = re.compile(
    r"\b(intra[\s-]?state|same\s+state|within\s+(?:tamil\s*nadu|tn|the\s+state)|"
    r"local\s+(?:customer|sale)|cgst|sgst)\b",
    re.IGNORECASE)

# How the customer wants the price shown. A display preference, never a rate:
# "without GST" does not make anything exempt, it asks to see the pre-tax
# figure first.
_SHOW_INCLUSIVE = re.compile(
    r"\b(gst\s+(?:included|inclusive)|incl(?:uding|usive)?\.?\s+(?:of\s+)?gst|"
    r"with\s+gst|add\s+gst|plus\s+gst|final\s+(?:customer\s+)?amount|"
    r"gst\s+bill|total\s+with\s+tax)\b", re.IGNORECASE)
_SHOW_EXCLUSIVE = re.compile(
    r"\b(without\s+gst|excl(?:uding|usive)?\.?\s+(?:of\s+)?gst|before\s+gst|"
    r"pre[\s-]?gst|ex[\s-]?gst)\b", re.IGNORECASE)


def detect_tax_mode(text: str, default: Optional[str] = None) -> dict:
    """Same state or another state, read from what the customer wrote.

    Returns the mode, what it was read from, and whether the text was
    contradictory. It never returns ZERO_RATED: a zero-rated supply is an
    export or SEZ fact the shop sets deliberately, not something a sentence in
    an order can switch on.
    """
    default = default or load_config()["defaultTaxMode"]
    inter = bool(_INTER_CUES.search(text or ""))
    intra = bool(_INTRA_CUES.search(text or ""))
    if inter and intra:
        return {"taxMode": default, "source": "default",
                "conflicting": True,
                "note": "The order mentions both the same state and another "
                        "state. The shop's default was used; please confirm "
                        "the place of supply."}
    if inter:
        return {"taxMode": INTER_STATE, "source": "customer order text",
                "conflicting": False, "note": None}
    if intra:
        return {"taxMode": INTRA_STATE, "source": "customer order text",
                "conflicting": False, "note": None}
    return {"taxMode": default, "source": "default", "conflicting": False,
            "note": None}


def detect_display(text: str) -> Optional[str]:
    """INCLUSIVE, EXCLUSIVE or None: which total the customer asked to see."""
    inclusive = bool(_SHOW_INCLUSIVE.search(text or ""))
    exclusive = bool(_SHOW_EXCLUSIVE.search(text or ""))
    if inclusive == exclusive:
        return None
    return INCLUSIVE if inclusive else EXCLUSIVE


# ---------------------------------------------------------------------------
# the two directions
# ---------------------------------------------------------------------------

def _check_mode(tax_mode: str) -> str:
    if tax_mode not in TAX_MODES:
        raise GstInputError(f"taxMode must be one of {', '.join(TAX_MODES)}")
    return tax_mode


def _split(taxable: Decimal, rate: Decimal, tax_mode: str) -> Dict[str, Decimal]:
    """CGST/SGST/IGST on one taxable value, each rounded on its own."""
    if tax_mode == ZERO_RATED or rate == ZERO:
        return {"cgst": ZERO, "sgst": ZERO, "igst": ZERO}
    if tax_mode == INTER_STATE:
        return {"cgst": ZERO, "sgst": ZERO,
                "igst": paise(taxable * rate / HUNDRED)}
    half = paise(taxable * rate / HUNDRED / TWO)
    return {"cgst": half, "sgst": half, "igst": ZERO}


def tax_from_exclusive(taxable_amount, rate_percent,
                       tax_mode: str = INTRA_STATE) -> dict:
    """GST on a GST-exclusive (taxable) amount.

    1000 at 18%, intra-state:  CGST 90, SGST 90, total 1180.
    1000 at 18%, inter-state:  IGST 180, total 1180.
    """
    _check_mode(tax_mode)
    taxable = paise(to_decimal(taxable_amount, field="taxable amount"))
    if taxable < ZERO:
        raise GstInputError("taxable amount must not be negative")
    rate = _validate_rate_input(rate_percent)
    parts = _split(taxable, rate, tax_mode)
    tax = parts["cgst"] + parts["sgst"] + parts["igst"]
    effective = ZERO if tax_mode == ZERO_RATED else rate
    return {
        "taxableAmount": out(taxable),
        "gstRate": float(effective),
        "cgst": out(parts["cgst"]),
        "sgst": out(parts["sgst"]),
        "igst": out(parts["igst"]),
        "totalTax": out(tax),
        "grandTotal": out(taxable + tax),
        "taxMode": tax_mode,
        "priceBasis": EXCLUSIVE,
        "evidence": _evidence(taxable, effective, parts, tax_mode),
    }


def tax_from_inclusive(inclusive_amount, rate_percent,
                       tax_mode: str = INTRA_STATE) -> dict:
    """The taxable value and GST inside a GST-inclusive amount.

    taxable = inclusive / (1 + rate), rounded; tax = inclusive - taxable, so
    the two always add back to exactly what the customer paid.
    """
    _check_mode(tax_mode)
    inclusive = paise(to_decimal(inclusive_amount, field="inclusive amount"))
    if inclusive < ZERO:
        raise GstInputError("inclusive amount must not be negative")
    rate = _validate_rate_input(rate_percent)
    effective = ZERO if tax_mode == ZERO_RATED else rate
    taxable = paise(inclusive / (Decimal(1) + effective / HUNDRED))
    tax = inclusive - taxable
    if tax_mode == INTER_STATE:
        parts = {"cgst": ZERO, "sgst": ZERO, "igst": tax}
    elif tax == ZERO:
        parts = {"cgst": ZERO, "sgst": ZERO, "igst": ZERO}
    else:
        cgst = paise(tax / TWO)
        parts = {"cgst": cgst, "sgst": tax - cgst, "igst": ZERO}
    return {
        "taxableAmount": out(taxable),
        "gstRate": float(effective),
        "cgst": out(parts["cgst"]),
        "sgst": out(parts["sgst"]),
        "igst": out(parts["igst"]),
        "totalTax": out(tax),
        "grandTotal": out(inclusive),
        "taxMode": tax_mode,
        "priceBasis": INCLUSIVE,
        "evidence": {
            "taxable": (f"{paise(inclusive)} / (1 + {rate_text(effective)}/100)"
                        f" = {paise(taxable)}"),
            "tax": f"{paise(inclusive)} - {paise(taxable)} = {paise(tax)}",
            "rounding": "half up, to the paisa",
        },
    }


def _validate_rate_input(rate_percent) -> Decimal:
    rate = to_decimal(rate_percent, field="GST rate")
    if rate < ZERO or rate > MAX_RATE_PERCENT:
        raise GstInputError(f"GST rate must be between 0 and {MAX_RATE_PERCENT}")
    return rate


def _evidence(taxable: Decimal, rate: Decimal, parts: Dict[str, Decimal],
              tax_mode: str) -> dict:
    if tax_mode == ZERO_RATED:
        tax_line = "zero-rated supply: no GST charged"
    elif tax_mode == INTER_STATE:
        tax_line = (f"IGST = {paise(taxable)} x {rate_text(rate)}% = "
                    f"{paise(parts['igst'])}")
    else:
        tax_line = (f"CGST = SGST = {paise(taxable)} x {rate_text(rate / TWO)}% "
                    f"= {paise(parts['cgst'])}")
    return {"tax": tax_line, "rounding": "half up, to the paisa, per line"}


# ---------------------------------------------------------------------------
# a quotation
# ---------------------------------------------------------------------------

def quote_gst(data, quote: dict, tax_mode: Optional[str] = None,
              config: Optional[dict] = None, *,
              tax_mode_source: str = "default",
              display: Optional[str] = None) -> dict:
    """GST for a whole quotation, line by line, from the engine's own figures.

    The taxable value of each line IS the quotation's `lineTotal` - the figure
    the customer was quoted - so this block can never disagree with the
    quotation it describes. Nothing on the quotation is rewritten, and its
    `total` stays the taxable subtotal it always was.

    If any line's product has no configured rate, the block reports that GST
    is unavailable rather than taxing the rest and presenting a partial total
    as the grand total.
    """
    config = config or load_config()
    tax_mode = _check_mode(tax_mode or config["defaultTaxMode"])
    lines_in = (quote or {}).get("lines") or []

    lines: List[dict] = []
    missing: List[str] = []
    totals = {"taxable": ZERO, "cgst": ZERO, "sgst": ZERO, "igst": ZERO}
    for line in lines_in:
        sku = line.get("skuId")
        product = data.products.get(sku) if data is not None else None
        treatment = rate_for(product, config) if product is not None else None
        if treatment is None:
            missing.append(str(sku))
            continue
        taxable = paise(to_decimal(line.get("lineTotal"), field="lineTotal"))
        rate = treatment["rate"]
        effective = ZERO if tax_mode == ZERO_RATED else rate
        parts = _split(taxable, effective, tax_mode)
        tax = parts["cgst"] + parts["sgst"] + parts["igst"]
        for key in ("cgst", "sgst", "igst"):
            totals[key] += parts[key]
        totals["taxable"] += taxable
        lines.append({
            "skuId": sku,
            "quantity": line.get("quantity"),
            "unitPrice": line.get("sellingPrice"),
            "taxableValue": out(taxable),
            "gstRate": float(effective),
            "treatment": treatment["treatment"],
            "hsnHeading": treatment["hsnHeading"],
            "cgst": out(parts["cgst"]),
            "sgst": out(parts["sgst"]),
            "igst": out(parts["igst"]),
            "taxAmount": out(tax),
            "lineTotal": out(taxable + tax),
            "evidence": {
                "taxable": f"{line.get('quantity')} x {line.get('sellingPrice')}"
                           f" = {paise(taxable)}",
                **_evidence(taxable, effective, parts, tax_mode),
            },
        })

    if missing:
        return {
            "available": False,
            "reason": "GST_RATE_NOT_CONFIGURED",
            "unconfiguredSkus": missing,
            "taxMode": tax_mode,
            "notice": NOT_TAX_ADVICE,
        }

    total_tax = totals["cgst"] + totals["sgst"] + totals["igst"]
    rates = sorted({l["gstRate"] for l in lines})
    return {
        "available": True,
        "taxMode": tax_mode,
        "taxModeSource": tax_mode_source,
        "priceBasis": config["priceBasis"],
        "display": display,
        "lines": lines,
        "subtotal": out(totals["taxable"]),
        "totalTaxableValue": out(totals["taxable"]),
        "cgst": out(totals["cgst"]),
        "sgst": out(totals["sgst"]),
        "igst": out(totals["igst"]),
        "totalGst": out(total_tax),
        "grandTotal": out(totals["taxable"] + total_tax),
        "rates": rates,
        "configStatus": config.get("status") or "DEMO_CONFIGURATION",
        "configVersion": config.get("configVersion"),
        "notice": NOT_TAX_ADVICE,
        "evidence": {
            "subtotal": " + ".join(str(paise(to_decimal(l["taxableValue"])))
                                   for l in lines) + f" = {paise(totals['taxable'])}",
            "totalGst": " + ".join(str(paise(to_decimal(l["taxAmount"])))
                                   for l in lines) + f" = {paise(total_tax)}",
            "grandTotal": f"{paise(totals['taxable'])} + {paise(total_tax)} = "
                          f"{paise(totals['taxable'] + total_tax)}",
            "source": "engine.gst.quote_gst",
        },
    }


# ---------------------------------------------------------------------------
# margin: GST is not the shop's money
# ---------------------------------------------------------------------------

def margin_after_gst(price, cost, rate_percent, *,
                     price_basis: str = EXCLUSIVE,
                     tax_mode: str = INTRA_STATE) -> dict:
    """What the shop earns on one sale once GST is set aside.

    `price` is the selling price as stated - exclusive (the demo catalogue) or
    inclusive (a shelf price that already contains GST). `cost` is the
    GST-exclusive supplier cost. Margin is always taxable sales minus cost:

        customer pays 11,800 incl. 18%  ->  taxable 10,000, GST 1,800
        cost 8,000                      ->  margin 2,000 - NOT 3,800
    """
    if price_basis == INCLUSIVE:
        tax = tax_from_inclusive(price, rate_percent, tax_mode)
    elif price_basis == EXCLUSIVE:
        tax = tax_from_exclusive(price, rate_percent, tax_mode)
    else:
        raise GstInputError("price basis must be EXCLUSIVE or INCLUSIVE")
    cost_d = paise(to_decimal(cost, field="cost"))
    if cost_d < ZERO:
        raise GstInputError("cost must not be negative")
    taxable = to_decimal(tax["taxableAmount"])
    margin = taxable - cost_d
    percent = (paise(margin / taxable * HUNDRED) if taxable > ZERO else None)
    return {
        "customerPays": tax["grandTotal"],
        "taxableSales": tax["taxableAmount"],
        "gstCollected": tax["totalTax"],
        "gstRate": tax["gstRate"],
        "cost": out(cost_d),
        "margin": out(margin),
        "marginPercent": None if percent is None else float(percent),
        "priceBasis": price_basis,
        "taxMode": tax_mode,
        "gstCountedAsMargin": False,
        "note": MARGIN_NOTE,
        "evidence": {
            **tax["evidence"],
            "margin": f"{paise(taxable)} - {cost_d} = {paise(margin)}",
            "marginPercent": (None if percent is None else
                              f"{paise(margin)} / {paise(taxable)} x 100 = "
                              f"{percent}%"),
        },
    }


# ---------------------------------------------------------------------------
# the purchase plan: what the cash has to cover
# ---------------------------------------------------------------------------

def plan_gst_view(data, plan: dict, config: Optional[dict] = None) -> dict:
    """The GST beside a purchase plan. Changes nothing about the plan.

    The allocator spends its budget in GST-exclusive supplier cost, exactly as
    before. This reports what that plan means for cash once the supplier's GST
    invoice arrives:

        purchase cost (ex-GST)   the plan's own totalSpend
        input GST                GST the supplier will charge on it
        cash out, incl. GST      what actually leaves the shop's account

    Input GST may be recoverable as input-tax credit against the GST the shop
    collects from customers, subject to eligibility and filing. Whether and
    when it is recovered is outside this planner, which is why it is shown
    as cash that has to be found now, and never netted off the plan.
    """
    config = config or load_config()
    lines = list((plan or {}).get("commitments") or []) + \
        list((plan or {}).get("restockSelected") or [])

    purchases = ZERO
    input_gst = ZERO
    missing: List[str] = []
    for line in lines:
        cost = line.get("lineCost")
        if not cost:
            continue
        product = data.products.get(line.get("skuId"))
        treatment = rate_for(product, config) if product is not None else None
        if treatment is None:
            missing.append(str(line.get("skuId")))
            continue
        amount = paise(to_decimal(cost, field="lineCost"))
        purchases += amount
        input_gst += paise(amount * treatment["rate"] / HUNDRED)

    budget = paise(to_decimal((plan or {}).get("budget") or 0, field="budget"))
    if missing:
        return {"available": False, "reason": "GST_RATE_NOT_CONFIGURED",
                "unconfiguredSkus": missing, "notice": NOT_TAX_ADVICE}

    cash_out = purchases + input_gst
    shortfall = cash_out - budget
    return {
        "available": True,
        "purchaseCostExGst": out(purchases),
        "inputGst": out(input_gst),
        "cashOutInclGst": out(cash_out),
        "budget": out(budget),
        "budgetCoversGst": shortfall <= ZERO,
        "extraCashForGst": out(shortfall) if shortfall > ZERO else 0.0,
        "allocationBasis": "GST-exclusive supplier cost",
        "inputTaxNote": ("Input GST may be claimable as input-tax credit, "
                         "subject to eligibility. It is not netted off this "
                         "plan."),
        "notice": NOT_TAX_ADVICE,
        "evidence": {
            "cashOut": f"{paise(purchases)} + {paise(input_gst)} = "
                       f"{paise(cash_out)}",
            "source": "engine.gst.plan_gst_view",
        },
    }


def configured_rates(data, config: Optional[dict] = None) -> Iterable[dict]:
    """One row per catalogue category: its rate and how sure the source is."""
    config = config or load_config()
    seen = sorted({p.category for p in data.products.values()})
    for category in seen:
        row = config["categories"].get(category)
        yield {
            "category": category,
            "rate": None if row is None else float(row["rate"]),
            "treatment": None if row is None else row["treatment"],
            "hsnHeading": None if row is None else row.get("hsnHeading"),
            "confidence": "NOT_CONFIGURED" if row is None else row.get("confidence"),
        }
