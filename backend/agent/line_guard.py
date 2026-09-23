"""Keeping one order line's search to that one order line.

WHY THIS EXISTS
---------------
An independent evaluation ran the documented three-line canonical order twenty
times. It completed seven times. Single-line orders completed three out of
three. The failure was never a wrong number - every failure was a safe
clarification - but a 35% completion rate on the flagship order is not a
working order desk.

The traces all showed the same thing. The model reads a three-line order,
calls `search_catalog` once per line as instructed, and puts the right words
in `requestedText` - but fills the structured filters from the wrong line:

    requestedText: "20 Anchor modular switches 1-Way 10A White"
    brand:         Havells      <- line 2
    category:      MCB          <- line 2
    colour:        Red          <- line 3
    length:        90m          <- line 3
    uom:           COIL         <- line 3

Every one of those filters is a hard filter in the matcher, so the search for
a real product the shop really stocks returned NOT_FOUND. The matcher was
right, the catalogue was right, and the customer was told their order could
not be processed.

WHAT THIS DOES
--------------
`requestedText` is the customer's own words for this line. The structured
filters are the model's transcription of them. When the two disagree, the
customer's words win, because they are the only part of the call that came
from a person.

So before a search runs, each stated filter is checked against the line's own
text, using vocabulary read off the catalogue - the brands, categories and
colours that actually exist - plus the unit vocabulary the UOM engine already
owns. A filter the line's text contradicts is not searched with.

WHAT THIS IS NOT
----------------
It is not a parser, and it does not split an order into lines: the model
already does that, and doing it twice would be a second order engine
disagreeing with the first. It reads one call's `requestedText` and one call's
filters and asks whether they describe the same thing.

It is not a matcher. It chooses no SKU, reads no price, and changes no rule
about what RESOLVED, AMBIGUOUS or NOT_FOUND mean. Everything it does happens
before `resolve_product` is called, and `resolve_product` then runs exactly as
it always has.

It never widens the brand. A contradicted brand is replaced with the brand the
customer named in that line, never dropped - dropping it would turn "no
Siemens" into an invitation to offer something else, which is the one mistake
this system exists to prevent. When the line's text names no single brand to
fall back on, the search does not run at all and the model is told why.

It never invents a filter. A filter the model did not send is not added, even
when the line's text would support one. Removing contamination is the whole
job; improving on the model's reading of a line is not.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Set, Tuple

from engine.models import Dataset
from engine.uom import normalize_uom

# The catalogue tokeniser, so "switches" and "Switch" are the same word here
# and in the matcher. Private, and imported rather than copied on purpose: a
# second tokeniser is a second vocabulary, and the two would drift.
from engine.matching import _tokens

# The filters a line's own words can be checked against. `specification` is
# deliberately absent: "32 amp", "32A" and "SP 32A C-Curve" are all the same
# specification written three ways, and no rule this module could apply would
# tell a contaminated spec from a normalised one without occasionally throwing
# away a correct search. A contaminated specification still ends in NOT_FOUND
# and is still named by the search diagnostic.
GUARDED_FILTERS = ("brand", "category", "colour", "length", "uom")

# A length as a person writes one. "90m" is a length; the "20" in "20 Anchor
# switches" is a quantity, and telling them apart is the difference between a
# working order and the one that started all of this.
_LENGTH = re.compile(
    r"(?<![\w.])(\d+(?:\.\d+)?)\s*(m|mtr|mtrs|meter|meters|metre|metres)(?![a-z])",
    re.IGNORECASE)

KEPT = "kept"
REPLACED = "replaced"
DROPPED = "dropped"


def _phrase(text: str) -> str:
    """Tokens of `text`, space-delimited so a multi-word value can be found."""
    return " " + " ".join(_tokens(text)) + " "


def _names(haystack: str, value: str) -> bool:
    """Does this text name this exact catalogue value?"""
    tokens = _tokens(value)
    return bool(tokens) and (" " + " ".join(tokens) + " ") in haystack


def _vocabulary(data: Dataset) -> Dict[str, Set[str]]:
    """The brands, categories and colours this catalogue actually contains."""
    products = data.products.values()
    return {
        "brand": {p.brand for p in products if p.brand},
        "category": {p.category for p in products if p.category},
        "colour": {p.colour for p in products if p.colour},
    }


def _stated(haystack: str, values: Set[str]) -> List[str]:
    """Catalogue values this line names, longest first, sub-phrases removed.

    "Cool White" and "White" are both found in a line that says Cool White.
    Only the longer one is what the customer said.
    """
    found = sorted((v for v in values if _names(haystack, v)),
                   key=lambda v: (-len(_tokens(v)), v))
    kept: List[str] = []
    for value in found:
        phrase = " " + " ".join(_tokens(value)) + " "
        if not any(phrase in " " + " ".join(_tokens(k)) + " " for k in kept):
            kept.append(value)
    return kept


def _stated_lengths(text: str) -> List[str]:
    """Lengths written in the line, normalised the way the catalogue writes them."""
    seen: List[str] = []
    for number, _unit in _LENGTH.findall(text or ""):
        value = f"{number}m"
        if value not in seen:
            seen.append(value)
    return seen


def _holds(value: str, values: List[str]) -> bool:
    return any(str(value).strip().lower() == v.lower() for v in values)


def _stated_units(haystack: str) -> List[str]:
    """Canonical units the line's own words contain."""
    seen: List[str] = []
    for token in haystack.split():
        unit = normalize_uom(token)
        if unit and unit not in seen:
            seen.append(unit)
    return seen


