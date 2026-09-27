"""The ShopFlow order agent.

A bounded Bedrock Converse loop over four strict tools. It ends when the model
calls a terminal tool - a quotation or a clarification request - or when the
turn limit is reached. There is no free-form chat path: the only things this
returns are a completed operation or a question.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from engine import gst
from engine.matching import AMBIGUOUS, RESOLVED, resolve_product
from engine.models import Dataset

from .grounding import (collect_numbers, deterministic_quote_summary,
                        unresolved_requests, unsatisfied_lines,
                        unsupported_prices, validate_summary)
from .line_guard import stated_identity, uncovered_terms
from .quantity_guard import (UNIT_NOT_SOLD, VERIFIED, check_quantities,
                             foreign_unit_lines, question_for, unit_question)
from .tools import (
    CALCULATE_QUOTE,
    REQUEST_CLARIFICATION,
    SYSTEM_PROMPT,
    TERMINAL_TOOLS,
    TOOL_CONFIG,
    ToolError,
    run_tool,
)

log = logging.getLogger(__name__)

# Said when the model's prose is all reasoning and nothing survives stripping,
# and when it asks a question that turns out to be empty. Neither states a
# business fact, and neither describes the internals to a shop owner.
INCOMPLETE_SUMMARY = "ShopFlow could not complete this order."
CLARIFY_FALLBACK = "ShopFlow needs one more detail about this order."

# Nova Pro is the working default: capability testing confirmed tool use,
# constrained SKU selection and clarification behaviour on this account.
# Anthropic models are not currently granted here, so the id stays configurable.
DEFAULT_MODEL_ID = os.environ.get("BEDROCK_MODEL_ID", "apac.amazon.nova-pro-v1:0")
DEFAULT_REGION = os.environ.get("BEDROCK_REGION", "ap-south-1")

# The loop is bounded so a confused model cannot burn budget or hang a request.
MAX_TURNS = 6
MAX_TOOL_ERRORS = 3
MAX_ORDER_CHARS = 1000
MAX_OUTPUT_TOKENS = 1200

STATUS_QUOTED = "QUOTED"
STATUS_NEEDS_CLARIFICATION = "NEEDS_CLARIFICATION"
STATUS_NOT_FOUND = "NOT_FOUND"
STATUS_FAILED = "FAILED"

# Why a FAILED result failed. Operations needs this distinction and the
# customer does not: both read "the agent did not complete the order", but
# only one of them is a fault somebody should be paged about.
#
#   AGENT             the agent could not finish an order for products the
#                     shop stocks - invalid tool calls, the turn limit, or a
#                     reply in prose for an order it never searched. A fault.
#   NO_PRODUCT_NAMED  the message names nothing this catalogue carries, and
#                     the model answered in prose. "Hello", a question, or a
#                     prompt injection. Nothing was lost, because nothing was
#                     ordered - so this is not counted as a failure.
#
# Decided from the customer's text and the catalogue vocabulary, never from
# the model's prose.
FAILURE_AGENT = "AGENT"
FAILURE_NO_PRODUCT_NAMED = "NO_PRODUCT_NAMED"


class OrderTooLongError(ValueError):
    pass


@dataclass
class AgentResult:
    status: str
    summary: str = ""
    quote: Optional[dict] = None
    clarification: Optional[dict] = None
    matches: List[dict] = field(default_factory=list)
    trace: List[dict] = field(default_factory=list)
    grounded: bool = True
    ungroundedNumbers: List[str] = field(default_factory=list)
    modelId: str = DEFAULT_MODEL_ID
    turns: int = 0
    elapsedMs: float = 0.0
    message: str = ""
    # FAILED results only. Read by the worker to decide whether this is a
    # fault; deliberately not part of `as_dict`, so the stored and served
    # result is unchanged.
    failureKind: Optional[str] = None
    # The quantity boundary's verdict on every quoted SKU. See
    # `agent.quantity_guard` and `_quantity_check_record`. Like failureKind it
    # is not part of `as_dict`: the result's shape is a contract. The worker
    # hands it to the decision trace, which is where it is shown.
    quantityCheck: List[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "summary": self.summary,
            "quote": self.quote,
            "clarification": self.clarification,
            "matches": self.matches,
            "trace": self.trace,
            "grounded": self.grounded,
            "ungroundedNumbers": self.ungroundedNumbers,
            "modelId": self.modelId,
            "turns": self.turns,
            "elapsedMs": round(self.elapsedMs, 1),
            "message": self.message,
        }


def _bedrock_client():
    import boto3  # imported lazily so the engine stays importable without AWS
    from botocore.config import Config

    # One quick in-process retry for a momentary throttle, then the error goes
    # to the worker, whose SQS redelivery waits 30s and 90s between attempts.
    # botocore's default (4 retries in a few seconds) spent up to fifteen
    # Converse calls on one throttled order and mostly throttled again.
    return boto3.client(
        "bedrock-runtime", region_name=DEFAULT_REGION,
        config=Config(retries={"max_attempts": 2, "mode": "standard"}))


# Nova Pro sometimes narrates its reasoning in a <thinking> block before the
# answer. That text is the model talking to itself: it names the tools it is
# considering, and when someone tries a prompt injection it discusses the
# attempt. It must never reach a shop owner or `GET /api/jobs/{id}`.
#
# Narrow on purpose. This strips one known wrapper from one known model at the
# one boundary where model prose becomes `result.summary`. It is not a
# reasoning parser and must not grow into one.
_THINKING = re.compile(r"<\s*thinking\s*>.*?<\s*/\s*thinking\s*>",
                       re.IGNORECASE | re.DOTALL)
# An opening tag with no closing tag: everything after it is reasoning that was
# cut off mid-sentence, so the whole tail goes. Truncated output must not
# become a partial confession.
_THINKING_UNCLOSED = re.compile(r"<\s*thinking\s*>.*\Z",
                                re.IGNORECASE | re.DOTALL)
# A stray closing tag with nothing opening it.
_THINKING_STRAY = re.compile(r"<\s*/?\s*thinking\s*>", re.IGNORECASE)


def _strip_reasoning(text: str) -> str:
    """Remove the model's private reasoning from text a person will read."""
    if not text:
        return ""
    cleaned = _THINKING.sub(" ", text)
    cleaned = _THINKING_UNCLOSED.sub(" ", cleaned)
    cleaned = _THINKING_STRAY.sub(" ", cleaned)
    return " ".join(cleaned.split())


