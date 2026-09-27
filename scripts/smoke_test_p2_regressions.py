"""Live regression check for the P2 fixes, against the deployed application.

   1-3  +4.99% / +5.00% / +5.01% supplier prices: the review's "material"
        verdict matches the one threshold (>= 5%)
   4    conflicting supplier price rows: CONFLICT, no comparison
   5    "increases by -Rs 500": a clarification, nothing simulated
   6    "decreases by Rs 500": simulated, supplier cost Rs 5,800
   7-9  budget = commitments, commitments - Rs 0.01, commitments + Rs 0.01
   10   "Hels" heard for Havells: corrected on the voice route, shown
   15   canonical order Rs 22,306.48
   16   canonical GST
   17   purchase plan at Rs 25,000
   18   What-If changes nothing

The page checks (navigation on the AI assistant, no model id on the quote)
and the AWS checks (owner-alert rule, alarm topic) are run separately - the
first needs a browser, the second AWS credentials.

    python scripts/smoke_test_p2_regressions.py [base-url]

Writes only what the application itself writes: job rows (24-hour TTL). The
price list it uploads carries two small price rises, which the worker judges
and may publish as supplier price alerts to the owner topic (de-duplicated per
price move, 30 days). No cost is confirmed.
"""

from __future__ import annotations

import base64
import io
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = (sys.argv[1] if len(sys.argv) > 1
        else "https://d3m3lwn03zb2eu.cloudfront.net").rstrip("/")
OWNER = {"x-shopflow-demo-owner": "demo-workspace"}
CANONICAL = ("20 Anchor modular switches 1-Way 10A White, 3 coils Finolex 1.5 "
             "sq mm FR wire red 90m, 2 Havells MCB SP 32A C-curve")
WIRE = "W-FIN-1.5-RED-90M"
COMMITMENTS = 12948.0

results = []


def call(method, path, body=None, headers=None, timeout=60):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"content-type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, json.loads(response.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        text = exc.read().decode()
        return exc.code, json.loads(text) if text.startswith("{") else {}


def poll(job_id, headers=None, limit=300):
    started = time.time()
    while time.time() - started < limit:
        status, job = call("GET", f"/api/jobs/{job_id}", headers=headers)
        if status == 200 and job.get("status") in ("DONE", "FAILED"):
            return job
        time.sleep(2)
    return {"status": "TIMEOUT"}


def check(number, name, ok, evidence):
    results.append((number, name, bool(ok)))
    print(f"{'PASS' if ok else 'FAIL'}  {number:>2}. {name}: {evidence}")


def whatif(question):
    return call("POST", "/api/shop-queries", {
        "kind": "WHAT_IF", "question": question, "skuId": WIRE}, OWNER)[1]


# 1-4: one price list through the live Textract path --------------------------
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "data"))
from PIL import Image, ImageDraw  # noqa: E402
from price_list_image import _font  # noqa: E402

rows = [("Anchor Modular Switch 1-Way 16A White", "132.29"),   # 126 -> +4.99%
        ("Anchor Modular Switch 2-Way 10A White", "96.60"),    # 92 -> +5.00%
        ("Havells MCB SP 16A C-Curve", "333.93"),              # 318 -> +5.01%
        ("Havells MCB SP 32A C-Curve", "358.00"),              # conflict
        ("Havells MCB SP 32A C-Curve", "400.00")]              # conflict
image = Image.new("RGB", (1000, 172 + 42 * len(rows) + 60), (253, 252, 249))
draw = ImageDraw.Draw(image)
draw.text((40, 30), "SRI BALAJI ELECTRICALS", fill=(15, 15, 15), font=_font(26, True))
draw.text((40, 70), "Madurai   |   Effective 27-09-2026", fill=(90, 90, 90), font=_font(15))
draw.text((45, 132), "DESCRIPTION", fill=(40, 40, 40), font=_font(15, True))
draw.text((800, 132), "RATE (Rs)", fill=(40, 40, 40), font=_font(15, True))
for index, (name, rate) in enumerate(rows):
    draw.text((45, 172 + 42 * index), name, fill=(25, 25, 25), font=_font(17))
    draw.text((800, 172 + 42 * index), rate, fill=(25, 25, 25), font=_font(17))
buffer = io.BytesIO()
image.save(buffer, "PNG")
status, body = call("POST", "/api/supplier-price-lists", {
    "contentType": "image/png",
    "imageBase64": base64.b64encode(buffer.getvalue()).decode()}, OWNER)
review = ((poll(body.get("jobId"), OWNER).get("result") or {}).get("review") or {})
by_sku = {}
for line in review.get("lines", []):
    by_sku.setdefault(line.get("skuId"), []).append(line)
