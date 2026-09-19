"""Supplier cost lookups and price-change detection."""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional

from .models import Dataset, SupplierPrice, money


@dataclass(frozen=True)
class PriceDelta:
    skuId: str
    supplierId: str
    previousCost: Optional[float]
    currentCost: float
    previousDate: Optional[str]
    currentDate: str

    @property
    def absoluteChange(self) -> Optional[float]:
        if self.previousCost is None:
            return None
        return money(self.currentCost - self.previousCost)

    @property
    def percentChange(self) -> Optional[float]:
        """Percent change against the prior price. None on first-ever price."""
        if self.previousCost is None or self.previousCost <= 0:
            return None
        return round(
            (self.currentCost - self.previousCost) / self.previousCost * 100.0, 2
        )

    @property
    def increased(self) -> bool:
        return self.absoluteChange is not None and self.absoluteChange > 0

    def as_evidence(self) -> dict:
        return {
            "skuId": self.skuId,
            "supplierId": self.supplierId,
            "previousCost": self.previousCost,
            "currentCost": self.currentCost,
            "previousDate": self.previousDate,
            "currentDate": self.currentDate,
            "absoluteChange": self.absoluteChange,
            "percentChange": self.percentChange,
        }


def primary_prices(data: Dataset, skuId: str) -> List[SupplierPrice]:
    """Price history from the SKU's own supplier only.

    Competing quotes from other suppliers live in the same history list, so they
    must be filtered out here - otherwise a rival's quote would silently become
    the shop's cost basis.
    """
    supplier_id = data.product(skuId).supplierId
    return [p for p in data.prices(skuId) if p.supplierId == supplier_id]


def current_cost(data: Dataset, skuId: str) -> float:
    """Latest cost from the SKU's supplier, falling back to the catalog price."""
    history = primary_prices(data, skuId)
    if history:
        return money(history[-1].unitCost)
    return money(data.product(skuId).costPrice)


def price_delta(data: Dataset, skuId: str) -> Optional[PriceDelta]:
    """Compare the newest supplier price against the one before it."""
    history: List[SupplierPrice] = primary_prices(data, skuId)
    if not history:
        return None
    latest = history[-1]
    previous = history[-2] if len(history) > 1 else None
    return PriceDelta(
        skuId=skuId,
        supplierId=latest.supplierId,
        previousCost=money(previous.unitCost) if previous else None,
        currentCost=money(latest.unitCost),
        previousDate=previous.effectiveDate if previous else None,
        currentDate=latest.effectiveDate,
    )


def detect_price_increases(
    data: Dataset, threshold_percent: float = 1.0
) -> List[PriceDelta]:
    """Every SKU whose latest supplier price rose by more than the threshold.

    Sorted by severity so the biggest increase surfaces first.
    """
    found = []
    for skuId in data.products:
        delta = price_delta(data, skuId)
        if delta is None or delta.percentChange is None:
            continue
        if delta.percentChange > threshold_percent:
            found.append(delta)
    return sorted(found, key=lambda d: d.percentChange or 0.0, reverse=True)


@dataclass(frozen=True)
class AlternativeQuote:
    skuId: str
    supplierId: str
    supplierName: str
    unitCost: float
    incumbentCost: float
    effectiveDate: str

    @property
    def saving(self) -> float:
        return money(self.incumbentCost - self.unitCost)

    @property
    def savingPercent(self) -> float:
        if self.incumbentCost <= 0:
            return 0.0
        return round(self.saving / self.incumbentCost * 100.0, 2)

    def as_evidence(self) -> dict:
        return {
            "skuId": self.skuId,
            "supplierId": self.supplierId,
            "supplierName": self.supplierName,
            "unitCost": self.unitCost,
            "incumbentCost": self.incumbentCost,
            "saving": self.saving,
            "savingPercent": self.savingPercent,
            "effectiveDate": self.effectiveDate,
        }


def cheaper_alternatives(data: Dataset, skuId: str) -> List[AlternativeQuote]:
    """Quotes from other suppliers that undercut the incumbent's current cost.

    Surfacing these is advisory only. Switching supplier is the owner's call,
    so nothing here feeds the allocator automatically.
    """
    incumbent = current_cost(data, skuId)
    own_supplier = data.product(skuId).supplierId

    latest_by_supplier: dict = {}
    for p in data.prices(skuId):
        if p.supplierId == own_supplier:
            continue
        latest_by_supplier[p.supplierId] = p  # history is date-sorted

    quotes = [
        AlternativeQuote(
            skuId=skuId,
            supplierId=p.supplierId,
            supplierName=data.suppliers[p.supplierId].name
            if p.supplierId in data.suppliers
            else p.supplierId,
            unitCost=money(p.unitCost),
            incumbentCost=incumbent,
            effectiveDate=p.effectiveDate,
        )
        for p in latest_by_supplier.values()
        if p.unitCost < incumbent
    ]
    return sorted(quotes, key=lambda q: (-q.saving, q.supplierId))


def margin_per_rupee(data: Dataset, skuId: str) -> float:
    """Margin per rupee of cash, measured at the CURRENT supplier cost.

    Using live cost rather than the catalog cost matters: a price rise erodes
    margin, and the allocator should see that immediately.
    """
    product = data.product(skuId)
    cost = current_cost(data, skuId)
    if cost <= 0:
        return 0.0
    return (product.sellingPrice - cost) / cost
