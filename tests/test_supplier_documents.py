"""Reading a supplier document, and what a read row is not allowed to do.

A dealer price list is a table, so Textract reads it: cells, coordinates and a
confidence score per word. The score is the point. A row read at 64% is a row
somebody should look at before the shop's purchase cost moves, and before this
there was no number with which to make that decision.

What the reader is NOT is the authority on anything. It turns pixels into text
and numbers and stops. Which SKU a row means is `engine.matching`, what the
shop paid before is `engine.pricing`, whether the difference matters is
`engine.supplier_prices`, and whether it affects purchasing is
`engine.cost_records` - none of which can call Textract or a model.

So most of this file is about the boundary:

    EXTRACTED -> MATCHED -> REVIEW_REQUIRED -> CONFIRMED

and about the one rule that makes it worth having: only CONFIRMED reaches
purchasing. A supplier cost that moved because a camera was slightly out of
focus is the failure this whole pipeline exists to prevent.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from agent.textract_reader import (  # noqa: E402
    CONFIDENCE_THRESHOLD,
    NOVA_PRO,
    TEXTRACT,
    TextractError,
    needs_review,
    parse_price,
    read_price_list,
    rows_from_blocks,
)
from engine.cost_records import build_cost_record  # noqa: E402
from engine.supplier_prices import (  # noqa: E402
    AMBIGUOUS,
    CONFIRMED,
    MATCHED,
    MIN_ROW_CONFIDENCE,
    REJECTED,
    REVIEW_STATES,
    STATE_CONFIRMED,
    STATE_MATCHED,
    STATE_REVIEW_REQUIRED,
    UNMATCHED,
    build_supplier_line,
    match_supplier_line,
    review_price_list,
    review_state,
)

WIRE = "W-FIN-1.5-RED-90M"
SWITCH = "SW-ANC-1W10A"
MCB = "MCB-HAV-SP-32A-C"

# The canonical supplier price story: the shop last paid 5,900 and the
# document says 6,300 - a 6.78% rise, and a 708 -> 308 margin.
CONFIRMED_WIRE_PRICE = 6300.0


# ---------------------------------------------------------------------------
# A Textract response, built by hand
# ---------------------------------------------------------------------------
# Shaped exactly as the real service returns one - verified against a live
# `analyze_document` call on frontend/site/sample-price-list.png, which reads
# five rows at 98.55% average word confidence. Built here rather than called
# so the suite runs offline and costs nothing.

def _word(text, confidence, ident):
    return {"Id": ident, "BlockType": "WORD", "Text": text,
            "Confidence": confidence}


def blocks_for(rows, headings=("DESCRIPTION", "RATE (Rs)"),
               lines=("SRI BALAJI ELECTRICALS", "Effective 15-09-2026")):
    """A TABLES response for `rows` of (description, price, confidence)."""
    blocks = [{"Id": "page", "BlockType": "PAGE"}]
    for index, text in enumerate(lines):
        blocks.append({"Id": f"line{index}", "BlockType": "LINE", "Text": text})

    cells, counter = [], 0
    table_rows = [(headings[0], headings[1], 99.9)] + list(rows)
    for row_index, (description, price, confidence) in enumerate(table_rows, 1):
        for column, value in ((1, description), (2, price)):
            counter += 1
            word_ids = []
            for part in str(value).split():
                counter += 1
                ident = f"w{counter}"
                blocks.append(_word(part, confidence, ident))
                word_ids.append(ident)
            cell_id = f"c{row_index}_{column}"
            cells.append(cell_id)
            blocks.append({
                "Id": cell_id, "BlockType": "CELL", "RowIndex": row_index,
                "ColumnIndex": column, "Confidence": 40.0,
                "Relationships": [{"Type": "CHILD", "Ids": word_ids}],
            })

    blocks.append({"Id": "table", "BlockType": "TABLE",
                   "Relationships": [{"Type": "CHILD", "Ids": cells}]})
    return blocks


class FakeTextract:
    def __init__(self, blocks=None, error=None):
        self._blocks = blocks or []
        self._error = error
        self.calls = []

    def analyze_document(self, **kwargs):
        self.calls.append(kwargs)
        if self._error:
            raise self._error
        return {"Blocks": self._blocks}


CLEAN_ROWS = [
    ("Finolex 1.5 sqmm FR Wire RED 90m coil", "6,300.00", 99.3),
    ("Anchor Modular Switch 1-Way 10A White", "58.99", 99.8),
    ("Havells MCB SP 32A C-Curve", "358.00", 99.7),
]


@pytest.fixture
def clean_blocks():
    return blocks_for(CLEAN_ROWS)


# ---------------------------------------------------------------------------
# 1. valid extraction
# ---------------------------------------------------------------------------

def test_a_price_list_is_read_into_rows(clean_blocks):
    rows, supplier, date = read_price_list(b"x",
                                           client=FakeTextract(clean_blocks))

    assert supplier == "SRI BALAJI ELECTRICALS"
    assert date == "15-09-2026"
    assert [r["description"] for r in rows] == [r[0] for r in CLEAN_ROWS]
    assert [r["price"] for r in rows] == [6300.0, 58.99, 358.0]
    assert all(r["source"] == TEXTRACT for r in rows)


def test_the_heading_row_is_not_a_product(clean_blocks):
    rows, _supplier, _date = read_price_list(b"x",
                                             client=FakeTextract(clean_blocks))
    assert all("DESCRIPTION" not in r["description"] for r in rows)
    assert len(rows) == len(CLEAN_ROWS)


def test_every_row_carries_its_own_provenance(clean_blocks):
    """Document, text, price, confidence, reader - per row, not per document."""
    rows, _s, _d = read_price_list(b"x", client=FakeTextract(clean_blocks))
    for row in rows:
        assert set(row) == {"description", "price", "confidence", "source"}
        assert row["confidence"] is not None
        assert row["price"] > 0


def test_the_extraction_is_synchronous_and_asks_for_tables(clean_blocks):
    """One call. No Step Functions, no job token, no completion topic."""
    client = FakeTextract(clean_blocks)
    read_price_list(b"x", client=client)
    assert len(client.calls) == 1
    assert client.calls[0]["FeatureTypes"] == ["TABLES"]


@pytest.mark.parametrize("text,expected", [
    ("6,300.00", 6300.0), ("58.99", 58.99), ("Rs 358", 358.0),
    ("₹6,300", 6300.0), ("18,450.00", 18450.0),
])
def test_printed_prices_are_read_as_written(text, expected):
    assert parse_price(text) == expected


@pytest.mark.parametrize("text", [
    "32A", "1.5 sqmm", "1-Way 10A", "C-Curve", "", "TOTAL", "GST extra 18%",
    "0", "-50",
])
def test_a_specification_is_never_read_as_a_price(text):
    """The difference between a 32-amp breaker and a 32-rupee one."""
    assert parse_price(text) is None


# ---------------------------------------------------------------------------
# 2. malformed and failed documents
# ---------------------------------------------------------------------------

def test_a_document_with_no_table_is_refused_not_guessed():
    with pytest.raises(TextractError):
        read_price_list(b"x", client=FakeTextract([
            {"Id": "p", "BlockType": "PAGE"},
            {"Id": "l", "BlockType": "LINE", "Text": "a photo of a cat"},
        ]))


def test_a_row_with_no_price_produces_no_row():
    """Not a zero, not a null, not a neighbouring row's figure. No row."""
    rows, _s, _d = rows_from_blocks(blocks_for([
        ("Finolex 1.5 sqmm FR Wire RED 90m coil", "6,300.00", 99.0),
        ("Anchor Modular Switch 1-Way 10A White", "ASK", 99.0),
    ]))
    assert [r["description"] for r in rows] == [
        "Finolex 1.5 sqmm FR Wire RED 90m coil"]


