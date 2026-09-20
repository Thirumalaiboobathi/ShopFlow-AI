"""Order-processing worker.

Invoked asynchronously by the API. Runs the bounded agent loop and writes the
result back onto the job record for the browser to poll.
"""

from __future__ import annotations

import json
import os
import time

import boto3

from agent.orchestrator import (
    DEFAULT_MODEL_ID,
    OrderTooLongError,
    run_order_agent,
)
from engine.loader import cached_dataset

TABLE_NAME = os.environ["TABLE_NAME"]
MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", DEFAULT_MODEL_ID)

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


def handler(event, context):
    job_id = event.get("jobId")
    if not job_id:
        print("ERROR: no jobId in event")
        return {"ok": False}

    item = _table.get_item(Key=_job_key(job_id)).get("Item")
    if not item:
        print(f"ERROR: job {job_id} not found")
        return {"ok": False}

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

    _update(
        job_id,
        status="DONE" if result.status != "FAILED" else "FAILED",
        result=json.dumps(result.as_dict()),
        finishedAt=int(time.time()),
    )
    return {"ok": True, "status": result.status}
