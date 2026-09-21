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

A message that exhausts its retries goes to the dead-letter queue with the job
still at PROCESSING. **Nothing here repairs it.** The DLQ alarm is how a human
finds out, and the jobId in the message is how they find the job. There is no
automatic recovery in this phase and this module does not pretend otherwise.
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
from observability import metrics

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


def _process_price_list(job_id: str, item: dict) -> dict:
    """Read one supplier price list and review it against the catalogue.

    `_claim` has already moved the job to PROCESSING.
    """
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
        # A document we could not read is a real answer, not a crash - and a
        # terminal one: the same photograph will not read any better on a
        # second attempt.
        print(f"price list {job_id} rejected: {type(exc).__name__}: {exc}")
        _update(job_id, status=STATUS_FAILED, error=str(exc)[:300])
        metrics.emit(metrics.ORDERS_FAILED, job_id=job_id,
                     dimensions={"JobType": JOB_PRICE_LIST,
                                 "Outcome": "FAILED"})
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
        "inputTokens": usage.get("inputTokens"),
        "outputTokens": usage.get("outputTokens"),
    }))

    metrics.emit(metrics.WORKER_PROCESSING_SECONDS, elapsed / 1000.0,
                 job_id=job_id, dimensions={"JobType": JOB_PRICE_LIST})
    metrics.emit(metrics.ORDERS_COMPLETED, job_id=job_id,
                 dimensions={"JobType": JOB_PRICE_LIST,
                             "Outcome": "REVIEWED"})
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
        results.append(_process_message(message))
    return {"ok": all(r.get("ok") for r in results), "results": results}


def _process_message(message: dict) -> dict:
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
        if is_retryable(exc):
            # Left exactly as it is - still PROCESSING, still recoverable.
            # Re-raised so SQS redelivers it.
            print(json.dumps({
                "event": "retryable_failure",
                "jobId": job_id,
                "jobType": job_type,
                "error": type(exc).__name__,
            }))
            metrics.emit(metrics.WORKER_FAILURES, job_id=job_id,
                         dimensions={"JobType": job_type,
                                     "Outcome": "RETRYABLE"})
            raise
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
        result = run_order_agent(cached_dataset(), order_text,
                                 model_id=MODEL_ID,
                                 language=str(item.get("language") or "en"))
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

    status = STATUS_DONE if result.status != "FAILED" else STATUS_FAILED
    _update(
        job_id,
        status=status,
        result=json.dumps(payload),
        finishedAt=int(time.time()),
    )
    # Counts and a duration. No total, no SKU, no customer - see the note at
    # the top of observability/metrics.py for why that line is drawn hard.
    metrics.emit(metrics.WORKER_PROCESSING_SECONDS, elapsed / 1000.0,
                 job_id=job_id, dimensions={"JobType": JOB_ORDER})
    metrics.emit(
        metrics.ORDERS_COMPLETED if status == STATUS_DONE
        else metrics.ORDERS_FAILED,
        job_id=job_id,
        dimensions={"JobType": JOB_ORDER, "Outcome": result.status},
    )
    return {"ok": True, "status": result.status}
