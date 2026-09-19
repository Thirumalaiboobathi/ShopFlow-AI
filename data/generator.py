"""Deterministic seed data for the ShopFlow demo shop.

One shop, ~150 SKUs, 26 weeks of weekly sales, four suppliers, supplier price
history, inventory and committed customer orders.

Everything is generated from a fixed random seed, so the same catalog, the same
velocities and the same budget tradeoff appear in tests, in the API, in the UI
and in the demo video. No number in the product is typed by hand.

Run `python data/generator.py` to write the JSON fixtures.
"""

from __future__ import annotations

import json
import random
from datetime import date, timedelta
from pathlib import Path
from typing import Dict, List

import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))

from engine.models import (  # noqa: E402
    CustomerOrder,
    Dataset,
    InventoryItem,
    OrderLine,
    Product,
    Supplier,
    SupplierPrice,
    WeeklySales,
)

SEED = 20260919
SHOP_ID = "demo"

# Last complete sales week, anchored so the dataset is stable over time.
LAST_WEEK_START = date(2026, 9, 7)
HISTORY_WEEKS = 26

# ---------------------------------------------------------------------------
# Suppliers
# ---------------------------------------------------------------------------

SUPPLIERS = [
    Supplier("SUP-BALAJI", "Sri Balaji Electricals", leadTimeDays=3),
    Supplier("SUP-ANNAI", "Annai Agencies", leadTimeDays=5),
    Supplier("SUP-KMT", "KMT Traders", leadTimeDays=7),
    Supplier("SUP-VELAN", "Velan Distributors", leadTimeDays=10),
]

# Which supplier normally stocks which brand.
BRAND_SUPPLIER = {
    "Finolex": "SUP-BALAJI",
    "Polycab": "SUP-ANNAI",
    "RR Kabel": "SUP-KMT",
    "Anchor": "SUP-BALAJI",
    "Legrand": "SUP-VELAN",
    "GM Modular": "SUP-ANNAI",
    "Havells": "SUP-BALAJI",
    "Schneider": "SUP-VELAN",
    "Philips": "SUP-KMT",
    "Syska": "SUP-KMT",
    "Crompton": "SUP-ANNAI",
    "Orient": "SUP-KMT",
    "Generic": "SUP-ANNAI",
}

BRAND_CODE = {
    "Finolex": "FIN", "Polycab": "POL", "RR Kabel": "RRK", "Anchor": "ANC",
    "Legrand": "LEG", "GM Modular": "GM", "Havells": "HAV", "Schneider": "SCH",
    "Philips": "PHI", "Syska": "SYS", "Crompton": "CRO", "Orient": "ORI",
    "Generic": "GEN",
}

COLOUR_CODE = {"Red": "RED", "Blue": "BLU", "Black": "BLK", "White": "WHT",
               "Brown": "BRN", "Cool White": "CW", "Warm White": "WW"}

# Gross margin multiplier applied to cost, by category.
CATEGORY_MARGIN = {
    "Wire": 1.12,
    "Switch": 1.35,
    "MCB": 1.28,
    "LED Lamp": 1.45,
    "Ceiling Fan": 1.22,
    "Accessory": 1.50,
}


def _sku(*parts) -> str:
    return "-".join(str(p) for p in parts if p not in (None, ""))


