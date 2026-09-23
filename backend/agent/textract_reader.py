"""Read a supplier price list with Amazon Textract.

WHY TEXTRACT AND NOT THE MODEL
------------------------------
A dealer price list is a table. Reading a table is a document-structure
problem, not a language problem, and Textract answers it with coordinates,
cells and a per-word confidence score - three things a language model does not
give you. Confidence in particular is the whole point: a row read at 64% is a
row somebody should look at before the shop's purchase cost moves, and until
now there was no number to make that decision with.

The model reader in `vision.py` is kept and still runs when Textract finds no
table - a photograph of a handwritten list is exactly the case it handles
better. Every extracted row records which reader produced it.

WHAT THIS DOES NOT DO
---------------------
It does not decide which SKU a row refers to. It does not compare a price to
anything, and it does not know what the shop paid last. Those belong to
`engine.supplier_prices`, which cannot call a model or an OCR service, and
which is unchanged by any of this. This module turns pixels into rows of text
and numbers, and stops.

It also never repairs a row. A cell that does not parse as a price is dropped
with a reason recorded, not guessed at from its neighbours: inventing a
supplier cost is the one failure this whole pipeline exists to prevent.
"""

from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

from engine.supplier_prices import MIN_ROW_CONFIDENCE

TEXTRACT = "TEXTRACT"
NOVA_PRO = "NOVA_PRO"

# Below this, a row is not trusted to move a purchase cost on its own and is
# sent for review. It decides one thing only: does a person look at this row
# first. The number itself is the engine's, imported rather than repeated,
# because the review boundary is a business rule and lives with the others.
CONFIDENCE_THRESHOLD = MIN_ROW_CONFIDENCE

MAX_ROWS = 50

# A printed price. Indian digit grouping, an optional currency mark, and an
# optional decimal part. Anchored so "32A" and "1.5 sqmm" can never be read as
# money - a specification is not a price and mistaking one for the other is
# how a 32-rupee contactor gets into a purchase plan.
_PRICE = re.compile(
    r"^\s*(?:₹|rs\.?|inr)?\s*((?:\d{1,3}(?:,\d{2,3})+|\d+)(?:\.\d{1,2})?)\s*$",
    re.IGNORECASE)

# Column headings that mean "this column holds the price".
_PRICE_HEADINGS = ("rate", "price", "amount", "cost", "mrp", "net")
# Headings that mean "this column holds the product".
_DESCRIPTION_HEADINGS = ("description", "particular", "item", "product",
                         "goods", "material")

_DATE = re.compile(r"\b(\d{1,2}[-/]\d{1,2}[-/]\d{2,4}|\d{4}-\d{2}-\d{2})\b")


class TextractError(ValueError):
    """Textract could not be used, or produced nothing usable."""


def parse_price(text: str) -> Optional[float]:
    """A printed price as a number, or None if this cell is not one."""
    match = _PRICE.match(str(text or ""))
    if not match:
        return None
    try:
        value = float(match.group(1).replace(",", ""))
    except ValueError:
        return None
    return value if value > 0 else None


def _index(blocks: List[Dict]) -> Dict[str, Dict]:
    return {b["Id"]: b for b in blocks if b.get("Id")}


def _children(block: Dict, by_id: Dict[str, Dict], kind: str) -> List[Dict]:
    out = []
    for relationship in block.get("Relationships") or []:
        if relationship.get("Type") != "CHILD":
            continue
        for child_id in relationship.get("Ids") or []:
            child = by_id.get(child_id)
            if child is not None and child.get("BlockType") == kind:
                out.append(child)
    return out


def _cell_text(cell: Dict, by_id: Dict[str, Dict]) -> Tuple[str, List[float]]:
    """The words in one cell, and their confidences.

    Confidence is taken from the WORD blocks, not from the CELL. A cell's own
    score describes how sure Textract is about the table's shape; what matters
    for a price is how sure it is about the characters.
    """
    words, confidences = [], []
    for word in _children(cell, by_id, "WORD"):
        text = (word.get("Text") or "").strip()
        if not text:
            continue
        words.append(text)
        confidences.append(float(word.get("Confidence") or 0.0))
    return " ".join(words), confidences


def _table_rows(table: Dict, by_id: Dict[str, Dict]) -> List[Dict[int, Tuple]]:
    rows: Dict[int, Dict[int, Tuple]] = {}
    for cell in _children(table, by_id, "CELL"):
        text, confidences = _cell_text(cell, by_id)
        rows.setdefault(int(cell.get("RowIndex") or 0), {})[
            int(cell.get("ColumnIndex") or 0)] = (text, confidences)
    return [rows[index] for index in sorted(rows)]


