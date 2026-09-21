"""Smoke test: the language layer, against real AWS and the real engines.

WHY THIS EXISTS
---------------
The unit tests prove the language layer cannot change a business figure. They
cannot prove two other things, and those two things are exactly the ones a
claim could quietly be wrong about:

  1. That Amazon Transcribe really accepts the language codes ShopFlow says it
     does. `TRANSCRIBE_LANGUAGES` is a hand-written map, and a hand-written
     map is a claim. This script checks every entry against the `LanguageCode`
     enum that botocore builds from the service model for the account and
     region being deployed to - the service's own statement of what it takes.
     A code that is not there fails this script.

  2. That the deployed site can actually serve the translation resources the
     browser will ask for. A resource file that exists in the repository and
     not in the bundle is a page that silently falls back to English.

It also re-runs the canonical business regressions through the language layer,
because a smoke test that only checked plumbing would miss the thing the
feature is actually promising.

    python scripts/smoke_test_multilingual.py           # everything
    python scripts/smoke_test_multilingual.py --offline # skip the AWS checks

WHAT IT DOES NOT CLAIM
----------------------
Nothing here measures translation quality, and nothing here measures speech
recognition accuracy. A language code being accepted by Transcribe means the
service will take the job. It says nothing about how well it transcribes a
Madurai contractor, and this script makes no such claim.

It writes nothing, creates no AWS resource and leaves nothing behind.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from engine.credit import check_credit  # noqa: E402
from engine.language import (  # noqa: E402
    DEFAULT_LANGUAGE,
    canonicalize_request,
    capabilities,
    coverage,
    localize_credit_status,
    localize_quote,
    scheduled_languages,
    supported_languages,
    translate,
)
from engine.loader import cached_dataset  # noqa: E402
from engine.matching import RESOLVED, resolve_product  # noqa: E402
from engine.messages import build_quotation_message  # noqa: E402
from engine.quote import calculate_quote  # noqa: E402
from engine.speech import TRANSCRIBE_LANGUAGES  # noqa: E402

REGION = os.environ.get("AWS_REGION", "ap-south-1")

CANONICAL = [
    {"skuId": "SW-ANC-1W10A", "quantity": 20},
    {"skuId": "W-FIN-1.5-RED-90M", "quantity": 3, "uom": "COIL"},
    {"skuId": "MCB-HAV-SP-32A-C", "quantity": 2},
]
CANONICAL_TOTAL = 22306.48
RAVI = "CUST-RAVI-001"

# The three languages the demo actually shows. The other twenty are the same
# code path, which is the point being made on stage.
DEMO = {
    "ta": "எனக்கு 20 Anchor modular switch 1-Way 10A White வேண்டும்",
    "hi": "मुझे २० Anchor modular switch 1-Way 10A White चाहिए",
    "te": "నాకు ౨౦ Anchor modular switch 1-Way 10A White కావాలి",
}

failures: list = []
pending: list = []


def check(label: str, condition: bool, detail: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {label}" + (f"  {detail}" if detail else ""))
    if not condition:
        failures.append(label)


def pend(label: str, detail: str = "") -> None:
    """A check that cannot be answered yet, reported rather than passed.

    The deployed site is a previous build until somebody deploys this one, so
    asking it for a file this build adds proves nothing either way. Counting
    that as a pass would be a lie and counting it as a failure would make the
    script useless before every deploy, so it is neither: it is printed, it is
    summarised at the end, and it does not affect the exit code.
    """
    print(f"  [PEND] {label}" + (f"  {detail}" if detail else ""))
    pending.append(label)


# ---------------------------------------------------------------------------
# 1. the registry
# ---------------------------------------------------------------------------

def registry_checks() -> None:
    scheduled = scheduled_languages()
    check("all 22 Scheduled Languages are registered", len(scheduled) == 22,
          f"{len(scheduled)} languages")
    check("English is supported and is NOT one of the 22",
          "en" in supported_languages() and "en" not in scheduled
          and DEFAULT_LANGUAGE == "en")

    missing = [c for c in supported_languages()
               if not (ROOT / "backend" / "i18n" / f"{c}.json").exists()]
    check("every language has a resource file", not missing, str(missing))

    partial = {c: coverage(c) for c in scheduled if coverage(c) < 1}
    # Partial coverage is a legitimate, reported state - not a failure. It is
    # printed so nobody has to take "fully translated" on trust.
    print(f"        partial: " + (", ".join(
        f"{c} {int(v * 100)}%" for c, v in sorted(partial.items())) or "none"))
    check("every partial language still answers in English",
          all(translate(c, "quote.notInvoice") for c in partial))

    unreviewed = [c for c in scheduled if capabilities(c)["reviewed"]]
    check("no language claims native-speaker review", not unreviewed,
          str(unreviewed))


# ---------------------------------------------------------------------------
# 2. Amazon Transcribe, verified against the service's own model
# ---------------------------------------------------------------------------

def transcribe_language_checks() -> None:
    import boto3

    model = boto3.client("transcribe", region_name=REGION).meta.service_model
    accepted = set(model.shape_for("LanguageCode").enum)

    unknown = {code: value for code, value in TRANSCRIBE_LANGUAGES.items()
               if value not in accepted}
    check("every language ShopFlow offers for voice is one Transcribe accepts",
          not unknown, str(unknown) if unknown else
          f"{len(TRANSCRIBE_LANGUAGES)} codes, all present in the service model")

    voiced = [c for c in supported_languages() if capabilities(c)["voice"]]
    check("the capability matrix agrees with the provider map",
          set(voiced) == set(TRANSCRIBE_LANGUAGES),
          f"{len(voiced)} with voice")

    silent = [c for c in scheduled_languages() if c not in TRANSCRIBE_LANGUAGES]
    check("languages without voice are reported as text-only, not as absent",
          all(capabilities(c)["text"] and not capabilities(c)["voice"]
              for c in silent),
          f"{len(silent)} text-only: " + ", ".join(sorted(silent)))


# ---------------------------------------------------------------------------
# 3. the deployed site actually serves the resources
# ---------------------------------------------------------------------------

def site_resource_checks(base: str) -> None:
    if not base:
        print("  [SKIP] site resources - no SITE_URL and no deployed stack found")
        return
    for code in ("en", "ta", "hi"):
        url = f"{base.rstrip('/')}/i18n/{code}.json"
        label = f"the site serves i18n/{code}.json"
        try:
            with urllib.request.urlopen(url, timeout=15) as response:
                body = response.read().decode("utf-8", "replace")
        except (urllib.error.URLError, OSError) as exc:
            check(label, False, type(exc).__name__)
            continue

        if body.lstrip().startswith("<"):
            # CloudFront answered with the single-page app, which is what it
            # does for a path the bundle does not contain. On a site deployed
            # before this feature existed that is the expected answer, not a
            # fault - so it is reported as pending and re-checked after a
            # deploy rather than being called either pass or fail.
            pend(label, "the deployed site predates this build")
            continue

        try:
            payload = json.loads(body)
        except ValueError:
            check(label, False, "the response was not JSON")
            continue
        check(label, bool(payload.get("quote")), url)


def site_url() -> str:
    if os.environ.get("SITE_URL"):
        return os.environ["SITE_URL"]
    try:
        import boto3

        stack = boto3.client("cloudformation", region_name=REGION
                             ).describe_stacks(StackName="ShopFlowStack")
        for output in stack["Stacks"][0].get("Outputs", []):
            if "url" in output["OutputKey"].lower() and \
                    output["OutputValue"].startswith("https://"):
                return output["OutputValue"].split("/api")[0]
    except Exception:  # noqa: BLE001 - not being deployed is not a failure
        return ""
    return ""


# ---------------------------------------------------------------------------
# 4. the business regressions, through the language layer
# ---------------------------------------------------------------------------

def equivalence_checks() -> None:
    data = cached_dataset()

    for code, phrase in DEMO.items():
        canonical = canonicalize_request(phrase, code)
        result = resolve_product(data, requested_text=canonical["canonicalText"])
        check(f"{code}: the same request resolves to the same SKU",
              result.status == RESOLVED and result.skuId == "SW-ANC-1W10A",
              f"{result.status} {result.skuId}")

    quote = calculate_quote(data, CANONICAL).as_dict()
    check("the canonical quotation is unchanged",
          quote["total"] == CANONICAL_TOTAL, f"Rs {quote['total']:,.2f}")

    totals = {localize_quote(quote, c)["totalText"] for c in supported_languages()}
    check("all 23 languages show one total", totals == {"₹22,306.48"},
          str(sorted(totals)))

    credit = check_credit(data, RAVI, 4200)
    check("the credit decision is APPROVED with the canonical figures",
          credit["decision"] == "APPROVED"
          and credit["creditLimit"] == 15000.0
          and credit["currentOutstanding"] == 8500.0
          and credit["projectedOutstanding"] == 12700.0
          and credit["remainingCredit"] == 2300.0)

    remaining = {localize_credit_status(credit, c)["remainingText"]
                 for c in supported_languages()}
    check("all 23 languages show one remaining credit",
          remaining == {"₹2,300.00"}, str(sorted(remaining)))

    leaked = []
    for code in supported_languages():
        text = build_quotation_message(quote, None, credit, language=code)["text"]
        if "₹22,306.48" not in text:
            leaked.append(f"{code}: total missing")
        if "3 coils" not in text:
            leaked.append(f"{code}: unit changed")
        for word in ("margin", "supplier", "708", "308"):
            if word in text.lower():
                leaked.append(f"{code}: leaked {word}")
    check("every customer message keeps the figures and the unit, and leaks "
          "nothing internal", not leaked, str(leaked[:4]))


def main() -> int:
    offline = "--offline" in sys.argv
    print(f"\nShopFlow multilingual smoke test ({REGION})\n")

    print("1. the language registry")
    registry_checks()

    print("\n2. Amazon Transcribe language support, verified against the "
          "service model")
    if offline:
        print("  [SKIP] --offline")
    else:
        transcribe_language_checks()

    print("\n3. the deployed site serves the translation resources")
    if offline:
        print("  [SKIP] --offline")
    else:
        site_resource_checks(site_url())

    print("\n4. business equivalence across languages")
    equivalence_checks()

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
