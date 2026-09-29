"""Order-processing worker.

Driven by Amazon SQS. Runs the bounded agent loop and writes the result back
onto the job record for the browser to poll.

DELIVERY IS AT LEAST ONCE
-------------------------
SQS can deliver the same message more than once - after a visibility timeout
expires, after a retry, or simply because the service chose to. So this worker
is written to be run twice on the same job without that being a problem.

`_claim` is the mechanism. It is a conditional DynamoDB update that moves a
job into PROCESSING only from QUEUED or PROCESSING. A job already at DONE or
FAILED fails the condition, and the message is acknowledged without the agent
being run again. That is what stops a duplicate from making a second Bedrock
call and writing a second, possibly different, quotation over the first.

RETRY IS FOR THINGS THAT MIGHT WORK NEXT TIME
---------------------------------------------
Two kinds of failure, treated differently, because treating them the same is
how a shop loses an order:

  * **Terminal** - the order is too long, the document cannot be read, the
    supplier line is invalid. Running it again produces the same answer. The
    job is marked FAILED and the message is acknowledged.

  * **Retryable** - Bedrock throttled us, a connection dropped, the service
    returned a 5xx. The job is LEFT as it is and the exception is re-raised,
    so SQS redelivers it. Marking these FAILED would turn a two-second blip
    into a lost order.

A retry waits a short, jittered interval rather than the full visibility
timeout: `_back_off` shortens the message's visibility to 30s and then 90s.
Bedrock throttling on this account is a per-minute quota, so a throttled order
is worth retrying a minute later, not six minutes later.

On the LAST delivery a transient failure is no longer handed back. The job is
marked FAILED with a message that says the shop is busy and the order should
be sent again, so a throttled order ends in an answer rather than sitting at
PROCESSING forever.

A message the worker cannot handle at all - a crash, or a Lambda timeout that
kills the invocation before any of this code runs - still goes to the
dead-letter queue with the job at PROCESSING. **Nothing here repairs that.**
The DLQ alarm is how a human finds out, and the jobId in the message is how
they find the job.
"""

from __future__ import annotations

import json
import os
import random
import time

import boto3
from boto3.dynamodb.conditions import Key

from agent.orchestrator import (
    FAILURE_NO_PRODUCT_NAMED,
    DEFAULT_MODEL_ID,
    OrderTooLongError,
    run_order_agent,
)
from agent.brief_summary import summarize_brief
from agent.negotiation_draft import draft_counter_offer
from agent.supplier_reply import extract_reply_terms
from engine import supplier_reply
from agent.decision_trace import build as build_decision_trace
from agent.textract_reader import (NOVA_PRO, TEXTRACT, TextractError,
                                   read_price_list)
from agent.vision import ExtractionError, extract_price_list
from engine.cost_records import DEFAULT_SHOP_ID, cost_pk, latest_confirmed_costs
from engine.credit import check_quote_credit
from engine.loader import cached_dataset
from engine.brief import brief_signature, build_brief
from engine.margin import margin_alerts, quotation_margin_impact
from engine.price_alerts import evaluate_price_change
from engine.supplier_prices import InvalidSupplierLineError, review_price_list
from engine import gst
from engine.quote import calculate_quote
from integrations import whatsapp
from integrations import whatsapp_inbound as wa_inbound
from observability import events, metrics

TABLE_NAME = os.environ["TABLE_NAME"]
MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", DEFAULT_MODEL_ID)
UPLOADS_BUCKET = os.environ.get("UPLOADS_BUCKET", "")
# The weekly restocking cash the alerts and the daily brief plan against.
BRIEF_BUDGET = float(os.environ.get("SHOPFLOW_BRIEF_BUDGET") or 25000)
# A supplier price move alerts once; the marker that says so expires after
# this, so the same move can alert again if it is still true a month later.
ALERT_DEDUP_SECONDS = 30 * 24 * 3600
# Set by a scheduled EventBridge rule. Nothing else sends it.
TASK_DAILY_BRIEF = "DAILY_BRIEF"

JOB_ORDER = "ORDER"
JOB_PRICE_LIST = "PRICE_LIST"
JOB_COUNTER_OFFER = "COUNTER_OFFER"
JOB_SUPPLIER_REPLY = "SUPPLIER_REPLY"
JOB_WHATSAPP = "WHATSAPP"

# Statuses. DONE is this codebase's completed state and has been since the
# first stage - the browser polls for it and the API returns it. It is named
# here rather than renamed to COMPLETED, because renaming it would break the
# API contract and every client of it for no gain.
STATUS_QUEUED = "QUEUED"
STATUS_PROCESSING = "PROCESSING"
STATUS_DONE = "DONE"
STATUS_FAILED = "FAILED"

# A job in one of these has already reached an answer. A duplicate delivery
# for one of them is acknowledged, not re-run.
TERMINAL_STATUSES = (STATUS_DONE, STATUS_FAILED)

# AWS error codes that mean "try again", as opposed to "this will never work".
# Anything not listed is treated as terminal, which is the safe default: a
# retryable failure wrongly treated as terminal loses one order and says so,
# while a terminal failure wrongly retried burns Bedrock spend three times and
# still fails.
RETRYABLE_ERROR_CODES = frozenset({
    "ThrottlingException", "Throttling", "TooManyRequestsException",
    "RequestThrottled", "RequestThrottledException",
    "ServiceUnavailable", "ServiceUnavailableException",
    "InternalServerError", "InternalServerException",
    "ModelTimeoutException", "ModelNotReadyException",
    "ProvisionedThroughputExceededException",
    "RequestTimeout", "RequestTimeoutException",
    "TransactionInProgressException",
})

# Bedrock's own "the model produced something unusable" errors. A live
# evaluation lost a Tamil-script order to one of these - "Model produced
# invalid sequence as part of ToolUse" - on its first attempt, and the same
# order answered correctly when it was simply sent again. The model call is
# not deterministic enough for one bad turn to be a verdict on the order, so
# these are retried like a throttle: through SQS, at most MAX_RECEIVES times
# in total, and then the job ends FAILED with an answer. They are NOT in
# RETRYABLE_ERROR_CODES because they are counted and worded separately.
MODEL_TRANSIENT_ERROR_CODES = frozenset({
    "ModelErrorException", "ModelStreamErrorException",
})

# Errors that say the REQUEST is wrong. Retrying cannot fix any of them, and
# they are listed so the classification below names them rather than letting
# them fall through to the default.
NON_RETRYABLE_ERROR_CODES = frozenset({
    "ValidationException", "AccessDeniedException",
    "ResourceNotFoundException", "UnrecognizedClientException",
    "ConditionalCheckFailedException",
})

