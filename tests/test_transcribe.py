"""Amazon Transcribe: the plumbing, and the boundary it must not cross.

Two things are being tested.

The first is ordinary: limits, validation, every failure mode, and cleanup.
Audio arrives from a microphone, so most of what reaches this endpoint will
one day be wrong in some way, and each wrong way should cost nothing.

The second is the architectural claim. Transcribe produces TEXT. It resolves
no SKU, quotes no price, checks no stock and makes no decision, and the
transcript it produces travels the SAME path a browser-recognised one does.
The last block proves that by taking the endpoint's own output and feeding it
to the existing workflows - not by inspecting the code and hoping.

AWS is mocked throughout. No credential is needed to run any of this.
"""

from __future__ import annotations

import base64
import json

import pytest

import lambdas.api.handler as api
from engine.credit import check_credit
from engine.loader import cached_dataset
from engine.speech import (
    ALLOWED_AUDIO_TYPES,
    AUDIO_PREFIX,
    IDENTIFY_LANGUAGE_OPTIONS,
    MAX_AUDIO_BYTES,
    MAX_TRANSCRIPT_CHARS,
    MIN_AUDIO_BYTES,
    PROVIDER,
    TRANSCRIBE_TIMEOUT_SECONDS,
    InvalidAudioError,
    audio_key,
    classify_job,
    detected_language,
    job_name,
    resolve_language,
    transcript_from_payload,
    validate_audio,
)
from engine.voice import answer_shop_query, classify_intent, normalize_transcript
from test_api import FakeQueue, FakeTable

AUDIO = b"\x1aE\xdf\xa3" + b"0" * 4000          # webm-ish, comfortably valid
CANONICAL_SPOKEN = ("Anna, 20 Anchor modular switch 1-Way 10 amp, 3 coil "
                    "Finolex 1.5 sq mm red wire 90m, 2 Havells MCB SP 32 amp.")


# ---------------------------------------------------------------------------
# fakes
# ---------------------------------------------------------------------------

class FakeS3:
    """Records writes AND deletes, because cleanup is part of the contract."""

    def __init__(self):
        self.objects = {}
        self.deleted = []

    def put_object(self, **kwargs):
        self.objects[kwargs["Key"]] = kwargs
        return {}

    def delete_object(self, Bucket, Key):
        self.deleted.append(Key)
        self.objects.pop(Key, None)
        return {}


class FakeTranscribe:
    """A Transcribe that can be told exactly how to behave."""

    def __init__(self, state="COMPLETED", fail_start=False):
        self.state = state
        self.fail_start = fail_start
        self.started = []
        self.deleted = []
        self.uri = "https://transcribe.example/result.json"

    def start_transcription_job(self, **kwargs):
        if self.fail_start:
            raise RuntimeError("BadRequestException")
        self.started.append(kwargs)
        return {"TranscriptionJob": {"TranscriptionJobStatus": "QUEUED"}}

    def get_transcription_job(self, TranscriptionJobName):
        if self.state == "MISSING":
            raise RuntimeError("NotFoundException")
        return {"TranscriptionJob": {
            "TranscriptionJobStatus": self.state,
            "Transcript": {"TranscriptFileUri": self.uri},
        }}

    def delete_transcription_job(self, TranscriptionJobName):
        self.deleted.append(TranscriptionJobName)
        return {}


@pytest.fixture
def voice_env(monkeypatch):
    table, queue, s3 = FakeTable(), FakeQueue(), FakeS3()
    transcribe = FakeTranscribe()
    monkeypatch.setenv("TABLE_NAME", "shopflow-demo")
    monkeypatch.setenv(
        "ORDERS_QUEUE_URL",
        "https://sqs.ap-south-1.amazonaws.com/000000000000/shopflow-orders")
    monkeypatch.setenv("UPLOADS_BUCKET", "shopflow-uploads-test")
    monkeypatch.setattr(api, "table", lambda: table)
    monkeypatch.setattr(api, "sqs_client", lambda: queue)
    monkeypatch.setattr(api, "s3_client", lambda: s3)
    monkeypatch.setattr(api, "transcribe_client", lambda: transcribe)
    return table, s3, transcribe, queue


