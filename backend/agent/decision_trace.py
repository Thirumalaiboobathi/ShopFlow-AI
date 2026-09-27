"""How ShopFlow worked this out.

WHAT THIS IS
------------
A structured account of one order, built from what the deterministic engines
actually returned. Ten steps at most, each one a small record of facts:

    {"step": "INVENTORY_CHECK", "skuId": "...", "requested": 3,
     "onHand": 1, "shortage": 2}

It exists because "trust me" is not an answer. A shop owner looking at a
₹22,306.48 quotation should be able to see the three searches, the three
matches, the two shortages and the arithmetic, and satisfy themselves that
nothing was invented.

WHAT IT IS NOT, AND CANNOT BECOME
---------------------------------
It is not the model's account of itself.

Nothing in this module reads model prose. Not `result.summary`, not the
clarification question, not the text blocks of any turn, and above all not a
`<thinking>` block - the model's private reasoning names the tools it is
weighing and, when somebody attempts a prompt injection, discusses the
attempt. That text has one legitimate destination, the raw `trace` audit
record, and this is not it.

The rule is structural rather than a matter of care: every step is built from
a named field of an engine dictionary, and `SAFE_FIELDS` below is the complete
list of keys a step may carry. A model could write whatever it liked into
`summary` and none of it could reach a step, because no step is ever built
from `summary`.

THREE VOICES, NEVER BLENDED
---------------------------
A quantity on a quotation has three possible sources, and the trace names
which one each number came from:

  CUSTOMER INPUT          `customerQuantity` - read from the customer's own
                          order text by `agent.quantity_guard`. Never the
                          model's reading of it.
  MODEL/TOOL PROPOSAL     `proposedQuantity`, and `PROPOSAL_REJECTED` steps -
                          what the model sent to calculate_quote. Structured
                          tool arguments, not prose.
  DETERMINISTIC DECISION  `verdict` and `quotedQuantity` - what the quantity
                          boundary decided and what the quotation carries.

An evaluation once found this trace reporting "4 requested" for an order of
two: it printed the engine's input, which was the model's proposal, under a
word that reads as the customer's. The words below say whose number it is.

TWO AUDIENCES
-------------
`customer_steps` may be shown to anyone. It carries what was ordered, what it
matched, what is in stock and what it costs the customer.

`owner_steps` adds what the shop paid, what the margin did, what the budget
was and what the planner decided. Those are the four things a customer may
never see, so they are a separate function with a separate return value rather
than a flag on one list - a boolean is one typo away from a leak.
"""

from __future__ import annotations

from typing import Dict, List, Optional

# Step names, in the order an order travels through them.
ORDER_RECEIVED = "ORDER_RECEIVED"
PRODUCTS_DETECTED = "PRODUCTS_DETECTED"
CATALOGUE_SEARCH = "CATALOGUE_SEARCH"
SKU_MATCHED = "SKU_MATCHED"
LINE_ISOLATED = "LINE_ISOLATED"
INVENTORY_CHECK = "INVENTORY_CHECK"
SHORTAGE_DETECTED = "SHORTAGE_DETECTED"
QUOTATION = "QUOTATION"
CLARIFICATION_REQUIRED = "CLARIFICATION_REQUIRED"
QUANTITY_CHECK = "QUANTITY_CHECK"
PROPOSAL_REJECTED = "PROPOSAL_REJECTED"
PROCESSING_FAILED = "PROCESSING_FAILED"
GST_APPLIED = "GST_APPLIED"
# The public form of INVENTORY_CHECK: whether the line is in stock, never how
# many units the shop holds.
AVAILABILITY = "AVAILABILITY"

# Owner-only steps.
SUPPLIER_PRICE_CHECK = "SUPPLIER_PRICE_CHECK"
MARGIN_CALCULATED = "MARGIN_CALCULATED"
BUDGET_APPLIED = "BUDGET_APPLIED"
PURCHASE_PLAN = "PURCHASE_PLAN"

CUSTOMER_STEPS = (ORDER_RECEIVED, PRODUCTS_DETECTED, CATALOGUE_SEARCH,
                  SKU_MATCHED, LINE_ISOLATED, PROPOSAL_REJECTED,
                  QUANTITY_CHECK, INVENTORY_CHECK, SHORTAGE_DETECTED,
                  QUOTATION, CLARIFICATION_REQUIRED, PROCESSING_FAILED)
