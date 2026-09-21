"""ShopFlow public API.

Three routes, no more than the workflow needs:

    POST /api/orders              accept an order, queue it, return a job id
    POST /api/supplier-price-lists  read a photographed price list, queue it
    POST /api/price-decisions     record the owner's ruling on a price change
    POST /api/purchase-plans      allocate a cash budget across purchases
    POST /api/shop-queries        answer a spoken stock/price/availability ask
    GET  /api/customers           the shop's khata accounts (synthetic demo data)
    GET  /api/customers/{id}      one khata account
    POST /api/credit/check        deterministic credit decision for an amount
    GET  /api/jobs/{id}           poll a queued job
    GET  /api/demo                the seeded example, so the UI hard-codes nothing

Order and price-list processing are asynchronous. A Bedrock tool loop takes
seconds, and a public demo that holds an HTTP connection open for that long is
a reliability risk, so the API queues the work and the browser polls.

Purchase planning is NOT queued. It calls no model at all - it is the
deterministic allocator over an already-loaded dataset, measured at about 4 ms
for the full 147-SKU shop. Wrapping that in a job record, an asynchronous
Lambda invoke and a polling loop would add three round trips and a second of
latency to hide four milliseconds of work.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from decimal import Decimal

import base64
from functools import lru_cache

from boto3.dynamodb.conditions import Key

from engine.cost_records import (
    DEFAULT_SHOP_ID,
    InvalidCostRecordError,
    build_cost_record,
    cost_pk,
    latest_confirmed_costs,
    to_plan_decisions,
)
from engine.credit import (
    InvalidOrderTotalError,
    check_credit,
    customer_view,
    list_customers,
)
from engine.loader import cached_dataset
from engine.margin import margin_alerts, margin_view
from engine.purchasing import (
    InvalidBudgetError,
    build_purchase_plan,
    what_if,
)
from engine.supplier_prices import DECISIONS, build_decision_record
from engine.voice import MAX_TRANSCRIPT_CHARS, answer_shop_query

MAX_ORDER_CHARS = 1000
# A customer id is an internal key, not free text. Bounded and pattern-checked
# so a junk value cannot travel any further than this function.
MAX_CUSTOMER_ID_CHARS = 64
CUSTOMER_ID_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
MAX_BODY_BYTES = 4096
MAX_CLARIFICATIONS = 10

# Price-list uploads travel as base64 in the request body, which keeps the
# uploads bucket entirely private - the browser never holds a credential or a
# presigned URL. Base64 costs about a third in size, hence the two limits.
MAX_IMAGE_BYTES = 2_500_000
MAX_UPLOAD_BODY_BYTES = 4_000_000
ALLOWED_IMAGE_TYPES = {"image/png", "image/jpeg", "image/jpg", "image/webp"}
IMAGE_EXTENSIONS = {"image/png": "png", "image/jpeg": "jpg",
                    "image/jpg": "jpg", "image/webp": "webp"}

JOB_ORDER = "ORDER"
JOB_PRICE_LIST = "PRICE_LIST"

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


@lru_cache(maxsize=1)
def s3_client():
    import boto3

    return boto3.client("s3")

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


def _to_dynamo(value):
    """DynamoDB stores no floats, so prices are written as Decimal.

    Conversion goes through str() rather than Decimal(float) so 6300.0 is
    stored as 6300.0 and not as its binary expansion.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, float):
        return Decimal(str(value))
    if isinstance(value, dict):
        return {k: _to_dynamo(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_dynamo(v) for v in value]
    return value


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

    # The khata account this order goes on, if any. Optional on purpose: the
    # anonymous counter sale is the common case and must behave exactly as it
    # always has, with no credit block anywhere in the result.
    customer_id = _CONTROL.sub("", str(payload.get("customerId") or "")).strip()
    if customer_id:
        if len(customer_id) > MAX_CUSTOMER_ID_CHARS or not CUSTOMER_ID_PATTERN.match(
                customer_id):
            return _response(400, {"error": "invalid customerId"})

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
        "jobType": JOB_ORDER,
        "status": "QUEUED",
        "orderText": order_text,
        "customerId": customer_id,
        "clarifications": cleaned,
        "createdAt": now,
        "expiresAt": now + JOB_TTL_SECONDS,
    })

    _start_worker(job_id)
    return _response(202, {"jobId": job_id, "status": "QUEUED"})


