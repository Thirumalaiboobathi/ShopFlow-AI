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
from .credit import (
    APPROVED,
    BLOCKED,
    LIMIT_EXCEEDED,
    NO_CREDIT_ACCOUNT,
    check_credit,
    check_quote_credit,
    list_customers,
)
from .uom import (
    SUPPORTED_UOMS,
    base_equivalent,
    conversion_note,
    normalize_uom,
    product_uom,
    resolve_uom,
)
from .margin import (
    HEALTHY,
    LOW_MARGIN,
    MARGIN_REDUCED,
    MARGIN_WARNING_PERCENT,
    NEGATIVE_MARGIN,
    margin_alerts,
    margin_view,
    quotation_margin_impact,
    suggested_selling_price,
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
    "APPROVED", "BLOCKED", "LIMIT_EXCEEDED", "NO_CREDIT_ACCOUNT",
    "check_credit", "check_quote_credit", "list_customers",
    "SUPPORTED_UOMS", "base_equivalent", "conversion_note", "normalize_uom",
    "product_uom", "resolve_uom",
    "HEALTHY", "LOW_MARGIN", "MARGIN_REDUCED", "NEGATIVE_MARGIN",
    "MARGIN_WARNING_PERCENT", "margin_alerts", "margin_view",
    "quotation_margin_impact", "suggested_selling_price",
    "scenario_report",
]
