"""Khata: can this customer take these goods on credit?

WHAT THIS IS
------------
An Indian hardware shop runs on the khata - the ledger page per contractor,
with a limit the owner set and a balance that moves every week. The question
at the counter is never "what is this customer's risk profile". It is "he owes
me eight and a half thousand, his limit is fifteen, this order is four two -
can I give it?".

This module answers exactly that question, with subtraction:

    projectedOutstanding = outstandingAmount + orderTotal
    remainingCredit      = creditLimit - projectedOutstanding

    APPROVED           the account exists, is ACTIVE, and the projected
                       balance is within the limit
    LIMIT_EXCEEDED     the projected balance is over the limit
    BLOCKED            the owner has suspended the account, whatever the
                       numbers say
    NO_CREDIT_ACCOUNT  there is no khata for this customer

WHAT THIS IS NOT
----------------
It is not credit scoring. There is no model, no score, no bureau data, no
history of other shops and nothing predictive. Every input is a number the
shop owner themselves put on the account, and the output is a comparison
between two of them. If the owner raises the limit, the answer changes; that
is the whole mechanism.

It is also not an enforcement gate. A LIMIT_EXCEEDED result does not cancel a
quotation, alter a price or change stock. The quotation stands, the customer
still sees it, and whether to sell anyway is the owner's call - shops extend
past the limit for a good contractor every day, and software that refuses to
print the quote would simply be switched off.

WHY DECIMAL
-----------
The rest of the engine rounds rupees with `models.money`, which is enough for
a total. It is not enough here, because this is a COMPARISON against a
threshold, and a balance that lands exactly on the limit must be approved
rather than lost to a floating-point hair. The arithmetic below is therefore
in `Decimal`, quantised to paise, and converted to float only on the way out
to JSON.

THE LLM DOES NOT DECIDE THIS
----------------------------
A model may read "put it on Ravi's account" and produce a customer id. It
never produces a limit, a balance, a projection or a decision. Those come from
here, from the shop's own record.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import List, Optional

from .models import CUSTOMER_ACTIVE, Customer, Dataset

# Decisions.
APPROVED = "APPROVED"
LIMIT_EXCEEDED = "LIMIT_EXCEEDED"
BLOCKED = "BLOCKED"
NO_CREDIT_ACCOUNT = "NO_CREDIT_ACCOUNT"

DECISIONS = (APPROVED, LIMIT_EXCEEDED, BLOCKED, NO_CREDIT_ACCOUNT)

CURRENCY = "INR"

PAISE = Decimal("0.01")

# Said on every result. The claim is deliberately narrow, and it is the claim
# the product is willing to defend.
NOT_CREDIT_SCORING = (
    "ShopFlow does not perform credit scoring. Khata decisions are "
    "deterministic checks against shop-defined customer credit limits and "
    "outstanding balances."
)

SYNTHETIC_DATA_NOTE = "Demo customer records are synthetic."


class InvalidOrderTotalError(ValueError):
    """The order total is missing, negative, or not a number."""


def _decimal(value, *, field: str) -> Decimal:
    """A money amount as Decimal, or an error. Never a silent zero."""
    if value is None or isinstance(value, bool):
        raise InvalidOrderTotalError(f"{field} must be a number")
    try:
        amount = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        raise InvalidOrderTotalError(f"{field} must be a number") from None
    if not amount.is_finite():
        raise InvalidOrderTotalError(f"{field} must be a finite number")
    return amount.quantize(PAISE, rounding=ROUND_HALF_UP)


def parse_order_total(value) -> Decimal:
    """Validate an order total. Zero is legitimate; negative is not.

    Zero comes up for real - a customer asking whether they have room on the
    account before they order anything - and the honest answer is their
    current headroom, not an error.
    """
    amount = _decimal(value, field="orderTotal")
    if amount < 0:
        raise InvalidOrderTotalError("orderTotal must not be negative")
    return amount


def _out(amount: Decimal) -> float:
    """Decimal to the float the rest of the API speaks, at paise precision."""
    return float(amount.quantize(PAISE, rounding=ROUND_HALF_UP))


def customer_view(customer: Customer) -> dict:
    """A khata account as the API returns it.

    The phone number is included because the shop identifies contractors by
    it. Every number in this repository is synthetic - see the note on the
    seed data.
    """
    return {
        "customerId": customer.customerId,
        "customerName": customer.customerName,
        "phone": customer.phone,
        "creditLimit": _out(_decimal(customer.creditLimit, field="creditLimit")),
        "outstandingAmount": _out(
            _decimal(customer.outstandingAmount, field="outstandingAmount")),
        "currency": customer.currency or CURRENCY,
        "paymentDueDays": int(customer.paymentDueDays),
        "status": str(customer.status).upper(),
        "synthetic": True,
    }


def list_customers(data: Dataset) -> List[dict]:
    """Every khata account, by name, so the UI needs no ordering rule."""
    return [
        customer_view(c)
        for c in sorted(data.customers.values(), key=lambda c: c.customerName)
    ]


def check_credit(data: Dataset, customer_id, order_total) -> dict:
    """The credit decision for one order against one account.

    Order of checks is the order a shop owner uses. Does the account exist at
    all; is it one I am still serving; and only then, do the numbers fit. A
    blocked account is reported as BLOCKED even when the order would fit
    inside the limit, because the limit is not why it was blocked.
    """
    total = parse_order_total(order_total)
    requested_id = "" if customer_id is None else str(customer_id)

    customer = data.customer(requested_id)
    if customer is None:
        # No account, so there is no limit and no balance to report. Returning
        # zeroes here would look like a customer with no credit rather than a
        # customer the shop has never opened a khata for.
        return {
            "customerId": requested_id,
            "customerName": None,
            "creditLimit": None,
            "currentOutstanding": None,
            "orderTotal": _out(total),
            "projectedOutstanding": None,
            "remainingCredit": None,
            "currency": CURRENCY,
            "paymentDueDays": None,
            "status": None,
            "decision": NO_CREDIT_ACCOUNT,
            "reason": "This customer has no khata account with the shop.",
            "evidence": None,
            "policy": NOT_CREDIT_SCORING,
            "synthetic": True,
        }

    limit = _decimal(customer.creditLimit, field="creditLimit")
    outstanding = _decimal(customer.outstandingAmount, field="outstandingAmount")
    projected = (outstanding + total).quantize(PAISE, rounding=ROUND_HALF_UP)
    remaining = (limit - projected).quantize(PAISE, rounding=ROUND_HALF_UP)

    if not customer.isActive:
        decision = BLOCKED
        reason = (
            f"{customer.customerName}'s account is {str(customer.status).upper()}. "
            f"The shop suspended it; the credit limit is not the reason."
        )
    elif projected > limit:
        decision = LIMIT_EXCEEDED
        # `remaining` is negative here, so the shortfall is its magnitude.
        reason = (
            f"This order would take {customer.customerName} "
            f"{_out(-remaining):,.2f} over a {_out(limit):,.2f} limit."
        )
    else:
        decision = APPROVED
        reason = (
            f"{_out(remaining):,.2f} would remain of "
            f"{customer.customerName}'s {_out(limit):,.2f} limit."
        )

    return {
        "customerId": customer.customerId,
        "customerName": customer.customerName,
        "creditLimit": _out(limit),
        "currentOutstanding": _out(outstanding),
        "orderTotal": _out(total),
        "projectedOutstanding": _out(projected),
        # Negative when the limit is exceeded, and deliberately not clamped:
        # "2,300 left" and "1,806.48 over" are the same field read two ways,
        # and hiding the sign would hide the size of the problem.
        "remainingCredit": _out(remaining),
        "currency": customer.currency or CURRENCY,
        "paymentDueDays": int(customer.paymentDueDays),
        "status": str(customer.status).upper(),
        "decision": decision,
        "reason": reason,
        "evidence": {
            "projectedOutstanding": (
                f"{_out(outstanding)} + {_out(total)} = {_out(projected)}"),
            "remainingCredit": (
                f"{_out(limit)} - {_out(projected)} = {_out(remaining)}"),
            "test": (
                f"{_out(projected)} "
                f"{'<=' if projected <= limit else '>'} {_out(limit)}"),
            "source": "engine.credit.check_credit",
        },
        "policy": NOT_CREDIT_SCORING,
        "synthetic": True,
    }


def check_quote_credit(data: Dataset, customer_id, quote: dict) -> Optional[dict]:
    """The credit decision for a quotation the engine already priced.

    The order total is taken from the quotation, never from the caller, so a
    request cannot have its credit checked against a figure the engine did not
    produce. Returns None when no customer was supplied - an anonymous order
    is the normal case and gets no credit block at all.

    The quotation itself is neither modified nor withheld. This adds a panel
    beside it and nothing more.
    """
    if not customer_id or not quote:
        return None
    total = quote.get("total")
    if total is None:
        return None
    return check_credit(data, customer_id, total)
