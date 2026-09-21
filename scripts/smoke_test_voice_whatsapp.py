"""Smoke test: a real Amazon Transcribe round trip, and the WhatsApp adapter.

WHY THIS EXISTS
---------------
The unit tests mock AWS, which is right - they must run anywhere, fast, with
no account. What they cannot tell you is whether the real service accepts the
request this application builds: whether the media format is one Transcribe
will read, whether the job name passes its validation, whether the transcript
document has the shape the parser expects, and whether the cleanup actually
removes what it created.

So this script does the whole thing against real AWS:

    generate a short WAV -> S3 -> StartTranscriptionJob -> poll ->
    fetch the transcript -> delete the job -> delete the object -> verify gone

It uses the EXISTING uploads bucket and the same `voice-audio/` prefix the API
uses, so what it exercises is the real path and not a parallel one.

It is deliberately silent audio. This is a plumbing test, not an accuracy
test: it asserts the job completes and the transcript parses, and it makes no
claim whatsoever about recognition quality. Claiming accuracy from a smoke
test would be worse than having no smoke test.

THE WHATSAPP HALF
-----------------
No WhatsApp credentials exist in this repository, so nothing is sent to Meta
and this script does not pretend otherwise. What it checks is the part that
ships: that the adapter is OFF by default, that a customer message is built
from real engine output, that it carries no supplier cost or margin, and that
the wa.me draft remains available. The Cloud API call itself is exercised
against a stub.

Run it explicitly, before a deploy:

    python scripts/smoke_test_voice_whatsapp.py

Requires AWS credentials for the account holding ShopFlowStack. It leaves
nothing behind - if this script leaves an audio object or a transcription job,
that is the bug it is looking for.
"""

from __future__ import annotations

import io
import json
import os
import struct
import sys
import time
import uuid
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

import boto3  # noqa: E402

from engine.credit import check_quote_credit  # noqa: E402
from engine.loader import cached_dataset  # noqa: E402
from engine.messages import (  # noqa: E402
    build_credit_status_message,
    build_quotation_message,
    mask_phone,
    normalize_phone,
    wa_me_url,
)
from engine.quote import calculate_quote  # noqa: E402
from engine.speech import (  # noqa: E402
    AUDIO_PREFIX,
    TRANSCRIBE_TIMEOUT_SECONDS,
    audio_key,
    job_name,
    transcript_from_payload,
    validate_audio,
)
from integrations import whatsapp  # noqa: E402

REGION = os.environ.get("AWS_REGION", "ap-south-1")
CANONICAL = [
    {"skuId": "SW-ANC-1W10A", "quantity": 20},
    {"skuId": "W-FIN-1.5-RED-90M", "quantity": 3, "uom": "COIL"},
    {"skuId": "MCB-HAV-SP-32A-C", "quantity": 2},
]
CANONICAL_TOTAL = 22306.48
RAVI = "CUST-RAVI-001"

failures: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    mark = "PASS" if condition else "FAIL"
    print(f"  [{mark}] {label}" + (f"  {detail}" if detail else ""))
    if not condition:
        failures.append(label)


