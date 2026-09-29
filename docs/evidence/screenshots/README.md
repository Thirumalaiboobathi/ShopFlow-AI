# Product screenshots — to be captured

**Nothing in this folder has been captured yet.** This is the list, so that
the screenshots, when taken from the live site by a person, show the product's
real flow. Do not add mock-ups, edited images or generated pictures: a
screenshot here must be a capture of https://d3m3lwn03zb2eu.cloudfront.net as
it actually behaved. If a screen shows something different from what is
described, capture it anyway and note the difference.

The AWS-console and coding-agent captures are a separate list, in
[`../README.md`](../README.md).

| # | File | Where | Must show |
|---|---|---|---|
| 1 | `01-messy-order.png` | Workspace → AI assistant | The order box with `20 Anchor modular switch 1 way white, 3 Finolex 1.5 red coil, 2 Havells MCB 32 amp C curve` typed in, before submitting |
| 2 | `02-clarification.png` | same, after submitting | ShopFlow's question (for example SP or DP) with the numbered catalogue options - it asks instead of guessing |
| 3 | `03-verified-quote.png` | same, after answering every question | The quotation: 20 / 3 / 2 lines, subtotal ₹22,306.48, GST ₹4,015.16, customer total ₹26,321.64, and the stock shortages |
| 4 | `04-supplier-price-increase.png` | Intelligence → Supplier documents | The sample price list read: Finolex ₹5,900 → ₹6,300, +6.78%, "Material change", with the Confirm / Ignore buttons |
| 5 | `05-margin-impact.png` | Intelligence → Alerts (after confirming) | Margin ₹708 → ₹308 (10.71% → 4.66%), LOW_MARGIN; the selling price unchanged |
| 6 | `06-walk-away.png` | same card | Walk-away price ₹5,947.20 and the ₹352.80 gap to the confirmed ₹6,300 |
| 7 | `07-supplier-reply.png` | Alerts → Supplier negotiation | The reply "6100 final, 5 coils min" read as ₹6,100 / 5 coils, ABOVE_WALK_AWAY by ₹152.80, the cash and commitment flags |
| 8 | `08-owner-decision.png` | same card | The Accept / Counter / Walk away guidance, with "nothing is recorded or sent" visible |

Before capturing 4-8 the demo needs the Finolex cost confirmed; after the
session, `python scripts/reset_confirmed_costs.py --confirm` returns the demo to
its starting state (see `docs/DEMO-RUNBOOK.md`).
