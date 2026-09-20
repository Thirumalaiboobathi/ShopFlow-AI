"""Deterministic product matching.

The language model may describe what a customer asked for. It may not decide
which SKU that is. Resolution happens here, over the real catalogue, by rules
that can be read and tested.

Three outcomes, and only three:

  RESOLVED   exactly one catalogue product fits
  AMBIGUOUS  several fit and they differ on an attribute the customer did not
             give - the caller must ask, never guess
  NOT_FOUND  nothing fits

A tie is never broken automatically. If two real products both fit the words
the customer used, ShopFlow asks - it does not pick the shop's usual variant
and show a badge about it. On a quotation the wrong variant means the wrong
price and the wrong delivery, and a default that is merely disclosed is still a
choice the customer never made.

`Product.isDefaultVariant` remains in the catalogue as metadata for owner-facing
features later. It takes no part in resolving a customer order.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .models import Dataset, Product

RESOLVED = "RESOLVED"
AMBIGUOUS = "AMBIGUOUS"
NOT_FOUND = "NOT_FOUND"

# Attributes a customer may leave unsaid, in the order we would ask about them.
CLARIFIABLE_ATTRS = ("brand", "specification", "colour", "length")

_WORD = re.compile(r"[a-z0-9.]+")

# Spoken forms that map onto catalogue vocabulary.
_SYNONYMS = {
    "sqmm": "sqmm", "sq": "", "mm": "", "square": "", "millimeter": "",
    "millimetre": "", "amp": "a", "amps": "a", "ampere": "a", "amperes": "a",
    "switches": "switch", "coils": "coil", "coil": "coil", "wires": "wire",
    "bulbs": "bulb", "lamps": "lamp", "fans": "fan", "pieces": "piece",
    "modular": "modular", "anna": "", "please": "", "need": "", "want": "",
}

_STOPWORDS = {"", "the", "a", "of", "for", "and", "with", "me", "give", "get"}


def _tokens(text: str) -> List[str]:
    out = []
    for raw in _WORD.findall((text or "").lower()):
        word = _SYNONYMS.get(raw, raw)
        if word in _STOPWORDS:
            continue
        out.append(word)
    return out


def _product_tokens(p: Product) -> List[str]:
    parts = [p.name, p.brand, p.category, p.specification or "",
             p.colour or "", p.length or "", p.skuId.replace("-", " ")]
    return _tokens(" ".join(parts))


@dataclass
class MatchCandidate:
    product: Product
    score: float

    def as_dict(self) -> dict:
        p = self.product
        return {
            "skuId": p.skuId,
            "name": p.name,
            "brand": p.brand,
            "category": p.category,
            "specification": p.specification,
            "colour": p.colour,
            "length": p.length,
            "unit": p.unit,
            "sellingPrice": p.sellingPrice,
            "isDefaultVariant": p.isDefaultVariant,
            "score": round(self.score, 3),
        }


@dataclass
class MatchResult:
    status: str
    requestedText: str
    skuId: Optional[str] = None
    resolvedBy: Optional[str] = None
    clarifyingAttribute: Optional[str] = None
    options: List[dict] = field(default_factory=list)
    candidates: List[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "requestedText": self.requestedText,
            "skuId": self.skuId,
            "resolvedBy": self.resolvedBy,
            "clarifyingAttribute": self.clarifyingAttribute,
            "options": self.options,
            "candidates": self.candidates,
        }


def _attr_matches(product: Product, attr: str, wanted: str) -> bool:
    actual = getattr(product, attr, None)
    if actual is None:
        return False
    a, w = str(actual).lower(), str(wanted).lower()
    if attr == "specification":
        # "32 amp" should match "SP 32A C-Curve"; "1.5" should match "1.5 sqmm".
        want_tokens = [t for t in _tokens(w) if t]
        have = " ".join(_tokens(a))
        return all(t in have.split() or t in have for t in want_tokens)
    if attr == "length":
        return _tokens(a) == _tokens(w) or w in a
    return a == w or w in a


def search_catalog(
    data: Dataset,
    *,
    query: str = "",
    brand: Optional[str] = None,
    category: Optional[str] = None,
    specification: Optional[str] = None,
    colour: Optional[str] = None,
    length: Optional[str] = None,
    # High enough to hold every variant of a real product family. A cap that
    # bites would hand back an alphabetically-biased subset, and a truncated
    # candidate list becomes a clarification that omits the right answer.
    limit: int = 40,
) -> List[MatchCandidate]:
    """Score catalogue products against the stated attributes and free text.

    Explicit attributes are hard filters: asking for Finolex can never return
    Polycab. Free text only ranks what survives those filters.
    """
    filters = {"brand": brand, "category": category,
               "specification": specification, "colour": colour, "length": length}
    filters = {k: v for k, v in filters.items() if v}

    pool: List[Product] = []
    for p in data.products.values():
        if all(_attr_matches(p, attr, val) for attr, val in filters.items()):
            pool.append(p)

    q_tokens = [t for t in _tokens(query) if t]
    scored: List[MatchCandidate] = []
    for p in pool:
        p_tokens = set(_product_tokens(p))
        if q_tokens:
            hits = sum(1 for t in q_tokens if t in p_tokens)
            if hits == 0 and not filters:
                continue
            score = hits / len(q_tokens)
        else:
            score = 1.0 if filters else 0.0
        scored.append(MatchCandidate(p, score))

    if q_tokens and scored:
        best = max(c.score for c in scored)
        # Keep only the strongest interpretations; a half-matching product is
        # noise, not a candidate worth asking the owner about.
        scored = [c for c in scored if c.score >= best - 1e-9]

    scored.sort(key=lambda c: (-c.score, c.product.skuId))
    return scored[:limit]


def resolve_product(
    data: Dataset,
    *,
    requested_text: str = "",
    brand: Optional[str] = None,
    category: Optional[str] = None,
    specification: Optional[str] = None,
    colour: Optional[str] = None,
    length: Optional[str] = None,
) -> MatchResult:
    """Turn a described product into exactly one SKU, or into a question."""
    candidates = search_catalog(
        data, query=requested_text, brand=brand, category=category,
        specification=specification, colour=colour, length=length,
    )

    if not candidates:
        return MatchResult(status=NOT_FOUND, requestedText=requested_text)

    if len(candidates) == 1:
        return MatchResult(
            status=RESOLVED,
            requestedText=requested_text,
            skuId=candidates[0].product.skuId,
            resolvedBy="exact",
            candidates=[c.as_dict() for c in candidates],
        )

    # More than one real product fits, so the customer's wording does not
    # identify what they want. Ask.
    #
    # The shop's default variant is deliberately NOT used to break this tie.
    # On a quotation the wrong variant is a wrong price and a wrong delivery,
    # and a default that is merely displayed is still a choice the customer
    # never made. `isDefaultVariant` stays on the catalogue as metadata for
    # owner-facing features later; it has no vote here.
    products = [c.product for c in candidates]
    differing = [
        attr for attr in CLARIFIABLE_ATTRS
        if len({getattr(p, attr) for p in products}) > 1
    ]
    attr = differing[0] if differing else None

    if len(differing) == 1 and attr:
        # One attribute separates them, so the question can be about that
        # attribute alone: "which colour?"
        options = []
        seen = set()
        for c in candidates:
            value = getattr(c.product, attr)
            if value in seen:
                continue
            seen.add(value)
            options.append({"value": value, **c.as_dict()})
    else:
        # Several attributes differ - colour and coil length, say - so there is
        # no single clean question. Offer the actual products instead.
        options = [{"value": c.product.name, **c.as_dict()} for c in candidates]

    return MatchResult(
        status=AMBIGUOUS,
        requestedText=requested_text,
        clarifyingAttribute=attr,
        options=options,
        candidates=[c.as_dict() for c in candidates],
    )


def sku_exists(data: Dataset, skuId: str) -> bool:
    return skuId in data.products
