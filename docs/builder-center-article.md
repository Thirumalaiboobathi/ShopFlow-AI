# ShopFlow AI: From Messy Orders to Smarter Purchasing Decisions

> **Draft for AWS Builder Center.** Written for the AWS "Zero to Shipped"
> Hackathon 2026.
>
> **Category:** Commercial Potential · **Focus track:** Startup
> **Live application:** https://d3m3lwn03zb2eu.cloudfront.net

---

## 1. The problem

A single-shop electrical retailer in Madurai runs the whole business on a
phone, a paper ledger, and memory. Three things quietly cost them money.

**Orders arrive as messy language.** A contractor sends *"Anna, 2 MCB 32 amp"*.
The shop stocks six different 32A MCBs — three brands, two pole configurations,
prices from ₹458 to ₹1,197. Supplying the wrong one means a callback, a return,
and a contractor who buys elsewhere next time.

**Supplier price rises hide in plain sight.** A dealer sends a photographed
price list on WhatsApp. Somewhere in it, one wire has moved from ₹5,900 to
₹6,300. Nobody compares it line by line against what was last paid, so the shop
keeps quoting at a margin it no longer earns.

**Cash is the real constraint.** The owner has ₹25,000 available this week, not
₹250,000. Deciding what to buy is a genuine tradeoff between promises already
made to customers and restocking what is about to run out. Most software
assumes this decision away.

None of these are information problems. The shop owner knows their trade far
better than any software does. They are *attention* problems — too many small
decisions, each requiring a cross-check nobody has time to do.

## 2. Why generic AI is not enough

The obvious move is to point a capable language model at the problem. Ask it to
read the order, look at the price list, suggest what to buy.

It works, mostly. That is exactly the danger.

A general assistant will produce a quotation total. It will also, occasionally,
produce a *plausible wrong one* — and a shop owner cannot tell which they are
looking at. A total that is wrong three percent of the time is worse than no
total at all, because the owner has no way to know which three percent, and is
about to spend real money on it.

The same applies to ambiguity. Asked for "2 MCB 32 amp", a general assistant
picks one and sounds confident. Confidence is the wrong response to a genuinely
underspecified request. The right response is a question.

So ShopFlow is built around one hard rule:

> **The language model never writes a business number.**

## 3. What ShopFlow does

ShopFlow turns messy customer messages and photographed supplier price lists
into verified quotations and cash-constrained purchasing decisions — and shows
the arithmetic behind every one of them.

```
customer order
  → understand → SKU match → detect ambiguity → ask the owner
  → inventory check → quotation

supplier price list photo
  → extract → match to catalogue → detect price change
  → OWNER CONFIRMS → durable confirmed supplier cost

owner's available cash
  → purchase plan: what to buy, what to defer, and why
```

## 4. The architecture

```
                    ┌──────────────────────────────┐
  Browser  ────────▶│  CloudFront (single origin)   │
  (no login)        └───────┬───────────────┬───────┘
                            │ /api/*        │ everything else
                            ▼               ▼
                 ┌─────────────────┐   ┌──────────────────┐
                 │ API Gateway     │   │ S3 (private,     │
                 │ HTTP API        │   │ OAC only)        │
                 │ 20 rps / 40     │   └──────────────────┘
                 └────────┬────────┘
                          ▼
              ┌───────────────────────┐
              │ API Lambda            │   NO BEDROCK PERMISSION
              │ validate · queue      │
              │ purchase plan (sync)  │
              └───┬───────────────┬───┘
                  │               │ async invoke
                  ▼               ▼
         ┌────────────────┐   ┌──────────────────────┐
         │ DynamoDB       │   │ Worker Lambda        │
         │ single table   │   │ agent loop · vision  │──▶ Bedrock
         └────────────────┘   └──────────┬───────────┘    (Nova Pro,
                                          ▼                 one model ARN)
                                   ┌──────────────┐
                                   │ S3 uploads   │
                                   │ (private)    │
                                   └──────────────┘
```

Two request patterns, each chosen for a reason.

**Orders and price lists are asynchronous.** A Bedrock tool loop takes seconds,
and holding a public HTTP connection open that long is a reliability risk. The
API validates, writes a job record, invokes the worker asynchronously, and
returns a job id. The browser polls.