OWNER_ONLY_STEPS = (SUPPLIER_PRICE_CHECK, MARGIN_CALCULATED, BUDGET_APPLIED,
                    PURCHASE_PLAN)

# Every key a step may carry, and nothing else. A step is assembled through
# `_step`, which drops anything not on this list, so adding a field to a trace
# is a deliberate edit here rather than something that happens by accident at
# a call site.
SAFE_FIELDS = frozenset({
    "step", "skuId", "name", "requestedText", "verdict", "status",
    "lineCount", "matchCount", "searchCount", "requested", "onHand",
    "available", "shortage", "unitPrice", "lineTotal", "quantity", "uom",
    "subtotal", "total", "currency", "attribute", "optionCount", "corrected",
    "reason", "detail",
    # Quantity, by source. See THREE VOICES above.
    "customerText", "customerQuantity", "customerQuantities",
    "proposedQuantity", "proposed", "quotedQuantity", "source",
    # GST, as the engine calculated it.
    "taxMode", "totalGst", "grandTotal", "inStock",
})

# ---------------------------------------------------------------------------
# The PUBLIC trace
# ---------------------------------------------------------------------------
# `customer_steps` is shown in the shop's own workspace, and it carries facts
# the shop needs and a customer does not: how many units are on the shelf,
# what the model proposed before the engine refused it, which filters the
# line guard corrected and the model's own wording of each search. An
# evaluation found all of that on the anonymous `GET /api/jobs/{id}`.
#
# `public_view` is what leaves the building. It is an allow-list twice over:
# of step NAMES, and of the FIELDS each step may carry. A step or field added
# to the workspace trace tomorrow is absent here until someone adds it on
# purpose. INVENTORY_CHECK is not passed through; it is replaced by an
# AVAILABILITY step that says in stock or not, and never how many.
PUBLIC_STEPS = (ORDER_RECEIVED, PRODUCTS_DETECTED, SKU_MATCHED, QUANTITY_CHECK,
                AVAILABILITY, QUOTATION, GST_APPLIED, CLARIFICATION_REQUIRED,
                PROCESSING_FAILED)
PUBLIC_FIELDS = frozenset({
    "step", "skuId", "name", "verdict", "status", "lineCount",
    "customerQuantity", "quotedQuantity", "unitPrice", "lineTotal", "uom",
    "total", "currency", "attribute", "optionCount", "inStock", "source",
    "taxMode", "totalGst", "grandTotal", "requestedText",
})
# Steps whose `requestedText` is the customer's own words (a clarification
# carries text already grounded by the orchestrator). Everywhere else that
# field is the MODEL's wording of a search, and it is dropped.
_PUBLIC_REQUESTED_TEXT = (CLARIFICATION_REQUIRED,)

# Why the quote tool refused a model proposal. A closed set, read from the
# tool's own `errorKind`, so no error message text - which can quote the
# model's arguments back - ever reaches a step.
REJECTION_KINDS = frozenset({"DUPLICATE_SKU", "INVALID_QUANTITY",
                             "UNKNOWN_SKU", "UOM_MISMATCH",
                             "INVALID_ARGUMENT"})

CUSTOMER_SOURCE = "customer order text"

# Owner steps additionally carry these. Kept separate so that a customer step
# cannot acquire one by a copy-paste: `customer_steps` never passes them.
OWNER_FIELDS = frozenset({
    "previousCost", "newCost", "changePercent", "previousMargin", "newMargin",
    "budget", "plannedSpend", "restockCost", "skuCount",
})

CURRENCY = "INR"

# Words that must never appear in a rendered trace, whatever happens upstream.
# Asserted in tests rather than filtered here: filtering would hide a leak,
# and there is no legitimate path by which any of these can arrive.
FORBIDDEN_SUBSTRINGS = ("<thinking", "</thinking", "system prompt",
                        "You are the order desk")


def _step(step_name: str, allowed: frozenset, **fields) -> Dict:
    """One trace step, carrying only fields on the allow-list."""
    out = {"step": step_name}
    for key, value in fields.items():
        if key in allowed and value is not None:
            out[key] = value
    return out


def _requested_key(text) -> str:
    return " ".join(str(text or "").split()).casefold()


