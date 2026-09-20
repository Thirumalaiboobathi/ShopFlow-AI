"""Reading a supplier price list with Nova Pro.

Bedrock is replaced by a scripted fake. What is tested is ShopFlow's side of
the contract: that an image is validated before it is sent, and that model
output is rejected unless it has the exact shape expected.
"""

from __future__ import annotations

import json

import pytest

from agent.vision import (
    MAX_ITEMS,
    ExtractionError,
    extract_price_list,
    parse_extraction,
    validate_image,
)

PNG = b"\x89PNG\r\n\x1a\n" + b"rest of a png"
JPEG = b"\xff\xd8\xff" + b"rest of a jpeg"

GOOD = {
    "supplier": {"name": "Sri Balaji Electricals"},
    "document": {"date": "15-09-2026"},
    "items": [{
        "description": "Finolex 1.5 sqmm FR Wire RED 90m coil",
        "supplierCode": None, "brand": "Finolex", "specification": "1.5 sqmm",
        "colour": "Red", "length": "90m", "unit": "coil", "price": 6300.0,
    }],
}


class FakeVision:
    def __init__(self, text):
        self.text = text
        self.calls = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        return {"output": {"message": {"content": [{"text": self.text}]}},
                "usage": {"inputTokens": 1034, "outputTokens": 465}}


# ---- image validation, before anything reaches Bedrock ----

def test_png_is_accepted():
    assert validate_image(PNG, "image/png") == "png"


def test_jpeg_is_accepted():
    assert validate_image(JPEG, "image/jpeg") == "jpeg"


def test_unsupported_type_is_rejected():
    with pytest.raises(ExtractionError):
        validate_image(b"%PDF-1.4", "application/pdf")


def test_empty_file_is_rejected():
    with pytest.raises(ExtractionError):
        validate_image(b"", "image/png")


def test_content_type_must_match_the_actual_bytes():
    """A PDF renamed as a PNG must not reach the model."""
    with pytest.raises(ExtractionError) as exc:
        validate_image(b"%PDF-1.4 payload", "image/png")
    assert "do not look like" in str(exc.value)


def test_invalid_image_is_rejected_without_calling_bedrock():
    fake = FakeVision(json.dumps(GOOD))
    with pytest.raises(ExtractionError):
        extract_price_list(b"%PDF", "image/png", client=fake)
    assert fake.calls == []


# ---- strict schema validation of model output ----

def test_well_formed_output_is_parsed():
    supplier, date, items = parse_extraction(json.dumps(GOOD))
    assert supplier == "Sri Balaji Electricals"
    assert date == "15-09-2026"
    assert items[0]["price"] == 6300.0


def test_output_wrapped_in_code_fences_is_accepted():
    fenced = "```json\n" + json.dumps(GOOD) + "\n```"
    supplier, _, items = parse_extraction(fenced)
    assert supplier == "Sri Balaji Electricals"
    assert len(items) == 1


def test_non_json_output_is_rejected():
    with pytest.raises(ExtractionError):
        parse_extraction("Here is the price list you asked for!")


def test_empty_output_is_rejected():
    with pytest.raises(ExtractionError):
        parse_extraction("")


def test_json_array_at_the_top_level_is_rejected():
    with pytest.raises(ExtractionError):
        parse_extraction("[]")


def test_missing_items_is_rejected():
    with pytest.raises(ExtractionError):
        parse_extraction(json.dumps({"supplier": {"name": "X"}}))


def test_empty_items_is_rejected():
    payload = {**GOOD, "items": []}
    with pytest.raises(ExtractionError):
        parse_extraction(json.dumps(payload))


def test_too_many_items_is_rejected():
    payload = {**GOOD, "items": [GOOD["items"][0]] * (MAX_ITEMS + 1)}
    with pytest.raises(ExtractionError):
        parse_extraction(json.dumps(payload))


def test_non_object_item_is_rejected():
    payload = {**GOOD, "items": ["Finolex wire 6300"]}
    with pytest.raises(ExtractionError):
        parse_extraction(json.dumps(payload))


def test_missing_supplier_name_is_tolerated():
    """A document with no printed supplier name is still readable."""
    payload = {"document": {"date": None}, "items": GOOD["items"]}
    supplier, date, items = parse_extraction(json.dumps(payload))
    assert supplier == ""
    assert date is None
    assert len(items) == 1


# ---- the full call ----

def test_extraction_sends_the_image_and_returns_parsed_rows():
    fake = FakeVision(json.dumps(GOOD))
    supplier, date, items, usage = extract_price_list(PNG, "image/png", client=fake)

    assert supplier == "Sri Balaji Electricals"
    assert items[0]["price"] == 6300.0
    assert usage["inputTokens"] == 1034

    sent = fake.calls[0]["messages"][0]["content"]
    assert sent[0]["image"]["format"] == "png"
    assert sent[0]["image"]["source"]["bytes"] == PNG
    assert fake.calls[0]["inferenceConfig"]["temperature"] == 0


def test_malformed_model_output_surfaces_as_an_extraction_error():
    fake = FakeVision("sorry, I cannot read that image")
    with pytest.raises(ExtractionError):
        extract_price_list(PNG, "image/png", client=fake)