**Purchase planning is synchronous.** It calls no model. It is arithmetic over
an already-loaded dataset, measured at **3.77 ms** for the full 147-SKU shop.
Wrapping that in a job record, an async invoke and a polling loop would add
three round trips and about a second of latency to hide four milliseconds of
work. Reaching for the async pattern a second time would have been consistency
for its own sake.

## 5. Where AI is used

Amazon Nova Pro, via the Bedrock Converse API, does four things:

- **Understands messy language.** Abbreviated, mixed-language, no SKU codes.
- **Selects tools.** Four strict tools — `search_catalog`, `get_inventory`,
  `calculate_quote`, `request_clarification` — in a loop bounded at six turns.
- **Asks for clarification** when the engine reports several real products match.
- **Reads photographed price lists** into structured rows using the same model's
  vision capability.
- **Summarises and explains** in plain language.

## 6. Where deterministic logic is used

Everything with a number in it. Inventory. SKU matching constraints. Shortages
and committed demand. Quote totals. Supplier price deltas and thresholds. Sales
velocity. Stock coverage. Stockout risk. Margin per rupee. Budget allocation.
Deferrals. Every figure the owner sees.

The engine is twelve Python modules under `backend/engine/`. It imports no AWS
SDK and calls no model. It runs offline, in a unit test, in milliseconds.

**This separation is enforced by architecture, not by prompt instructions:**

- **The API Lambda has no `bedrock:` permission at all.** Purchase planning
  happens there. There is no model on that path to invent anything — verified
  against the deployed IAM role, not just the template.
- **The worker's Bedrock permission is scoped to exactly one model**: the Nova
  Pro foundation-model ARN and its inference profile.
- **SKU ids are rejected unless they exist in the catalogue**, so a hallucinated
  product cannot enter through the front door.
- **Numbers the model writes into its summary are validated** against the
  engine's tool output before the response is returned. Responses ship with
  `grounded: true` and `ungroundedNumbers: []`.

A prompt that says "do not invent numbers" is a request. An IAM policy with no
`bedrock:InvokeModel` statement is a guarantee.

## 7. The human confirmation boundary

Detecting a supplier price rise and acting on it are two different things, and
ShopFlow keeps them apart deliberately.

Extraction never writes a cost — reading a price off a photograph is not
agreement to pay it. Only an explicit owner confirmation persists anything. A
rejection writes nothing at all. And a confirmed *supplier cost* never rewrites
the *selling price*: repricing the shelf is a commercial decision, not an
arithmetic consequence of a supplier's invoice.

When the owner confirms, one durable record is written:

```json
{
  "PK": "SHOP#demo",
  "SK": "COST#W-FIN-1.5-RED-90M",
  "recordType": "CONFIRMED_SUPPLIER_COST",
  "supplierId": "SUP-BALAJI",
  "confirmedCost": 6300,
  "currency": "INR",
  "effectiveDate": "15-09-2026",
  "sourceJobId": "a250d927...",
  "confirmedAt": 1789915870
}
```

Small details that matter: the supplier comes from the **catalogue**, not the
document, so a price list cannot silently reassign a SKU to a different
supplier. The currency is explicit, because a cost without one is not a cost.
And there is **no TTL** — job records expire because they are the workings of
one document, but this is shop state and must not vanish overnight.

Every purchase plan afterwards reports where each cost came from —
`CONFIRMED_SUPPLIER_PRICE` or `SEEDED_SUPPLIER_PRICE` — carried from the record
rather than inferred by comparing numbers. That distinction matters: a confirmed
price that happens to *equal* the previous one is still confirmed, and that is
exactly the case a numeric comparison would mislabel.

## 8. Supplier price intelligence

The owner photographs a dealer price list. Nova Pro reads it into structured
rows against a strict schema. The engine then matches each row to the catalogue
and reports one of three outcomes — matched, ambiguous, or unmatched — because
forcing a match would be worse than admitting there isn't one.

For the sample list: five lines, three matched, one ambiguous, one not a product
this shop stocks.

The comparison is pure arithmetic, shown with its own working:

