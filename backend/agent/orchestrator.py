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
import time
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from engine.models import Dataset

from .grounding import deterministic_quote_summary, validate_summary
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


def _text_of(message: dict) -> str:
    return " ".join(
        block["text"] for block in message.get("content", []) if "text" in block
    ).strip()


def run_order_agent(
    data: Dataset,
    order_text: str,
    *,
    client=None,
    model_id: str = DEFAULT_MODEL_ID,
) -> AgentResult:
    """Turn one customer order into a quotation or a clarification request."""
    order_text = (order_text or "").strip()
    if not order_text:
        raise ValueError("order text is empty")
    if len(order_text) > MAX_ORDER_CHARS:
        raise OrderTooLongError(
            f"order text exceeds {MAX_ORDER_CHARS} characters")

    client = client or _bedrock_client()
    started = time.perf_counter()

    messages: List[Dict] = [
        {"role": "user", "content": [{"text": f"Customer order: {order_text}"}]}
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
            # The model answered in prose. That is not an outcome ShopFlow can
            # act on, so the task has not completed.
            result.summary = _text_of(out_message)
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
        result.status = STATUS_NEEDS_CLARIFICATION
        result.clarification = clarification
        result.summary = clarification["question"]
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
