"""Lightweight numeric grounding check.

Every number the model writes in its summary must already appear in the
deterministic tool output. If one does not, the summary is discarded and a
sentence built from the engine's own figures is used instead.

This is deliberately small. It catches the failure that matters - a confident
sentence containing a price or a stock level nobody calculated - without trying
to be a general-purpose fact checker.
"""

from __future__ import annotations

import re
from typing import Iterable, List, Set, Tuple

from engine.matching import AMBIGUOUS, RESOLVED

# Matches 22306.48, 22,306.48, 6, 4.25
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")

# Ordinary words that carry a digit but assert nothing about the business.
_TOLERANCE = 0.01


def _parse(text: str) -> float | None:
    try:
        return float(text.replace(",", ""))
    except ValueError:
        return None


def collect_numbers(payload) -> Set[float]:
    """Every numeric value anywhere in the tool output, including inside text."""
    found: Set[float] = set()

    def walk(node):
        if isinstance(node, bool):
            return
        if isinstance(node, (int, float)):
            found.add(float(node))
            return
        if isinstance(node, str):
            for match in _NUMBER.findall(node):
                value = _parse(match)
                if value is not None:
                    found.add(value)
            return
        if isinstance(node, dict):
            for value in node.values():
                walk(value)
            return
        if isinstance(node, (list, tuple)):
            for value in node:
                walk(value)

    walk(payload)

    # Accept the rounded forms a summary would naturally use.
    for value in list(found):
        found.add(round(value))
        found.add(round(value, 1))
        found.add(round(value, 2))
    return found


def find_unsupported_numbers(text: str, allowed: Set[float]) -> List[str]:
    """Numbers in `text` that no tool produced."""
    unsupported = []
    for match in _NUMBER.findall(text or ""):
        value = _parse(match)
        if value is None:
            continue
        if not any(abs(value - ok) <= _TOLERANCE for ok in allowed):
            unsupported.append(match)
    return unsupported


def validate_summary(text: str, payload) -> Tuple[bool, List[str]]:
    """True when every number in the summary traces back to tool output."""
    allowed = collect_numbers(payload)
    unsupported = find_unsupported_numbers(text, allowed)
    return (not unsupported), unsupported


def deterministic_quote_summary(quote: dict) -> str:
    """A summary assembled only from engine figures, used when grounding fails."""
    lines = quote.get("lines", [])
    shortages = [l for l in lines if l.get("shortageQty", 0) > 0]
    parts = [
        f"{len(lines)} items quoted at Rs {quote.get('total')}."
    ]
    if shortages:
        detail = ", ".join(
            f"{l['name']} short by {l['shortageQty']} {l['unit']}"
            for l in shortages
        )
        parts.append(f"Stock shortfall: {detail}.")
    else:
        parts.append("Everything ordered is in stock.")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Quote completeness
# ---------------------------------------------------------------------------
# A second grounding question, about coverage rather than arithmetic: does the
# quotation actually cover everything the customer asked for?
#
# This exists because of a real live run. The model leaked `length: "90m"`
# from the wire line into the switch search, `search_catalog` correctly
# returned NOT_FOUND and correctly refused to invent a skuId - and the model
# then called calculate_quote with the two lines that had resolved. The
# arithmetic was right (19824.0 + 916.48 = 20740.48) and the result came back
# as QUOTED. A customer who asked for three products would have been shown a
# bill for two, with the missing one mentioned nowhere but the match list.
#
# The engines were not at fault and neither was the quotation. The gap was
# that nothing deterministic checked the quote against the request before the
# result crossed the boundary. An agent may make an imperfect tool call; it
# must not be able to make the business accept an incomplete quotation.


def _requested_key(text) -> str:
    return " ".join(str(text or "").split()).casefold()


def unsatisfied_lines(matches, quote) -> List[dict]:
    """Requested product lines that the quotation does not cover.

    One entry per distinct `requestedText` that was searched for and did not
    end up in the quote, in the order it was first searched. An empty list
    means every product the customer was understood to have asked for is
    priced in the quotation.

    A line counts as satisfied when either:

      * one of its searches RESOLVED to a skuId that is in the quote, or
      * one of its searches was AMBIGUOUS and the quote contains a skuId the
        matcher itself offered as an option for it.

    The second rule is what keeps the clarification round working. The owner
    answers an ambiguous line by picking one of the options the engine
    offered, and the next run quotes that SKU - sometimes without searching
    again, sometimes searching and coming back AMBIGUOUS a second time. Either
    way the chosen SKU came from the matcher's own list for that line, so the
    line is resolved even though no single search says RESOLVED.

    `candidates` is deliberately not consulted. A RESOLVED match lists its own
    skuId there, so accepting candidates would let a resolved-but-dropped line
    look satisfied - which is precisely the case this is here to catch.
    """
    quoted = {line.get("skuId") for line in (quote or {}).get("lines") or []}
    quoted.discard(None)

    grouped: dict = {}
    for match in matches or []:
        key = _requested_key(match.get("requestedText"))
        if not key:
            # Nothing to attribute to a product line. A search with no
            # requested text cannot be shown to be missing from the quote.
            continue
        grouped.setdefault(key, []).append(match)

    missing = []
    for attempts in grouped.values():
        satisfied = False
        for match in attempts:
            if match.get("status") == RESOLVED and match.get("skuId") in quoted:
                satisfied = True
                break
            if match.get("status") == AMBIGUOUS and any(
                    option.get("skuId") in quoted
                    for option in match.get("options") or []):
                satisfied = True
                break
        if not satisfied:
            # Report the last attempt: it carries the most refined attributes
            # and, when the line was ambiguous, the options worth offering.
            missing.append(attempts[-1])
    return missing