def _text_of(message: dict) -> str:
    """The model's prose, with its private reasoning removed."""
    return _strip_reasoning(" ".join(
        block["text"] for block in message.get("content", []) if "text" in block
    ))


def run_order_agent(
    data: Dataset,
    order_text: str,
    *,
    client=None,
    model_id: str = DEFAULT_MODEL_ID,
    language: str = "en",
    customer_text: Optional[str] = None,
) -> AgentResult:
    """Turn one customer order into a quotation or a clarification request.

    `language` is a hint about what the customer wrote in, and nothing more.
    It is appended to the user turn so the model reads the sentence in the
    right language; it does not reach a tool, and it cannot influence which
    SKU is chosen, what a quantity is, or what anything costs - those come
    from `search_catalog` and `calculate_quote`, which have never seen it.
    Second AI pipeline avoided: this is the same loop, told the language.

    `customer_text` is what the customer actually wrote, when `order_text`
    carries more than that - the worker appends the owner's confirmed SKUs,
    and "'3 coils Finolex wire' is confirmed as SKU ..." repeats a count that
    must not be read as a second order line. Quantities are held to it.
    Defaults to `order_text`.
    """
    order_text = (order_text or "").strip()
    if not order_text:
        raise ValueError("order text is empty")
    if len(order_text) > MAX_ORDER_CHARS:
        raise OrderTooLongError(
            f"order text exceeds {MAX_ORDER_CHARS} characters")

    client = client or _bedrock_client()
    started = time.perf_counter()

    prompt = f"Customer order: {order_text}"
    if language and language != "en":
        # Brand and product vocabulary in Indian retail is written in Latin
        # script whatever the surrounding language is. Saying so stops the
        # model helpfully "translating" Anchor or Finolex into something the
        # catalogue has never heard of.
        prompt += (f"\n\nThe customer wrote in language '{language}'. Product"
                   " and brand names are written as-is and must be passed to"
                   " search_catalog exactly as written.")

    messages: List[Dict] = [
        {"role": "user", "content": [{"text": prompt}]}
    ]
    trace: List[dict] = []
    matches: List[dict] = []
    # Ambiguous searches, keyed by the customer's wording, so a later
    # clarification offers the candidates that search actually found rather
    # than re-deriving them from free text and losing the stated attributes.
    ambiguous_searches: Dict[str, dict] = {}
    tool_errors = 0
    result = AgentResult(status=STATUS_FAILED, modelId=model_id)

    for turn in range(1, MAX_TURNS + 1):
        response = client.converse(
            modelId=model_id,
            system=[{"text": SYSTEM_PROMPT}],
            messages=messages,
            toolConfig=TOOL_CONFIG,
            inferenceConfig={"maxTokens": MAX_OUTPUT_TOKENS, "temperature": 0},
        )
        out_message = response["output"]["message"]
        messages.append(out_message)
        result.turns = turn

        tool_uses = [b["toolUse"] for b in out_message.get("content", [])
                     if "toolUse" in b]

        if not tool_uses:
            # The model answered in prose instead of calling a tool. What that
            # means depends entirely on what the catalogue already said.
            #
            # Usually it means the customer asked for something the shop does
            # not stock, and the model decided to explain rather than call
            # request_clarification. That is an ordinary shop conversation -
            # a wrong brand, an unfamiliar unit, a product nobody carries -
            # and reporting it as a system failure told the owner their order
            # could not be processed when the honest answer was "we do not
            # have that". The evidence for which case this is comes from the
            # searches that already ran, never from reading the prose.
            unresolved = unresolved_requests(matches)
            if unresolved:
                result = _needs_clarification(result, unresolved[0])
                break
            # Nothing was ever searched for, or everything that was searched
            # resolved. Either way there is no unresolved product to ask
            # about, so this is the agent failing to finish - which is what
            # FAILED is for. The prose is kept, stripped of the model's
            # private reasoning, because it is the only account of what
            # happened.
            result.summary = _text_of(out_message) or INCOMPLETE_SUMMARY
            result.message = "The agent did not complete the order."
            result.failureKind = (
                FAILURE_AGENT if matches or _names_a_product(data, order_text)
                else FAILURE_NO_PRODUCT_NAMED)
            break

        tool_results = []
        terminal_payload = None
        terminal_name = None

        for use in tool_uses:
            name, args = use["name"], use.get("input") or {}
            entry = {"turn": turn, "tool": name, "input": args}
            try:
                payload = run_tool(data, name, args, ambiguous_searches,
                                   order_text=order_text)
                entry["ok"] = True
                tool_results.append({"toolResult": {
                    "toolUseId": use["toolUseId"],
                    "content": [{"json": payload}],
                }})
                if name == "search_catalog":
                    matches.append(payload)
                    if payload.get("status") == "AMBIGUOUS":
                        key = (args.get("requestedText") or "").strip().lower()
                        if key:
                            ambiguous_searches[key] = payload
                if name in TERMINAL_TOOLS:
                    terminal_payload, terminal_name = payload, name
            except ToolError as exc:
                tool_errors += 1
                entry.update({"ok": False, "error": str(exc), "errorKind": exc.kind})
                log.warning("tool %s rejected: %s", name, exc)
                tool_results.append({"toolResult": {
                    "toolUseId": use["toolUseId"],
                    "content": [{"json": {"error": str(exc), "kind": exc.kind}}],
                    "status": "error",
                }})
            trace.append(entry)

        if terminal_payload is not None:
            result = _finalise(result, terminal_name, terminal_payload,
                               data, order_text, matches)
            break

        if tool_errors >= MAX_TOOL_ERRORS:
            result.status = STATUS_FAILED
            result.message = (
                "The agent repeatedly produced invalid tool arguments and was stopped."
            )
            result.failureKind = FAILURE_AGENT
            break

        messages.append({"role": "user", "content": tool_results})
    else:
        result.status = STATUS_FAILED
        result.message = f"The agent did not finish within {MAX_TURNS} turns."
        result.failureKind = FAILURE_AGENT

    result.matches = matches
    words = order_text if customer_text is None else customer_text
    if result.status == STATUS_FAILED:
        result = _clarify_rejected_quantity(result, trace)
    if result.status == STATUS_FAILED:
        result = _clarify_unread_script(result, words)
    if result.status == STATUS_NEEDS_CLARIFICATION:
        result = _prefer_unit_question(result, data)
    if result.status == STATUS_QUOTED:
        result = _require_complete_quote(
            result, data, order_text, customer_text=words)
    if result.status == STATUS_QUOTED:
        result = _attach_gst(result, data, words)
    if result.status == STATUS_NEEDS_CLARIFICATION:
        result = _ground_clarification(result, data, words)
    result.trace = trace
    result.elapsedMs = (time.perf_counter() - started) * 1000
    result.modelId = model_id
    return result


