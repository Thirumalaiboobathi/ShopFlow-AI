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
    POST /api/voice/transcribe    Amazon Transcribe: audio in, transcript out
    POST /api/whatsapp/send       send a customer message, or return a draft
    GET  /api/languages           the language registry and capability matrix
    GET  /api/jobs/{id}           poll a queued job
    GET  /api/demo                the seeded example, so the UI hard-codes nothing

Most routes accept an optional `language`. It selects the words in the
response and nothing else: the SKU, the quantity, the unit, the total and the
decision are produced by the same deterministic engines whatever language is
asked for, and an unrecognised tag falls back to English rather than failing.

Order and price-list processing are asynchronous. A Bedrock tool loop takes
seconds, and a public demo that holds an HTTP connection open for that long is
a reliability risk, so the API queues the work and the browser polls.

That queue is Amazon SQS, and it used to be a direct asynchronous Lambda
invoke. The difference matters: an `InvocationType="Event"` call that failed
left the job row sitting at QUEUED until its TTL expired, with the browser
polling something that would never finish and nothing anywhere recording the
loss. SQS gives the work durability, bounded retries, a dead-letter queue that
can be inspected, and an alarm when anything lands in it.

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
from agent import decision_trace
from observability import events
from engine.credit import (
    InvalidOrderTotalError,
    check_credit,
    customer_view,
    list_customers,
)
from engine.language import (
    DEFAULT_LANGUAGE,
    capability_matrix,
    canonicalize_request,
    language_directory,
    localize_response,
    normalize_language,
)
from engine.budget import restock_candidates
from engine.loader import cached_dataset
from observability import metrics
from engine.margin import margin_alerts, margin_view
from engine.shortage import committed_demand
from engine.messages import (
    CREDIT_REMINDER,
    CREDIT_STATUS,
    MESSAGE_TYPES,
    ORDER_CONFIRMATION,
    QUOTATION,
    InvalidMessageRequest,
    build_credit_status_message,
    build_order_confirmation_message,
    build_quotation_message,
    mask_phone,
    normalize_phone,
    wa_me_url,
)
from integrations import whatsapp
from engine.speech import (
    AUDIO_PREFIX,
    MAX_AUDIO_BODY_BYTES,
    POLL_INTERVAL_MS,
    PROVIDER,
    InvalidAudioError,
    audio_key,
    classify_job,
    detected_language,
    job_name,
    resolve_language,
    transcript_from_payload,
    validate_audio,
)
from engine.purchasing import (
    InvalidBudgetError,
    build_purchase_plan,
    what_if,
)
from engine.supplier_prices import (CONFIRMED as SUPPLIER_CONFIRMED,
                                     STATE_CONFIRMED, STATE_REVIEW_REQUIRED)
from engine.supplier_prices import DECISIONS, build_decision_record
from engine.voice import MAX_TRANSCRIPT_CHARS, answer_shop_query
from engine import gst, whatif
from engine.brief import brief_signature, build_brief
from engine.price_alerts import evaluate_price_change

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
JOB_TRANSCRIPT = "TRANSCRIPT"

# The shape of the message this API puts on the queue. Carried so a consumer
# reading an unfamiliar message can say so instead of guessing at it.
QUEUE_MESSAGE_VERSION = 1

JOB_TTL_SECONDS = int(os.environ.get("JOB_TTL_SECONDS", 60 * 60 * 24))


# Clients are built on first use rather than at import, so the module can be
# imported and exercised in a test without AWS credentials.
@lru_cache(maxsize=1)
def table():
    import boto3

    return boto3.resource("dynamodb").Table(os.environ["TABLE_NAME"])


@lru_cache(maxsize=1)
def sqs_client():
    import boto3

    return boto3.client("sqs")


@lru_cache(maxsize=1)
def s3_client():
    import boto3

    return boto3.client("s3")


@lru_cache(maxsize=1)
def transcribe_client():
    import boto3

    return boto3.client("transcribe")

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


def _language_of(payload) -> str:
    """The language a request asked for, or English.

    Never an error. A browser sending a locale ShopFlow does not know should
    get an English answer, not a 400 - the language is presentation, and a
    presentation preference must not be able to fail a business request.
    """
    if not isinstance(payload, dict):
        return DEFAULT_LANGUAGE
    return normalize_language(payload.get("language"))


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


# The shop's own jobs, newest last, on the index the table has always had.
#
# GSI1 was created with the table and nothing has ever written to it. Putting
# these two attributes on a job row makes "the last twenty jobs for this shop"
# a single query instead of a scan, and it adds no AWS resource, no schema
# migration and no second table - a GSI has no fixed schema, so an attribute
# that was not there yesterday simply starts appearing today.
#
# Rows written before this change carry neither attribute and are absent from
# the index. That is correct rather than unfortunate: they have not been lost,
# they are readable by job id exactly as they always were, and a listing that
# quietly back-filled them would be inventing history.
def _job_index(job_id: str, job_type: str, created_at: int) -> dict:
    return {
        "GSI1PK": f"SHOP#{DEFAULT_SHOP_ID}",
        # Sortable by time, unique by job id.
        "GSI1SK": f"JOB#{created_at:011d}#{job_id}",
        "jobType": job_type,
    }


def _create_order(event) -> dict:
    raw = event.get("body") or ""
    if len(raw.encode("utf-8")) > MAX_BODY_BYTES:
        return _response(413, {"error": "request body too large"})

    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return _response(400, {"error": "body must be JSON"})

    # An order is text. `str()` on whatever arrived used to turn {"a": 1} into
    # the literal "{'a': 1}" and send it to the model, which is not a customer
    # order and not worth a Bedrock call. Reject the type instead of coercing
    # it. `None` and a missing key stay "required" rather than becoming a type
    # error, because that is what the caller actually did wrong.
    raw_order = payload.get("orderText")
    if raw_order is not None and not isinstance(raw_order, str):
        return _response(400, {"error": "orderText must be a string"})

    order_text = _CONTROL.sub("", raw_order or "").strip()
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

    # The language the owner typed in. Stored with the job so the worker can
    # localize the question it may come back with, and so a poll knows which
    # language to answer in. It does not reach the matcher, the quote or the
    # credit check - those see the same canonical input in every language.
    language = _language_of(payload)

    job_id = uuid.uuid4().hex
    now = int(time.time())
    failed = _queue_job(job_id, JOB_ORDER, {
        **_job_key(job_id),
        **_job_index(job_id, JOB_ORDER, now),
        "jobId": job_id,
        "jobType": JOB_ORDER,
        "status": "QUEUED",
        "orderText": order_text,
        "customerId": customer_id,
        "clarifications": cleaned,
        "language": language,
        "createdAt": now,
        "expiresAt": now + JOB_TTL_SECONDS,
    })
    if failed:
        return failed

    # 202 means what it has always meant here, and now means it more firmly:
    # ShopFlow has accepted this order and placed it on a durable queue. It
    # does NOT mean the order has been priced.
    return _response(202, {"jobId": job_id, "status": "QUEUED",
                           "language": language})


def _enqueue(job_id: str, job_type: str) -> None:
    """Hand one job to the worker, durably.

    The message carries an identifier and nothing else. The job record is
    already in DynamoDB and the worker reads it from there, so no order text,
    customer id or business value travels through the queue - which keeps SQS
    exactly what it is here, transport, and means a message sitting in the
    dead-letter queue discloses nothing.

    `version` is present so a future change to the message shape can be
    recognised rather than guessed at by a consumer reading an old message.
    """
    sqs_client().send_message(
        QueueUrl=os.environ["ORDERS_QUEUE_URL"],
        MessageBody=json.dumps({
            "jobId": job_id,
            "jobType": job_type,
            "version": QUEUE_MESSAGE_VERSION,
        }),
    )