# How a failure is classified, for logs and metrics. See `failure_class`.
TRANSIENT_PROVIDER = "TRANSIENT_PROVIDER"   # throttle, 5xx, network
TRANSIENT_MODEL = "TRANSIENT_MODEL"         # the model produced an unusable turn
NON_RETRYABLE_INPUT = "NON_RETRYABLE_INPUT" # the request itself is wrong
BUSINESS_VALIDATION = "BUSINESS_VALIDATION" # the order breaks a business rule
UNKNOWN_FAILURE = "UNKNOWN"                 # anything else: terminal, safely

# Exception CLASS names that mean the same thing, for the botocore errors that
# are not ClientError and so carry no response code.
RETRYABLE_EXCEPTION_NAMES = frozenset({
    "EndpointConnectionError", "ConnectionClosedError", "ConnectTimeoutError",
    "ReadTimeoutError", "ConnectionError", "HTTPClientError",
    "IncompleteReadError", "ResponseStreamingError",
})


# The subset of retryable codes that mean "you are asking too fast". Counted
# separately, because a throttle is a capacity fact - the account's Bedrock
# quota is requests per minute - and not a fault in the order or the code.
THROTTLE_ERROR_CODES = frozenset({
    "ThrottlingException", "Throttling", "TooManyRequestsException",
    "RequestThrottled", "RequestThrottledException",
    "ProvisionedThroughputExceededException",
})

# Must equal the queue's redrive `maxReceiveCount` (asserted against the
# synthesized template in tests/test_queue.py). On this delivery a transient
# failure is final: handing it back would only send it to the dead-letter
# queue with the job stuck at PROCESSING.
MAX_RECEIVES = 3

# Seconds before the next delivery, by attempt, plus up to 50% jitter so a
# burst of throttled orders does not come back in the same second and throttle
# again. Well inside the 360s visibility timeout it replaces.
RETRY_BACKOFF_SECONDS = (30, 90)

BUSY_MESSAGE = ("ShopFlow is busy and could not process this order after "
                f"{MAX_RECEIVES} attempts. Nothing was quoted. Please send the "
                "order again.")
MODEL_ERROR_MESSAGE = ("ShopFlow could not read this order after "
                       f"{MAX_RECEIVES} attempts. Nothing was quoted. Please "
                       "send it again, or write the product names as they "
                       "appear on the box.")


class RetryableFailure(RuntimeError):
    """Raised to hand a message back to SQS for another attempt."""


_table = boto3.resource("dynamodb").Table(TABLE_NAME)


def is_retryable(exc: BaseException) -> bool:
    """Whether another attempt could plausibly succeed.

    Read from the error itself rather than from where it was raised, so a
    throttle is recognised whether it came from Bedrock, DynamoDB or S3.
    """
    if isinstance(exc, RetryableFailure):
        return True
    if type(exc).__name__ in RETRYABLE_EXCEPTION_NAMES:
        return True
    if is_model_error(exc):
        return True
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = (response.get("Error") or {}).get("Code")
        if code in RETRYABLE_ERROR_CODES:
            return True
        if code in NON_RETRYABLE_ERROR_CODES:
            return False
        status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
        # 5xx is the service's problem; 4xx is ours and will not improve.
        if isinstance(status, int) and status >= 500:
            return True
    return False


def _error_code(exc: BaseException):
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        return (response.get("Error") or {}).get("Code")
    return None


def is_model_error(exc: BaseException) -> bool:
    """Bedrock reported that the model's own output was unusable."""
    return (_error_code(exc) in MODEL_TRANSIENT_ERROR_CODES
            or type(exc).__name__ in MODEL_TRANSIENT_ERROR_CODES)


def failure_class(exc: BaseException) -> str:
    """One word for what kind of failure this was. For logs and metrics only;
    `is_retryable` is what decides."""
    from agent.orchestrator import OrderTooLongError

    if isinstance(exc, OrderTooLongError):
        return BUSINESS_VALIDATION
    if is_model_error(exc):
        return TRANSIENT_MODEL
    if is_retryable(exc):
        return TRANSIENT_PROVIDER
    if _error_code(exc) in NON_RETRYABLE_ERROR_CODES:
        return NON_RETRYABLE_INPUT
    return UNKNOWN_FAILURE


def is_throttle(exc: BaseException) -> bool:
    """A retryable failure that means the request rate, not the request."""
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = (response.get("Error") or {}).get("Code")
        if code in THROTTLE_ERROR_CODES:
            return True
    return type(exc).__name__ in THROTTLE_ERROR_CODES


def _delivery(record: dict) -> dict:
    """What SQS said about this delivery: which attempt, and how to reach it."""
    attributes = record.get("attributes") or {}
    try:
        receives = int(attributes.get("ApproximateReceiveCount") or 1)
    except (TypeError, ValueError):
        receives = 1
    queue_url = None
    parts = str(record.get("eventSourceARN") or "").split(":")
    if len(parts) == 6 and parts[2] == "sqs":
        queue_url = f"https://sqs.{parts[3]}.amazonaws.com/{parts[4]}/{parts[5]}"
    return {"receives": receives, "queueUrl": queue_url,
            "receiptHandle": record.get("receiptHandle")}


def _back_off(delivery: dict) -> None:
    """Bring the next attempt forward from the 360s visibility timeout.

    Best effort. If it fails, SQS still redelivers after the full visibility
    timeout, which is exactly the behaviour before this existed.
    """
    if not delivery or not delivery.get("queueUrl") \
            or not delivery.get("receiptHandle"):
        return
    attempt = max(1, int(delivery.get("receives") or 1))
    base = RETRY_BACKOFF_SECONDS[min(attempt, len(RETRY_BACKOFF_SECONDS)) - 1]
    delay = int(base + random.uniform(0, base / 2))
    try:
        boto3.client("sqs").change_message_visibility(
            QueueUrl=delivery["queueUrl"],
            ReceiptHandle=delivery["receiptHandle"],
            VisibilityTimeout=delay)
    except Exception as exc:  # noqa: BLE001 - the default timeout still applies
        print(json.dumps({"event": "backoff_not_applied",
                          "error": type(exc).__name__}))


def _claim(job_id: str, item: dict) -> bool:
    """Take ownership of a job, or report that it is already finished.

    One conditional write does the whole job of idempotency. The condition
    allows QUEUED (the normal case) and PROCESSING (a genuine retry after a
    visibility timeout, where this worker is picking up its own abandoned
    work). It refuses DONE and FAILED, which is what makes a duplicate
    delivery a no-op instead of a second Bedrock call.

    Returns True if this invocation owns the job and should process it.
    """
    from botocore.exceptions import ClientError

    try:
        _table.update_item(
            Key=_job_key(job_id),
            UpdateExpression=("SET #status = :processing, startedAt = :now "
                              "ADD attempts :one"),
            ConditionExpression=(
                "attribute_exists(PK) AND #status IN (:queued, :processing)"),
            ExpressionAttributeNames={"#status": "status"},
            ExpressionAttributeValues={
                ":processing": STATUS_PROCESSING,
                ":queued": STATUS_QUEUED,
                ":now": int(time.time()),
                ":one": 1,
            },
        )
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") \
                == "ConditionalCheckFailedException":
            return False
        raise


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
        costs = _confirmed_cost_map()
        if not costs:
            return None
        impact = quotation_margin_impact(
            quote, margin_alerts(cached_dataset(), costs))
        return impact if impact["affectedCount"] else None
    except Exception as exc:  # noqa: BLE001
        print(f"margin protection skipped: {type(exc).__name__}: {exc}")
        return None