def _start_worker(job_id: str) -> None:
    lambda_client().invoke(
        FunctionName=os.environ["WORKER_FUNCTION_NAME"],
        InvocationType="Event",
        Payload=json.dumps({"jobId": job_id}).encode("utf-8"),
    )


def _create_price_list(event) -> dict:
    """Accept a photographed supplier price list and queue it for reading."""
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        # API Gateway may hand the body back already encoded; the JSON we want
        # is inside it either way.
        try:
            raw = base64.b64decode(raw).decode("utf-8")
        except Exception:
            return _response(400, {"error": "body could not be decoded"})

    if len(raw.encode("utf-8")) > MAX_UPLOAD_BODY_BYTES:
        return _response(413, {"error": "uploaded file is too large"})

    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return _response(400, {"error": "body must be JSON"})

    content_type = str(payload.get("contentType") or "").lower().strip()
    if content_type not in ALLOWED_IMAGE_TYPES:
        return _response(400, {
            "error": "contentType must be one of "
                     + ", ".join(sorted(ALLOWED_IMAGE_TYPES))})

    encoded = payload.get("imageBase64")
    if not isinstance(encoded, str) or not encoded:
        return _response(400, {"error": "imageBase64 is required"})

    try:
        image_bytes = base64.b64decode(encoded, validate=True)
    except Exception:
        return _response(400, {"error": "imageBase64 is not valid base64"})

    if not image_bytes:
        return _response(400, {"error": "image is empty"})
    if len(image_bytes) > MAX_IMAGE_BYTES:
        return _response(413, {
            "error": f"image must be {MAX_IMAGE_BYTES // 1000} KB or smaller"})

    job_id = uuid.uuid4().hex
    key = f"price-lists/{job_id}.{IMAGE_EXTENSIONS[content_type]}"

    # Private bucket, server-side encrypted. Nothing about this object is
    # reachable from the browser.
    s3_client().put_object(
        Bucket=os.environ["UPLOADS_BUCKET"],
        Key=key,
        Body=image_bytes,
        ContentType=content_type,
        ServerSideEncryption="AES256",
    )

    now = int(time.time())
    table().put_item(Item={
        **_job_key(job_id),
        "jobId": job_id,
        "jobType": JOB_PRICE_LIST,
        "status": "QUEUED",
        "imageKey": key,
        "imageContentType": content_type,
        "imageBytes": len(image_bytes),
        "createdAt": now,
        "expiresAt": now + JOB_TTL_SECONDS,
    })

    _start_worker(job_id)
    return _response(202, {"jobId": job_id, "status": "QUEUED",
                           "jobType": JOB_PRICE_LIST})