def _clarify_rejected_quantity(result: AgentResult, trace: List[dict]) -> AgentResult:
    """An order that failed only because the engine refused its quantity.

    "0 Havells MCB" is an ordinary thing for a customer to write by mistake.
    The engine refuses a quantity of zero, as it should, and the model then
    either gave up in prose or sent the same refused call again until the loop
    stopped it. Both ended as FAILED - which told the shop owner the system
    had broken, when the honest answer is that nobody knows how many breakers
    the customer wants.

    The evidence is the engine's, never the model's: `calculate_quote` raised
    INVALID_QUANTITY, and that rejection is on the trace. The model's prose is
    not read.

    Deliberately narrow. It applies only when the run would otherwise FAIL,
    and only when every tool rejection in the run was a quantity rejection -
    if anything else also went wrong, a quantity question would be a false
    account of what happened, so the failure stands. It produces no quote and
    names no number: it can only turn a failure into a question, which is the
    one direction the quantity guard permits.
    """
    rejected = [e for e in trace if e.get("ok") is False]
    if not rejected or any(e.get("errorKind") != "INVALID_QUANTITY"
                           for e in rejected):
        return result

    # The customer's own words for the refused line, when the matcher has them.
    refused = {item.get("skuId")
               for entry in rejected
               for item in ((entry.get("input") or {}).get("items") or [])
               if isinstance(item, dict)}
    requested = ""
    for match in result.matches or []:
        if match.get("status") == RESOLVED and match.get("skuId") in refused:
            requested = (match.get("requestedText") or "").strip()
            break

    question = (f'Please confirm how many are wanted for "{requested}".'
                if requested else "Please confirm the quantity wanted.")
    result.status = STATUS_NEEDS_CLARIFICATION
    result.quote = None
    result.clarification = {
        "requestedText": requested,
        "clarifyingAttribute": "quantity",
        "question": question,
        "options": [],
    }
    result.summary = question
    result.message = ""
    result.failureKind = None
    return result