def set_transcript(monkeypatch, text, language="en-IN"):
    """Stand in for the fetch of Transcribe's result document."""
    monkeypatch.setattr(api, "_fetch_transcript", lambda uri: {
        "results": {"transcripts": [{"transcript": text}],
                    "language_code": language},
    })


def post_audio(audio=AUDIO, content_type="audio/webm", language="auto", **extra):
    body = {"contentType": content_type, "language": language, **extra}
    if audio is not None:
        body["audioBase64"] = base64.b64encode(audio).decode("ascii")
    return {"routeKey": "POST /api/voice/transcribe", "body": json.dumps(body)}


def get_job(job_id):
    return {"routeKey": "GET /api/jobs/{jobId}",
            "pathParameters": {"jobId": job_id}}


def body_of(response):
    return json.loads(response["body"])


def start_and_finish(voice_env, monkeypatch, text, state="COMPLETED"):
    """Submit a clip and poll it once, with the transcript already decided."""
    _table, _s3, transcribe, _lam = voice_env
    transcribe.state = state
    set_transcript(monkeypatch, text)
    job_id = body_of(api.handler(post_audio(), None))["jobId"]
    return job_id, body_of(api.handler(get_job(job_id), None))


# ---------------------------------------------------------------------------
# 1-4  the request
# ---------------------------------------------------------------------------

def test_1_a_valid_recording_is_accepted_and_a_job_is_started(voice_env):
    table, s3, transcribe, queue = voice_env

    response = api.handler(post_audio(), None)
    assert response["statusCode"] == 202

    payload = body_of(response)
    assert payload["status"] == "QUEUED"
    assert payload["jobType"] == "TRANSCRIPT"
    assert payload["provider"] == PROVIDER

    # The audio went to its own prefix in the private bucket.
    key = list(s3.objects)[0]
    assert key.startswith(AUDIO_PREFIX)
    assert s3.objects[key]["ServerSideEncryption"] == "AES256"

    # One Transcribe job, named so IAM can be scoped to it.
    assert len(transcribe.started) == 1
    assert transcribe.started[0]["TranscriptionJobName"].startswith("shopflow-")
    # No worker, and therefore no Bedrock, anywhere on this path.
    assert queue.messages == []


def test_1b_the_stored_job_holds_the_key_but_never_the_audio(voice_env):
    table, _s3, _t, _l = voice_env
    job_id = body_of(api.handler(post_audio(), None))["jobId"]
    item = table.items[(f"JOB#{job_id}", "META")]

    assert item["audioKey"].startswith(AUDIO_PREFIX)
    assert item["jobType"] == "TRANSCRIPT"
    assert item["expiresAt"] > item["createdAt"]
    # The recording itself is not in the business record, in any encoding.
    serialised = json.dumps(item, default=str)
    assert base64.b64encode(AUDIO).decode("ascii")[:32] not in serialised


@pytest.mark.parametrize("content_type", [
    "audio/aiff", "video/mp4", "image/png", "text/plain", "", None,
    "application/octet-stream",
])
def test_2_an_unsupported_audio_type_is_refused_before_anything_is_written(
        voice_env, content_type):
    _table, s3, transcribe, _l = voice_env
    response = api.handler(post_audio(content_type=content_type), None)

    assert response["statusCode"] == 400
    # Nothing uploaded and no job started, so a bad request costs nothing.
    assert s3.objects == {}
    assert transcribe.started == []


def test_3_oversized_audio_is_refused(voice_env):
    _table, s3, transcribe, _l = voice_env
    response = api.handler(post_audio(b"0" * (MAX_AUDIO_BYTES + 1)), None)

    assert response["statusCode"] in (400, 413)
    assert s3.objects == {}
    assert transcribe.started == []


def test_3b_an_oversized_request_body_is_refused_without_decoding_it(voice_env):
    huge = {"routeKey": "POST /api/voice/transcribe",
            "body": "x" * (api.MAX_AUDIO_BODY_BYTES + 10)}
    assert api.handler(huge, None)["statusCode"] == 413


def test_4_empty_and_tiny_recordings_are_refused(voice_env):
    assert api.handler(post_audio(b""), None)["statusCode"] == 400
    assert api.handler(post_audio(b"0" * (MIN_AUDIO_BYTES - 1)),
                       None)["statusCode"] == 400
    assert api.handler(post_audio(audio=None), None)["statusCode"] == 400


