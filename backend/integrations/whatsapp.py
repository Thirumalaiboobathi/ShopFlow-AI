"""WhatsApp Business Cloud API adapter.

WHAT THIS IS
------------
A transport. It takes a message that `engine.messages` already built and hands
it to Meta's Cloud API. It contains no business logic, calculates nothing, and
knows nothing about SKUs, prices, stock, credit or margin - it receives a
finished string and a destination.

STATUS: CONFIGURATION REQUIRED
------------------------------
This adapter is written against the real Cloud API and makes real HTTPS calls
when it is configured. It is NOT configured in this repository, and no
credentials exist here. `WHATSAPP_API_ENABLED` defaults to **false**, in which
case nothing is sent and the caller falls back to the `wa.me` draft the
product has always used.

That fallback is not a stub standing in for a broken feature. It is the
shipping behaviour: the owner reviews a draft and sends it themselves. The
Cloud API path is what a shop with a Meta Business account would switch on.

Because no credentials were available, **the Cloud API path has never been
executed against Meta's servers by the author**. What is tested is the
adapter's behaviour: the request it builds, every error it maps, that it never
returns a token, and that the disabled path falls back correctly. Nothing in
this repository claims a working WhatsApp Business connection.

THE 24-HOUR WINDOW
------------------
Meta does not allow arbitrary outbound messages. A free-form text message is
permitted only inside a 24-hour customer service window opened by the
customer's own last message. Outside it, an approved message template is
required, which means a Meta Business account, a registered number, and
template review.

Both are supported: with `WHATSAPP_TEMPLATE_NAME` set the adapter sends a
template, otherwise a plain text message. Neither can make an unapproved
message deliverable, and the adapter reports a template rejection as exactly
that rather than as a generic failure.

CONFIGURATION
-------------
Names only - no value for any of these appears anywhere in this repository.

    WHATSAPP_API_ENABLED        "true" to send; anything else falls back
    WHATSAPP_PHONE_NUMBER_ID    the sending number's id from Meta
    WHATSAPP_ACCESS_TOKEN       the access token (dev/local use)
    WHATSAPP_TOKEN_SECRET_ARN   preferred: a Secrets Manager ARN holding it
    WHATSAPP_TEMPLATE_NAME      optional approved template name
    WHATSAPP_TEMPLATE_LANGUAGE  optional template language, default en
    WHATSAPP_API_VERSION        optional Graph version, default v21.0

A token in a Lambda environment variable is acceptable for a local trial and
is not what a real deployment should do; `WHATSAPP_TOKEN_SECRET_ARN` is read
first when both are present, and the CDK grants access to that one secret ARN
and nothing else.
"""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from typing import Optional

GRAPH_HOST = "graph.facebook.com"
DEFAULT_API_VERSION = "v21.0"
REQUEST_TIMEOUT_SECONDS = 10

ENV_ENABLED = "WHATSAPP_API_ENABLED"
ENV_PHONE_ID = "WHATSAPP_PHONE_NUMBER_ID"
ENV_TOKEN = "WHATSAPP_ACCESS_TOKEN"
ENV_TOKEN_SECRET = "WHATSAPP_TOKEN_SECRET_ARN"
ENV_TEMPLATE = "WHATSAPP_TEMPLATE_NAME"
ENV_TEMPLATE_LANG = "WHATSAPP_TEMPLATE_LANGUAGE"
ENV_API_VERSION = "WHATSAPP_API_VERSION"

# Failure reasons the caller may act on. Every one of them leaves the wa.me
# draft available, because a message that could not be sent is still a message
# the owner can send by hand.
DISABLED = "DISABLED"
NOT_CONFIGURED = "NOT_CONFIGURED"
AUTH_FAILED = "AUTH_FAILED"
RATE_LIMITED = "RATE_LIMITED"
TEMPLATE_REJECTED = "TEMPLATE_REJECTED"
INVALID_RECIPIENT = "INVALID_RECIPIENT"
NETWORK_ERROR = "NETWORK_ERROR"
TIMEOUT = "TIMEOUT"
API_ERROR = "API_ERROR"

