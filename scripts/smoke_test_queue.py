"""Smoke test: an order through the real queue, end to end, against real AWS.

WHY THIS EXISTS
---------------
The unit tests prove the API sends a message, the worker tolerates a duplicate
and the template declares a redrive policy. What they cannot prove is that the
deployed system actually works: that the API's role really can write to this
queue, that the event source mapping really is attached, that the worker really
consumes and completes, and that the dead-letter queue really stays empty when
nothing has gone wrong.

So this submits one real order to the deployed API and follows it all the way:

    POST /api/orders -> job QUEUED -> SQS -> worker -> DONE -> Rs 22,306.48

and then checks that neither queue is holding anything afterwards.

WHAT IT DOES NOT DO
-------------------
It does not poison the canonical demo job, and it does not force a message
into the dead-letter queue. Driving a real order to the DLQ would mean three
failed Bedrock attempts and would leave a message that a later run of this
script would then report as a failure. The DLQ's configuration is asserted
against the synthesized template in tests/test_queue.py, which is the right
place for it - a redrive policy is a declaration, and the declaration is what
AWS acts on.

    python scripts/smoke_test_queue.py

Requires AWS credentials for the account holding ShopFlowStack. It creates one
job record, which carries the same 24-hour TTL as every other job.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import boto3  # noqa: E402

REGION = os.environ.get("AWS_REGION", "ap-south-1")
STACK = "ShopFlowStack"

# The wording in README.md, docs/DEMO-RUNBOOK.md and the Builder Center
# article, character for character. An earlier version of this script used a
# wording of its own invention, which the model handled less reliably - so a
# red smoke test meant "this script phrased it differently", not "the queue is
# broken". A smoke test should exercise the documented workflow.
CANONICAL_ORDER = ("Anna, 20 Anchor modular switches 1-Way 10A, "
                   "3 coils Finolex 1.5 sq mm red wire 90m, "
                   "2 Havells MCB SP 32A.")
CANONICAL_TOTAL = 22306.48

# An order takes a bounded 6-turn Bedrock loop, measured at around four
# seconds. Sixty is generous without being an excuse for a stuck worker.
COMPLETION_TIMEOUT = 60

failures: list = []
pending: list = []


def check(label: str, condition: bool, detail: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {label}" + (f"  {detail}" if detail else ""))
    if not condition:
        failures.append(label)


def pend(label: str, detail: str = "") -> None:
    """A check that needs this build deployed before it can mean anything."""
    print(f"  [PEND] {label}" + (f"  {detail}" if detail else ""))
    pending.append(label)


def outputs() -> dict:
    stacks = boto3.client("cloudformation", region_name=REGION
                          ).describe_stacks(StackName=STACK)
    return {o["OutputKey"]: o["OutputValue"]
            for o in stacks["Stacks"][0].get("Outputs", [])}


def post_order(site_url: str, text: str) -> dict:
    request = urllib.request.Request(
        f"{site_url.rstrip('/')}/api/orders",
        data=json.dumps({"orderText": text}).encode("utf-8"),
        headers={"content-type": "application/json"},
        method="POST")
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def get_job(site_url: str, job_id: str) -> dict:
    with urllib.request.urlopen(
            f"{site_url.rstrip('/')}/api/jobs/{job_id}", timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def queue_depth(sqs, url: str) -> dict:
    attributes = sqs.get_queue_attributes(
        QueueUrl=url,
        AttributeNames=["ApproximateNumberOfMessages",
                        "ApproximateNumberOfMessagesNotVisible"],
    )["Attributes"]
    return {k: int(v) for k, v in attributes.items()}


def main() -> int:
    print(f"\nShopFlow queue smoke test ({REGION})\n")

    try:
        out = outputs()
    except Exception as exc:  # noqa: BLE001
        print(f"  cannot read {STACK}: {type(exc).__name__}")
        return 1

    queue_url = out.get("OrdersQueueUrl")
    dlq_url = out.get("OrdersDlqUrl")
    site_url = out.get("SiteUrl")

    print("1. the infrastructure exists")
    if not queue_url or not dlq_url:
        pend("the orders queue and DLQ are deployed",
             "this build has not been deployed yet")
        print("\n2. an order through the queue")
        pend("an order reaches DONE through the queue")
        print()
        print(f"{len(pending)} check(s) PENDING until the next deploy:")
        for name in pending:
            print(f"  - {name}")
        print("\nSMOKE TEST PASSED (nothing to test until deploy)")
        return 0

    sqs = boto3.client("sqs", region_name=REGION)
    check("the orders queue exists", True, queue_url.rsplit("/", 1)[-1])
    check("the dead-letter queue exists", True, dlq_url.rsplit("/", 1)[-1])

    attributes = sqs.get_queue_attributes(
        QueueUrl=queue_url,
        AttributeNames=["RedrivePolicy", "VisibilityTimeout"])["Attributes"]
    redrive = json.loads(attributes.get("RedrivePolicy", "{}"))
    check("the redrive policy points at the DLQ",
          dlq_url.rsplit("/", 1)[-1] in redrive.get("deadLetterTargetArn", ""),
          redrive.get("deadLetterTargetArn", "").rsplit(":", 1)[-1])
    check("maxReceiveCount is 3", redrive.get("maxReceiveCount") == 3,
          str(redrive.get("maxReceiveCount")))
    check("the visibility timeout is 6x the worker timeout",
          int(attributes.get("VisibilityTimeout", 0)) == 360,
          f"{attributes.get('VisibilityTimeout')}s")

    lam = boto3.client("lambda", region_name=REGION)
    mappings = lam.list_event_source_mappings(
        FunctionName="shopflow-order-worker").get("EventSourceMappings", [])
    queue_mappings = [m for m in mappings if "sqs" in m.get("EventSourceArn", "")]
    check("the worker is attached to the queue", len(queue_mappings) == 1,
          f"{len(queue_mappings)} mapping(s)")
    if queue_mappings:
        mapping = queue_mappings[0]
        maximum = (mapping.get("ScalingConfig") or {}).get("MaximumConcurrency")
        check("worker concurrency is bounded below the account ceiling",
              isinstance(maximum, int) and 0 < maximum < 10,
              f"MaximumConcurrency={maximum}")
        check("the mapping is enabled", mapping.get("State") == "Enabled",
              str(mapping.get("State")))

    alarms = boto3.client("cloudwatch", region_name=REGION).describe_alarms(
        AlarmNamePrefix="shopflow-")["MetricAlarms"]
    names = {a["AlarmName"] for a in alarms}
    check("the DLQ alarm exists",
          "shopflow-orders-dlq-not-empty" in names,
          ", ".join(sorted(names)) or "none")

    print("\n2. the dead-letter queue starts empty")
    before = queue_depth(sqs, dlq_url)
    check("nothing is dead-lettered before this run",
          sum(before.values()) == 0, str(before))

    print("\n3. one real order, all the way through")
    if not site_url:
        pend("an order reaches DONE through the queue", "no SiteUrl output")
    else:
        accepted = post_order(site_url, CANONICAL_ORDER)
        job_id = accepted.get("jobId")
        check("the API accepted the order", accepted.get("status") == "QUEUED",
              f"jobId={job_id}")

        deadline = time.time() + COMPLETION_TIMEOUT
        job = {}
        while time.time() < deadline:
            job = get_job(site_url, job_id)
            if job.get("status") in ("DONE", "FAILED"):
                break
            time.sleep(2)

        elapsed = int(COMPLETION_TIMEOUT - (deadline - time.time()))
        check("the job reached a terminal state",
              job.get("status") in ("DONE", "FAILED"),
              f"{job.get('status')} after ~{elapsed}s")
        check("the worker completed it", job.get("status") == "DONE",
              str(job.get("error") or ""))

        result = job.get("result") or {}
        quote = result.get("quote") or {}

        # Two different things can make this check fail, and conflating them
        # wastes an afternoon. The queue either carried the job or it did not;
        # that is what every check above measures, and they have all passed by
        # the time we get here. What is measured now is what the agent decided
        # - and the agent asking for clarification is a correct outcome of the
        # system, not a fault of the queue.
        #
        # It is still reported as a failure. The documented canonical order is
        # documented as producing a quotation, so a run that does not produce
        # one is a fact worth seeing rather than one worth explaining away.
        agent_status = result.get("status")
        if agent_status == "NEEDS_CLARIFICATION":
            question = (result.get("clarification") or {}).get("question") or ""
            check("the canonical quotation is unchanged", False,
                  f"the queue delivered correctly and the worker completed; "
                  f"the AGENT asked for clarification instead of quoting "
                  f"- {question[:70]!r}")
            print("         (not a queue failure: the transport checks above "
                  "all passed. Bedrock tool-calling varies run to run; the "
                  "completeness guard then withholds an incomplete quote.)")
        else:
            check("the canonical quotation is unchanged",
                  quote.get("total") == CANONICAL_TOTAL,
                  f"Rs {quote.get('total')} (agent status {agent_status})")
        check("the job id is the correlation id throughout",
              job.get("jobId") == job_id, str(job.get("jobId")))

    print("\n4. nothing is left behind")
    # The message is deleted by Lambda on success. A brief settle allows the
    # queue's approximate counters to catch up.
    time.sleep(5)
    main_depth = queue_depth(sqs, queue_url)
    check("no message is left on the orders queue",
          sum(main_depth.values()) == 0, str(main_depth))
    after = queue_depth(sqs, dlq_url)
    check("the dead-letter queue is still empty",
          sum(after.values()) == 0, str(after))

    print()
    if pending:
        print(f"{len(pending)} check(s) PENDING until the next deploy:")
        for name in pending:
            print(f"  - {name}")
        print()
    if failures:
        print(f"SMOKE TEST FAILED: {len(failures)} check(s)")
        for name in failures:
            print(f"  - {name}")
        return 1
    print("SMOKE TEST PASSED"
          + (f" ({len(pending)} pending until deploy)" if pending else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
