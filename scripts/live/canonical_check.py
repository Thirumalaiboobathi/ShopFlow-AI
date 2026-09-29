"""Live: the canonical order, N independent times (default 5).

    python scripts/live/canonical_check.py [runs]

Each run must quote exactly 20 x SW-ANC-1W10A, 3 x W-FIN-1.5-RED-90M and
2 x MCB-HAV-SP-32A-C, with subtotal 22,306.48, GST 4,015.16 and total
26,321.64, and the stock shortages 6 / 2 / 0. A clarification counts as a
failure here: this order is fully specified.
"""

from __future__ import annotations

import sys

from _live import (CANONICAL, CANONICAL_LINES, GRAND_TOTAL, GST_TOTAL,
                   SUBTOTAL, Checks, call, poll)

RUNS = int(sys.argv[1]) if len(sys.argv) > 1 else 5
checks = Checks("canonical_check")

for run in range(1, RUNS + 1):
    status, accepted = call("POST", "/api/orders", {"orderText": CANONICAL})
    job = poll(accepted.get("jobId"))
    result = job.get("result") or {}
    quote = result.get("quote") or {}
    tax = quote.get("gst") or {}
    lines = {(l.get("skuId"), l.get("quantity")) for l in quote.get("lines") or []}
    stock = {l.get("skuId"): l.get("inStock") for l in quote.get("lines") or []}
    checks.check(
        f"run {run}",
        status == 202 and result.get("status") == "QUOTED"
        and lines == CANONICAL_LINES and quote.get("total") == SUBTOTAL
        and tax.get("totalGst") == GST_TOTAL and tax.get("grandTotal") == GRAND_TOTAL
        and stock == {"SW-ANC-1W10A": False, "W-FIN-1.5-RED-90M": False,
                      "MCB-HAV-SP-32A-C": True},
        f"{result.get('status')} total={quote.get('total')} gst={tax.get('totalGst')} "
        f"grand={tax.get('grandTotal')}")

checks.finish()
