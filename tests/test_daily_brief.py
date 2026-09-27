"""Today's shop brief: engine figures only, a summary that is kept only when
every number in it is the brief's, and a brief that stands without it.
"""

from __future__ import annotations

import copy
import json

import pytest

from agent.brief_summary import summarize_brief
from engine import brief as eb
from engine.loader import load_dataset
from observability import events
from test_price_alerts import AlertTable, Bus
from test_queue import worker  # noqa: F401

WIRE = "W-FIN-1.5-RED-90M"
CONFIRMED = {WIRE: 6300.0}
NOW = 1790500000                       # 2026-09-27 in India


@pytest.fixture(scope="module")
def shop():
    return load_dataset()


@pytest.fixture(scope="module")
def brief(shop):
    return eb.build_brief(shop, CONFIRMED, now=NOW)


# ---------------------------------------------------------------------------
# the structure
# ---------------------------------------------------------------------------

def test_no_confirmed_cost_means_no_supplier_or_margin_items(shop):
    b = eb.build_brief(shop, {}, now=NOW)
    assert b["supplierAlerts"] == [] and b["marginRisks"] == []
    assert b["walkAway"] == []
    assert any("no confirmed supplier price change" in l for l in b["lines"])
    assert b["grounded"] is True


def test_the_date_is_the_shop_date(brief):
    assert brief["date"] == "2026-09-27"


def test_shortages_are_the_planner_commitments(brief):
    by_sku = {s["skuId"]: s for s in brief["shortages"]}
    assert set(by_sku) == {"SW-ANC-1W10A", WIRE}
    assert (by_sku["SW-ANC-1W10A"]["committedQty"],
            by_sku["SW-ANC-1W10A"]["onHand"],
            by_sku["SW-ANC-1W10A"]["shortageQty"]) == (20, 14, 6)
    assert by_sku[WIRE]["shortageQty"] == 2
    assert all(s["funded"] for s in brief["shortages"])


def test_low_stock_is_the_planner_restock_tier(shop, brief):
    from engine.whatif import _plan
    plan = _plan(shop, 25000.0, CONFIRMED)
    assert brief["counts"]["lowStock"] == \
        len(plan["restockSelected"]) + len(plan["restockDeferred"])
    risks = [x["stockoutRisk"] for x in brief["lowStock"]]
    assert risks == sorted(risks, reverse=True)
    assert len(brief["lowStock"]) <= eb.MAX_LOW_STOCK


def test_the_supplier_alert_is_the_price_alert_engine(brief):
    [a] = brief["supplierAlerts"]
    assert (a["oldCost"], a["newCost"], a["absoluteDelta"],
            a["percentageDelta"]) == (5900.0, 6300.0, 400.0, 6.78)
    assert a["severity"] == "CRITICAL"


def test_the_margin_risk(brief):
    [m] = brief["marginRisks"]
    assert m["skuId"] == WIRE and m["status"] == "LOW_MARGIN"
    assert (m["marginAmount"], m["marginPercent"]) == (308.0, 4.66)


def test_the_planner_budget(brief):
    p = brief["planner"]
    assert (p["budget"], p["commitmentCost"], p["restockCost"],
            p["totalSpend"], p["remaining"]) == \
        (25000.0, 12948.0, 12045.16, 24993.16, 6.84)


def test_the_walk_away_price(brief):
    [w] = brief["walkAway"]
    assert (w["walkAwayPrice"], w["currentCost"], w["aboveBy"]) == \
        (5947.20, 6300.0, 352.80)


def test_the_combined_brief_reads_as_the_example(brief):
    text = "\n".join(brief["lines"])
    assert "2 product(s) short against customer commitments" in text
    assert "₹5,900.00 -> ₹6,300.00 (+6.78%)" in text
    assert "Margin: ₹708.00 -> ₹308.00." in text
    assert "maximum supplier cost ₹5,947.20" in text
    assert "₹24,993.16 planned from ₹25,000.00; ₹6.84 remaining." in text
    first, second = brief["priorityActions"][:2]
    assert first["kind"] == "RENEGOTIATE_OR_REPRICE"
    assert first["text"] == (
        "Do not add discretionary restock of Finolex 1.5 sqmm FR Wire Red 90m "
        "coil at ₹6,300.00 because it exceeds the ₹5,947.20 walk-away price. "
        "Renegotiate or change the selling price.")
    assert second["kind"] == "BUY_FOR_COMMITMENTS"
    assert second["text"] == (
        "Buy the quantity required to fulfil existing commitments: 2 line(s), "
        "₹12,948.00 of the ₹25,000.00 budget. Keeping these promises costs "
        "₹705.60 more than at the walk-away price.")
    assert brief["hasAttention"] is True