for number, sku, pct, material in ((1, "SW-ANC-1W16A", 4.99, False),
                                   (2, "SW-ANC-2W10A", 5.0, True),
                                   (3, "MCB-HAV-SP-16A-C", 5.01, True)):
    comparison = ((by_sku.get(sku) or [{}])[0].get("comparison") or {})
    check(number, f"+{pct:.2f}% supplier price",
          comparison.get("percentageDelta") == pct
          and comparison.get("materialChange") is material
          and ">=" in (comparison.get("evidence") or {}).get("threshold", ""),
          f"{sku} {comparison.get('previousPrice')} -> "
          f"{comparison.get('currentPrice')} ({comparison.get('percentageDelta')}%) "
          f"material={comparison.get('materialChange')} "
          f"rule={(comparison.get('evidence') or {}).get('threshold')!r}")
conflicts = by_sku.get("MCB-HAV-SP-32A-C") or []
check(4, "conflicting supplier price rows",
      [l.get("status") for l in conflicts] == ["CONFLICT", "CONFLICT"]
      and all(l.get("comparison") is None for l in conflicts),
      f"{[l.get('status') for l in conflicts]} {review.get('conflictNotice')!r}")

# 5-6, 18: What-If --------------------------------------------------------
negative = whatif("What if Finolex wire price increases by -₹500?")
check(5, "increases by -₹500", negative.get("status") == "NEEDS_CLARIFICATION"
      and negative.get("stateChanged") is False and "scenario" not in negative,
      f"{negative.get('status')} - {negative.get('explanation')}")
decrease = whatif("What if supplier cost decreases by ₹500?")
check(6, "decreases by ₹500", decrease.get("status") == "SIMULATED"
      and (decrease.get("scenario") or {}).get("supplierCost") == 5800.0,
      f"{decrease.get('status')} - {decrease.get('explanation')}")

# 7-9, 17: the planner ------------------------------------------------------
for number, budget in ((7, COMMITMENTS), (8, COMMITMENTS - 0.01),
                       (9, COMMITMENTS + 0.01)):
    status, plan = call("POST", "/api/purchase-plans", {"budget": budget}, OWNER)
    short = budget < COMMITMENTS
    ok = (status == 200 and plan.get("allCommitmentsFunded") is (not short)
          and plan.get("restockCost") == 0.0
          and round(plan.get("totalSpend", 0) + plan.get("remaining", 0), 2)
          == round(budget, 2))
    check(number, f"budget ₹{budget:,.2f}", ok,
          f"commitments {plan.get('commitmentCost')} funded="
          f"{plan.get('allCommitmentsFunded')} restock {plan.get('restockCost')} "
          f"remaining {plan.get('remaining')}")
status, plan = call("POST", "/api/purchase-plans", {"budget": 25000}, OWNER)
check(17, "purchase plan ₹25,000",
      (plan.get("commitmentCost"), plan.get("restockCost"),
       plan.get("totalSpend"), plan.get("remaining")) ==
      (12948.0, 12045.16, 24993.16, 6.84),
      f"{plan.get('commitmentCost')} + {plan.get('restockCost')} = "
      f"{plan.get('totalSpend')}, {plan.get('remaining')} left")

status, before = call("GET", "/api/intelligence", headers=OWNER)
changed = whatif("What if Finolex wire price increases by ₹300?")
status, after = call("GET", "/api/intelligence", headers=OWNER)
check(18, "What-If changes nothing", changed.get("stateChanged") is False
      and (before.get("brief") or {}).get("planner") ==
      (after.get("brief") or {}).get("planner"),
      f"stateChanged={changed.get('stateChanged')}")

# 10: voice brand correction -------------------------------------------------
status, spoken = call("POST", "/api/shop-queries", {
    "transcript": "2 Hels MCB SP 32 amp C curve venum"}, OWNER)
check(10, "'Hels' heard for Havells", spoken.get("delegateTo") == "ORDER"
      and "Havells" in (spoken.get("normalizedTranscript") or "")
      and "Hels -> Havells" in (spoken.get("aliasesApplied") or []),
      f"{spoken.get('normalizedTranscript')!r} {spoken.get('aliasesApplied')}")

# 15-16: the canonical order ------------------------------------------------
for attempt in range(4):
    status, body = call("POST", "/api/orders", {"orderText": CANONICAL})
    if status not in (429, 503):
        break
    time.sleep(2 * (attempt + 1))
job = poll(body.get("jobId")) if status == 202 else {}
quote = ((job.get("result") or {}).get("quote") or {})
check(15, "canonical order", quote.get("total") == 22306.48,
      f"{(job.get('result') or {}).get('status')} {quote.get('total')}")
gst = quote.get("gst") or {}
check(16, "canonical GST", (gst.get("cgst"), gst.get("sgst"), gst.get("totalGst"),
                            gst.get("grandTotal")) ==
      (2007.58, 2007.58, 4015.16, 26321.64),
      f"{gst.get('cgst')} + {gst.get('sgst')} = {gst.get('totalGst')}; "
      f"{gst.get('grandTotal')}")

failed = [r for r in results if not r[2]]
print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
