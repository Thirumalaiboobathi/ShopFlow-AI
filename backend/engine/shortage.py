"""Committed customer demand and the resulting stock shortages."""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from typing import Dict, Iterable, List

from .models import CustomerOrder, Dataset


@dataclass(frozen=True)
class Shortage:
    skuId: str
    committedQty: int
    onHand: int
    shortageQty: int
    earliestPromisedDate: str

    @property
    def isShort(self) -> bool:
        return self.shortageQty > 0

    def as_evidence(self) -> dict:
        return {
            "skuId": self.skuId,
            "committedQty": self.committedQty,
            "onHand": self.onHand,
            "shortageQty": self.shortageQty,
            "earliestPromisedDate": self.earliestPromisedDate,
        }


def committed_demand(orders: Iterable[CustomerOrder]) -> Dict[str, int]:
    """Total units promised to customers, per SKU, across committed orders."""
    demand: Dict[str, int] = defaultdict(int)
    for order in orders:
        if not order.committed:
            continue
        for line in order.lines:
            demand[line.skuId] += max(0, line.quantity)
    return dict(demand)


def earliest_promise(orders: Iterable[CustomerOrder]) -> Dict[str, str]:
    """Soonest promised date per SKU - the tie-break for Tier 1 ordering."""
    promise: Dict[str, str] = {}
    for order in orders:
        if not order.committed:
            continue
        for line in order.lines:
            current = promise.get(line.skuId)
            if current is None or order.promisedDate < current:
                promise[line.skuId] = order.promisedDate
    return promise


def shortage_qty(committed: int, on_hand: int) -> int:
    """Units that must be bought to honour commitments. Never negative."""
    return max(0, max(0, committed) - max(0, on_hand))


def shortages(data: Dataset, orders: Iterable[CustomerOrder] | None = None) -> List[Shortage]:
    """Every committed SKU with its shortage, including zero-shortage lines.

    Zero-shortage entries are kept so the UI can show "no action needed" for a
    line the customer ordered, rather than silently omitting it.
    """
    order_list = list(data.committedOrders() if orders is None else orders)
    demand = committed_demand(order_list)
    promise = earliest_promise(order_list)

    result: List[Shortage] = []
    for skuId in sorted(demand):
        on_hand = data.onHand(skuId)
        result.append(
            Shortage(
                skuId=skuId,
                committedQty=demand[skuId],
                onHand=on_hand,
                shortageQty=shortage_qty(demand[skuId], on_hand),
                earliestPromisedDate=promise.get(skuId, ""),
            )
        )
    return result


def uncommitted_stock(data: Dataset, skuId: str, demand: Dict[str, int]) -> int:
    """Stock left for walk-in sales once commitments are reserved.

    Restocking urgency is judged on this figure, not raw on-hand, otherwise a
    product entirely spoken for by an order looks comfortably stocked.
    """
    return max(0, data.onHand(skuId) - demand.get(skuId, 0))
