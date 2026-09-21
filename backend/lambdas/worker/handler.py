"""Order-processing worker.

Invoked asynchronously by the API. Runs the bounded agent loop and writes the
result back onto the job record for the browser to poll.
"""

from __future__ import annotations

import json
import os
import time

import boto3
from boto3.dynamodb.conditions import Key

from agent.orchestrator import (
    DEFAULT_MODEL_ID,
    OrderTooLongError,
    run_order_agent,
)
from agent.vision import ExtractionError, extract_price_list
from engine.cost_records import DEFAULT_SHOP_ID, cost_pk, latest_confirmed_costs
from engine.credit import check_quote_credit
from engine.loader import cached_dataset
from engine.margin import margin_alerts, quotation_margin_impact
from engine.supplier_prices import InvalidSupplierLineError, review_price_list

TABLE_NAME = os.environ["TABLE_NAME"]
MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", DEFAULT_MODEL_ID)
UPLOADS_BUCKET = os.environ.get("UPLOADS_BUCKET", "")

JOB_ORDER = "ORDER"
JOB_PRICE_LIST = "PRICE_LIST"

_table = boto3.resource("dynamodb").Table(TABLE_NAME)


def _job_key(job_id: str) -> dict:
    return {"PK": f"JOB#{job_id}", "SK": "META"}


def _update(job_id: str, **fields) -> None:
    names = {f"#{k}": k for k in fields}
    values = {f":{k}": v for k, v in fields.items()}
    expression = "SET " + ", ".join(f"#{k} = :{k}" for k in fields)
    _table.update_item(
        Key=_job_key(job_id),
        UpdateExpression=expression,
        ExpressionAttributeNames=names,
        ExpressionAttributeValues=values,
    )


def _margin_protection(payload: dict):
    """Which confirmed supplier price changes touch this quotation.

    Attached to a finished quotation so the owner sees the margin consequence
    on the document it applies to, rather than having to go and ask.

    It reports and nothing more. The quotation's own figures are already
    calculated and are not re-derived, adjusted or re-totalled here - a
    supplier cost change does not reprice a quotation the customer was given.

    Best effort on purpose: an order must not fail because a margin panel
    could not be built, so any problem here is logged and dropped.
    """
    quote = (payload or {}).get("quote")
    if not quote or not quote.get("lines"):
        return None
    try:
        rows = _table.query(
            KeyConditionExpression=Key("PK").eq(cost_pk(DEFAULT_SHOP_ID))
            & Key("SK").begins_with("COST#")
        ).get("Items", [])
        confirmed = latest_confirmed_costs([
            {
                "skuId": row.get("skuId"),
                "confirmedCost": float(row.get("confirmedCost") or 0),
                "confirmedAt": row.get("confirmedAt"),
                "sourceJobId": row.get("sourceJobId"),
            }
            for row in rows
        ])
        if not confirmed:
            return None
        costs = {sku: entry["cost"] for sku, entry in confirmed.items()}
        impact = quotation_margin_impact(
            quote, margin_alerts(cached_dataset(), costs))
        return impact if impact["affectedCount"] else None
    except Exception as exc:  # noqa: BLE001
        print(f"margin protection skipped: {type(exc).__name__}: {exc}")
        return None