def _confirmed_cost_map() -> dict:
    """The shop's latest confirmed supplier cost per SKU. May raise."""
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
    return {sku: entry["cost"] for sku, entry in confirmed.items()}


def _first_alert(alert: dict, now: int) -> bool:
    """Record that this price move has alerted. False if it already had.

    One conditional write, so the same move read from the same price list
    twice - or redelivered by SQS - alerts once. If the marker cannot be
    written for any other reason the alert goes out: a duplicate email is a
    smaller failure than a missed price shock.
    """
    from botocore.exceptions import ClientError

    try:
        _table.put_item(
            Item={"PK": f"ALERT#{DEFAULT_SHOP_ID}", "SK": alert["alertId"],
                  "severity": alert["severity"], "skuId": alert["skuId"],
                  "createdAt": now, "expiresAt": now + ALERT_DEDUP_SECONDS},
            ConditionExpression="attribute_not_exists(PK)")
        return True
    except ClientError as exc:
        if exc.response.get("Error", {}).get("Code") == \
                "ConditionalCheckFailedException":
            return False
        print(json.dumps({"event": "price_alert_dedup_unavailable",
                          "alertId": alert["alertId"],
                          "error": f"{type(exc).__name__}"}))
        return True


def _publish_price_alerts(job_id: str, payload: dict) -> list:
    """Judge every supplier cost increase on a price list; publish the alerts.

    The comparison is the supplier price engine's; the judgement is
    engine.price_alerts', which reuses the walk-away price and the planner.
    Only a MATCHED line has a comparison, so an ambiguous or unknown row can
    never raise an alert - it waits for the owner to say which product it is.
    Best effort: a failure here is logged and counted and the price list is
    still DONE.
    """
    increases = events.unique([
        line for line in payload.get("lines") or []
        if (line.get("comparison") or {}).get("direction") == "INCREASE"
        # A price far outside what the shop last paid is more often a misread
        # than a rise. It waits for the owner; once confirmed, Intelligence
        # shows it with every other confirmed change.
        and (line.get("comparison") or {}).get("plausibility") != "EXTREME_CHANGE"])
    if not increases:
        return []
    try:
        confirmed = _confirmed_cost_map()
    except Exception as exc:  # noqa: BLE001
        print(f"price alerts: confirmed costs unavailable: {type(exc).__name__}")
        confirmed = {}
    data, now, sent = cached_dataset(), int(time.time()), []
    for line in increases:
        comparison = line["comparison"]
        try:
            alert = evaluate_price_change(
                data, line["skuId"], comparison["previousPrice"],
                comparison["currentPrice"],
                supplier=payload.get("supplierName") or "",
                confirmed_costs=confirmed, budget=BRIEF_BUDGET, now=now)
        except Exception as exc:  # noqa: BLE001
            print(json.dumps({"event": "price_alert_failed", "jobId": job_id,
                              "skuId": line.get("skuId"),
                              "error": f"{type(exc).__name__}: {str(exc)[:200]}"}))
            metrics.emit(metrics.ALERT_PUBLISH_FAILURES, job_id=job_id)
            continue
        if not alert["alert"]:
            continue
        if not _first_alert(alert, now):
            print(json.dumps({"event": "price_alert_duplicate", "jobId": job_id,
                              "alertId": alert["alertId"]}))
            continue
        events.publish(events.SUPPLIER_PRICE_CHANGED, job_id=job_id,
                       **events.supplier_price_alert(alert))
        metrics.emit(metrics.SUPPLIER_ALERTS, job_id=job_id,
                     dimensions={"Severity": alert["severity"]})
        print(json.dumps({"event": "price_alert", "jobId": job_id,
                          "alertId": alert["alertId"],
                          "severity": alert["severity"],
                          "triggers": alert["triggers"]}))
        sent.append(alert)
    return sent


def run_daily_brief(now: int | None = None, client=None) -> dict:
    """The scheduled daily brief: build it, maybe summarise it, store it.

    The structured brief is the engine's and is stored whatever happens to
    the summary. The summary is one Bedrock call, skipped when nothing needs
    attention, and kept only if every number in it is in the brief.
    """
    now = int(time.time()) if now is None else now
    brief = build_brief(cached_dataset(), _confirmed_cost_map(),
                        budget=BRIEF_BUDGET, now=now)
    summary = None
    if brief["hasAttention"]:
        from agent.orchestrator import _bedrock_client
        summary = summarize_brief(brief, client or _bedrock_client(), MODEL_ID)
    signature = brief_signature(brief)
    _table.put_item(Item={
        "PK": f"BRIEF#{DEFAULT_SHOP_ID}", "SK": "LATEST",
        "briefDate": brief["date"], "generatedAt": now,
        "signature": signature,
        "summary": json.dumps(summary) if summary else "",
        "counts": json.dumps(brief["counts"]),
    })
    events.publish(events.DAILY_SHOP_BRIEF_GENERATED,
                   **events.daily_shop_brief_generated(brief))
    metrics.emit(metrics.DAILY_BRIEFS, dimensions={
        "Outcome": "SUMMARIZED" if summary else "DETERMINISTIC"})
    print(json.dumps({"event": "daily_brief", "date": brief["date"],
                      "counts": brief["counts"], "summarized": bool(summary),
                      "grounded": brief["grounded"]}))
    return {"status": "DONE", "date": brief["date"], "signature": signature,
            "summarized": bool(summary), "counts": brief["counts"]}


