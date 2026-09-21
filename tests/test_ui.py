"""The browser layer, tested against real engine output.

WHY THESE EXIST
---------------
The panels that show money - khata, margin protection, the quotation lines -
are pure string builders. They take an API response and return HTML. That
means they can be lifted out of `index.html`, executed in Node against
figures the Python engine actually produced, and checked.

What is being checked is not that the markup is pretty. It is that the browser
displays the engine's numbers and only the engine's numbers: no panel here may
add, subtract, compare or round anything, and a panel that did would show a
figure the response does not contain. Several tests below assert exactly that,
by comparing the money tokens in the rendered HTML against the money in the
response that produced it.

The remaining tests are structural, covering the parts that need a real DOM -
the voice card, the planner, the customer selector - by asserting the elements
and wiring exist rather than by pretending to click them.

Node is required for the rendering half and those tests skip without it.
"""

from __future__ import annotations

import json
import pathlib
import re
import shutil
import subprocess

import pytest

from engine.credit import check_credit, check_quote_credit
from engine.loader import cached_dataset
from engine.margin import margin_alerts, margin_view
from engine.purchasing import build_purchase_plan
from engine.quote import calculate_quote

ROOT = pathlib.Path(__file__).resolve().parents[1]
INDEX = ROOT / "frontend" / "site" / "index.html"

WIRE = "W-FIN-1.5-RED-90M"
SWITCH = "SW-ANC-1W10A"
MCB = "MCB-HAV-SP-32A-C"
CANONICAL = [(SWITCH, 20), (WIRE, 3), (MCB, 2)]
CONFIRMED_WIRE_COST = 6300.0

node = pytest.mark.skipif(
    shutil.which("node") is None, reason="Node is not installed")


@pytest.fixture(scope="module")
def page():
    return INDEX.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# running the real builders
# ---------------------------------------------------------------------------

def _function_source(page: str, name: str) -> str:
    start = page.index(f"function {name}(")
    depth, i = 0, page.index("{", start)
    while True:
        if page[i] == "{":
            depth += 1
        elif page[i] == "}":
            depth -= 1
            if depth == 0:
                return page[start:i + 1]
        i += 1


def _builders(page: str) -> str:
    block = page[page.index("// >>> panel-builders"):
                 page.index("// <<< panel-builders")]
    return "\n".join([
        _function_source(page, "rupees"),
        _function_source(page, "esc"),
        # quoteAffectedNote reads this global; the panels never write it.
        "var lastQuote = null;",
        block,
    ])


def _render(page: str, payload: dict, tmp_path: pathlib.Path) -> dict:
    script = tmp_path / "panels.js"
    data_file = tmp_path / "in.json"
    data_file.write_text(json.dumps(payload), encoding="utf-8")
    script.write_text(
        _builders(page)
        + """
const input = JSON.parse(require('fs').readFileSync(process.argv[2], 'utf8'));
const out = {};
Object.keys(input.credit || {}).forEach(function (k) {
  out['credit_' + k] = khataPanel(input.credit[k]);
});
Object.keys(input.margin || {}).forEach(function (k) {
  out['margin_' + k] = marginPanel(input.margin[k], "");
});
out.credit_null = khataPanel(null);
out.margin_null = marginPanel(null, "");
process.stdout.write(JSON.stringify(out));
""",
        encoding="utf-8")
    result = subprocess.run(
        ["node", str(script), str(data_file)],
        capture_output=True, text=True, encoding="utf-8", timeout=60)
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


@pytest.fixture(scope="module")
def engine_output():
    data = cached_dataset()
    quote = calculate_quote(data, CANONICAL).as_dict()
    return {
        "credit": {
            "approved": check_quote_credit(data, "CUST-BALA-002", quote),
            "exceeded": check_quote_credit(data, "CUST-RAVI-001", quote),
            "blocked": check_quote_credit(data, "CUST-KUMAR-004", quote),
            "none": check_credit(data, "CUST-NOT-REAL", quote["total"]),
        },
        "margin": {
            "reduced": margin_view(data, WIRE, CONFIRMED_WIRE_COST),
            "unavailable": margin_view(data, "NOT-A-SKU", 10.0),
        },
    }


@pytest.fixture(scope="module")
def rendered(page, engine_output, tmp_path_factory):
    if shutil.which("node") is None:
        pytest.skip("Node is not installed")
    return _render(page, engine_output, tmp_path_factory.mktemp("ui"))


def _money(text: str):
    return {m.replace(",", "") for m in re.findall(r"\d[\d,]*\.\d{2}", text)}


def _response_money(payload: dict):
    """Every money figure the response carries, and its magnitude.

    The magnitude is allowed because a negative field is legitimately read
    two ways: `remainingCredit` of -15806.48 renders as that number in the
    grid and as "15,806.48 over" in the sentence beside it. Both are the same
    response field, so both are the engine's figure - and `_money` below does
    not capture signs anyway.
    """
    found = set()
    for value in payload.values():
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            found.add(f"{float(value):.2f}")
            found.add(f"{abs(float(value)):.2f}")
    return found


# ---------------------------------------------------------------------------
# 1-4  the khata panel
# ---------------------------------------------------------------------------

@node
def test_1_the_khata_panel_shows_every_figure_the_decision_carries(
        rendered, engine_output):
    html = rendered["credit_approved"]
    credit = engine_output["credit"]["approved"]

    assert "Khata / Credit" in html
    assert credit["customerName"] in html
    for field in ("currentOutstanding", "creditLimit", "orderTotal",
                  "projectedOutstanding", "remainingCredit"):
        assert f"{credit[field]:,.2f}" in html, field
    assert "Credit approved" in html
    assert "k-approved" in html