def _process_price_list(job_id: str, item: dict) -> dict:
    """Read one supplier price list and review it against the catalogue."""
    _update(job_id, status="PROCESSING", startedAt=int(time.time()))
    started = time.perf_counter()

    try:
        obj = boto3.client("s3").get_object(
            Bucket=UPLOADS_BUCKET, Key=item["imageKey"])
        image_bytes = obj["Body"].read()

        supplier, doc_date, items, usage = extract_price_list(
            image_bytes, item.get("imageContentType") or "image/png",
            model_id=MODEL_ID,
        )
        review = review_price_list(cached_dataset(), supplier, doc_date, items)
    except (ExtractionError, InvalidSupplierLineError) as exc:
        # A document we could not read is a real answer, not a crash.
        print(f"price list {job_id} rejected: {type(exc).__name__}: {exc}")
        _update(job_id, status="FAILED", error=str(exc)[:300])
        return {"ok": False}
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR price list {job_id}: {type(exc).__name__}: {exc}")
        _update(job_id, status="FAILED", error="price list processing failed")
        return {"ok": False}

    elapsed = (time.perf_counter() - started) * 1000
    payload = review.as_dict()

    # Metadata only. The document's contents are never logged.
    print(json.dumps({
        "event": "price_list_processed",
        "jobId": job_id,
        "jobType": JOB_PRICE_LIST,
        "status": "DONE",
        "modelId": MODEL_ID,
        "elapsedMs": round(elapsed, 1),
        "imageBytes": int(item.get("imageBytes", 0)),
        "itemCount": payload["lineCount"],
        "matchedCount": payload["matchedCount"],
        "ambiguousCount": payload["ambiguousCount"],
        "unmatchedCount": payload["unmatchedCount"],
        "materialChangeCount": payload["materialChangeCount"],
        "inputTokens": usage.get("inputTokens"),
        "outputTokens": usage.get("outputTokens"),
    }))

    _update(
        job_id,
        status="DONE",
        result=json.dumps({
            "status": "REVIEWED",
            "jobType": JOB_PRICE_LIST,
            "modelId": MODEL_ID,
            "elapsedMs": round(elapsed, 1),
            "review": payload,
        }),
        finishedAt=int(time.time()),
    )
    return {"ok": True, "status": "REVIEWED"}


def handler(event, context):
    job_id = event.get("jobId")
    if not job_id:
        print("ERROR: no jobId in event")
        return {"ok": False}

    item = _table.get_item(Key=_job_key(job_id)).get("Item")
    if not item:
        print(f"ERROR: job {job_id} not found")
        return {"ok": False}

    if item.get("jobType") == JOB_PRICE_LIST:
        return _process_price_list(job_id, item)

    order_text = item.get("orderText") or ""
    clarifications = item.get("clarifications") or []

    if clarifications:
        # Fold the owner's confirmed choices into the prompt so the agent does
        # not have to ask the same question twice.
        confirmed = "; ".join(
            f"'{c.get('requestedText')}' is confirmed as SKU {c.get('skuId')}"
            for c in clarifications
        )
        order_text = f"{order_text}\n\nThe shop owner has confirmed: {confirmed}."

    _update(job_id, status="PROCESSING", startedAt=int(time.time()))

    started = time.perf_counter()
    try:
        result = run_order_agent(cached_dataset(), order_text, model_id=MODEL_ID)
    except OrderTooLongError as exc:
        _update(job_id, status="FAILED", error=str(exc))
        return {"ok": False}
    except Exception as exc:  # noqa: BLE001
        # Log the type for CloudWatch; never echo model output into the record.
        print(f"ERROR agent failed for {job_id}: {type(exc).__name__}: {exc}")
        _update(job_id, status="FAILED", error="order processing failed")
        return {"ok": False}

    elapsed = (time.perf_counter() - started) * 1000
    print(json.dumps({
        "event": "order_processed",
        "jobId": job_id,
        "status": result.status,
        "turns": result.turns,
        "grounded": result.grounded,
        "elapsedMs": round(elapsed, 1),
        "modelId": result.modelId,
        "orderChars": len(order_text),
    }))

    payload = result.as_dict()
    protection = _margin_protection(payload)
    if protection:
        payload["marginProtection"] = protection

    # Credit, when this order goes on someone's khata. The quotation is
    # already priced and is neither withheld nor altered by the answer: a
    # customer over their limit still gets their quotation, and whether to
    # sell anyway is the owner's decision, not the software's.
    credit = check_quote_credit(
        cached_dataset(), item.get("customerId"), payload.get("quote"))
    if credit:
        payload["credit"] = credit

    _update(
        job_id,
        status="DONE" if result.status != "FAILED" else "FAILED",
        result=json.dumps(payload),
        finishedAt=int(time.time()),
    )
    return {"ok": True, "status": result.status}