def _read_document(job_id: str, image_bytes: bytes, content_type: str):
    """Read one supplier document, with Textract first and the model second.

    Textract is tried first because a dealer price list is a table and it
    returns one, with a per-word confidence score that decides whether a row
    needs a human before it can move a purchase cost. The model reader is the
    fallback for the documents Textract finds no table in - a photographed
    handwritten list, most obviously - and it remains exactly as it was.

    A fallback is not a repair. Whichever reader runs, its rows go to the same
    deterministic matcher and the same price comparison, and neither reader
    gets to decide what a row means. The one that ran is recorded on every
    row so an owner can see how their document was read.
    """
    try:
        rows, supplier, doc_date, excluded = read_price_list(
            image_bytes, bucket="", key="", with_excluded=True)
        metrics.emit(metrics.DOCUMENTS_EXTRACTED, job_id=job_id,
                     dimensions={"Reader": TEXTRACT}, rowCount=len(rows))
        return supplier, doc_date, rows, {}, TEXTRACT, excluded
    except Exception as exc:  # noqa: BLE001 - fall back, do not fail
        # Textract being unavailable, throttled or unable to find a table is
        # not the end of the document: it is a reason to try the other reader.
        # If that one fails too, its error is the one the owner sees, because
        # it is the one about the document rather than about a service.
        print(json.dumps({"event": "textract_fallback", "jobId": job_id,
                          "reason": f"{type(exc).__name__}: {str(exc)[:200]}"}))
        if not isinstance(exc, TextractError):
            metrics.emit(metrics.DOCUMENT_EXTRACTION_FAILURES, job_id=job_id,
                         dimensions={"Reader": TEXTRACT})

    supplier, doc_date, items, usage = extract_price_list(
        image_bytes, content_type, model_id=MODEL_ID)
    for row in items:
        if isinstance(row, dict):
            # The model reports no confidence, so none is claimed. `source`
            # is what lets the interface say which reader produced the row.
            row.setdefault("source", NOVA_PRO)
    metrics.emit(metrics.DOCUMENTS_EXTRACTED, job_id=job_id,
                 dimensions={"Reader": NOVA_PRO}, rowCount=len(items))
    # The model reader's unusable rows are excluded by the review itself.
    return supplier, doc_date, items, usage, NOVA_PRO, []