def _rejected_proposals(trace: List[Dict]) -> List[Dict]:
    """calculate_quote calls the tool refused, as structured facts.

    From the raw tool trace: the tool name, whether it succeeded, the error
    KIND and - for a duplicated SKU only - the SKU and the quantities that
    would once have been added together. An invented SKU id is never repeated
    here, and neither is the error message.
    """
    steps = []
    for entry in trace or []:
        if entry.get("tool") != "calculate_quote" or entry.get("ok"):
            continue
        kind = entry.get("errorKind")
        if kind not in REJECTION_KINDS:
            kind = "INVALID_ARGUMENT"
        items = (entry.get("input") or {}).get("items") or []
        if kind == "DUPLICATE_SKU" and isinstance(items, list):
            by_sku: Dict[str, List] = {}
            for item in items:
                if isinstance(item, dict) and isinstance(item.get("skuId"), str):
                    by_sku.setdefault(item["skuId"], []).append(item.get("quantity"))
            for sku, quantities in by_sku.items():
                if len(quantities) > 1:
                    steps.append(_step(
                        PROPOSAL_REJECTED, SAFE_FIELDS, reason=kind, skuId=sku,
                        proposed=" + ".join(str(q) for q in quantities)))
            continue
        steps.append(_step(PROPOSAL_REJECTED, SAFE_FIELDS, reason=kind))
    return steps


def customer_steps(result_dict: Dict,
                   quantity_check: Optional[List[Dict]] = None) -> List[Dict]:
    """The trace anyone may see, from one agent result.

    Takes the result as a dictionary - the same shape stored on the job row -
    so a trace can be rebuilt from a stored result later without re-running
    anything. Reads `matches`, `quote`, the structured tool `trace` and the
    quantity boundary's verdict, and nothing else - never prose.
    """
    result = result_dict or {}
    matches = result.get("matches") or []
    quote = result.get("quote") or None
    steps: List[Dict] = []

    # 1. the order arrived. The text itself is deliberately not carried: it is
    # already on the job row, and a trace is about what was decided.
    lines = {_requested_key(m.get("requestedText")) for m in matches}
    lines.discard("")
    steps.append(_step(ORDER_RECEIVED, SAFE_FIELDS, status=result.get("status")))
    steps.append(_step(PRODUCTS_DETECTED, SAFE_FIELDS, lineCount=len(lines)))

    # 2-4. one search per attempt, with the matcher's own verdict, plus any
    # correction the line guard made before the search ran.
    matched = 0
    for match in matches:
        requested = (match.get("requestedText") or "").strip()
        steps.append(_step(
            CATALOGUE_SEARCH, SAFE_FIELDS,
            requestedText=requested or None,
            verdict=match.get("status"),
            optionCount=len(match.get("options") or []) or None,
            attribute=match.get("clarifyingAttribute") or None,
        ))

        isolation = match.get("lineIsolation") or {}
        for change in isolation.get("changes") or []:
            steps.append(_step(
                LINE_ISOLATED, SAFE_FIELDS,
                requestedText=requested or None,
                corrected=change.get("filter"),
                reason=change.get("reason"),
            ))

        if match.get("status") == "RESOLVED" and match.get("skuId"):
            matched += 1
            steps.append(_step(SKU_MATCHED, SAFE_FIELDS,
                               requestedText=requested or None,
                               skuId=match.get("skuId")))

    if matches:
        steps.append(_step(PRODUCTS_DETECTED, SAFE_FIELDS,
                           searchCount=len(matches), matchCount=matched))

    # 4b. the model's quote proposals the tool refused, and the quantity
    # boundary's verdict on each quoted SKU - customer, model and decision
    # named apart.
    steps.extend(_rejected_proposals(result.get("trace") or []))

    customer_qty: Dict[str, int] = {}
    for check in quantity_check or []:
        steps.append(_step(
            QUANTITY_CHECK, SAFE_FIELDS,
            skuId=check.get("skuId"),
            customerText=check.get("customerText"),
            customerQuantity=check.get("customerQuantity"),
            customerQuantities=(check.get("customerQuantities")
                                if len(check.get("customerQuantities") or []) > 1
                                else None),
            proposedQuantity=check.get("proposedQuantity"),
            quotedQuantity=check.get("quotedQuantity"),
            verdict=check.get("verdict"),
            source=CUSTOMER_SOURCE,
        ))
        if check.get("customerQuantity") is not None:
            customer_qty[check.get("skuId")] = check["customerQuantity"]

    # 5-7. what the quotation said. Every figure is copied from the engine's
    # own line, never recomputed here - a trace that does its own arithmetic
    # is a second opinion about the total.
    if quote:
        for line in quote.get("lines") or []:
            steps.append(_step(
                INVENTORY_CHECK, SAFE_FIELDS,
                skuId=line.get("skuId"),
                name=line.get("name"),
                # `requested` is kept for readers of the old shape. It is the
                # quoted quantity - which, for a quotation that stands, the
                # quantity boundary has already held equal to the customer's.
                requested=line.get("quantity"),
                quotedQuantity=line.get("quantity"),
                customerQuantity=customer_qty.get(line.get("skuId")),
                onHand=line.get("onHand"),
                shortage=line.get("shortageQty"),
                unitPrice=line.get("sellingPrice"),
                lineTotal=line.get("lineTotal"),
                uom=line.get("catalogueUom"),
            ))
            if (line.get("shortageQty") or 0) > 0:
                steps.append(_step(
                    SHORTAGE_DETECTED, SAFE_FIELDS,
                    skuId=line.get("skuId"),
                    requested=line.get("quantity"),
                    onHand=line.get("onHand"),
                    shortage=line.get("shortageQty"),
                ))
        steps.append(_step(
            QUOTATION, SAFE_FIELDS,
            lineCount=quote.get("lineCount"),
            total=quote.get("total"),
            currency=CURRENCY,
        ))
        tax = quote.get("gst") or {}
        if tax.get("available"):
            steps.append(_step(
                GST_APPLIED, SAFE_FIELDS,
                taxMode=tax.get("taxMode"),
                totalGst=tax.get("totalGst"),
                grandTotal=tax.get("grandTotal"),
                source="engine.gst.quote_gst",
                currency=CURRENCY,
            ))

    # 8. the two ways an order ends without a quotation. The clarification's
    # question is the model's wording and is NOT carried: the attribute and
    # the customer's own requested text say what was asked, without quoting a
    # sentence a model wrote.
    clarification = result.get("clarification") or None
    if clarification and not quote:
        steps.append(_step(
            CLARIFICATION_REQUIRED, SAFE_FIELDS,
            requestedText=(clarification.get("requestedText") or "").strip() or None,
            attribute=clarification.get("clarifyingAttribute") or None,
            optionCount=len(clarification.get("options") or []),
        ))

    if result.get("status") == "FAILED":
        steps.append(_step(PROCESSING_FAILED, SAFE_FIELDS,
                           status="FAILED",
                           reason="the agent did not complete the order"))

    return steps


