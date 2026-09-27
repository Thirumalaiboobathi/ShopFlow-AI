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
# The same product more than once on one document. At different prices the
# document contradicts itself: every one of those rows is a CONFLICT, none has
# a comparison, and so none can raise an alert or be confirmed as a cost. At
# the same price the first row stands and the repeats are DUPLICATE.
CONFLICT = "CONFLICT"
DUPLICATE = "DUPLICATE"

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

# The review boundary a document row travels along:
#
#     EXTRACTED  ->  MATCHED  ->  REVIEW_REQUIRED  ->  CONFIRMED
#
# EXTRACTED       something was read off the page. A price, and nothing more.
# MATCHED         it resolved to exactly one catalogue SKU, confidently, and
#                 its price has not moved materially.
# REVIEW_REQUIRED the owner must look: the SKU is ambiguous or unknown, the
#                 characters were read with low confidence, or the price moved
#                 far enough to matter.
# CONFIRMED       the owner has ruled on it.
#
# Only CONFIRMED may reach purchasing. That is enforced not by this constant
# but by `engine.cost_records`, which is written only on a confirmation and is
# the single thing the planner reads.
STATE_EXTRACTED = "EXTRACTED"
STATE_MATCHED = "MATCHED"
STATE_REVIEW_REQUIRED = "REVIEW_REQUIRED"
STATE_CONFIRMED = "CONFIRMED"
REVIEW_STATES = (STATE_EXTRACTED, STATE_MATCHED, STATE_REVIEW_REQUIRED,
                 STATE_CONFIRMED)

# Below this, a row's characters were not read confidently enough to move a
# purchase cost without somebody looking. Textract reports per-word confidence
# as a percentage; the sample dealer list reads at 98.6% average.
#
# This is the single definition. `agent.textract_reader` imports it rather
# than keeping a second number that could drift away from this one.
MIN_ROW_CONFIDENCE = 90.0


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
    # How the row was read, and how sure the reader was. Both are provenance,
    # not business data: they decide whether a person looks at the row, and
    # they never take part in a price comparison.
    confidence: Optional[float] = None
    source: Optional[str] = None

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
            "confidence": self.confidence,
            "source": self.source,
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
    reviewState: str = STATE_EXTRACTED
    conflictingPrices: Optional[List[float]] = None

    def as_dict(self) -> dict:
        out = {
            "line": self.line.as_dict(),
            "status": self.status,
            "reviewState": self.reviewState,
            "skuId": self.skuId,
            "matchedName": self.matchedName,
            "clarifyingAttribute": self.clarifyingAttribute,
            "candidates": self.candidates,
            "comparison": self.comparison.as_dict() if self.comparison else None,
        }
        if self.conflictingPrices is not None:
            out["conflictingPrices"] = self.conflictingPrices
        return out


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

    confidence = raw.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        confidence = None
    else:
        confidence = round(float(confidence), 2)

    return SupplierLine(
        description=description,
        price=money(float(price)),
        supplierCode=opt("supplierCode"),
        brand=opt("brand"),
        specification=opt("specification"),
        colour=opt("colour"),
        length=opt("length"),
        unit=opt("unit"),
        confidence=confidence,
        source=opt("source"),
    )


def review_state(result: "SupplierLineResult",
                 decisions: Optional[Dict[str, str]] = None,
                 min_confidence: float = MIN_ROW_CONFIDENCE) -> str:
    """Where along the review boundary one extracted row currently sits.

    Derived, never stored: it is a reading of facts that already exist - the
    match verdict, the confidence the row was read at, whether the price moved
    materially, and whether the owner has ruled. Nothing here decides anything
    that was not already decided somewhere that can be tested on its own.

    The default is caution. A row only reaches MATCHED by being unambiguous,
    read confidently and unchanged in price; anything else waits for a person.
    """
    decided = (decisions or {}).get(result.skuId or "")
    if decided in DECISIONS:
        return STATE_CONFIRMED if decided == CONFIRMED else STATE_REVIEW_REQUIRED

    if result.status != MATCHED:
        # Ambiguous or unknown. The matcher refuses to choose and so does this.
        return STATE_REVIEW_REQUIRED

    confidence = result.line.confidence
    if confidence is not None and float(confidence) < min_confidence:
        return STATE_REVIEW_REQUIRED

    if result.comparison is not None and result.comparison.materialChange:
        # A material move is the whole reason the owner is being shown this.
        return STATE_REVIEW_REQUIRED

    return STATE_MATCHED


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


