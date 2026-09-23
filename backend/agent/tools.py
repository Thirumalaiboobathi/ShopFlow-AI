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

from engine.matching import (AMBIGUOUS, NOT_FOUND, RESOLVED,
                             resolve_product, search_catalog)
from engine.models import Dataset
from engine.quote import (
    DuplicateSkuError,
    InvalidQuantityError,
    UnknownSkuError,
    UomMismatchError,
    calculate_quote,
    check_inventory,
)
from engine.uom import METER, STOCKING_UOMS, SUPPORTED_UOMS, normalize_uom

from .line_guard import isolate_line

SEARCH_CATALOG = "search_catalog"
GET_INVENTORY = "get_inventory"
CALCULATE_QUOTE = "calculate_quote"
REQUEST_CLARIFICATION = "request_clarification"

TERMINAL_TOOLS = {CALCULATE_QUOTE, REQUEST_CLARIFICATION}

# A search that was refused rather than run, because its filters described a
# different product from its own requestedText. It is not a matcher verdict -
# the matcher never saw it - and it deliberately resolves nothing, so a line
# that ends here can never reach a quotation.
NEEDS_CORRECTION = "NEEDS_CORRECTION"

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
                        "uom": {
                            "type": "string",
                            "enum": list(STOCKING_UOMS),
                            "description": (
                                "The unit the customer counted in, if they said one: "
                                "'3 coils' is COIL, '2 boxes' is BOX, '20 pieces' is "
                                "PIECE. Report what they said - do not convert it and "
                                "do not supply a unit they did not use."
                            ),
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
                                    "uom": {
                                        "type": "string",
                                        "enum": list(SUPPORTED_UOMS),
                                        "description": (
                                            "The unit the customer used, exactly as "
                                            "they said it. Never converted, never "
                                            "supplied when they did not say one."
                                        ),
                                    },
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
5a. Units come from the customer's words too. If they counted in coils, boxes, packs, lengths, metres or pieces, pass that as uom. If they did not say a unit, omit it. Never convert one unit into another - "90 metres" is not "1 coil", and deciding that is not your job.
6. When every product is RESOLVED, call calculate_quote once with all the items together.
7. Never state a price, total, stock level or shortage yourself. Those come from calculate_quote.

Work through the order and finish with exactly one call to either calculate_quote or request_clarification."""


class ToolError(Exception):
    """A tool rejected the model's arguments."""

    def __init__(self, message: str, kind: str = "INVALID_ARGUMENT"):
        self.kind = kind
        super().__init__(message)


# Brand and category are what the customer actually named. A diagnostic may
# never suggest relaxing them: dropping `brand` turns "no Siemens contactor"
# into an invitation to offer a Havells one, and dropping `category` offers a
# switch to someone who asked for a breaker. A brand the shop does not carry
# is a true NOT_FOUND and the honest answer is that it is not stocked.
_IDENTITY_FILTERS = ("brand", "category")

# Attributes OF a product, within the brand and category already asked for.
# These are where a model mistake is both plausible and safe to point out -
# the quantity arriving as `length` is the one that prompted all of this.
#
# `uom` is deliberately not among them. The matcher treats a stated unit as a
# hard filter on purpose: asking for a box of something stocked only in coils
# is NOT_FOUND because quietly selling the coil is selling the customer
# something they did not ask for. Telling the model to search again without
# the unit would undo that, so a unit mismatch is reported separately and
# sent to the owner as a question instead.
_DIAGNOSABLE_FILTERS = ("specification", "colour", "length")


def _diagnose_no_match(data: Dataset, args: Dict) -> Dict:
    """Why did this search match nothing?

    A NOT_FOUND used to say only "this product is not in the catalogue", which
    is true of the search and not always true of the product. The model kept
    putting the quantity into `length` - "20 Anchor modular switches" became
    `length="20"` - and since a switch has no length, that filter excluded
    every real candidate. The catalogue was then blamed for a typo in the
    query, and a shop owner was told their order could not be processed.

    This answers the question the matcher already had the information to
    answer: which stated attribute removed the last candidate, and does this
    kind of product carry that attribute at all. It is computed by re-running
    the SAME `search_catalog` with one attribute dropped at a time - no new
    matching rule, no relaxed threshold, and no change to what RESOLVED,
    AMBIGUOUS or NOT_FOUND mean. The search that counts has already run and
    its verdict stands.

    Two things it will not do. It never suggests relaxing brand or category,
    so a diagnostic can only ever lead to a better search for the product the
    customer named. And it never returns a skuId: a count tells the model a
    better search exists, but only a real search may hand it an id to quote.
    """
    supplied = {f: args.get(f)
                for f in _IDENTITY_FILTERS + _DIAGNOSABLE_FILTERS + ("uom",)
                if args.get(f)}
    # What every probe keeps: who the customer named, and the unit they asked
    # for. Holding the unit fixed is what keeps the answer honest - drop it
    # and a box-vs-coil mismatch gets blamed on the colour.
    identity = {f: v for f, v in supplied.items()
                if f in _IDENTITY_FILTERS or f == "uom"}
    attributes = {f: v for f, v in supplied.items() if f in _DIAGNOSABLE_FILTERS}
    query = (args.get("requestedText") or "").strip()
    blank = {"suppliedFilters": supplied, "excludingFilters": [], "notes": []}

    if not attributes and not args.get("uom"):
        return blank  # nothing safe to question

    # Does anything of this brand and category exist at all? If not, the
    # product genuinely is not stocked and no attribute advice would be true.
    named = {f: v for f, v in identity.items() if f in _IDENTITY_FILTERS}
    if named and not search_catalog(data, query=query, **named):
        return blank

    excluding, notes = [], []
    unit_mismatch = None
    for attr, value in attributes.items():
        without = dict(identity)
        without.update({k: v for k, v in attributes.items() if k != attr})
        survivors = search_catalog(data, query=query, **without)
        if not survivors:
            continue  # dropping this one alone does not help
        excluding.append(attr)

        # "No switch has a length" is a different message from "no switch is
        # 90m long", and only the first tells the model it misread the order.
        carries = attr == "uom" or any(
            getattr(c.product, attr, None) for c in survivors)
        count = len(survivors)
        if not carries:
            notes.append(
                f"{count} product(s) match once {attr} is dropped, and none of "
                f"them has a {attr} at all. The filter {attr}={value!r} "
                f"excluded them. If {value!r} was the quantity or belongs in "
                f"another field, do not send it as {attr}."
            )
        else:
            notes.append(
                f"{count} product(s) match once {attr} is dropped. "
                f"{attr}={value!r} excluded them - check that value."
            )

    # A unit mismatch, reported but never offered as something to drop.
    unit = args.get("uom")
    if unit and not excluding:
        without_unit = dict(named)
        without_unit.update(attributes)
        survivors = search_catalog(data, query=query, **without_unit)
        if not survivors and normalize_uom(unit) == METER \
                and "length" in without_unit:
            # Counted in metres, the metre figure is the COUNT, not the
            # product's length: "2 metres of red wire" arrives as length=2m,
            # and that filter hides the 90m coil the customer is describing.
            # Asked once without it. Nothing is converted either way.
            without_unit.pop("length")
            survivors = search_catalog(data, query=query, **without_unit)
        if survivors:
            stocked = sorted({c.product.unit for c in survivors if c.product.unit})
            # The same fact as the note, in a shape the orchestrator can act
            # on without reading prose: the product exists, in another unit.
            # No skuId - the orchestrator re-runs the search itself.
            unit_mismatch = {"requestedUnit": unit, "stockedAs": stocked,
                             "filters": without_unit}
            notes.append(
                f"This product is stocked as {', '.join(stocked)}, not "
                f"{unit!r}. Do not convert the quantity and do not search "
                f"again without the unit - call request_clarification and ask "
                f"the shop owner which unit they want."
            )

    out = {"suppliedFilters": supplied,
           "excludingFilters": excluding,
           "notes": notes}
    if unit_mismatch:
        out["unitMismatch"] = unit_mismatch
    return out


def _tool_search_catalog(data: Dataset, args: Dict,
                         order_text: str = "") -> Dict:
    requested = (args.get("requestedText") or "").strip()

    # One line's search may only carry one line's attributes. When the stated
    # filters contradict the customer's own words for this product, the
    # contaminated search is not run: see `line_guard`.
    corrected, isolation = isolate_line(data, args, order_text)
    if isolation and isolation["blocking"]:
        return {
            "status": NEEDS_CORRECTION,
            "requestedText": requested,
            "skuId": None,
            "resolvedBy": None,
            "clarifyingAttribute": None,
            "options": [],
            "candidates": [],
            "lineIsolation": isolation,
            "instruction": (
                "This search was not run. " + " ".join(isolation["blocking"]) +
                " Call search_catalog again for one product at a time, with "
                "requestedText and the filters describing the same product. "
                "Do not invent a skuId."
            ),
        }

    effective = corrected or args
    result = resolve_product(
        data,
        requested_text=requested,
        brand=effective.get("brand"),
        category=effective.get("category"),
        specification=effective.get("specification"),
        colour=effective.get("colour"),
        length=effective.get("length"),
        uom=effective.get("uom"),
    )
    payload = result.as_dict()
    if isolation:
        payload["lineIsolation"] = isolation

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
        diagnostic = _diagnose_no_match(data, effective)
        payload["diagnostic"] = diagnostic
        if isolation:
            # The old blind spot: a search whose filters described a different
            # product from its own requestedText came back "this product is
            # not in the catalogue", which blamed the shop for a mistake in
            # the query. The filters that disagreed with the line have already
            # been corrected by now, so say which ones, and say that the
            # answer below is about the corrected search.
            corrected = ", ".join(
                f"{c['filter']}={c['from']!r}"
                + (f" -> {c['to']!r}" if c["to"] is not None else " (removed)")
                for c in isolation["changes"])
            diagnostic["notes"].insert(0, (
                f"These filters disagreed with the words of this line and "
                f"were corrected before searching: {corrected}. This result "
                f"is for the corrected search."))
        if diagnostic["excludingFilters"]:
            payload["instruction"] = (
                "No product matched, but the diagnostic shows which stated "
                "filter removed the candidates. Call search_catalog again "
                "without that filter, or with a corrected value. Do not "
                "invent a skuId."
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
    except DuplicateSkuError as exc:
        # Refused rather than added up. The model is told to list the SKU once
        # at the quantity the customer asked for - and whatever it sends next
        # is still held to the order text before it can become a quotation.
        raise ToolError(
            f"{', '.join(exc.skuIds)} appears more than once. List each skuId "
            f"once, with the quantity the customer asked for. Do not add "
            f"lines together.",
            kind="DUPLICATE_SKU",
        ) from exc
    except InvalidQuantityError as exc:
        raise ToolError(str(exc), kind="INVALID_QUANTITY") from exc
    except UomMismatchError as exc:
        # The unit is a question, not a rounding problem. The model is sent to
        # the clarification tool rather than being allowed to retry with a
        # quantity it converted itself.
        raise ToolError(
            f"{exc.resolution.get('message')} Do not convert the quantity "
            f"yourself. Call {REQUEST_CLARIFICATION} and ask the shop owner "
            f"how many {exc.resolution.get('catalogueUom', '').lower()}s "
            f"they want.",
            kind="UOM_MISMATCH",
        ) from exc

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
             context: Dict | None = None, order_text: str = "") -> Dict:
    """Run one tool.

    `context` carries ambiguous searches from earlier turns, keyed by the
    customer's wording, so a clarification can reuse the resolution that
    produced it. `order_text` is the whole order, which lets a search tell an
    attribute belonging to another line from one it simply cannot read; it is
    never matched against, and nothing is resolved from it.
    """
    handler = HANDLERS.get(name)
    if handler is None:
        raise ToolError(f"unknown tool: {name}", kind="UNKNOWN_TOOL")
    if handler is _tool_request_clarification:
        return handler(data, args or {}, context)
    if handler is _tool_search_catalog:
        return handler(data, args or {}, order_text)
    return handler(data, args or {})
