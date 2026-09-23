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
from agent.decision_trace import build as build_decision_trace
from agent.textract_reader import (NOVA_PRO, TEXTRACT, TextractError,
                                   read_price_list)
from agent.vision import ExtractionError, extract_price_list
from engine.cost_records import DEFAULT_SHOP_ID, cost_pk, latest_confirmed_costs
from engine.credit import check_quote_credit
from engine.loader import cached_dataset
from engine.margin import margin_alerts, quotation_margin_impact
from engine.supplier_prices import InvalidSupplierLineError, review_price_list
from observability import events, metrics

TABLE_NAME = os.environ["TABLE_NAME"]
MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", DEFAULT_MODEL_ID)
UPLOADS_BUCKET = os.environ.get("UPLOADS_BUCKET", "")

JOB_ORDER = "ORDER"
JOB_PRICE_LIST = "PRICE_LIST"

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
    response = getattr(exc, "response", None)
    if isinstance(response, dict):
        code = (response.get("Error") or {}).get("Code")
        if code in RETRYABLE_ERROR_CODES:
            return True
        status = (response.get("ResponseMetadata") or {}).get("HTTPStatusCode")
        # 5xx is the service's problem; 4xx is ours and will not improve.
        if isinstance(status, int) and status >= 500:
            return True
    return False


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
        rows, supplier, doc_date = read_price_list(
            image_bytes, bucket="", key="")
        metrics.emit(metrics.DOCUMENTS_EXTRACTED, job_id=job_id,
                     dimensions={"Reader": TEXTRACT}, rowCount=len(rows))
        return supplier, doc_date, rows, {}, TEXTRACT
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
    return supplier, doc_date, items, usage, NOVA_PRO


def _process_price_list(job_id: str, item: dict) -> dict:
    """Read one supplier price list and review it against the catalogue.

    `_claim` has already moved the job to PROCESSING.
    """
    started = time.perf_counter()

    try:
        obj = boto3.client("s3").get_object(
            Bucket=UPLOADS_BUCKET, Key=item["imageKey"])
        image_bytes = obj["Body"].read()

        supplier, doc_date, items, usage, reader = _read_document(
            job_id, image_bytes, item.get("imageContentType") or "image/png")
        review = review_price_list(cached_dataset(), supplier, doc_date, items)
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

    # A material supplier price move is a business fact, so it is an event.
    # The numbers are the comparison's own - see engine.supplier_prices - and
    # one SKU produces one event however many rows named it.
    for detail in events.unique([
        events.supplier_price_changed(line["comparison"])
        for line in payload["lines"]
        if line.get("comparison") and line["comparison"].get("materialChange")
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
        return _process_order(job_id, item)
    except Exception as exc:  # noqa: BLE001
        receives = (delivery or {}).get("receives") or 1
        if is_retryable(exc) and receives < MAX_RECEIVES:
            # Left exactly as it is - still PROCESSING, still recoverable.
            # Re-raised so SQS redelivers it, sooner than the visibility
            # timeout would.
            throttled = is_throttle(exc)
            print(json.dumps({
                "event": "retryable_failure",
                "jobId": job_id,
                "jobType": job_type,
                "error": type(exc).__name__,
                "throttled": throttled,
                "attempt": receives,
            }))
            metrics.emit(metrics.WORKER_FAILURES, job_id=job_id,
                         dimensions={"JobType": job_type,
                                     "Outcome": ("THROTTLED" if throttled
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
                "throttled": is_throttle(exc),
                "attempt": receives,
            }))
            _update(job_id, status=STATUS_FAILED, error=BUSY_MESSAGE,
                    finishedAt=int(time.time()))
            metrics.emit(metrics.WORKER_FAILURES, job_id=job_id,
                         dimensions={"JobType": job_type, "Outcome": "TERMINAL"})
            metrics.emit(metrics.ORDERS_FAILED, job_id=job_id,
                         dimensions={"JobType": job_type, "Outcome": "FAILED"})
            events.publish(events.ORDER_PROCESSING_FAILED, job_id=job_id,
                           jobId=job_id, jobType=job_type,
                           reason="retries_exhausted")
            return {"ok": False, "reason": "retries-exhausted"}
        print(f"ERROR terminal failure for {job_id}: "
              f"{type(exc).__name__}: {exc}")
        # Worded for the job that actually failed. This message is returned by
        # GET /api/jobs/{id} and shown to the owner, so a price list failing
        # here must not tell them their order failed.
        _update(job_id, status=STATUS_FAILED,
                error=("price list processing failed"
                       if job_type == JOB_PRICE_LIST
                       else "order processing failed"))
        metrics.emit(metrics.WORKER_FAILURES, job_id=job_id,
                     dimensions={"JobType": job_type, "Outcome": "TERMINAL"})
        metrics.emit(metrics.ORDERS_FAILED, job_id=job_id,
                     dimensions={"JobType": job_type, "Outcome": "FAILED"})
        return {"ok": False, "reason": "terminal"}


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
    clarifications = item.get("clarifications") or []

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
                                 customer_text=item.get("orderText") or "")
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