```
W-FIN-1.5-RED-90M    ₹5,900 → ₹6,300    +6.78%  INCREASE
calc: (6300.0 - 5900.0) / 5900.0 x 100 = 6.78%
```

Moves under 5% are reported but not flagged — dealer rates drift constantly, and
interrupting the owner over a 1.71% change trains them to ignore alerts.

## 9. Cash-constrained purchasing

The owner says: *"I only have ₹25,000."*

**Tier 1 — customer commitments.** Any shortage created by a promise already
made is funded first, earliest promised date first. A customer promise outranks
every discretionary restock regardless of how profitable that restock would be.

**Tier 2 — discretionary restocking.** Whatever survives Tier 1 is allocated by
a transparent score, highest first:

```
coverageWeeks  = availableAfterCommitments / weeklyVelocity
riskHorizon    = supplierLeadTimeWeeks + 2
stockoutRisk   = clamp(1 - coverageWeeks / riskHorizon, 0, 1)
marginPerRupee = (sellingPrice - currentSupplierCost) / currentSupplierCost

priority       = stockoutRisk × marginPerRupee
```

A greedy allocator, not an optimiser — deliberately, so every rupee can be
explained in one sentence and reproduced exactly by a test.

Two guarantees hold at every budget tested, from ₹0 to ₹100,000: **total spend
never exceeds the budget**, and **remaining cash is never negative**.

Every deferred item explains itself:

```
SW-ANC-BELL — Anchor Modular Switch Bell Push White
  Deferred: remaining budget cannot fund this restock.
  Stock covers about 0.5 weeks at 3.9 units/week; lead time 3 days.
  priority = 0.7875 x 0.35 = 0.275617
```

## 10. The live end-to-end example

Every figure below comes from the deployed application.

**A messy order becomes a verified quote.**

> *"Anna, 20 Anchor modular switches 1-Way 10A, 3 coils Finolex 1.5 sq mm red
> wire 90m, 2 Havells MCB SP 32A."*

→ **₹22,306.48**, every line checked against live inventory.

**An ambiguous order becomes a question.** *"Anna, 2 MCB 32 amp."* → ShopFlow
asks for the **brand** and offers the six real 32A MCBs the shop stocks.

**A photographed price list reveals a rise.** ₹5,900 → ₹6,300, **+6.78%**.

**The owner confirms.** The supplier cost persists. The selling price stays at
₹6,608, untouched.

**"I only have ₹25,000."**

| | |
|---|---:|
| Customer commitments | **₹12,948.00** |
| Restocking | **₹12,045.16** |
| Total recommended spend | **₹24,993.16** |
| Cash remaining | **₹6.84** |
| Restocks funded / deferred | 9 / 49 |

**And the consequence is made explicit.** Confirming that +6.78% rise cost the
shop **₹803.40 of restocking capacity** — ₹800 extra on two wire coils, plus
₹3.40 as the allocator refits what remains.

That number is the product in a sentence: a price change buried in a photograph,
traced all the way to what the shop can afford to put on its shelves this week.
Without it, the owner absorbs that silently and wonders three months later why
the cash isn't there.

## 11. AWS architecture

| Service | Use |
|---|---|
| **Amazon CloudFront** | Public entry point; one origin for site and API |
| **Amazon S3** | Static site and uploads — both private, OAC only |
| **Amazon API Gateway** (HTTP API) | 7 routes, stage-wide throttling 20 rps / 40 burst |
| **AWS Lambda** | API, worker, health — Python 3.13 |
| **Amazon DynamoDB** | Single table, PK/SK + GSI1, TTL on job records |
| **Amazon Bedrock** | Amazon Nova Pro — language and document vision |
| **Amazon CloudWatch** | 4 log groups, 14-day retention |
| **AWS Budgets** | $25/month guard with 50/80/100% alerts |
| **AWS CloudFormation / CDK** | One stack, Python CDK |

Serving a single-page app and a JSON API from one CloudFront distribution is
what makes the whole thing reachable at one URL with no CORS and no second
domain.

## 12. Security and least privilege

- **No AWS credentials ever reach the browser.** Price-list images travel as
  base64 through the API; no presigned URLs, and the uploads bucket stays
  entirely private.