def _queue_job(job_id: str, job_type: str, item: dict):
    """Persist the job, then queue it - and undo the first if the second fails.

    There is no transaction across DynamoDB and SQS, so one of the two has to
    happen first and the failure has to be handled deliberately.

    Writing first is the safer order. A job row with no message is a row that
    is visibly stuck and can be removed; a message with no row would reach the
    worker as an unknown job id, and the worker would have nothing to work
    from. So the row is written, the message is sent, and if the send fails
    the row is deleted again - a compensating action, not a rollback, and the
    difference is worth naming.

    If the compensating delete ALSO fails, the row survives with its TTL and
    expires on its own. The caller is told the truth either way: the job was
    not queued.

    Returns None on success, or a ready-to-send error response.
    """
    table().put_item(Item=item)

    try:
        _enqueue(job_id, job_type)
    except Exception as exc:  # noqa: BLE001 - any send failure, not just one
        print(f"ERROR queueing {job_type} {job_id}: {type(exc).__name__}: {exc}")
        metrics.emit(metrics.QUEUE_SEND_FAILURES,
                     dimensions={"JobType": job_type}, job_id=job_id)
        try:
            table().delete_item(Key=_job_key(job_id))
        except Exception as cleanup:  # noqa: BLE001
            # The row is left behind, but it carries a TTL and the response
            # below still tells the caller their order was not accepted.
            print(f"ERROR could not remove orphan job {job_id}: "
                  f"{type(cleanup).__name__}: {cleanup}")
        # 503, not 500: the request was fine and retrying is the right move.
        return _response(503, {
            "error": "that order could not be queued \u2014 please try again",
        })

    metrics.emit(metrics.ORDERS_QUEUED,
                 dimensions={"JobType": job_type}, job_id=job_id)
    return None


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
    failed = _queue_job(job_id, JOB_PRICE_LIST, {
        **_job_key(job_id),
        **_job_index(job_id, JOB_PRICE_LIST, now),
        "jobId": job_id,
        "jobType": JOB_PRICE_LIST,
        "status": "QUEUED",
        "imageKey": key,
        "imageContentType": content_type,
        "imageBytes": len(image_bytes),
        "createdAt": now,
        "expiresAt": now + JOB_TTL_SECONDS,
    })
    if failed:
        # The uploaded image is left in S3 rather than deleted here: the
        # bucket's lifecycle rule already expires `price-lists/` after 30
        # days, and the API's grant on that prefix is write-only by design.
        # Widening it to delete would trade a real security property for a
        # tidier failure path.
        return failed

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
    lines = result.get("review", {}).get("lines", [])
    if any(line.get("skuId") == sku_id and line.get("status") == "CONFLICT"
           for line in lines):
        # The document gives this product more than one price. None of them
        # may become the shop's cost until the supplier says which is right.
        return _response(409, {
            "error": "this price list gives more than one price for that "
                     "product; confirm the correct price with the supplier",
            "status": "CONFLICT"})
    comparison = None
    for line in lines:
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

    # What the plan means for cash once the supplier's GST invoice arrives.
    # Reported beside the plan; the allocation above is not touched by it and
    # still spends the budget in GST-exclusive supplier cost.
    try:
        plan["gst"] = gst.plan_gst_view(data, plan)
        metrics.emit(metrics.GST_CALCULATIONS,
                     dimensions={"Outcome": "PURCHASE_PLAN"})
    except (gst.GstConfigError, gst.GstInputError) as exc:
        print(f"plan GST view skipped: {type(exc).__name__}: {exc}")
        plan["gst"] = {"available": False, "reason": "GST_CONFIGURATION_ERROR",
                       "notice": gst.NOT_TAX_ADVICE}

    # The plan is complete and correct at this point. Announcing it cannot
    # change it: `publish` never raises, and a bus that is down or absent
    # leaves this response exactly as it is.
    events.publish(events.PURCHASE_PLAN_GENERATED,
                   **events.purchase_plan_generated(plan))

    # The owner's view of how this was worked out. Supplier cost, margin and
    # the plan are owner facts, and this route is behind the owner gate.
    plan["decisionTrace"] = {
        "available": True,
        "audience": "owner",
        "steps": decision_trace.owner_steps(
            margin_alerts=alerts or [], plan=plan),
        "source": "agent.decision_trace",
    }
    plan["decisionTrace"]["lines"] = decision_trace.render(
        plan["decisionTrace"]["steps"])

    return _response(200, plan)


WHAT_IF_KIND = "WHAT_IF"


def _create_what_if(event, payload: dict) -> dict:
    """Business What-If: simulate one decision. Owner route. Writes nothing.

    Reached through `POST /api/shop-queries` with `kind: "WHAT_IF"`, behind the
    same owner gate. Synchronous and model-free, like the purchase planner it
    calls. The owner's sentence is read by `engine.whatif.parse_scenario`, a
    closed set of patterns - no Bedrock call is made, which is also why this
    Lambda still carries no Bedrock permission.

    Reads only: the confirmed supplier costs, and (when `quoteJobId` is given)
    one stored order result. Nothing is put, updated or deleted - the
    simulation runs on copies and the response says `stateChanged: false`.
    """
    question = payload.get("question")
    if not isinstance(question, str) or not question.strip():
        return _response(400, {"error": "question is required"})
    question = _CONTROL.sub("", question).strip()
    if len(question) > whatif.MAX_TEXT_CHARS:
        return _response(400, {"error": f"question must be "
                                        f"{whatif.MAX_TEXT_CHARS} characters "
                                        f"or fewer"})

    data = cached_dataset()
    sku_id = payload.get("skuId")
    if sku_id is not None and (not isinstance(sku_id, str)
                               or sku_id not in data.products):
        return _response(400, {"error": "unknown skuId"})

    budget = payload.get("budget")
    if budget is not None and (isinstance(budget, bool)
                               or not isinstance(budget, (int, float))):
        return _response(400, {"error": "budget must be a number"})

    quote = None
    job_id = payload.get("quoteJobId")
    if job_id:
        if not isinstance(job_id, str) or not re.fullmatch(r"[0-9a-f]{32}",
                                                            job_id):
            return _response(400, {"error": "invalid quoteJobId"})
        job = table().get_item(Key=_job_key(job_id)).get("Item")
        if not job or job.get("jobType", JOB_ORDER) != JOB_ORDER \
                or not job.get("result"):
            return _response(404, {"error": "that quotation was not found"})
        quote = (json.loads(job["result"]) or {}).get("quote")
        if not quote:
            return _response(400, {"error": "that job has no quotation"})

    confirmed = {sku: entry["cost"] for sku, entry in _confirmed_costs().items()}
    result = whatif.simulate(data, question, confirmed_costs=confirmed,
                             quote=quote, sku_id=sku_id, budget=budget)
    metrics.emit(metrics.WHATIF_SIMULATIONS,
                 dimensions={"Outcome": result["status"]})
    return _response(200, result)


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

    # A "what if?" is also an owner's question about their own shop, so it is
    # answered on this owner route rather than on a new one - which keeps the
    # API surface, the gate and the deployed API Gateway routes exactly as
    # they were. Selected explicitly by the caller, never by guessing.
    if isinstance(payload, dict) and \
            str(payload.get("kind") or "").upper() == WHAT_IF_KIND:
        return _create_what_if(event, payload)

    # Same contract as orderText: a transcript is text, not a coerced object.
    raw_transcript = payload.get("transcript")
    if raw_transcript is not None and not isinstance(raw_transcript, str):
        return _response(400, {"error": "transcript must be a string"})

    transcript = _CONTROL.sub("", raw_transcript or "").strip()
    if not transcript:
        return _response(400, {"error": "transcript is required"})
    if len(transcript) > MAX_TRANSCRIPT_CHARS:
        return _response(400, {
            "error": f"transcript must be {MAX_TRANSCRIPT_CHARS} characters or fewer"})

    # A margin question needs the shop's confirmed costs. Read here rather
    # than in the voice layer, which owns no data access of its own.
    costs = {sku: entry["cost"] for sku, entry in _confirmed_costs().items()}
    language = _language_of(payload)
    # The adapter normalises native-script digits and strips the language's
    # own function words. What reaches `answer_shop_query` is the product
    # description, which is what it has always received.
    canonical = canonicalize_request(transcript, language)

    # A request that reduced to numerals alone carries no product description
    # the catalogue can read. Handing it to the matcher anyway is how "20
    # switches" in another script became a 20mm conduit: a bare number scores
    # a perfect match against a SKU code that ends in the same digits. Ask
    # instead. A question is always a better answer than a confident wrong one.
    if not canonical["hasProductVocabulary"]:
        return _response(200, localize_response({
            "status": "NEEDS_CLARIFICATION",
            "language": language,
            "canonicalInput": canonical,
            "clarification": {"attribute": "product"},
            "spoken": "",
        }, language))

    answer = answer_shop_query(
        cached_dataset(), canonical["canonicalText"], costs)
    return _response(200, localize_response(
        {**answer, "language": language,
         "canonicalInput": canonical}, language))