def build_catalog() -> List[Product]:
    products: List[Product] = []

    def add(skuId, brand, category, spec, colour, length, unit, cost, name):
        selling = round(cost * CATEGORY_MARGIN[category], 2)
        products.append(
            Product(
                skuId=skuId, brand=brand, category=category, specification=spec,
                colour=colour, length=length, unit=unit,
                sellingPrice=selling, costPrice=round(cost, 2),
                supplierId=BRAND_SUPPLIER[brand], name=name,
            )
        )

    # ---- Wire (35) : the variant-heavy category that drives ambiguity ----
    wire_cost_90m = {"0.75": 3150.0, "1.0": 4100.0, "1.5": 5900.0,
                     "2.5": 9600.0, "4.0": 15200.0}
    wire_sets = [
        ("Finolex", ["1.0", "1.5", "2.5", "4.0"], ["Red", "Blue", "Black"]),
        ("Polycab", ["1.0", "1.5", "2.5"], ["Red", "Blue", "Black"]),
        ("Havells", ["1.5", "2.5"], ["Red", "Black"]),
        ("RR Kabel", ["1.0", "1.5", "2.5"], ["Red", "Blue", "Black"]),
    ]
    brand_factor = {"Finolex": 1.0, "Polycab": 0.97, "Havells": 1.03, "RR Kabel": 0.92}
    for brand, specs, colours in wire_sets:
        for spec in specs:
            for colour in colours:
                cost = wire_cost_90m[spec] * brand_factor[brand]
                add(
                    _sku("W", BRAND_CODE[brand], spec, COLOUR_CODE[colour], "90M"),
                    brand, "Wire", f"{spec} sqmm", colour, "90m", "coil", cost,
                    f"{brand} {spec} sqmm FR Wire {colour} 90m coil",
                )
    # A 180m variant so "Finolex 1.5 red wire" is also ambiguous on length.
    add(_sku("W", "FIN", "1.5", "RED", "180M"), "Finolex", "Wire", "1.5 sqmm",
        "Red", "180m", "coil", wire_cost_90m["1.5"] * 1.94,
        "Finolex 1.5 sqmm FR Wire Red 180m coil")

    # ---- Switches (24) : same spec across four brands ----
    switch_specs = [
        ("1-Way 10A", "1W10A", 58.0), ("2-Way 10A", "2W10A", 92.0),
        ("1-Way 16A", "1W16A", 126.0), ("Bell Push", "BELL", 74.0),
        ("Socket 6A", "SKT6A", 88.0), ("Socket 16A", "SKT16A", 148.0),
    ]
    switch_brand_factor = {"Anchor": 1.0, "Legrand": 1.85, "GM Modular": 0.88,
                           "Havells": 1.22}
    for brand, factor in switch_brand_factor.items():
        for spec, code, cost in switch_specs:
            add(_sku("SW", BRAND_CODE[brand], code), brand, "Switch", spec,
                "White", None, "piece", cost * factor,
                f"{brand} Modular Switch {spec} White")

    # ---- MCB (36) ----
    mcb_amp_cost = {6: 295.0, 10: 305.0, 16: 318.0, 20: 336.0, 32: 358.0, 40: 402.0}
    pole_factor = {"SP": 1.0, "DP": 1.95}
    mcb_brand_factor = {"Havells": 1.0, "Schneider": 1.18, "Legrand": 1.34}
    for brand, bf in mcb_brand_factor.items():
        for pole in ("SP", "DP"):
            for amp in (6, 10, 16, 20, 32, 40):
                cost = mcb_amp_cost[amp] * pole_factor[pole] * bf
                add(_sku("MCB", BRAND_CODE[brand], pole, f"{amp}A", "C"), brand,
                    "MCB", f"{pole} {amp}A C-Curve", None, None, "piece", cost,
                    f"{brand} MCB {pole} {amp}A C-Curve")

    # ---- LED lamps (24) ----
    led_cost = {5: 62.0, 9: 84.0, 12: 118.0, 18: 176.0}
    led_brand_factor = {"Philips": 1.0, "Syska": 0.82, "Havells": 0.93}
    for brand, bf in led_brand_factor.items():
        for w, cost in led_cost.items():
            for colour in ("Cool White", "Warm White"):
                add(_sku("LED", BRAND_CODE[brand], f"{w}W", COLOUR_CODE[colour]),
                    brand, "LED Lamp", f"{w}W B22", colour, None, "piece",
                    cost * bf, f"{brand} LED Bulb {w}W B22 {colour}")

    # ---- Ceiling fans (12) ----
    fan_brand_cost = {"Crompton": 1480.0, "Orient": 1620.0, "Havells": 1890.0}
    for brand, cost in fan_brand_cost.items():
        for size in ("1200MM", "900MM"):
            for colour in ("Brown", "White"):
                factor = 1.0 if size == "1200MM" else 0.86
                add(_sku("FAN", BRAND_CODE[brand], size, COLOUR_CODE[colour]),
                    brand, "Ceiling Fan", size.replace("MM", "mm Sweep"), colour,
                    None, "piece", cost * factor,
                    f"{brand} Ceiling Fan {size.replace('MM', 'mm')} {colour}")

    # ---- Accessories (16) ----
    accessories = [
        ("CONDUIT-20", "PVC Conduit Pipe 20mm", "20mm", "length", 62.0),
        ("CONDUIT-25", "PVC Conduit Pipe 25mm", "25mm", "length", 84.0),
        ("JB-4X4", "Junction Box 4x4", "4x4", "piece", 46.0),
        ("JB-6X4", "Junction Box 6x4", "6x4", "piece", 68.0),
        ("TAPE-PVC", "PVC Insulation Tape", "18mm x 10m", "piece", 11.0),
        ("HOLDER-B22", "Batten Holder B22", "B22", "piece", 34.0),
        ("ROSE-CEIL", "Ceiling Rose 3-Plate", "3-Plate", "piece", 29.0),
        ("PLUG-3PIN", "3-Pin Plug Top 6A", "6A", "piece", 42.0),
        ("EXT-4WAY", "Extension Board 4-Way", "4-Way", "piece", 268.0),
        ("LUG-CU-10", "Copper Lug 10 sqmm", "10 sqmm", "piece", 18.0),
        ("GLAND-PG13", "Cable Gland PG13", "PG13", "piece", 26.0),
        ("CLIP-CLAMP", "Cable Clip Clamp 20mm", "20mm", "pack", 38.0),
        ("FUSE-WIRE", "Fuse Wire Spool", "15A", "piece", 24.0),
        ("BELL-DOOR", "Door Bell Ding Dong", "230V", "piece", 186.0),
        ("REG-FAN", "Fan Regulator Step Type", "Step", "piece", 148.0),
        ("TESTER-LINE", "Line Tester Screwdriver", "500V", "piece", 32.0),
    ]
    for code, name, spec, unit, cost in accessories:
        add(_sku("ACC", code), "Generic", "Accessory", spec, None, None, unit,
            cost, name)

    return products


