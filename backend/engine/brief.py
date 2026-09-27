"""Today's shop brief: what the owner should know this morning.

WHAT IT IS
----------
One structured summary of facts the other engines already produce:

    shortages        committed customer orders the shelf cannot fill
                     (the purchase planner's tier 1)
    lowStock         products below their reorder point (the planner's
                     restock tier), highest stock-out risk first
    supplierAlerts   confirmed supplier cost moves, judged by
                     engine.price_alerts - which reuses the walk-away price
    marginRisks      products the margin engine calls LOW or NEGATIVE
    planner          the purchase plan at the owner's weekly budget
    walkAway         the walk-away price for each supplier alert
    promiseKeeping   customer commitments kept apart from discretionary
                     restock: what must be bought to keep a promise, at what
                     cost, and whether stock beyond that should be bought at
                     the current supplier cost given the walk-away price
    priorityActions  a short list chosen by fixed rules from the above

Nothing is calculated here that another engine owns: this module selects,
orders and counts. Every number in `lines` and `priorityActions` is a number
in the structure beside it, which `check_grounding` proves on every call.

WHAT IT IS NOT
--------------
It is not a dashboard and not a forecast. It writes nothing. A language model
may later turn it into two or three sentences (agent.brief_summary); the
structure is complete without that, and is what the UI shows.
"""

from __future__ import annotations

import datetime as _dt
from decimal import Decimal
from typing import Dict, List, Optional

from .margin import LOW_MARGIN, NEGATIVE_MARGIN, margin_alerts, previous_supplier_cost
from .messages import format_rupees
from .models import Dataset
from .price_alerts import CRITICAL, AlertConfig, alert_config, evaluate_price_change
from .whatif import DEFAULT_BUDGET, _plan, check_grounding

MAX_LOW_STOCK = 5
MAX_ACTIONS = 4
IST = _dt.timezone(_dt.timedelta(hours=5, minutes=30))


def _today(now: Optional[int]) -> str:
    moment = (_dt.datetime.fromtimestamp(now, IST) if now is not None
              else _dt.datetime.now(IST))
    return moment.date().isoformat()


def build_brief(data: Dataset, confirmed_costs: Optional[Dict[str, float]] = None,
                *, budget: float = DEFAULT_BUDGET,
                config: Optional[AlertConfig] = None,
                now: Optional[int] = None) -> dict:
    """The brief for one shop, from its catalogue and confirmed costs."""
    confirmed = {k: float(v) for k, v in (confirmed_costs or {}).items()
                 if k in data.products}
    cfg = config or alert_config()
    plan = _plan(data, budget, confirmed)

    shortages = []
    for c in plan["commitments"]:
        ev = c.get("evidence") or {}
        shortages.append({
            "skuId": c["skuId"], "product": c.get("productName"),
            "committedQty": ev.get("committedQty"), "onHand": ev.get("onHand"),
            "shortageQty": ev.get("shortageQty"),
            "promisedBy": ev.get("earliestPromisedDate"),
            "funded": bool(c.get("selected")) and
            c.get("fundedQty") == c.get("requestedQty"),
            "cost": c.get("fullLineCost"),
        })

    restock = plan["restockSelected"] + plan["restockDeferred"]
    restock = sorted(restock, key=lambda r: -((r.get("evidence") or {})
                                              .get("stockoutRisk") or 0))
    low_stock = [{
        "skuId": r["skuId"], "product": r.get("productName"),
        "onHand": r.get("currentStock"),
        "coverageWeeks": (r.get("evidence") or {}).get("coverageWeeks"),
        "stockoutRisk": (r.get("evidence") or {}).get("stockoutRisk"),
        "fundedThisWeek": bool(r.get("selected")),
    } for r in restock[:MAX_LOW_STOCK]]

    supplier_alerts = []
    for sku_id, cost in sorted(confirmed.items()):
        previous = previous_supplier_cost(data, sku_id)
        if previous is None or previous <= 0 or previous == cost:
            continue
        alert = evaluate_price_change(data, sku_id, previous, cost,
                                      confirmed_costs=confirmed, budget=budget,
                                      config=cfg, now=now)
        if alert["alert"]:
            supplier_alerts.append(alert)
    supplier_alerts.sort(key=lambda a: (a["severity"] != CRITICAL,
                                        -a["absoluteDelta"]))

    margin_risks = [{
        "skuId": m["skuId"], "product": m.get("productName"),
        "marginAmount": m.get("newMarginAmount", m.get("oldMarginAmount")),
        "marginPercent": m.get("newMarginPercent", m.get("oldMarginPercent")),
        "status": m.get("status"),
    } for m in margin_alerts(data, confirmed,
                             warning_percent=cfg.marginFloorPercent)
        if m.get("status") in (LOW_MARGIN, NEGATIVE_MARGIN)]

    walk_away = [{
        "skuId": a["skuId"], "product": a["product"],
        "walkAwayPrice": a["walkAway"]["price"],
        "bindingLimit": a["walkAway"]["bindingLimit"],
        "currentCost": a["newCost"],
        "aboveBy": (a["walkAway"]["differenceFromCurrentCost"]
                    if a["walkAway"]["currentCostAboveWalkAway"] else 0.0),
    } for a in supplier_alerts]

    promise_keeping = _promise_keeping(plan, supplier_alerts)

    planner = {
        "budget": plan["budget"], "commitmentCost": plan["commitmentCost"],
        "restockCost": plan["restockCost"], "totalSpend": plan["totalSpend"],
        "remaining": plan["remaining"],
        "allCommitmentsFunded": plan["allCommitmentsFunded"],
        "restockSelected": plan["counts"]["restockSelected"],
        "restockDeferred": plan["counts"]["restockDeferred"],
    }

    brief = {
        "date": _today(now),
        "generatedAt": now,
        "shortages": shortages,
        "lowStock": low_stock,
        "supplierAlerts": supplier_alerts,
        "marginRisks": margin_risks,
        "planner": planner,
        "walkAway": walk_away,
        "promiseKeeping": promise_keeping,
        "counts": {
            "shortages": len(shortages),
            "unfundedShortages": sum(1 for s in shortages if not s["funded"]),
            "lowStock": len(restock),
            "supplierAlerts": len(supplier_alerts),
            "criticalAlerts": sum(1 for a in supplier_alerts
                                  if a["severity"] == CRITICAL),
            "marginRisks": len(margin_risks),
        },
        "config": cfg.as_dict(),
        "source": "engine.brief",
        "synthetic": True,
    }
    brief["priorityActions"] = _actions(brief)
    brief["lines"] = _lines(brief)
    brief["hasAttention"] = bool(brief["priorityActions"])
    grounded, unsupported = check_grounding(
        {"rows": [brief], "explanation": " ".join(
            brief["lines"] + [a["text"] for a in brief["priorityActions"]])},
        ignore=[x.get("product") or "" for x in
                shortages + low_stock + supplier_alerts + margin_risks])
    brief["grounded"] = grounded
    brief["ungroundedNumbers"] = unsupported
    return brief