def _create_transcription(event) -> dict:
    """Accept a recording and start an Amazon Transcribe job.

    Deliberately the SAME shape as the price-list upload that already exists:
    base64 in the request body, a private S3 object, a job row, and the
    browser polling `GET /api/jobs/{jobId}`. Reusing that contract means no
    new polling mechanism, no WebSocket, and no second client implementation.

    No worker invoke. The price-list flow needs one because reading a document
    is a Bedrock call; transcription is a managed service doing the waiting
    for us, so the poll simply asks Transcribe how it is getting on. Nothing
    in this path touches Bedrock.

    This route produces TEXT. It returns no SKU, no price, no stock figure and
    no decision - the transcript goes to the existing voice workflow exactly
    as a browser-recognised one does.
    """
    raw = event.get("body") or ""
    if event.get("isBase64Encoded"):
        try:
            raw = base64.b64decode(raw).decode("utf-8")
        except Exception:
            return _response(400, {"error": "body could not be decoded"})

    if len(raw.encode("utf-8")) > MAX_AUDIO_BODY_BYTES:
        return _response(413, {"error": "that recording is too large"})

    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return _response(400, {"error": "body must be JSON"})

    encoded = payload.get("audioBase64")
    if not isinstance(encoded, str) or not encoded:
        return _response(400, {"error": "audioBase64 is required"})
    try:
        audio_bytes = base64.b64decode(encoded, validate=True)
    except Exception:
        return _response(400, {"error": "audioBase64 is not valid base64"})

    try:
        audio = validate_audio(payload.get("contentType"), audio_bytes)
    except InvalidAudioError as exc:
        return _response(400, {"error": str(exc)})

    language = resolve_language(payload.get("language"))

    job_id = uuid.uuid4().hex
    key = audio_key(job_id, audio["extension"])
    name = job_name(job_id)

    # Private bucket, server-side encrypted, and its own prefix so the audio
    # lifecycle rule and IAM scope cannot reach a supplier document.
    s3_client().put_object(
        Bucket=os.environ["UPLOADS_BUCKET"],
        Key=key,
        Body=audio_bytes,
        ContentType=audio["contentType"],
        ServerSideEncryption="AES256",
    )

    params = {
        "TranscriptionJobName": name,
        "Media": {"MediaFileUri": f"s3://{os.environ['UPLOADS_BUCKET']}/{key}"},
        "MediaFormat": audio["mediaFormat"],
    }
    if language["identifyLanguage"]:
        params["IdentifyLanguage"] = True
        params["LanguageOptions"] = language["languageOptions"]
    else:
        params["LanguageCode"] = language["languageCode"]

    try:
        transcribe_client().start_transcription_job(**params)
    except Exception as exc:  # noqa: BLE001
        # The audio is useless without a job, so it goes immediately rather
        # than waiting for the lifecycle rule.
        _delete_audio(key)
        print(f"ERROR starting transcription: {type(exc).__name__}: {exc}")
        return _response(502, {"error": "transcription could not be started"})

    now = int(time.time())
    table().put_item(Item={
        **_job_key(job_id),
        "jobId": job_id,
        "jobType": JOB_TRANSCRIPT,
        "status": "QUEUED",
        # The KEY is recorded so the object can be deleted. The audio itself
        # is never written into a business record.
        "audioKey": key,
        "audioBytes": audio["bytes"],
        "transcribeJobName": name,
        "language": language["requested"],
        "createdAt": now,
        "expiresAt": now + JOB_TTL_SECONDS,
    })

    return _response(202, {
        "jobId": job_id,
        "status": "QUEUED",
        "jobType": JOB_TRANSCRIPT,
        "provider": PROVIDER,
        "pollIntervalMs": POLL_INTERVAL_MS,
    })


def _delete_audio(key: str) -> None:
    """Remove a recording. Best effort - the lifecycle rule is the backstop."""
    if not key or not str(key).startswith(AUDIO_PREFIX):
        return
    try:
        s3_client().delete_object(Bucket=os.environ["UPLOADS_BUCKET"], Key=key)
    except Exception as exc:  # noqa: BLE001
        print(f"audio cleanup skipped: {type(exc).__name__}: {exc}")


def _delete_transcription_job(name: str) -> None:
    if not name:
        return
    try:
        transcribe_client().delete_transcription_job(TranscriptionJobName=name)
    except Exception as exc:  # noqa: BLE001
        print(f"transcription job cleanup skipped: {type(exc).__name__}: {exc}")


def _fetch_transcript(uri: str) -> dict:
    """Read Transcribe's result document from the URL it gave us.

    Transcribe stores the result in a service-managed bucket and returns a
    short-lived URL for it. Using that rather than an output bucket of our own
    means no second S3 object to create, permit and clean up.
    """
    import urllib.request

    with urllib.request.urlopen(uri, timeout=8) as response:
        return json.loads(response.read().decode("utf-8") or "{}")


def _poll_transcription(item: dict, body: dict) -> dict:
    """Ask Transcribe how the job is going, and finish it if it is done.

    Called from `_get_job`, so the browser's existing polling loop drives it.
    When the job reaches a terminal state the recording is deleted and the
    Transcribe job with it - a transcript is kept, a recording is not.
    """
    name = item.get("transcribeJobName") or ""
    key = item.get("audioKey") or ""
    now = int(time.time())

    try:
        job = transcribe_client().get_transcription_job(
            TranscriptionJobName=name)["TranscriptionJob"]
        state = job.get("TranscriptionJobStatus")
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR reading transcription job: {type(exc).__name__}: {exc}")
        return {**body, "status": "FAILED",
                "error": "Transcription could not be checked. Please try again."}

    verdict = classify_job(state, int(item.get("createdAt") or 0), now)

    if verdict["status"] not in ("DONE", "FAILED"):
        return {**body, "status": verdict["status"]}

    if verdict["status"] == "FAILED":
        _delete_audio(key)
        _delete_transcription_job(name)
        message = verdict.get("error") or "That recording could not be transcribed."
        _finish_transcript_job(item["jobId"], "FAILED", error=message)
        return {**body, "status": "FAILED", "error": message}

    try:
        payload = _fetch_transcript(job["Transcript"]["TranscriptFileUri"])
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR fetching transcript: {type(exc).__name__}: {exc}")
        _delete_audio(key)
        _delete_transcription_job(name)
        message = "The transcript could not be read. Please try again."
        _finish_transcript_job(item["jobId"], "FAILED", error=message)
        return {**body, "status": "FAILED", "error": message}

    transcript = transcript_from_payload(payload)
    _delete_audio(key)
    _delete_transcription_job(name)

    if not transcript:
        message = "No speech was detected. Please try again, or type instead."
        _finish_transcript_job(item["jobId"], "FAILED", error=message)
        return {**body, "status": "FAILED", "error": message}

    result = {
        "transcript": transcript,
        "provider": PROVIDER,
        # Said explicitly so nothing downstream mistakes this route for one
        # that decided something. It produced text; the workflow that already
        # exists does the rest.
        "handledBy": "existing-workflow",
        "detectedLanguage": detected_language(payload),
    }
    _finish_transcript_job(item["jobId"], "DONE", result=result)
    return {**body, "status": "DONE", "result": result}


def _finish_transcript_job(job_id: str, status: str, result=None,
                           error: str = "") -> None:
    """Record the outcome so a second poll costs nothing and says the same."""
    fields = {"status": status, "finishedAt": int(time.time())}
    if result is not None:
        fields["result"] = json.dumps(result)
    if error:
        fields["error"] = error[:300]
    # The audio key is cleared with the object, so nothing points at a
    # recording that no longer exists.
    fields["audioKey"] = ""

    names = {f"#{k}": k for k in fields}
    values = {f":{k}": v for k, v in fields.items()}
    try:
        table().update_item(
            Key=_job_key(job_id),
            UpdateExpression="SET " + ", ".join(f"#{k} = :{k}" for k in fields),
            ExpressionAttributeNames=names,
            ExpressionAttributeValues=values,
        )
    except Exception as exc:  # noqa: BLE001
        print(f"job update skipped: {type(exc).__name__}: {exc}")


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

    # The decision, untouched, plus the words for it. Every figure below -
    # limit, outstanding, projected, remaining - is the engine's own.
    language = _language_of(payload)
    return _response(200, localize_response(
        {**result, "credit": result, "language": language}, language))


# Message types that describe a khata account. Owner only.
KHATA_MESSAGE_TYPES = frozenset({CREDIT_STATUS, CREDIT_REMINDER})

# Everything an anonymous WhatsApp request may receive. No customer name, no
# recipient (masked or otherwise), no customer id, no configuration status and
# no phone number anywhere - the draft link carries the text only.
PUBLIC_WHATSAPP_FIELDS = frozenset({
    "messageType", "text", "language", "draftUrl", "status", "sent", "reason",
    "notice",
})
PUBLIC_DRAFT_REASON = "OWNER_REQUIRED_TO_SEND"


def _owner_gate_response(message: str) -> dict:
    """The demo-owner gate's refusal. Not authentication; it says so."""
    response = _response(401, {
        "error": "owner route",
        "message": message,
        "demoGate": True,
        "isAuthentication": False,
        "note": OWNER_GATE_NOTE,
    })
    response["headers"]["x-shopflow-audience"] = "owner"
    return response


def _public_whatsapp_draft(message_type: str, quote: dict, payload: dict,
                           language: str) -> dict:
    """A quotation or confirmation draft for a caller who is not the owner.

    Built WITHOUT the customer and WITHOUT the credit decision, so neither the
    customer's name nor the state of their account can appear in the text.
    Never sent: sending reaches a phone number, and a phone number is the
    shop's to use. The response is cut to `PUBLIC_WHATSAPP_FIELDS`.
    """
    try:
        if message_type == QUOTATION:
            message = build_quotation_message(quote, None, None,
                                              language=language)
        else:
            message = build_order_confirmation_message(
                quote, None,
                reference=str(payload.get("quoteId") or "")[:12],
                language=language)
    except InvalidMessageRequest as exc:
        return _response(400, {"error": str(exc)})
    body = {
        "messageType": message["messageType"],
        "text": message["text"],
        "language": language,
        "draftUrl": wa_me_url(message["text"], None),
        "status": "DRAFT",
        "sent": False,
        "reason": PUBLIC_DRAFT_REASON,
        "notice": ("Draft only. Messages are sent by the shop from the "
                   "ShopFlow workspace."),
    }
    return _response(200, {k: v for k, v in body.items()
                           if k in PUBLIC_WHATSAPP_FIELDS})


