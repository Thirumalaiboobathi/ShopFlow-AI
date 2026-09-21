"""Amazon Transcribe, reduced to the part that can be reasoned about.

WHAT THIS MODULE IS
-------------------
Everything about the transcription path that is a decision rather than a
network call: which audio types are accepted, how large a clip may be, which
languages are asked for, how a Transcribe job's state maps onto a ShopFlow job
status, and how a transcript is pulled out of the result payload.

The boto3 calls themselves live in the API handler beside the other AWS
clients. This file has no AWS import at all, so every rule below is testable
without an account and without a mock.

WHAT TRANSCRIBE IS ALLOWED TO DO
--------------------------------
Produce text. That is the whole of it.

    microphone -> audio -> Amazon Transcribe -> transcript
                                                    |
                                    engine.voice.normalize_transcript
                                                    |
                                        the EXISTING workflows

There is no second order pipeline, no Transcribe-only handler, and nothing
here that knows what a SKU, a price or a customer is. A transcript from
Transcribe and a transcript from the browser's own recogniser are the same
string travelling the same path; only the provider label differs.

LANGUAGE - AND AN HONEST LIMIT
------------------------------
Transcribe supports `ta-IN` and `en-IN`, and this shop's owner speaks both,
often in the same sentence. Transcribe does not: a batch job resolves to ONE
language for the whole clip, so genuine Tanglish - "20 Anchor modular switch
venum" - is transcribed by whichever model wins, and the other half suffers.

That is a real limitation and it is not papered over. Automatic identification
is restricted to the two languages this shop actually uses so the choice is at
least sensible, the owner can force one, and the browser recogniser remains
available as an alternative. No accuracy claim is made for either path.

RETENTION
---------
Audio is transient. The uploaded object is deleted as soon as a transcript is
obtained or the job fails, the Transcribe job itself is deleted with it, and
an S3 lifecycle rule expires anything the cleanup missed. Audio is never
written into a business record - a job row holds the transcript text, never
the recording.
"""

from __future__ import annotations

import re
from typing import Dict, Optional, Tuple

# The provider label that travels on every response from this path.
PROVIDER = "amazon-transcribe"
BROWSER_PROVIDER = "browser-speech"

# Audio types the browser can realistically record and Transcribe can read.
# The value is the MediaFormat Transcribe expects, and the file extension.
#   webm  - what Chrome's MediaRecorder produces by default
#   ogg   - Firefox's default
#   mp4   - Safari
#   wav   - what a desktop recording tool produces
ALLOWED_AUDIO_TYPES: Dict[str, Tuple[str, str]] = {
    "audio/webm": ("webm", "webm"),
    "audio/ogg": ("ogg", "ogg"),
    "audio/mp4": ("mp4", "mp4"),
    "audio/mpeg": ("mp3", "mp3"),
    "audio/mp3": ("mp3", "mp3"),
    "audio/wav": ("wav", "wav"),
    "audio/x-wav": ("wav", "wav"),
    "audio/flac": ("flac", "flac"),
}

# Cost control, and the reason for each number.
#
# A shop order is one or two sentences. Thirty seconds is generous for that and
# short enough that a runaway recording cannot quietly become a long job.
MAX_RECORDING_SECONDS = 30
# Roughly 30 s of Opus in a webm container at a sane bitrate, with headroom.
MAX_AUDIO_BYTES = 2_000_000
# Base64 costs about a third on top, and the request carries a little JSON.
MAX_AUDIO_BODY_BYTES = 3_000_000
# Anything shorter than this is a click, not speech.
MIN_AUDIO_BYTES = 256

# The same ceiling the typed and browser-spoken paths already use, so a
# transcript cannot enter the workflow longer than a transcript ever could.
MAX_TRANSCRIPT_CHARS = 1000

# A short clip finishes in well under a minute. Past this the browser is told
# the job timed out rather than being left polling; the job and its audio are
# cleaned up either way.
TRANSCRIBE_TIMEOUT_SECONDS = 150

# Poll spacing the frontend is told to use. Kept here so the limit and the
# cadence that respects it live together.
POLL_INTERVAL_MS = 1500
MAX_POLL_ATTEMPTS = 40