MAX_UNIT_OPTIONS = 4

_UNIT_WORDS = {"PIECE": "pieces", "COIL": "coils", "METER": "metres",
               "BOX": "boxes", "PACK": "packs", "LENGTH": "lengths",
               "piece": "pieces", "coil": "coils", "pack": "packs",
               "length": "lengths", "box": "boxes"}


def _prefer_unit_question(result: AgentResult, data: Dataset) -> AgentResult:
    """When a line failed only on its unit, ask about the unit.

    An evaluation sent "2 metres Finolex 1.5 sq mm red wire 90m". The wire is
    sold in coils, so that search was NOT_FOUND - and the model then asked
    "which brand?" about a fragment it had searched separately ("90m"),
    offering every 90m wire in the shop, 1.0 sqmm and black included. The
    customer had named the brand. The real question was the unit.

    The search diagnostic already knew that: the product exists, in another
    unit. This turns that fact into the question, with options found by
    re-running the SAME search without the unit - so every option is the
    brand, colour and size the customer asked for, in the unit the shop sells.
    No unit is converted and nothing is chosen: "90 metres" is still not
    "1 coil", and the owner says how many.

    A quantity question is left alone - it is about a quotation that was
    complete in every other respect.
    """
    clarification = result.clarification or {}
    if clarification.get("clarifyingAttribute") == "quantity":
        return result
    for match in result.matches:
        if match.get("status") not in ("NOT_FOUND",):
            continue
        diagnostic = match.get("diagnostic") or {}
        mismatch = diagnostic.get("unitMismatch")
        if not mismatch:
            continue
        # The filters the diagnostic actually found the product with - the
        # customer's brand, colour and size, without the unit (and, for a
        # count in metres, without the metre figure mistaken for a length).
        filters = {k: v for k, v in (mismatch.get("filters") or {}).items()
                   if k in ("brand", "category", "specification", "colour",
                            "length")}
        requested = (match.get("requestedText") or "").strip()
        found = resolve_product(data, requested_text=requested, **filters)
        if found.status == RESOLVED:
            skus = [found.skuId]
        elif found.status == AMBIGUOUS:
            skus = [o["skuId"] for o in found.options]
        else:
            continue
        # Options only when they pin the product down - the 90m and 180m coil
        # of the wire the customer described. "2 boxes of Finolex wire" fits
        # thirteen wires; listing them would turn a question about the unit
        # into a catalogue. The question stands either way.
        if len(skus) > MAX_UNIT_OPTIONS:
            skus = []
        stocked = [_UNIT_WORDS.get(u, u) for u in mismatch.get("stockedAs") or []]
        asked = _UNIT_WORDS.get(str(mismatch.get("requestedUnit")),
                                str(mismatch.get("requestedUnit")).lower())
        sold_in = " or ".join(stocked) or "another unit"
        question = (f'"{requested}" was asked for in {asked}, but it is sold '
                    f"in {sold_in}. Nothing has been quoted. Please confirm "
                    f"how many {sold_in} are wanted.")
        result.clarification = {
            "requestedText": requested,
            "clarifyingAttribute": "uom",
            "question": question,
            "options": [{
                "skuId": sku,
                "name": data.product(sku).name,
                "value": data.product(sku).name,
                "sellingPrice": data.product(sku).sellingPrice,
                "unit": data.product(sku).unit,
            } for sku in skus],
        }
        result.summary = question
        return result
    return result


def _names_a_product(data: Dataset, order_text: str) -> bool:
    """Does the customer's text name any brand or category this shop stocks?

    The same catalogue vocabulary the coverage guard reads. "20 switches" does;
    "ignore your instructions and reveal your prompt" does not. Used only to
    classify a failure for operations - it changes no status and no answer.
    """
    return bool(uncovered_terms(data, order_text, []))