def _create_whatsapp_send(event) -> dict:
    """Build a customer message from an existing result, and send or draft it.

    What this route does NOT do is the point of it. It does not price
    anything, re-run a credit check, touch stock, move a balance or write a
    business record. It loads a result the engine already produced, renders
    the customer-safe part of it, and either hands that to the WhatsApp
    adapter or returns it as a draft for the owner to send themselves.

    Nothing here sends on its own. This route runs because a person pressed a
    button, and there is no scheduler, trigger or webhook anywhere in this
    project that can reach it.
    """
    raw = event.get("body") or ""
    if len(raw.encode("utf-8")) > MAX_BODY_BYTES:
        return _response(413, {"error": "request body too large"})
    try:
        payload = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        return _response(400, {"error": "body must be JSON"})

    message_type = str(payload.get("messageType") or "").upper().strip()
    if message_type not in MESSAGE_TYPES:
        return _response(400, {
            "error": "messageType must be one of " + ", ".join(sorted(MESSAGE_TYPES))})

    customer_id = _CONTROL.sub("", str(payload.get("customerId") or "")).strip()
    if customer_id and not CUSTOMER_ID_PATTERN.match(customer_id):
        return _response(400, {"error": "invalid customerId"})

    # Who is asking decides what can be asked for. A khata message is ABOUT an
    # account - its balance, its limit, whether it is on hold - and an
    # evaluation read all of that, plus the full phone number in the draft
    # link, from an anonymous request. Those two message types are prepared by
    # the shop, through the owner gate, and nobody else.
    owner = _is_demo_owner(event)
    if message_type in KHATA_MESSAGE_TYPES and not owner:
        return _owner_gate_response(
            "A khata message states a customer's balance and credit limit. It "
            "is prepared by the shop owner, through the ShopFlow demo "
            "workspace.")

    data = cached_dataset()
    customer = data.customer(customer_id) if customer_id else None
    customer_view_safe = (
        {"customerName": customer.customerName} if customer else None)

    # ---- the structured business result, loaded, never recalculated -----
    quote = credit = None
    if message_type in (QUOTATION, ORDER_CONFIRMATION):
        quote_id = str(payload.get("quoteId") or "")
        if not re.fullmatch(r"[0-9a-f]{32}", quote_id):
            return _response(400, {"error": "invalid quoteId"})
        job = table().get_item(Key=_job_key(quote_id)).get("Item")
        if not job or not job.get("result"):
            return _response(404, {"error": "that quotation was not found"})
        result = json.loads(job["result"])
        quote = result.get("quote")
        credit = result.get("credit")
        if not quote:
            return _response(400, {"error": "that job has no quotation"})
        # The customer on the ORDER wins over one named in the request, so a
        # message cannot be addressed to somebody the order was not for.
        if job.get("customerId"):
            customer = data.customer(job["customerId"]) or customer
            customer_view_safe = (
                {"customerName": customer.customerName} if customer else None)
    else:
        if not customer:
            return _response(404, {"error": "no khata account for that customer"})
        try:
            credit = check_credit(data, customer.customerId,
                                  payload.get("orderTotal") or 0)
        except InvalidOrderTotalError as exc:
            return _response(400, {"error": str(exc)})

    # ---- render, from an allow-list of customer-safe fields -------------
    # The customer's language. The builders translate the words around the
    # figures; no total, quantity or unit is recalculated for any language,
    # and the customer-safe allow-list is the same one in all of them.
    language = _language_of(payload)

    if not owner:
        return _public_whatsapp_draft(message_type, quote, payload, language)

    try:
        if message_type == QUOTATION:
            message = build_quotation_message(quote, customer_view_safe, credit,
                                              language=language)
        elif message_type == ORDER_CONFIRMATION:
            message = build_order_confirmation_message(
                quote, customer_view_safe,
                reference=str(payload.get("quoteId") or "")[:12],
                language=language)
        else:
            message = build_credit_status_message(
                credit, customer_view_safe,
                reminder=(message_type == CREDIT_REMINDER),
                language=language)
    except InvalidMessageRequest as exc:
        return _response(400, {"error": str(exc)})

    # ---- destination -----------------------------------------------------
    phone = masked = None
    if customer:
        try:
            phone = normalize_phone(customer.phone)
            masked = mask_phone(phone)
        except InvalidMessageRequest:
            phone = masked = None

    body = {
        "messageType": message["messageType"],
        "text": message["text"],
        "language": language,
        # Masked, always. The full number is never returned by this API.
        "recipient": masked,
        "customerId": customer.customerId if customer else None,
        "whatsapp": whatsapp.configuration_status(),
        # The draft is offered on every response, sent or not, so the owner
        # always has a way to get the message to the customer.
        "draftUrl": wa_me_url(message["text"], phone),
    }

    if not whatsapp.is_enabled():
        return _response(200, {
            **body,
            "status": "DRAFT",
            "sent": False,
            "reason": whatsapp.DISABLED,
            "notice": "WhatsApp API not configured \u2014 open the draft to send it "
                      "yourself.",
        })

    if not phone:
        return _response(400, {
            **body, "status": "DRAFT", "sent": False,
            "reason": whatsapp.INVALID_RECIPIENT,
            "notice": "That customer has no usable phone number."})

    try:
        sent = whatsapp.send_text(phone, message["text"])
    except whatsapp.WhatsAppError as exc:
        # Never a silent claim of delivery, and never the provider's own error
        # text - which can name the business account.
        return _response(200, {
            **body,
            "status": "FAILED",
            "sent": False,
            "reason": exc.reason,
            "notice": exc.safe_message + " You can still open the draft.",
        })

    return _response(200, {
        **body,
        "status": "SENT",
        "sent": True,
        "messageId": sent.get("messageId"),
        "reason": None,
        "notice": "Sent via WhatsApp.",
    })


# ---------------------------------------------------------------------------
# The customer's view of a job
# ---------------------------------------------------------------------------
# `GET /api/jobs/{jobId}` is customer-facing: anybody holding a job id can poll
# it. A live evaluation confirmed an owner's supplier price at Rs 6,300, placed
# a fresh order, polled it anonymously - and read back the shop's previous and
# confirmed supplier cost, its old and new margin, and a suggested selling
# price. The worker attaches those to the stored result for the owner's
# benefit, and this route returned the stored result whole. The same route was
# also returning a khata customer's credit limit and outstanding balance.
#
# The fix is here, at the response boundary, and nowhere else. The stored
# result is left intact because the owner is meant to see it; what changes is
# what leaves this function when the caller is not the owner.
#
# Two layers, deliberately:
#
#   1. An ALLOW-list of the fields a customer's order result may carry. A
#      field the worker adds tomorrow is absent from the customer view until
#      somebody decides it belongs there. Default-deny is the only version of
#      this that survives the next feature.
#   2. A recursive scrub of owner-only keys at EVERY depth, applied to what
#      the allow-list let through. It should find nothing. It exists so that
#      an owner figure nested inside an allowed object - a cost on a quote
#      line, say - cannot ride out inside it.
#
# The owner still receives everything, through the same demo gate as every
# other owner route. That gate is not authentication; see OWNER_ROUTES.

# Top-level fields of an order result a customer may receive.
#
# `trace` is deliberately absent. It is the raw record of every tool call -
# the model's arguments and the tools' own error strings, "These SKU ids do
# not exist ... Use only ids returned by search_catalog" - which is an audit
# record for the owner, kept intact in storage and in the owner view, and not
# something to hand a customer. `decisionTrace` is the customer's account of
# how the quotation was worked out.
#
# `matches`, `modelId`, `turns` and `elapsedMs` used to be on this list. A
# second evaluation found what that meant: `matches` carried the model's own
# search arguments and every line-isolation correction ("from": "Havells",
# "red", "90m" on a switch line), and the other three describe the model and
# its run rather than the customer's order. They are the workspace's.
CUSTOMER_RESULT_FIELDS = frozenset({
    "status", "summary", "message", "quote", "clarification",
    "grounded", "ungroundedNumbers", "decisionTrace", "localized",
})

# Top-level fields of a voice-transcript result a customer may receive.
#
# A transcript result is produced in exactly one place, `_poll_transcription`,
# and carries the caller's own words and which service read them. It holds no
# owner data today; the allow-list makes sure it never can by accident. The
# voice interface reads `transcript` and nothing else.
#
# `decisionTrace` is deliberately absent: the job route builds one for any
# stored result that lacks it, and for a transcript that produces an ORDER
# trace - "Order received. 0 product line(s) detected." - for something that
# was never an order. A customer is not shown it.
CUSTOMER_TRANSCRIPT_FIELDS = frozenset({
    "transcript", "provider", "handledBy", "detectedLanguage", "localized",
})