def test_4b_a_malformed_request_is_refused(voice_env):
    for event in (
        {"routeKey": "POST /api/voice/transcribe", "body": "not json"},
        {"routeKey": "POST /api/voice/transcribe", "body": "{}"},
        {"routeKey": "POST /api/voice/transcribe",
         "body": json.dumps({"audioBase64": "not base64!!", "contentType": "audio/webm"})},
    ):
        assert api.handler(event, None)["statusCode"] == 400


# ---------------------------------------------------------------------------
# 5-9  the outcome
# ---------------------------------------------------------------------------

def test_5_a_completed_job_returns_the_transcript_and_nothing_else(
        voice_env, monkeypatch):
    _job_id, body = start_and_finish(voice_env, monkeypatch, "20 anchor switches")

    assert body["status"] == "DONE"
    assert body["result"]["transcript"] == "20 anchor switches"
    assert body["result"]["provider"] == PROVIDER
    assert body["result"]["handledBy"] == "existing-workflow"

    # The endpoint decided nothing. No business field appears anywhere in it.
    serialised = json.dumps(body)
    for forbidden in ("skuId", "sellingPrice", "total", "onHand", "decision",
                      "creditLimit", "quote", "marginProtection"):
        assert forbidden not in serialised, forbidden


def test_5b_the_recording_is_deleted_as_soon_as_the_transcript_is_read(
        voice_env, monkeypatch):
    _table, s3, transcribe, _l = voice_env
    start_and_finish(voice_env, monkeypatch, "hello")

    assert len(s3.deleted) == 1
    assert s3.deleted[0].startswith(AUDIO_PREFIX)
    assert s3.objects == {}
    # And the Transcribe job with it - nothing is left behind.
    assert len(transcribe.deleted) == 1


def test_5c_a_finished_job_stops_calling_transcribe_on_later_polls(
        voice_env, monkeypatch):
    _table, _s3, transcribe, _l = voice_env
    job_id, first = start_and_finish(voice_env, monkeypatch, "hello")

    transcribe.state = "MISSING"   # a second call would now raise
    second = body_of(api.handler(get_job(job_id), None))

    assert second["status"] == "DONE"
    assert second["result"]["transcript"] == first["result"]["transcript"]


def test_6_a_failed_transcription_is_reported_and_cleaned_up(
        voice_env, monkeypatch):
    _table, s3, transcribe, _l = voice_env
    _job_id, body = start_and_finish(voice_env, monkeypatch, "", state="FAILED")

    assert body["status"] == "FAILED"
    assert body["error"]
    assert s3.deleted and transcribe.deleted


def test_7_a_job_that_never_finishes_times_out_rather_than_polling_forever(
        voice_env, monkeypatch):
    table, s3, transcribe, _l = voice_env
    transcribe.state = "IN_PROGRESS"
    job_id = body_of(api.handler(post_audio(), None))["jobId"]

    # Still running, inside the budget: keep waiting.
    assert body_of(api.handler(get_job(job_id), None))["status"] == "PROCESSING"

    # Now push the job's start time past the deadline.
    item = table.items[(f"JOB#{job_id}", "META")]
    item["createdAt"] = item["createdAt"] - TRANSCRIBE_TIMEOUT_SECONDS - 5

    body = body_of(api.handler(get_job(job_id), None))
    assert body["status"] == "FAILED"
    assert "timed out" in body["error"].lower()
    # A timeout still cleans up.
    assert s3.deleted and transcribe.deleted


def test_8_an_aws_error_on_start_deletes_the_audio_and_reports_cleanly(
        voice_env):
    _table, s3, transcribe, _l = voice_env
    transcribe.fail_start = True

    response = api.handler(post_audio(), None)

    assert response["statusCode"] == 502
    assert "error" in body_of(response)
    # The clip was uploaded before the job failed, so it is removed at once.
    assert s3.deleted and s3.objects == {}


def test_8b_an_aws_error_while_polling_is_reported_not_raised(
        voice_env, monkeypatch):
    _table, _s3, transcribe, _l = voice_env
    job_id = body_of(api.handler(post_audio(), None))["jobId"]
    transcribe.state = "MISSING"

    body = body_of(api.handler(get_job(job_id), None))
    assert body["status"] == "FAILED"
    assert body["error"]