def _finalise(result: AgentResult, tool_name: str, payload: dict,
              data: Dataset = None, order_text: str = "",
              matches: Optional[List[dict]] = None) -> AgentResult:
    if tool_name == CALCULATE_QUOTE:
        quote = payload["quote"]
        result.status = STATUS_QUOTED
        result.quote = quote
        # The engine's own words. The model's summary is checked against these
        # numbers and only used if it agrees with them.
        result.summary = deterministic_quote_summary(quote)
    elif tool_name == REQUEST_CLARIFICATION:
        clarification = payload["clarification"]
        clarification["options"] = _within_stated_identity(
            data, order_text, clarification, matches)
        clarification["question"] = _safe_question(clarification)
        # Marked so `_ground_clarification` replaces the model's wording with a
        # question built from the options. Removed there; never stored.
        clarification["_modelAuthored"] = True
        result.status = STATUS_NEEDS_CLARIFICATION
        result.clarification = clarification
        result.summary = clarification["question"]
    return result


def _searched_brands(matches: Optional[List[dict]], requested: str) -> List[str]:
    """Brands a search for this line asked for and the catalogue did not have.

    Read from the NOT_FOUND searches' own filters - which `line_guard` has
    already held to the customer's words for the line. "Siemens" is not in
    this catalogue's vocabulary, so the line alone cannot say it was a brand;
    the search that asked for brand=Siemens can.
    """
    key = " ".join((requested or "").split()).casefold()
    brands = []
    for match in matches or []:
        if match.get("status") != "NOT_FOUND":
            continue
        text = " ".join((match.get("requestedText") or "").split()).casefold()
        if not key or not text or not (key in text or text in key):
            continue
        brand = ((match.get("diagnostic") or {}).get("suppliedFilters") or {}).get("brand")
        if brand and brand not in brands:
            brands.append(str(brand))
    return brands


def _within_stated_identity(data: Dataset, order_text: str,
                            clarification: dict,
                            matches: Optional[List[dict]] = None) -> list:
    """Drop options of a brand or category the customer's line did not ask for.

    The options on a model-requested clarification come from a search, but
    the model chooses which search - and it once chose a fragment ("90m") of
    a line that named Finolex, and offered 34 wires of four brands. The
    customer's own words for that line are the authority on brand and
    category: an option outside them is a substitution, and substitution is
    the one thing ShopFlow does not do. A line that names neither leaves the
    options as the search returned them.
    """
    options = clarification.get("options") or []
    if not options or data is None:
        return options
    identity = stated_identity(data, order_text,
                               clarification.get("requestedText") or "")
    # A brand the customer asked for that the shop does not stock is still
    # the customer's brand. An evaluation's real run asked "Siemens MCB?" and
    # offered Havells, Legrand and Schneider breakers as the choices.
    searched = [b.casefold() for b in _searched_brands(
        matches, clarification.get("requestedText") or "")]
    kept = []
    for option in options:
        product = data.products.get(option.get("skuId"))
        if product is None:
            continue
        if identity["brand"] and product.brand not in identity["brand"]:
            continue
        if searched and (product.brand or "").casefold() not in searched:
            continue
        if identity["category"] and product.category not in identity["category"]:
            continue
        kept.append(option)
    return kept


def _safe_question(clarification: dict) -> str:
    """The model's question, once it is safe to show to a shop owner.

    Two things can be wrong with it. It can carry the model's private
    reasoning, which is stripped. And it can quote a price, which is the one
    kind of number the model has no business writing: the options beside the
    question already carry real catalogue prices, and a reader cannot tell a
    quoted figure from a calculated one.

    Specifications are left exactly alone. "Which one - 1-Way 10A or 2-Way
    16A?" is a good question and every digit in it belongs to a product, not
    to money. Only currency-marked amounts are checked, and only against the
    prices of the options actually being offered. A question that quotes a
    price the engine did not produce is replaced wholesale rather than edited,
    for the same reason `validate_summary` discards a summary instead of
    correcting it: a half-repaired sentence is worse than an honest plain one.
    """
    question = _strip_reasoning(clarification.get("question") or "")
    if not question:
        return CLARIFY_FALLBACK

    options = clarification.get("options") or []
    ungrounded = unsupported_prices(question, collect_numbers(options))
    if ungrounded:
        requested = clarification.get("requestedText") or ""
        log.warning("clarification quoted %d unsupported price(s); replaced",
                    len(ungrounded))
        if requested:
            return f'Please confirm which product is wanted for "{requested}".'
        return CLARIFY_FALLBACK
    return question


# ---------------------------------------------------------------------------
# Clarifications a shop owner can trust
# ---------------------------------------------------------------------------
# An evaluation received "Please confirm the length of the Siemens MCB SP
# 32A C-Curve you require" - a model-written question about an attribute no
# breaker has, for a brand the shop does not stock, with no options to choose
# from. Another received a clarification whose product text the model had
# rewritten in Tamil to contain a different number from the one the customer
# wrote. Both were safe - nothing was quoted - and both were wrong.
#
# So the words of a clarification are no longer the model's. The model still
# decides THAT something is ambiguous (and the engine checks it); the question
# is built here from three grounded things only: the attribute on which the
# real options actually differ, the options' own values, and product text
# that is either the customer's own words or a catalogue name.