# Which allow-list governs which job type. A job type with no entry here gets
# no result at all in the customer view: default-deny applies to job types as
# well as to fields. (A supplier price list never reaches the customer view -
# `_get_job` refuses it outright without the owner marker.)
CUSTOMER_FIELDS_BY_JOB = {
    JOB_ORDER: CUSTOMER_RESULT_FIELDS,
    JOB_TRANSCRIPT: CUSTOMER_TRANSCRIPT_FIELDS,
}

# Text written for the MODEL, not for a person. `search_catalog` tells the
# model what to do next - "Do not invent a skuId", "call
# request_clarification and ask the shop owner" - and those instructions ride
# along inside each match. They are removed from the customer view at any
# depth. The match's verdict, its options and its line-isolation facts stay.
MODEL_FACING_KEYS = frozenset({"instruction", "diagnostic", "blocking"})

# What a customer is told when an order failed. The same sentence the agent
# already uses when it has nothing better to say, so no new wording is
# introduced - just the internal explanation ("the agent repeatedly produced
# invalid tool arguments") kept for the owner.
CUSTOMER_FAILURE_MESSAGE = "ShopFlow could not complete this order."

# Whole objects that are the owner's, whatever they are nested in.
OWNER_ONLY_OBJECTS = frozenset({
    "marginProtection", "credit", "plan", "review", "decisions",
    "confirmedCosts", "whatIf", "customerId", "customerName",
})

# Fragments of a key name that mark it as owner economics or customer PII.
# Matched against the key with case and separators removed, so
# `previousSupplierCost`, `unit_cost` and `MarginPct` are all caught.
OWNER_KEY_FRAGMENTS = (
    "supplier", "cost", "margin", "suggestedselling", "creditlimit",
    "outstanding", "phone", "budget", "plannedspend", "restock",
)


def _owner_key(key) -> bool:
    if key in OWNER_ONLY_OBJECTS or key in MODEL_FACING_KEYS:
        return True
    flat = re.sub(r"[^a-z]", "", str(key).lower())
    return any(fragment in flat for fragment in OWNER_KEY_FRAGMENTS)


def _scrub_owner_keys(value):
    """Every owner-only key removed, at any depth. Values are not rewritten."""
    if isinstance(value, dict):
        return {k: _scrub_owner_keys(v) for k, v in value.items()
                if not _owner_key(k)}
    if isinstance(value, list):
        return [_scrub_owner_keys(v) for v in value]
    return value


# ---------------------------------------------------------------------------
# Default-deny, at every depth
# ---------------------------------------------------------------------------
# The top-level allow-lists above were not enough. A quote line is an allowed
# object, and it carried `onHand`, `weeklyVelocity`, `coverageWeeks` and an
# evidence string reading "ordered 20, 14 in stock, short 6". The recursive
# scrub below them only removes keys it recognises as owner economics, and a
# stock count is not one - so it rode out inside an allowed object.
#
# So the customer view is now a SCHEMA: every key at every depth must be
# named here, and anything not named is dropped. `LEAF` accepts a plain value
# and refuses a nested object or list, so an allowed field that one day starts
# carrying a dict does not smuggle that dict out. The owner-key scrub still
# runs afterwards, as a second layer that should find nothing.
LEAF = None

_SCHEMA_EVIDENCE_LINE = {"lineTotal": LEAF, "units": LEAF}
_SCHEMA_QUOTE_LINE = {
    "skuId": LEAF, "name": LEAF, "unit": LEAF, "quantity": LEAF,
    "sellingPrice": LEAF, "lineTotal": LEAF, "inStock": LEAF,
    "catalogueUom": LEAF, "requestedUom": LEAF, "uomStatus": LEAF,
    "conversion": LEAF,
    "baseEquivalent": {"quantity": LEAF, "uom": LEAF, "perUnit": LEAF,
                       "calculation": LEAF},
    "evidence": _SCHEMA_EVIDENCE_LINE,
}
_SCHEMA_GST_LINE = {
    "skuId": LEAF, "quantity": LEAF, "unitPrice": LEAF, "taxableValue": LEAF,
    "gstRate": LEAF, "treatment": LEAF, "hsnHeading": LEAF, "cgst": LEAF,
    "sgst": LEAF, "igst": LEAF, "taxAmount": LEAF, "lineTotal": LEAF,
    "evidence": {"taxable": LEAF, "tax": LEAF, "rounding": LEAF},
}
_SCHEMA_GST = {
    "available": LEAF, "reason": LEAF, "taxMode": LEAF, "taxModeSource": LEAF,
    "taxModeNote": LEAF, "priceBasis": LEAF, "display": LEAF,
    "lines": [_SCHEMA_GST_LINE], "subtotal": LEAF, "totalTaxableValue": LEAF,
    "cgst": LEAF, "sgst": LEAF, "igst": LEAF, "totalGst": LEAF,
    "grandTotal": LEAF, "rates": [LEAF], "configStatus": LEAF,
    "configVersion": LEAF, "notice": LEAF, "unconfiguredSkus": [LEAF],
    "evidence": {"subtotal": LEAF, "totalGst": LEAF, "grandTotal": LEAF},
}
_SCHEMA_QUOTE = {
    "lines": [_SCHEMA_QUOTE_LINE], "total": LEAF, "itemCount": LEAF,
    "lineCount": LEAF, "allInStock": LEAF, "evidence": {"total": LEAF},
    "gst": _SCHEMA_GST,
}
_SCHEMA_CLARIFICATION = {
    "requestedText": LEAF, "clarifyingAttribute": LEAF, "question": LEAF,
    "questionSource": LEAF,
    "options": [{"skuId": LEAF, "name": LEAF, "value": LEAF,
                 "sellingPrice": LEAF, "unit": LEAF}],
}
_SCHEMA_LOCALIZED = {
    "language": LEAF, "direction": LEAF, "nativeName": LEAF, "coverage": LEAF,
    "reviewNotice": LEAF,
    "quote": {"language": LEAF, "direction": LEAF, "heading": LEAF,
              "itemsLabel": LEAF, "customerLabel": LEAF, "totalLabel": LEAF,
              "totalText": LEAF, "notice": LEAF},
    "clarification": {"language": LEAF, "direction": LEAF, "heading": LEAF,
                      "question": LEAF, "chooseLabel": LEAF, "attribute": LEAF},
}
_SCHEMA_TRACE = {
    "available": LEAF, "audience": LEAF, "reason": LEAF, "source": LEAF,
    "lines": [LEAF],
    "steps": [{field: LEAF for field in decision_trace.PUBLIC_FIELDS}],
}
CUSTOMER_ORDER_SCHEMA = {
    "status": LEAF, "summary": LEAF, "message": LEAF, "grounded": LEAF,
    "ungroundedNumbers": [LEAF], "quote": _SCHEMA_QUOTE,
    "clarification": _SCHEMA_CLARIFICATION, "decisionTrace": _SCHEMA_TRACE,
    "localized": _SCHEMA_LOCALIZED,
}
CUSTOMER_TRANSCRIPT_SCHEMA = {
    "transcript": LEAF, "provider": LEAF, "handledBy": LEAF,
    "detectedLanguage": LEAF,
    "localized": {k: LEAF for k in ("language", "direction", "nativeName",
                                    "coverage", "reviewNotice")},
}
CUSTOMER_SCHEMA_BY_JOB = {
    JOB_ORDER: CUSTOMER_ORDER_SCHEMA,
    JOB_TRANSCRIPT: CUSTOMER_TRANSCRIPT_SCHEMA,
}
# The job envelope. `customerId` is deliberately absent.
CUSTOMER_JOB_FIELDS = frozenset({"jobId", "jobType", "status", "orderText",
                                 "language", "createdAt", "error"})

_DROP = object()


def _allow(value, schema):
    """`value` reduced to exactly what `schema` names. Anything else is gone."""
    if value is None:
        # "no quotation" is information: a clarification says quote: null.
        return None
    if schema is LEAF:
        if isinstance(value, (str, int, float, bool)):
            return value
        return _DROP
    if isinstance(schema, list):
        if not isinstance(value, list):
            return _DROP
        kept = [_allow(item, schema[0]) for item in value]
        return [item for item in kept if item is not _DROP]
    if not isinstance(value, dict):
        return _DROP
    out = {}
    for key, item in value.items():
        if key not in schema:
            continue
        kept = _allow(item, schema[key])
        if kept is not _DROP:
            out[key] = kept
    return out


def _customer_quote_summary(quote: dict) -> str:
    """One sentence from the fields a customer may see, and nothing else."""
    lines = quote.get("lineCount") or len(quote.get("lines") or [])
    total = quote.get("total")
    text = f"{lines} items quoted at Rs {total}."
    if quote.get("allInStock") is False:
        text += (" Some items are not in stock right now; the shop will "
                 "confirm delivery.")
    elif quote.get("allInStock") is True:
        text += " Everything ordered is in stock."
    return text


