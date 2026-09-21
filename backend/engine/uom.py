"""Units of measure: what "3 coils" means, and what it does not.

WHY THIS EXISTS
---------------
A shop sells wire by the coil, conduit by the length, clips by the pack and
everything else by the piece. Until now ShopFlow carried a `unit` string on
each product and used it for display only, which meant "3 coils Finolex wire"
and "3 Finolex wire" were the same request. They are not, and "90 metres
Finolex wire" is a third thing again.

This module makes the unit part of the request, and makes reconciling it a
deterministic decision with four outcomes:

  UNSPECIFIED         no unit was stated; the catalogue unit is assumed
  MATCHES             the stated unit IS the catalogue unit
  CONVERTIBLE         the stated unit is the SKU's configured base unit, so the
                      relationship is known but the order still has to be
                      placed in whole catalogue units - the owner confirms
  NOT_RECONCILABLE    the stated unit has no configured relationship with this
                      SKU at all

WHAT THIS IS NOT
----------------
It is not a UOM framework. There is no unit graph, no dimensional analysis and
no conversion between two units that a product does not itself declare. Five
units occur in this catalogue and one more (METER) exists only as a base unit
underneath COIL; that is the whole vocabulary, and a unit nobody sells is
refused rather than invented.

THE RULE THAT MATTERS: NO SILENT CONVERSION
-------------------------------------------
A configured conversion is used to SHOW an equivalence ("3 COIL = 270 M"). It
is never used to rewrite an order quantity. Someone who asks for 90 metres of
wire is told the shop sells it in 90 m coils and asked to confirm - they are
not quietly given one coil. The difference matters because a coil is
indivisible: cutting it is a different product at a different price, and a
system that guesses here gets both wrong.

Conversions are configured PER PRODUCT, from the catalogue. Nothing here
assumes a coil is 90 metres; the 90 comes off the SKU, and a SKU with no
`baseQuantity` has no conversion at all.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional

from .models import Product

# The units this shop actually trades in, plus METER, which is a base unit for
# wire and is never itself a stocking unit here.
PIECE = "PIECE"
COIL = "COIL"
METER = "METER"
BOX = "BOX"
PACK = "PACK"
LENGTH = "LENGTH"

SUPPORTED_UOMS = (PIECE, COIL, METER, BOX, PACK, LENGTH)

# Units a product may be stocked and sold in. METER is excluded on purpose:
# no SKU in this catalogue is stocked by the metre, and treating it as a
# stocking unit is exactly the silent conversion this module exists to stop.
STOCKING_UOMS = (PIECE, COIL, BOX, PACK, LENGTH)

# Spoken and written forms, mapped to the canonical unit. Singular, plural and
# the abbreviations that actually appear on a counter in Tamil Nadu. Anything
# not listed here is NOT a unit - it is left alone for the matcher to read as
# part of the product description.
_ALIASES: Dict[str, str] = {
    "pc": PIECE, "pcs": PIECE, "piece": PIECE, "pieces": PIECE,
    "no": PIECE, "nos": PIECE, "number": PIECE, "numbers": PIECE,
    "unit": PIECE, "units": PIECE,
    "coil": COIL, "coils": COIL, "bundle": COIL, "bundles": COIL,
    "roll": COIL, "rolls": COIL,
    "m": METER, "mtr": METER, "mtrs": METER,
    "meter": METER, "meters": METER, "metre": METER, "metres": METER,
    "box": BOX, "boxes": BOX, "carton": BOX, "cartons": BOX,
    "pack": PACK, "packs": PACK, "packet": PACK, "packets": PACK,
    "length": LENGTH, "lengths": LENGTH, "rod": LENGTH, "rods": LENGTH,
    # Tamil / Tanglish. Only forms that map onto a unit above with no
    # ambiguity. "mootai" (sack) and similar are deliberately absent: the shop
    # does not sell in them, so reading one would be inventing a unit.
    "meetar": METER, "mittar": METER,
    "kandu": COIL, "sutru": COIL,
    "petti": BOX,
}

# Longest first, so "metres" is read before "m".
_ALIAS_KEYS = sorted(_ALIASES, key=len, reverse=True)

# Outcome of reconciling a requested unit with a SKU.
UOM_UNSPECIFIED = "UNSPECIFIED"
UOM_MATCHES = "MATCHES"
UOM_CONVERTIBLE = "CONVERTIBLE"
UOM_NOT_RECONCILABLE = "NOT_RECONCILABLE"

# Statuses that mean the order cannot proceed on this line as stated.
NEEDS_CLARIFICATION = (UOM_CONVERTIBLE, UOM_NOT_RECONCILABLE)

_QTY_UOM = re.compile(
    r"(?<![\w.])(\d+(?:\.\d+)?)\s*([a-z]+)(?![\w])", re.IGNORECASE)


class UnknownUomError(ValueError):
    """A unit was supplied that this shop does not trade in."""


def normalize_uom(value) -> Optional[str]:
    """One spoken or written unit, as a canonical unit. None if it is not one.

    Returning None for an unrecognised word is deliberate and is not an error:
    most words in an order are not units, and a unit this shop does not use is
    not silently mapped onto one that it does.
    """
    if value is None:
        return None
    word = str(value).strip().lower().rstrip(".")
    if not word:
        return None
    if word.upper() in SUPPORTED_UOMS:
        return word.upper()
    return _ALIASES.get(word)


def require_uom(value) -> str:
    """Canonical unit, or a hard error. For callers validating a request."""
    unit = normalize_uom(value)
    if unit is None:
        raise UnknownUomError(
            f"unknown unit: {value!r}. Supported units are "
            + ", ".join(SUPPORTED_UOMS)
        )
    return unit


def product_uom(product: Product) -> str:
    """The unit a SKU is stocked, priced and sold in.

    Reads the catalogue's own `uom` field, falling back to the legacy `unit`
    string so a product written before this module existed still resolves.
    There is one answer per SKU and it comes from the catalogue - never from
    the words a customer used.
    """
    return normalize_uom(getattr(product, "uom", "") or product.unit) or PIECE


def has_conversion(product: Product) -> bool:
    """Whether this SKU declares how much of a base unit it contains."""
    base_qty = getattr(product, "baseQuantity", None)
    base_uom = normalize_uom(getattr(product, "baseUom", None))
    try:
        return base_uom is not None and float(base_qty) > 0
    except (TypeError, ValueError):
        return False


def base_equivalent(product: Product, quantity: float) -> Optional[dict]:
    """How much of the base unit `quantity` catalogue units comes to.

    Presentation only. `3 coils` stays 3 coils on the quotation; this adds
    "270 M equivalent" beside it. Returns None where the SKU declares no
    conversion, and no conversion is ever assumed.
    """
    if not has_conversion(product):
        return None
    per_unit = float(product.baseQuantity) / float(
        getattr(product, "uomQuantity", 1) or 1)
    total = float(quantity) * per_unit
    return {
        "quantity": round(total, 3),
        "uom": normalize_uom(product.baseUom),
        "perUnit": round(per_unit, 3),
        "calculation": (
            f"{_plain(quantity)} {product_uom(product)} x {_plain(per_unit)} "
            f"{normalize_uom(product.baseUom)} = {_plain(total)} "
            f"{normalize_uom(product.baseUom)}"
        ),
    }


def _plain(value: float) -> str:
    """A number as a shop would write it: 90, not 90.0; 1.5 stays 1.5."""
    number = float(value)
    return str(int(number)) if number == int(number) else str(round(number, 3))


def conversion_note(product: Product) -> Optional[str]:
    """"1 COIL = 90 METER", where the SKU declares it."""
    if not has_conversion(product):
        return None
    qty = getattr(product, "uomQuantity", 1) or 1
    return (f"{_plain(qty)} {product_uom(product)} = "
            f"{_plain(product.baseQuantity)} {normalize_uom(product.baseUom)}")


def resolve_uom(product: Product, requested_uom=None) -> dict:
    """Reconcile a requested unit with what the SKU is actually sold in.

    Four outcomes, no fifth, and none of them rewrites a quantity. The caller
    decides what to do about a status that needs clarification - this function
    only says which one it is and why.
    """
    catalogue = product_uom(product)
    view = {
        "catalogueUom": catalogue,
        "requestedUom": None,
        "conversion": conversion_note(product),
        "needsClarification": False,
        "message": None,
    }

    if requested_uom is None or str(requested_uom).strip() == "":
        return {**view, "status": UOM_UNSPECIFIED}

    requested = normalize_uom(requested_uom)
    if requested is None:
        return {
            **view,
            "status": UOM_NOT_RECONCILABLE,
            "requestedUom": str(requested_uom),
            "needsClarification": True,
            "message": (
                f"'{requested_uom}' is not a unit this shop sells in. "
                f"{product.name} is sold by the {catalogue.lower()}."
            ),
        }

    view["requestedUom"] = requested

    if requested == catalogue:
        return {**view, "status": UOM_MATCHES}

    base = normalize_uom(getattr(product, "baseUom", None))
    if has_conversion(product) and requested == base:
        # The relationship IS known - and that is precisely why it must not be
        # applied silently. A coil is not divisible at this price, so the shop
        # owner confirms how many coils they mean.
        return {
            **view,
            "status": UOM_CONVERTIBLE,
            "needsClarification": True,
            "message": (
                f"{product.name} is sold by the {catalogue.lower()} "
                f"({conversion_note(product)}). Confirm how many "
                f"{catalogue.lower()}s you need - ShopFlow will not convert "
                f"this for you."
            ),
        }

    return {
        **view,
        "status": UOM_NOT_RECONCILABLE,
        "needsClarification": True,
        "message": (
            f"{product.name} is not sold by the {requested.lower()}. "
            f"It is sold by the {catalogue.lower()}."
        ),
    }


def products_with_uom(products, uom) -> List[Product]:
    """Every product stocked in a given unit. Used as a hard match filter."""
    wanted = normalize_uom(uom)
    if wanted is None:
        return []
    return [p for p in products if product_uom(p) == wanted]


def split_quantity_and_uom(text: str) -> Optional[dict]:
    """Pull a leading "3 coils" / "90 metres" out of a phrase.

    Returns None when the phrase states no unit, which is the common case and
    not a failure. Only the FIRST quantity-and-unit pair is read: a line
    describes one product, and a second number in it is a specification
    ("1.5 sq mm"), not a second order quantity.
    """
    for match in _QTY_UOM.finditer(text or ""):
        unit = normalize_uom(match.group(2))
        if unit is None:
            continue
        return {
            "quantity": float(match.group(1)),
            "uom": unit,
            "matched": match.group(0).strip(),
        }
    return None


def uom_evidence(product: Product, requested_uom=None,
                 quantity: float = 1) -> dict:
    """Everything a quotation line needs to show about units, in one object.

    The requested unit is preserved alongside the catalogue unit rather than
    replaced by it. A customer who said "3 coils" should see "3 COILS" on the
    quotation, not a normalised quantity they never asked for.
    """
    resolution = resolve_uom(product, requested_uom)
    return {
        **resolution,
        "requestedQuantity": quantity,
        "baseEquivalent": base_equivalent(product, quantity),
    }