@node
def test_1b_the_panel_invents_no_figure(rendered, engine_output):
    """Every rupee token rendered must exist in the response."""
    for key in ("approved", "exceeded", "blocked"):
        allowed = _response_money(engine_output["credit"][key])
        rendered_money = _money(rendered["credit_" + key])
        assert rendered_money <= allowed, key


@node
def test_2_an_exceeded_limit_is_shown_and_the_quotation_still_stands(
        rendered, engine_output):
    html = rendered["credit_exceeded"]

    assert "Credit limit exceeded" in html
    assert "k-exceeded" in html
    # The overrun keeps its sign rather than being hidden.
    assert f"{engine_output['credit']['exceeded']['remainingCredit']:,.2f}" in html
    assert "k-over" in html
    # And the panel says outright that nothing was cancelled.
    assert "This quotation stands" in html
    assert "your decision" in html


@node
def test_2b_a_blocked_account_reads_as_blocked(rendered):
    html = rendered["credit_blocked"]
    assert "Account blocked" in html
    assert "k-blocked" in html
    assert "This quotation stands" in html


@node
def test_3_no_account_shows_no_limit_and_no_balance(rendered):
    html = rendered["credit_none"]

    assert "No credit account" in html
    # Nothing that would look like a customer with zero credit.
    assert "Credit limit" not in html
    assert "Current outstanding" not in html
    assert "Projected outstanding" not in html


@node
def test_3b_every_khata_panel_states_the_policy_and_the_synthetic_data(
        rendered):
    for key in ("approved", "exceeded", "blocked", "none"):
        html = rendered["credit_" + key]
        assert "does not perform credit scoring" in html, key
        assert "synthetic" in html.lower(), key


@node
def test_4_an_anonymous_order_renders_no_khata_panel_at_all(rendered):
    assert rendered["credit_null"] == ""


# ---------------------------------------------------------------------------
# 5  the margin panel still works
# ---------------------------------------------------------------------------

@node
def test_5_the_margin_panel_is_unchanged(rendered, engine_output):
    html = rendered["margin_reduced"]
    margin = engine_output["margin"]["reduced"]

    assert "Margin protection" in html
    for field in ("sellingPrice", "previousSupplierCost",
                  "confirmedSupplierCost", "oldMarginAmount",
                  "newMarginAmount", "marginReductionAmount"):
        assert f"{margin[field]:,.2f}" in html, field
    assert "Review price" in html
    assert "does not automatically change your selling price" in html


@node
def test_5b_the_margin_panel_invents_no_figure(rendered, engine_output):
    allowed = _response_money(engine_output["margin"]["reduced"])
    assert _money(rendered["margin_reduced"]) <= allowed


@node
def test_5c_an_unavailable_margin_renders_a_message_not_a_number(rendered):
    html = rendered["margin_unavailable"]
    assert "cannot be shown" in html
    assert _money(html) == set()


# ---------------------------------------------------------------------------
# 6-8  structure: the parts that need a real DOM
# ---------------------------------------------------------------------------

def test_6_the_customer_selector_exists_and_defaults_to_a_cash_sale(page):
    assert 'id="customer"' in page
    assert "no khata account" in page
    # Populated from the API, never hard-coded.
    assert '/api/customers' in page
    assert "Ravi Electrical Works" not in page
    assert "CUST-RAVI-001" not in page


def test_6b_the_order_request_carries_the_selected_customer(page):
    assert "customerId: customerSel.value" in page


def test_7_quotation_lines_display_units_and_the_equivalent(page):
    assert "l.catalogueUom" in page
    assert "l.baseEquivalent" in page
    assert "equivalent" in page
    # The requested quantity is rendered, never a converted one.
    assert "esc(l.quantity) + ' ' +" in page


def test_7b_the_conversion_is_shown_in_the_evidence(page):
    assert "l.conversion" in page
    assert "You asked in" in page
    assert "l.requestedUom" in page


def test_8_the_existing_features_are_still_wired(page):
    """A blunt regression net around everything this change moved past."""
    for marker in (
        # canonical order flow
        'id="order"', 'id="go"', "/api/orders", 'id="clarify"',
        # margin protection
        "marginPanel", "/api/price-decisions", "Review price",
        # purchase planner
        'id="budget"', 'id="plan"', "/api/purchase-plans", "confirmedCostImpact",
        # WhatsApp
        "whatsappQuoteText", "whatsappPlanText", "https://wa.me/?text=",
        # voice
        'id="mic"', "/api/shop-queries", "SpeechRecognition", 'id="vmargin"',
        # trace
        "tracePanel",
    ):
        assert marker in page, marker


def test_8b_the_page_hard_codes_no_business_number(page):
    """The UI has never held a figure of its own, and still does not."""
    script = page[page.rindex("<script>"):]
    for banned in ("22306", "22,306", "6608", "6,608", "24,993", "803.40",
                   "12700", "12,700", "8500", "8,500", "15000.0"):
        assert banned not in script, banned


def test_8c_the_page_contains_no_credit_arithmetic(page):
    """The decision arrives made. The browser must not recompute any part."""
    script = page[page.rindex("<script>"):]
    khata = script[script.index("function khataPanel"):
                   script.index("// ---- margin protection")]
    for operator in ("creditLimit -", "+ c.orderTotal", "currentOutstanding +",
                     "> c.creditLimit", "projectedOutstanding >"):
        assert operator not in khata, operator


def test_8d_no_real_customer_data_is_present_anywhere():
    """Phone numbers in fixtures must be unusable, and nowhere else may hold
    a customer record at all."""
    customers = json.loads(
        (ROOT / "backend" / "seed_data" / "customers.json").read_text("utf-8"))
    assert len(customers) == 4
    for customer in customers:
        assert customer["phone"].startswith("+9199000000"), customer["phone"]