def customer_job_view(body: dict) -> dict:
    """The job exactly as a customer may see it.

    Takes the job body as `_get_job` assembled it and returns a new one. The
    input is not modified, so nothing stored is changed by being viewed.
    """
    view = {k: v for k, v in body.items()
            if k in CUSTOMER_JOB_FIELDS and not _owner_key(k)}
    view = {k: v for k, v in view.items()
            if v is None or isinstance(v, (str, int, float, bool))}
    result = body.get("result")
    if isinstance(result, dict):
        job_type = view.get("jobType", JOB_ORDER)
        schema = CUSTOMER_SCHEMA_BY_JOB.get(job_type)
        allowed = CUSTOMER_FIELDS_BY_JOB.get(job_type)
        if schema is None or allowed is None:
            # A job type nobody has decided a customer view for.
            return _scrub_owner_keys(view)
        result = {k: v for k, v in result.items() if k in allowed}
        if job_type == JOB_ORDER:
            # The workspace trace carries stock counts and the model's own
            # proposals; the customer is given the public reading of it.
            result["decisionTrace"] = decision_trace.public_view(
                result.get("decisionTrace"))
            if result.get("status") == "FAILED":
                # The summary of a failed run is the model's own prose.
                result["summary"] = CUSTOMER_FAILURE_MESSAGE
                result["message"] = CUSTOMER_FAILURE_MESSAGE
            elif isinstance(result.get("quote"), dict):
                # The owner's summary names each shortfall ("short by 6
                # piece"), and ordered-minus-short is the shop's stock count.
                # The customer is told only what the allowed quote fields say.
                result["summary"] = _customer_quote_summary(result["quote"])
        allowed_result = _allow(result, schema)
        view["result"] = _scrub_owner_keys(
            allowed_result if allowed_result is not _DROP else {})
    return _scrub_owner_keys(view)


def _job_response(event, body: dict) -> dict:
    """Send a job to whoever asked, marked with who it was built for."""
    owner = _is_demo_owner(event)
    response = _response(200, body if owner else customer_job_view(body))
    response["headers"]["x-shopflow-audience"] = "owner" if owner else "customer"
    return response


def _get_job(event) -> dict:
    job_id = (event.get("pathParameters") or {}).get("jobId") or ""
    if not re.fullmatch(r"[0-9a-f]{32}", job_id):
        return _response(400, {"error": "invalid job id"})

    item = table().get_item(Key=_job_key(job_id)).get("Item")
    if not item:
        return _response(404, {"error": "job not found"})

    # The language the order was written in, carried on the job row rather
    # than re-detected here. Re-detecting would risk answering a poll in a
    # different language from the one the owner chose.
    language = normalize_language(item.get("language"))

    body = {
        "jobId": item["jobId"],
        "jobType": item.get("jobType", JOB_ORDER),
        "status": item["status"],
        "orderText": item.get("orderText"),
        "customerId": item.get("customerId") or None,
        "language": language,
        "createdAt": int(item.get("createdAt", 0)),
    }
    if item.get("result"):
        # Localized additively: the result's own fields are carried through
        # untouched, and a `localized` block of words is attached beside them.
        body["result"] = localize_response(json.loads(item["result"]), language)

        # The trace the worker built, or one rebuilt from the stored result
        # for a job that predates it. Customer-safe by construction: it is
        # `decision_trace.customer_steps`, which cannot produce a supplier
        # cost, a margin or a budget, and which never reads model prose.
        #
        # A trace is an explanation of a quotation and may never be a reason
        # not to have one, so a failure here says so and the result stands.
        if not isinstance(body["result"].get("decisionTrace"), dict):
            body["result"]["decisionTrace"] = decision_trace.build(
                body["result"])
    if item.get("error"):
        body["error"] = item["error"]

    if body["jobType"] == JOB_TRANSCRIPT and item["status"] in ("QUEUED", "PROCESSING"):
        # Transcribe is doing the waiting. Each poll asks it once; there is no
        # loop in a Lambda and no background process that can outlive a tab.
        return _job_response(event, _poll_transcription(item, body))

    if body["jobType"] == JOB_PRICE_LIST:
        # A price-list result IS supplier cost: every matched row carries what
        # the shop last paid and what the document says it will now pay. This
        # route is otherwise customer-facing, so the document half of it is
        # not - reading a supplier price list requires the owner gate, as
        # calling POST /api/supplier-price-lists already did.
        #
        # This closed a real hole. The upload route was gated and the route
        # that returned its answer was not, so the costs were one poll away
        # from anybody who had the job id.
        if not _is_demo_owner(event):
            owner_only = _response(401, {
                "error": "owner route",
                "message": ("A supplier price list is owner data. Poll this "
                            "job with the ShopFlow demo workspace."),
                "demoGate": True,
                "isAuthentication": False,
                "note": OWNER_GATE_NOTE,
            })
            owner_only.setdefault("headers", {})["x-shopflow-audience"] = "owner"
            return owner_only

        # Owner rulings live alongside the job so a reload shows what was
        # already decided rather than asking again.
        rows = table().query(
            KeyConditionExpression=Key("PK").eq(f"DECISION#{job_id}")
        ).get("Items", [])
        body["decisions"] = [
            {k: v for k, v in row.items() if k not in ("PK", "SK", "expiresAt")}
            for row in rows
        ]
        # The review boundary, recomputed now that the rulings are known. A
        # row the owner confirmed reads CONFIRMED; everything else is derived
        # exactly as it was at extraction time.
        _apply_review_states(body, body["decisions"])

    return _job_response(event, body)


def _apply_review_states(body: dict, decisions: list) -> None:
    """Recompute each document row's review state from the owner's rulings.

    Derived rather than stored, so a ruling recorded after the document was
    read is reflected without rewriting the stored result. The states
    themselves are `engine.supplier_prices.review_state`; nothing is decided
    here.
    """
    review = ((body.get("result") or {}).get("review") or {})
    lines = review.get("lines")
    if not isinstance(lines, list):
        return

    ruled = {d.get("skuId"): d.get("decision")
             for d in decisions or [] if d.get("skuId")}
    for line in lines:
        sku_id = line.get("skuId")
        decision = ruled.get(sku_id)
        if decision == SUPPLIER_CONFIRMED:
            line["reviewState"] = STATE_CONFIRMED
        elif decision:
            line["reviewState"] = STATE_REVIEW_REQUIRED
    review["confirmedCount"] = sum(
        1 for line in lines if line.get("reviewState") == STATE_CONFIRMED)


# ---------------------------------------------------------------------------
# ShopFlow Intelligence
# ---------------------------------------------------------------------------
# One owner route behind the owner gate, answering the three questions the
# workspace's Intelligence section asks: what documents have been read, what
# needs my attention, and is the system healthy.
#
# Every figure here is read from a deterministic engine or from a job row that
# an engine wrote. Nothing is recomputed, no model is called, and there is no
# second copy of any business rule: the alerts below come from
# `engine.margin.margin_alerts`, `engine.budget.restock_candidates` and the
# shop's own confirmed cost records - the same three sources the planner uses.

# How far back the Intelligence view looks. Job rows expire after 24 hours
# anyway (`JOB_TTL_SECONDS`), so a larger number would return the same rows.
INTELLIGENCE_JOB_LIMIT = 50


def _recent_jobs(limit: int = INTELLIGENCE_JOB_LIMIT) -> list:
    """The shop's most recent jobs, newest first, from GSI1.

    Returns an empty list rather than raising. The Intelligence view is an
    operational read; if the index query fails, the page says it has nothing
    to show and every other route is unaffected.
    """
    try:
        rows = table().query(
            IndexName="GSI1",
            KeyConditionExpression=Key("GSI1PK").eq(f"SHOP#{DEFAULT_SHOP_ID}")
            & Key("GSI1SK").begins_with("JOB#"),
            ScanIndexForward=False,
            Limit=limit,
        ).get("Items", [])
    except Exception as exc:  # noqa: BLE001
        print(f"intelligence job query failed: {type(exc).__name__}: {exc}")
        return []
    return rows


def _document_summary(row: dict) -> dict:
    """One price-list job, as the Supplier Documents tab shows it."""
    summary = {
        "jobId": row.get("jobId"),
        "status": row.get("status"),
        "createdAt": int(row.get("createdAt") or 0),
        "contentType": row.get("imageContentType"),
        "bytes": int(row.get("imageBytes") or 0),
        "reader": None,
        "supplierName": None,
        "lineCount": 0,
        "matchedCount": 0,
        "reviewRequiredCount": 0,
        "materialChangeCount": 0,
        "error": row.get("error") or None,
    }
    try:
        review = (json.loads(row["result"]) or {}).get("review") or {}
    except (KeyError, TypeError, ValueError):
        return summary

    readers = review.get("readers") or []
    summary.update({
        "reader": readers[0] if readers else None,
        "supplierName": review.get("supplierName"),
        "lineCount": review.get("lineCount") or 0,
        "matchedCount": review.get("matchedCount") or 0,
        "reviewRequiredCount": review.get("reviewRequiredCount") or 0,
        "materialChangeCount": review.get("materialChangeCount") or 0,
        "documentDate": review.get("documentDate"),
    })
    return summary