def test_8c_a_transcript_that_cannot_be_fetched_fails_safely(
        voice_env, monkeypatch):
    _table, s3, transcribe, _l = voice_env
    transcribe.state = "COMPLETED"

    def explode(uri):
        raise OSError("network")

    monkeypatch.setattr(api, "_fetch_transcript", explode)
    job_id = body_of(api.handler(post_audio(), None))["jobId"]

    body = body_of(api.handler(get_job(job_id), None))
    assert body["status"] == "FAILED"
    assert s3.deleted


def test_9_an_empty_transcript_is_a_failure_with_something_to_do_about_it(
        voice_env, monkeypatch):
    _job_id, body = start_and_finish(voice_env, monkeypatch, "   ")

    assert body["status"] == "FAILED"
    assert "no speech" in body["error"].lower()


def test_9b_a_transcript_is_truncated_to_the_existing_ceiling(
        voice_env, monkeypatch):
    _job_id, body = start_and_finish(voice_env, monkeypatch, "wire " * 800)
    assert len(body["result"]["transcript"]) <= MAX_TRANSCRIPT_CHARS


# ---------------------------------------------------------------------------
# 10-12  language
# ---------------------------------------------------------------------------

def test_10_automatic_identification_is_restricted_to_two_languages(voice_env):
    _table, _s3, transcribe, _l = voice_env
    api.handler(post_audio(language="auto"), None)

    params = transcribe.started[0]
    assert params["IdentifyLanguage"] is True
    assert params["LanguageOptions"] == list(IDENTIFY_LANGUAGE_OPTIONS)
    assert "LanguageCode" not in params


@pytest.mark.parametrize("choice,code", [("ta", "ta-IN"), ("en", "en-IN")])
def test_10b_an_explicit_language_pins_the_job(voice_env, choice, code):
    _table, _s3, transcribe, _l = voice_env
    api.handler(post_audio(language=choice), None)

    params = transcribe.started[0]
    assert params["LanguageCode"] == code
    assert "IdentifyLanguage" not in params


def test_10c_an_unknown_language_falls_back_to_identification(voice_env):
    _table, _s3, transcribe, _l = voice_env
    api.handler(post_audio(language="klingon"), None)
    assert transcribe.started[0]["IdentifyLanguage"] is True


# ---------------------------------------------------------------------------
# 13-16  the transcript reaches the EXISTING workflows
# ---------------------------------------------------------------------------

def test_13_the_transcript_goes_through_the_existing_normalisation(
        voice_env, monkeypatch):
    """Not a parallel path: the same `normalize_transcript` the browser path
    has always used, with the same result."""
    _job_id, body = start_and_finish(
        voice_env, monkeypatch, "3 coils Finolex 1.5 sq mm red wire")

    transcript = body["result"]["transcript"]
    normalized, _aliases = normalize_transcript(transcript)
    assert "coil" in normalized
    assert "sq mm" in normalized


def test_14_a_spoken_order_reaches_the_existing_order_workflow(
        voice_env, monkeypatch):
    _job_id, body = start_and_finish(voice_env, monkeypatch, CANONICAL_SPOKEN)

    answer = answer_shop_query(cached_dataset(), body["result"]["transcript"])
    assert answer["status"] == "DELEGATE"
    assert answer["delegateTo"] == "ORDER"
    assert answer["handledBy"] == "existing-workflow"


def test_15_a_spoken_margin_question_reaches_the_existing_margin_workflow(
        voice_env, monkeypatch):
    _job_id, body = start_and_finish(
        voice_env, monkeypatch, "Finolex 1.5 sq mm red wire 90m margin evlo?")

    answer = answer_shop_query(
        cached_dataset(), body["result"]["transcript"],
        {"W-FIN-1.5-RED-90M": 6300.0})

    assert answer["intent"] == "MARGIN"
    assert answer["margin"]["comparisonAvailable"] is True
    # The figure came from the engine, not from anything on the audio path.
    assert answer["margin"]["newMarginAmount"] == 308.0


