"""Shared fixtures.

Two kinds of dataset are used here. `seeded` is the real 147-SKU demo shop and
is used for integration-level assertions. `make_dataset` builds a tiny, fully
controlled shop so each rule can be pinned to an exact expected number rather
than to whatever the generator happened to produce.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Dict, Iterable, List, Sequence

import pytest

from engine.models import (
    CustomerOrder,
    Dataset,
    InventoryItem,
    OrderLine,
    Product,
    Supplier,
    SupplierPrice,
    WeeklySales,
)

from data.generator import build_dataset

WEEK0 = date(2026, 3, 16)


def weeks(n: int) -> List[str]:
    return [(WEEK0 + timedelta(weeks=i)).isoformat() for i in range(n)]


def make_product(
    skuId: str,
    cost: float,
    selling: float,
    supplierId: str = "S-FAST",
    *,
    brand: str = "BrandA",
    category: str = "Wire",
    specification: str = "1.5 sqmm",
    colour: str | None = None,
    length: str | None = None,
) -> Product:
    return Product(
        skuId=skuId, brand=brand, category=category, specification=specification,
        colour=colour, length=length, unit="piece", sellingPrice=selling,
        costPrice=cost, supplierId=supplierId, name=skuId,
    )


def make_dataset(
    products: Sequence[Product],
    *,
    inventory: Dict[str, int] | None = None,
    velocity: Dict[str, float] | None = None,
    orders: Iterable[CustomerOrder] = (),
    prices: Dict[str, List[SupplierPrice]] | None = None,
    suppliers: Sequence[Supplier] | None = None,
    history_weeks: int = 8,
) -> Dataset:
    """Build a dataset whose velocities are exactly what the test asks for."""
    suppliers = suppliers or [
        Supplier("S-FAST", "Fast Supplier", leadTimeDays=7),
        Supplier("S-SLOW", "Slow Supplier", leadTimeDays=14),
    ]
    inventory = inventory or {}
    velocity = velocity or {}
    week_list = weeks(history_weeks)

    sales: Dict[str, List[WeeklySales]] = {}
    for p in products:
        per_week = velocity.get(p.skuId, 0.0)
        sales[p.skuId] = [
            WeeklySales(p.skuId, w, int(round(per_week))) for w in week_list
        ]

    return Dataset(
        products={p.skuId: p for p in products},
        inventory={p.skuId: InventoryItem(p.skuId, inventory.get(p.skuId, 0))
                   for p in products},
        sales=sales,
        suppliers={s.supplierId: s for s in suppliers},
        priceHistory=prices or {},
        orders=list(orders),
    )


def make_order(
    orderId: str,
    lines: Dict[str, int],
    *,
    promisedDate: str = "2026-09-21",
    committed: bool = True,
) -> CustomerOrder:
    return CustomerOrder(
        orderId=orderId,
        customerName="Test Customer",
        placedDate="2026-09-18",
        promisedDate=promisedDate,
        lines=[OrderLine(sku, qty) for sku, qty in lines.items()],
        committed=committed,
    )


@pytest.fixture(scope="session")
def seeded() -> Dataset:
    return build_dataset()


# The canonical order as a customer writes it, and one model turn that reads
# it properly: a search per line, then the quotation, all in the same turn.
#
# A scripted quotation needs both halves now. The quote is held to the
# quantities the customer wrote (agent.quantity_guard), and to a search for
# every product the customer named (agent.line_guard.uncovered_terms), so a
# quote scripted against the placeholder text "order", or against the real
# order with nothing searched, is correctly withheld.
CANONICAL_ORDER = ("Anna, 20 Anchor modular switches 1-Way 10A, "
                   "3 coils Finolex 1.5 sq mm red wire 90m, 2 Havells MCB SP 32A.")

CANONICAL_SEARCHES = [
    {"requestedText": "20 Anchor modular switches 1-Way 10A",
     "brand": "Anchor", "category": "Switch", "specification": "1-Way 10A"},
    {"requestedText": "3 coils Finolex 1.5 sq mm red wire 90m",
     "brand": "Finolex", "category": "Wire", "colour": "Red", "length": "90m",
     "uom": "COIL"},
    {"requestedText": "2 Havells MCB SP 32A",
     "brand": "Havells", "category": "MCB", "specification": "SP 32A"},
]


def canonical_quote_turn(items, prefix: str = "c") -> dict:
    """One assistant turn: the three canonical searches, then calculate_quote."""
    content = [{"toolUse": {"toolUseId": f"{prefix}{i}", "name": "search_catalog",
                            "input": search}}
               for i, search in enumerate(CANONICAL_SEARCHES)]
    content.append({"toolUse": {"toolUseId": f"{prefix}q", "name": "calculate_quote",
                                "input": {"items": items}}})
    return {"role": "assistant", "content": content}