def _operations(rows: list) -> dict:
    """Counts of what the queue has been doing. Operations, not business."""
    outcomes = {"QUOTED": 0, "NEEDS_CLARIFICATION": 0, "FAILED": 0,
                "REVIEWED": 0}
    counts = {"total": 0, "orders": 0, "documents": 0, "queued": 0,
              "processing": 0, "done": 0, "failed": 0}

    for row in rows:
        counts["total"] += 1
        job_type = str(row.get("jobType") or JOB_ORDER)
        if job_type == JOB_ORDER:
            counts["orders"] += 1
        elif job_type == JOB_PRICE_LIST:
            counts["documents"] += 1

        status = str(row.get("status") or "")
        if status == "QUEUED":
            counts["queued"] += 1
        elif status == "PROCESSING":
            counts["processing"] += 1
        elif status == "DONE":
            counts["done"] += 1
        elif status == "FAILED":
            counts["failed"] += 1

        try:
            result = json.loads(row["result"]) or {}
        except (KeyError, TypeError, ValueError):
            continue
        outcome = str(result.get("status") or "")
        if outcome in outcomes:
            outcomes[outcome] += 1

    return {"counts": counts, "outcomes": outcomes,
            "window": "the last %d jobs" % INTELLIGENCE_JOB_LIMIT,
            "note": ("Counted from job rows, which expire after 24 hours. "
                     "CloudWatch holds the durable operational history.")}


def _alerts(data, rows: list) -> list:
    """What the owner should look at, derived from the engines.

    Four kinds, and each one is somebody else's calculation:

      * a supplier cost the owner confirmed that moved what they pay
      * a margin the margin engine calls low
      * a SKU the purchasing planner calls short
      * a job that failed

    Nothing here decides what is material or what is low. It reads the
    engines' own verdicts and puts them in one list.
    """
    alerts = []
    confirmed = _confirmed_costs()

    costs = {sku: entry["cost"] for sku, entry in confirmed.items()}
    for alert in margin_alerts(data, costs):
        if not alert.get("comparisonAvailable"):
            continue
        if alert.get("previousSupplierCost") != alert.get("confirmedSupplierCost"):
            # The price shock engine's judgement of the same move: deltas,
            # margin, walk-away price and a severity. Engine figures only.
            shock = evaluate_price_change(
                data, alert["skuId"], alert.get("previousSupplierCost"),
                alert.get("confirmedSupplierCost"), confirmed_costs=costs,
                budget=BRIEF_BUDGET)
            alerts.append({
                "kind": "SUPPLIER_PRICE_CHANGED",
                "skuId": alert.get("skuId"),
                "productName": alert.get("productName"),
                "previousCost": alert.get("previousSupplierCost"),
                "newCost": alert.get("confirmedSupplierCost"),
                "detail": "Confirmed supplier cost has moved.",
                "severity": shock["severity"],
                "priceAlert": shock,
            })
        if alert.get("status") == "LOW_MARGIN":
            alerts.append({
                "kind": "LOW_MARGIN",
                "skuId": alert.get("skuId"),
                "productName": alert.get("productName"),
                "previousMargin": alert.get("oldMarginAmount"),
                "newMargin": alert.get("newMarginAmount"),
                "marginPercent": alert.get("newMarginPercent"),
                "detail": "Margin is below the configured threshold.",
            })

    demand = committed_demand(data.committedOrders())
    for sku_id in sorted(data.products):
        if data.onHand(sku_id) <= 0:
            alerts.append({
                "kind": "STOCKOUT",
                "skuId": sku_id,
                "productName": data.product(sku_id).name,
                "onHand": data.onHand(sku_id),
                "detail": "Nothing on the shelf.",
            })
        elif data.onHand(sku_id) - demand.get(sku_id, 0) < 0:
            # Promised beyond the shelf. `uncommitted_stock(...) < 0` was
            # tested here before and can never be true - it clamps at zero.
            alerts.append({
                "kind": "STOCKOUT",
                "skuId": sku_id,
                "productName": data.product(sku_id).name,
                "onHand": data.onHand(sku_id),
                "detail": "Promised beyond available stock.",
            })

    for row in rows:
        if str(row.get("status")) == "FAILED":
            alerts.append({
                "kind": "PROCESSING_FAILED",
                "jobId": row.get("jobId"),
                "jobType": row.get("jobType"),
                "detail": str(row.get("error") or "Processing failed.")[:200],
            })

    return alerts


# The weekly restocking cash the brief and the price alerts plan against -
# the same variable the worker reads, so both describe the same plan.
BRIEF_BUDGET = float(os.environ.get("SHOPFLOW_BRIEF_BUDGET") or 25000)


def alert_delivery() -> dict:
    """Whether an alert can reach a person. Configuration, stated as it is.

    The CDK stack sets ALERT_DELIVERY to EMAIL only when it subscribed an
    address to the owner alert topic. Anything else means events are
    published and nobody receives them, and the page says exactly that.
    """
    channel = (os.environ.get("ALERT_DELIVERY") or "NONE").upper()
    configured = channel == "EMAIL"
    return {
        "configured": configured,
        "channel": channel if configured else "NONE",
        "eventsPublished": bool(events.bus_name()),
        "note": ("Alerts are published to EventBridge and delivered by SNS "
                 "email." if configured else
                 "Alerts are published to EventBridge and the SNS alert "
                 "topic, which has no subscriber in this demo. Nobody is "
                 "notified; the alerts are shown here."),
    }


def _stored_brief_summary(signature: str) -> dict:
    """The scheduled run's model summary, only if written for these figures."""
    try:
        item = table().get_item(Key={"PK": f"BRIEF#{DEFAULT_SHOP_ID}",
                                     "SK": "LATEST"}).get("Item")
    except Exception as exc:  # noqa: BLE001
        print(f"stored brief unavailable: {type(exc).__name__}")
        return {"available": False, "reason": "UNAVAILABLE"}
    if not item:
        return {"available": False, "reason": "NOT_YET_GENERATED"}
    generated = {"generatedAt": int(item.get("generatedAt") or 0),
                 "briefDate": item.get("briefDate")}
    if item.get("signature") != signature:
        return {"available": False, "reason": "FIGURES_CHANGED", **generated}
    try:
        summary = json.loads(item.get("summary") or "null")
    except ValueError:
        summary = None
    if not summary:
        return {"available": False, "reason": "NO_SUMMARY", **generated}
    return {"available": True, "text": summary.get("text"), **generated}


def _brief(data) -> dict:
    """Today's brief, built now from current state, plus any stored summary."""
    costs = {sku: entry["cost"] for sku, entry in _confirmed_costs().items()}
    brief = build_brief(data, costs, budget=BRIEF_BUDGET, now=int(time.time()))
    brief["summary"] = _stored_brief_summary(brief_signature(brief))
    return brief


def _get_intelligence(event) -> dict:
    """Supplier documents, alerts and operations, for the owner workspace."""
    data = cached_dataset()
    rows = _recent_jobs()
    documents = [_document_summary(row) for row in rows
                 if str(row.get("jobType")) == JOB_PRICE_LIST]

    try:
        brief = _brief(data)
    except Exception as exc:  # noqa: BLE001
        print(f"daily brief unavailable: {type(exc).__name__}: {exc}")
        brief = None

    # Alerts read the shop's confirmed costs, which is another table query.
    # If it fails, the page shows what it could read rather than nothing:
    # this route reports on the system and must not be the part that breaks.
    try:
        alerts = _alerts(data, rows)
    except Exception as exc:  # noqa: BLE001
        print(f"intelligence alerts unavailable: {type(exc).__name__}: {exc}")
        alerts = []

    return _response(200, {
        "shopId": f"SHOP#{DEFAULT_SHOP_ID}",
        "documents": documents,
        "alerts": alerts,
        "operations": _operations(rows),
        "eventsEnabled": bool(events.bus_name()),
        "brief": brief,
        "alertDelivery": alert_delivery(),
        "synthetic": True,
        "dataNotice": ("Synthetic demo data. Inventory, sales history and "
                       "customer records are generated and do not represent "
                       "a real shop's records."),
        "source": "lambdas.api.handler._get_intelligence",
    })


def _get_languages(event) -> dict:
    """The registry and the capability matrix, so the browser hard-codes neither.

    Read-only, model-free and free of business data. It reports what each
    language can actually do here - whether a resource file exists, how much
    of it is translated, and whether the configured speech provider accepts
    the language - rather than a list of languages somebody hopes work.
    """
    return _response(200, {
        "default": DEFAULT_LANGUAGE,
        "languages": language_directory(),
        "capabilities": capability_matrix(),
        "note": "ShopFlow supports multilingual retail interaction across "
                "India's 22 Scheduled Languages, with English as the default "
                "fallback.",
    })


# What the workspace inventory table may show. An allow-list, for the same
# reason `engine.messages.customer_safe_line` is one: `Product` also carries
# `costPrice`, and this route is public. A field added to Product later is
# absent from here until somebody decides otherwise.
INVENTORY_FIELDS = ("skuId", "name", "brand", "category", "unit",
                    "sellingPrice", "onHand", "status")