def stated_identity(data: Dataset, order_text: str,
                    requested_text: str) -> Dict[str, List[str]]:
    """The brands and categories named in the order line that holds this text.

    The line is the comma-, "and"- or sentence-delimited stretch of the order
    that contains `requested_text`. Empty lists when the text is not found in
    the order or the line names neither - an empty answer restricts nothing.
    """
    requested = _phrase(requested_text).strip()
    if not requested or not (order_text or "").strip():
        return {"brand": [], "category": []}
    vocabulary = _vocabulary(data)
    for line in re.split(r"[\n;,]|(?<!\d)\.(?!\d)|\s(?:and|&)\s", order_text,
                         flags=re.IGNORECASE):
        phrase = _phrase(line)
        if f" {requested} " in phrase:
            return {"brand": _stated(phrase, vocabulary["brand"]),
                    "category": _stated(phrase, vocabulary["category"])}
    return {"brand": [], "category": []}


def uncovered_terms(data: Dataset, order_text: str, matches) -> List[Dict]:
    """Catalogue words the customer used that no search ever looked up.

    The completeness guard asks whether every line that was SEARCHED FOR is in
    the quote. It cannot ask about a line the model never searched for at all,
    because nothing in the record mentions it - and that is a real hole, not a
    theoretical one. A live six-line order came back QUOTED with five lines on
    it: the model simply skipped one, quoted the rest, and every check agreed
    with every other check because they were all reading the same incomplete
    record.

    So this reads the one thing that is not the model's account of the order:
    the customer's own sentence. If it names a brand or a category that no
    search ever mentioned, something was asked for and never looked up, and a
    quotation cannot stand.

    Deliberately narrow. It reads only brands and categories that exist in
    this catalogue, so an adjective or a greeting cannot trigger it, and it
    resolves nothing - it returns the customer's word and stops. Being wrong
    here costs a clarification, which is a question a shop owner can answer.
    Being wrong the other way costs a customer a bill for less than they
    ordered.
    """
    if not (order_text or "").strip():
        return []

    order = _phrase(order_text)
    searched = " ".join(_phrase(m.get("requestedText") or "")
                        for m in (matches or [])
                        if (m.get("requestedText") or "").strip())
    vocabulary = _vocabulary(data)

    out: List[Dict] = []
    for field in ("brand", "category"):
        for value in _stated(order, vocabulary[field]):
            if not _names(searched, value):
                out.append({"term": value, "kind": field})
    return out


