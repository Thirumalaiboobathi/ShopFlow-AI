"""The customer's job view carries no stock level, model detail or tool data.

A live evaluation read `onHand`, `weeklyVelocity` and `coverageWeeks` from a
job marked `x-shopflow-audience: customer`, and a summary saying "short by 6
piece" - which, beside "20 ordered", is the shop's stock count. The owner's
view of the same job keeps all of it.
"""

from __future__ import annotations

import json

import lambdas.api.handler as api
from engine.loader import load_dataset
from engine.quote import calculate_quote

FORBIDDEN_KEYS = ("onHand", "weeklyVelocity", "coverageWeeks", "shortageQty",
                  "unitCost", "costPrice", "supplierCost", "margin", "modelId",
                  "turns", "elapsedMs", "matches", "toolCalls", "reasoning",
                  "quantityCheck", "customerId")


def _body():
    shop = load_dataset()
    quote = calculate_quote(shop, [("SW-ANC-1W10A", 20), ("W-FIN-1.5-RED-90M", 3),
                                   ("MCB-HAV-SP-32A-C", 2)]).as_dict()
    return {
        "jobId": "a" * 32, "jobType": "ORDER", "status": "DONE",
        "orderText": "20 Anchor ...", "customerId": "CUST-BALA-002",
        "language": "en", "createdAt": 1,
        "result": {
            "status": "QUOTED",
            "summary": ("3 items quoted at Rs 22306.48. Stock shortfall: Anchor "
                        "Modular Switch 1-Way 10A White short by 6 piece."),
            "quote": quote, "modelId": "apac.amazon.nova-pro-v1:0", "turns": 2,
            "elapsedMs": 3000.0, "matches": [{"skuId": "X", "candidates": []}],
            "quantityCheck": [{"proposedQuantity": 4}],
            "grounded": True, "ungroundedNumbers": [],
        },
    }


def test_customer_view_has_no_internal_fields():
    text = json.dumps(api.customer_job_view(_body()))
    for key in FORBIDDEN_KEYS:
        assert f'"{key}"' not in text, key


def test_customer_summary_does_not_reveal_stock_levels():
    view = api.customer_job_view(_body())
    summary = view["result"]["summary"]
    assert "short by" not in summary
    assert "6 piece" not in summary
    assert summary.startswith("3 items quoted at Rs 22306.48.")
    assert "not in stock right now" in summary
    assert view["result"]["quote"]["total"] == 22306.48
    assert [l["quantity"] for l in view["result"]["quote"]["lines"]] == [20, 3, 2]


def test_owner_view_keeps_the_operational_detail():
    body = _body()
    event = {"headers": {"x-shopflow-demo-owner": "demo-workspace"}}
    response = api._job_response(event, body)
    owner = json.loads(response["body"])
    assert response["headers"]["x-shopflow-audience"] == "owner"
    assert owner["result"]["quote"]["lines"][0]["onHand"] == 14
    assert "short by 6" in owner["result"]["summary"]


def test_anonymous_job_poll_gets_the_customer_view():
    response = api._job_response({"headers": {}}, _body())
    assert response["headers"]["x-shopflow-audience"] == "customer"
    assert '"onHand"' not in response["body"]