def test_16_a_transcript_can_drive_the_existing_credit_workflow(
        voice_env, monkeypatch):
    """Transcribe supplies the words. The decision is still subtraction in
    `engine.credit`, against the shop's own record."""
    _job_id, body = start_and_finish(
        voice_env, monkeypatch, "Ravi Electrical Works ku 4200 order")

    assert "4200" in body["result"]["transcript"]
    decision = check_credit(cached_dataset(), "CUST-RAVI-001", 4200)
    assert decision["decision"] == "APPROVED"
    assert decision["projectedOutstanding"] == 12700.0


def test_16b_the_transcribe_endpoint_itself_resolves_no_product(
        voice_env, monkeypatch):
    """The clearest statement of the boundary: speech in, text out."""
    _job_id, body = start_and_finish(voice_env, monkeypatch, CANONICAL_SPOKEN)
    assert set(body["result"]) == {
        "transcript", "provider", "handledBy", "detectedLanguage"}


# ---------------------------------------------------------------------------
# 17  the pure layer
# ---------------------------------------------------------------------------

def test_17_the_speech_module_reaches_no_aws_service():
    """Checked on the parsed module, not on its text.

    A word-search would fail on the docstring, which explains where the boto3
    calls live precisely because they do not live here. What matters is that
    nothing is imported and nothing is called.
    """
    import ast
    import inspect

    from engine import speech

    tree = ast.parse(inspect.getsource(speech))

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert not imported & {"boto3", "botocore", "urllib", "requests"}, imported

    called = {
        node.func.attr for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    for forbidden in ("start_transcription_job", "get_transcription_job",
                      "put_object", "put_item", "invoke_model"):
        assert forbidden not in called, f"speech.py calls {forbidden}"


def test_17b_validation_accepts_every_advertised_audio_type():
    for content_type in ALLOWED_AUDIO_TYPES:
        checked = validate_audio(content_type, AUDIO)
        assert checked["mediaFormat"]
        assert checked["bytes"] == len(AUDIO)


def test_17c_keys_and_job_names_refuse_anything_but_a_job_id():
    assert job_name("a" * 32) == "shopflow-" + "a" * 32
    assert audio_key("b" * 32, "webm") == f"{AUDIO_PREFIX}{'b' * 32}.webm"
    for bad in ("", "../../etc", "NOTHEX", "a" * 31, None):
        with pytest.raises(InvalidAudioError):
            job_name(bad)
        with pytest.raises(InvalidAudioError):
            audio_key(bad, "webm")
    with pytest.raises(InvalidAudioError):
        audio_key("c" * 32, "../x")


def test_17d_the_transcript_extractor_survives_a_malformed_document():
    for payload in (None, {}, {"results": None}, {"results": {}},
                    {"results": {"transcripts": "no"}},
                    {"results": {"transcripts": [{}]}}):
        assert transcript_from_payload(payload) == ""


def test_17e_control_characters_are_stripped_from_a_transcript():
    payload = {"results": {"transcripts": [{"transcript": "twenty\x00 switches"}]}}
    assert transcript_from_payload(payload) == "twenty switches"


def test_17f_the_detected_language_is_reported_when_transcribe_gives_one():
    assert detected_language(
        {"results": {"language_code": "ta-IN"}}) == "ta-IN"
    assert detected_language({}) is None


def test_17g_job_states_map_onto_the_applications_own_statuses():
    assert classify_job("COMPLETED", 0, 0)["status"] == "DONE"
    assert classify_job("FAILED", 0, 0)["status"] == "FAILED"
    assert classify_job("QUEUED", 100, 101)["status"] == "QUEUED"
    assert classify_job("IN_PROGRESS", 100, 101)["status"] == "PROCESSING"
    late = classify_job("IN_PROGRESS", 0, TRANSCRIBE_TIMEOUT_SECONDS + 1)
    assert late["status"] == "FAILED" and late["timedOut"] is True


def test_17h_language_resolution_never_invents_a_language():
    assert resolve_language("ta")["languageCode"] == "ta-IN"
    for junk in (None, "", "auto", "fr", "zz", 5):
        resolved = resolve_language(junk)
        assert resolved["identifyLanguage"] is True
        assert resolved["languageOptions"] == list(IDENTIFY_LANGUAGE_OPTIONS)