def _create_price_decision(event) -> dict:
    """Record the owner's ruling on one detected price change.

    Recording is all this does. The catalogue cost is not rewritten - see
    engine.supplier_prices.build_decision_record.
    """
    raw = event.get("body") or ""
    if len(raw.encode("utf-8")) > MAX_BODY_BYTES:
        return _response(413, {"error": "request body too large"})
    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return _response(400, {"error": "body must be JSON"})

    job_id = str(payload.get("jobId") or "")
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        return _response(400, {"error": "invalid job id"})

    decision = str(payload.get("decision") or "").upper().strip()
    if decision not in DECISIONS:
        return _response(400, {
            "error": "decision must be one of " + ", ".join(sorted(DECISIONS))})

    sku_id = str(payload.get("skuId") or "")
    data = cached_dataset()
    if sku_id not in data.products:
        return _response(400, {"error": f"unknown skuId: {sku_id}"})

    job = table().get_item(Key=_job_key(job_id)).get("Item")
    if not job or not job.get("result"):
        return _response(404, {"error": "price list job not found"})

    # The comparison is taken from the stored job, never from the request, so
    # a caller cannot post figures the engine did not produce.
    result = json.loads(job["result"])
    comparison = None
    for line in result.get("review", {}).get("lines", []):
        if line.get("skuId") == sku_id and line.get("comparison"):
            comparison = line["comparison"]
            break
    if comparison is None:
        return _response(400, {
            "error": "that SKU has no price change on this price list"})

    record = build_decision_record(data, job_id, sku_id, decision, comparison)
    now = int(time.time())
    table().put_item(Item=_to_dynamo({
        "PK": f"DECISION#{job_id}",
        "SK": f"SKU#{sku_id}",
        **record,
        "decidedAt": now,
        "expiresAt": now + JOB_TTL_SECONDS,
    }))

    # A confirmation is the only thing that may set the shop's purchase cost,
    # and this is the only route that can issue one. The decision record above
    # expires with the job; this one does not, because it is shop state rather
    # than the workings of one document.
    if decision == "CONFIRMED":
        try:
            cost_record = build_cost_record(
                data, sku_id, comparison.get("currentPrice"),
                source_job_id=job_id,
                confirmed_at=now,
                effective_date=result.get("review", {}).get("documentDate") or "",
            )
        except InvalidCostRecordError as exc:
            return _response(400, {"error": str(exc)})
        table().put_item(Item=_to_dynamo(cost_record))
        record = {
            **record,
            "purchaseCostPersisted": True,
            "confirmedCost": cost_record["confirmedCost"],
            "currency": cost_record["currency"],
            "confirmedAt": now,
            # The margin consequence of the cost the owner just accepted,
            # returned immediately so it is visible at the moment of the
            # decision rather than only on the next purchase plan. It reports;
            # it changes nothing, and the selling price is untouched.
            "marginProtection": margin_view(
                data, sku_id, cost_record["confirmedCost"]),
        }
    else:
        # Rejection records the ruling and nothing else - the shop keeps
        # planning at the cost it already knows.
        record = {**record, "purchaseCostPersisted": False}

    return _response(201, record)


def _create_purchase_plan(event) -> dict:
    """Allocate a stated cash budget across commitments and restocking.

    Answered synchronously: this is arithmetic over seeded data, not a model
    call. See the module docstring.

    An optional `priceListJobId` points at a completed supplier price review.
    Any change the owner CONFIRMED there is used as the purchase cost, which is
    the step build_decision_record deliberately left for the owner to trigger.
    Confirmed figures are read from the stored decision rows, never from the
    request, so a caller cannot price a plan at a number the engine never saw.
    """
    raw = event.get("body") or ""
    if len(raw.encode("utf-8")) > MAX_BODY_BYTES:
        return _response(413, {"error": "request body too large"})
    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return _response(400, {"error": "body must be JSON"})

    try:
        budget = float(payload.get("budget"))
    except (TypeError, ValueError):
        return _response(400, {"error": "budget must be a number"})

    confirmed = _confirmed_costs()
    decisions = to_plan_decisions(confirmed)

    # An explicit price-list job still works, and still wins: it is the owner
    # looking at one document and asking what it would mean. Kept for
    # backward compatibility with Stage 5 callers.
    job_id = str(payload.get("priceListJobId") or "")
    if job_id:
        if not re.fullmatch(r"[0-9a-f]{32}", job_id):
            return _response(400, {"error": "invalid job id"})
        job_rows = table().query(
            KeyConditionExpression=Key("PK").eq(f"DECISION#{job_id}")
        ).get("Items", [])
        by_sku = {d["skuId"]: d for d in decisions}
        for row in job_rows:
            sku = row.get("skuId")
            if not sku:
                continue
            by_sku[sku] = {
                "skuId": sku,
                "decision": row.get("decision"),
                "currentPrice": _to_float(row.get("currentPrice")),
                "previousPrice": _to_float(row.get("previousPrice")),
                "sourceJobId": job_id,
                "confirmedAt": int(row.get("decidedAt") or 0),
            }
        decisions = list(by_sku.values())

    data = cached_dataset()
    try:
        plan = build_purchase_plan(data, budget, decisions)
        plan["whatIf"] = what_if(data, budget, decisions)
    except InvalidBudgetError as exc:
        return _response(400, {"error": str(exc)})

    # What the confirmed costs did to the shop's margin. Read-only, computed
    # by the engine from the same three numbers the shop already holds, and
    # deliberately separate from the allocation above - no plan figure depends
    # on it, and no selling price is touched.
    alerts = margin_alerts(
        data, {sku: entry["cost"] for sku, entry in confirmed.items()})
    if alerts:
        plan["marginProtection"] = alerts

    return _response(200, plan)


