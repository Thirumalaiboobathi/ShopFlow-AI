"""Shared plumbing for the live checks in scripts/live/.

Every check calls the DEPLOYED application and judges the answer. Nothing
here stubs, replays or records a response, so a result means what it says.

    SHOPFLOW_BASE_URL   the site (default: the public CloudFront URL)
    SHOPFLOW_FORCE_IPV4 "1" to resolve IPv4 only (some networks stall on IPv6)

Exit status is 0 only when every check passed. The last line printed is one
JSON object - script, base URL, UTC start and end, passed, failed - so a run
can be pasted into docs/evidence as it is.

What a run writes: the application's own job rows (an order, a counter-offer
draft, a supplier-reply reading), which expire after 24 hours under the
table's TTL. No supplier cost is confirmed and no permanent record is changed.
"""

from __future__ import annotations

import json
import os
import socket
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

if os.environ.get("SHOPFLOW_FORCE_IPV4") == "1":
    _resolve = socket.getaddrinfo
    socket.getaddrinfo = lambda *a, **k: [
        x for x in _resolve(*a, **k) if x[0] == socket.AF_INET]

BASE = (os.environ.get("SHOPFLOW_BASE_URL")
        or "https://d3m3lwn03zb2eu.cloudfront.net").rstrip("/")
# The demo workspace gate. Not a secret and not authentication - it is in the
# page source; see README "Who may see what".
OWNER = {"x-shopflow-demo-owner": "demo-workspace"}

CANONICAL = ("20 Anchor modular switches 1-Way 10A White, 3 coils Finolex 1.5 "
             "sq mm FR wire red 90m, 2 Havells MCB SP 32A C-curve")
CANONICAL_LINES = {("SW-ANC-1W10A", 20), ("W-FIN-1.5-RED-90M", 3),
                   ("MCB-HAV-SP-32A-C", 2)}
SUBTOTAL, GST_TOTAL, GRAND_TOTAL = 22306.48, 4015.16, 26321.64


def call(method: str, path: str, body=None, headers=None, timeout: int = 60):
    """(status, parsed JSON or {}). Retries a throttled submit, never a 4xx."""
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"content-type": "application/json", **(headers or {})})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                text = response.read().decode() or "{}"
                return response.status, _json(text)
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 503) and attempt < 3:
                time.sleep(3 * (attempt + 1))
                continue
            return exc.code, _json(exc.read().decode())
    return 0, {}


def _json(text: str):
    try:
        return json.loads(text)
    except ValueError:
        return {}


def poll(job_id, headers=None, limit: int = 240) -> dict:
    """The finished job, or {"status": "TIMEOUT"}."""
    started = time.time()
    while job_id and time.time() - started < limit:
        _s, job = call("GET", f"/api/jobs/{job_id}", headers=headers)
        if job.get("status") in ("DONE", "FAILED"):
            return job
        time.sleep(2)
    return {"status": "TIMEOUT"}


class Checks:
    def __init__(self, script: str):
        self.script, self.results = script, []
        self.started = datetime.now(timezone.utc).isoformat(timespec="seconds")

    def check(self, name: str, ok: bool, detail="") -> bool:
        self.results.append((name, bool(ok)))
        print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}", flush=True)
        return bool(ok)

    def finish(self) -> None:
        failed = [n for n, ok in self.results if not ok]
        print(json.dumps({
            "script": self.script, "base": BASE, "startedAt": self.started,
            "finishedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "passed": len(self.results) - len(failed), "failed": len(failed),
            "failedChecks": failed}, ensure_ascii=False))
        sys.exit(1 if failed else 0)