- **Both S3 buckets are private**, public access blocked; CloudFront reaches the
  site bucket through Origin Access Control only.
- **Bedrock permission exists on one function and one model.** The API function
  has none.
- **Input is bounded everywhere** — order text 1,000 characters, request body
  4 KB, images 2.5 MB with both MIME-type and magic-byte checks, 25 items per
  order, 50 lines per price list, 6 agent turns.
- **Price comparison figures are read from the stored job**, never the request
  body, so a caller cannot post numbers the engine never produced.
- **Logs are contents-free.** The worker logs model id, latency, token counts
  and result counts — never order text, product descriptions, supplier names or
  prices. API Gateway access logs carry only requestId, IP, time, route, status
  and latency.
- **A $25 monthly budget was in place before** a public endpoint that could call
  Bedrock existed.

## 13. Testing and engineering lessons

**336 tests run in about two seconds with no AWS account**, because the engine
is pure. Plus **25 real-DynamoDB smoke checks**, run explicitly before a deploy.

The most valuable lesson of the project:

### A green test suite is not evidence of a working system

Twice the in-memory `FakeTable` used by the tests was wrong in a way no unit
test could catch — and in opposite directions.

**First it was too permissive.** It accepted Python floats. Real DynamoDB
rejects them. Every unit test passed, and the deployed endpoint returned 500 on
the first confirmation. Found in CloudWatch, fixed by converting to `Decimal`.

**Then it was too naive.** The durable-cost feature queries with a composite
condition:

```python
Key("PK").eq("SHOP#demo") & Key("SK").begins_with("COST#")
```

The fake read `_values[1]` and assumed it was the partition key string. For an
`And` condition that is a `BeginsWith` *object*, so the query matched nothing —
the planner silently found no confirmed costs at all, **and all 336 tests still
passed while the feature was completely dead.**

That one was caught only by being suspicious that the suite went green on the
first run, which was too easy for a change of that size. The fake now parses the
condition properly and raises rather than guessing, and a test asserts the
fake's own behaviour.

The general lesson: a test double is a model of a dependency, and a wrong model
fails silently in whichever direction it is wrong.

### Other lessons worth recording

**One CloudFront distribution serving both a SPA and an API needs care.**
Mapping 404 to `index.html` for SPA routes also swallowed genuine API 404s,
turning them into 200s with an HTML body. Fixed by mapping only 403 — S3 behind
OAC answers 403 for a missing object, while API Gateway answers 404 for an
unknown route, which separates the two cleanly.

**Account limits are shared.** Reserving Lambda concurrency failed because this
account's total concurrency is 10 and AWS requires 10 unreserved. The
reservation was removed rather than risking the starvation of four unrelated
projects in the same account. The right fix was to want less, not to demand
more.

**A conditional resource is a deletion waiting to happen.** The cost budget is
created inside `if alert_email:` — optional CDK context. Synthesising without it
produces a template with no budget, and deploying that *destroys the existing
cost guard*. `cdk diff` showed `[-] AWS::Budgets::Budget MonthlyBudget destroy`
before a deploy that would otherwise have removed it. A prose warning is not a
control, so this is now enforced by a deploy script that refuses to run without
the alert email and aborts if the diff would remove the budget.

**Consequential state changes need an explicit human boundary.** Detecting a
price rise and applying it are separate operations, and keeping them separate is
most of what makes the system trustworthy.

## 14. How the coding agent was used

Built with **Claude Opus 5 via Claude Code**, connected to AWS account
`675613597178` in `ap-south-1` through the AWS CLI using a dedicated IAM user.

The agent worked directly against AWS rather than producing code for a human to
run:

- **Inspected the live environment read-only before changing anything** —
  identity, region, bootstrap state, existing stacks — specifically to avoid
  touching four unrelated projects sharing the account.
- **Probed Bedrock by actually invoking every candidate model** rather than
  trusting the model list. This is how blocked Anthropic access was discovered,
  which set the model choice for the entire project.
- **Authored the CDK stack, then read its own synthesised CloudFormation
  template before deploying.** Two defects were caught at exactly that step,
  including a route-path mismatch that would have made every API call 404.
- **Deployed, then verified against the public URL** rather than assuming
  success — HTTP status, headers, page content, bucket privacy, IAM scope,
  CloudWatch events.