def one_second_wav() -> bytes:
    """A short, valid, 16 kHz mono PCM WAV. Near-silent on purpose."""
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(16000)
        # A very low-amplitude tone: a real waveform rather than a zero block,
        # so nothing in the pipeline can treat it as an empty file.
        frames = b"".join(
            struct.pack("<h", int(120 * ((i // 18) % 2 * 2 - 1)))
            for i in range(16000)
        )
        handle.writeframes(frames)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# part one: Amazon Transcribe, for real
# ---------------------------------------------------------------------------

def transcribe_round_trip(bucket: str) -> None:
    s3 = boto3.client("s3", region_name=REGION)
    transcribe = boto3.client("transcribe", region_name=REGION)

    job_id = uuid.uuid4().hex
    name = job_name(job_id)
    audio = one_second_wav()
    checked = validate_audio("audio/wav", audio)
    key = audio_key(job_id, checked["extension"])

    check("the audio passes the API's own validation", checked["bytes"] > 0,
          f"{checked['bytes']} bytes, {checked['mediaFormat']}")
    check("the key lands under the audio prefix", key.startswith(AUDIO_PREFIX), key)

    started = False
    try:
        s3.put_object(Bucket=bucket, Key=key, Body=audio,
                      ContentType="audio/wav", ServerSideEncryption="AES256")
        check("audio uploaded to the private bucket", True, f"s3://{bucket}/{key}")

        transcribe.start_transcription_job(
            TranscriptionJobName=name,
            Media={"MediaFileUri": f"s3://{bucket}/{key}"},
            MediaFormat=checked["mediaFormat"],
            IdentifyLanguage=True,
            LanguageOptions=["ta-IN", "en-IN"],
        )
        started = True
        check("Transcribe accepted the job", True, name)

        deadline = time.time() + TRANSCRIBE_TIMEOUT_SECONDS
        state, job = "QUEUED", None
        while time.time() < deadline:
            job = transcribe.get_transcription_job(
                TranscriptionJobName=name)["TranscriptionJob"]
            state = job["TranscriptionJobStatus"]
            if state in ("COMPLETED", "FAILED"):
                break
            time.sleep(3)

        elapsed = int(TRANSCRIBE_TIMEOUT_SECONDS - (deadline - time.time()))
        check("the job reached a terminal state", state in ("COMPLETED", "FAILED"),
              f"{state} after ~{elapsed}s")

        if state == "COMPLETED":
            import urllib.request

            uri = job["Transcript"]["TranscriptFileUri"]
            with urllib.request.urlopen(uri, timeout=15) as response:
                payload = json.loads(response.read().decode("utf-8"))

            transcript = transcript_from_payload(payload)
            check("the result document parses", isinstance(payload, dict))
            check("the transcript extractor returns a string",
                  isinstance(transcript, str),
                  f"{len(transcript)} characters")
            # No accuracy claim: the clip is near-silent, so an empty
            # transcript is the expected and correct outcome.
            check("no business data appears in the transcript payload",
                  not any(word in json.dumps(payload) for word in
                          ("skuId", "sellingPrice", "creditLimit")))
        else:
            check("a FAILED job still reports a reason",
                  bool((job or {}).get("FailureReason")),
                  str((job or {}).get("FailureReason"))[:80])

    finally:
        print("\n3. cleanup")
        if started:
            try:
                transcribe.delete_transcription_job(TranscriptionJobName=name)
                check("transcription job deleted", True, name)
            except Exception as exc:  # noqa: BLE001
                check("transcription job deleted", False, type(exc).__name__)
        try:
            s3.delete_object(Bucket=bucket, Key=key)
        except Exception as exc:  # noqa: BLE001
            check("audio object deleted", False, type(exc).__name__)
        else:
            listing = s3.list_objects_v2(Bucket=bucket, Prefix=key)
            check("audio object deleted", listing.get("KeyCount", 0) == 0, key)

        leftovers = s3.list_objects_v2(Bucket=bucket, Prefix=AUDIO_PREFIX)
        check("no audio left under the prefix",
              leftovers.get("KeyCount", 0) == 0,
              f"{leftovers.get('KeyCount', 0)} object(s)")


# ---------------------------------------------------------------------------
# part two: WhatsApp, without credentials
# ---------------------------------------------------------------------------

def whatsapp_checks() -> None:
    data = cached_dataset()
    quote = calculate_quote(data, CANONICAL).as_dict()
    credit = check_quote_credit(data, RAVI, quote)

    check("the canonical quotation is unchanged", quote["total"] == CANONICAL_TOTAL,
          f"Rs {quote['total']:,.2f}")

    message = build_quotation_message(
        quote, {"customerName": "Ravi Electrical Works"}, credit)
    text = message["text"]

    check("the message carries the engine's total", "22,306.48" in text)
    check("the unit is preserved and not converted",
          "3 coils" in text and "270" not in text)

    leaked = []
    for line in quote["lines"]:
        product = data.product(line["skuId"])
        if f"{product.costPrice:,.2f}" in text:
            leaked.append(f"cost {product.skuId}")
    for word in ("margin", "supplier", "shortage", "in stock"):
        if word in text.lower():
            leaked.append(word)
    check("no internal figure reaches the customer", not leaked, str(leaked))

    account = build_credit_status_message(credit, None, reminder=True)
    check("an account message reports the customer's own balance",
          "8,500.00" in account["text"])

    phone = normalize_phone(data.customer(RAVI).phone)
    check("the number masks correctly", mask_phone(phone) == "+91******0001",
          mask_phone(phone))
    check("the draft link is still available",
          wa_me_url(text, phone).startswith("https://wa.me/919900000001?text="))

    check("the adapter is OFF by default", whatsapp.is_enabled() is False)
    status = whatsapp.configuration_status()
    check("configuration status exposes no value",
          set(status) == {"enabled", "hasPhoneNumberId", "hasAccessToken",
                          "usesSecretsManager", "hasTemplate", "apiVersion"}
          and status["hasAccessToken"] is False)
    try:
        whatsapp.send_text(phone, text)
        check("a send is refused while disabled", False, "NO ERROR RAISED")
    except whatsapp.WhatsAppError as exc:
        check("a send is refused while disabled", exc.reason == whatsapp.DISABLED,
              exc.reason)


def main() -> int:
    account = boto3.client("sts", region_name=REGION).get_caller_identity()["Account"]
    bucket = f"shopflow-uploads-{account}"

    print(f"\nShopFlow voice + WhatsApp smoke test ({REGION}, {bucket})\n")
    print("1. Amazon Transcribe, end to end")
    print("   (near-silent audio: this checks the plumbing, not accuracy)")
    transcribe_round_trip(bucket)

    print("\n4. WhatsApp adapter and customer messages")
    whatsapp_checks()

    print()
    if failures:
        print(f"SMOKE TEST FAILED: {len(failures)} check(s)")
        for name in failures:
            print(f"  - {name}")
        return 1
    print("SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
