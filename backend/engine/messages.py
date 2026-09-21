"""Customer-facing messages, built from figures the engine already produced.

WHAT THIS MODULE IS
-------------------
The text a customer receives. Three kinds and no more: a quotation, an order
confirmation, and a statement of where their khata account stands.

It is a renderer. Every number in every sentence below is interpolated from a
value that was calculated elsewhere - by `quote.calculate_quote`, by
`credit.check_credit` - and nothing here adds, multiplies, compares or rounds
a business figure. There is no path from this file to a model, a store or a
network, which is asserted structurally by a test.

THE LINE BETWEEN INTERNAL AND CUSTOMER-FACING
---------------------------------------------
This is the part that matters, and it is why the projection below exists
rather than the builders reading the quote dict directly.

A ShopFlow quotation line carries the shop's own working: what the shop pays
its supplier, how fast the item sells, how many weeks of cover are left, what
margin the line earns. A customer must never see any of it. Sending a
contractor a message that quietly reveals the shop buys a coil at 5,900 and
sells it at 6,608 would damage the relationship the product exists to serve.

So `customer_safe_line` is an allow-list, not a deny-list. A field is absent
from a customer message unless it is named here, which means a future field
added to a quotation line is invisible to this file by default rather than
leaking until someone notices.

UNITS
-----
The unit comes from the quotation and is only lower-cased and pluralised.
"3 COIL" becomes "3 coils". It is never converted, never restated in another
unit, and never replaced by an equivalent - the customer is quoted for what
they will receive.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

# The three message types this adapter will send. Deliberately short: there is
# no marketing, no bulk send, no conversation and no support agent here.
QUOTATION = "QUOTATION"
ORDER_CONFIRMATION = "ORDER_CONFIRMATION"
CREDIT_STATUS = "CREDIT_STATUS"
CREDIT_REMINDER = "CREDIT_REMINDER"

MESSAGE_TYPES = (QUOTATION, ORDER_CONFIRMATION, CREDIT_STATUS, CREDIT_REMINDER)

# Quotation-line fields a customer message may read. Anything not on this list
# is shop-internal and never rendered.
CUSTOMER_SAFE_LINE_FIELDS = (
    "name", "quantity", "catalogueUom", "unit", "lineTotal",
)

# Named so a test can assert none of them ever reaches a customer message.
# These are the shop's working, not the customer's business.
INTERNAL_ONLY_FIELDS = (
    "costPrice", "currentSupplierCost", "unitCost", "supplierId",
    "supplierName", "marginPerRupee", "marginPerUnit", "onHand",
    "weeklyVelocity", "coverageWeeks", "stockoutRisk", "priority",
    "baseEquivalent", "evidence", "shortageQty",
)

MAX_MESSAGE_CHARS = 3500
MAX_LINES_IN_MESSAGE = 25

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


class InvalidMessageRequest(ValueError):
    """The message cannot be built from what was supplied."""


# ---------------------------------------------------------------------------
# formatting
# ---------------------------------------------------------------------------

def format_rupees(value) -> str:
    """Indian digit grouping, for a figure the engine produced.

    The single implementation in the codebase; `engine.voice` renders spoken
    amounts through this one too, so a rupee figure reads the same whether it
    is spoken, shown or sent.
    """
    try:
        amount = round(float(value) + 0.0, 2)
    except (TypeError, ValueError):
        return ""
    sign = "-" if amount < 0 else ""
    whole = f"{abs(amount):,.2f}"
    integer, _, frac = whole.partition(".")
    integer = integer.replace(",", "")
    if len(integer) > 3:
        head, tail = integer[:-3], integer[-3:]
        head = re.sub(r"(\d)(?=(\d\d)+$)", r"\1,", head)
        integer = f"{head},{tail}"
    return f"{sign}₹{integer}.{frac}"


def format_units(quantity, uom) -> str:
    """"3 coils", from the quotation's own quantity and unit.

    Lower-cased and pluralised for reading. Never converted: the unit shown to
    the customer is the unit they are being sold in.
    """
    unit = str(uom or "").strip().lower() or "unit"
    try:
        count = float(quantity)
    except (TypeError, ValueError):
        return f"{quantity} {unit}"
    plural = "" if count == 1 else "s"
    shown = int(count) if count == int(count) else count
    return f"{shown} {unit}{plural}"


def normalize_phone(phone) -> str:
    """A customer's number in E.164, or an error.

    Validated rather than cleaned up hopefully: a message sent to a mistyped
    number reaches a stranger, so anything that is not plainly a country code
    followed by a national number is refused.
    """
    raw = str(phone or "")
    # A control character in a phone number is not something to clean up and
    # carry on with - it means the value came from somewhere it should not
    # have, so the number is refused rather than repaired.
    if _CONTROL.search(raw):
        raise InvalidMessageRequest("phone number contains control characters")
    cleaned = re.sub(r"[\s\-().]", "", raw.strip())
    if not cleaned.startswith("+"):
        raise InvalidMessageRequest("phone number must start with a country code")
    digits = cleaned[1:]
    if not digits.isdigit():
        raise InvalidMessageRequest("phone number must contain digits only")
    # E.164: at most 15 digits, and a country code plus a national number is
    # never shorter than 8 in practice.
    if not 8 <= len(digits) <= 15:
        raise InvalidMessageRequest("phone number length is not valid")
    return "+" + digits


def mask_phone(phone) -> str:
    """"+91******0001" - safe for a log, a trace or an API response.

    The country code and the last four digits are kept because an owner needs
    to recognise which contractor a line refers to. Everything between is
    replaced, and the full number is never returned by anything that writes to
    a log or crosses the API boundary.
    """
    raw = _CONTROL.sub("", str(phone or "")).strip()
    cleaned = re.sub(r"[\s\-().]", "", raw)
    if not cleaned:
        return ""
    plus = "+" if cleaned.startswith("+") else ""
    digits = cleaned.lstrip("+")
    if len(digits) <= 6:
        return plus + "*" * len(digits)
    head, tail = digits[:2], digits[-4:]
    return f"{plus}{head}{'*' * (len(digits) - 6)}{tail}"


# ---------------------------------------------------------------------------
# the customer-safe projection
# ---------------------------------------------------------------------------

def customer_safe_line(line: Dict) -> dict:
    """One quotation line, reduced to what a customer may see.

    An allow-list. A field the shop adds to a quotation line later is absent
    from customer messages until somebody decides otherwise, which is the
    right default for anything that leaves the building.
    """
    safe = {k: line.get(k) for k in CUSTOMER_SAFE_LINE_FIELDS if k in line}
    return {
        "name": str(safe.get("name") or ""),
        "quantity": safe.get("quantity"),
        "uom": safe.get("catalogueUom") or safe.get("unit") or "",
        "lineTotal": safe.get("lineTotal"),
    }


def customer_safe_quote(quote: Dict) -> dict:
    """A whole quotation, reduced to what a customer may see.

    Note what is NOT here: shortages, stock, velocity, cover, supplier cost,
    margin and the evidence trail. Those are the shop's working. A customer
    sees what they are buying and what it costs.
    """
    if not isinstance(quote, dict) or not quote.get("lines"):
        raise InvalidMessageRequest("that quotation has no lines")
    lines = [customer_safe_line(l) for l in quote["lines"][:MAX_LINES_IN_MESSAGE]]
    return {
        "lines": lines,
        "total": quote.get("total"),
        "lineCount": len(lines),
        "truncated": len(quote["lines"]) > MAX_LINES_IN_MESSAGE,
    }


# ---------------------------------------------------------------------------
# the messages
# ---------------------------------------------------------------------------

_FOOTER = "Sent by the shop using ShopFlow AI."

# What a customer is told about their credit position on a quotation: the
# outcome, and nothing else. Their limit and balance appear only in a message
# that is ABOUT their account, which they are entitled to see.
_CREDIT_WORDS = {
    "APPROVED": "Approved",
    "LIMIT_EXCEEDED": "Over your current credit limit",
    "BLOCKED": "On hold — please speak to the shop",
    "NO_CREDIT_ACCOUNT": "Cash sale",
}


def _clean(text: str) -> str:
    return _CONTROL.sub("", str(text or "")).strip()


def _finish(lines: List[str]) -> str:
    text = "\n".join(lines).strip()
    if len(text) > MAX_MESSAGE_CHARS:
        raise InvalidMessageRequest("that message is too long to send")
    return text


def build_quotation_message(quote: Dict, customer: Optional[Dict] = None,
                            credit: Optional[Dict] = None) -> dict:
    """The quotation, as the customer receives it.

    `quote` is the engine's own quotation dict and `credit` the engine's own
    credit decision. Neither is recomputed, and no total is derived here - the
    figure sent is the figure the engine calculated.
    """
    safe = customer_safe_quote(quote)

    out = ["ShopFlow AI — Quotation", ""]
    if customer and customer.get("customerName"):
        out += [f"Customer: {_clean(customer['customerName'])}", ""]

    out.append("Items:")
    for line in safe["lines"]:
        out.append(f"• {_clean(line['name'])}")
        out.append(f"  {format_units(line['quantity'], line['uom'])}"
                   f" — {format_rupees(line['lineTotal'])}")
    if safe["truncated"]:
        out.append("  (more items — ask the shop for the full list)")

    out += ["", f"Total: {format_rupees(safe['total'])}"]

    if credit and credit.get("decision"):
        word = _CREDIT_WORDS.get(credit["decision"], credit["decision"])
        out += ["", f"Credit status: {word}"]

    out += ["",
            "This is a quotation, not an invoice. Prices are subject to "
            "stock at the time of order.",
            _FOOTER]
    return {"messageType": QUOTATION, "text": _finish(out), "quote": safe}


def build_order_confirmation_message(quote: Dict,
                                     customer: Optional[Dict] = None,
                                     reference: str = "") -> dict:
    """Confirmation that the shop has taken the order.

    Sending this does not place, record or fulfil anything - it tells a
    customer what the shop has agreed to. The order already exists; this is a
    message about it.
    """
    safe = customer_safe_quote(quote)

    out = ["ShopFlow AI — Order Confirmation", ""]
    if customer and customer.get("customerName"):
        out += [f"Customer: {_clean(customer['customerName'])}", ""]
    if reference:
        out += [f"Reference: {_clean(reference)[:32]}", ""]

    out.append("Confirmed:")
    for line in safe["lines"]:
        out.append(f"• {_clean(line['name'])}")
        out.append(f"  {format_units(line['quantity'], line['uom'])}"
                   f" — {format_rupees(line['lineTotal'])}")

    out += ["", f"Total: {format_rupees(safe['total'])}", "",
            "The shop will contact you about delivery.", _FOOTER]
    return {"messageType": ORDER_CONFIRMATION, "text": _finish(out),
            "quote": safe}


def build_credit_status_message(credit: Dict, customer: Optional[Dict] = None,
                                reminder: bool = False) -> dict:
    """Where the customer's khata account stands.

    Their own account, so their own balance and limit belong here - unlike on
    a quotation, where only the outcome is shown. Nothing about the shop's
    costs, margins or purchasing appears in either.

    A reminder is worded as a reminder and is still sent only when a person
    asks for it to be. Nothing in ShopFlow sends one on a schedule.
    """
    if not isinstance(credit, dict) or not credit.get("decision"):
        raise InvalidMessageRequest("no credit decision to report")

    heading = ("ShopFlow AI — Account Reminder" if reminder
               else "ShopFlow AI — Account Status")
    out = [heading, ""]
    name = (customer or {}).get("customerName") or credit.get("customerName")
    if name:
        out += [f"Customer: {_clean(name)}", ""]

    if credit.get("decision") == "NO_CREDIT_ACCOUNT":
        out += ["You do not have a credit account with the shop.",
                "Orders are on a cash basis.", "", _FOOTER]
        return {"messageType": CREDIT_REMINDER if reminder else CREDIT_STATUS,
                "text": _finish(out)}

    if credit.get("currentOutstanding") is not None:
        out.append(f"Outstanding: {format_rupees(credit['currentOutstanding'])}")
    if credit.get("creditLimit") is not None:
        out.append(f"Credit limit: {format_rupees(credit['creditLimit'])}")
    if credit.get("paymentDueDays"):
        out.append(f"Payment terms: {int(credit['paymentDueDays'])} days")

    word = _CREDIT_WORDS.get(credit["decision"], credit["decision"])
    out += ["", f"Status: {word}"]

    if reminder:
        out += ["", "Please settle the outstanding amount when convenient."]

    out += ["", _FOOTER]
    return {"messageType": CREDIT_REMINDER if reminder else CREDIT_STATUS,
            "text": _finish(out)}


def wa_me_url(text: str, phone: Optional[str] = None) -> str:
    """The existing draft link, unchanged and still the fallback.

    With a number it opens a chat with that contact; without one the owner
    picks the contact themselves, which is what the page has always done.
    """
    from urllib.parse import quote as urlquote

    destination = ""
    if phone:
        destination = normalize_phone(phone).lstrip("+")
    return f"https://wa.me/{destination}?text={urlquote(text or '', safe='')}"
