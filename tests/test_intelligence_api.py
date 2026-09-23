"""The Intelligence route, and the two job polls either side of it.

Three things are being held in place here.

A supplier price list IS supplier cost: every matched row on it carries what
the shop last paid and what the document says it will now pay. Uploading one
has always needed the owner gate; reading the answer back did not, which meant
those costs were one poll away from anybody holding a job id. They are behind
the same gate now.

An order's decision trace is the opposite case. It explains a quotation to the
person who placed it, so it travels on the customer-facing job route - and it
is therefore built by `decision_trace.customer_steps`, which cannot produce a
supplier cost, a margin or a budget at all.

And the Intelligence route itself: one owner read, answering what has been
uploaded, what needs attention and whether the queue is healthy, from the
engines that already decide those things.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))
sys.path.insert(0, str(ROOT / "backend" / "lambdas" / "api"))

import handler as api  # noqa: E402
from test_api import FakeQueue, FakeS3, FakeTable, body_of  # noqa: E402

OWNER_HEADERS = {"x-shopflow-demo-owner": "demo-workspace"}

WIRE = "W-FIN-1.5-RED-90M"
SWITCH = "SW-ANC-1W10A"
MCB = "MCB-HAV-SP-32A-C"


@pytest.fixture
def env(monkeypatch):
    table, queue, s3 = FakeTable(), FakeQueue(), FakeS3()
    monkeypatch.setenv("TABLE_NAME", "shopflow-demo")
    monkeypatch.setenv(
        "ORDERS_QUEUE_URL",
        "https://sqs.ap-south-1.amazonaws.com/000000000000/shopflow-orders")
    monkeypatch.setenv("UPLOADS_BUCKET", "shopflow-uploads-test")
    monkeypatch.setattr(api, "table", lambda: table)
    monkeypatch.setattr(api, "sqs_client", lambda: queue)
    monkeypatch.setattr(api, "s3_client", lambda: s3)
    return table, queue, s3


def order_job(table, result: dict) -> str:
    """A finished order job on the table, as the worker would have left it."""
    job_id = "a" * 32
    table.items[(f"JOB#{job_id}", "META")] = {
        "PK": f"JOB#{job_id}", "SK": "META", "jobId": job_id,
        "jobType": "ORDER", "status": "DONE", "createdAt": 1_758_000_000,
        "orderText": "20 Anchor modular switches", "language": "en",
        "result": json.dumps(result),
    }
    return job_id


def price_list_job(table, review: dict) -> str:
    job_id = "b" * 32
    table.items[(f"JOB#{job_id}", "META")] = {
        "PK": f"JOB#{job_id}", "SK": "META", "jobId": job_id,
        "jobType": "PRICE_LIST", "status": "DONE", "createdAt": 1_758_000_000,
        "imageContentType": "image/png", "imageBytes": 40016,
        "result": json.dumps({"status": "REVIEWED", "jobType": "PRICE_LIST",
                              "review": review}),
    }
    return job_id


REVIEW = {
    "supplierName": "Sri Balaji Electricals", "documentDate": "15-09-2026",
    "lineCount": 2, "matchedCount": 2, "ambiguousCount": 0,
    "unmatchedCount": 0, "materialChangeCount": 1, "reviewRequiredCount": 1,
    "lowConfidenceCount": 0, "readers": ["TEXTRACT"],
    "lines": [
        {"skuId": WIRE, "status": "MATCHED", "reviewState": "REVIEW_REQUIRED",
         "line": {"description": "Finolex 1.5 sqmm FR Wire RED 90m coil",
                  "price": 6300.0, "confidence": 99.3, "source": "TEXTRACT"},
         "comparison": {"previousPrice": 5900.0, "currentPrice": 6300.0,
                        "percentageDelta": 6.78, "materialChange": True}},
        {"skuId": MCB, "status": "MATCHED", "reviewState": "MATCHED",
         "line": {"description": "Havells MCB SP 32A C-Curve", "price": 358.0,
                  "confidence": 99.7, "source": "TEXTRACT"},
         "comparison": {"previousPrice": 358.0, "currentPrice": 358.0,
                        "percentageDelta": 0.0, "materialChange": False}},
    ],
}

QUOTED_RESULT = {
    "status": "QUOTED", "summary": "Quotation ready.",
    "matches": [{"requestedText": "20 Anchor modular switches",
                 "status": "RESOLVED", "skuId": SWITCH}],
    "quote": {"lines": [{"skuId": SWITCH, "name": "Anchor Modular Switch",
                         "quantity": 20, "onHand": 14, "shortageQty": 6,
                         "sellingPrice": 78.3, "lineTotal": 1566.0,
                         "catalogueUom": "PIECE"}],
              "total": 1566.0, "lineCount": 1},
}


# ---------------------------------------------------------------------------
# 1. a price-list result is owner data
# ---------------------------------------------------------------------------

def test_polling_a_price_list_job_without_the_gate_is_refused(env):
    table, _q, _s = env
    job_id = price_list_job(table, REVIEW)

    response = api.handler({"routeKey": "GET /api/jobs/{jobId}",
                            "pathParameters": {"jobId": job_id}}, None)

    assert response["statusCode"] == 401
    assert response["headers"]["x-shopflow-audience"] == "owner"


def test_the_refusal_carries_no_supplier_price(env):
    table, _q, _s = env
    job_id = price_list_job(table, REVIEW)

    body = api.handler({"routeKey": "GET /api/jobs/{jobId}",
                        "pathParameters": {"jobId": job_id}}, None)["body"]

    for leaked in ("6300", "5900", "6.78", "previousPrice", "Sri Balaji"):
        assert leaked not in body, leaked


def test_the_owner_still_reads_the_whole_document(env):
    table, _q, _s = env
    job_id = price_list_job(table, REVIEW)

    body = body_of(api.handler(
        {"routeKey": "GET /api/jobs/{jobId}", "headers": OWNER_HEADERS,
         "pathParameters": {"jobId": job_id}}, None))

    review = body["result"]["review"]
    assert review["supplierName"] == "Sri Balaji Electricals"
    assert review["lines"][0]["comparison"]["percentageDelta"] == 6.78
    assert review["readers"] == ["TEXTRACT"]


def test_a_confirmed_row_reads_confirmed_after_a_reload(env):
    """The review state is derived, so a later ruling shows without a rewrite."""
    table, _q, _s = env
    job_id = price_list_job(table, REVIEW)
    table.items[(f"DECISION#{job_id}", f"SKU#{WIRE}")] = {
        "PK": f"DECISION#{job_id}", "SK": f"SKU#{WIRE}", "skuId": WIRE,
        "decision": "CONFIRMED",
    }

    body = body_of(api.handler(
        {"routeKey": "GET /api/jobs/{jobId}", "headers": OWNER_HEADERS,
         "pathParameters": {"jobId": job_id}}, None))

    states = {line["skuId"]: line["reviewState"]
              for line in body["result"]["review"]["lines"]}
    assert states[WIRE] == "CONFIRMED"
    assert states[MCB] == "MATCHED"
    assert body["result"]["review"]["confirmedCount"] == 1


def test_a_rejected_row_does_not_read_confirmed(env):
    table, _q, _s = env
    job_id = price_list_job(table, REVIEW)
    table.items[(f"DECISION#{job_id}", f"SKU#{WIRE}")] = {
        "PK": f"DECISION#{job_id}", "SK": f"SKU#{WIRE}", "skuId": WIRE,
        "decision": "REJECTED",
    }

    body = body_of(api.handler(
        {"routeKey": "GET /api/jobs/{jobId}", "headers": OWNER_HEADERS,
         "pathParameters": {"jobId": job_id}}, None))

    line = next(l for l in body["result"]["review"]["lines"]
                if l["skuId"] == WIRE)
    assert line["reviewState"] == "REVIEW_REQUIRED"


# ---------------------------------------------------------------------------
# 2. an order's trace is customer-facing, and safe
# ---------------------------------------------------------------------------

def test_an_order_job_carries_its_decision_trace(env):
    table, _q, _s = env
    job_id = order_job(table, QUOTED_RESULT)

    body = body_of(api.handler({"routeKey": "GET /api/jobs/{jobId}",
                                "pathParameters": {"jobId": job_id}}, None))

    trace = body["result"]["decisionTrace"]
    assert trace["available"] is True
    assert trace["audience"] == "customer"
    assert any(s["step"] == "SHORTAGE_DETECTED" for s in trace["steps"])
    assert any("short by 6" in line for line in trace["lines"])


def test_the_trace_on_a_customer_route_carries_no_owner_data(env):
    table, _q, _s = env
    job_id = order_job(table, QUOTED_RESULT)

    body = api.handler({"routeKey": "GET /api/jobs/{jobId}",
                        "pathParameters": {"jobId": job_id}}, None)["body"]
    trace = json.dumps(json.loads(body)["result"]["decisionTrace"])

    for internal in ("costPrice", "unitCost", "supplierPrice", "margin",
                     "budget", "plannedSpend", "previousCost"):
        assert internal not in trace, internal


def test_a_stored_trace_is_not_rebuilt_over(env):
    """The worker's own trace is kept; only a job without one is rebuilt."""
    table, _q, _s = env
    stored = {"available": True, "audience": "customer",
              "steps": [{"step": "ORDER_RECEIVED"}], "lines": ["from worker"]}
    job_id = order_job(table, dict(QUOTED_RESULT, decisionTrace=stored))

    body = body_of(api.handler({"routeKey": "GET /api/jobs/{jobId}",
                                "pathParameters": {"jobId": job_id}}, None))
    assert body["result"]["decisionTrace"]["lines"] == ["from worker"]


