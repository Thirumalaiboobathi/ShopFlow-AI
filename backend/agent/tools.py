"""Tool definitions for the ShopFlow order agent.

The schemas are strict and the implementations are thin: each one forwards to
the deterministic engine and returns what the engine said. The model chooses
which tool to call and with what arguments. It does not get to choose the
answer.

Two of these tools are terminal. `calculate_quote` completes the operation and
`request_clarification` hands the question back to the owner. Reaching either
ends the agent loop, which is what makes ShopFlow an operation that finishes
rather than a conversation that continues.
"""

from __future__ import annotations

from typing import Callable, Dict, List

from engine.matching import AMBIGUOUS, NOT_FOUND, RESOLVED, resolve_product
from engine.models import Dataset
from engine.quote import (
    InvalidQuantityError,
    UnknownSkuError,
    calculate_quote,
    check_inventory,
)

SEARCH_CATALOG = "search_catalog"
GET_INVENTORY = "get_inventory"
CALCULATE_QUOTE = "calculate_quote"
REQUEST_CLARIFICATION = "request_clarification"

TERMINAL_TOOLS = {CALCULATE_QUOTE, REQUEST_CLARIFICATION}

MAX_LINE_QUANTITY = 10_000
MAX_ITEMS_PER_ORDER = 25


TOOL_CONFIG = {
    "tools": [
        {
            "toolSpec": {
                "name": SEARCH_CATALOG,
                "description": (
                    "Look up one requested product in the shop catalogue. Call this "
                    "once per distinct product in the order. Returns RESOLVED with a "
                    "real skuId, AMBIGUOUS with the attribute that is missing, or "
                    "NOT_FOUND. You must never write a skuId yourself - only use ids "
                    "returned by this tool."
                ),
                "inputSchema": {"json": {
                    "type": "object",
                    "properties": {
                        "requestedText": {
                            "type": "string",
                            "description": "The customer's words for this product.",
                        },
                        "brand": {
                            "type": "string",
                            "description": "Brand if the customer named one, e.g. Finolex, Anchor, Havells. Omit if not stated - never guess a brand.",
                        },
                        "category": {
                            "type": "string",
                            "description": "One of: Wire, Switch, MCB, LED Lamp, Ceiling Fan, Accessory.",
                        },
                        "specification": {
                            "type": "string",
                            "description": "Size or rating, e.g. '1.5 sqmm', '32A', '9W'.",
                        },
                        "colour": {
                            "type": "string",
                            "description": "Colour if stated, e.g. Red, Blue, Black. Omit if not stated.",
                        },
                        "length": {
                            "type": "string",
                            "description": "Length if stated, e.g. '90m'. Omit if not stated.",
                        },
                    },
                    "required": ["requestedText"],
                }},
            }
        },
        {
            "toolSpec": {
                "name": GET_INVENTORY,
                "description": (
                    "Current stock for SKUs already returned by search_catalog."
                ),
                "inputSchema": {"json": {
                    "type": "object",
                    "properties": {
                        "skuIds": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "SKU ids returned by search_catalog.",
                        }
                    },
                    "required": ["skuIds"],
                }},
            }
        },
        {
            "toolSpec": {
                "name": CALCULATE_QUOTE,
                "description": (
                    "Complete the order. Prices the lines, checks stock and returns "
                    "the quotation with shortages. Call this once, when every product "
                    "has been resolved to a real skuId. This ends the task."
                ),
                "inputSchema": {"json": {
                    "type": "object",
                    "properties": {
                        "items": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "skuId": {"type": "string"},
                                    "quantity": {"type": "integer"},
                                },
                                "required": ["skuId", "quantity"],
                            },
                        }
                    },
                    "required": ["items"],
                }},
            }
        },
        {
            "toolSpec": {
                "name": REQUEST_CLARIFICATION,
                "description": (
                    "Ask the shop owner one question when a product is AMBIGUOUS. "
                    "Use this instead of choosing a variant yourself. This ends the "
                    "task."
                ),
                "inputSchema": {"json": {
                    "type": "object",
                    "properties": {
                        "skuIdOptions": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Candidate SKU ids from search_catalog.",
                        },
                        "clarifyingAttribute": {
                            "type": "string",
                            "description": "The attribute that is missing, e.g. colour.",
                        },
                        "question": {
                            "type": "string",
                            "description": "One short question for the owner.",
                        },
                        "requestedText": {
                            "type": "string",
                            "description": "The customer's words for this product.",
                        },
                    },
                    "required": ["clarifyingAttribute", "question"],
                }},
            }
        },
    ]
}


SYSTEM_PROMPT = """You are the order desk for an independent electrical shop in Tamil Nadu, India.

A customer has sent one order in everyday shop language, often mixing Tamil and English. Your job is to turn that order into a quotation. You are completing a task, not having a conversation.

Rules you must follow:

1. Call search_catalog once for every distinct product in the order. Pass only the attributes the customer actually stated.
2. Never write a skuId yourself. Only use ids that search_catalog returned.
3. If search_catalog returns AMBIGUOUS, do not pick one. Call request_clarification with that attribute and stop.
4. Never substitute a different brand. If the customer said Finolex, it is Finolex or nothing.
5. Quantities come from the customer's words. "3 coils" is 3. Do not invent quantities.
6. When every product is RESOLVED, call calculate_quote once with all the items together.
7. Never state a price, total, stock level or shortage yourself. Those come from calculate_quote.

Work through the order and finish with exactly one call to either calculate_quote or request_clarification."""


class ToolError(Exception):
    """A tool rejected the model's arguments."""

    def __init__(self, message: str, kind: str = "INVALID_ARGUMENT"):
        self.kind = kind
        super().__init__(message)