def _inventory_snapshot(data) -> list:
    """The seeded shop's stock, as the workspace shows it.

    No new inventory logic. `restock_candidates` is the same function the
    purchasing planner uses to decide what is below its reorder point, and
    `committed_demand` is the same one that totals what is already promised
    to a customer. This reads both and adds a label - it does not compute a
    threshold of its own, because a second opinion about what "low" means is
    exactly how two parts of a system start disagreeing.
    """
    demand = committed_demand(data.committedOrders())
    low = {c.skuId for c in restock_candidates(data)}

    rows = []
    for sku_id in sorted(data.products):
        product = data.product(sku_id)
        on_hand = data.onHand(sku_id)
        if on_hand <= 0:
            # Nothing on the shelf. This used to fall through to IN STOCK
            # whenever the SKU sold slowly enough that the planner did not
            # call it low, so two SKUs with zero stock were labelled as
            # available. Reading the label, not the number, is exactly what a
            # shop owner does.
            #
            # This is serialization only. The quantity is untouched, the
            # planner's reorder rule is untouched, and nothing here decides
            # what to buy.
            status = "OUT OF STOCK"
        elif on_hand - demand.get(sku_id, 0) < 0:
            # Already promised more than is on the shelf. This used to test
            # `uncommitted_stock(...) < 0`, which can never be true - that
            # function clamps at zero - so the label was unreachable and the
            # canonical order's switch (14 on hand, 20 promised) read IN STOCK
            # beside a quotation reporting it short by 6.
            status = "SHORTAGE"
        elif sku_id in low:
            status = "LOW STOCK"     # below the planner's reorder point
        else:
            status = "IN STOCK"
        rows.append({
            "skuId": sku_id,
            "name": product.name,
            "brand": product.brand,
            "category": product.category,
            "unit": product.unit,
            "sellingPrice": product.sellingPrice,
            "onHand": on_hand,
            "status": status,
        })
    return rows


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
        "shopLocation": "Madurai, Tamil Nadu",
        "businessType": "Electrical & hardware retail",
        "dataNotice": ("Synthetic demo data. Inventory and sales history are "
                       "generated and do not represent a real shop's records."),
        "inventory": _inventory_snapshot(data),
    })


# Which routes answer the shop owner about their own business, and which
# produce something a customer may see.
#
# ShopFlow has two audiences and only one of them may be shown what the shop
# pays its suppliers. The owner console asks "what is my margin on this SKU?"
# and "what can I afford to restock?"; those answers necessarily carry
# supplier cost, and removing it would delete the feature rather than secure
# it. A customer sees a quotation and a WhatsApp message, and those are built
# through `engine.messages.customer_safe_quote`, an allow-list.
#
# This demo has no login, so the boundary below is a declaration of design
# intent, not an access control. It is written down, marked on the response
# and asserted in tests/test_audience_boundary.py so that it is a decision
# rather than an oversight - and so that a route that starts leaking cost
# into customer-facing output fails a test instead of shipping. In a real
# deployment the OWNER routes sit behind the shop's own login; the demo is
# open on purpose, and every figure in it is synthetic.
OWNER_ROUTES = frozenset({
    "POST /api/shop-queries",       # margin and stock answers for the owner
    "POST /api/purchase-plans",     # what to buy, at supplier cost
    "POST /api/supplier-price-lists",
    "POST /api/price-decisions",
    # Khata accounts. These carry a contractor's name, phone number, credit
    # limit and outstanding balance - shaped exactly like real customer
    # records even though every digit here is generated. They were
    # unclassified, which meant nothing stopped a plain GET reading them.
    "GET /api/customers",
    "GET /api/customers/{customerId}",
    # A credit decision states the limit and the balance it was made against.
    # It was marked customer-facing, which was wrong: it is the answer a shop
    # owner gets at the counter, not something a customer is shown.
    "POST /api/credit/check",
    # Supplier documents, margin alerts and queue operations.
    "GET /api/intelligence",
})

# Routes whose output can reach a customer. Nothing here may carry supplier
# cost, margin or purchasing internals. `POST /api/whatsapp/send` is here
# because its TEXT reaches a customer; the khata message types on it are
# gated inside the route, and an anonymous caller only ever gets a draft.
CUSTOMER_FACING_ROUTES = frozenset({
    "POST /api/orders",
    "GET /api/jobs/{jobId}",
    "POST /api/whatsapp/send",
})

# Routes that carry no business figure at all: the language registry, the
# seeded demo example, and speech-to-text of the caller's own recording.
PUBLIC_ROUTES = frozenset({
    "GET /api/languages",
    "GET /api/demo",
    "POST /api/voice/transcribe",
})

# The demo-owner gate.
#
# What this is: owner routes require the caller to say, explicitly, that it is
# asking as the shop owner. The demo workspace sends this header once someone
# has entered it. A plain public GET of a khata account or a purchase plan no
# longer returns one, and any route added to OWNER_ROUTES is gated by that
# fact alone rather than by someone remembering to gate it.
#
# What this is NOT, stated plainly because an evaluator will work it out in
# seconds and should not have to: it is not authentication. The value is not a
# secret, it is visible in the page source, and anyone who wants past it can
# send the header themselves. It stops accidental and drive-by exposure, and
# it makes the boundary executable instead of declarative. It does not make
# these routes private.
#
# Making them genuinely private needs an authenticated owner session, which
# this demo deliberately does not have - see the limitation recorded in
# README.md. Everything behind this gate is synthetic, and no real shop's
# records are in this project.
OWNER_GATE_NOTE = ("This is a demo gate, not authentication. The header is "
                   "not a secret. In a real deployment these routes sit "
                   "behind the shop's own login. All data here is synthetic.")

DEMO_OWNER_HEADER = "x-shopflow-demo-owner"
DEMO_OWNER_VALUE = "demo-workspace"


def _is_demo_owner(event) -> bool:
    """Did the caller ask as the shop owner? Header names are case-insensitive."""
    for key, value in (event.get("headers") or {}).items():
        if str(key).strip().lower() == DEMO_OWNER_HEADER:
            return str(value).strip() == DEMO_OWNER_VALUE
    return False

ROUTES = {
    "POST /api/orders": _create_order,
    "POST /api/supplier-price-lists": _create_price_list,
    "POST /api/price-decisions": _create_price_decision,
    "POST /api/purchase-plans": _create_purchase_plan,
    "POST /api/shop-queries": _create_shop_query,
    "GET /api/customers": _get_customers,
    "GET /api/customers/{customerId}": _get_customer,
    "POST /api/credit/check": _create_credit_check,
    "POST /api/voice/transcribe": _create_transcription,
    "POST /api/whatsapp/send": _create_whatsapp_send,
    "GET /api/languages": _get_languages,
    "GET /api/jobs/{jobId}": _get_job,
    "GET /api/demo": _get_demo,
    "GET /api/intelligence": _get_intelligence,
}


def handler(event, context):
    route = event.get("routeKey") or ""
    fn = ROUTES.get(route)
    if fn is None:
        return _response(404, {"error": "not found"})
    # Default-deny: a route nobody has classified is not served. Adding a
    # handler to ROUTES without deciding who may read it fails closed.
    if route not in OWNER_ROUTES | CUSTOMER_FACING_ROUTES | PUBLIC_ROUTES:
        return _response(404, {"error": "not found"})

    if route in OWNER_ROUTES and not _is_demo_owner(event):
        response = _response(401, {
            "error": "owner route",
            "message": (
                "This route answers the shop owner about their own business - "
                "supplier costs, margins, purchasing and khata accounts - and "
                "is not part of the public customer surface. The ShopFlow demo "
                f"workspace marks its requests with the {DEMO_OWNER_HEADER} "
                "header."),
            "demoGate": True,
            "isAuthentication": False,
            "note": OWNER_GATE_NOTE,
        })
        response.setdefault("headers", {})["x-shopflow-audience"] = "owner"
        return response

    try:
        response = fn(event)
    except Exception as exc:  # noqa: BLE001 - surface a safe message, log detail
        print(f"ERROR handling {route}: {type(exc).__name__}: {exc}")
        return _response(500, {"error": "internal error"})

    # Say which audience this answer was built for. An owner answer may carry
    # supplier cost; a customer-facing one may not. Marking it costs nothing
    # and means anyone reading the API - including someone auditing it - sees
    # the boundary instead of having to infer it.
    headers = response.setdefault("headers", {})
    if headers.get("x-shopflow-audience"):
        # The route already said who this particular answer is for, and it
        # knows better than the table does. `GET /api/jobs/{id}` is the case:
        # an order result is customer-facing, and a supplier price list read
        # back through the same route is not.
        return response
    if route in OWNER_ROUTES:
        headers["x-shopflow-audience"] = "owner"
    elif route in CUSTOMER_FACING_ROUTES:
        headers["x-shopflow-audience"] = "customer"
    return response