# Safe sentences. Meta's own error text can contain account identifiers, so it
# is logged by code and never returned to the browser verbatim.
_MESSAGES = {
    DISABLED: "WhatsApp sending is not switched on for this shop.",
    NOT_CONFIGURED: "WhatsApp is switched on but not fully configured.",
    AUTH_FAILED: "WhatsApp rejected the shop's credentials.",
    RATE_LIMITED: "WhatsApp is rate limiting messages. Try again shortly.",
    TEMPLATE_REJECTED: (
        "WhatsApp would not deliver this message. Outside the 24-hour reply "
        "window an approved message template is required."),
    INVALID_RECIPIENT: "WhatsApp does not recognise that number.",
    NETWORK_ERROR: "WhatsApp could not be reached.",
    TIMEOUT: "WhatsApp did not respond in time.",
    API_ERROR: "WhatsApp returned an error.",
}


class WhatsAppError(RuntimeError):
    """A send failed. Carries a reason code and a sentence safe to display."""

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        self.detail = detail
        super().__init__(_MESSAGES.get(reason, _MESSAGES[API_ERROR]))

    @property
    def safe_message(self) -> str:
        return _MESSAGES.get(self.reason, _MESSAGES[API_ERROR])


def _env(name: str) -> str:
    return str(os.environ.get(name) or "").strip()


def is_enabled() -> bool:
    """False unless explicitly switched on. The safe default is the default."""
    return _env(ENV_ENABLED).lower() in ("true", "1", "yes", "on")


def _access_token() -> str:
    """The token, from Secrets Manager if an ARN is configured.

    Fetched per invocation rather than cached across warm starts, so rotating
    the secret takes effect without redeploying. The value is never logged,
    never returned and never stored anywhere by this module.
    """
    arn = _env(ENV_TOKEN_SECRET)
    if arn:
        import boto3

        try:
            secret = boto3.client("secretsmanager").get_secret_value(SecretId=arn)
        except Exception as exc:  # noqa: BLE001
            raise WhatsAppError(NOT_CONFIGURED,
                                f"secret unreadable: {type(exc).__name__}") from exc
        raw = secret.get("SecretString") or ""
        # A secret may hold the bare token or a small JSON document.
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return str(parsed.get("token") or parsed.get("accessToken") or "")
        except (ValueError, TypeError):
            pass
        return raw.strip()
    return _env(ENV_TOKEN)


def configuration_status() -> dict:
    """What is configured - as booleans. No value, ever.

    Safe to return from the API and to put in an execution trace: it says
    whether a token exists, never what it is.
    """
    return {
        "enabled": is_enabled(),
        "hasPhoneNumberId": bool(_env(ENV_PHONE_ID)),
        "hasAccessToken": bool(_env(ENV_TOKEN) or _env(ENV_TOKEN_SECRET)),
        "usesSecretsManager": bool(_env(ENV_TOKEN_SECRET)),
        "hasTemplate": bool(_env(ENV_TEMPLATE)),
        "apiVersion": _env(ENV_API_VERSION) or DEFAULT_API_VERSION,
    }


def _payload(to: str, text: str) -> dict:
    """The Cloud API request body.

    A template when one is configured, a plain text message otherwise. The
    message body travels as the template's single parameter, so the text the
    customer sees is the text `engine.messages` built either way.
    """
    template = _env(ENV_TEMPLATE)
    if template:
        return {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "template",
            "template": {
                "name": template,
                "language": {"code": _env(ENV_TEMPLATE_LANG) or "en"},
                "components": [{
                    "type": "body",
                    "parameters": [{"type": "text", "text": text}],
                }],
            },
        }
    return {
        "messaging_product": "whatsapp",
        "recipient_type": "individual",
        "to": to,
        "type": "text",
        "text": {"preview_url": False, "body": text},
    }


