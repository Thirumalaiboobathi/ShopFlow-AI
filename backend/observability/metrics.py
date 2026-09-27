"""CloudWatch metrics, emitted through the log line that was already written.

WHAT THIS IS
------------
Embedded Metric Format. A structured JSON line on stdout that CloudWatch Logs
parses into metrics automatically. Two properties make it the right choice
here rather than `PutMetricData`:

  * **No IAM.** Nothing calls a CloudWatch API, so no role gains a permission.
    A metric emitter that cannot make a network call also cannot fail a
    request, retry, or add latency to an order.
  * **No second write.** These functions already log a line per job. EMF turns
    that line into a metric instead of adding one.

WHAT MAY NEVER GO IN HERE
-------------------------
A business figure. Not a total, not a price, not a credit limit, not a margin,
not a quantity a customer asked for.

The reason is concrete: CloudWatch metrics and their dimensions are retained,
queryable and visible to anyone with console access, long after the job row
has expired. A quotation total in a metric is a business figure leaking into
an operational system through the one door nobody thinks to check. So the
allow-list below is a list of names, `_SAFE_DIMENSIONS` is short, and a test
asserts that no money-shaped value can reach either.

Counting orders is operations. Recording what one was worth is not.
"""

from __future__ import annotations

import json
import sys
import time
from typing import Dict, Optional

NAMESPACE = "ShopFlow"

# Every metric this application emits. A name not on this list is dropped
# rather than sent, so a typo becomes a missing metric instead of a new one
# billed at $0.30/month forever.
ORDERS_QUEUED = "ShopFlowOrdersQueued"
ORDERS_COMPLETED = "ShopFlowOrdersCompleted"
ORDERS_FAILED = "ShopFlowOrdersFailed"
WORKER_PROCESSING_SECONDS = "ShopFlowWorkerProcessingSeconds"
WORKER_FAILURES = "ShopFlowWorkerFailures"
DUPLICATE_DELIVERIES = "ShopFlowDuplicateDeliveries"
QUEUE_SEND_FAILURES = "ShopFlowQueueSendFailures"

# Business events. These count deterministic things that happened in the shop,
# never what anything was worth: `SupplierPriceChanges` is a count of changes,
# not a sum of money, for the same reason the rest of this module refuses
# business figures.
BUSINESS_EVENTS = "ShopFlowBusinessEvents"
EVENT_PUBLISH_FAILURES = "ShopFlowEventPublishFailures"
ALERT_PUBLISH_FAILURES = "ShopFlowAlertPublishFailures"
DOCUMENTS_EXTRACTED = "ShopFlowDocumentsExtracted"
DOCUMENT_EXTRACTION_FAILURES = "ShopFlowDocumentExtractionFailures"
# Emitted as EMF like the rest, so they appear in CloudWatch under this
# namespace without any infrastructure change. No dashboard widget or alarm
# is attached to them in this change.
WHATIF_SIMULATIONS = "ShopFlowWhatIfSimulations"
GST_CALCULATIONS = "ShopFlowGstCalculations"
MODEL_ERRORS = "ShopFlowModelErrors"
# Supplier shock alerts, by Severity (WARNING / CRITICAL). One metric
# with a dimension rather than three metrics. And the scheduled daily brief,
# by Outcome (SUMMARIZED: a grounded model summary was attached;
# DETERMINISTIC: the structured brief only).
SUPPLIER_ALERTS = "ShopFlowSupplierAlerts"
DAILY_BRIEFS = "ShopFlowDailyBriefs"

_UNITS: Dict[str, str] = {
    ORDERS_QUEUED: "Count",
    ORDERS_COMPLETED: "Count",
    ORDERS_FAILED: "Count",
    WORKER_PROCESSING_SECONDS: "Seconds",
    WORKER_FAILURES: "Count",
    DUPLICATE_DELIVERIES: "Count",
    QUEUE_SEND_FAILURES: "Count",
    BUSINESS_EVENTS: "Count",
    EVENT_PUBLISH_FAILURES: "Count",
    ALERT_PUBLISH_FAILURES: "Count",
    DOCUMENTS_EXTRACTED: "Count",
    DOCUMENT_EXTRACTION_FAILURES: "Count",
    WHATIF_SIMULATIONS: "Count",
    GST_CALCULATIONS: "Count",
    MODEL_ERRORS: "Count",
    SUPPLIER_ALERTS: "Count",
    DAILY_BRIEFS: "Count",
}

# Deliberately two, and deliberately low-cardinality. Every distinct dimension
# VALUE creates a separate billed metric, so a dimension carrying a job id
# would create one metric per order. `jobId` belongs in the log line beside
# the metric - searchable, free, and not retained as a metric forever.
_SAFE_DIMENSIONS = ("JobType", "Outcome", "EventType", "Reader", "Severity")