def test_textract_being_unavailable_raises_rather_than_returning_nothing():
    client = FakeTextract(error=RuntimeError("Textract is unavailable"))
    with pytest.raises(RuntimeError):
        read_price_list(b"x", client=client)


def test_an_empty_document_changes_no_pricing_state():
    """The failure path must leave the shop's costs exactly where they were."""
    with pytest.raises(TextractError):
        read_price_list(b"x", client=FakeTextract([]))


# ---------------------------------------------------------------------------
# 3. confidence and the review boundary
# ---------------------------------------------------------------------------

def test_a_low_confidence_row_needs_review():
    assert needs_review({"confidence": 64.6}) is True
    assert needs_review({"confidence": 99.3}) is False
    assert CONFIDENCE_THRESHOLD == MIN_ROW_CONFIDENCE == 90.0


def test_a_reader_that_reports_no_confidence_does_not_claim_doubt():
    """Nova Pro gives no score. Unknown is not the same as bad."""
    assert needs_review({"confidence": None, "source": NOVA_PRO}) is False


def test_a_low_confidence_match_is_held_for_review(seeded):
    """Confidently matched, but not confidently READ. That is still a question."""
    line = build_supplier_line({
        "description": "Anchor Modular Switch 1-Way 10A White",
        "price": 58.99, "confidence": 64.6, "source": TEXTRACT})
    result = match_supplier_line(seeded, line)

    assert result.status == MATCHED
    assert result.skuId == SWITCH
    assert review_state(result) == STATE_REVIEW_REQUIRED