def _confirmed_costs() -> dict:
    """The shop's durable confirmed purchase costs, keyed by SKU.

    Read wherever a confirmed cost matters - the planner, a margin view, a
    spoken margin question - so there is one query and one shape rather than
    three. A price the owner confirmed last week is what they pay today,
    whether or not they still have the document open.
    """
    rows = table().query(
        KeyConditionExpression=Key("PK").eq(cost_pk(DEFAULT_SHOP_ID))
        & Key("SK").begins_with("COST#")
    ).get("Items", [])
    return latest_confirmed_costs([
        {
            "skuId": row.get("skuId"),
            "confirmedCost": _to_float(row.get("confirmedCost")),
            "currency": row.get("currency"),
            "supplierId": row.get("supplierId"),
            "effectiveDate": row.get("effectiveDate"),
            "sourceJobId": row.get("sourceJobId"),
            "confirmedAt": row.get("confirmedAt"),
        }
        for row in rows
    ])


def _to_float(value):
    """Decimal from DynamoDB back to a plain float for the engine."""
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _create_shop_query(event) -> dict:
    """Answer a spoken shop question, or say which workflow should handle it.

    Synchronous and model-free, like the purchase planner: this is catalogue
    and inventory lookup through the existing matcher, so there is nothing to
    wait for.

    Only stock, price and availability are answered here. An order or a budget
    question is reported back with `delegateTo` so the caller uses the existing
    /api/orders and /api/purchase-plans endpoints - voice must not grow a
    second implementation of either workflow.

    A spoken request to confirm a supplier price is never executed. It comes
    back as NEEDS_HUMAN_CONFIRMATION, and the owner confirms on screen.
    """
    raw = event.get("body") or ""
    if len(raw.encode("utf-8")) > MAX_BODY_BYTES:
        return _response(413, {"error": "request body too large"})
    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return _response(400, {"error": "body must be JSON"})

    transcript = _CONTROL.sub("", str(payload.get("transcript") or "")).strip()
    if not transcript:
        return _response(400, {"error": "transcript is required"})
    if len(transcript) > MAX_TRANSCRIPT_CHARS:
        return _response(400, {
            "error": f"transcript must be {MAX_TRANSCRIPT_CHARS} characters or fewer"})

    # A margin question needs the shop's confirmed costs. Read here rather
    # than in the voice layer, which owns no data access of its own.
    costs = {sku: entry["cost"] for sku, entry in _confirmed_costs().items()}
    return _response(
        200, answer_shop_query(cached_dataset(), transcript, costs))


def _get_customers(event) -> dict:
    """Every khata account. Read-only, and synthetic to the last digit."""
    return _response(200, {
        "customers": list_customers(cached_dataset()),
        "synthetic": True,
        "note": "Demo customer records are synthetic. No real customer data "
                "is stored anywhere in this project.",
    })


