"""Live regression check for the P1 fixes, against the deployed application.

An independent evaluation found four P1 defects on the LIVE deployment. The
unit tests in tests/test_p1_live_regressions.py pin the fixes offline; this
sends the evaluator's exact inputs to the public URL and checks the answers:

   1  canonical order                 Rs 22,306.48
   2  two-line Anchor + Finolex       quoted, 2 and 3 ("and" and comma)
   3  minus 2 MCB                     a question, never a quotation
   4  less 3 Anchor                   a question, never a quotation
   5  conflicting supplier prices     CONFLICT, no comparison, no decision
   6  unreadable supplier rows        reported, with the exclusion sentence
   7  supplier alert                  the Finolex CRITICAL alert is shown
   8  daily brief                     present and grounded
   9  promise-keeping cost            commitments apart from restock
  10  What-If                         stateChanged false, figures unchanged
  11  anonymous owner route           401, isAuthentication false
  12  canonical GST                   2,007.58 + 2,007.58 = 4,015.16
  13  walk-away price                 Rs 5,947.20

    python scripts/smoke_test_p1_regressions.py [base-url]

Writes only what the application itself writes: order and price-list job
rows, which expire after 24 hours. The price list it uploads carries no
matched increase, so it cannot publish a price alert. No cost is confirmed.
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


def order(text):
    for attempt in range(4):
        status, body = call("POST", "/api/orders", {"orderText": text})
        if status not in (429, 503):
            break
        time.sleep(2 * (attempt + 1))
    if status != 202:
        return {"status": f"HTTP {status}"}
    return poll(body["jobId"])


def check(number, name, ok, evidence):
    results.append((number, name, bool(ok)))
    print(f"{'PASS' if ok else 'FAIL'}  {number:>2}. {name}: {evidence}")


def quoted(job):
    result = job.get("result") or {}
    quote = result.get("quote") or {}
    return result.get("status"), {l["skuId"]: l["quantity"]
                                  for l in quote.get("lines", [])}, quote


# 1 + 12: canonical order and GST ----------------------------------------------
job = order(CANONICAL)
status, lines, quote = quoted(job)
check(1, "canonical order", status == "QUOTED" and quote.get("total") == 22306.48
      and lines == {"SW-ANC-1W10A": 20, WIRE: 3, "MCB-HAV-SP-32A-C": 2},
      f"{status} {quote.get('total')} {lines}")
gst = quote.get("gst") or {}
check(12, "canonical GST", (gst.get("cgst"), gst.get("sgst"), gst.get("totalGst"),
                            gst.get("grandTotal")) ==
      (2007.58, 2007.58, 4015.16, 26321.64),
      f"CGST {gst.get('cgst')} SGST {gst.get('sgst')} GST {gst.get('totalGst')} "
      f"total {gst.get('grandTotal')}")

# 2: the two-line order, with "and" and with a comma -------------------------
for joiner in (" and ", ", "):
    text = ("2 Anchor modular switches 1-Way 10A White" + joiner +
            "3 coils Finolex 1.5 sq mm FR wire red 90m")
    status, lines, quote = quoted(order(text))
    check(2, f"two-line Anchor + Finolex ({joiner.strip() or 'comma'})",
          status == "QUOTED" and lines == {"SW-ANC-1W10A": 2, WIRE: 3}
          and quote.get("total") == 19980.6,
          f"{status} {lines} {quote.get('total')}")

# 3 + 4: signed quantities --------------------------------------------------
for number, text in ((3, "minus 2 Havells MCB SP 32A C-curve"),
                     (4, "less 3 Anchor modular switches 1-Way 10A White")):
    job = order(text)
    result = job.get("result") or {}
    question = (result.get("clarification") or {}).get("question", "")
    check(number, repr(text), result.get("status") == "NEEDS_CLARIFICATION"
          and not result.get("quote"),
          f"{result.get('status')} - {question[:110]}")

# 5 + 6: a price list that contradicts itself, with unreadable rows ------------
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "data"))
from PIL import Image, ImageDraw  # noqa: E402
from price_list_image import _font  # noqa: E402

rows = [("Havells MCB SP 32A C-Curve", "358.00"),
        ("Havells MCB SP 20A C-Curve", "336.00"),
        ("Havells MCB SP 32A C-Curve", "400.00"),
        ("Anchor Modular Switch Bell Push White", "N/A"),
        ("Anchor Modular Switch Socket 16A White", "-148.00")]
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
review_job = body.get("jobId")
review = ((poll(review_job, OWNER).get("result") or {}).get("review") or {})
conflicts = [l for l in review.get("lines", []) if l.get("status") == "CONFLICT"]
check(5, "conflicting supplier prices",
      len(conflicts) == 2 and all(l.get("comparison") is None for l in conflicts)
      and conflicts[0].get("conflictingPrices") == [358.0, 400.0]
      and review.get("materialChangeCount") == 0,
      f"{[l.get('status') for l in review.get('lines', [])]} "
      f"notice={review.get('conflictNotice')!r}")
status, body = call("POST", "/api/price-decisions", {
    "jobId": review_job, "skuId": "MCB-HAV-SP-32A-C", "decision": "CONFIRMED"},
    OWNER)
check(5, "a conflicting price cannot be confirmed", status == 409,
      f"HTTP {status} {body.get('error', '')[:80]}")
check(6, "unreadable supplier rows are reported",
      review.get("excludedCount") == 2 and review.get("exclusionNotice") ==
      "2 rows could not be interpreted and were excluded from price analysis.",
      f"{review.get('exclusionNotice')!r} "
      f"{[(r.get('description'), r.get('reason')) for r in review.get('excludedRows', [])]}")

# 7, 8, 9, 13: intelligence ---------------------------------------------------
status, intel = call("GET", "/api/intelligence", headers=OWNER)
alert = next((a for a in intel.get("alerts", []) if a.get("priceAlert")), {})
pa = alert.get("priceAlert") or {}
check(7, "supplier alert", pa.get("severity") == "CRITICAL" and
      (pa.get("oldCost"), pa.get("newCost"), pa.get("percentageDelta")) ==
      (5900.0, 6300.0, 6.78), f"{pa.get('severity')} {pa.get('oldCost')} -> "
      f"{pa.get('newCost')} ({pa.get('percentageDelta')}%)")
brief = intel.get("brief") or {}
planner = brief.get("planner") or {}
check(8, "daily brief", brief.get("grounded") is True and
      (planner.get("commitmentCost"), planner.get("restockCost"),
       planner.get("totalSpend"), planner.get("remaining")) ==
      (12948.0, 12045.16, 24993.16, 6.84),
      f"grounded={brief.get('grounded')} planner={planner}")
pk = brief.get("promiseKeeping") or {}
wire = next((c for c in pk.get("commitments", []) if c["skuId"] == WIRE), {})
extra = next((d for d in pk.get("discretionary", []) if d["skuId"] == WIRE), {})
check(9, "promise-keeping cost",
      (wire.get("quantity"), wire.get("cost"), wire.get("funded"),
       wire.get("aboveWalkAwayCost")) == (2, 12600.0, True, 705.6)
      and extra.get("decision") == "DO_NOT_BUY",
      f"{wire.get('text')} | {extra.get('text')}")
walk = next((w for w in brief.get("walkAway", []) if w["skuId"] == WIRE), {})
check(13, "walk-away price", (walk.get("walkAwayPrice"), walk.get("aboveBy")) ==
      (5947.2, 352.8), f"{walk.get('walkAwayPrice')} (above by {walk.get('aboveBy')})")

# 10: What-If writes nothing ---------------------------------------------------
before = (brief.get("planner"), [c.get("confirmedCost") for c in
                                 intel.get("alerts", []) if c.get("newCost")])
status, whatif = call("POST", "/api/shop-queries", {
    "kind": "WHAT_IF", "question": "What if Finolex wire price increases by ₹300?",
    "skuId": WIRE}, OWNER)
status2, after_intel = call("GET", "/api/intelligence", headers=OWNER)
after = ((after_intel.get("brief") or {}).get("planner"),
         [c.get("confirmedCost") for c in after_intel.get("alerts", [])
          if c.get("newCost")])
check(10, "What-If state unchanged", whatif.get("stateChanged") is False
      and before == after, f"stateChanged={whatif.get('stateChanged')} "
      f"planner unchanged={before == after}")

# 11: anonymous owner route --------------------------------------------------
status, body = call("GET", "/api/intelligence")
check(11, "anonymous owner route", status == 401 and
      body.get("isAuthentication") is False and "brief" not in body,
      f"HTTP {status} isAuthentication={body.get('isAuthentication')}")

failed = [r for r in results if not r[2]]
print(f"\n{len(results) - len(failed)}/{len(results)} checks passed")
sys.exit(1 if failed else 0)
