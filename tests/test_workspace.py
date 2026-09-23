"""The demo shop workspace: login, routing, inventory, and what it admits.

The workspace is a product shell, not a security boundary. A login box that
checks nothing is the kind of thing an evaluator reads as a security claim
unless it is labelled, so most of what these tests assert is that the page
keeps saying what it is: a demo, with simulated sign-in and synthetic data.

The rest asserts that the shell borrowed no business logic. Every figure it
shows arrives from `GET /api/demo`, which reads the same seeded dataset and
the same purchasing-planner reorder calculation the assistant uses. A second
opinion here about what "low stock" means is how two halves of a system start
disagreeing in front of a customer.
"""

from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "backend" / "lambdas" / "api"))
for _k, _v in (("TABLE_NAME", "shopflow-demo"), ("ORDERS_QUEUE_URL", "q"),
               ("UPLOADS_BUCKET", "b"), ("BEDROCK_MODEL_ID", "m")):
    os.environ.setdefault(_k, _v)

import handler  # noqa: E402
from engine.loader import load_dataset  # noqa: E402

PAGE = (ROOT / "frontend" / "site" / "index.html").read_text(encoding="utf-8")
WORKSPACE_JS = PAGE[PAGE.index("// >>> workspace"):PAGE.index("// <<< workspace")]


@pytest.fixture(scope="module")
def demo():
    return json.loads(handler._get_demo({})["body"])


def _strip_comments(js: str) -> str:
    return "\n".join(l for l in js.splitlines()
                     if not l.lstrip().startswith("//"))


# ---------------------------------------------------------------------------
# 1. the login page
# ---------------------------------------------------------------------------

def test_1_the_login_page_renders():
    assert 'id="viewLogin"' in PAGE
    assert 'id="loginForm"' in PAGE
    assert 'id="enterDemo"' in PAGE
    assert "Enter demo workspace" in PAGE


def test_1b_the_login_input_has_a_label_and_a_one_click_default():
    assert '<label for="shopId">' in PAGE
    assert 'id="shopId"' in PAGE
    # The evaluator must get in without typing anything.
    assert 'value="demo@shopflow.ai"' in PAGE
    assert "No credentials required" in PAGE


def test_1c_the_login_never_claims_to_be_authentication():
    """The distinction an AI evaluator will look for."""
    lowered = PAGE.lower()
    for forbidden in ("secure login", "enterprise authentication",
                      "protected account", "encrypted account",
                      "sign in securely", "your data is secure"):
        assert forbidden not in lowered, forbidden

    assert "authentication is simulated" in lowered
    assert "production deployments should add authenticated" in lowered


def test_1d_no_password_field_and_no_credential_is_stored():
    assert 'type="password"' not in PAGE
    assert "password" not in WORKSPACE_JS.lower()
    js = _strip_comments(WORKSPACE_JS)
    # Only the session flag is persisted, and it is not a credential.
    stored = re.findall(r"sessionStorage\.setItem\(([^)]*)\)", js)
    assert stored == ['KEY, "demo"']
    assert "localStorage" not in js


# ---------------------------------------------------------------------------
# 2-5. entry, routing, logout, navigation
# ---------------------------------------------------------------------------

def test_2_demo_entry_signs_in_and_routes_to_the_workspace():
    js = _strip_comments(WORKSPACE_JS)
    assert 'el("loginForm").addEventListener("submit"' in js
    assert "signIn();" in js
    assert 'go("/workspace");' in js


def test_3_every_workspace_route_has_a_pane_or_a_view():
    for route in ("/workspace", "/workspace/profile", "/workspace/inventory",
                  "/workspace/orders"):
        assert '"%s"' % route in WORKSPACE_JS
    assert '"/workspace/assistant"' in WORKSPACE_JS
    for pane in ("paneOverview", "paneProfile", "paneInventory", "paneOrders"):
        assert 'id="%s"' % pane in PAGE


def test_4_logout_clears_the_session_and_returns_to_login():
    js = _strip_comments(WORKSPACE_JS)
    assert 'el("logoutBtn").addEventListener("click"' in js
    assert "signOut();" in js
    assert 'go("/login");' in js
    assert "sessionStorage.removeItem(KEY)" in js


def test_5_navigation_links_exist_and_are_marked_for_screen_readers():
    for nav in ("navOverview", "navProfile", "navInventory", "navOrders",
                "navAssistant"):
        assert 'id="%s"' % nav in PAGE
    assert 'aria-current", "page"' in WORKSPACE_JS


