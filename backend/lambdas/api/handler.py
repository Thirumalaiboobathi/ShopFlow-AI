"""ShopFlow public API.

Three routes, no more than the workflow needs:

    POST /api/orders      accept an order, queue it, return a job id
    GET  /api/jobs/{id}   poll that job
    GET  /api/demo        the seeded example order, so the UI hard-codes nothing

Order processing is asynchronous. A Bedrock tool loop takes seconds, and a
public demo that holds an HTTP connection open for that long is a reliability
risk, so the API queues the work and the browser polls.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from decimal import Decimal

from functools import lru_cache

from engine.loader import cached_dataset

MAX_ORDER_CHARS = 1000
MAX_BODY_BYTES = 4096
MAX_CLARIFICATIONS = 10

JOB_TTL_SECONDS = int(os.environ.get("JOB_TTL_SECONDS", 60 * 60 * 24))


# Clients are built on first use rather than at import, so the module can be
# imported and exercised in a test without AWS credentials.
@lru_cache(maxsize=1)
def table():
    import boto3

    return boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])


@lru_cache(maxsize=1)
def lambda_client():
    import boto3

    return boto3.client("lambda")

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _response(status: int, body: dict) -> dict:
    return {
        "statusCode": status,
        "headers": {"content-type": "application/json", "cache-control": "no-store"},
        "body": json.dumps(body, default=_json_default),
    }


def _json_default(value):
    if isinstance(value, Decimal):
        return float(value)
    raise TypeError(f"not serialisable: {type(value)}")


def _job_key(job_id: str) -> dict:
    return {"PK": f"JOB#{job_id}", "SK": "META"}


def _create_order(event) -> dict:
    raw = event.get("body") or ""
    if len(raw.encode("utf-8")) > MAX_BODY_BYTES:
        return _response(413, {"error": "request body too large"})

    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return _response(400, {"error": "body must be JSON"})

    order_text = _CONTROL.sub("", str(payload.get("orderText") or "")).strip()
    if not order_text:
        return _response(400, {"error": "orderText is required"})
    if len(order_text) > MAX_ORDER_CHARS:
        return _response(
            400, {"error": f"orderText must be {MAX_ORDER_CHARS} characters or fewer"})

    # Owner-confirmed answers to an earlier clarification. Validated here so an
    # invented SKU can never re-enter the flow through the front door.
    clarifications = payload.get("clarifications") or []
    if not isinstance(clarifications, list) or len(clarifications) > MAX_CLARIFICATIONS:
        return _response(400, {"error": "clarifications must be a short array"})

    data = cached_dataset()
    cleaned = []
    for item in clarifications:
        if not isinstance(item, dict):
            return _response(400, {"error": "each clarification must be an object"})
        sku_id = str(item.get("skuId") or "")
        if sku_id not in data.products:
            return _response(400, {"error": f"unknown skuId: {sku_id}"})
        cleaned.append({
            "requestedText": _CONTROL.sub("", str(item.get("requestedText") or ""))[:200],
            "skuId": sku_id,
        })

    job_id = uuid.uuid4().hex
    now = int(time.time())
    table().put_item(Item={
        **_job_key(job_id),
        "jobId": job_id,
        "status": "QUEUED",
        "orderText": order_text,
        "clarifications": cleaned,
        "createdAt": now,
        "expiresAt": now + JOB_TTL_SECONDS,
    })

    lambda_client().invoke(
        FunctionName=os.environ["WORKER_FUNCTION_NAME"],
        InvocationType="Event",
        Payload=json.dumps({"jobId": job_id}).encode("utf-8"),
    )

    return _response(202, {"jobId": job_id, "status": "QUEUED"})


def _get_job(event) -> dict:
    job_id = (event.get("pathParameters") or {}).get("jobId") or ""
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        return _response(400, {"error": "invalid job id"})

    item = table().get_item(Key=_job_key(job_id)).get("Item")
    if not item:
        return _response(404, {"error": "job not found"})

    body = {
        "jobId": item["jobId"],
        "status": item["status"],
        "orderText": item.get("orderText"),
        "createdAt": int(item.get("createdAt", 0)),
    }
    if item.get("result"):
        body["result"] = json.loads(item["result"])
    if item.get("error"):
        body["error"] = item["error"]
    return _response(200, body)


def _get_demo(event) -> dict:
    """Example inputs, read from the seeded shop so the UI invents nothing."""
    data = cached_dataset()
    order = data.orders[0] if data.orders else None
    example = ""
    if order:
        parts = []
        for line in order.lines:
            p = data.product(line.skuId)
            descriptor = " ".join(x for x in [p.brand, p.specification, p.colour] if x)
            parts.append(f"{line.quantity} {descriptor}")
        example = "Anna, " + ", ".join(parts) + "."

    return _response(200, {
        "shopName": "Demo Electricals, Madurai",
        "catalogSize": len(data.products),
        "exampleOrder": (
            "Anna, 20 Anchor modular switches, 3 coils Finolex 1.5 sq mm red wire, "
            "2 MCB 32 amp."
        ),
        "ambiguousExample": "Anna, 3 coils Finolex 1.5 sq mm wire.",
        "derivedFromSeededOrder": example,
    })


ROUTES = {
    "POST /api/orders": _create_order,
    "GET /api/jobs/{jobId}": _get_job,
    "GET /api/demo": _get_demo,
}


def handler(event, context):
    route = event.get("routeKey") or ""
    fn = ROUTES.get(route)
    if fn is None:
        return _response(404, {"error": "not found"})
    try:
        return fn(event)
    except Exception as exc:  # noqa: BLE001 - surface a safe message, log detail
        print(f"ERROR handling {route}: {type(exc).__name__}: {exc}")
        return _response(500, {"error": "internal error"})