# ShopFlow language codes mapped to Amazon Transcribe language codes.
#
# THIS LIST IS VERIFIED, NOT ASSUMED. Every code below is one the configured
# Transcribe API declares in its own `LanguageCode` enum, which
# `scripts/smoke_test_multilingual.py` re-checks against botocore's service
# model on the account being deployed to. A language is absent from this map
# because the service does not accept it, not because nobody got to it - and
# the capability matrix reads `voice: false` straight from that absence.
#
# Accepting a code is NOT a claim about recognition quality. It says the
# service will take the job. How well it transcribes a Madurai contractor
# saying "Anchor modular switch" is not something this repository has
# measured, and nothing here claims otherwise.
TRANSCRIBE_LANGUAGES: Dict[str, str] = {
    "en": "en-IN",
    "hi": "hi-IN",
    "ta": "ta-IN",
    "te": "te-IN",
    "bn": "bn-IN",
    "gu": "gu-IN",
    "kn": "kn-IN",
    "ml": "ml-IN",
    "mr": "mr-IN",
    "or": "or-IN",
    "pa": "pa-IN",
    # The only Nepali locale Transcribe offers is the Nepal one. It is the
    # same language; the locale is named here rather than implied.
    "ne": "ne-NP",
}

# The original two-language name, kept because callers and tests use it.
LANGUAGES: Dict[str, str] = TRANSCRIBE_LANGUAGES
# What automatic identification is allowed to choose between. Restricting it
# is the point - left open, Transcribe will happily decide a Tanglish clip is
# Indonesian. This stays at the two languages the Madurai shop speaks: "Auto"
# is for that shop, and any owner wanting another language selects it, which
# pins the job to a single code instead of guessing.
IDENTIFY_LANGUAGE_OPTIONS = ("ta-IN", "en-IN")

DEFAULT_LANGUAGE = "auto"

# Said on screen, and deliberately modest.
ACCURACY_NOTICE = (
    "Voice transcription powered by AWS. Recognition quality may vary by "
    "language, accent and device."
)
TANGLISH_NOTICE = (
    "Amazon Transcribe resolves a clip to one language. Mixed Tamil and "
    "English in a single sentence may transcribe poorly - pick a language, "
    "edit the text, or use browser recognition instead."
)

# Job states, mapped onto the statuses this application already uses.
STATUS_QUEUED = "QUEUED"
STATUS_PROCESSING = "PROCESSING"
STATUS_DONE = "DONE"
STATUS_FAILED = "FAILED"

_JOB_ID = re.compile(r"^[0-9a-f]{32}$")


class InvalidAudioError(ValueError):
    """The uploaded audio cannot be accepted. Carries a safe message."""


class TranscriptionFailed(RuntimeError):
    """Transcribe could not produce a transcript for this clip."""


def media_format(content_type: str) -> str:
    """The MediaFormat Transcribe expects for an accepted content type."""
    entry = ALLOWED_AUDIO_TYPES.get(str(content_type or "").lower().strip())
    if entry is None:
        raise InvalidAudioError("unsupported audio type")
    return entry[0]


def audio_extension(content_type: str) -> str:
    entry = ALLOWED_AUDIO_TYPES.get(str(content_type or "").lower().strip())
    if entry is None:
        raise InvalidAudioError("unsupported audio type")
    return entry[1]


def validate_audio(content_type, audio_bytes) -> dict:
    """Accept a recording, or refuse it with a reason fit to show a shopkeeper.

    Every limit is checked here rather than at the boto3 call, so an oversized
    or unreadable clip costs nothing: no S3 object is written and no
    transcription job is started.
    """
    kind = str(content_type or "").lower().strip()
    if kind not in ALLOWED_AUDIO_TYPES:
        raise InvalidAudioError(
            "audio type must be one of " + ", ".join(sorted(ALLOWED_AUDIO_TYPES)))

    if not isinstance(audio_bytes, (bytes, bytearray)):
        raise InvalidAudioError("audio is required")
    size = len(audio_bytes)
    if size == 0:
        raise InvalidAudioError("audio is empty")
    if size < MIN_AUDIO_BYTES:
        raise InvalidAudioError("that recording is too short to transcribe")
    if size > MAX_AUDIO_BYTES:
        raise InvalidAudioError(
            f"audio must be {MAX_AUDIO_BYTES // 1000} KB or smaller "
            f"(about {MAX_RECORDING_SECONDS} seconds)")

    return {
        "contentType": kind,
        "mediaFormat": media_format(kind),
        "extension": audio_extension(kind),
        "bytes": size,
    }