- **Diagnosed real failures from CloudWatch logs and CloudFormation events**,
  including a stack wedged in `UPDATE_ROLLBACK_FAILED`.
- **Recorded its own mistakes**, including three wrong diagnoses drawn from a
  stale `cdk.out` directory, and two incorrect test expectations about greedy
  allocation.

The agent was most useful not when writing code quickly, but when it read
something before trusting it — a synthesised template, a `cdk diff`, a log
stream, or a suspiciously clean test run.

## 15. Development process

Built in gated stages. Each ended with a stop-and-report, and a human approved
before the next began.

| Stage | Delivered |
|---|---|
| 1 | Deterministic engine, synthetic shop, first tests |
| 2 | Minimal AWS deployment, public URL |
| 3 | Order-to-quote with the Bedrock agent loop |
| 3.1 | Hardened variant ambiguity |
| 4 | Supplier price intelligence |
| 5 | Cash-constrained purchasing planner |
| 5.1 | Durable confirmed supplier costs |

Two decisions came from human review rather than the agent. The first: an early
version broke a variant tie using the shop's default variant. That was rejected
— a quotation system must not silently choose a product variant on a customer's
behalf. The second: per-route API Gateway throttling was removed permanently
after it wedged a deployment, in favour of a single stage-wide limit.

A development log was written as the work happened, not reconstructed
afterwards — roughly 1,570 lines including every failure and correction.

## 16. Limitations and data disclosure

**The demo dataset is entirely synthetic.** All 147 SKUs, prices, stock levels,
sales history and the supplier price list are generated by `data/generator.py`
at a fixed seed. They are modelled on a Madurai electrical retailer but are
**not** a real shop's records.

**No real shop has used this system.** There are no users.

**No business-accuracy claim is made.** The arithmetic is exact and tested;
whether the *recommendations* are commercially correct is unmeasured. There are
no time-saved, revenue, margin or market-size figures in this project, because
none have been measured. Evaluation against real anonymised shop orders remains
open work.

Also outstanding: document extraction is tested against one price-list layout
rather than arbitrary supplier documents; confirmed costs store only the current
value per SKU, not a history; ambiguous price-list lines are surfaced but not
resolvable in the interface; clarification is single-round; and there is no
authentication or multi-shop support. Voice input and Tamil output were
deliberately not built, and are not claimed.

## 17. Commercial potential

The wedge is narrow on purpose: one trade, one country, one workflow that
already happens daily on WhatsApp. Independent electrical and hardware retailers
in India place orders, receive dealer price lists, and decide what to restock
every week, with no ERP and no POS.

What makes it plausibly commercial rather than merely useful is that it attaches
to a decision where money moves. A shop deciding how to spend ₹25,000 this week
is making a decision worth getting right, repeatedly, forever. And the ₹803.40
example shows the product surfacing a consequence the owner would otherwise
never have seen.

What would have to be proven before any revenue claim: that the recommendations
are commercially sound against real shop data, that extraction generalises
across dealer document formats, and that owners trust and act on the output.
None of those are established today, and this project does not assert them.

## 18. Live demo

**https://d3m3lwn03zb2eu.cloudfront.net** — no login, no signup.

Try in order:

1. Paste the canonical order — expect **₹22,306.48**.
2. Try *"Anna, 2 MCB 32 amp."* — watch it refuse to guess.
3. Click **Use the sample price list** — find the **+6.78%** rise.
4. **Confirm** it — note that the selling price does not move.
5. Plan with **₹25,000** — see nine restocks funded and forty-nine deferred.
6. Open any **"Why?"** — see the full calculation.

## 19. Hackathon category and track

- **Category:** Commercial Potential
- **Focus track:** Startup
- **Live application:** https://d3m3lwn03zb2eu.cloudfront.net
- **Coding agent:** Claude Opus 5 via Claude Code
- **Region:** `ap-south-1`
- **Model:** Amazon Nova Pro via Amazon Bedrock

---

*ShopFlow is not an AI chatbot that tells a shop owner what it thinks. It is an
operational system where AI handles messy language and interaction, while
deterministic code owns business state, arithmetic, constraints and purchasing
decisions.*
