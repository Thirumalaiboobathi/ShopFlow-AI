"""Load the generated seed fixtures into a Dataset.

Stdlib only, so the engine stays runnable anywhere - a test, a laptop, or a
Lambda cold start. The JSON is produced by data/generator.py and shipped inside
the deployment bundle, which is why there is only ever one set of numbers.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Dict, List

from .models import (
    Customer,
    CustomerOrder,
    Dataset,
    InventoryItem,
    OrderLine,
    Product,
    Supplier,
    SupplierPrice,
    WeeklySales,
)

DEFAULT_SEED_DIR = Path(__file__).resolve().parents[1] / "seed_data"


def _read(seed_dir: Path, name: str) -> List[dict]:
    return json.loads((seed_dir / f"{name}.json").read_text(encoding="utf-8"))


def load_dataset(seed_dir: Path | str | None = None) -> Dataset:
    directory = Path(seed_dir) if seed_dir else DEFAULT_SEED_DIR

    products = {p["skuId"]: Product(**p) for p in _read(directory, "products")}
    inventory = {i["skuId"]: InventoryItem(**i) for i in _read(directory, "inventory")}
    suppliers = {s["supplierId"]: Supplier(**s) for s in _read(directory, "suppliers")}

    sales: Dict[str, List[WeeklySales]] = {}
    for row in _read(directory, "sales"):
        sales.setdefault(row["skuId"], []).append(WeeklySales(**row))

    prices: Dict[str, List[SupplierPrice]] = {}
    for row in _read(directory, "price_history"):
        prices.setdefault(row["skuId"], []).append(SupplierPrice(**row))

    orders = [
        CustomerOrder(
            orderId=o["orderId"], customerName=o["customerName"],
            placedDate=o["placedDate"], promisedDate=o["promisedDate"],
            lines=[OrderLine(**l) for l in o["lines"]],
            committed=o.get("committed", True),
        )
        for o in _read(directory, "orders")
    ]

    # Khata accounts. Tolerated as absent rather than required, so a dataset
    # assembled from a partial fixture directory still loads - a shop with no
    # credit accounts is a legitimate shop, and `credit.check_credit` answers
    # NO_CREDIT_ACCOUNT for every id in that case rather than crashing.
    customers = {}
    if (directory / "customers.json").exists():
        customers = {c["customerId"]: Customer(**c)
                     for c in _read(directory, "customers")}

    return Dataset(products=products, inventory=inventory, sales=sales,
                   suppliers=suppliers, priceHistory=prices, orders=orders,
                   customers=customers)


@lru_cache(maxsize=1)
def cached_dataset() -> Dataset:
    """Loaded once per process - a Lambda container reuses it across requests."""
    return load_dataset()