def isolate_line(data: Dataset, args: Dict,
                 order_text: str = "") -> Tuple[Optional[Dict], Optional[Dict]]:
    """Check one search's filters against its own line, and correct or refuse.

    `order_text` is the whole order the line came out of, and it is what makes
    a removal evidence-based rather than a guess. A filter this line does not
    name but ANOTHER line of the same order does is contamination and is
    removed. A filter nobody in the order named is left alone, because the
    customer may have written it in Tamil, or in words this vocabulary does
    not carry - and in that case the safe reading is that they meant it.

    That distinction is doing real work. "2 boxes of wire" written in Tamil
    reaches the tool as uom=BOX with a line whose words this module cannot
    read. Dropping the unit there would find the coil and sell it to someone
    who asked for a box, which is exactly what the matcher refuses to do.

    Returns `(corrected_args, report)`.

      * `(None, None)`   every stated filter is consistent with the line. The
                         search runs exactly as it would have before this
                         module existed - the ordinary path is untouched.
      * `(args, report)` contamination was found and removed. The search runs
                         with the corrected arguments and `report` records
                         every change for the trace.
      * `(None, report)` the contradiction cannot be resolved from the line's
                         own words. Nothing is searched and the model is told
                         what disagreed.
    """
    text = args.get("requestedText") or ""
    if not text.strip():
        return None, None

    haystack = _phrase(text)
    # The rest of the order, so a value can be shown to belong to another line.
    elsewhere = _phrase(order_text) if (order_text or "").strip() else ""
    vocabulary = _vocabulary(data)

    corrected = dict(args)
    changes: List[Dict] = []
    blocking: List[str] = []

    def drop(field, value, reason):
        corrected.pop(field, None)
        changes.append({"filter": field, "from": value, "to": None,
                        "action": DROPPED, "reason": reason})

    def replace(field, value, wanted, reason):
        corrected[field] = wanted
        changes.append({"filter": field, "from": value, "to": wanted,
                        "action": REPLACED, "reason": reason})

    for field in GUARDED_FILTERS:
        value = args.get(field)
        if not value:
            continue

        if field == "length":
            stated = _stated_lengths(text)
            other = _stated_lengths(order_text)
            wanted = (_stated_lengths(str(value)) or [str(value).strip()])[0]
        elif field == "uom":
            stated = _stated_units(haystack)
            other = _stated_units(elsewhere)
            wanted = normalize_uom(value) or str(value).strip()
        else:
            stated = _stated(haystack, vocabulary[field])
            other = _stated(elsewhere, vocabulary[field]) if elsewhere else []
            wanted = str(value).strip()

        if _holds(wanted, stated):
            continue                       # the line says exactly this

        if not stated:
            # The line names nothing of this kind, so there is no
            # contradiction to see - only, sometimes, evidence.
            if field == "length":
                # A length is written the same way in every language, so a
                # line that does not contain one did not state one. Rule 5a:
                # an attribute the customer did not give is not a fact, and as
                # a hard filter it can only exclude the right product.
                drop(field, value, "the line states no length")
            elif field != "brand" and _holds(wanted, other):
                # Another line of this same order names it. That is what
                # cross-line contamination looks like, and it is the only
                # ground on which a filter is removed from a line whose own
                # words say nothing about it.
                drop(field, value,
                     f"{value} is stated on another line of this order, "
                     f"not on this one")
            # Everything else is left exactly as the model sent it. The brand
            # is never widened, and a value nobody in the order named may well
            # be one the customer wrote in a language this does not read.
            continue

        # The line names something of this kind, and it is not what was sent.
        if field == "brand":
            if len(stated) == 1:
                replace(field, value, stated[0],
                        f"the line names {stated[0]}, not {value}")
            else:
                blocking.append(
                    f"brand={value!r} was sent, but this line names "
                    f"{', '.join(stated)}. One search_catalog call describes "
                    f"one product.")
            continue

        if field != "category" and len(stated) == 1:
            replace(field, value, stated[0],
                    f"the line says {stated[0]}, not {value}")
            continue

        # Category, and any case where the line names more than one value.
        # The filter is removed rather than swapped: a line that says "fan
        # regulator" names a fan and is an accessory, so preferring the named
        # category over the sent one would be as wrong as keeping it. Dropping
        # only widens the search, and a widened search that does not land on
        # exactly one product becomes a question, not a guess.
        drop(field, value, f"the line names {', '.join(stated)}, not {value}")

    if blocking:
        return None, {"requestedText": text, "changes": changes,
                      "blocking": blocking}
    if not changes:
        return None, None
    return corrected, {"requestedText": text, "changes": changes,
                       "blocking": []}