def _tool_search_catalog(data: Dataset, args: Dict) -> Dict:
    requested = (args.get("requestedText") or "").strip()
    result = resolve_product(
        data,
        requested_text=requested,
        brand=args.get("brand"),
        category=args.get("category"),
        specification=args.get("specification"),
        colour=args.get("colour"),
        length=args.get("length"),
    )
    payload = result.as_dict()

    if result.status == RESOLVED:
        payload["instruction"] = (
            "Use this skuId. If more products remain, search for them next; "
            "otherwise call calculate_quote."
        )
    elif result.status == AMBIGUOUS:
        payload["instruction"] = (
            f"Do not choose. Call request_clarification asking for "
            f"{result.clarifyingAttribute}."
        )
    else:
        payload["instruction"] = (
            "This product is not in the catalogue. Do not invent a skuId."
        )
    return payload


def _tool_get_inventory(data: Dataset, args: Dict) -> Dict:
    sku_ids = args.get("skuIds") or []
    if not isinstance(sku_ids, list) or not sku_ids:
        raise ToolError("skuIds must be a non-empty array")
    try:
        return {"inventory": check_inventory(data, sku_ids)}
    except UnknownSkuError as exc:
        raise ToolError(
            f"These SKU ids do not exist: {', '.join(exc.skuIds)}. "
            f"Use only ids returned by {SEARCH_CATALOG}.",
            kind="UNKNOWN_SKU",
        ) from exc


def _tool_calculate_quote(data: Dataset, args: Dict) -> Dict:
    items = args.get("items") or []
    if not isinstance(items, list) or not items:
        raise ToolError("items must be a non-empty array")
    if len(items) > MAX_ITEMS_PER_ORDER:
        raise ToolError(f"an order may contain at most {MAX_ITEMS_PER_ORDER} lines")

    for item in items:
        qty = item.get("quantity") if isinstance(item, dict) else None
        if isinstance(qty, int) and qty > MAX_LINE_QUANTITY:
            raise ToolError(
                f"quantity {qty} exceeds the maximum of {MAX_LINE_QUANTITY}")

    try:
        quote = calculate_quote(data, items)
    except UnknownSkuError as exc:
        raise ToolError(
            f"These SKU ids do not exist: {', '.join(exc.skuIds)}. "
            f"Use only ids returned by {SEARCH_CATALOG}.",
            kind="UNKNOWN_SKU",
        ) from exc
    except InvalidQuantityError as exc:
        raise ToolError(str(exc), kind="INVALID_QUANTITY") from exc

    return {"quote": quote.as_dict()}


def _tool_request_clarification(data: Dataset, args: Dict,
                                context: Dict | None = None) -> Dict:
    attribute = (args.get("clarifyingAttribute") or "").strip()
    question = (args.get("question") or "").strip()
    if not attribute or not question:
        raise ToolError("clarifyingAttribute and question are both required")

    # Only real SKUs may be offered as choices, exactly as elsewhere.
    raw_options = args.get("skuIdOptions") or []
    unknown = [s for s in raw_options if s not in data.products]
    if unknown:
        raise ToolError(
            f"These SKU ids do not exist: {', '.join(unknown)}.",
            kind="UNKNOWN_SKU",
        )

    requested = args.get("requestedText") or ""
    if not raw_options:
        # The model usually omits the candidate list, and a question the owner
        # cannot answer with one tap is not much use. Rebuild it.
        #
        # Prefer the search that actually produced the ambiguity: it carried
        # the customer's stated attributes (category MCB, specification 32A),
        # and re-deriving from the free text alone loses them - which once
        # offered 6A breakers to someone who asked for 32A.
        remembered = (context or {}).get(requested.strip().lower())
        if remembered and remembered.get("status") == AMBIGUOUS:
            raw_options = [o["skuId"] for o in remembered.get("options", [])]
            attribute = remembered.get("clarifyingAttribute") or attribute
        elif requested:
            fallback = resolve_product(data, requested_text=requested)
            if fallback.status == AMBIGUOUS:
                raw_options = [o["skuId"] for o in fallback.options]
                attribute = fallback.clarifyingAttribute or attribute

    options = [{
        "skuId": s,
        "name": data.product(s).name,
        "value": getattr(data.product(s), attribute, None),
        "sellingPrice": data.product(s).sellingPrice,
        "unit": data.product(s).unit,
    } for s in raw_options]

    # Two candidates can share the attribute being asked about - Red wire comes
    # in a 90m and a 180m coil - and offering "Red" twice is not a choice.
    # Fall back to full product names when the labels would collide.
    labels = [o["value"] for o in options]
    if len(set(labels)) != len(labels) or any(v is None for v in labels):
        for option in options:
            option["value"] = option["name"]

    return {"clarification": {
        "requestedText": args.get("requestedText") or "",
        "clarifyingAttribute": attribute,
        "question": question,
        "options": options,
    }}


HANDLERS: Dict[str, Callable[..., Dict]] = {
    SEARCH_CATALOG: _tool_search_catalog,
    GET_INVENTORY: _tool_get_inventory,
    CALCULATE_QUOTE: _tool_calculate_quote,
    REQUEST_CLARIFICATION: _tool_request_clarification,
}


def run_tool(data: Dataset, name: str, args: Dict,
             context: Dict | None = None) -> Dict:
    """Run one tool. `context` carries ambiguous searches from earlier turns,
    keyed by the customer's wording, so a clarification can reuse the
    resolution that produced it."""
    handler = HANDLERS.get(name)
    if handler is None:
        raise ToolError(f"unknown tool: {name}", kind="UNKNOWN_TOOL")
    if handler is _tool_request_clarification:
        return handler(data, args or {}, context)
    return handler(data, args or {})
