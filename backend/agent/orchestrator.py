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

from engine.models import Dataset

from .grounding import (collect_numbers, deterministic_quote_summary,
                        unresolved_requests, unsatisfied_lines,
                        unsupported_prices, validate_summary)
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

    return boto3.client("bedrock-runtime", region_name=DEFAULT_REGION)


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
) -> AgentResult:
    """Turn one customer order into a quotation or a clarification request.

    `language` is a hint about what the customer wrote in, and nothing more.
    It is appended to the user turn so the model reads the sentence in the
    right language; it does not reach a tool, and it cannot influence which
    SKU is chosen, what a quantity is, or what anything costs - those come
    from `search_catalog` and `calculate_quote`, which have never seen it.
    Second AI pipeline avoided: this is the same loop, told the language.
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
            break

        tool_results = []
        terminal_payload = None
        terminal_name = None

        for use in tool_uses:
            name, args = use["name"], use.get("input") or {}
            entry = {"turn": turn, "tool": name, "input": args}
            try:
                payload = run_tool(data, name, args, ambiguous_searches)
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
            result = _finalise(result, terminal_name, terminal_payload)
            break

        if tool_errors >= MAX_TOOL_ERRORS:
            result.status = STATUS_FAILED
            result.message = (
                "The agent repeatedly produced invalid tool arguments and was stopped."
            )
            break

        messages.append({"role": "user", "content": tool_results})
    else:
        result.status = STATUS_FAILED
        result.message = f"The agent did not finish within {MAX_TURNS} turns."

    result.matches = matches
    if result.status == STATUS_QUOTED:
        result = _require_complete_quote(result)
    result.trace = trace
    result.elapsedMs = (time.perf_counter() - started) * 1000
    result.modelId = model_id
    return result


def _finalise(result: AgentResult, tool_name: str, payload: dict) -> AgentResult:
    if tool_name == CALCULATE_QUOTE:
        quote = payload["quote"]
        result.status = STATUS_QUOTED
        result.quote = quote
        # The engine's own words. The model's summary is checked against these
        # numbers and only used if it agrees with them.
        result.summary = deterministic_quote_summary(quote)
    elif tool_name == REQUEST_CLARIFICATION:
        clarification = payload["clarification"]
        clarification["question"] = _safe_question(clarification)
        result.status = STATUS_NEEDS_CLARIFICATION
        result.clarification = clarification
        result.summary = clarification["question"]
    return result


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


def _require_complete_quote(result: AgentResult) -> AgentResult:
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
    if not missing:
        return result

    log.warning("incomplete quotation withheld: %d requested line(s) not "
                "covered by the quote", len(missing))
    result.quote = None
    return _needs_clarification(result, missing[0])


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
