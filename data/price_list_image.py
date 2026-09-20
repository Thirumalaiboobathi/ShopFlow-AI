"""Generate the canonical demo supplier price list as an image.

The rates are derived from the seeded catalogue rather than typed, so the
figures the demo shows trace back to the same dataset as everything else. One
line carries the planted rise the owner is meant to catch; the rest exercise
the paths that are easy to get wrong - a change too small to matter, an
unchanged line, a description that matches several products, and a product the
shop does not stock.

Run `python data/price_list_image.py` to write frontend/site/sample-price-list.png.
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "backend"))

from engine.loader import load_dataset  # noqa: E402
from engine.pricing import current_cost  # noqa: E402

OUT_PATH = ROOT / "frontend" / "site" / "sample-price-list.png"

SUPPLIER = "SRI BALAJI ELECTRICALS"
LOCATION = "Madurai"
EFFECTIVE = "15-09-2026"

# The planted rise: the shop last paid 5,900 for this coil.
PLANTED_WIRE_SKU = "W-FIN-1.5-RED-90M"
PLANTED_WIRE_PRICE = 6300.00


def _font(size: int, bold: bool = False):
    names = (["arialbd.ttf", "Arial_Bold.ttf", "DejaVuSans-Bold.ttf"] if bold
             else ["arial.ttf", "Arial.ttf", "DejaVuSans.ttf"])
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def build_rows(data) -> list[tuple[str, float]]:
    """The printed rows, priced from the catalogue where they are unchanged."""
    switch = data.product("SW-ANC-1W10A")
    mcb = data.product("MCB-HAV-SP-32A-C")

    return [
        # Matched, and materially more expensive: 5,900 -> 6,300 is +6.78%.
        ("Finolex 1.5 sqmm FR Wire RED 90m coil", PLANTED_WIRE_PRICE),
        # Matched, but the move is too small to be worth interrupting anyone.
        (f"{switch.brand} Modular Switch 1-Way 10A White",
         round(current_cost(data, switch.skuId) * 1.017, 2)),
        # Matched and unchanged.
        (f"{mcb.brand} MCB SP 32A C-Curve", current_cost(data, mcb.skuId)),
        # Ambiguous: the shop stocks this wire in three colours.
        ("Finolex 1.5 sqmm FR Wire 90m coil", 6300.00),
        # Not in the catalogue at all.
        ("Kaveri 4-core Armoured Cable 25 sqmm", 18450.00),
    ]


def render(rows: list[tuple[str, float]]) -> Image.Image:
    width, height = 1000, 520
    img = Image.new("RGB", (width, height), (253, 252, 249))
    d = ImageDraw.Draw(img)

    title = _font(26, bold=True)
    meta = _font(15)
    head = _font(15, bold=True)
    body = _font(17)

    d.text((40, 30), SUPPLIER, fill=(15, 15, 15), font=title)
    d.text((40, 66), f"{LOCATION}   |   Dealer price list", fill=(90, 90, 90), font=meta)
    d.text((40, 88), f"Effective {EFFECTIVE}   |   GST extra 18%",
           fill=(90, 90, 90), font=meta)

    d.line((40, 120, width - 40, 120), fill=(20, 20, 20), width=3)
    d.text((45, 132), "DESCRIPTION", fill=(40, 40, 40), font=head)
    d.text((width - 200, 132), "RATE (Rs)", fill=(40, 40, 40), font=head)
    d.line((40, 156, width - 40, 156), fill=(180, 180, 180), width=1)

    y = 172
    for name, rate in rows:
        d.text((45, y), name, fill=(25, 25, 25), font=body)
        d.text((width - 200, y), f"{rate:,.2f}", fill=(25, 25, 25), font=body)
        y += 42
        d.line((40, y - 10, width - 40, y - 10), fill=(226, 224, 219), width=1)

    d.line((40, y + 4, width - 40, y + 4), fill=(20, 20, 20), width=2)
    d.text((45, y + 16), "Prices subject to change without notice. E & O E.",
           fill=(120, 120, 120), font=meta)
    return img


def main() -> None:
    data = load_dataset()
    rows = build_rows(data)

    previous = current_cost(data, PLANTED_WIRE_SKU)
    delta = PLANTED_WIRE_PRICE - previous
    pct = delta / previous * 100

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    render(rows).save(OUT_PATH)

    print(f"written {OUT_PATH}  ({OUT_PATH.stat().st_size:,} bytes)")
    print("\nprinted rows:")
    for name, rate in rows:
        print(f"  {rate:>10,.2f}  {name}")
    print(f"\nplanted change on {PLANTED_WIRE_SKU}:")
    print(f"  shop last paid   Rs {previous:,.2f}")
    print(f"  price list says  Rs {PLANTED_WIRE_PRICE:,.2f}")
    print(f"  delta            Rs {delta:,.2f}  ({pct:+.2f}%)")


if __name__ == "__main__":
    main()