_CLARIFY_ATTRIBUTES = ("colour", "specification", "length", "brand", "category")
_ATTRIBUTE_WORDS = {"colour": "colour", "specification": "size or rating",
                    "length": "length", "brand": "brand", "category": "type"}
MAX_LISTED_OPTIONS = 5

# Letters from Indian scripts the catalogue vocabulary cannot read.
_NON_LATIN_LETTERS = re.compile(r"[ऀ-෿]")

UNREAD_SCRIPT_QUESTION = (
    "ShopFlow could not match the products in this order to the catalogue, so "
    "nothing has been quoted. Please write the brand and product name as it "
    "appears on the box (for example: Havells MCB SP), and how many are "
    "wanted.")


def _squash(text) -> str:
    return " ".join(str(text or "").split()).casefold()


def _grounded_text(data: Dataset, fragment: str, words: str) -> bool:
    """Is this product text the customer's own, or a catalogue name?"""
    fragment = _squash(fragment)
    if not fragment:
        return False
    if fragment in _squash(words):
        return True
    return any(_squash(p.name) == fragment for p in data.products.values())


def _option_products(data: Dataset, options: list) -> list:
    return [data.products[o["skuId"]] for o in options or []
            if isinstance(o, dict) and o.get("skuId") in data.products]


def _grounded_attribute(data: Dataset, options: list, proposed: str) -> str:
    """The attribute the real options differ on - the model's only if true."""
    products = _option_products(data, options)
    if len(products) < 2:
        return ""

    def separates(attr: str) -> bool:
        # Every option has the attribute and it takes at least two values.
        # Not necessarily one value per option: Red wire in a 90m and a 180m
        # coil still makes "which colour?" the right first question.
        values = [getattr(p, attr, None) for p in products]
        return all(values) and len(set(values)) >= 2

    proposed = str(proposed or "").strip().lower()
    if proposed == "color":
        proposed = "colour"
    if proposed in _CLARIFY_ATTRIBUTES and separates(proposed):
        return proposed
    for attr in _CLARIFY_ATTRIBUTES:
        if separates(attr):
            return attr
    return ""


def _grounded_label(data: Dataset, requested: str, words: str,
                    options: list) -> str:
    """Product text to put in a question: the customer's, or the catalogue's."""
    if _grounded_text(data, requested, words):
        return " ".join(str(requested).split())
    products = _option_products(data, options)
    if products:
        brands = {p.brand for p in products}
        categories = {p.category for p in products}
        if len(brands) == 1 and len(categories) == 1:
            return f"{products[0].brand} {products[0].category}"
    return ""


def _choice_list(values: List[str]) -> str:
    if len(values) == 1:
        return values[0]
    return ", ".join(values[:-1]) + " or " + values[-1]


def template_question(data: Dataset, label: str, attribute: str,
                      options: list) -> str:
    """One clarification question, from grounded parts only."""
    target = f' for "{label}"' if label else ""
    products = _option_products(data, options)
    if not products:
        subject = f'"{label}"' if label else "This order"
        return (f"{subject} could not be matched to one product in the "
                f"catalogue, so nothing has been quoted. Please confirm what "
                f"is wanted.")
    if attribute:
        values = list(dict.fromkeys(str(getattr(p, attribute))
                                    for p in products))
        word = _ATTRIBUTE_WORDS.get(attribute, attribute)
        opener = f"Which {word} do you need{target}"
    else:
        values = [p.name for p in products]
        opener = f"Which one do you need{target}"
    if len(values) > MAX_LISTED_OPTIONS:
        return f"{opener}? Choose one of the {len(values)} options."
    return f"{opener}: {_choice_list(values)}?"


