"""Quotation and inventory check.

Every figure a customer or owner sees comes from here. The caller supplies
validated SKU ids and quantities; this module supplies the arithmetic and the
evidence trail behind it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Iterable, List, Sequence, Tuple

from .models import Dataset, money
from .pricing import current_cost
from .shortage import shortage_qty
from .uom import base_equivalent, conversion_note, product_uom, uom_evidence
from .velocity import coverage_weeks, velocity_for


class UnknownSkuError(ValueError):
    """Raised when a SKU is not in the catalogue.

    This is the hard boundary that stops an invented SKU reaching a customer.
    """

    def __init__(self, skuIds: Sequence[str]):
        self.skuIds = list(skuIds)
        super().__init__(f"unknown SKU ids: {', '.join(self.skuIds)}")


class InvalidQuantityError(ValueError):
    pass


class UomMismatchError(ValueError):
    """The unit the customer used cannot be reconciled with the SKU.

    Raised rather than resolved, because every way of resolving it is a guess.
    Someone asking for 90 metres of a wire sold in 90 m coils might mean one
    coil or might mean a cut length the shop does not sell; someone asking for
    a box of switches is describing a pack size this shop does not stock. Both
    are questions, and the caller turns this into one.
    """

    def __init__(self, skuId: str, resolution: dict):
        self.skuId = skuId
        self.resolution = resolution
        super().__init__(resolution.get("message") or "unit cannot be reconciled")


@dataclass
class QuoteLine:
    skuId: str
    name: str
    unit: str
    quantity: int
    sellingPrice: float
    lineTotal: float
    onHand: int
    shortageQty: int
    weeklyVelocity: float
    coverageWeeks: float | None
    # Units. `quantity` is, and stays, a count of CATALOGUE units - the price
    # and the stock check are both denominated in them. What the customer
    # actually said is preserved beside it rather than overwritten, so a
    # quotation for "3 coils" reads back as 3 coils.
    catalogueUom: str = "PIECE"
    requestedUom: str | None = None
    uomStatus: str = "UNSPECIFIED"
    conversion: str | None = None
    baseEquivalent: dict | None = None

    @property
    def inStock(self) -> bool:
        return self.shortageQty == 0

    def as_dict(self) -> dict:
        return {
            "skuId": self.skuId,
            "name": self.name,
            "unit": self.unit,
            "quantity": self.quantity,
            "sellingPrice": self.sellingPrice,
            "lineTotal": self.lineTotal,
            "onHand": self.onHand,
            "shortageQty": self.shortageQty,
            "inStock": self.inStock,
            "weeklyVelocity": self.weeklyVelocity,
            "coverageWeeks": self.coverageWeeks,
            # The requested unit is reported as given. Nothing downstream may
            # substitute the converted figure for the ordered quantity.
            "catalogueUom": self.catalogueUom,
            "requestedUom": self.requestedUom,
            "uomStatus": self.uomStatus,
            "conversion": self.conversion,
            "baseEquivalent": self.baseEquivalent,
            "evidence": {
                "lineTotal": f"{self.quantity} x {self.sellingPrice} = {self.lineTotal}",
                "shortage": (
                    f"ordered {self.quantity}, {self.onHand} in stock, "
                    f"short {self.shortageQty}"
                ),
                "coverage": (
                    None if self.coverageWeeks is None else
                    f"{self.onHand} units at {self.weeklyVelocity}/week "
                    f"= {self.coverageWeeks} weeks of cover"
                ),
                "units": (
                    f"{self.quantity} {self.catalogueUom}"
                    + (f" = {self.baseEquivalent['quantity']} "
                       f"{self.baseEquivalent['uom']}"
                       if self.baseEquivalent else "")
                ),
            },
        }


@dataclass
class Quote:
    lines: List[QuoteLine] = field(default_factory=list)

    @property
    def total(self) -> float:
        return money(sum(l.lineTotal for l in self.lines))

    @property
    def itemCount(self) -> int:
        return sum(l.quantity for l in self.lines)

    @property
    def shortages(self) -> List[QuoteLine]:
        return [l for l in self.lines if l.shortageQty > 0]

    def as_dict(self) -> dict:
        return {
            "lines": [l.as_dict() for l in self.lines],
            "total": self.total,
            "itemCount": self.itemCount,
            "lineCount": len(self.lines),
            "shortageCount": len(self.shortages),
            "allInStock": not self.shortages,
            "evidence": {
                "total": " + ".join(f"{l.lineTotal}" for l in self.lines)
                         + f" = {self.total}",
                "source": "engine.quote.calculate_quote",
            },
        }


def check_inventory(data: Dataset, skuIds: Iterable[str]) -> List[dict]:
    """Stock position for each SKU. Unknown ids are an error, not a zero."""
    ids = list(skuIds)
    unknown = [s for s in ids if s not in data.products]
    if unknown:
        raise UnknownSkuError(unknown)

    out = []
    for skuId in ids:
        velocity = round(velocity_for(data, skuId).weeklyVelocity, 2)
        on_hand = data.onHand(skuId)
        cover = coverage_weeks(on_hand, velocity)
        out.append({
            "skuId": skuId,
            "name": data.product(skuId).name,
            "onHand": on_hand,
            "unit": data.product(skuId).unit,
            "weeklyVelocity": velocity,
            "coverageWeeks": None if cover == float("inf") else round(cover, 2),
            "supplierLeadTimeDays": data.supplierFor(skuId).leadTimeDays,
            "currentSupplierCost": current_cost(data, skuId),
            # Stock is counted in the SKU's own unit and stays that way. The
            # equivalent is shown beside it, never instead of it.
            "uom": product_uom(data.product(skuId)),
            "conversion": conversion_note(data.product(skuId)),
            "baseEquivalent": base_equivalent(data.product(skuId), on_hand),
        })
    return out


def calculate_quote(
    data: Dataset, items: Sequence[Tuple[str, int] | Dict]
) -> Quote:
    """Price an order and check it against stock.

    `items` may be (skuId, quantity) pairs or dicts with those keys. A dict
    may also carry `uom`, the unit the customer actually used. Unknown SKUs,
    non-positive quantities and units that cannot be reconciled with the SKU
    are all rejected outright rather than coerced into something plausible.
    """
    normalised: List[Tuple[str, int]] = []
    requested_uoms: Dict[str, object] = {}
    for item in items:
        if isinstance(item, dict):
            skuId, qty = item.get("skuId"), item.get("quantity")
            stated = item.get("uom") or item.get("requestedUom")
            # First statement wins. Two lines for one SKU in two different
            # units is a question for the owner, not something to reconcile
            # here; in practice the agent sends one line per product.
            if stated and skuId not in requested_uoms:
                requested_uoms[skuId] = stated
        else:
            skuId, qty = item
        normalised.append((skuId, qty))

    unknown = [s for s, _ in normalised if s not in data.products]
    if unknown:
        raise UnknownSkuError(unknown)

    merged: Dict[str, int] = {}
    for skuId, qty in normalised:
        if not isinstance(qty, int) or isinstance(qty, bool) or qty <= 0:
            raise InvalidQuantityError(
                f"quantity for {skuId} must be a positive integer, got {qty!r}")
        merged[skuId] = merged.get(skuId, 0) + qty

    # Units are validated before anything is priced. A line whose unit cannot
    # be reconciled is not quoted at a guessed quantity and then flagged - it
    # stops the quotation, and the caller asks.
    for skuId in merged:
        resolution = uom_evidence(
            data.product(skuId), requested_uoms.get(skuId), merged[skuId])
        if resolution["needsClarification"]:
            raise UomMismatchError(skuId, resolution)

    lines = []
    for skuId, qty in merged.items():
        p = data.product(skuId)
        on_hand = data.onHand(skuId)
        units = uom_evidence(p, requested_uoms.get(skuId), qty)
        velocity = round(velocity_for(data, skuId).weeklyVelocity, 2)
        cover = coverage_weeks(on_hand, velocity)
        lines.append(QuoteLine(
            skuId=skuId,
            name=p.name,
            unit=p.unit,
            quantity=qty,
            sellingPrice=p.sellingPrice,
            lineTotal=money(p.sellingPrice * qty),
            onHand=on_hand,
            shortageQty=shortage_qty(qty, on_hand),
            weeklyVelocity=velocity,
            coverageWeeks=None if cover == float("inf") else round(cover, 2),
            catalogueUom=units["catalogueUom"],
            requestedUom=units["requestedUom"],
            uomStatus=units["status"],
            conversion=units["conversion"],
            baseEquivalent=units["baseEquivalent"],
        ))

    return Quote(lines=lines)
