"""Health endpoint for the ShopFlow foundation.

Deliberately narrow: it proves the Lambda runs, that its IAM role can reach the
ShopFlow DynamoDB table, and nothing else. No Bedrock, no data, no secrets.
"""

from __future__ import annotations

import json
import os
import time

import boto3
from botocore.exceptions import ClientError

TABLE_NAME = os.environ["TABLE_NAME"]
SERVICE_VERSION = os.environ.get("SERVICE_VERSION", "0.1.0")
STAGE = os.environ.get("STAGE", "prod")

_dynamodb = boto3.resource("dynamodb")
_table = _dynamodb.Table(TABLE_NAME)


def _check_table() -> dict:
    """Round-trip the table with a miss-by-design read.

    A GetItem for a key that will never exist is the cheapest call that still
    proves connectivity and IAM permission end to end.
    """
    started = time.perf_counter()
    try:
        _table.get_item(Key={"PK": "HEALTH#probe", "SK": "NONE"})
        return {
            "status": "ok",
            "latencyMs": round((time.perf_counter() - started) * 1000, 1),
        }
    except ClientError as exc:
        return {
            "status": "error",
            "code": exc.response["Error"]["Code"],
            "latencyMs": round((time.perf_counter() - started) * 1000, 1),
        }


def handler(event, context):
    table = _check_table()
    healthy = table["status"] == "ok"

    body = {
        "service": "shopflow-api",
        "status": "ok" if healthy else "degraded",
        "version": SERVICE_VERSION,
        "stage": STAGE,
        "region": os.environ.get("AWS_REGION", "unknown"),
        "checks": {"dynamodb": table},
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    return {
        "statusCode": 200 if healthy else 503,
        "headers": {
            "content-type": "application/json",
            # The foundation is public and static; never let a health result
            # be cached long enough to hide an outage.
            "cache-control": "no-store",
        },
        "body": json.dumps(body),
    }