def resolve_language(requested) -> dict:
    """Which language Transcribe is asked for, and how.

    "auto" restricts automatic identification to the two languages this shop
    uses. An explicit choice pins the job to one. An unknown value falls back
    to automatic rather than erroring - a mistyped preference should not cost
    the owner their recording.
    """
    choice = str(requested or DEFAULT_LANGUAGE).lower().strip()
    # "ta-IN" and "ta" mean the same thing to a caller; only the primary
    # subtag is meaningful to this map.
    choice = choice.split("-")[0] if choice != DEFAULT_LANGUAGE else choice
    if choice in TRANSCRIBE_LANGUAGES:
        return {
            "requested": choice,
            "identifyLanguage": False,
            "languageCode": TRANSCRIBE_LANGUAGES[choice],
            "languageOptions": None,
        }
    return {
        "requested": DEFAULT_LANGUAGE,
        "identifyLanguage": True,
        "languageCode": None,
        "languageOptions": list(IDENTIFY_LANGUAGE_OPTIONS),
    }


def job_name(job_id: str) -> str:
    """The Transcribe job name for a ShopFlow job id.

    Prefixed so the IAM policy can be scoped to this application's jobs by
    name, and so a job in the console is identifiable. The id is already a
    32-character hex string, which is within Transcribe's allowed character
    set, but it is validated here rather than assumed.
    """
    if not _JOB_ID.match(str(job_id or "")):
        raise InvalidAudioError("invalid job id")
    return f"shopflow-{job_id}"


AUDIO_PREFIX = "voice-audio/"


def audio_key(job_id: str, extension: str) -> str:
    """Where the recording lives while it is being transcribed.

    Its own prefix, separate from `price-lists/`, so the lifecycle rule and
    the IAM scope for audio cannot reach a supplier document.
    """
    if not _JOB_ID.match(str(job_id or "")):
        raise InvalidAudioError("invalid job id")
    ext = str(extension or "").lower()
    if not re.fullmatch(r"[a-z0-9]{2,5}", ext):
        raise InvalidAudioError("invalid audio extension")
    return f"{AUDIO_PREFIX}{job_id}.{ext}"


def transcript_from_payload(payload) -> str:
    """The spoken words, out of Transcribe's result document.

    Transcribe returns a large object with per-word confidences and
    alternatives. Only the joined transcript is taken, it is trimmed to the
    same ceiling every other transcript obeys, and control characters are
    stripped - the text goes straight into the existing voice normaliser next.
    """
    if not isinstance(payload, dict):
        return ""
    results = payload.get("results")
    if not isinstance(results, dict):
        return ""
    transcripts = results.get("transcripts")
    if not isinstance(transcripts, list):
        return ""

    parts = []
    for row in transcripts:
        if isinstance(row, dict) and isinstance(row.get("transcript"), str):
            parts.append(row["transcript"])
    text = " ".join(p.strip() for p in parts if p.strip())
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", text)
    return re.sub(r"\s+", " ", text).strip()[:MAX_TRANSCRIPT_CHARS]


def detected_language(payload) -> Optional[str]:
    """Which language Transcribe decided on, when it was asked to identify."""
    if not isinstance(payload, dict):
        return None
    results = payload.get("results")
    if isinstance(results, dict) and results.get("language_code"):
        return str(results["language_code"])
    if payload.get("LanguageCode"):
        return str(payload["LanguageCode"])
    return None


def classify_job(transcribe_status, started_at: int, now: int) -> dict:
    """A Transcribe job state, as a ShopFlow job status.

    Timeout is decided here rather than by the browser giving up, so a job
    that never finishes is closed, its audio deleted, and the owner told -
    instead of a polling loop running until the tab is closed.
    """
    state = str(transcribe_status or "").upper()
    elapsed = max(0, int(now) - int(started_at or 0))

    if state == "COMPLETED":
        return {"status": STATUS_DONE, "elapsedSeconds": elapsed, "error": None}
    if state == "FAILED":
        return {
            "status": STATUS_FAILED,
            "elapsedSeconds": elapsed,
            "error": "That recording could not be transcribed.",
        }
    if elapsed > TRANSCRIBE_TIMEOUT_SECONDS:
        return {
            "status": STATUS_FAILED,
            "elapsedSeconds": elapsed,
            "error": "Transcription timed out. Please try again, or type the "
                     "order instead.",
            "timedOut": True,
        }
    if state == "QUEUED":
        return {"status": STATUS_QUEUED, "elapsedSeconds": elapsed, "error": None}
    return {"status": STATUS_PROCESSING, "elapsedSeconds": elapsed, "error": None}