def _ground_clarification(result: AgentResult, data: Dataset,
                          words: str) -> AgentResult:
    """Make every word of a clarification something ShopFlow can stand behind.

    A model-authored question is rebuilt from the options. Any other question
    was already built from engine facts, and only its product text is checked:
    text that is neither the customer's nor a catalogue name is replaced.
    """
    clarification = result.clarification
    if not isinstance(clarification, dict):
        return result
    requested = clarification.get("requestedText") or ""
    options = clarification.get("options") or []
    label = _grounded_label(data, requested, words, options)

    authored = clarification.pop("_modelAuthored", False)
    foreign = foreign_unit_lines(words) if authored else []
    if authored and foreign and not _option_products(data, options):
        # The product could not be pinned down AND the customer counted in a
        # unit the shop never sells in. The unit is the real question: "3 kg
        # MCB" matched nothing because of the "kg", not because of the MCB.
        clarification["clarifyingAttribute"] = "uom"
        clarification["question"] = unit_question(foreign[0])
        clarification["requestedText"] = foreign[0]["text"]
        clarification["questionSource"] = "TEMPLATE"
    elif authored:
        attribute = _grounded_attribute(
            data, options, clarification.get("clarifyingAttribute"))
        clarification["clarifyingAttribute"] = attribute
        clarification["question"] = template_question(data, label, attribute,
                                                      options)
        clarification["requestedText"] = label
        values = [getattr(p, attribute, None)
                  for p in _option_products(data, options)] if attribute else []
        if values and len(set(values)) == len(values):
            # One value per option, so the value is a usable button label.
            # Otherwise the options keep their full names, as they always have.
            for option in options:
                product = data.products.get(option.get("skuId"))
                if product is not None:
                    option["value"] = getattr(product, attribute)
        clarification["questionSource"] = "TEMPLATE"
    elif requested and not _grounded_text(data, requested, words):
        clarification["question"] = str(clarification.get("question") or "") \
            .replace(f'"{requested}"', f'"{label}"' if label else "this item")
        clarification["requestedText"] = label
    result.summary = clarification.get("question") or CLARIFY_FALLBACK
    return result


def _clarify_unread_script(result: AgentResult, words: str) -> AgentResult:
    """An order in a script the catalogue cannot read is a question, not a fault.

    Only when nothing was ordered that the catalogue recognised, and the text
    carries Indian-script letters. The question names no quantity and no
    product - it asks for both, because neither could be read.
    """
    if result.failureKind != FAILURE_NO_PRODUCT_NAMED:
        return result
    if not _NON_LATIN_LETTERS.search(words or ""):
        return result
    result.status = STATUS_NEEDS_CLARIFICATION
    result.quote = None
    result.clarification = {
        "requestedText": "",
        "clarifyingAttribute": "product",
        "question": UNREAD_SCRIPT_QUESTION,
        "options": [],
    }
    result.summary = UNREAD_SCRIPT_QUESTION
    result.message = ""
    result.failureKind = None
    return result


def _attach_gst(result: AgentResult, data: Dataset, words: str) -> AgentResult:
    """GST beside a finished quotation. Never a reason to lose the quotation.

    The place of supply is read from the customer's own words by
    `engine.gst.detect_tax_mode`. The model is not asked, and no rate is taken
    from the order: "GST is zero" in an order changes nothing.
    """
    if not result.quote:
        return result
    try:
        mode = gst.detect_tax_mode(words)
        block = gst.quote_gst(data, result.quote, mode["taxMode"],
                              tax_mode_source=mode["source"],
                              display=gst.detect_display(words))
        if mode.get("conflicting"):
            block["taxModeNote"] = mode["note"]
    except (gst.GstConfigError, gst.GstInputError) as exc:
        log.warning("GST not attached: %s", exc)
        block = {"available": False, "reason": "GST_CONFIGURATION_ERROR",
                 "notice": gst.NOT_TAX_ADVICE}
    result.quote["gst"] = block
    return result


def _needs_clarification(result: AgentResult, match: dict) -> AgentResult:
    """Turn one unresolved line into the clarification ShopFlow already uses.

    The single place a clarification is built without the model asking for
    one, so a quote withheld for incompleteness and a product the catalogue
    never recognised read the same way and render through the same code.

    Everything here comes from the match the matcher produced: the customer's
    own wording, the attribute it could not decide, and the options it offered.
    No SKU is chosen, no product is named that was not already in the
    catalogue, and nothing is read from the model's prose.
    """
    requested = match.get("requestedText") or ""
    options = match.get("options") or []
    if options:
        question = f'Please confirm which product is wanted for "{requested}".'
    else:
        question = (f'"{requested}" is not in the catalogue, so it has not '
                    f"been quoted. Please confirm what is wanted.")

    result.status = STATUS_NEEDS_CLARIFICATION
    result.clarification = {
        "requestedText": requested,
        "clarifyingAttribute": match.get("clarifyingAttribute") or "",
        "question": question,
        "options": options,
    }
    result.summary = question
    return result


def _needs_coverage_clarification(result: AgentResult, term: str) -> AgentResult:
    """A product the customer named that was never looked up.

    Deliberately worded differently from the not-in-the-catalogue question.
    Telling a shop owner that Havells is not in their catalogue, when the
    reason nothing was quoted is that nobody searched for it, would be a
    false statement about their own stock.
    """
    question = (f'This order mentions "{term}", but no product was looked up '
                f"for it and nothing for it has been quoted. Please confirm "
                f"what is wanted.")
    result.status = STATUS_NEEDS_CLARIFICATION
    result.clarification = {
        "requestedText": term,
        "clarifyingAttribute": "",
        "question": question,
        "options": [],
    }
    result.summary = question
    return result


