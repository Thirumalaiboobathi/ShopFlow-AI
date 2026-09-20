"""Supplier price intelligence: matching, comparison and owner decisions.

A supplier price list is a document written by someone else. Reading it is a
language problem; deciding what it means for the shop is not. This module owns
the second half: which catalogue SKU a printed line refers to, what the shop
last paid, and whether the difference matters.

Nothing here talks to a model. The percentage a shop owner acts on is computed
from two numbers the shop can point at - its own last recorded cost, and the
figure printed on the document.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .matching import AMBIGUOUS as MATCH_AMBIGUOUS
from .matching import RESOLVED as MATCH_RESOLVED
from .matching import resolve_product
from .models import Dataset, money
from .pricing import PRICE_ALERT_THRESHOLD_PERCENT, current_cost

# Match outcomes for a printed supplier line.
MATCHED = "MATCHED"
AMBIGUOUS = "AMBIGUOUS"
UNMATCHED = "UNMATCHED"

# Price movement classification.
INCREASE = "INCREASE"
DECREASE = "DECREASE"
UNCHANGED = "UNCHANGED"

# Owner decisions on a detected change.
CONFIRMED = "CONFIRMED"
REJECTED = "REJECTED"
PENDING = "PENDING"
DECISIONS = {CONFIRMED, REJECTED}

MAX_SUPPLIER_LINES = 50


class InvalidSupplierLineError(ValueError):
    pass


@dataclass(frozen=True)
class SupplierLine:
    """One printed row, as extracted from the document."""

    description: str
    price: float
    supplierCode: Optional[str] = None
    brand: Optional[str] = None
    specification: Optional[str] = None
    colour: Optional[str] = None
    length: Optional[str] = None
    unit: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "description": self.description,
            "price": self.price,
            "supplierCode": self.supplierCode,
            "brand": self.brand,
            "specification": self.specification,
            "colour": self.colour,
            "length": self.length,
            "unit": self.unit,
        }


@dataclass(frozen=True)
class PriceComparison:
    skuId: str
    previousPrice: Optional[float]
    currentPrice: float
    threshold: float

    @property
    def absoluteDelta(self) -> Optional[float]:
        if self.previousPrice is None:
            return None
        return money(self.currentPrice - self.previousPrice)

    @property
    def percentageDelta(self) -> Optional[float]:
        """Change against what the shop last paid. None when there is no
        previous price to compare against - a first quote is not a rise."""
        if self.previousPrice is None or self.previousPrice <= 0:
            return None
        return round(
            (self.currentPrice - self.previousPrice) / self.previousPrice * 100.0, 2
        )

    @property
    def direction(self) -> str:
        delta = self.absoluteDelta
        if delta is None or delta == 0:
            return UNCHANGED
        return INCREASE if delta > 0 else DECREASE

    @property
    def materialChange(self) -> bool:
        """Material means: big enough that the owner should look.

        Judged on magnitude, so a sharp fall is surfaced too - a supplier
        cutting a price is worth knowing about before the next purchase.
        """
        pct = self.percentageDelta
        if pct is None:
            return False
        return abs(pct) > self.threshold

    def as_dict(self) -> dict:
        return {
            "skuId": self.skuId,
            "previousPrice": self.previousPrice,
            "currentPrice": self.currentPrice,
            "absoluteDelta": self.absoluteDelta,
            "percentageDelta": self.percentageDelta,
            "direction": self.direction,
            "materialChange": self.materialChange,
            "thresholdPercent": self.threshold,
            "evidence": self._evidence(),
        }

    def _evidence(self) -> dict:
        if self.previousPrice is None:
            return {
                "calculation": "no previously recorded price for this SKU",
                "source": "engine.supplier_prices.compare_price",
            }
        return {
            "calculation": (
                f"({self.currentPrice} - {self.previousPrice}) / "
                f"{self.previousPrice} x 100 = {self.percentageDelta}%"
            ),
            "absolute": f"{self.currentPrice} - {self.previousPrice} "
                        f"= {self.absoluteDelta}",
            "threshold": f"material when |change| > {self.threshold}%",
            "previousPriceSource": "shop's last recorded supplier cost",
            "currentPriceSource": "price read from the uploaded document",
            "source": "engine.supplier_prices.compare_price",
        }


@dataclass
class SupplierLineResult:
    line: SupplierLine
    status: str
    skuId: Optional[str] = None
    matchedName: Optional[str] = None
    clarifyingAttribute: Optional[str] = None
    candidates: List[dict] = field(default_factory=list)
    comparison: Optional[PriceComparison] = None

    def as_dict(self) -> dict:
        return {
            "line": self.line.as_dict(),
            "status": self.status,
            "skuId": self.skuId,
            "matchedName": self.matchedName,
            "clarifyingAttribute": self.clarifyingAttribute,
            "candidates": self.candidates,
            "comparison": self.comparison.as_dict() if self.comparison else None,
        }


def build_supplier_line(raw: Dict) -> SupplierLine:
    """Validate one extracted row. Rejects rather than repairs."""
    if not isinstance(raw, dict):
        raise InvalidSupplierLineError("supplier line must be an object")

    description = str(raw.get("description") or "").strip()
    if not description:
        raise InvalidSupplierLineError("description is required")

    price = raw.get("price")
    if isinstance(price, bool) or not isinstance(price, (int, float)):
        raise InvalidSupplierLineError(
            f"price for {description!r} must be a number, got {price!r}")
    if price <= 0:
        raise InvalidSupplierLineError(
            f"price for {description!r} must be positive, got {price!r}")

    def opt(key: str) -> Optional[str]:
        value = raw.get(key)
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    return SupplierLine(
        description=description,
        price=money(float(price)),
        supplierCode=opt("supplierCode"),
        brand=opt("brand"),
        specification=opt("specification"),
        colour=opt("colour"),
        length=opt("length"),
        unit=opt("unit"),
    )


def compare_price(
    data: Dataset,
    skuId: str,
    new_price: float,
    threshold: float = PRICE_ALERT_THRESHOLD_PERCENT,
) -> PriceComparison:
    """Compare a quoted price against the shop's last recorded cost."""
    if skuId not in data.products:
        raise ValueError(f"unknown SKU: {skuId}")

    previous = current_cost(data, skuId)
    return PriceComparison(
        skuId=skuId,
        previousPrice=money(previous) if previous > 0 else None,
        currentPrice=money(new_price),
        threshold=threshold,
    )


