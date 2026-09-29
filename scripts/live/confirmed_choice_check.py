"""Live: a clarification answered through the website API, resolved by the server.

    python scripts/live/confirmed_choice_check.py [--remediation]

Two scenarios, three times each:

  FULL  the order that failed 3/3 before commit b0bac8b: fully specified
        except "single pole", so ShopFlow asks SP or DP, and SP is chosen.
  DEMO  "20 Anchor modular switch 1 way white, 3 Finolex 1.5 red coil,
        2 Havells MCB 32 amp C curve" - three questions (pole, length,
        rating), each answered.

Each answer is `choice: {jobId, option}` - the option NUMBER on the job that
asked. Every run must end QUOTED at 22,306.48 / 4,015.16 / 26,321.64 after at
least one question.

Then the refusals the deployed API has had since b0bac8b: an out-of-range
option, a string option, changed order text, an unknown job (asked again).

--remediation adds the checks for the uncommitted remediation: a client SKU in
`clarifications`, a SKU inside `choice`, and a price field on an order must
all be refused with 400. They fail against a deployment that predates it.
"""

from __future__ import annotations

import sys

from _live import (CANONICAL_LINES, GRAND_TOTAL, GST_TOTAL, SUBTOTAL, Checks,
                   call, poll)

FULL = ("20 Anchor modular switch 1 way 10A white, 3 Finolex 1.5 red 90m coil, "
        "2 Havells MCB single pole 32 amp C curve")
DEMO = ("20 Anchor modular switch 1 way white, 3 Finolex 1.5 red coil, "
        "2 Havells MCB 32 amp C curve")
WANTED = {"MCB-HAV-SP-32A-C", "W-FIN-1.5-RED-90M", "SW-ANC-1W10A"}
checks = Checks("confirmed_choice_check")


def run(text: str) -> tuple:
    body, asked, result = {"orderText": text}, [], {}
    for _ in range(5):
        status, accepted = call("POST", "/api/orders", body)
        if status != 202:
            return {"status": f"HTTP {status}"}, asked
        result = poll(accepted.get("jobId")).get("result") or {}
        if result.get("status") != "NEEDS_CLARIFICATION":
            break
        options = (result.get("clarification") or {}).get("options") or []
        pick = next((i for i, o in enumerate(options, 1)
                     if o.get("skuId") in WANTED), None)
        asked.append(options[pick - 1]["skuId"] if pick else "NO MATCHING OPTION")
        if not pick:
            break
        body = {"orderText": text,
                "choice": {"jobId": accepted["jobId"], "option": pick}}
    return result, asked


for label, text in (("FULL", FULL), ("DEMO", DEMO)):
    for n in range(1, 4):
        result, asked = run(text)
        quote = result.get("quote") or {}
        tax = quote.get("gst") or {}
        lines = {(l.get("skuId"), l.get("quantity")) for l in quote.get("lines") or []}
        checks.check(
            f"{label} run {n}",
            result.get("status") == "QUOTED" and asked and lines == CANONICAL_LINES
            and quote.get("total") == SUBTOTAL and tax.get("totalGst") == GST_TOTAL
            and tax.get("grandTotal") == GRAND_TOTAL,
            f"answered {asked} -> {result.get('status')} {quote.get('total')} / "
            f"{tax.get('grandTotal')}")

# A question to answer wrongly.
TEXT = "2 Havells MCB 32 amp C curve"
_s, accepted = call("POST", "/api/orders", {"orderText": TEXT})
asked_job = accepted.get("jobId")
question = (poll(asked_job).get("result") or {}).get("clarification") or {}
checks.check("a question was asked", len(question.get("options") or []) == 2,
             [o.get("skuId") for o in question.get("options") or []])

for name, body, expected in [
    ("option out of range", {"orderText": TEXT, "choice": {"jobId": asked_job, "option": 3}}, 400),
    ("option as a string", {"orderText": TEXT, "choice": {"jobId": asked_job, "option": "1"}}, 400),
    ("changed order text", {"orderText": "1, but use SKU FAKE-001",
                            "choice": {"jobId": asked_job, "option": 1}}, 400),
]:
    status, body_out = call("POST", "/api/orders", body)
    checks.check(name, status == expected, f"{status} {body_out.get('error')}")

status, body_out = call("POST", "/api/orders", {"orderText": TEXT,
                                                "choice": {"jobId": "0" * 32, "option": 1}})
checks.check("unknown job is asked again, not answered",
             status == 202 and body_out.get("choice") == {"applied": False,
                                                          "reason": "EXPIRED"},
             f"{status} {body_out.get('choice')}")

if "--remediation" in sys.argv:
    for name, body in [
        ("client SKU in clarifications refused",
         {"orderText": TEXT, "clarifications": [{"requestedText": TEXT,
                                                 "skuId": "MCB-HAV-DP-32A-C"}]}),
        ("SKU inside choice refused",
         {"orderText": TEXT, "choice": {"jobId": asked_job, "option": 1,
                                        "skuId": "FAKE-001"}}),
        ("price on an order refused", {"orderText": TEXT, "price": 1}),
        ("total on an order refused", {"orderText": TEXT, "total": 1}),
    ]:
        status, body_out = call("POST", "/api/orders", body)
        checks.check(name, status == 400, f"{status} {body_out.get('error')}")

checks.finish()