def test_a_confident_unchanged_match_needs_no_review(seeded):
    line = build_supplier_line({
        "description": "Havells MCB SP 32A C-Curve", "price": 358.0,
        "confidence": 99.7, "source": TEXTRACT})
    result = match_supplier_line(seeded, line)

    assert result.status == MATCHED
    assert review_state(result) == STATE_MATCHED


def test_a_material_price_move_is_always_reviewed(seeded):
    """Read perfectly, matched exactly - and 6.78% more than last time."""
    line = build_supplier_line({
        "description": "Finolex 1.5 sqmm FR Wire RED 90m coil",
        "price": CONFIRMED_WIRE_PRICE, "confidence": 99.9, "source": TEXTRACT})
    result = match_supplier_line(seeded, line)

    assert result.status == MATCHED
    assert result.comparison.materialChange is True
    assert result.comparison.percentageDelta == 6.78
    assert review_state(result) == STATE_REVIEW_REQUIRED


def test_an_ambiguous_row_is_reviewed_never_chosen(seeded):
    """Finolex 1.5 sqmm with no colour matches several real products."""
    line = build_supplier_line({
        "description": "Finolex 1.5 sqmm FR Wire 90m coil", "price": 6300.0,
        "confidence": 99.6, "source": TEXTRACT})
    result = match_supplier_line(seeded, line)

    assert result.status == AMBIGUOUS
    assert result.skuId is None
    assert review_state(result) == STATE_REVIEW_REQUIRED


def test_an_unknown_product_is_reviewed_not_invented(seeded):
    line = build_supplier_line({
        "description": "Kaveri 4-core Armoured Cable 400 sqmm",
        "price": 18450.0, "confidence": 97.8, "source": TEXTRACT})
    result = match_supplier_line(seeded, line)

    assert result.status in (UNMATCHED, AMBIGUOUS)
    assert result.skuId is None or result.status == AMBIGUOUS
    assert review_state(result) == STATE_REVIEW_REQUIRED


def test_the_same_sku_twice_is_two_rows_and_neither_is_dropped(seeded):
    """A document may list a product twice. Both are shown; neither is merged."""
    review = review_price_list(seeded, "Sri Balaji", "15-09-2026", [
        {"description": "Havells MCB SP 32A C-Curve", "price": 358.0,
         "confidence": 99.0, "source": TEXTRACT},
        {"description": "Havells MCB SP 32A C-Curve", "price": 372.0,
         "confidence": 99.0, "source": TEXTRACT},
    ]).as_dict()

    assert review["lineCount"] == 2
    assert [line["skuId"] for line in review["lines"]] == [MCB, MCB]
    # Two different prices for one SKU is precisely the case where nothing may
    # be applied without the owner choosing.
    assert {line["line"]["price"] for line in review["lines"]} == {358.0, 372.0}


def test_every_state_is_one_of_the_four(seeded):
    review = review_price_list(seeded, "Sri Balaji", None, [
        {"description": d, "price": float(p.replace(",", "")), "confidence": c,
         "source": TEXTRACT}
        for d, p, c in CLEAN_ROWS
    ]).as_dict()
    for line in review["lines"]:
        assert line["reviewState"] in REVIEW_STATES


# ---------------------------------------------------------------------------
# 4. the confirmation boundary
# ---------------------------------------------------------------------------

def test_a_confirmed_row_reads_confirmed(seeded):
    line = build_supplier_line({
        "description": "Finolex 1.5 sqmm FR Wire RED 90m coil",
        "price": CONFIRMED_WIRE_PRICE, "confidence": 99.9})
    result = match_supplier_line(seeded, line)
    assert review_state(result, {WIRE: CONFIRMED}) == STATE_CONFIRMED


def test_a_rejected_row_does_not_become_matched(seeded):
    """Rejecting is a decision that the row must not be applied."""
    line = build_supplier_line({
        "description": "Finolex 1.5 sqmm FR Wire RED 90m coil",
        "price": CONFIRMED_WIRE_PRICE, "confidence": 99.9})
    result = match_supplier_line(seeded, line)
    assert review_state(result, {WIRE: REJECTED}) == STATE_REVIEW_REQUIRED


