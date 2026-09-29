# Commercial intelligence — local evidence

**Script:** `python scripts/commercial_evidence.py` · **Run:** 2026-09-29, local,
against the seeded dataset · **Result:** 22 passed, 0 failed (exit 0)

The script runs the engines and the API handler **in-process**. It calls no AWS
service and writes nothing. It has **not** been run against the deployed app:
this feature is not deployed yet.

The Finolex scenario uses the confirmed ₹6,300 cost against the seeded ₹5,900.
Seeded commitments: 3 coils promised, 1 on hand, 2 short.

## Money at risk — Finolex 1.5 sqmm red 90m

| Figure | Value | Definition |
|---|---|---|
| Supplier price change | +₹400.00 per coil | 6,300 − 5,900 |
| Purchase cash required | ₹12,600.00 | 2 short × ₹6,300. Spent to fulfil the order, **not a loss** |
| Supplier price exposure | ₹800.00 | 2 × ₹400 |
| Margin impact | ₹800.00 | 2 × (₹708 − ₹308). **The same rupees as the exposure** |
| Total exposure | ₹800.00 | Exposure only. The margin impact is not added |

## Buy now vs wait

| | Budget ₹25,000 | Budget ₹12,947.99 |
|---|---|---|
| Buy now: cash required | ₹12,600.00 | ₹12,600.00 |
| Buy now: funded within budget | 2 of 2 | 1 of 2 |
| Buy now: commitment covered | yes | no |
| Wait: cash required now | ₹0.00 | ₹0.00 |
| Wait: shortage remaining | 2 | 2 |
| Wait: capacity kept | ₹12,600.00 | ₹6,300.00 |
| `recommendation` | `null` | `null` |
| `ownerDecisionRequired` | `true` | `true` |

The funding figures are the purchase planner's (commitments first, earliest
promise first). ₹12,947.99 is the planner's existing boundary. The facts read,
for example: "Current supplier price is ₹6,300.00." "Waiting leaves 2
committed coil(s) uncovered." No price forecast is made.

## Supplier quote comparison

The offers are illustrative, not real supplier quotes. Two coils are needed.

| Offer | Unit price | MOQ | Buy | Purchase cost | Status | Flags |
|---|---|---|---|---|---|---|
| Supplier A | ₹6,300 | 2 | 2 | ₹12,600 | FITS_REQUIREMENT | — |
| Supplier B | ₹6,150 | 5 | 5 | ₹30,750 | MOQ_BLOCKED | BETTER_UNIT_PRICE, HIGHER_TOTAL_COST |
| Supplier C | ₹6,450 | 1 | 2 | ₹12,900 | FITS_REQUIREMENT | HIGHER_TOTAL_COST |

The lowest unit price has the highest total cost. Nothing is ranked or chosen.

## Supplier reliability

- **The shop's four suppliers** (SUP-ANNAI, SUP-BALAJI, SUP-KMT, SUP-VELAN):
  each is `INSUFFICIENT_DATA`, with a `null` score and `null` confidence.
  ShopFlow records no purchase orders or deliveries, so there is nothing to
  score.
- **`DEMO / SYNTHETIC SUPPLIER HISTORY`**
  (`tests/fixtures/synthetic_supplier_history.json`): 19 **invented** orders for
  an invented supplier `SYN-SUPPLIER-1`. They exist only to exercise the
  scoring rules.
  - Result: **95/100**, confidence `LIMITED` (19 verified orders).
  - Components: completion 19/19, in full 18/19, on time 17/19, invoiced at
    the quoted price 18/19. Each is weighted 0.25.
  - With the first 9 orders only: `INSUFFICIENT_DATA`, score `null`.

## Refusals (API handler, in-process)

| Request | Status |
|---|---|
| Anonymous caller | 401 |
| Client `marginAtRisk` | 400 |
| Client `totalExposure` | 400 |
| Client `recommendation` | 400 |
| Offer carrying `total` | 400 |
| Instruction as a supplier name | 400 |
| Client reliability `score` | 400 |
| Client supplier `history` | 400 |
| Injected `skuId` | 400 |
| Store after all calls | unchanged (nothing written) |

## Not verified

- **Live:** none of this is deployed. After deploying, the same four `kind`s
  can be called on `POST /api/shop-queries` with the owner header.
- **Reliability on real data:** the shop has none.