def test_an_order_poll_is_still_open_to_the_customer(env):
    table, _q, _s = env
    job_id = order_job(table, QUOTED_RESULT)

    response = api.handler({"routeKey": "GET /api/jobs/{jobId}",
                            "pathParameters": {"jobId": job_id}}, None)
    assert response["statusCode"] == 200
    assert response["headers"]["x-shopflow-audience"] == "customer"


# ---------------------------------------------------------------------------
# 3. the Intelligence route
# ---------------------------------------------------------------------------

def test_intelligence_needs_the_owner_gate(env):
    assert api.handler({"routeKey": "GET /api/intelligence"},
                       None)["statusCode"] == 401


def test_intelligence_reports_documents_alerts_and_operations(env):
    table, _q, _s = env
    price_list_job(table, REVIEW)
    order_job(table, QUOTED_RESULT)
    # Both jobs joined the index when they were created; these were written
    # directly, so they are indexed here the same way the API would.
    for (pk, _sk), item in list(table.items.items()):
        if pk.startswith("JOB#"):
            item.update(api._job_index(item["jobId"], item["jobType"],
                                       item["createdAt"]))

    body = body_of(api.handler({"routeKey": "GET /api/intelligence",
                                "headers": OWNER_HEADERS}, None))

    assert body["shopId"] == "SHOP#demo"
    assert body["synthetic"] is True
    assert "synthetic" in body["dataNotice"].lower()
    assert isinstance(body["documents"], list)
    assert isinstance(body["alerts"], list)
    assert set(body["operations"]) == {"counts", "outcomes", "window", "note"}