# ---------------------------------------------------------------------------
# Canonical demo scenario - planted, then measured by the engine
# ---------------------------------------------------------------------------

DEMO_ORDER_SKUS = {
    "SW-ANC-1W10A": 20,       # Anchor modular switches
    "W-FIN-1.5-RED-90M": 3,   # Finolex 1.5 sqmm red wire coils
    "MCB-HAV-SP-32A-C": 2,    # 32A MCB
}

# Stock levels chosen so the order produces a real mix: a shortage that is
# cheap to fix, a shortage that is expensive to fix, and a line already covered.
PLANTED_INVENTORY = {
    "SW-ANC-1W10A": 14,
    "W-FIN-1.5-RED-90M": 1,
    "MCB-HAV-SP-32A-C": 5,
}

# Weekly units for the last 8 weeks, for SKUs whose velocity must be stable
# because the demo narrates it out loud.
PLANTED_RECENT_SALES = {
    "W-FIN-1.5-RED-90M": [4, 5, 4, 4, 3, 5, 4, 5],   # 4.25 / week
    "SW-ANC-1W10A": [11, 9, 13, 10, 12, 8, 11, 10],  # 10.5 / week
    "MCB-HAV-SP-32A-C": [3, 2, 4, 3, 3, 2, 4, 3],    # 3.0 / week
}

DEAD_STOCK_SKU = "FAN-ORI-900MM-BRN"
SLOW_MOVING_SKU = "LED-SYS-18W-WW"
CHEAPER_ALTERNATIVE_SKU = "MCB-HAV-SP-32A-C"
PRICE_INCREASE_SKU = "W-FIN-1.5-RED-90M"


def week_starts() -> List[str]:
    return [
        (LAST_WEEK_START - timedelta(weeks=HISTORY_WEEKS - 1 - i)).isoformat()
        for i in range(HISTORY_WEEKS)
    ]