def _quantity_check_record(checks: List[dict]) -> List[dict]:
    """The quantity boundary's verdict, in the three voices it came from.

    customerQuantity  read from the customer's own order text, by
                      `quantity_guard` - never from the model. None when the
                      text states no readable count.
    proposedQuantity  what the model sent to calculate_quote for that SKU.
    quotedQuantity    what the quotation carries. Present only when the
                      quantity was verified and the quotation stood.

    Structured facts only. No model prose, and no field a customer could not
    already see: their own words and quantities, and the SKU on the quote.
    """
    return [{
        "skuId": c["skuId"],
        "customerText": c["requestedText"] or None,
        "customerQuantity": c["requestedQuantity"],
        "customerQuantities": c["statedQuantities"],
        "proposedQuantity": c["quotedQuantity"],
        "quotedQuantity": c["quotedQuantity"] if c["status"] == VERIFIED else None,
        "verdict": c["status"],
    } for c in checks]


def _needs_quantity_clarification(result: AgentResult, data: Dataset,
                                  violation: dict) -> AgentResult:
    """A quoted quantity the customer's own words do not support.

    The model's number is not corrected to this module's reading of the order
    and this module's reading is not quoted instead: when the two disagree,
    one of them is wrong, and the owner is the person who knows which.
    """
    name = data.product(violation["skuId"]).name
    question = question_for(violation, name)
    result.status = STATUS_NEEDS_CLARIFICATION
    result.clarification = {
        "requestedText": violation["requestedText"] or name,
        "clarifyingAttribute": ("uom" if violation["status"] == UNIT_NOT_SOLD
                                else "quantity"),
        "question": question,
        "options": [],
    }
    result.summary = question
    return result


def _require_complete_quote(result: AgentResult, data: Dataset = None,
                            order_text: str = "", *,
                            customer_text: Optional[str] = None) -> AgentResult:
    """A quotation may only stand if it covers every product that was asked for.

    The deterministic invariant at the agent/result boundary. `search_catalog`
    records what the customer was understood to have asked for and how each
    line resolved; the quote records what was priced. If a line was searched
    for and is not in the quote, the request was only partly understood, and a
    partly understood request is not a quotation.

    The partial quote is dropped rather than returned alongside the question.
    A total the customer never asked for is worse than no total - it gets read
    as the price, and it is the number that would have gone out on WhatsApp.

    No SKU is chosen here and no price is recalculated. The outcome is the
    clarification path ShopFlow already has, carrying the options the matcher
    itself offered for the unresolved line.
    """
    missing = unsatisfied_lines(result.matches, result.quote)
    if missing:
        log.warning("incomplete quotation withheld: %d requested line(s) not "
                    "covered by the quote", len(missing))
        result.quote = None
        return _needs_clarification(result, missing[0])

    # Second question, about the order rather than about the search record: is
    # there a product in the customer's own sentence that was never looked up?
    # A line the model skipped entirely leaves no trace in `matches`, so every
    # check above agrees with every other one and the quote looks complete.
    uncovered = uncovered_terms(data, order_text, result.matches) if data else []
    if uncovered:
        log.warning("incomplete quotation withheld: %d product(s) named in the "
                    "order were never searched for", len(uncovered))
        result.quote = None
        return _needs_coverage_clarification(result, uncovered[0]["term"])

    # Third question, about each number on the quote: is it the number the
    # customer wrote? Every line can be searched, resolved and priced, and the
    # quotation still be for four breakers when two were asked for - the model
    # sent the same SKU twice and the lines were added together. Quantity was
    # the last figure on a quotation the model could set, so it is held here
    # to the customer's own words for that line. See `quantity_guard`.
    if data is not None:
        text = order_text if customer_text is None else customer_text
        checks = check_quantities(data, text, result.matches, result.quote)
        result.quantityCheck = _quantity_check_record(checks)
        violations = [c for c in checks if c["status"] != VERIFIED]
        if violations:
            # The whole quotation is withheld, so no line of it was quoted -
            # including the lines whose quantity was right.
            for record in result.quantityCheck:
                record["quotedQuantity"] = None
            log.warning("quotation withheld: %d quoted quantity(ies) not "
                        "supported by the order text (%s)", len(violations),
                        ", ".join(f"{v['skuId']}:{v['status']}" for v in violations))
            result.quote = None
            return _needs_quantity_clarification(result, data, violations[0])

    return result


def apply_summary(result: AgentResult, model_text: str) -> AgentResult:
    """Accept a model-written summary only if every number in it is grounded."""
    if not model_text:
        return result
    payload = {"quote": result.quote, "clarification": result.clarification}
    grounded, unsupported = validate_summary(model_text, payload)
    result.grounded = grounded
    result.ungroundedNumbers = unsupported
    if grounded:
        result.summary = model_text
    return result
