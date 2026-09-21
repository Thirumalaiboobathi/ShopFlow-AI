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

_UNITS: Dict[str, str] = {
    ORDERS_QUEUED: "Count",
    ORDERS_COMPLETED: "Count",
    ORDERS_FAILED: "Count",
    WORKER_PROCESSING_SECONDS: "Seconds",
    WORKER_FAILURES: "Count",
    DUPLICATE_DELIVERIES: "Count",
    QUEUE_SEND_FAILURES: "Count",
}

# Deliberately two, and deliberately low-cardinality. Every distinct dimension
# VALUE creates a separate billed metric, so a dimension carrying a job id
# would create one metric per order. `jobId` belongs in the log line beside
# the metric - searchable, free, and not retained as a metric forever.
_SAFE_DIMENSIONS = ("JobType", "Outcome")

# Job types and outcomes are closed sets. Anything else is dropped, which
# bounds cardinality by construction rather than by care.
_ALLOWED_VALUES = {
    "JobType": {"ORDER", "PRICE_LIST", "TRANSCRIPT", "UNKNOWN"},
    "Outcome": {"QUOTED", "NEEDS_CLARIFICATION", "NOT_FOUND", "REVIEWED",
                "FAILED", "DUPLICATE", "RETRYABLE", "TERMINAL", "UNKNOWN"},
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
        document = {
            "_aws": {
                "Timestamp": int(time.time() * 1000),
                "CloudWatchMetrics": [{
                    "Namespace": NAMESPACE,
                    # One dimension SET. CloudWatch bills per unique
                    # combination, so this stays as a single grouping.
                    "Dimensions": [list(clean)] if clean else [[]],
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