def test_a_document_summary_names_its_reader_and_review_count(env):
    table, _q, _s = env
    job_id = price_list_job(table, REVIEW)
    item = table.items[(f"JOB#{job_id}", "META")]
    item.update(api._job_index(job_id, "PRICE_LIST", item["createdAt"]))

    body = body_of(api.handler({"routeKey": "GET /api/intelligence",
                                "headers": OWNER_HEADERS}, None))

    document = body["documents"][0]
    assert document["reader"] == "TEXTRACT"
    assert document["supplierName"] == "Sri Balaji Electricals"
    assert document["lineCount"] == 2
    assert document["reviewRequiredCount"] == 1


def test_the_alerts_are_engine_verdicts_not_new_rules(env):
    body = body_of(api.handler({"routeKey": "GET /api/intelligence",
                                "headers": OWNER_HEADERS}, None))
    kinds = {a["kind"] for a in body["alerts"]}
    assert kinds <= {"SUPPLIER_PRICE_CHANGED", "LOW_MARGIN", "STOCKOUT",
                     "PROCESSING_FAILED"}
    for alert in body["alerts"]:
        assert alert["detail"]


def test_a_stockout_alert_matches_the_inventory_snapshot(env):
    """Out of stock here must mean out of stock on the inventory page."""
    body = body_of(api.handler({"routeKey": "GET /api/intelligence",
                                "headers": OWNER_HEADERS}, None))
    alerted = {a["skuId"] for a in body["alerts"] if a["kind"] == "STOCKOUT"
               and a["detail"] == "Nothing on the shelf."}

    inventory = body_of(api.handler({"routeKey": "GET /api/demo"}, None))
    empty = {row["skuId"] for row in inventory["inventory"]
             if row["status"] == "OUT OF STOCK"}
    assert alerted == empty