def match_supplier_line(
    data: Dataset,
    line: SupplierLine,
    threshold: float = PRICE_ALERT_THRESHOLD_PERCENT,
) -> SupplierLineResult:
    """Resolve one printed line to a catalogue SKU, or refuse to.

    Uses the same resolver as customer orders, so the safety rule is identical:
    several matching products means a question, never a quiet pick.
    """
    resolution = resolve_product(
        data,
        requested_text=line.description,
        brand=line.brand,
        specification=line.specification,
        colour=line.colour,
        length=line.length,
    )

    if resolution.status == MATCH_RESOLVED:
        return SupplierLineResult(
            line=line,
            status=MATCHED,
            skuId=resolution.skuId,
            matchedName=data.product(resolution.skuId).name,
            comparison=compare_price(data, resolution.skuId, line.price, threshold),
        )

    if resolution.status == MATCH_AMBIGUOUS:
        return SupplierLineResult(
            line=line,
            status=AMBIGUOUS,
            clarifyingAttribute=resolution.clarifyingAttribute,
            candidates=resolution.options,
        )

    return SupplierLineResult(line=line, status=UNMATCHED)


@dataclass
class PriceListReview:
    supplierName: str
    documentDate: Optional[str]
    results: List[SupplierLineResult]
    threshold: float = PRICE_ALERT_THRESHOLD_PERCENT

    @property
    def matched(self) -> List[SupplierLineResult]:
        return [r for r in self.results if r.status == MATCHED]

    @property
    def materialChanges(self) -> List[SupplierLineResult]:
        return [
            r for r in self.matched
            if r.comparison is not None and r.comparison.materialChange
        ]

    def as_dict(self) -> dict:
        return {
            "supplierName": self.supplierName,
            "documentDate": self.documentDate,
            "thresholdPercent": self.threshold,
            "lineCount": len(self.results),
            "matchedCount": len(self.matched),
            "ambiguousCount": sum(1 for r in self.results if r.status == AMBIGUOUS),
            "unmatchedCount": sum(1 for r in self.results if r.status == UNMATCHED),
            "materialChangeCount": len(self.materialChanges),
            "lines": [r.as_dict() for r in self.results],
            "source": "engine.supplier_prices.review_price_list",
        }


def review_price_list(
    data: Dataset,
    supplier_name: str,
    document_date: Optional[str],
    raw_lines: List[Dict],
    threshold: float = PRICE_ALERT_THRESHOLD_PERCENT,
) -> PriceListReview:
    """Turn extracted rows into a reviewed, priced, evidence-carrying result."""
    if not isinstance(raw_lines, list) or not raw_lines:
        raise InvalidSupplierLineError("the document produced no usable lines")
    if len(raw_lines) > MAX_SUPPLIER_LINES:
        raise InvalidSupplierLineError(
            f"a price list may carry at most {MAX_SUPPLIER_LINES} lines")

    results = [
        match_supplier_line(data, build_supplier_line(raw), threshold)
        for raw in raw_lines
    ]
    return PriceListReview(
        supplierName=supplier_name or "Unknown supplier",
        documentDate=document_date,
        results=results,
        threshold=threshold,
    )


def build_decision_record(
    data: Dataset, jobId: str, skuId: str, decision: str, comparison: Dict
) -> dict:
    """An owner's ruling on one detected change.

    Recording the decision is deliberately all this does. The catalogue's cost
    price is never written here: a confirmed change is evidence that the owner
    agrees, and applying it to stock valuation and purchasing is a separate,
    later step that they trigger knowingly.
    """
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {sorted(DECISIONS)}")
    if skuId not in data.products:
        raise ValueError(f"unknown SKU: {skuId}")

    return {
        "jobId": jobId,
        "skuId": skuId,
        "decision": decision,
        "productName": data.product(skuId).name,
        "previousPrice": comparison.get("previousPrice"),
        "currentPrice": comparison.get("currentPrice"),
        "percentageDelta": comparison.get("percentageDelta"),
        "catalogPriceChanged": False,
    }