# What each discretionary restock line comes to, and why.
DO_NOT_BUY = "DO_NOT_BUY"      # current supplier cost is above the walk-away
BUY = "BUY"                    # the planner funds it this week
DEFER = "DEFER"                # the planner could not fund it this week
NOT_NEEDED = "NOT_NEEDED"      # not below its reorder point


def _money(value) -> float:
    return float(Decimal(str(value)).quantize(Decimal("0.01")))


def _promise_keeping(plan: dict, supplier_alerts: List[dict]) -> dict:
    """Keeping customer promises, kept apart from discretionary restock.

    The brief used to say "do not restock Finolex at 6,300" while the same
    plan bought two Finolex coils at 6,300 for customers already promised
    them. Both were right - they are different purchases - and side by side
    they read as a contradiction. This names them apart.

    Every figure is the purchase planner's (commitments, restock quantities,
    unit costs, funded or not) or the walk-away engine's (via the supplier
    alert). The one product here - the premium paid over the walk-away price
    to keep a promise - is (unit cost - walk-away price) x committed quantity
    of those two engines' own figures.
    """
    walk = {a["skuId"]: a for a in supplier_alerts}
    commitments = []
    for c in plan["commitments"]:
        ev = c.get("evidence") or {}
        qty, unit = c.get("requestedQty"), c.get("unitCost")
        funded = bool(c.get("selected")) and c.get("fundedQty") == qty
        entry = {
            "skuId": c["skuId"], "product": c.get("productName"),
            "quantity": qty, "unitCost": unit,
            "cost": c.get("fullLineCost"), "funded": funded,
            "promisedBy": ev.get("earliestPromisedDate"),
            "walkAwayPrice": None, "aboveWalkAwayCost": 0.0,
        }
        alert = walk.get(c["skuId"])
        text = (f"{qty} x {entry['product']} at {format_rupees(unit)} = "
                f"{format_rupees(entry['cost'])}, "
                f"{'funded' if funded else 'not funded'}.")
        if alert and alert["walkAway"]["currentCostAboveWalkAway"] and qty:
            price = alert["walkAway"]["price"]
            entry["walkAwayPrice"] = price
            entry["aboveWalkAwayCost"] = _money(
                (Decimal(str(unit)) - Decimal(str(price))) * qty)
            text += (f" Keeping this promise costs "
                     f"{format_rupees(entry['aboveWalkAwayCost'])} more than "
                     f"at the walk-away price of {format_rupees(price)}.")
        entry["text"] = text
        commitments.append(entry)

    restock = {r["skuId"]: r for r in
               plan["restockSelected"] + plan["restockDeferred"]}
    discretionary = []
    for a in supplier_alerts:
        r = restock.get(a["skuId"])
        price = a["walkAway"]["price"]
        above = a["walkAway"]["currentCostAboveWalkAway"]
        selected = bool(r and r.get("selected"))
        if above:
            decision = DO_NOT_BUY
            text = (f"Do not add discretionary restock of {a['product']} at "
                    f"{format_rupees(a['newCost'])} because it exceeds the "
                    f"{format_rupees(price)} walk-away price.")
        elif selected:
            decision = BUY
            text = (f"Restock {r.get('fundedQty')} x {a['product']} at "
                    f"{format_rupees(a['newCost'])}, within the "
                    f"{format_rupees(price)} walk-away price.")
        elif r:
            decision = DEFER
            text = (f"{a['product']}: restock deferred - this week's budget "
                    f"does not fund it.")
        else:
            decision = NOT_NEEDED
            text = (f"{a['product']}: no discretionary restock is needed this "
                    f"week.")
        discretionary.append({
            "skuId": a["skuId"], "product": a["product"],
            "recommendedQty": r.get("requestedQty") if r else 0,
            "plannerSelected": selected,
            "currentCost": a["newCost"], "walkAwayPrice": price,
            "aboveBy": (a["walkAway"]["differenceFromCurrentCost"]
                        if above else 0.0),
            "decision": decision, "text": text,
        })

    return {
        "commitments": commitments,
        "commitmentCost": plan["commitmentCost"],
        "allCommitmentsFunded": plan["allCommitmentsFunded"],
        "aboveWalkAwayCost": _money(sum(Decimal(str(c["aboveWalkAwayCost"]))
                                        for c in commitments)),
        "discretionary": discretionary,
        "source": "engine.purchasing (via engine.whatif._plan) and "
                  "engine.whatif.walk_away_price (via engine.price_alerts)",
    }


