"""Khata: the credit decision, and everything it refuses to be.

Two properties run through every test here.

The first is that the decision is arithmetic. There is no model anywhere in
this path, no score, and no judgement - given a limit, a balance and an order
total, the answer is fixed, and these tests pin it to the boundary rather than
to a comfortable middle.

The second is that a credit decision changes nothing. It does not reprice a
quotation, withhold one, move a balance or write a row. The last block checks
that against the real seeded shop, because a credit feature that quietly
cancelled orders would be worse than no credit feature at all.

All customer records are synthetic. See `backend/seed_data/customers.json`.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from engine.credit import (
    APPROVED,
    BLOCKED,
    LIMIT_EXCEEDED,
    NO_CREDIT_ACCOUNT,
    InvalidOrderTotalError,
    check_credit,
    check_quote_credit,
    customer_view,
    list_customers,
)
from engine.loader import cached_dataset
from engine.models import CUSTOMER_ACTIVE, CUSTOMER_BLOCKED, Customer, Dataset
from engine.quote import calculate_quote

RAVI = "CUST-RAVI-001"
BALA = "CUST-BALA-002"
SELVAM = "CUST-SELVAM-003"
KUMAR = "CUST-KUMAR-004"

CANONICAL = [("SW-ANC-1W10A", 20), ("W-FIN-1.5-RED-90M", 3),
             ("MCB-HAV-SP-32A-C", 2)]


@pytest.fixture(scope="module")
def shop():
    return cached_dataset()


def one(limit, outstanding, status=CUSTOMER_ACTIVE, due=30):
    """A dataset holding exactly one khata account."""
    return Dataset(customers={
        "C-1": Customer(
            customerId="C-1", customerName="Test Contractor",
            phone="+919900000099", creditLimit=limit,
            outstandingAmount=outstanding, paymentDueDays=due, status=status,
        )
    })


# ---------------------------------------------------------------------------
# 1-3  the four decisions
# ---------------------------------------------------------------------------

def test_1_an_order_inside_the_limit_is_approved():
    result = check_credit(one(15000, 8500), "C-1", 4200)

    assert result["decision"] == APPROVED
    assert result["currentOutstanding"] == 8500.0
    assert result["orderTotal"] == 4200.0
    assert result["projectedOutstanding"] == 12700.0
    assert result["remainingCredit"] == 2300.0


def test_1b_the_brief_example_reproduces_exactly(shop):
    """The worked example in the specification, against the seeded shop."""
    result = check_credit(shop, RAVI, 4200)

    assert result["customerId"] == RAVI
    assert result["customerName"] == "Ravi Electrical Works"
    assert result["creditLimit"] == 15000.0
    assert result["currentOutstanding"] == 8500.0
    assert result["orderTotal"] == 4200.0
    assert result["projectedOutstanding"] == 12700.0
    assert result["remainingCredit"] == 2300.0
    assert result["decision"] == APPROVED


def test_2_landing_exactly_on_the_limit_is_approved():
    """`<=`, not `<`. A shop that refuses the rupee that fills the limit is
    wrong, and floating point must not be what decides it."""
    result = check_credit(one(15000, 8500), "C-1", 6500)

    assert result["projectedOutstanding"] == 15000.0
    assert result["remainingCredit"] == 0.0
    assert result["decision"] == APPROVED


def test_2b_one_paisa_over_the_limit_is_exceeded():
    result = check_credit(one(15000, 8500), "C-1", 6500.01)

    assert result["projectedOutstanding"] == 15000.01
    assert result["remainingCredit"] == -0.01
    assert result["decision"] == LIMIT_EXCEEDED


def test_3_an_order_over_the_limit_is_reported_with_the_shortfall():
    result = check_credit(one(15000, 8500), "C-1", 10000)

    assert result["decision"] == LIMIT_EXCEEDED
    assert result["projectedOutstanding"] == 18500.0
    # Negative rather than clamped: the size of the overrun is the useful part.
    assert result["remainingCredit"] == -3500.0
    assert "3,500.00 over" in result["reason"]


def test_4_an_unknown_customer_has_no_account_rather_than_no_credit():
    result = check_credit(one(15000, 8500), "NOBODY", 100)

    assert result["decision"] == NO_CREDIT_ACCOUNT
    # Not zeroes. A customer with no khata is not a customer with a zero limit.
    assert result["creditLimit"] is None
    assert result["currentOutstanding"] is None
    assert result["projectedOutstanding"] is None
    assert result["remainingCredit"] is None
    assert result["orderTotal"] == 100.0


def test_5_a_blocked_account_is_blocked_even_when_the_order_would_fit():
    result = check_credit(one(15000, 0, status=CUSTOMER_BLOCKED), "C-1", 100)

    assert result["decision"] == BLOCKED
    # The numbers are still reported - the owner may want to see them - but
    # they are not the reason, and the message says so.
    assert result["projectedOutstanding"] == 100.0
    assert result["remainingCredit"] == 14900.0
    assert "credit limit is not the reason" in result["reason"]


def test_5b_blocked_outranks_limit_exceeded():
    result = check_credit(one(1000, 5000, status=CUSTOMER_BLOCKED), "C-1", 9999)
    assert result["decision"] == BLOCKED


def test_5c_status_is_compared_case_insensitively():
    data = one(15000, 0)
    data.customers["C-1"] = Customer(
        **{**vars(data.customers["C-1"]), "status": "active"})
    assert check_credit(data, "C-1", 100)["decision"] == APPROVED


# ---------------------------------------------------------------------------
# 6-9  boundaries and awkward numbers
# ---------------------------------------------------------------------------

def test_6_a_clean_account_is_approved_for_its_whole_limit():
    result = check_credit(one(40000, 0), "C-1", 40000)

    assert result["currentOutstanding"] == 0.0
    assert result["projectedOutstanding"] == 40000.0
    assert result["remainingCredit"] == 0.0
    assert result["decision"] == APPROVED


def test_7_a_zero_order_reports_the_headroom_and_does_not_error():
    """"Have I got room on the account?" is a real question at a counter."""
    result = check_credit(one(15000, 8500), "C-1", 0)

    assert result["decision"] == APPROVED
    assert result["orderTotal"] == 0.0
    assert result["projectedOutstanding"] == 8500.0
    assert result["remainingCredit"] == 6500.0


def test_7b_a_negative_order_total_is_refused():
    with pytest.raises(InvalidOrderTotalError):
        check_credit(one(15000, 8500), "C-1", -1)


def test_7c_a_missing_or_malformed_order_total_is_refused():
    for bad in (None, "", "abc", [], {}, True, float("nan"), float("inf")):
        with pytest.raises(InvalidOrderTotalError):
            check_credit(one(15000, 8500), "C-1", bad)


def test_8_paise_are_exact():
    """Decimal, not float. 0.1 + 0.2 arithmetic must not decide a limit."""
    result = check_credit(one(1000.00, 999.70), "C-1", 0.30)

    assert result["projectedOutstanding"] == 1000.00
    assert result["remainingCredit"] == 0.00
    assert result["decision"] == APPROVED


def test_8b_decimal_and_string_amounts_are_accepted_identically():
    a = check_credit(one(15000, 8500), "C-1", Decimal("4200.50"))
    b = check_credit(one(15000, 8500), "C-1", "4200.50")
    c = check_credit(one(15000, 8500), "C-1", 4200.50)

    assert a["projectedOutstanding"] == b["projectedOutstanding"] == \
        c["projectedOutstanding"] == 12700.50


def test_8c_a_float_that_cannot_be_represented_exactly_still_lands_on_the_limit():
    """0.1 + 0.2 != 0.3 in binary. The decision must not notice."""
    result = check_credit(one(0.3, 0.1), "C-1", 0.2)
    assert result["projectedOutstanding"] == 0.3
    assert result["decision"] == APPROVED


def test_9_large_values_are_handled_without_drift():
    result = check_credit(one(100_000_000.00, 99_999_999.99), "C-1", 0.01)

    assert result["projectedOutstanding"] == 100_000_000.00
    assert result["remainingCredit"] == 0.0
    assert result["decision"] == APPROVED


def test_9b_a_very_large_order_is_simply_exceeded():
    result = check_credit(one(15000, 0), "C-1", 10_000_000)
    assert result["decision"] == LIMIT_EXCEEDED
    assert result["remainingCredit"] == -9_985_000.0


# ---------------------------------------------------------------------------
# 10  credit against a real quotation
# ---------------------------------------------------------------------------

def test_10_credit_is_checked_against_the_engines_own_quotation_total(shop):
    quote = calculate_quote(shop, CANONICAL).as_dict()
    result = check_quote_credit(shop, BALA, quote)

    assert result["orderTotal"] == quote["total"]
    assert result["decision"] == APPROVED


def test_10b_the_canonical_order_exceeds_ravis_limit(shop):
    quote = calculate_quote(shop, CANONICAL).as_dict()
    result = check_quote_credit(shop, RAVI, quote)

    assert result["decision"] == LIMIT_EXCEEDED
    assert result["orderTotal"] == 22306.48


def test_10c_an_anonymous_order_gets_no_credit_block_at_all(shop):
    """The existing cash-sale flow must be untouched, not merely tolerated."""
    quote = calculate_quote(shop, CANONICAL).as_dict()

    assert check_quote_credit(shop, None, quote) is None
    assert check_quote_credit(shop, "", quote) is None


def test_10d_a_credit_check_does_not_change_the_quotation(shop):
    before = calculate_quote(shop, CANONICAL).as_dict()
    check_quote_credit(shop, RAVI, before)
    after = calculate_quote(shop, CANONICAL).as_dict()

    assert after["total"] == before["total"] == 22306.48
    assert after == before


def test_10e_the_total_comes_from_the_quote_object_not_from_a_parameter(shop):
    """There is no way to hand this function an amount.

    `check_quote_credit` takes a quotation and reads `total` off it. It has no
    amount parameter at all, so a request cannot have its credit checked
    against a figure the engine never produced - the API passes the quotation
    the worker just priced, and nothing else is accepted. This is a structural
    property, so it is checked structurally.
    """
    import inspect

    params = list(inspect.signature(check_quote_credit).parameters)
    assert params == ["data", "customer_id", "quote"], params

    quote = calculate_quote(shop, CANONICAL).as_dict()
    assert check_quote_credit(shop, RAVI, quote)["orderTotal"] == quote["total"]
    # A quotation with no total is not credit-checked against a guess.
    assert check_quote_credit(shop, RAVI, {"lines": []}) is None


# ---------------------------------------------------------------------------
# 11  the seeded accounts, and the safety properties
# ---------------------------------------------------------------------------

def test_11_the_seeded_shop_has_four_synthetic_accounts(shop):
    customers = list_customers(shop)
    assert len(customers) == 4
    assert all(c["synthetic"] is True for c in customers)
    # Sorted by name, so the UI needs no ordering rule of its own.
    assert [c["customerName"] for c in customers] == sorted(
        c["customerName"] for c in customers)


def test_11b_no_seeded_phone_number_is_dialable(shop):
    """Fixtures must not carry anything that could reach a real person."""
    for customer in shop.customers.values():
        assert customer.phone.startswith("+9199000000"), customer.phone


def test_11c_the_four_accounts_cover_the_four_decisions(shop):
    quote = calculate_quote(shop, CANONICAL).as_dict()
    decisions = {
        cid: check_quote_credit(shop, cid, quote)["decision"]
        for cid in (BALA, RAVI, KUMAR)
    }
    assert decisions[BALA] == APPROVED
    assert decisions[RAVI] == LIMIT_EXCEEDED
    assert decisions[KUMAR] == BLOCKED
    assert check_credit(shop, "CUST-NOT-REAL", 100)["decision"] == NO_CREDIT_ACCOUNT
    assert check_credit(shop, SELVAM, 0)["decision"] == APPROVED


def test_11d_every_result_states_that_this_is_not_credit_scoring(shop):
    for cid in (BALA, RAVI, KUMAR, SELVAM, "CUST-NOT-REAL"):
        result = check_credit(shop, cid, 100)
        assert "does not perform credit scoring" in result["policy"]
        assert result["synthetic"] is True


def test_11e_the_module_cannot_write_anything_or_call_a_model():
    """Structural: there is no route from here to a store or to Bedrock."""
    import inspect

    from engine import credit

    source = inspect.getsource(credit)
    for forbidden in ("boto3", "put_item", "update_item", "delete_item",
                      "bedrock", "invoke_model", "converse", "requests"):
        assert forbidden not in source, f"credit.py reaches for {forbidden}"


def test_11f_a_credit_check_leaves_the_customer_record_untouched(shop):
    before = {c.customerId: vars(c).copy() for c in shop.customers.values()}

    for cid in (BALA, RAVI, KUMAR, SELVAM):
        check_credit(shop, cid, 5000)

    assert {c.customerId: vars(c) for c in shop.customers.values()} == before


def test_11g_customer_view_exposes_no_field_the_record_does_not_hold(shop):
    view = customer_view(shop.customers[RAVI])
    assert set(view) == {
        "customerId", "customerName", "phone", "creditLimit",
        "outstandingAmount", "currency", "paymentDueDays", "status",
        "synthetic",
    }


def test_11h_an_empty_shop_answers_no_credit_account_rather_than_crashing():
    assert check_credit(Dataset(), "ANYONE", 100)["decision"] == NO_CREDIT_ACCOUNT
    assert list_customers(Dataset()) == []