def _process_price_list(job_id: str, item: dict) -> dict:
    """Read one supplier price list and review it against the catalogue.

    `_claim` has already moved the job to PROCESSING.
    """
    started = time.perf_counter()

    try:
        obj = boto3.client("s3").get_object(
            Bucket=UPLOADS_BUCKET, Key=item["imageKey"])
        image_bytes = obj["Body"].read()

        supplier, doc_date, items, usage, reader, excluded = _read_document(
            job_id, image_bytes, item.get("imageContentType") or "image/png")
        review = review_price_list(cached_dataset(), supplier, doc_date, items,
                                   excluded_rows=excluded)
    except (ExtractionError, InvalidSupplierLineError) as exc:
        # A document we could not read is a real answer, not a crash - and a
        # terminal one: the same photograph will not read any better on a
        # second attempt.
        print(f"price list {job_id} rejected: {type(exc).__name__}: {exc}")
        _update(job_id, status=STATUS_FAILED, error=str(exc)[:300])
        metrics.emit(metrics.ORDERS_FAILED, job_id=job_id,
                     dimensions={"JobType": JOB_PRICE_LIST,
                                 "Outcome": "FAILED"})
        metrics.emit(metrics.DOCUMENT_EXTRACTION_FAILURES, job_id=job_id)
        # No pricing state changed. A document that could not be read leaves
        # every confirmed cost exactly where it was.
        events.publish(events.ORDER_PROCESSING_FAILED, job_id=job_id,
                       jobId=job_id, jobType=JOB_PRICE_LIST,
                       reason=type(exc).__name__)
        return {"ok": False}
    except Exception as exc:  # noqa: BLE001
        # Everything else is classified by the caller, so a throttled Bedrock
        # call or an S3 blip becomes a retry rather than a lost document.
        print(f"price list {job_id} failed: {type(exc).__name__}: {exc}")
        raise

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
        "conflictCount": payload["conflictCount"],
        "excludedCount": payload["excludedCount"],
        "materialChangeCount": payload["materialChangeCount"],
        "reviewRequiredCount": payload["reviewRequiredCount"],
        "reader": reader,
        "inputTokens": usage.get("inputTokens"),
        "outputTokens": usage.get("outputTokens"),
    }))

    metrics.emit(metrics.WORKER_PROCESSING_SECONDS, elapsed / 1000.0,
                 job_id=job_id, dimensions={"JobType": JOB_PRICE_LIST})
    metrics.emit(metrics.ORDERS_COMPLETED, job_id=job_id,
                 dimensions={"JobType": JOB_PRICE_LIST,
                             "Outcome": "REVIEWED"})

    # A supplier cost increase worth an alert is published with the alert's
    # figures (engine.price_alerts), once per price move. A material DECREASE
    # is still a business fact and still an event, with the comparison's own
    # numbers as before.
    _publish_price_alerts(job_id, payload)
    for detail in events.unique([
        events.supplier_price_changed(line["comparison"])
        for line in payload["lines"]
        if line.get("comparison") and line["comparison"].get("materialChange")
        and line["comparison"].get("direction") == "DECREASE"
    ]):
        events.publish(events.SUPPLIER_PRICE_CHANGED, job_id=job_id, **detail)
    _update(
        job_id,
        status=STATUS_DONE,
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


def _process_counter_offer(job_id: str, item: dict, client=None) -> dict:
    """Word one supplier counter-offer. Never retried, never sent.

    The terms were written onto the job by the API from the engines; they are
    worded here, checked by `engine.negotiation.validate_draft`, and replaced
    by the fixed template if the model fails or strays. A model failure is
    therefore not a reason to redeliver: the template is a correct answer.
    """
    started = time.perf_counter()
    try:
        terms = json.loads(item.get("terms") or "null")
    except (TypeError, ValueError):
        terms = None
    if not isinstance(terms, dict) or not terms.get("skuId"):
        print(json.dumps({"event": "counter_offer_invalid_job", "jobId": job_id}))
        _update(job_id, status=STATUS_FAILED,
                error="counter-offer draft failed", finishedAt=int(time.time()))
        return {"ok": False, "reason": "terminal"}

    if client is None:
        try:
            from agent.orchestrator import _bedrock_client
            client = _bedrock_client()
        except Exception as exc:  # noqa: BLE001 - the template stands
            print(json.dumps({"event": "counter_offer_client_unavailable",
                              "jobId": job_id, "error": type(exc).__name__}))
    outcome = draft_counter_offer(cached_dataset(), terms, client, MODEL_ID)
    elapsed = round((time.perf_counter() - started) * 1000, 1)
    print(json.dumps({"event": "counter_offer_drafted", "jobId": job_id,
                      "skuId": terms["skuId"], "source": outcome["source"],
                      "fallbackReason": outcome["fallbackReason"],
                      "valid": outcome["validation"]["valid"],
                      "elapsedMs": elapsed}))
    _update(job_id, status=STATUS_DONE, finishedAt=int(time.time()),
            result=json.dumps({
                "status": "DRAFTED",
                "jobType": JOB_COUNTER_OFFER,
                "terms": terms,
                "draft": outcome["draft"],
                "draftSource": outcome["source"],
                "fallbackReason": outcome["fallbackReason"],
                "validation": outcome["validation"],
                "modelProblems": outcome["problems"],
                "ownerApprovalRequired": True,
                "sent": False,
                "stateChanged": False,
                "elapsedMs": elapsed,
            }))
    return {"ok": True, "status": "DRAFTED"}


def _process_supplier_reply(job_id: str, item: dict, client=None) -> dict:
    """Read one supplier reply and compare it with the shop's limits.

    The model only extracts; `engine.supplier_reply` checks the extraction
    against the reply's own text and evaluates the offer. A model failure is
    a question for the owner, not a retry. Nothing is decided or sent.
    """
    started = time.perf_counter()
    try:
        context = json.loads(item.get("context") or "null")
    except (TypeError, ValueError):
        context = None
    reply = item.get("replyText") or ""
    if not isinstance(context, dict) or not context.get("available") or not reply:
        print(json.dumps({"event": "supplier_reply_invalid_job", "jobId": job_id}))
        _update(job_id, status=STATUS_FAILED,
                error="supplier reply reading failed", finishedAt=int(time.time()))
        return {"ok": False, "reason": "terminal"}

    if client is None:
        try:
            from agent.orchestrator import _bedrock_client
            client = _bedrock_client()
        except Exception as exc:  # noqa: BLE001 - becomes a question
            print(json.dumps({"event": "supplier_reply_client_unavailable",
                              "jobId": job_id, "error": type(exc).__name__}))
    read = extract_reply_terms(reply, context, client, MODEL_ID)
    flagged = supplier_reply.instruction_like(reply)
    result = {"jobType": JOB_SUPPLIER_REPLY, "context": context,
              "extracted": read["extracted"], "readError": read["error"],
              "instructionLikeText": flagged, "ownerDecisionRequired": True,
              "stateChanged": False, "sent": False}
    if not read["ok"]:
        check = {"status": supplier_reply.AMBIGUOUS, "problems": ["NOT_READ"],
                 "terms": None}
    else:
        check = supplier_reply.check_extraction(reply, read["extracted"], context)
    result["check"] = check
    if check["status"] == "OK":
        result["status"] = "EVALUATED"
        result["evaluation"] = supplier_reply.evaluate_offer(context, check["terms"])
    else:
        result["status"] = ("NEEDS_CLARIFICATION"
                            if check["status"] == supplier_reply.AMBIGUOUS
                            else "REJECTED_TERMS")
        result["question"] = supplier_reply.question(check)
    result["elapsedMs"] = round((time.perf_counter() - started) * 1000, 1)
    print(json.dumps({"event": "supplier_reply_evaluated", "jobId": job_id,
                      "skuId": context.get("skuId"), "status": result["status"],
                      "priceStatus": (result.get("evaluation") or {}).get("status"),
                      "problems": check["problems"][:8],
                      "instructionLikeText": flagged,
                      "elapsedMs": result["elapsedMs"]}))
    _update(job_id, status=STATUS_DONE, finishedAt=int(time.time()),
            result=json.dumps(result))
    return {"ok": True, "status": result["status"]}


def handler(event, context):
    """The SQS entry point.

    Batch size is 1 (see the CDK stack), so there is one record per
    invocation and raising is the correct way to ask for a retry - there is no
    other record in the batch to be re-run as a side effect.

    A direct `{"jobId": ...}` event is also accepted. Nothing in the deployed
    stack sends one: the API's role no longer carries `lambda:InvokeFunction`
    on this function, so that shape is reachable only by an operator
    re-driving a job by hand, or by a test. It is a maintenance door, not a
    second production path.
    """
    if isinstance(event, dict) and event.get("shopflowTask") == TASK_DAILY_BRIEF:
        # The scheduled rule's constant input. Not an SQS message and not a
        # job: nothing to claim, nothing to retry.
        try:
            return run_daily_brief()
        except Exception as exc:  # noqa: BLE001 - logged, visible, not retried
            print(json.dumps({"event": "daily_brief_failed",
                              "error": f"{type(exc).__name__}: {str(exc)[:200]}"}))
            metrics.emit(metrics.WORKER_FAILURES,
                         dimensions={"JobType": "UNKNOWN", "Outcome": "FAILED"})
            return {"status": "FAILED"}

    records = event.get("Records") if isinstance(event, dict) else None
    if records is None:
        return _process_message(event or {})

    results = []
    for record in records:
        try:
            message = json.loads(record.get("body") or "{}")
        except (ValueError, TypeError):
            # An unparseable body will never parse. Retrying it three times
            # and dead-lettering it teaches nobody anything, so it is logged
            # and acknowledged.
            print(f"ERROR: message body is not JSON, discarding: "
                  f"{str(record.get('messageId'))[:64]}")
            metrics.emit(metrics.WORKER_FAILURES,
                         dimensions={"JobType": "UNKNOWN",
                                     "Outcome": "TERMINAL"})
            results.append({"ok": False, "reason": "malformed"})
            continue
        results.append(_process_message(message, _delivery(record)))
    return {"ok": all(r.get("ok") for r in results), "results": results}


def _process_message(message: dict, delivery: dict | None = None) -> dict:
    job_id = message.get("jobId") if isinstance(message, dict) else None
    if not job_id or not isinstance(job_id, str):
        print("ERROR: no jobId in message")
        metrics.emit(metrics.WORKER_FAILURES,
                     dimensions={"JobType": "UNKNOWN", "Outcome": "TERMINAL"})
        return {"ok": False, "reason": "no-job-id"}

    item = _table.get_item(Key=_job_key(job_id)).get("Item")
    if not item:
        # Either the job expired under its TTL, or the API's compensating
        # delete ran after a failed send and this message is its ghost.
        # Neither improves on a retry.
        print(f"ERROR: job {job_id} not found")
        metrics.emit(metrics.WORKER_FAILURES, job_id=job_id,
                     dimensions={"JobType": "UNKNOWN", "Outcome": "TERMINAL"})
        return {"ok": False, "reason": "unknown-job"}

    job_type = str(item.get("jobType") or JOB_ORDER)

    if item.get("status") in TERMINAL_STATUSES or not _claim(job_id, item):
        # A duplicate delivery of work that already has an answer. This is the
        # normal, expected consequence of at-least-once delivery, and the only
        # correct response to it is to do nothing.
        print(json.dumps({
            "event": "duplicate_delivery_ignored",
            "jobId": job_id,
            "jobType": job_type,
            "status": str(item.get("status")),
        }))
        metrics.emit(metrics.DUPLICATE_DELIVERIES, job_id=job_id,
                     dimensions={"JobType": job_type, "Outcome": "DUPLICATE"})
        return {"ok": True, "duplicate": True, "status": item.get("status")}

    try:
        if job_type == JOB_PRICE_LIST:
            return _process_price_list(job_id, item)
        if job_type == JOB_COUNTER_OFFER:
            return _process_counter_offer(job_id, item)
        if job_type == JOB_SUPPLIER_REPLY:
            return _process_supplier_reply(job_id, item)
        if job_type == JOB_WHATSAPP:
            return _process_whatsapp(job_id, item)
        return _process_order(job_id, item)
    except Exception as exc:  # noqa: BLE001
        receives = (delivery or {}).get("receives") or 1
        kind = failure_class(exc)
        if kind == TRANSIENT_MODEL:
            metrics.emit(metrics.MODEL_ERRORS, job_id=job_id,
                         dimensions={"JobType": job_type,
                                     "Outcome": ("RETRYABLE"
                                                 if receives < MAX_RECEIVES
                                                 else "TERMINAL")})
        if is_retryable(exc) and receives < MAX_RECEIVES:
            # Left exactly as it is - still PROCESSING, still recoverable.
            # Re-raised so SQS redelivers it, sooner than the visibility
            # timeout would. `_claim` on the next delivery accepts a job at
            # PROCESSING, so the retry re-runs the agent once; a job that has
            # meanwhile reached DONE or FAILED is never run again.
            throttled = is_throttle(exc)
            print(json.dumps({
                "event": "retryable_failure",
                "jobId": job_id,
                "jobType": job_type,
                "error": type(exc).__name__,
                "failureClass": kind,
                "throttled": throttled,
                "attempt": receives,
            }))
            metrics.emit(metrics.WORKER_FAILURES, job_id=job_id,
                         dimensions={"JobType": job_type,
                                     "Outcome": ("THROTTLED" if throttled
                                                 else "MODEL_ERROR"
                                                 if kind == TRANSIENT_MODEL
                                                 else "RETRYABLE")})
            _back_off(delivery)
            raise
        if is_retryable(exc):
            # The last delivery, and still transient. Handing it back would
            # dead-letter it with the job stuck at PROCESSING, so the job ends
            # here with an answer the owner can act on: send it again.
            print(json.dumps({
                "event": "retries_exhausted",
                "jobId": job_id,
                "jobType": job_type,
                "error": type(exc).__name__,
                "failureClass": kind,
                "throttled": is_throttle(exc),
                "attempt": receives,
            }))
            _update(job_id, status=STATUS_FAILED,
                    error=(MODEL_ERROR_MESSAGE if kind == TRANSIENT_MODEL
                           else BUSY_MESSAGE),
                    finishedAt=int(time.time()))
            metrics.emit(metrics.WORKER_FAILURES, job_id=job_id,
                         dimensions={"JobType": job_type, "Outcome": "TERMINAL"})
            metrics.emit(metrics.ORDERS_FAILED, job_id=job_id,
                         dimensions={"JobType": job_type, "Outcome": "FAILED"})
            events.publish(events.ORDER_PROCESSING_FAILED, job_id=job_id,
                           jobId=job_id, jobType=job_type,
                           reason="retries_exhausted")
            _whatsapp_failure_reply(job_id, job_type, busy=True)
            return {"ok": False, "reason": "retries-exhausted"}
        print(f"ERROR terminal failure for {job_id} ({kind}): "
              f"{type(exc).__name__}: {exc}")
        # Worded for the job that actually failed. This message is returned by
        # GET /api/jobs/{id} and shown to the owner, so a price list failing
        # here must not tell them their order failed.
        _update(job_id, status=STATUS_FAILED,
                error=("price list processing failed"
                       if job_type == JOB_PRICE_LIST
                       else "counter-offer draft failed"
                       if job_type == JOB_COUNTER_OFFER
                       else "supplier reply reading failed"
                       if job_type == JOB_SUPPLIER_REPLY
                       else "order processing failed"))
        _whatsapp_failure_reply(job_id, job_type, busy=False)
        metrics.emit(metrics.WORKER_FAILURES, job_id=job_id,
                     dimensions={"JobType": job_type, "Outcome": "TERMINAL"})
        metrics.emit(metrics.ORDERS_FAILED, job_id=job_id,
                     dimensions={"JobType": job_type, "Outcome": "FAILED"})
        return {"ok": False, "reason": "terminal"}


# ---------------------------------------------------------------------------
# WhatsApp: a customer message, through the same order path
# ---------------------------------------------------------------------------
# The webhook recorded the message and queued its job id; this is where it is
# read. An order goes through `_process_order` - the same agent, the same
# quantity guard, the same engines, the same stored result as a website
# order - and the customer is sent the interpretation. A YES re-prices those
# lines through `engine.quote.calculate_quote` and `engine.gst.quote_gst` and
# sends the quotation. Nothing here computes a figure.
#
# The reply is sent once. A send that fails is recorded on the job for the
# owner and not retried, because a retry after a timeout could deliver the
# same message twice and a customer cannot un-read a duplicate quotation. An
# SQS redelivery of a finished job is refused by `_claim` before this runs, so
# a duplicate delivery sends nothing.

def _whatsapp_conversation_key(item: dict) -> dict:
    return {"PK": f"WACONV#{item.get('senderKey')}", "SK": "META"}


def _whatsapp_conversation(item: dict, now: int) -> dict:
    row = _table.get_item(Key=_whatsapp_conversation_key(item)).get("Item") or {}
    if int(row.get("expiresAt") or 0) <= now:
        # TTL removes rows lazily; an expired conversation is no conversation.
        return {}
    return row


def _save_whatsapp_conversation(item: dict, now: int, **state) -> None:
    _table.put_item(Item={
        **_whatsapp_conversation_key(item),
        "updatedAt": now,
        "lastMessageId": item.get("externalMessageId"),
        "expiresAt": now + wa_inbound.CONVERSATION_SECONDS,
        **state,
    })


def _whatsapp_send(item: dict, text: str) -> dict:
    """Send one reply. Never raises; the outcome is returned for the record."""
    try:
        sent = whatsapp.send_text("+" + str(item.get("waSender") or ""), text)
        return {"sent": True, "reason": "OK", "messageId": sent.get("messageId"),
                "text": text}
    except whatsapp.WhatsAppError as exc:
        return {"sent": False, "reason": exc.reason, "text": text}
    except Exception as exc:  # noqa: BLE001 - a reply may never fail the job
        print(json.dumps({"event": "whatsapp_reply_error",
                          "error": type(exc).__name__}))
        return {"sent": False, "reason": whatsapp.API_ERROR, "text": text}


def _reprice(pending: dict) -> dict:
    """The confirmed order, priced again by the engines. Raises if it cannot be.

    The lines are the ones the customer confirmed - SKU, quantity and the unit
    they used - taken from the earlier engine result, never from the model.
    GST uses the place of supply the earlier quotation read from the
    customer's own words.
    """
    stored = json.loads(pending.get("result") or "{}")
    quote = stored.get("quote") or {}
    items = []
    for line in quote.get("lines") or []:
        item = {"skuId": line["skuId"], "quantity": line["quantity"]}
        if line.get("requestedUom"):
            item["uom"] = line["requestedUom"]
        items.append(item)
    if not items:
        raise ValueError("no confirmed lines")
    data = cached_dataset()
    fresh = calculate_quote(data, items).as_dict()
    earlier_tax = quote.get("gst") or {}
    try:
        fresh["gst"] = gst.quote_gst(
            data, fresh, earlier_tax.get("taxMode"),
            tax_mode_source=earlier_tax.get("taxModeSource") or "default",
            display=earlier_tax.get("display"))
    except (gst.GstConfigError, gst.GstInputError):
        fresh["gst"] = {"available": False, "reason": "GST_CONFIGURATION_ERROR",
                        "notice": gst.NOT_TAX_ADVICE}
    return fresh


def _whatsapp_order(job_id: str, item: dict, text: str,
                    clarifications: list, now: int) -> tuple:
    """Run the message through the existing order path; say what came back."""
    _process_order(job_id, {**item, "orderText": text,
                            "clarifications": clarifications,
                            "customerId": "", "language": "en"})
    if text != item.get("orderText"):
        # What was actually read, when a choice rewrote the customer's line.
        _update(job_id, waOrderRead=text[:wa_inbound.MAX_TEXT_CHARS])
    row = _table.get_item(Key=_job_key(job_id)).get("Item") or {}
    payload = json.loads(row.get("result") or "{}")
    status = payload.get("status")
    if status == "QUOTED" and payload.get("quote"):
        _save_whatsapp_conversation(item, now, status="AWAITING_CONFIRMATION",
                                    pendingJobId=job_id)
        return "AWAITING_CONFIRMATION", wa_inbound.interpretation(payload["quote"])
    if status == "NEEDS_CLARIFICATION":
        question, options = wa_inbound.clarification(payload.get("clarification"))
        if options:
            _save_whatsapp_conversation(
                item, now, status="AWAITING_CHOICE", options=options,
                originalText=text,
                requestedText=str((payload.get("clarification") or {})
                                  .get("requestedText") or "")[:200],
                clarifications=clarifications)
        else:
            _save_whatsapp_conversation(item, now, status="IDLE")
        return "NEEDS_CLARIFICATION", question
    # No quotation and no question: a greeting, a message naming nothing the
    # shop sells, or an order the agent could not finish. The customer is
    # asked for product names either way; the owner's row keeps the detail.
    _save_whatsapp_conversation(item, now, status="IDLE")
    return "NOT_UNDERSTOOD", wa_inbound.NO_PRODUCT


def _process_whatsapp(job_id: str, item: dict) -> dict:
    now = int(time.time())
    text = str(item.get("orderText") or "")
    conversation = _whatsapp_conversation(item, now)
    state = conversation.get("status")
    kind, choice = wa_inbound.intent(text)
    result = None

    if not item.get("supported"):
        kind, outcome, reply = "UNSUPPORTED", "UNSUPPORTED", wa_inbound.UNSUPPORTED
    elif item.get("tooLong"):
        kind, outcome, reply = "TOO_LONG", "TOO_LONG", wa_inbound.TOO_LONG
    elif kind == wa_inbound.CONFIRM:
        if state == "AWAITING_CONFIRMATION":
            pending = _table.get_item(
                Key=_job_key(str(conversation.get("pendingJobId")))).get("Item")
            try:
                quote = _reprice(pending or {})
                result = {"status": "CONFIRMED_QUOTE", "quote": quote,
                          "confirmsJobId": conversation.get("pendingJobId")}
                outcome, reply = "QUOTATION_SENT", wa_inbound.quotation(quote)
            except Exception as exc:  # noqa: BLE001 - the engines refused
                print(json.dumps({"event": "whatsapp_reprice_failed",
                                  "jobId": job_id, "error": type(exc).__name__}))
                outcome, reply = "REPRICE_FAILED", wa_inbound.REPRICE_FAILED
            _save_whatsapp_conversation(item, now, status="IDLE")
        else:
            outcome, reply = "NOTHING_PENDING", wa_inbound.NOTHING_PENDING
    elif kind == wa_inbound.DECLINE:
        outcome, reply = "CANCELLED", wa_inbound.CANCELLED
        _save_whatsapp_conversation(item, now, status="IDLE")
    elif kind == wa_inbound.AMBIGUOUS_REPLY and state == "AWAITING_CONFIRMATION":
        outcome, reply = "ASKED_YES_NO", wa_inbound.ASK_YES_NO
    elif kind == wa_inbound.CHOICE and state == "AWAITING_CHOICE" and \
            1 <= (choice or 0) <= len(conversation.get("options") or []):
        option = conversation["options"][choice - 1]
        original = str(conversation.get("originalText") or "")
        # The chosen product's catalogue name, written into the customer's
        # own line, and the whole order read again. Passing the choice as a
        # "confirmed SKU" note instead lets the model skip searching that
        # line, and the coverage guard then - correctly - refuses a
        # quotation for a product nobody looked up.
        rewritten = wa_inbound.apply_choice(
            original, conversation.get("requestedText") or "", option["name"])
        if rewritten:
            outcome, reply = _whatsapp_order(
                job_id, item, rewritten,
                list(conversation.get("clarifications") or []), now)
        else:
            clarifications = list(conversation.get("clarifications") or []) + [{
                "requestedText": conversation.get("requestedText") or "",
                "skuId": option["skuId"]}]
            outcome, reply = _whatsapp_order(job_id, item, original,
                                             clarifications, now)
    elif kind == wa_inbound.OWNER_REQUEST:
        outcome, reply = "OWNER_ONLY", wa_inbound.OWNER_ONLY
    else:
        kind = wa_inbound.ORDER
        outcome, reply = _whatsapp_order(job_id, item, text, [], now)

    sent = _whatsapp_send(item, reply)
    fields = {"waIntent": kind, "waOutcome": outcome,
              "waReply": json.dumps(sent), "finishedAt": int(time.time())}
    if kind not in (wa_inbound.ORDER, wa_inbound.CHOICE):
        # An order's row was already finished by `_process_order`.
        fields["status"] = STATUS_DONE
        if result is not None:
            fields["result"] = json.dumps(result)
    _update(job_id, **fields)
    print(json.dumps({"event": "whatsapp_message_processed", "jobId": job_id,
                      "intent": kind, "outcome": outcome,
                      "replySent": sent["sent"], "replyReason": sent["reason"]}))
    return {"ok": True, "status": outcome, "replySent": sent["sent"]}


def _whatsapp_failure_reply(job_id: str, job_type: str, busy: bool) -> None:
    """After a WhatsApp job fails for good, tell the customer. Never raises."""
    if job_type != JOB_WHATSAPP:
        return
    try:
        item = _table.get_item(Key=_job_key(job_id)).get("Item") or {}
        sent = _whatsapp_send(item, wa_inbound.BUSY if busy else wa_inbound.NOT_READ)
        _update(job_id, waReply=json.dumps(sent), waOutcome="FAILED")
    except Exception as exc:  # noqa: BLE001
        print(json.dumps({"event": "whatsapp_failure_reply_error",
                          "error": type(exc).__name__}))


def _publish_order_events(job_id: str, payload: dict, status: str,
                          failure_kind: str | None = None) -> None:
    """Announce what the engines decided about one order. Never raises.

    Three things are worth an event here and nothing else is. A shortage is a
    fact about stock the owner may need to act on; a margin that fell past the
    threshold is a fact about money; an order that failed inside the agent is
    the one outcome nobody is watching for.

    A clarification is emitted too, but it is deliberately NOT routed to an
    alert: asking which colour the customer meant is the system working, and
    an owner who is emailed about it learns to ignore the emails.
    """
    quote = payload.get("quote") or {}

    for detail in events.unique([
        events.stockout_detected(line)
        for line in quote.get("lines") or []
        if (line.get("shortageQty") or 0) > 0
    ]):
        events.publish(events.STOCKOUT_DETECTED, job_id=job_id, **detail)

    protection = payload.get("marginProtection") or {}
    for detail in events.unique([
        events.low_margin_detected(alert)
        for alert in protection.get("affected") or []
    ]):
        events.publish(events.LOW_MARGIN_DETECTED, job_id=job_id, **detail)

    if status == "NEEDS_CLARIFICATION":
        clarification = payload.get("clarification") or {}
        events.publish(events.ORDER_NEEDS_CLARIFICATION, job_id=job_id,
                       jobId=job_id,
                       attribute=clarification.get("clarifyingAttribute") or None)
    elif status == "FAILED" and failure_kind != FAILURE_NO_PRODUCT_NAMED:
        # A message that named nothing the shop sells - a greeting, a
        # question, a prompt injection - was answered, not lost. Announcing it
        # as a processing failure would page the owner about correct
        # behaviour, which is how alerts come to be ignored.
        events.publish(events.ORDER_PROCESSING_FAILED, job_id=job_id,
                       jobId=job_id, jobType=JOB_ORDER, reason="agent")


def _process_order(job_id: str, item: dict) -> dict:
    order_text = item.get("orderText") or ""
    # Choices the API resolved from options ShopFlow offered. The agent looks
    # these lines up itself; the sentence below only tells the model so.
    choices = list(item.get("confirmed") or [])
    clarifications = (item.get("clarifications") or []) + choices

    if clarifications:
        # Fold the owner's confirmed choices into the prompt so the agent does
        # not have to ask the same question twice.
        confirmed = "; ".join(
            f"'{c.get('requestedText')}' is confirmed as SKU {c.get('skuId')}"
            for c in clarifications
        )
        order_text = f"{order_text}\n\nThe shop owner has confirmed: {confirmed}."

    # `_claim` has already moved this job to PROCESSING and recorded the
    # attempt, so there is no status write here.
    started = time.perf_counter()
    try:
        # The language the owner typed in, carried from the job row. A hint to
        # the model about the sentence, not an input to any business tool.
        # Quantities are held to what the customer wrote, not to the
        # confirmation sentence appended above: that sentence repeats the
        # customer's words, count included, and would read as a second line.
        result = run_order_agent(cached_dataset(), order_text,
                                 model_id=MODEL_ID,
                                 language=str(item.get("language") or "en"),
                                 customer_text=item.get("orderText") or "",
                                 confirmed=choices)
    except OrderTooLongError as exc:
        # Terminal by definition: the text will be the same length next time.
        _update(job_id, status=STATUS_FAILED, error=str(exc))
        metrics.emit(metrics.ORDERS_FAILED, job_id=job_id,
                     dimensions={"JobType": JOB_ORDER, "Outcome": "FAILED"})
        return {"ok": False}
    except Exception as exc:  # noqa: BLE001
        # Classification happens in `_process_message`, which is the one place
        # that decides whether SQS should see this again. Re-raised so the job
        # is not marked FAILED on a throttle.
        print(f"agent failed for {job_id}: {type(exc).__name__}: {exc}")
        raise

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

    # How this was worked out, built from the engines' own output and stored
    # beside the result so it can be shown without re-running anything.
    #
    # This is the customer-safe view: matches, stock, shortages and the
    # quotation. Supplier cost, margin and the purchase plan are the owner's
    # and are added by the API behind the owner gate - `customer_steps`
    # cannot produce them at all. Nothing here reads model prose, so no
    # reasoning can arrive through it. If building it raises, the trace says
    # it is unavailable and the quotation is untouched.
    # The quantity verdict is passed beside the result rather than inside it:
    # it is what lets the trace say "the customer wrote 2, the model proposed
    # 4" instead of "4 requested".
    payload["decisionTrace"] = build_decision_trace(
        payload, quantity_check=getattr(result, "quantityCheck", None))

    status = STATUS_DONE if result.status != "FAILED" else STATUS_FAILED
    _update(
        job_id,
        status=status,
        result=json.dumps(payload),
        finishedAt=int(time.time()),
    )

    # Business events, AFTER the job row is written.
    #
    # The order is deliberate: the business result is durable before anything
    # is announced, so a bus that is down, throttled or not configured cannot
    # leave a customer without the quotation that was already calculated.
    # Every number below is copied out of engine output - none is derived
    # here and none came from the model.
    _publish_order_events(job_id, payload, result.status,
                          getattr(result, "failureKind", None))
    # Counts and a duration. No total, no SKU, no customer - see the note at
    # the top of observability/metrics.py for why that line is drawn hard.
    metrics.emit(metrics.WORKER_PROCESSING_SECONDS, elapsed / 1000.0,
                 job_id=job_id, dimensions={"JobType": JOB_ORDER})
    if ((payload.get("quote") or {}).get("gst") or {}).get("available"):
        # A count of quotations that carried a GST block. Never the tax.
        metrics.emit(metrics.GST_CALCULATIONS, job_id=job_id,
                     dimensions={"JobType": JOB_ORDER, "Outcome": "QUOTED"})
    # ShopFlowOrdersFailed is what the agent-failure alarm watches, so only a
    # genuine agent failure may reach it. A clarification is a completed
    # order. A FAILED result for a message that named no product is recorded
    # under its own outcome: the job still reads FAILED to the customer, but
    # nobody is paged because a customer said hello.
    if status == STATUS_DONE:
        metric, outcome = metrics.ORDERS_COMPLETED, result.status
    elif getattr(result, "failureKind", None) == FAILURE_NO_PRODUCT_NAMED:
        metric, outcome = metrics.ORDERS_COMPLETED, "NO_PRODUCT"
    else:
        metric, outcome = metrics.ORDERS_FAILED, result.status
    metrics.emit(metric, job_id=job_id,
                 dimensions={"JobType": JOB_ORDER, "Outcome": outcome})
    return {"ok": True, "status": result.status}