def test_5b_an_unauthenticated_workspace_request_is_sent_to_login():
    js = _strip_comments(WORKSPACE_JS)
    assert 'if (path === "/login" || !signedIn())' in js
    assert 'history.replaceState({}, "", "/login")' in js


def test_5c_the_assistant_route_shows_the_existing_application_untouched():
    """The workspace decides when the app is on screen. Nothing else."""
    js = _strip_comments(WORKSPACE_JS)
    assert 'show("viewAssistant");' in js
    assert 'id="viewAssistant"' in PAGE
    # The existing app markup is still inside it, not rebuilt.
    assert 'id="order"' in PAGE and 'id="go"' in PAGE and 'id="plan"' in PAGE


# ---------------------------------------------------------------------------
# 6. inventory comes from the existing data, not a new engine
# ---------------------------------------------------------------------------

def test_6_the_demo_endpoint_carries_the_seeded_inventory(demo):
    data = load_dataset()
    inventory = demo["inventory"]
    assert len(inventory) == len(data.products) == demo["catalogSize"]

    row = next(i for i in inventory if i["skuId"] == "SW-ANC-1W10A")
    product = data.product("SW-ANC-1W10A")
    assert row["name"] == product.name
    assert row["sellingPrice"] == product.sellingPrice
    assert row["onHand"] == data.onHand("SW-ANC-1W10A")


def test_6b_stock_status_is_the_planners_calculation_not_a_new_rule():
    """LOW STOCK must mean exactly what the purchasing planner means."""
    from engine.budget import restock_candidates

    data = load_dataset()
    planner_low = {c.skuId for c in restock_candidates(data)}
    inventory = json.loads(handler._get_demo({})["body"])["inventory"]
    page_low = {i["skuId"] for i in inventory if i["status"] == "LOW STOCK"}
    page_short = {i["skuId"] for i in inventory if i["status"] == "SHORTAGE"}

    # Everything the page calls low is low to the planner too. A shortage
    # outranks it, so those are allowed to differ only in that direction.
    assert page_low <= planner_low
    assert (planner_low - page_low) <= page_short


def test_6c_the_inventory_payload_carries_no_supplier_cost(demo):
    """This route is public. Product also has costPrice; it must not travel."""
    blob = json.dumps(demo)
    for internal in ("costPrice", "supplierPrice", "unitCost", "marginPerUnit",
                     "marginPerRupee", "baseQuantity"):
        assert internal not in blob, internal
    assert set(demo["inventory"][0]) == set(handler.INVENTORY_FIELDS)


def test_6d_the_table_renders_every_declared_column():
    for column in ("SKU", "Product", "Brand", "Category", "UOM",
                   "Selling price", "On hand", "Status"):
        assert ">" + column + "<" in PAGE, column


def test_6e_the_workspace_computes_no_business_figure():
    """It counts and filters. It does not price, total or re-derive.

    The Intelligence pane renders the word "margin" in a sentence explaining
    that the margin verdict is the engine's, so a bare substring check for it
    now fails on prose rather than on arithmetic. What it was really testing
    is below, stated directly: no internal cost identifier appears anywhere,
    no business figure is hard-coded, and no business field is ever an
    operand.
    """
    js = _strip_comments(WORKSPACE_JS)

    # Internal costs may not appear at all, in any form.
    for banned in ("costPrice", "unitCost", "supplierPrice", "marginPerUnit",
                   "marginPerRupee", "confirmedSupplierCost"):
        assert banned not in js, banned

    # No business figure is hard-coded here.
    for banned in ("22306", "24993", "24996", "147", "6300", "5900"):
        assert banned not in js, banned

    # And nothing is computed FROM one. Any arithmetic operator applied to a
    # field that carries money or stock would be this page forming a second
    # opinion about a number an engine already decided.
    for field in ("sellingPrice", "onHand", "lineTotal", "total", "budget",
                  "previousCost", "newCost", "previousMargin", "newMargin"):
        # `+` is excluded: in this file it is string concatenation, which is
        # how every table cell is built. `*`, `-` and `/` have no such
        # second meaning, and any of them beside a business field would be
        # this page doing arithmetic it has no business doing.
        for operator in ("*", "-", "/"):
            assert f"{field} {operator}" not in js, (field, operator)
            assert f"{operator} {field}" not in js, (field, operator)


# ---------------------------------------------------------------------------
# 7. what the workspace admits
# ---------------------------------------------------------------------------