def _classify(status: int, body: dict) -> str:
    """Meta's response, as one of the reasons the caller understands."""
    error = (body or {}).get("error") or {}
    code = error.get("code")
    subcode = error.get("error_subcode")

    if status in (401, 403) or code in (190, 200, 10):
        return AUTH_FAILED
    if status == 429 or code in (4, 80007, 130429):
        return RATE_LIMITED
    if code in (132000, 132001, 132005, 132007, 132012, 132015, 131026):
        # Template missing, not approved, parameter mismatch, or undeliverable
        # outside the customer service window.
        return TEMPLATE_REJECTED
    if code in (131030, 131031, 131047, 133010) or subcode == 2494008:
        return INVALID_RECIPIENT
    if status >= 500:
        return API_ERROR
    return API_ERROR


def send_text(to: str, text: str, *, timeout: int = REQUEST_TIMEOUT_SECONDS) -> dict:
    """Send one message. Raises `WhatsAppError` with a reason on any failure.

    `to` must already be normalised E.164 - `engine.messages.normalize_phone`
    does that, and this adapter does not attempt to repair a number.

    The return value carries the provider's message id and the MASKED
    recipient. The full number is deliberately not echoed, so a caller cannot
    accidentally log it by logging this result.
    """
    from engine.messages import mask_phone

    if not is_enabled():
        raise WhatsAppError(DISABLED)

    phone_id = _env(ENV_PHONE_ID)
    token = _access_token()
    if not phone_id or not token:
        raise WhatsAppError(NOT_CONFIGURED)
    if not to or not str(to).startswith("+"):
        raise WhatsAppError(INVALID_RECIPIENT)
    if not str(text or "").strip():
        raise WhatsAppError(API_ERROR, "empty message body")

    version = _env(ENV_API_VERSION) or DEFAULT_API_VERSION
    url = f"https://{GRAPH_HOST}/{version}/{phone_id}/messages"
    data = json.dumps(_payload(str(to).lstrip("+"), text)).encode("utf-8")

    request = urllib.request.Request(
        url, data=data, method="POST",
        headers={
            "content-type": "application/json",
            "authorization": f"Bearer {token}",
        },
    )

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = json.loads(response.read().decode("utf-8") or "{}")
            status = response.status
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8") or "{}")
        except Exception:  # noqa: BLE001
            body = {}
        reason = _classify(exc.code, body)
        # Logged by CODE only. Meta's message text can name the business
        # account, and the recipient never appears here at all.
        print(json.dumps({"event": "whatsapp_send_failed", "reason": reason,
                          "httpStatus": exc.code,
                          "providerCode": ((body.get("error") or {}).get("code"))}))
        raise WhatsAppError(reason, f"http {exc.code}") from exc
    except socket.timeout as exc:
        raise WhatsAppError(TIMEOUT) from exc
    except urllib.error.URLError as exc:
        if isinstance(getattr(exc, "reason", None), socket.timeout):
            raise WhatsAppError(TIMEOUT) from exc
        raise WhatsAppError(NETWORK_ERROR) from exc
    except (ValueError, OSError) as exc:
        raise WhatsAppError(NETWORK_ERROR, type(exc).__name__) from exc

    if status >= 400:
        raise WhatsAppError(_classify(status, body), f"http {status}")

    messages = body.get("messages") or []
    message_id = (messages[0].get("id") if messages and isinstance(messages[0], dict)
                  else None)
    print(json.dumps({"event": "whatsapp_send", "reason": "OK",
                      "recipient": mask_phone(to),
                      "hasMessageId": bool(message_id)}))
    return {
        "sent": True,
        "provider": "whatsapp-cloud-api",
        "messageId": message_id,
        "recipientMasked": mask_phone(to),
    }