def test_only_confirmed_rows_reach_purchasing(seeded):
    """The rule the whole boundary exists for.

    Purchasing reads exactly one thing: the shop's confirmed cost records.
    An extracted row, a matched row and a reviewed row write none, so a
    document cannot move a purchase cost by being read - only by being agreed
    to. This asserts the mechanism rather than the intention: a cost record is
    produced by `build_cost_record`, and nothing in the extraction path calls
    it.
    """
    import ast
    import inspect

    from agent import textract_reader
    from engine import supplier_prices

    # Checked against the parsed module rather than its text, so a comment
    # that mentions cost records - and this file's own docstring does - is
    # not mistaken for a dependency on them.
    for module in (textract_reader, supplier_prices):
        tree = ast.parse(inspect.getsource(module))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.add(node.module or "")
                imported.update(alias.name for alias in node.names)
            elif isinstance(node, ast.Import):
                imported.update(alias.name for alias in node.names)
        assert not any("cost_records" in name for name in imported),             module.__name__
        assert "build_cost_record" not in imported, module.__name__
        assert not hasattr(module, "build_cost_record"), module.__name__

    # And a confirmation does write one, with the document's own figure.
    record = build_cost_record(seeded, WIRE, CONFIRMED_WIRE_PRICE,
                               source_job_id="job1", confirmed_at=1_758_000_000)
    assert record["confirmedCost"] == CONFIRMED_WIRE_PRICE
    assert record["skuId"] == WIRE


def test_extraction_never_writes_a_selling_price(seeded):
    """A supplier cost is not a retail price and may not become one."""
    before = seeded.product(WIRE).sellingPrice
    review_price_list(seeded, "Sri Balaji", None, [
        {"description": "Finolex 1.5 sqmm FR Wire RED 90m coil",
         "price": CONFIRMED_WIRE_PRICE, "confidence": 99.9}])
    assert seeded.product(WIRE).sellingPrice == before


# ---------------------------------------------------------------------------
# 5. the document review carries no surprises
# ---------------------------------------------------------------------------

def test_the_review_counts_what_needs_a_person(seeded):
    review = review_price_list(seeded, "Sri Balaji", "15-09-2026", [
        # confident, matched, unchanged
        {"description": "Havells MCB SP 32A C-Curve", "price": 358.0,
         "confidence": 99.7, "source": TEXTRACT},
        # confident, matched, materially more expensive
        {"description": "Finolex 1.5 sqmm FR Wire RED 90m coil",
         "price": CONFIRMED_WIRE_PRICE, "confidence": 99.3,
         "source": TEXTRACT},
        # badly read
        {"description": "Anchor Modular Switch 1-Way 10A White",
         "price": 58.99, "confidence": 64.6, "source": TEXTRACT},
    ]).as_dict()

    assert review["lineCount"] == 3
    assert review["matchedCount"] == 3
    assert review["materialChangeCount"] == 1
    assert review["lowConfidenceCount"] == 1
    assert review["reviewRequiredCount"] == 2
    assert review["readers"] == [TEXTRACT]


def test_the_review_payload_names_its_own_source(seeded):
    review = review_price_list(seeded, "Sri Balaji", None, [
        {"description": "Havells MCB SP 32A C-Curve", "price": 358.0}]).as_dict()
    assert review["source"] == "engine.supplier_prices.review_price_list"


def test_a_row_read_without_confidence_still_works(seeded):
    """The model reader's rows have no score and must not be second-class."""
    review = review_price_list(seeded, "Sri Balaji", None, [
        {"description": "Havells MCB SP 32A C-Curve", "price": 358.0,
         "source": NOVA_PRO}]).as_dict()

    line = review["lines"][0]
    assert line["line"]["confidence"] is None
    assert line["line"]["source"] == NOVA_PRO
    assert line["reviewState"] == STATE_MATCHED
    assert review["lowConfidenceCount"] == 0


def test_a_nonsense_confidence_value_is_discarded_not_trusted(seeded):
    line = build_supplier_line({
        "description": "Havells MCB SP 32A C-Curve", "price": 358.0,
        "confidence": "very sure"})
    assert line.confidence is None


def test_no_internal_cost_field_travels_on_an_extracted_row(seeded):
    """A row carries what was printed, not what the shop's catalogue says."""
    review = review_price_list(seeded, "Sri Balaji", None, [
        {"description": "Havells MCB SP 32A C-Curve", "price": 358.0,
         "confidence": 99.0}]).as_dict()
    blob = json.dumps(review["lines"][0]["line"])
    for internal in ("costPrice", "supplierId", "marginPerUnit", "onHand"):
        assert internal not in blob, internal
