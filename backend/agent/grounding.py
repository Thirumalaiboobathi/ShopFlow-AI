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
