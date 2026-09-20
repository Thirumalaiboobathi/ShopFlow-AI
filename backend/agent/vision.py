"""Read a photographed supplier price list with Nova Pro.

The model's entire job here is transcription: what does this document say. It
reads descriptions and printed prices and returns them as JSON. It does not
decide which catalogue product a line refers to, what the shop paid before, or
whether a change matters - that is `engine.supplier_prices`, which cannot call
a model.

Model output is validated against a strict shape and rejected if it does not
fit. A malformed extraction is an error, not something to be repaired into
something plausible.
"""

from __future__ import annotations

import json
import re
from typing import Dict, List, Optional, Tuple

from .orchestrator import DEFAULT_MODEL_ID, DEFAULT_REGION

MAX_OUTPUT_TOKENS = 3000
MAX_ITEMS = 50

SUPPORTED_FORMATS = {
    "image/png": "png",
    "image/jpeg": "jpeg",
    "image/jpg": "jpeg",
    "image/webp": "webp",
}

# Leading bytes for each accepted type, so a declared content type that does
# not match the actual file is rejected before it reaches the model.
_MAGIC = {
    "png": (b"\x89PNG\r\n\x1a\n",),
    "jpeg": (b"\xff\xd8\xff",),
    "webp": (b"RIFF",),
}

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


class ExtractionError(ValueError):
    """The document could not be read into the expected shape."""


EXTRACTION_PROMPT = """You are reading a photographed dealer price list from an electrical goods supplier in India.

Transcribe what the document says. Return JSON only, no prose and no code fences, in exactly this shape:

{
  "supplier": {"name": "string"},
  "document": {"date": "string or null"},
  "items": [
    {
      "description": "the product line exactly as printed",
      "supplierCode": "string or null",
      "brand": "string or null",
      "specification": "size or rating, e.g. 1.5 sqmm or 32A, or null",
      "colour": "string or null",
      "length": "string or null",
      "unit": "coil, piece, metre, or null",
      "price": 0.0
    }
  ]
}

Rules:
- Copy prices exactly as printed. Do not convert, round, or add tax.
- price must be a number, never a string, and never null.
- Fill brand, specification, colour and length only when the line states them. Use null otherwise. Do not guess.
- Include every product row. Ignore headers, totals, tax notes and footers.
- If the supplier name or date is not printed, use null."""


def validate_image(image_bytes: bytes, content_type: str) -> str:
    """Return the Bedrock image format, or raise if this is not an image we
    accept. Checks the file's own leading bytes, not just the declared type."""
    fmt = SUPPORTED_FORMATS.get((content_type or "").lower().strip())
    if fmt is None:
        raise ExtractionError(
            f"unsupported image type {content_type!r}; "
            f"accepted: {', '.join(sorted(SUPPORTED_FORMATS))}")
    if not image_bytes:
        raise ExtractionError("image is empty")

    magics = _MAGIC.get(fmt, ())
    if magics and not any(image_bytes.startswith(m) for m in magics):
        raise ExtractionError(
            f"file contents do not look like {fmt}; declared {content_type!r}")
    return fmt


def _strip_fences(text: str) -> str:
    return _FENCE.sub("", text or "").strip()


def parse_extraction(text: str) -> Tuple[str, Optional[str], List[Dict]]:
    """Validate the model's JSON into (supplier name, date, raw items).

    Only the shape is enforced here. Whether a price is sane, and which SKU a
    line means, belongs to the engine.
    """
    cleaned = _strip_fences(text)
    if not cleaned:
        raise ExtractionError("the model returned nothing")

    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise ExtractionError(f"model output was not valid JSON: {exc}") from exc

    if not isinstance(payload, dict):
        raise ExtractionError("model output must be a JSON object")

    supplier = payload.get("supplier")
    if supplier is not None and not isinstance(supplier, dict):
        raise ExtractionError("supplier must be an object")
    supplier_name = ""
    if isinstance(supplier, dict) and supplier.get("name"):
        supplier_name = str(supplier["name"]).strip()

    document = payload.get("document")
    if document is not None and not isinstance(document, dict):
        raise ExtractionError("document must be an object")
    document_date = None
    if isinstance(document, dict) and document.get("date"):
        document_date = str(document["date"]).strip() or None

    items = payload.get("items")
    if not isinstance(items, list):
        raise ExtractionError("items must be an array")
    if not items:
        raise ExtractionError("no product lines were found in the document")
    if len(items) > MAX_ITEMS:
        raise ExtractionError(
            f"document produced {len(items)} lines, more than the {MAX_ITEMS} allowed")
    for item in items:
        if not isinstance(item, dict):
            raise ExtractionError("each item must be an object")

    return supplier_name, document_date, items


def extract_price_list(
    image_bytes: bytes,
    content_type: str,
    *,
    client=None,
    model_id: str = DEFAULT_MODEL_ID,
) -> Tuple[str, Optional[str], List[Dict], Dict]:
    """Read one price-list image. Returns (supplier, date, items, usage)."""
    fmt = validate_image(image_bytes, content_type)

    if client is None:
        import boto3

        client = boto3.client("bedrock-runtime", region_name=DEFAULT_REGION)

    response = client.converse(
        modelId=model_id,
        messages=[{
            "role": "user",
            "content": [
                {"image": {"format": fmt, "source": {"bytes": image_bytes}}},
                {"text": EXTRACTION_PROMPT},
            ],
        }],
        inferenceConfig={"maxTokens": MAX_OUTPUT_TOKENS, "temperature": 0},
    )

    blocks = response["output"]["message"].get("content", [])
    text = " ".join(b["text"] for b in blocks if "text" in b)
    supplier_name, document_date, items = parse_extraction(text)
    return supplier_name, document_date, items, response.get("usage", {})