def test_every_number_in_the_words_is_in_the_structure(brief):
    assert brief["grounded"] is True
    assert brief["ungroundedNumbers"] == []


def test_an_unfunded_commitment_becomes_a_critical_action(shop):
    b = eb.build_brief(shop, CONFIRMED, budget=5000, now=NOW)
    assert b["counts"]["unfundedShortages"] >= 1
    assert any(a["kind"] == "FUND_COMMITMENT" and a["severity"] == "CRITICAL"
               for a in b["priorityActions"])
    assert b["grounded"] is True


def test_it_is_deterministic_and_writes_nothing():
    shop = load_dataset()
    snapshot = copy.deepcopy(shop)
    confirmed = dict(CONFIRMED)
    first = eb.build_brief(shop, confirmed, now=NOW)
    assert eb.build_brief(shop, confirmed, now=NOW) == first
    assert shop.products == snapshot.products
    assert shop.inventory == snapshot.inventory
    assert confirmed == CONFIRMED


def test_the_signature_follows_the_figures(shop, brief):
    assert eb.brief_signature(brief) == \
        eb.brief_signature(eb.build_brief(shop, CONFIRMED, now=NOW))
    assert eb.brief_signature(brief) != \
        eb.brief_signature(eb.build_brief(shop, {}, now=NOW))


# ---------------------------------------------------------------------------
# the optional summary
# ---------------------------------------------------------------------------

class Model:
    def __init__(self, text=None, error=None):
        self.text, self.error, self.calls = text, error, 0

    def converse(self, **_kw):
        self.calls += 1
        if self.error:
            raise self.error
        return {"output": {"message": {"content": [{"text": self.text}]}}}


def test_a_grounded_summary_is_kept(brief):
    model = Model("Finolex went from ₹5,900.00 to ₹6,300.00; do not restock "
                  "above ₹5,947.20. ₹6.84 of ₹25,000.00 is left after the plan.")
    s = summarize_brief(brief, model, "m")
    assert s and s["grounded"] is True and model.calls == 1


def test_a_summary_with_an_invented_number_is_rejected(brief):
    model = Model("Finolex rose to ₹6,450.00; you have ₹9,999.00 spare.")
    assert summarize_brief(brief, model, "m") is None


def test_a_failed_model_call_returns_nothing_and_does_not_raise(brief):
    from botocore.exceptions import ClientError
    err = ClientError({"Error": {"Code": "ThrottlingException"}}, "Converse")
    assert summarize_brief(brief, Model(error=err), "m") is None


def test_no_model_call_when_nothing_needs_attention(brief):
    quiet = {**brief, "hasAttention": False}
    model = Model("anything")
    assert summarize_brief(quiet, model, "m") is None
    assert model.calls == 0


# ---------------------------------------------------------------------------
# the scheduled run on the worker
# ---------------------------------------------------------------------------

@pytest.fixture
def scheduled(worker, monkeypatch):  # noqa: F811
    table = AlertTable(CONFIRMED)
    bus = Bus()
    monkeypatch.setattr(worker, "_table", table)
    monkeypatch.setenv("EVENT_BUS_NAME", "shopflow-business-events")
    monkeypatch.setattr(events, "_client", bus)
    return worker, table, bus


