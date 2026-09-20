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

import base64
from functools import lru_cache

from boto3.dynamodb.conditions import Key

from engine.loader import cached_dataset
from engine.supplier_prices import DECISIONS, build_decision_record

MAX_ORDER_CHARS = 1000
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
        "jobType": JOB_ORDER,
        "status": "QUEUED",
        "orderText": order_text,
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
    table().put_item(Item={
        "PK": f"DECISION#{job_id}",
        "SK": f"SKU#{sku_id}",
        **record,
        "decidedAt": now,
        "expiresAt": now + JOB_TTL_SECONDS,
    })
    return _response(201, record)


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