def _actions(brief: dict) -> List[dict]:
    """Fixed rules, most urgent first. Each names the figures it is about."""
    actions = []
    for d in brief["promiseKeeping"]["discretionary"]:
        if d["decision"] == DO_NOT_BUY:
            alert = next(a for a in brief["supplierAlerts"]
                         if a["skuId"] == d["skuId"])
            actions.append({
                "kind": "RENEGOTIATE_OR_REPRICE", "skuId": d["skuId"],
                "severity": alert["severity"],
                "text": (d["text"] + " Renegotiate or change the selling "
                         "price."),
            })
    for s in brief["shortages"]:
        if not s["funded"]:
            actions.append({
                "kind": "FUND_COMMITMENT", "skuId": s["skuId"],
                "severity": CRITICAL,
                "text": (f"{s['product']}: {s['shortageQty']} short for a "
                         f"customer order promised by {s['promisedBy']}, and "
                         f"this week's budget does not cover it."),
            })
    if brief["shortages"] and all(s["funded"] for s in brief["shortages"]):
        pk = brief["promiseKeeping"]
        text = (f"Buy the quantity required to fulfil existing commitments: "
                f"{len(brief['shortages'])} line(s), "
                f"{format_rupees(pk['commitmentCost'])} of the "
                f"{format_rupees(brief['planner']['budget'])} budget.")
        if pk["aboveWalkAwayCost"]:
            text += (f" Keeping these promises costs "
                     f"{format_rupees(pk['aboveWalkAwayCost'])} more than at "
                     f"the walk-away price.")
        actions.append({
            "kind": "BUY_FOR_COMMITMENTS", "skuId": None, "severity": "WARNING",
            "text": text,
        })
    return actions[:MAX_ACTIONS]


def _lines(brief: dict) -> List[str]:
    c, p = brief["counts"], brief["planner"]
    lines = []
    lines.append(f"Stock: {c['shortages']} product(s) short against customer "
                 f"commitments; {c['lowStock']} below reorder point.")
    for a in brief["supplierAlerts"][:2]:
        lines.append(f"Supplier: {a['product']} "
                     f"{format_rupees(a['oldCost'])} -> "
                     f"{format_rupees(a['newCost'])} "
                     f"(+{a['percentageDelta']:.2f}%).")
        lines.append(f"Margin: {format_rupees(a['oldMargin'])} -> "
                     f"{format_rupees(a['newMargin'])}.")
        lines.append(f"Walk-away: maximum supplier cost "
                     f"{format_rupees(a['walkAway']['price'])}.")
    if not brief["supplierAlerts"]:
        lines.append("Supplier: no confirmed supplier price change needs "
                     "attention.")
    lines.append(f"Purchasing: {format_rupees(p['totalSpend'])} planned from "
                 f"{format_rupees(p['budget'])}; "
                 f"{format_rupees(p['remaining'])} remaining.")
    return lines


def brief_signature(brief: dict) -> str:
    """A fingerprint of the brief's figures, so a stored summary written for
    yesterday's numbers is never shown beside today's."""
    import hashlib
    import json
    core = {k: brief.get(k) for k in ("date", "counts", "planner")}
    core["alerts"] = [(a["alertId"], a["severity"])
                      for a in brief.get("supplierAlerts") or []]
    return hashlib.sha256(json.dumps(core, sort_keys=True,
                                     default=str).encode()).hexdigest()[:16]