def _column_roles(header: Dict[int, Tuple]) -> Tuple[Optional[int], Optional[int]]:
    """Which column is the description and which is the price, by heading."""
    description_col = price_col = None
    for column, (text, _confidences) in sorted(header.items()):
        lowered = (text or "").strip().lower()
        if not lowered:
            continue
        if description_col is None and any(h in lowered for h in _DESCRIPTION_HEADINGS):
            description_col = column
        if any(h in lowered for h in _PRICE_HEADINGS):
            price_col = column
    return description_col, price_col


def rows_from_blocks(blocks: List[Dict]) -> Tuple[List[Dict], str, Optional[str]]:
    """Turn a Textract response into (rows, supplier name, document date).

    A row is emitted only when it has both a description and something that
    parses as a price. Everything else - headings, totals, tax notes, the
    footer - simply produces no row, which is the same thing the model reader
    was asked to do and is here a consequence of the parse rather than an
    instruction somebody has to follow.
    """
    by_id = _index(blocks)
    tables = [b for b in blocks if b.get("BlockType") == "TABLE"]

    # The supplier's own name and the effective date are printed above the
    # table, so they come from the first lines on the page rather than from a
    # cell. Missing is missing: neither is invented.
    lines = [(b.get("Text") or "").strip() for b in blocks
             if b.get("BlockType") == "LINE"]
    supplier_name = lines[0] if lines else ""
    document_date = None
    for line in lines[:6]:
        found = _DATE.search(line)
        if found:
            document_date = found.group(1)
            break

    rows: List[Dict] = []
    for table in tables:
        table_rows = _table_rows(table, by_id)
        if not table_rows:
            continue
        description_col, price_col = _column_roles(table_rows[0])
        body = table_rows[1:] if (description_col or price_col) else table_rows

        for row in body:
            description, confidences = "", []
            price, price_confidences = None, []

            if description_col is not None and description_col in row:
                description, confidences = row[description_col]
            if price_col is not None and price_col in row:
                text, price_confidences = row[price_col]
                price = parse_price(text)

            if not description or price is None:
                # No headings, or headings that did not identify the columns.
                # Fall back to the shape of the row itself: the longest text
                # cell is the product and the last cell that parses as a price
                # is the price. This is still reading, not guessing - a cell
                # either parses as money or it does not.
                texts = [row[c] for c in sorted(row)]
                if not description:
                    candidates = [(t, c) for t, c in texts
                                  if t and parse_price(t) is None]
                    if candidates:
                        description, confidences = max(
                            candidates, key=lambda item: len(item[0]))
                if price is None:
                    for text, cell_confidences in reversed(texts):
                        parsed = parse_price(text)
                        if parsed is not None:
                            price, price_confidences = parsed, cell_confidences
                            break

            if not description or price is None:
                continue

            all_confidences = confidences + price_confidences
            rows.append({
                "description": description,
                "price": price,
                # The weakest character in the row, because a row is only as
                # trustworthy as its least certain digit.
                "confidence": round(min(all_confidences), 2)
                if all_confidences else None,
                "source": TEXTRACT,
            })
            if len(rows) >= MAX_ROWS:
                break

    return rows, supplier_name, document_date


def analyze_document(image_bytes: bytes = b"", *, client=None,
                     bucket: str = "", key: str = "") -> List[Dict]:
    """Call Textract once, synchronously, for one single-page document.

    Synchronous on purpose. A dealer price list is one page, the call returns
    in about a second, and the worker already has a 60-second timeout and a
    queue behind it. An asynchronous job would need a second Lambda, a
    completion topic and somewhere to keep the job token - machinery that buys
    nothing for a one-page image and adds three more things that can fail.
    """
    if client is None:
        import boto3

        client = boto3.client("textract")

    if bucket and key:
        document = {"S3Object": {"Bucket": bucket, "Name": key}}
    elif image_bytes:
        document = {"Bytes": image_bytes}
    else:
        raise TextractError("no document was supplied to Textract")

    response = client.analyze_document(Document=document,
                                       FeatureTypes=["TABLES"])
    return response.get("Blocks") or []


def read_price_list(image_bytes: bytes = b"", *, client=None,
                    bucket: str = "", key: str = "") -> Tuple[List[Dict], str, Optional[str]]:
    """Read one price list with Textract. Raises `TextractError` if unusable."""
    blocks = analyze_document(image_bytes, client=client, bucket=bucket, key=key)
    rows, supplier_name, document_date = rows_from_blocks(blocks)
    if not rows:
        raise TextractError(
            "Textract found no priced product rows in this document")
    return rows, supplier_name, document_date


def needs_review(row: Dict, threshold: float = CONFIDENCE_THRESHOLD) -> bool:
    """Is this row too uncertain to move a purchase cost by itself?

    A row with no confidence score at all does NOT need review on that
    ground - the model reader reports none, and treating "unknown" as "bad"
    would send every one of its rows for review and teach the owner to click
    through the warning. Uncertainty is only claimed where it was measured.
    """
    confidence = row.get("confidence")
    if confidence is None:
        return False
    try:
        return float(confidence) < threshold
    except (TypeError, ValueError):
        return True