def _demand_class(rnd: random.Random, product: Product) -> str:
    """Assign a movement profile. Weighted so most stock is mid-to-slow."""
    roll = rnd.random()
    if product.category in ("Accessory", "Switch", "LED Lamp"):
        return "fast" if roll < 0.35 else "medium" if roll < 0.75 else "slow"
    if product.category == "Wire":
        return "fast" if roll < 0.25 else "medium" if roll < 0.70 else "slow"
    if product.category == "Ceiling Fan":
        return "medium" if roll < 0.45 else "slow"
    return "fast" if roll < 0.20 else "medium" if roll < 0.65 else "slow"


_BASE_DEMAND = {"fast": (6, 14), "medium": (2, 6), "slow": (0, 2)}


def build_sales(rnd: random.Random, products: List[Product]) -> Dict[str, List[WeeklySales]]:
    weeks = week_starts()
    sales: Dict[str, List[WeeklySales]] = {}

    for p in products:
        cls = _demand_class(rnd, p)
        low, high = _BASE_DEMAND[cls]
        base = rnd.uniform(low, high)
        rows = []
        for i, w in enumerate(weeks):
            # Mild seasonality plus noise; never negative.
            season = 1.0 + 0.15 * ((i % 13) / 13.0 - 0.5)
            units = max(0, int(round(rnd.gauss(base * season, base * 0.35))))
            rows.append(WeeklySales(p.skuId, w, units))
        sales[p.skuId] = rows

    # Dead stock: sold earlier in the period, nothing for the last 12 weeks.
    if DEAD_STOCK_SKU in sales:
        rows = sales[DEAD_STOCK_SKU]
        sales[DEAD_STOCK_SKU] = [
            WeeklySales(r.skuId, r.weekStart, r.unitsSold if i < HISTORY_WEEKS - 12 else 0)
            for i, r in enumerate(rows)
        ]

    # Slow mover: a trickle of sales against a large stock holding.
    if SLOW_MOVING_SKU in sales:
        rows = sales[SLOW_MOVING_SKU]
        pattern = [1, 0, 0, 1, 0, 0, 0, 1]
        sales[SLOW_MOVING_SKU] = [
            WeeklySales(r.skuId, r.weekStart,
                        pattern[i % len(pattern)] if i >= HISTORY_WEEKS - 8 else r.unitsSold)
            for i, r in enumerate(rows)
        ]

    # Demo SKUs get fixed recent weeks so narrated velocities never drift.
    for skuId, recent in PLANTED_RECENT_SALES.items():
        if skuId not in sales:
            continue
        rows = sales[skuId]
        head = rows[: HISTORY_WEEKS - len(recent)]
        tail = [
            WeeklySales(skuId, rows[HISTORY_WEEKS - len(recent) + i].weekStart, units)
            for i, units in enumerate(recent)
        ]
        sales[skuId] = head + tail

    return sales


def build_inventory(rnd: random.Random, products: List[Product],
                    sales: Dict[str, List[WeeklySales]]) -> Dict[str, InventoryItem]:
    inventory: Dict[str, InventoryItem] = {}
    for p in products:
        recent = sales[p.skuId][-8:]
        velocity = sum(w.unitsSold for w in recent) / max(1, len(recent))
        # Most stock sits somewhere between 0.5 and 6 weeks of cover.
        cover = rnd.uniform(0.5, 6.0)
        inventory[p.skuId] = InventoryItem(p.skuId, max(0, int(round(velocity * cover))))

    inventory[DEAD_STOCK_SKU] = InventoryItem(DEAD_STOCK_SKU, 9)
    inventory[SLOW_MOVING_SKU] = InventoryItem(SLOW_MOVING_SKU, 48)
    for skuId, qty in PLANTED_INVENTORY.items():
        inventory[skuId] = InventoryItem(skuId, qty)
    return inventory