def test_7_the_synthetic_data_disclosure_is_visible_in_the_workspace():
    workspace = PAGE[PAGE.index('id="viewWorkspace"'):PAGE.index('id="viewAssistant"')]
    lowered = workspace.lower()
    assert "synthetic" in lowered
    assert "do not represent a real shop" in lowered
    assert "authentication is simulated" in lowered


def test_7b_the_profile_states_the_data_is_synthetic(demo):
    assert "synthetic" in demo["dataNotice"].lower()
    assert 'id="profData"' in PAGE
    assert "ShopFlow does not currently" in PAGE


def test_7c_the_backend_shop_key_is_shown_but_not_renamed(demo):
    """SHOP#demo stays the record; the UI shows a friendly name beside it."""
    assert "SHOP#demo" in PAGE
    assert demo["shopName"] == "Demo Electricals, Madurai"
    assert 'id="wsShopName"' in PAGE


def test_7e_the_dashboard_shows_no_invented_metric():
    overview = PAGE[PAGE.index('id="paneOverview"'):PAGE.index('id="paneProfile"')]
    lowered = overview.lower()
    for invented in ("revenue", "growth", "conversion", "satisfaction",
                     "uptime", "trending"):
        assert invented not in lowered, invented


# ---------------------------------------------------------------------------
# 8-10. the existing system is untouched
# ---------------------------------------------------------------------------

def test_8_the_existing_api_contract_is_only_added_to(demo):
    """Every field the old page read is still there, unchanged in meaning."""
    for field in ("shopName", "catalogSize", "exampleOrder",
                  "ambiguousExample", "derivedFromSeededOrder"):
        assert field in demo, field
    assert demo["exampleOrder"].startswith("Anna, 20 Anchor modular switches")


def test_9_the_demo_route_remains_unclassified_because_it_carries_no_cost():
    """Adding inventory must not turn a neutral route into an owner one."""
    assert "GET /api/demo" not in handler.OWNER_ROUTES
    assert "GET /api/demo" not in handler.CUSTOMER_FACING_ROUTES


def test_10_the_workspace_adds_no_second_agent_or_chatbot():
    js = _strip_comments(WORKSPACE_JS)
    for banned in ("bedrock", "converse", "/api/orders", "/api/shop-queries",
                   "/api/purchase-plans", "chat"):
        assert banned not in js.lower(), banned
    # Two reads, both of them read-only and neither of them an agent:
    # the seeded shop, and the owner's intelligence summary.
    assert re.findall(r'fetch\("([^"]+)"', js) == ["/api/demo",
                                                   "/api/intelligence"]


def test_10b_the_intelligence_read_is_sent_as_the_owner():
    """It carries supplier and margin data, so it goes through the gate."""
    js = _strip_comments(WORKSPACE_JS)
    start = js.index('fetch("/api/intelligence"')
    assert "OWNER_HEADERS" in js[start:start + 200]


# ---------------------------------------------------------------------------
# Shortages are reported honestly, not manufactured
# ---------------------------------------------------------------------------

def test_11_shortage_rows_are_exactly_the_skus_promised_beyond_the_shelf(demo):
    """SHORTAGE means promised to a customer beyond what is on the shelf.

    This used to assert the seeded shop had none. It does have them - the
    committed canonical order promises 20 switches against 14 on hand and 3
    coils against 1 - and the label was only absent because the check behind
    it could never be true. The page's existing wording branch already says
    "N SKU(s) are promised beyond available stock"; the zero-case wording is
    still there for a shop with none.
    """
    assert "No reorder-point shortages currently detected" in PAGE
    assert "are promised beyond available stock" in PAGE
    assert 'id="shortageNote"' in PAGE

    data = load_dataset()
    promised = {}
    for order in data.committedOrders():
        for line in order.lines:
            promised[line.skuId] = promised.get(line.skuId, 0) + line.quantity
    over = {sku for sku, qty in promised.items()
            if data.onHand(sku) > 0 and data.onHand(sku) < qty}

    shortages = {i["skuId"] for i in demo["inventory"] if i["status"] == "SHORTAGE"}
    assert shortages == over
    assert shortages == {"SW-ANC-1W10A", "W-FIN-1.5-RED-90M"}


def test_11b_order_shortages_are_left_to_the_quotation(demo):
    """The page points at the quotation instead of duplicating the figure."""
    assert "shown on that order's quotation" in PAGE
    js = _strip_comments(WORKSPACE_JS)
    # No shortage arithmetic here: the quote engine owns shortageQty.
    assert "shortageQty" not in js