def owner_steps(*, comparisons: Optional[List[Dict]] = None,
                margin_alerts: Optional[List[Dict]] = None,
                plan: Optional[Dict] = None) -> List[Dict]:
    """The steps only the shop owner may see: cost, margin, budget, plan.

    Separate from `customer_steps` on purpose. These carry supplier cost and
    margin, and the one reliable way to keep them off a customer-facing route
    is for the customer-facing function to be incapable of producing them.
    """
    steps: List[Dict] = []
    allowed = SAFE_FIELDS | OWNER_FIELDS

    for comparison in comparisons or []:
        steps.append(_step(
            SUPPLIER_PRICE_CHECK, allowed,
            skuId=comparison.get("skuId"),
            previousCost=comparison.get("previousPrice"),
            newCost=comparison.get("currentPrice"),
            changePercent=comparison.get("percentageDelta"),
            detail=comparison.get("direction"),
        ))

    for alert in margin_alerts or []:
        steps.append(_step(
            MARGIN_CALCULATED, allowed,
            skuId=alert.get("skuId"),
            previousMargin=alert.get("oldMarginAmount"),
            newMargin=alert.get("newMarginAmount"),
        ))

    if plan:
        steps.append(_step(BUDGET_APPLIED, allowed, budget=plan.get("budget"),
                           currency=CURRENCY))
        steps.append(_step(
            PURCHASE_PLAN, allowed,
            plannedSpend=plan.get("totalSpend"),
            restockCost=plan.get("restockCost"),
            skuCount=len(plan.get("restockSelected") or [])
            + len(plan.get("commitments") or []),
            currency=CURRENCY,
        ))

    return steps