# The share of a printed line's words a catalogue product must carry to be
# offered for it at all. An evaluation's sample price list had "Kaveri 4-core
# Armoured Cable 25 sqmm" - a brand this shop does not stock - and the review
# offered "Cable Clip Clamp 20mm" for it, on the single shared word "cable"
# (one word in seven). Held for review, so nothing was changed, but a
# suggestion that wrong teaches the owner to stop reading suggestions.
#
# Applied to a single weak candidate too: that one would otherwise be MATCHED
# and its printed price compared against the cost of an unrelated SKU.
# Supplier review only; customer orders are matched exactly as before.
MIN_SUPPLIER_MATCH_SCORE = 0.5


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

    best = max((c.get("score") or 0.0 for c in resolution.candidates),
               default=0.0)
    if resolution.status in (MATCH_RESOLVED, MATCH_AMBIGUOUS) \
            and best < MIN_SUPPLIER_MATCH_SCORE:
        return SupplierLineResult(line=line, status=UNMATCHED)

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
    excluded: List[dict] = field(default_factory=list)

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
        conflicts = sorted({r.skuId for r in self.results
                            if r.status == CONFLICT})
        excluded = len(self.excluded)
        return {
            "supplierName": self.supplierName,
            "documentDate": self.documentDate,
            "thresholdPercent": self.threshold,
            "lineCount": len(self.results),
            "matchedCount": len(self.matched),
            "ambiguousCount": sum(1 for r in self.results if r.status == AMBIGUOUS),
            "unmatchedCount": sum(1 for r in self.results if r.status == UNMATCHED),
            "conflictCount": len(conflicts),
            "conflictRowCount": sum(1 for r in self.results
                                    if r.status == CONFLICT),
            "duplicateCount": sum(1 for r in self.results
                                  if r.status == DUPLICATE),
            "conflictNotice": (
                f"{len(conflicts)} product(s) appear more than once on this "
                f"document at different prices. No price decision or alert "
                f"was made for them. Confirm the correct price with the "
                f"supplier." if conflicts else None),
            "excludedCount": excluded,
            "excludedRows": list(self.excluded),
            "exclusionNotice": exclusion_notice(excluded),
            "materialChangeCount": len(self.materialChanges),
            "reviewRequiredCount": sum(
                1 for r in self.results if r.reviewState == STATE_REVIEW_REQUIRED),
            "lowConfidenceCount": sum(
                1 for r in self.results
                if r.line.confidence is not None
                and float(r.line.confidence) < MIN_ROW_CONFIDENCE),
            "readers": sorted(
                {r.line.source for r in self.results if r.line.source}),
            "lines": [r.as_dict() for r in self.results],
            "source": "engine.supplier_prices.review_price_list",
        }


def review_price_list(
    data: Dataset,
    supplier_name: str,
    document_date: Optional[str],
    raw_lines: List[Dict],
    threshold: float = PRICE_ALERT_THRESHOLD_PERCENT,
    excluded_rows: Optional[List[Dict]] = None,
) -> PriceListReview:
    """Turn extracted rows into a reviewed, priced, evidence-carrying result.

    `excluded_rows` are rows the reader saw but could not price. They are
    carried through, with any row this function itself rejects, so the owner
    is told how many rows were left out of the analysis and which.
    """
    if not isinstance(raw_lines, list) or not raw_lines:
        raise InvalidSupplierLineError("the document produced no usable lines")
    if len(raw_lines) > MAX_SUPPLIER_LINES:
        raise InvalidSupplierLineError(
            f"a price list may carry at most {MAX_SUPPLIER_LINES} lines")

    excluded = [dict(row) for row in excluded_rows or []
                if isinstance(row, dict)]
    results = []
    for raw in raw_lines:
        try:
            line = build_supplier_line(raw)
        except InvalidSupplierLineError as exc:
            # One unreadable row is not a reason to refuse the rest of the
            # document, and not a row to guess at. It is reported.
            row = raw if isinstance(raw, dict) else {}
            price = row.get("price")
            excluded.append({
                "description": str(row.get("description") or "")[:200],
                "printedPrice": "" if price is None else str(price)[:40],
                "reason": "INVALID_ROW",
                "detail": str(exc)[:200],
                "source": row.get("source"),
            })
            continue
        result = match_supplier_line(data, line, threshold)
        # No decisions exist yet at extraction time, so this is the state the
        # row starts in. The API recomputes it once the owner's rulings are
        # known, which is why it is derived rather than stored.
        result.reviewState = review_state(result)
        results.append(result)
    if not results:
        raise InvalidSupplierLineError("the document produced no usable lines")

    _mark_repeats(results)
    return PriceListReview(
        supplierName=supplier_name or "Unknown supplier",
        documentDate=document_date,
        results=results,
        threshold=threshold,
        excluded=excluded,
    )


def exclusion_notice(count: int) -> Optional[str]:
    """The owner-facing sentence for rows left out of the price analysis."""
    if not count:
        return None
    if count == 1:
        return ("1 row could not be interpreted and was excluded from price "
                "analysis.")
    return (f"{count} rows could not be interpreted and were excluded from "
            f"price analysis.")


def _mark_repeats(results: List[SupplierLineResult]) -> None:
    """The same SKU on more than one row of one document.

    Different prices: the document contradicts itself, and none of its
    figures is more believable than another. Every row for that SKU becomes a
    CONFLICT with no comparison - so no alert, no material change and no
    confirmable cost - and waits for the owner. The same price: the first row
    stands and the others are DUPLICATE, so the change is judged once.
    """
    by_sku: Dict[str, List[SupplierLineResult]] = {}
    for result in results:
        if result.status == MATCHED and result.skuId:
            by_sku.setdefault(result.skuId, []).append(result)
    for rows in by_sku.values():
        if len(rows) < 2:
            continue
        prices = sorted({float(r.line.price) for r in rows})
        if len(prices) > 1:
            for r in rows:
                r.status = CONFLICT
                r.comparison = None
                r.conflictingPrices = prices
                r.reviewState = STATE_REVIEW_REQUIRED
        else:
            for r in rows[1:]:
                r.status = DUPLICATE
                r.comparison = None
                r.reviewState = rows[0].reviewState


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