def test_intelligence_survives_an_index_that_is_not_there(env, monkeypatch):
    """An operational read may not be able to fail an owner's page."""
    table, _q, _s = env

    def broken_query(**_kwargs):
        raise RuntimeError("no such index")

    monkeypatch.setattr(table, "query", broken_query)
    response = api.handler({"routeKey": "GET /api/intelligence",
                            "headers": OWNER_HEADERS}, None)

    assert response["statusCode"] == 200
    assert body_of(response)["documents"] == []


def test_intelligence_says_whether_events_are_configured(env, monkeypatch):
    monkeypatch.delenv("EVENT_BUS_NAME", raising=False)
    assert body_of(api.handler({"routeKey": "GET /api/intelligence",
                                "headers": OWNER_HEADERS},
                               None))["eventsEnabled"] is False

    monkeypatch.setenv("EVENT_BUS_NAME", "shopflow-business-events")
    assert body_of(api.handler({"routeKey": "GET /api/intelligence",
                                "headers": OWNER_HEADERS},
                               None))["eventsEnabled"] is True


def test_a_new_job_joins_the_index(env):
    """Without this the Intelligence view would list nothing, forever."""
    table, _q, _s = env
    job_id = body_of(api.handler(
        {"routeKey": "POST /api/orders",
         "body": json.dumps({"orderText": "20 Anchor switches"})},
        None))["jobId"]

    item = table.items[(f"JOB#{job_id}", "META")]
    assert item["GSI1PK"] == "SHOP#demo"
    assert item["GSI1SK"].startswith("JOB#")
    assert item["GSI1SK"].endswith(job_id)


def test_a_promised_beyond_stock_alert_matches_the_shortage_rows(env):
    """The alert and the SHORTAGE label were both behind a check that could
    never be true. Each now says what the other says."""
    body = body_of(api.handler({"routeKey": "GET /api/intelligence",
                                "headers": OWNER_HEADERS}, None))
    promised = {a["skuId"] for a in body["alerts"] if a["kind"] == "STOCKOUT"
                and a["detail"] == "Promised beyond available stock."}

    inventory = body_of(api.handler({"routeKey": "GET /api/demo"}, None))
    short = {row["skuId"] for row in inventory["inventory"]
             if row["status"] == "SHORTAGE"}
    assert promised == short
    assert promised == {"SW-ANC-1W10A", "W-FIN-1.5-RED-90M"}