def render(steps: List[Dict]) -> List[str]:
    """One short English line per step, for the interface.

    Built here rather than in the browser so that the words are tested with
    the numbers. Every figure in a rendered line came out of the step, which
    came out of an engine.
    """
    out = []
    for step in steps or []:
        name = step.get("step")
        if name == ORDER_RECEIVED:
            out.append("Order received.")
        elif name == PRODUCTS_DETECTED and step.get("lineCount") is not None:
            out.append(f"{step['lineCount']} product line(s) detected.")
        elif name == PRODUCTS_DETECTED:
            out.append(f"{step.get('searchCount', 0)} catalogue search(es), "
                       f"{step.get('matchCount', 0)} matched.")
        elif name == CATALOGUE_SEARCH:
            out.append(f"Searched \"{step.get('requestedText', '')}\" "
                       f"— {step.get('verdict', '')}.")
        elif name == LINE_ISOLATED:
            out.append(f"Corrected {step.get('corrected')} before searching: "
                       f"{step.get('reason')}.")
        elif name == SKU_MATCHED:
            out.append(f"Matched to {step.get('skuId')}.")
        elif name == PROPOSAL_REJECTED:
            if step.get("reason") == "DUPLICATE_SKU":
                out.append(f"Model proposal rejected: {step.get('skuId')} listed "
                           f"more than once ({step.get('proposed')}). Lines are "
                           f"never added together.")
            else:
                out.append(f"Model proposal rejected ({step.get('reason')}).")
        elif name == QUANTITY_CHECK:
            out.append(_render_quantity(step))
        elif name == INVENTORY_CHECK:
            if step.get("customerQuantity") is not None:
                out.append(f"{step.get('skuId')}: customer asked for "
                           f"{step['customerQuantity']}, "
                           f"{step.get('quotedQuantity')} quoted, "
                           f"{step.get('onHand', 0)} in stock.")
            else:
                out.append(f"{step.get('skuId')}: {step.get('quotedQuantity', step.get('requested'))} "
                           f"quoted, {step.get('onHand', 0)} in stock.")
        elif name == SHORTAGE_DETECTED:
            out.append(f"{step.get('skuId')}: short by {step.get('shortage')}.")
        elif name == QUOTATION:
            out.append(f"Quotation: {step.get('lineCount')} line(s), "
                       f"total {step.get('total')}.")
        elif name == GST_APPLIED:
            out.append(f"GST ({step.get('taxMode')}): {step.get('totalGst')}; "
                       f"total including GST {step.get('grandTotal')}.")
        elif name == AVAILABILITY:
            out.append(f"{step.get('skuId')}: {step.get('quotedQuantity')} "
                       f"quoted, "
                       + ("in stock." if step.get("inStock")
                          else "not all in stock - the shop will confirm."))
        elif name == CLARIFICATION_REQUIRED:
            out.append(f"Clarification needed for "
                       f"\"{step.get('requestedText', '')}\".")
        elif name == PROCESSING_FAILED:
            out.append("Processing failed. Review required.")
        elif name == SUPPLIER_PRICE_CHECK:
            out.append(f"Supplier price {step.get('skuId')}: "
                       f"{step.get('previousCost')} → {step.get('newCost')} "
                       f"({step.get('changePercent')}%).")
        elif name == MARGIN_CALCULATED:
            out.append(f"Margin {step.get('skuId')}: "
                       f"{step.get('previousMargin')} → "
                       f"{step.get('newMargin')}.")
        elif name == BUDGET_APPLIED:
            out.append(f"Budget applied: {step.get('budget')}.")
        elif name == PURCHASE_PLAN:
            out.append(f"Purchase plan: {step.get('plannedSpend')} across "
                       f"{step.get('skuCount')} SKU(s).")
    return out