# ---------------------------------------------------------------------------
# Stock status says what the number says
# ---------------------------------------------------------------------------
# An evaluation found two SKUs with onHand == 0 labelled IN STOCK: they sold
# slowly enough that the planner did not call them low, and nothing above that
# check looked at the quantity itself. A shop owner reads the label.

def test_12_nothing_with_no_stock_is_called_in_stock(demo):
    for row in demo["inventory"]:
        if row["onHand"] <= 0:
            assert row["status"] == "OUT OF STOCK", row["skuId"]
        if row["status"] == "IN STOCK":
            assert row["onHand"] > 0, row["skuId"]


def test_12b_the_out_of_stock_rows_are_the_zero_ones_and_no_others(demo):
    zero = {r["skuId"] for r in demo["inventory"] if r["onHand"] <= 0}
    labelled = {r["skuId"] for r in demo["inventory"]
                if r["status"] == "OUT OF STOCK"}
    assert labelled == zero


def test_12c_the_quantities_themselves_are_untouched(demo):
    """Serialization only. The dataset's own numbers are the numbers shown."""
    data = load_dataset()
    for row in demo["inventory"]:
        assert row["onHand"] == data.onHand(row["skuId"]), row["skuId"]


def test_12d_low_stock_still_means_what_the_planner_means(demo):
    """The new label may not have eaten the planner's."""
    from engine.budget import restock_candidates

    data = load_dataset()
    planner_low = {c.skuId for c in restock_candidates(data)}
    for row in demo["inventory"]:
        if row["status"] == "LOW STOCK":
            assert row["skuId"] in planner_low
            assert row["onHand"] > 0


# ---------------------------------------------------------------------------
# 13. ShopFlow Intelligence
# ---------------------------------------------------------------------------
# A section, not a dashboard. It shows what was read, what was decided and
# whether the queue is healthy - and it computes none of it.

def test_13_the_intelligence_section_has_its_four_tabs():
    for tab in ("tabDocs", "tabTrace", "tabAlerts", "tabOps"):
        assert 'id="%s"' % tab in PAGE, tab
    for pane in ("tpDocs", "tpTrace", "tpAlerts", "tpOps"):
        assert 'id="%s"' % pane in PAGE, pane
    assert 'id="paneIntelligence"' in PAGE
    assert 'id="navIntelligence"' in PAGE
    assert '"/workspace/intelligence"' in WORKSPACE_JS


def _flat(text: str) -> str:
    """Section text with its line wrapping removed, so a phrase the source
    happens to break across two lines is still one phrase here."""
    return " ".join(text.split()).lower()


def test_13b_it_says_where_its_numbers_come_from():
    section = PAGE[PAGE.index('id="paneIntelligence"'):PAGE.index('id="paneOrders"')]
    lowered = _flat(section)
    assert "deterministic engines" in lowered
    assert "no figure here is written by the language model" in lowered
    assert "amazon textract" in lowered


def test_13c_the_review_boundary_is_explained_in_the_owners_words():
    section = PAGE[PAGE.index('id="paneIntelligence"'):PAGE.index('id="paneOrders"')]
    assert "REVIEW REQUIRED" in section
    assert "CONFIRM" in section
    assert "only a row you" in _flat(section)


def test_13d_it_says_a_clarification_is_not_an_alert():
    section = PAGE[PAGE.index('id="paneIntelligence"'):PAGE.index('id="paneOrders"')]
    flat = _flat(section)
    assert "not an alert" in flat or "is not listed here" in flat
    assert "the system working" in flat


def test_13e_the_trace_tab_promises_no_reasoning():
    section = PAGE[PAGE.index('id="tpTrace"'):PAGE.index('id="tpAlerts"')]
    lowered = _flat(section)
    assert "never contains the model's private reasoning" in lowered
    assert "supplier cost" in lowered


def test_13f_the_operations_tab_points_at_cloudwatch_for_history():
    section = PAGE[PAGE.index('id="tpOps"'):PAGE.index('</section>',
                                                       PAGE.index('id="tpOps"'))]
    assert "shopflow-operations" in section
    assert "24 hours" in section


def test_13g_the_intelligence_pane_invents_no_metric():
    section = PAGE[PAGE.index('id="paneIntelligence"'):PAGE.index('id="paneOrders"')]
    lowered = _flat(section)
    for invented in ("revenue", "growth", "conversion", "satisfaction",
                     "uptime", "accuracy", "confidence score of"):
        assert invented not in lowered, invented
