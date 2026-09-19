"""ShopFlow deterministic business engine.

Every number a shop owner sees originates here. The engine has no dependency on
AWS, Bedrock, or any language model: it is plain Python over plain data, so the
claim "the AI never invents business numbers" is enforced by architecture
rather than by prompt instructions.
"""

from .models import (
    CustomerOrder,
    Dataset,
    InventoryItem,
    OrderLine,
    Product,
    Supplier,
    SupplierPrice,
    WeeklySales,
    money,
)
from .velocity import coverage_weeks, sales_velocity, velocity_for, coverage_for
from .shortage import committed_demand, shortage_qty, shortages, uncommitted_stock
from .pricing import (
    cheaper_alternatives,
    current_cost,
    detect_price_increases,
    margin_per_rupee,
    price_delta,
)
from .budget import (
    BudgetPlan,
    PlanLine,
    allocate_budget,
    restock_candidates,
    BUY,
    DEFER,
    PARTIAL,
    TIER1,
    TIER2,
)
from .scenarios import scenario_report

__all__ = [
    "CustomerOrder", "Dataset", "InventoryItem", "OrderLine", "Product",
    "Supplier", "SupplierPrice", "WeeklySales", "money",
    "coverage_weeks", "sales_velocity", "velocity_for", "coverage_for",
    "committed_demand", "shortage_qty", "shortages", "uncommitted_stock",
    "cheaper_alternatives", "current_cost", "detect_price_increases",
    "margin_per_rupee", "price_delta",
    "BudgetPlan", "PlanLine", "allocate_budget", "restock_candidates",
    "BUY", "DEFER", "PARTIAL", "TIER1", "TIER2",
    "scenario_report",
]