def _render_quantity(step: Dict) -> str:
    sku = step.get("skuId")
    customer = step.get("customerQuantity")
    proposed = step.get("proposedQuantity")
    verdict = step.get("verdict")
    if "proposedQuantity" not in step:
        # The public form: the customer's number and the decision, never the
        # model's proposal.
        if verdict == "VERIFIED" and step.get("quotedQuantity") is not None:
            return (f"{sku}: customer asked for {customer}; quoted "
                    f"{step['quotedQuantity']}.")
        if verdict == "VERIFIED":
            return (f"{sku}: customer asked for {customer}; not quoted because "
                    f"another line needs confirming.")
        if customer is not None:
            return (f"{sku}: customer asked for {customer}; the quantity could "
                    f"not be verified, so nothing was quoted.")
        return f"{sku}: the quantity needs confirming, so nothing was quoted."
    if verdict == "VERIFIED":
        quoted = step.get("quotedQuantity")
        if quoted is None:
            return (f"{sku}: customer asked for {customer}; model proposed "
                    f"{proposed} - agreed, but the quotation was withheld for "
                    f"another line.")
        return (f"{sku}: customer asked for {customer}; model proposed "
                f"{proposed}; quoted {quoted}.")
    if verdict == "MISMATCH":
        return (f"{sku}: customer asked for {customer}; model proposed "
                f"{proposed} - rejected, nothing quoted.")
    if verdict == "CONFLICTING":
        stated = ", ".join(str(q) for q in step.get("customerQuantities") or [])
        return (f"{sku}: the order states more than one quantity ({stated}); "
                f"model proposed {proposed} - not quoted.")
    return (f"{sku}: no quantity could be read from the order; model proposed "
            f"{proposed} - not quoted.")


def unavailable(reason: str = "Trace unavailable") -> Dict:
    """What the interface shows when a trace could not be built.

    A trace is an explanation of a quotation, so it may never be a reason not
    to have one. If building it raises, the caller reports this and the
    quotation stands untouched.
    """
    return {"available": False, "reason": reason, "steps": [], "lines": []}


def _public_step(step: Dict) -> Optional[Dict]:
    """One workspace step as the public may see it, or None to drop it."""
    name = step.get("step")
    if name == INVENTORY_CHECK:
        # Converted, not passed through: in stock or not, never how many.
        shortage = step.get("shortage")
        step = {"step": AVAILABILITY, "skuId": step.get("skuId"),
                "name": step.get("name"),
                "quotedQuantity": step.get("quotedQuantity",
                                           step.get("requested")),
                "customerQuantity": step.get("customerQuantity"),
                "unitPrice": step.get("unitPrice"),
                "lineTotal": step.get("lineTotal"), "uom": step.get("uom"),
                "inStock": (shortage or 0) == 0}
        name = AVAILABILITY
    if name not in PUBLIC_STEPS:
        return None
    if name == PRODUCTS_DETECTED and step.get("lineCount") is None:
        return None  # the search-count variant: how often the model searched
    out = {}
    for key, value in step.items():
        if key not in PUBLIC_FIELDS or value is None:
            continue
        if key == "requestedText" and name not in _PUBLIC_REQUESTED_TEXT:
            continue
        out[key] = value
    return out


def public_view(trace: Optional[Dict]) -> Dict:
    """The decision trace as a customer may see it. Never raises.

    Takes the stored (workspace) trace and returns a new one: allowed steps
    only, allowed fields only, lines re-rendered from what survived. The input
    is not modified.
    """
    try:
        if not isinstance(trace, dict) or not trace.get("available"):
            return unavailable()
        steps = [s for s in (_public_step(dict(step))
                             for step in trace.get("steps") or []
                             if isinstance(step, dict)) if s]
        return {"available": True, "audience": "customer", "steps": steps,
                "lines": render(steps), "source": "agent.decision_trace"}
    except Exception as exc:  # noqa: BLE001
        return unavailable(f"Trace unavailable ({type(exc).__name__})")


def build(result_dict: Dict, *, owner: bool = False,
          quantity_check: Optional[List[Dict]] = None, **owner_inputs) -> Dict:
    """The trace for one order result, for one audience. Never raises.

    `quantity_check` is the quantity boundary's verdict, passed beside the
    result because it is not part of the stored result's shape. A trace rebuilt
    later from a stored result has no quantity steps rather than invented ones.
    """
    try:
        steps = customer_steps(result_dict, quantity_check)
        if owner:
            steps = steps + owner_steps(**owner_inputs)
        return {"available": True, "audience": "owner" if owner else "customer",
                "steps": steps, "lines": render(steps),
                "source": "agent.decision_trace"}
    except Exception as exc:  # noqa: BLE001 - an explanation may not break a quote
        return unavailable(f"Trace unavailable ({type(exc).__name__})")