def test_the_schedule_input_runs_the_brief_and_stores_it(scheduled, monkeypatch):
    worker, table, bus = scheduled
    import agent.orchestrator as orch
    model = Model("Do not restock Finolex above ₹5,947.20.")
    monkeypatch.setattr(orch, "_bedrock_client", lambda: model)
    out = worker.handler({"shopflowTask": "DAILY_BRIEF"}, None)
    assert out["status"] == "DONE" and out["summarized"] is True
    stored = table.items[("BRIEF#demo", "LATEST")]
    assert json.loads(stored["summary"])["grounded"] is True
    assert stored["signature"] == out["signature"]
    [entry] = bus.entries
    assert entry["DetailType"] == "DailyShopBriefGenerated"
    detail = json.loads(entry["Detail"])
    assert detail["plannedSpend"] == 24993.16 and detail["remaining"] == 6.84
    assert detail["criticalAlerts"] == 1


def test_a_bedrock_failure_still_stores_the_deterministic_brief(scheduled,
                                                                 monkeypatch):
    worker, table, _bus = scheduled
    from botocore.exceptions import ClientError
    import agent.orchestrator as orch
    monkeypatch.setattr(orch, "_bedrock_client", lambda: Model(
        error=ClientError({"Error": {"Code": "ThrottlingException"}}, "Converse")))
    out = worker.run_daily_brief(now=NOW)
    assert out["status"] == "DONE" and out["summarized"] is False
    stored = table.items[("BRIEF#demo", "LATEST")]
    assert stored["summary"] == ""
    assert json.loads(stored["counts"])["supplierAlerts"] == 1


def test_a_brief_failure_is_logged_not_raised(scheduled, monkeypatch, capsys):
    worker, _table, _bus = scheduled
    monkeypatch.setattr(worker, "_confirmed_cost_map",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert worker.handler({"shopflowTask": "DAILY_BRIEF"}, None) == \
        {"status": "FAILED"}
    assert "daily_brief_failed" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# the owner route serves it; the customer never sees it
# ---------------------------------------------------------------------------

@pytest.fixture
def api_table(monkeypatch):
    import lambdas.api.handler as api
    from test_api import FakeTable
    table = FakeTable()
    table.put_item({"PK": "SHOP#demo", "SK": f"COST#{WIRE}", "skuId": WIRE,
                    "confirmedCost": 6300.0, "confirmedAt": 1,
                    "sourceJobId": "d" * 32})
    monkeypatch.setenv("TABLE_NAME", "shopflow-demo")
    monkeypatch.setattr(api, "table", lambda: table)
    return api, table


def _intel(api, owner=True):
    event = {"routeKey": "GET /api/intelligence"}
    if owner:
        event["headers"] = {"x-shopflow-demo-owner": "demo-workspace"}
    return api.handler(event, None)


def test_the_intelligence_route_serves_the_brief(api_table):
    api, _table = api_table
    body = json.loads(_intel(api)["body"])
    b = body["brief"]
    assert b["planner"]["remaining"] == 6.84
    assert b["walkAway"][0]["walkAwayPrice"] == 5947.20
    assert b["summary"] == {"available": False, "reason": "NOT_YET_GENERATED"}
    assert body["alertDelivery"]["configured"] is False
    alert = next(a for a in body["alerts"] if a["kind"] == "SUPPLIER_PRICE_CHANGED")
    assert alert["severity"] == "CRITICAL"
    assert alert["priceAlert"]["percentageDelta"] == 6.78


def test_a_stored_summary_is_shown_only_for_the_same_figures(api_table):
    api, table = api_table
    b = json.loads(_intel(api)["body"])["brief"]
    table.put_item({"PK": "BRIEF#demo", "SK": "LATEST", "briefDate": b["date"],
                    "generatedAt": 1, "signature": eb.brief_signature(b),
                    "summary": json.dumps({"text": "ok", "grounded": True})})
    assert json.loads(_intel(api)["body"])["brief"]["summary"]["text"] == "ok"
    table.put_item({"PK": "BRIEF#demo", "SK": "LATEST", "briefDate": b["date"],
                    "generatedAt": 1, "signature": "stale",
                    "summary": json.dumps({"text": "old", "grounded": True})})
    s = json.loads(_intel(api)["body"])["brief"]["summary"]
    assert s["available"] is False and s["reason"] == "FIGURES_CHANGED"


def test_the_brief_is_behind_the_owner_gate(api_table):
    api, _table = api_table
    response = _intel(api, owner=False)
    assert response["statusCode"] == 401
    for leaked in ("5947", "6300", "brief", "walkAway"):
        assert leaked not in response["body"]