def build_price_history(rnd: random.Random, products: List[Product]) -> Dict[str, List[SupplierPrice]]:
    history: Dict[str, List[SupplierPrice]] = {}
    old_date = (LAST_WEEK_START - timedelta(weeks=20)).isoformat()
    mid_date = (LAST_WEEK_START - timedelta(weeks=9)).isoformat()
    new_date = (LAST_WEEK_START - timedelta(days=2)).isoformat()

    for p in products:
        base = p.costPrice
        # Most SKUs drift slightly; a minority move enough to matter.
        drift = rnd.choice([0.0, 0.0, 0.0, 0.012, -0.008, 0.025, 0.04])
        rows = [
            SupplierPrice(p.skuId, p.supplierId, old_date, round(base / (1 + drift), 2)),
            SupplierPrice(p.skuId, p.supplierId, mid_date, round(base / (1 + drift / 2), 2)),
            SupplierPrice(p.skuId, p.supplierId, new_date, round(base, 2)),
        ]
        history[p.skuId] = rows

    # Planted supplier price increase on the wire the demo order needs:
    # 5900 -> 6300 is the rise the owner is asked to react to.
    history[PRICE_INCREASE_SKU] = [
        SupplierPrice(PRICE_INCREASE_SKU, "SUP-BALAJI", old_date, 5750.0),
        SupplierPrice(PRICE_INCREASE_SKU, "SUP-BALAJI", mid_date, 5900.0),
        SupplierPrice(PRICE_INCREASE_SKU, "SUP-BALAJI", new_date, 6300.0),
    ]

    # Planted cheaper alternative: a rival quotes the same MCB below the
    # incumbent. Advisory only - the owner decides whether to switch.
    incumbent = history[CHEAPER_ALTERNATIVE_SKU][-1].unitCost
    history[CHEAPER_ALTERNATIVE_SKU] = history[CHEAPER_ALTERNATIVE_SKU] + [
        SupplierPrice(CHEAPER_ALTERNATIVE_SKU, "SUP-KMT", new_date,
                      round(incumbent * 0.91, 2))
    ]
    return history


def build_orders() -> List[CustomerOrder]:
    return [
        CustomerOrder(
            orderId="ORD-2026-0918-01",
            customerName="Murugan Electricals (contractor)",
            placedDate="2026-09-18",
            promisedDate="2026-09-21",
            lines=[OrderLine(sku, qty) for sku, qty in DEMO_ORDER_SKUS.items()],
            committed=True,
        )
    ]


def build_dataset() -> Dataset:
    """The single source of truth for every ShopFlow demo number."""
    rnd = random.Random(SEED)
    products = build_catalog()
    sales = build_sales(rnd, products)
    inventory = build_inventory(rnd, products, sales)
    prices = build_price_history(rnd, products)

    # Planted SKU ids are written by hand and must match generated ids exactly.
    # A typo here would otherwise create a phantom row that never surfaces.
    catalog_ids = {p.skuId for p in products}
    planted = (
        set(DEMO_ORDER_SKUS) | set(PLANTED_INVENTORY) | set(PLANTED_RECENT_SALES)
        | {DEAD_STOCK_SKU, SLOW_MOVING_SKU, CHEAPER_ALTERNATIVE_SKU, PRICE_INCREASE_SKU}
    )
    unknown = sorted(planted - catalog_ids)
    if unknown:
        raise ValueError(f"planted SKU ids not present in catalog: {unknown}")

    return Dataset(
        products={p.skuId: p for p in products},
        inventory=inventory,
        sales=sales,
        suppliers={s.supplierId: s for s in SUPPLIERS},
        priceHistory=prices,
        orders=build_orders(),
    )


SEED_DIR = Path(__file__).resolve().parent / "seed"


def write_seed(data: Dataset, out_dir: Path = SEED_DIR) -> Dict[str, int]:
    out_dir.mkdir(parents=True, exist_ok=True)
    payloads = {
        "products": [vars(p) for p in data.products.values()],
        "inventory": [vars(i) for i in data.inventory.values()],
        "suppliers": [vars(s) for s in data.suppliers.values()],
        "sales": [vars(w) for rows in data.sales.values() for w in rows],
        "price_history": [vars(p) for rows in data.priceHistory.values() for p in rows],
        "orders": [
            {**vars(o), "lines": [vars(l) for l in o.lines]} for o in data.orders
        ],
    }
    counts = {}
    for name, rows in payloads.items():
        path = out_dir / f"{name}.json"
        path.write_text(json.dumps(rows, indent=2), encoding="utf-8")
        counts[name] = len(rows)
    return counts


if __name__ == "__main__":
    dataset = build_dataset()
    written = write_seed(dataset)
    print(f"seed written to {SEED_DIR}")
    for key, count in written.items():
        print(f"  {key:15} {count}")