def _get_customer(event) -> dict:
    customer_id = (event.get("pathParameters") or {}).get("customerId") or ""
    if not CUSTOMER_ID_PATTERN.match(customer_id):
        return _response(400, {"error": "invalid customerId"})

    customer = cached_dataset().customer(customer_id)
    if customer is None:
        return _response(404, {"error": "no khata account for that customer"})
    return _response(200, customer_view(customer))


def _create_credit_check(event) -> dict:
    """The credit decision for one amount against one khata account.

    Synchronous and model-free, like the purchase planner: it is a comparison
    between two numbers the shop already holds. Bedrock is never called for a
    credit decision, and no model contributes to one.

    Nothing is written. A credit check records no enquiry, moves no balance
    and changes no limit - it answers a question and stops.
    """
    raw = event.get("body") or ""
    if len(raw.encode("utf-8")) > MAX_BODY_BYTES:
        return _response(413, {"error": "request body too large"})
    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return _response(400, {"error": "body must be JSON"})

    customer_id = _CONTROL.sub("", str(payload.get("customerId") or "")).strip()
    if not customer_id:
        return _response(400, {"error": "customerId is required"})
    if not CUSTOMER_ID_PATTERN.match(customer_id):
        return _response(400, {"error": "invalid customerId"})

    try:
        result = check_credit(cached_dataset(), customer_id,
                              payload.get("orderTotal"))
    except InvalidOrderTotalError as exc:
        return _response(400, {"error": str(exc)})

    return _response(200, result)


def _get_job(event) -> dict:
    job_id = (event.get("pathParameters") or {}).get("jobId") or ""
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        return _response(400, {"error": "invalid job id"})

    item = table().get_item(Key=_job_key(job_id)).get("Item")
    if not item:
        return _response(404, {"error": "job not found"})

    body = {
        "jobId": item["jobId"],
        "jobType": item.get("jobType", JOB_ORDER),
        "status": item["status"],
        "orderText": item.get("orderText"),
        "customerId": item.get("customerId") or None,
        "createdAt": int(item.get("createdAt", 0)),
    }
    if item.get("result"):
        body["result"] = json.loads(item["result"])
    if item.get("error"):
        body["error"] = item["error"]

    if body["jobType"] == JOB_PRICE_LIST:
        # Owner rulings live alongside the job so a reload shows what was
        # already decided rather than asking again.
        rows = table().query(
            KeyConditionExpression=Key("PK").eq(f"DECISION#{job_id}")
        ).get("Items", [])
        body["decisions"] = [
            {k: v for k, v in row.items() if k not in ("PK", "SK", "expiresAt")}
            for row in rows
        ]
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
        # Every line names its variant, because the catalogue really does
        # stock several of each: six Anchor modular switches, Red 1.5 wire in
        # 90m and 180m, and six 32A MCBs across three brands. A vaguer order
        # is answered with a question, which is the point of the second
        # example below.
        "exampleOrder": (
            "Anna, 20 Anchor modular switches 1-Way 10A, "
            "3 coils Finolex 1.5 sq mm red wire 90m, "
            "2 Havells MCB SP 32A."
        ),
        "ambiguousExample": "Anna, 3 coils Finolex 1.5 sq mm wire.",
        "derivedFromSeededOrder": example,
    })


ROUTES = {
    "POST /api/orders": _create_order,
    "POST /api/supplier-price-lists": _create_price_list,
    "POST /api/price-decisions": _create_price_decision,
    "POST /api/purchase-plans": _create_purchase_plan,
    "POST /api/shop-queries": _create_shop_query,
    "GET /api/customers": _get_customers,
    "GET /api/customers/{customerId}": _get_customer,
    "POST /api/credit/check": _create_credit_check,
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