# Metrics that are ALSO published with no dimensions at all.
#
# A metric emitted only under JobType/Outcome exists in CloudWatch only under
# that exact pair, so an alarm has to name one - and "an order failed" is not
# a question about one pair. An agent failure marks the job FAILED and
# acknowledges the message, which is correct (it will never succeed on a
# retry) and which is also why it never reaches the dead-letter queue and why
# the DLQ alarm has never seen one.
#
# So these two are published a second time with an empty dimension set, which
# is what an alarm can actually watch. It costs one extra custom metric each
# and is the whole of the fix: a failure that is invisible to every alarm is
# not being monitored, however many alarms there are.
_AGGREGATE_METRICS = frozenset({
    ORDERS_FAILED, WORKER_FAILURES,
    # Publishing an event or an alert is not business logic, so a failure here
    # must be visible without being able to change an order. Undimensioned so
    # a single alarm or dashboard widget can watch it.
    EVENT_PUBLISH_FAILURES, ALERT_PUBLISH_FAILURES,
    DOCUMENT_EXTRACTION_FAILURES,
})

# Job types and outcomes are closed sets. Anything else is dropped, which
# bounds cardinality by construction rather than by care.
_ALLOWED_VALUES = {
    "JobType": {"ORDER", "PRICE_LIST", "TRANSCRIPT", "UNKNOWN"},
    # NO_PRODUCT: a message naming nothing the shop sells, answered without
    # an order - not a failure. THROTTLED: a retryable failure caused by the
    # Bedrock request-rate quota, counted apart from other transient errors.
    # MODEL_ERROR: Bedrock said the model's own turn was unusable; retried.
    # SIMULATED / REFUSED: a What-If answer, or a scenario it declined.
    # PURCHASE_PLAN: GST worked out beside a purchase plan.
    "Outcome": {"QUOTED", "NEEDS_CLARIFICATION", "NOT_FOUND", "REVIEWED",
                "FAILED", "DUPLICATE", "RETRYABLE", "THROTTLED", "TERMINAL",
                "NO_PRODUCT", "UNKNOWN", "MODEL_ERROR", "SIMULATED", "REFUSED",
                "PURCHASE_PLAN", "SUMMARIZED", "DETERMINISTIC"},
    # The closed set of business events. A typo becomes a dropped dimension
    # rather than a new billed metric, exactly as with the two above.
    "EventType": {"SupplierPriceChanged", "StockoutDetected",
                  "LowMarginDetected", "PurchasePlanGenerated",
                  "OrderNeedsClarification", "OrderProcessingFailed",
                  "DailyShopBriefGenerated"},
    # Which reader produced a document's rows.
    "Reader": {"TEXTRACT", "NOVA_PRO", "NONE"},
    # A supplier alert's severity, as the alert engine judged it.
    "Severity": {"INFO", "WARNING", "CRITICAL"},
}


def _clean_dimensions(dimensions: Optional[Dict[str, str]]) -> Dict[str, str]:
    out = {}
    for key in _SAFE_DIMENSIONS:
        value = (dimensions or {}).get(key)
        if value is None:
            continue
        value = str(value)
        if value in _ALLOWED_VALUES[key]:
            out[key] = value
    return out


def emit(metric: str, value: float = 1, *,
         dimensions: Optional[Dict[str, str]] = None,
         job_id: str = "", **fields) -> Optional[dict]:
    """One metric, as an EMF log line. Returns the document, or None if dropped.

    Never raises. A telemetry failure must not be able to fail an order, so
    every problem here ends as a dropped metric and the caller carries on.
    The return value exists so tests can assert on the document rather than
    on captured stdout.
    """
    try:
        if metric not in _UNITS:
            return None
        clean = _clean_dimensions(dimensions)
        # The dimension SETS this metric is published under. One grouping for
        # the breakdown, plus - for the two metrics an alarm watches - the
        # undimensioned total. CloudWatch bills per set, so this list stays
        # short by construction.
        sets = [list(clean)] if clean else [[]]
        if metric in _AGGREGATE_METRICS and [] not in sets:
            sets.append([])
        document = {
            "_aws": {
                "Timestamp": int(time.time() * 1000),
                "CloudWatchMetrics": [{
                    "Namespace": NAMESPACE,
                    "Dimensions": sets,
                    "Metrics": [{"Name": metric, "Unit": _UNITS[metric]}],
                }],
            },
            metric: float(value),
            **clean,
        }
        # Properties, not dimensions: searchable in Logs Insights, and not
        # retained as a billed metric. This is where the correlation id goes.
        if job_id:
            document["jobId"] = str(job_id)
        for key, field in (fields or {}).items():
            if isinstance(field, (str, int, float, bool)) or field is None:
                document[key] = field
        print(json.dumps(document), file=sys.stdout)
        return document
    except Exception:  # noqa: BLE001 - telemetry must never break a request
        return None
