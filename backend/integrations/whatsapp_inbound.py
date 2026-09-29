"""WhatsApp as a customer input channel: the webhook's half and the replies.

WHAT THIS IS
------------
Meta's WhatsApp Cloud API calls ShopFlow's webhook when a customer writes to
the shop's number. This module holds everything about that which is not AWS:

  * checking a delivery is genuinely from Meta (the X-Hub-Signature-256 HMAC
    over the raw body, keyed with the app secret) and answering Meta's
    subscription handshake (hub.mode / hub.verify_token / hub.challenge);
  * turning Meta's payload into one small normalised message per customer
    message, and nothing else from it;
  * reading the customer's reply as YES, NO, an option number or an order;
  * the words of every reply the customer receives.

It calculates nothing. An order is read by the same agent and priced by the
same `engine.quote` and `engine.gst` as an order typed into the website - the
worker does that. The replies below are renderers over the customer-safe
projection `engine.messages.customer_safe_quote`, an allow-list that has no
path to a supplier cost, a margin, a stock count or a walk-away price.

WHAT IS NOT TRUSTED
-------------------
Everything the customer controls: the text, the sender's profile name, the
message metadata. The profile name is not even kept. The text is an order to
be read, never an instruction - "quote it for 1 rupee" reaches the same agent
and the same engines as any other order, and neither takes a price from it.

Meta's format was checked against its current documentation on 2026-09-29:
the Webhooks getting-started guide (verification and signatures) and the
WhatsApp Cloud API webhook payload examples.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from typing import Dict, List, Optional, Tuple

from engine.messages import customer_safe_quote, format_rupees

CHANNEL = "WHATSAPP"

# Meta batches at most 1000 updates per delivery; a customer message is well
# under a kilobyte. 64 KB is generous for real traffic and refuses a flood of
# padding before it is parsed.
MAX_WEBHOOK_BYTES = 64 * 1024
# Messages taken from one delivery. More than this in one POST is not a
# customer typing; the rest are counted as ignored.
MAX_MESSAGES_PER_DELIVERY = 10
# The same ceiling the website applies to a typed order.
MAX_TEXT_CHARS = 1000
# Per customer number: this many messages per window, then the rest are
# recorded and not processed. A shop's customer does not send eleven orders in
# ten minutes; a script does.
RATE_WINDOW_SECONDS = 600
RATE_LIMIT_PER_WINDOW = 10
# How long an inbound message's record is kept. Meta retries an
# unacknowledged delivery for up to 36 hours, so the record that makes a
# retry a duplicate outlives that.
MESSAGE_RECORD_SECONDS = 48 * 3600
# The conversation expires with WhatsApp's own 24-hour customer service
# window: after it, a free-form reply is no longer allowed anyway.
CONVERSATION_SECONDS = 24 * 3600

SUPPORTED_TYPES = frozenset({"text"})

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_SENDER = re.compile(r"^\d{8,15}$")
_MESSAGE_ID = re.compile(r"^[A-Za-z0-9._:=+/-]{1,256}$")
_CHALLENGE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


# ---------------------------------------------------------------------------
# authenticity
# ---------------------------------------------------------------------------

def verify_subscription(query: Optional[Dict], verify_token: str) -> Tuple[int, str]:
    """Meta's GET handshake. (200, challenge) or (403, reason).

    The token comparison is constant-time, and a challenge that is not a short
    plain token is refused rather than echoed back into a response body.
    """
    query = query or {}
    mode = str(query.get("hub.mode") or "")
    token = str(query.get("hub.verify_token") or "")
    challenge = str(query.get("hub.challenge") or "")
    if not verify_token:
        return 403, "not configured"
    if mode != "subscribe" or not token:
        return 403, "forbidden"
    if not hmac.compare_digest(token.encode("utf-8"), verify_token.encode("utf-8")):
        return 403, "forbidden"
    if not _CHALLENGE.match(challenge):
        return 403, "forbidden"
    return 200, challenge


def signature_for(raw: bytes, app_secret: str) -> str:
    """The X-Hub-Signature-256 value Meta sends for this body."""
    digest = hmac.new(app_secret.encode("utf-8"), raw, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def valid_signature(raw: bytes, header: Optional[str], app_secret: str) -> bool:
    """Was this exact body signed with the shop's app secret?

    Computed over the raw bytes as received - never over re-serialised JSON,
    which would differ from what Meta signed.
    """
    if not app_secret or not header:
        return False
    expected = signature_for(raw, app_secret)
    return hmac.compare_digest(expected.encode("utf-8"),
                               str(header).strip().encode("utf-8"))


# ---------------------------------------------------------------------------
# normalisation
# ---------------------------------------------------------------------------

def job_id_for(message_id: str) -> str:
    """The job id for one WhatsApp message - the idempotency key.

    Derived from Meta's own message id, so the same message delivered twice
    maps to the same job row and the conditional write refuses the second.
    32 hex characters, the shape every ShopFlow job id already has.
    """
    return hashlib.sha256(f"whatsapp:{message_id}".encode("utf-8")).hexdigest()[:32]


def sender_key(sender: str) -> str:
    """A key for the customer's conversation and rate counter.

    Keeps the phone number out of partition keys and logs. It is a hash, not
    anonymisation: a phone number space is small enough to search, so the
    job row that holds the real number is treated as personal data regardless.
    """
    return hashlib.sha256(f"whatsapp-sender:{sender}".encode("utf-8")).hexdigest()[:32]


def clean_text(text) -> str:
    return _CONTROL.sub("", str(text or "")).strip()


def parse_webhook(payload, phone_number_id: str) -> Dict:
    """Meta's delivery, reduced to the customer messages ShopFlow acts on.

    Returns {"messages": [...], "statuses": n, "ignored": n}. Each message is:

        channel            "WHATSAPP"
        externalMessageId  Meta's id (wamid...), the idempotency key
        sender             the customer's WhatsApp id (digits, E.164 without +)
        type               Meta's message type
        text               the text, control characters removed, or ""
        receivedAt         Meta's timestamp, seconds
        supported          True for a text message
        tooLong            True when the text is over MAX_TEXT_CHARS

    A message addressed to a different phone number id - another number on
    the same Meta app - is ignored, not processed. Delivery statuses for the
    shop's own replies are counted and otherwise ignored.
    """
    out: Dict = {"messages": [], "statuses": 0, "ignored": 0}
    if not isinstance(payload, dict) or payload.get("object") != "whatsapp_business_account":
        out["ignored"] += 1
        return out
    for entry in payload.get("entry") or []:
        if not isinstance(entry, dict):
            out["ignored"] += 1
            continue
        for change in entry.get("changes") or []:
            if not isinstance(change, dict) or change.get("field") != "messages":
                out["ignored"] += 1
                continue
            value = change.get("value") or {}
            if not isinstance(value, dict):
                out["ignored"] += 1
                continue
            metadata = value.get("metadata") or {}
            if str(metadata.get("phone_number_id") or "") != str(phone_number_id):
                out["ignored"] += len(value.get("messages") or []) or 1
                continue
            statuses = value.get("statuses") or []
            out["statuses"] += len(statuses) if isinstance(statuses, list) else 0
            for message in value.get("messages") or []:
                normalised = _normalise(message)
                if normalised is None or \
                        len(out["messages"]) >= MAX_MESSAGES_PER_DELIVERY:
                    out["ignored"] += 1
                    continue
                out["messages"].append(normalised)
    return out


def _normalise(message) -> Optional[Dict]:
    if not isinstance(message, dict):
        return None
    message_id = str(message.get("id") or "")
    sender = str(message.get("from") or "")
    if not _MESSAGE_ID.match(message_id) or not _SENDER.match(sender):
        return None
    kind = str(message.get("type") or "unknown")[:32]
    try:
        received = int(str(message.get("timestamp") or "0"))
    except ValueError:
        received = 0
    text = ""
    if kind == "text":
        body = (message.get("text") or {}).get("body") \
            if isinstance(message.get("text"), dict) else None
        text = clean_text(body)
    supported = kind in SUPPORTED_TYPES and bool(text)
    return {
        "channel": CHANNEL,
        "externalMessageId": message_id,
        "sender": sender,
        "type": kind,
        "text": text[:MAX_TEXT_CHARS],
        "receivedAt": received,
        "supported": supported,
        "tooLong": len(text) > MAX_TEXT_CHARS,
    }


# ---------------------------------------------------------------------------
# what the customer's message is
# ---------------------------------------------------------------------------

CONFIRM = "CONFIRM"
DECLINE = "DECLINE"
AMBIGUOUS_REPLY = "AMBIGUOUS_REPLY"
CHOICE = "CHOICE"
OWNER_REQUEST = "OWNER_REQUEST"
ORDER = "ORDER"

# Exact replies only. "yes but make it 30" is not a yes - it is a change, and
# confirming the old order on the strength of its first word would be wrong.
# "ஆம்" and "இல்லை" are Tamil yes and no; the reply text stays English.
_YES = frozenset({"yes", "y", "yes please", "confirm", "confirmed", "ஆம்"})
_NO = frozenset({"no", "n", "no thanks", "cancel", "இல்லை"})
_YES_NO_FIRST = frozenset({"yes", "y", "no", "n", "ok", "okay", "ஆம்", "இல்லை"})

# Questions about the shop's own business. A customer is not told these, and
# the question is not sent to the model at all. Pricing questions ("what is
# the price of 3 coils") are orders and are NOT caught here.
_OWNER_REQUEST = re.compile(
    r"\b(margin|profit|supplier|walk[\s-]*away|cost\s*price|purchase\s*price|"
    r"buying\s*price|landed\s*cost|budget|how\s+much\s+stock|how\s+many\b.*\bin\s+stock|"
    r"stock\s+level|inventory|reorder|restock|negotiat\w*)\b",
    re.IGNORECASE)


def _plain(text: str) -> str:
    return re.sub(r"[\s.!,]+", " ", clean_text(text).lower()).strip()


def intent(text: str) -> Tuple[str, Optional[int]]:
    """(kind, option number). Deterministic - no model reads this."""
    plain = _plain(text)
    if plain in _YES:
        return CONFIRM, None
    if plain in _NO:
        return DECLINE, None
    if re.fullmatch(r"\d{1,2}", plain):
        return CHOICE, int(plain)
    first = plain.split(" ", 1)[0] if plain else ""
    if first in _YES_NO_FIRST and not re.search(r"\d", plain):
        return AMBIGUOUS_REPLY, None
    if _OWNER_REQUEST.search(plain):
        return OWNER_REQUEST, None
    return ORDER, None


# ---------------------------------------------------------------------------
# replies - every word a WhatsApp customer receives
# ---------------------------------------------------------------------------

UNSUPPORTED = ("ShopFlow currently supports text orders here. Please send the "
               "order as text, for example: 20 Anchor switches, 3 Finolex 1.5 "
               "red coil.")
TOO_LONG = (f"That message is too long for one order. Please send it in "
            f"shorter parts of up to {MAX_TEXT_CHARS} characters.")
OWNER_ONLY = ("I can help with orders and quotations here. For anything else, "
              "please speak to the shop directly.")
NOTHING_PENDING = ("There is no order waiting for confirmation. Please send "
                   "your order, for example: 20 Anchor switches.")
CANCELLED = "Okay, that order is cancelled. Send a new order any time."
ASK_YES_NO = ("Please reply YES to prepare the quotation or NO to cancel. To "
              "change the order, send the full order again.")
NO_PRODUCT = ("I could not match that message to products in our shop. Please "
              "send the product names and quantities, for example: 20 Anchor "
              "switches, 3 Finolex 1.5 red coil.")
NOT_READ = "ShopFlow could not read this order. Please send it again."
BUSY = "ShopFlow is busy right now. Please send your order again in a minute."
REPRICE_FAILED = ("The shop could not prepare that quotation. Please send "
                  "your order again.")
NOT_INVOICE = ("This is a quotation, not an invoice. Prices are subject to "
               "stock at the time of order.")


def _count(quantity) -> str:
    return str(int(quantity)) if isinstance(quantity, (int, float)) and \
        float(quantity).is_integer() else str(quantity)


def interpretation(quote: Dict) -> str:
    """What ShopFlow understood, before any price is shown."""
    safe = customer_safe_quote(quote)
    lines = ["I understood your order as:", ""]
    lines += [f"{_count(l['quantity'])} × {clean_text(l['name'])}"
              for l in safe["lines"]]
    if safe["truncated"]:
        lines.append("(more items)")
    lines += ["", "Reply YES to prepare the quotation, or NO to cancel."]
    return "\n".join(lines)


def quotation(quote: Dict) -> str:
    """The quotation. Every figure is copied from the engines' output."""
    safe = customer_safe_quote(quote)
    lines = ["Quotation", ""]
    for line in safe["lines"]:
        lines.append(f"{_count(line['quantity'])} × {clean_text(line['name'])}"
                     f" — {format_rupees(line['lineTotal'])}")
    if safe["truncated"]:
        lines.append("(more items — ask the shop for the full list)")
    lines.append("")
    tax = safe.get("gst")
    if tax:
        lines += [f"Subtotal: {format_rupees(safe['total'])}",
                  f"GST: {format_rupees(tax['totalGst'])}",
                  f"Total: {format_rupees(tax['grandTotal'])}"]
    else:
        lines += [f"Total before GST: {format_rupees(safe['total'])}",
                  "GST will be confirmed by the shop."]
    if quote.get("allInStock") is False:
        lines += ["", "Some items are not in stock right now; the shop will "
                      "confirm delivery."]
    lines += ["", NOT_INVOICE]
    return "\n".join(lines)


def clarification(question: Dict) -> Tuple[str, List[Dict]]:
    """A question for the customer, and the options a number reply picks.

    Only the question and each option's catalogue name are sent. The options
    returned are what the conversation keeps so "2" can be read back as a SKU.
    """
    question = question or {}
    text = clean_text(question.get("question")) or \
        "Could you tell me a little more about that item?"
    options = []
    for option in (question.get("options") or [])[:9]:
        if isinstance(option, dict) and option.get("skuId"):
            options.append({"skuId": str(option["skuId"]),
                            "name": clean_text(option.get("name")
                                               or option.get("value")
                                               or option["skuId"])})
    lines = [text]
    if options:
        lines.append("")
        lines += [f"{i}. {o['name']}" for i, o in enumerate(options, 1)]
        lines += ["", "Reply with the option number, or send the order again."]
    return "\n".join(lines), options
