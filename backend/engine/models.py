"""Core domain types for ShopFlow.

Pure stdlib. No AWS, no Bedrock, no I/O. Everything here is a plain value
object so the business rules can be exercised in a local test run.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Dict, List, Optional


def money(value: float) -> float:
    """Rupees, rounded to paise. Keeps float drift out of reported totals."""
    return round(value + 0.0, 2)


# How a committed order line may be funded when cash is short.
#   PARTIAL_ALLOWED - buying some of the units still helps the customer
#                     (wire coils, switches, bulbs - anything sold by count)
#   ALL_OR_NOTHING  - a half delivery is worthless, so buy all or defer all
#                     (a matched set, or a single indivisible item)
# Everything is PARTIAL_ALLOWED today. The field exists so the distinction is
# recorded in the model rather than assumed by the allocator.
PARTIAL_ALLOWED = "PARTIAL_ALLOWED"
ALL_OR_NOTHING = "ALL_OR_NOTHING"


@dataclass(frozen=True)
class Product:
    skuId: str
    brand: str
    category: str
    specification: str
    colour: Optional[str]
    length: Optional[str]
    unit: str
    sellingPrice: float
    costPrice: float
    supplierId: str
    name: str
    fulfilmentPolicy: str = PARTIAL_ALLOWED
    # --- unit of measure -------------------------------------------------
    # The unit this SKU is stocked, priced and sold in, as a canonical value
    # (PIECE, COIL, BOX, PACK, LENGTH). `unit` above is the older free-text
    # display string and is kept because names and evidence already read from
    # it; `engine.uom.product_uom` is the single accessor and falls back to it.
    uom: str = ""
    # How many `uom` one catalogue line represents. One, everywhere in this
    # shop. It exists so the conversion below is expressed per stated quantity
    # rather than per assumed single unit.
    uomQuantity: float = 1.0
    # How much of `baseUom` one `uomQuantity` of `uom` contains - 90 metres in
    # a 90 m coil. Configured PER PRODUCT and left unset wherever the shop has
    # not measured it, because an assumed conversion is worse than none. Used
    # to SHOW an equivalence, never to rewrite an ordered quantity.
    baseQuantity: Optional[float] = None
    baseUom: Optional[str] = None
    # The variant a shop reaches for when the customer does not specify, e.g.
    # the 90m coil rather than the 180m one. Used only to break a tie between
    # otherwise-identical candidates, and always reported in the evidence so
    # the owner can see the choice was made and override it. It never crosses
    # a brand the customer actually named.
    isDefaultVariant: bool = False

    @property
    def marginPerUnit(self) -> float:
        return money(self.sellingPrice - self.costPrice)

    @property
    def marginPerRupee(self) -> float:
        """Margin earned per rupee of cash spent. 0 if cost is unknown."""
        if self.costPrice <= 0:
            return 0.0
        return (self.sellingPrice - self.costPrice) / self.costPrice

    # The tuple of attributes that distinguish variants of the same product.
    # Ambiguity detection compares candidates on exactly these fields.
    def variantKey(self) -> tuple:
        return (self.brand, self.category, self.specification, self.colour, self.length)


@dataclass
class InventoryItem:
    skuId: str
    onHand: int

    @property
    def available(self) -> int:
        """Negative stock is a data error, not negative demand. Floor at zero."""
        return max(0, self.onHand)


@dataclass(frozen=True)
class WeeklySales:
    skuId: str
    weekStart: str  # ISO date of the Monday
    unitsSold: int


@dataclass(frozen=True)
class Supplier:
    supplierId: str
    name: str
    leadTimeDays: int

    @property
    def leadTimeWeeks(self) -> float:
        return self.leadTimeDays / 7.0


@dataclass(frozen=True)
class SupplierPrice:
    skuId: str
    supplierId: str
    effectiveDate: str  # ISO date
    unitCost: float


@dataclass(frozen=True)
class OrderLine:
    skuId: str
    quantity: int


@dataclass(frozen=True)
class CustomerOrder:
    orderId: str
    customerName: str
    placedDate: str
    promisedDate: str
    lines: List[OrderLine]
    committed: bool = True


# A customer's credit status with this shop.
#   ACTIVE    may be given goods on credit, up to their limit
#   BLOCKED   may not, whatever the limit says - a decision the owner made
CUSTOMER_ACTIVE = "ACTIVE"
CUSTOMER_BLOCKED = "BLOCKED"


@dataclass(frozen=True)
class Customer:
    """One khata account: what this customer may owe, and what they owe now.

    This is a shop's own ledger card, not a credit bureau record. There is no
    score, no history of other shops, and nothing predictive - the limit is a
    number the owner set, and the outstanding is what is on the account today.

    Every customer in this repository is SYNTHETIC demo data. See
    `backend/seed_data/customers.json`.
    """

    customerId: str
    customerName: str
    phone: str
    creditLimit: float
    outstandingAmount: float
    paymentDueDays: int
    status: str = CUSTOMER_ACTIVE
    currency: str = "INR"

    @property
    def isActive(self) -> bool:
        return str(self.status).upper() == CUSTOMER_ACTIVE


@dataclass
class Dataset:
    """Everything the engine needs, indexed for lookup."""

    products: Dict[str, Product] = field(default_factory=dict)
    inventory: Dict[str, InventoryItem] = field(default_factory=dict)
    sales: Dict[str, List[WeeklySales]] = field(default_factory=dict)
    suppliers: Dict[str, Supplier] = field(default_factory=dict)
    priceHistory: Dict[str, List[SupplierPrice]] = field(default_factory=dict)
    orders: List[CustomerOrder] = field(default_factory=list)
    customers: Dict[str, Customer] = field(default_factory=dict)

    def product(self, skuId: str) -> Product:
        return self.products[skuId]

    def onHand(self, skuId: str) -> int:
        item = self.inventory.get(skuId)
        return item.available if item else 0

    def supplierFor(self, skuId: str) -> Supplier:
        return self.suppliers[self.products[skuId].supplierId]

    def weeklySales(self, skuId: str) -> List[WeeklySales]:
        """Chronologically ordered weekly aggregates."""
        return sorted(self.sales.get(skuId, []), key=lambda w: w.weekStart)

    def prices(self, skuId: str) -> List[SupplierPrice]:
        return sorted(self.priceHistory.get(skuId, []), key=lambda p: p.effectiveDate)

    def customer(self, customerId: str) -> Optional[Customer]:
        """The khata account, or None. A missing customer is never invented."""
        return self.customers.get(customerId)

    def committedOrders(self) -> List[CustomerOrder]:
        return [o for o in self.orders if o.committed]


def to_jsonable(obj) -> dict:
    return asdict(obj)
