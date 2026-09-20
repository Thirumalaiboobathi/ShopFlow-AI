# ShopFlow AI — Development Log

Written as the work happens. Not reconstructed afterwards.

Coding agent: **Claude Opus 5 via Claude Code**, connected to AWS account
`675613597178` through the AWS CLI with an IAM user (`AI-agent`), region
`ap-south-1`.

Format per entry: prompt → agent action → AWS change → result → human decision.

---

## Day 1 — 2026-09-19

### Stage 1.0 — Environment discovery

**Prompt.** "Inspect the environment and repository. Check AWS CLI/CDK, identity,
region, Bedrock model access, Strands availability, and test the voice path."

**Agent action.** Ran read-only inspection of the workstation and the AWS account.

**Findings.**

| Item | Result |
| --- | --- |
| Working directory | empty, not a git repository |
| Node / npm | v24.13.0 / 10.2.5 |
| Python / boto3 | 3.13.4 / 1.43.39 |
| AWS CLI | 2.34.59 |
| AWS CDK CLI | not installed — will use `npx aws-cdk` |
| AWS identity | `arn:aws:iam::675613597178:user/AI-agent` |
| Region | `ap-south-1` (Mumbai) |
| CDK bootstrap | **already present** (`CDKToolkit` stack, `cdk-hnb659fds-assets-…`) |
| Profile `bedrock-account` | invalid security token — ignored, `default` used |

**AWS changes.** None. Read-only.

**Human decision.** Proceed in `ap-south-1`. Mumbai is the right region for an
Indian retail product and the bootstrap already exists, so no new region setup.

---

### Stage 1.1 — Bedrock model availability

**Agent action.** Enumerated foundation models and inference profiles, then
actually invoked them. Listing a model does not prove access, so every
candidate was called with a real `Converse` request.

**Result.**

| Model | Outcome |
| --- | --- |
| `apac.amazon.nova-pro-v1:0` | ✅ works, including vision |
| `global.amazon.nova-2-lite-v1:0` | ✅ works |
| `apac.amazon.nova-micro` / `nova-lite` | ✅ works |
| all `anthropic.*` profiles | ❌ `ResourceNotFoundException: Model use case details have not been submitted for this account` |
| `anthropic.claude-sonnet-5` / `opus-5` | ❌ `AccessDeniedException: not available for this account` |

**Important nuance, recorded honestly.** On the *first* probe, three Anthropic
models (`global.anthropic.claude-sonnet-4-6`,
`global.anthropic.claude-haiku-4-5`, `apac.anthropic.claude-3-7-sonnet`)
returned successful text completions. Every call after that failed with the
use-case-form error. A re-run of the identical probe confirmed the failure is
consistent, not intermittent. The initial successes appear to have been a short
grace window that closed. **Anthropic is treated as unavailable.**

**AWS changes.** None. Read-only API calls.

**Human decision.** Do not block on Anthropic. Submit the Anthropic use-case
form in parallel; build on Nova Pro now.

---

### Stage 1.2 — Capability probes

Three things had to be proven before committing to the architecture.

**1. Multimodal price-list extraction.** Generated a synthetic Madurai dealer
price sheet (5 line items) and asked Nova Pro to return strict JSON.
Result: supplier, effective date and all five rates extracted exactly
(6300, 6300, 10250, 78.5, 412). 1,671 input tokens. ✅

**2. Tool use and the core trust behaviour.** Gave Nova a constrained
`search_catalog` tool and the demo order *"Anna, 20 Anchor modular switches,
3 coils Finolex 1.5 sq mm wire, 2 MCB 32 amp."* Both Nova Pro and Nova 2 Lite:

- called the tool rather than answering from memory,
- returned only SKU IDs the tool had supplied,
- and **refused to guess the wire colour**, emitting a clarification request
  naming all three candidates.

This is the single most important behaviour in the product, and it works on a
model we definitely have access to. ✅

**3. Voice.** Polly (`Kajal`, en-IN) → S3 → Transcribe (en-IN):

```
SPOKEN : Anna, twenty Anchor modular switches venum, three coils Finolex
         one point five square mm red wire, and two MCB thirty two amp.
HEARD  : And now 20 anchor modular switches venom, 3 coils final X
         1.5 square millimeter red wire, and 2 MCP 32 amp.
```

**Every quantity survived** (20, 3, 1.5, 2, 32). **Every domain term broke**
(Finolex → "final X", MCB → "MCP", Anna → "And now", venum → "venom").

**AWS changes.** Created `shopflow-voiceprobe-675613597178-ap-south-1` (S3) and
one Transcribe job. To be folded into the stack or deleted.

**Human decision.** Two consequences, both adopted:

- The **editable transcript is mandatory**, not a nicety. The owner corrects
  the text before the agent ever sees it.
- Add a **Transcribe custom vocabulary** of brand and domain terms
  (Finolex, Polycab, Anchor, Havells, MCB, sqmm, …). Cheap, high impact.

Caveat recorded deliberately: Polly speech is **not** a substitute for a real
Tamil-accented shop recording. This validates plumbing only. A genuine
code-mixed recording is still required before any accuracy claim is made.

---

### Stage 1.3 — Deterministic core

**Prompt.** "Implement the seed generator and business engine first. The engine
must be completely independent of Bedrock, Strands, AWS SDK, Lambda and
DynamoDB."

**Agent action.** Built `backend/engine/` as a pure-stdlib Python package and
`data/generator.py` as a deterministic seed generator.

**Files created.**

```
backend/engine/models.py       domain types
backend/engine/velocity.py     sales velocity, coverage weeks
backend/engine/shortage.py     committed demand, shortages
backend/engine/pricing.py      supplier cost, price delta, rival quotes
backend/engine/budget.py       two-tier allocator
backend/engine/scenarios.py    detectors for the planted conditions
data/generator.py              147-SKU seeded shop
data/demo_scenario.py          canonical scenario, computed not typed
tests/                         83 tests
```

**Architecture decision — why the engine is pure.** The product claim is *"the
AI never invents business numbers."* Enforcing that by prompt instruction is
unverifiable. Enforcing it by architecture is testable: the engine cannot call
a model, because it does not import one. The LLM's only route to a number will
be a tool call into this package.

**Bug found and fixed by the agent.** The first generator run produced 147
products but **148** inventory rows. Cause: a planted SKU id was hand-written as
`FAN-ORI-900-BRN` while the catalog builder generates `FAN-ORI-900MM-BRN`, so
the dead-stock plant created a phantom inventory row for a product that does not
exist. Fixed the id and added a guard in `build_dataset()` that raises if any
planted id is absent from the catalog — the class of bug, not just the instance.

**AWS changes.** None. Entirely local.

---

### Stage 1.4 — Canonical demo scenario

Every figure below is **computed by the engine from the seed**. None is typed
into the application.

```
ORDER ORD-2026-0918-01 — Murugan Electricals (contractor)
   20 x Anchor Modular Switch 1-Way 10A   on hand 14, 10.5/wk, 1.33 wk cover
    3 x Finolex 1.5 sqmm Wire Red 90m     on hand  1,  4.25/wk, 0.24 wk cover
    2 x Havells MCB SP 32A C-Curve        on hand  5,  3.0/wk, 1.67 wk cover
   QUOTE TOTAL  Rs 22,306.48

SHORTAGES      switches 6,  wire 2,  MCB 0
PRICE RISE     W-FIN-1.5-RED-90M  5,900 -> 6,300  (+6.78%)
RIVAL QUOTE    MCB-HAV-SP-32A-C   KMT Traders 325.78 vs 358.00 (save 9%)

BUDGET Rs 25,000
  Tier 1 committed orders   Rs 12,948.00
  Tier 2 restocking         Rs 12,045.16
  Total spend               Rs 24,993.16
  Remaining                 Rs      6.84
  49 restock lines deferred, each with a calculated risk
```

The ₹25,000 budget **genuinely binds**: all commitments are funded, 99.97% of
the cash is spent, and 49 discretionary restocks are refused. `assert_budget_binds()`
fails the build if that ever stops being true.

**Human decision.** Keep ₹25,000 — the tradeoff is real and was not engineered
by hand-picking numbers.

---

### Stage 1.5 — Budget policy, documented

Tier 1 — committed customer orders, funded first, earliest promised date first.
A customer promise outranks every discretionary restock regardless of margin.

Tier 2 — remaining budget, allocated greedily by:

```
coverageWeeks  = availableAfterCommitments / weeklyVelocity
riskHorizon    = supplierLeadTimeWeeks + SAFETY_WEEKS (2)
stockoutRisk   = clamp(1 - coverageWeeks / riskHorizon, 0, 1)
marginPerRupee = (sellingPrice - currentSupplierCost) / currentSupplierCost

priority       = stockoutRisk * marginPerRupee
```

`stockoutRisk` already carries velocity, current stock and lead time, which is
why they are not multiplied in again. Dead stock has zero velocity, therefore
infinite coverage, therefore zero priority — it is never restocked.

**This is a deterministic greedy allocator, not an optimiser, and is described
as such everywhere.** It is reproducible, explainable and unit-tested; that is
the property that matters for a tool a shop owner has to trust.

---

### Stage 1.6 — Tests

`83 passed in 0.35s`. Coverage spans velocity, coverage weeks, shortage,
price delta, committed-order priority, the four required budget cases (A–D),
edge cases (zero/negative stock, zero/negative budget, dead stock,
over-allocation), ambiguity detection and scenario reproducibility.

Two tests failed on first run. Both were **wrong expectations, not engine bugs**:

1. compared a rounded display value against an unrounded product;
2. assumed a committed SKU could not also be a restock candidate — it can, and
   should be, because filling the order leaves nothing for walk-in demand.

Recorded because the second one is a case where the agent's first assumption
about its own design was wrong and the behaviour under test was right.

---

---

## Day 1, Stage 2 — 2026-09-19 — First public deployment

> **The first public deployment was intentionally performed before feature
> completion to eliminate ship-gate risk.** A challenge that requires a live,
> publicly reachable URL is lost by failing to deploy, not by shipping a thin
> first version. The foundation went out today so that every later change is a
> deploy to something already proven, never a first attempt under deadline.

### Review decisions applied before deployment

Four decisions were taken at the Stage 1 review and implemented first.

1. **Tier display.** The engine keeps Tier 1 and Tier 2 allocations separate;
   `BudgetPlan.merged_lines()` produces one row per SKU for the UI with the
   per-tier split nested underneath, so the combined figure is readable and the
   decision stays auditable.
2. **Price alert threshold.** `PRICE_ALERT_THRESHOLD_PERCENT = 5.0`, defined
   once in `engine/pricing.py` and imported everywhere. Effect on the seed:
   flagged SKUs fell from **51 to 1** — the planted +6.78% wire rise — while 1%
   remains available for analysis.
3. **Partial fulfilment.** `Product.fulfilmentPolicy` is now explicit:
   `PARTIAL_ALLOWED` (default) or `ALL_OR_NOTHING`. The allocator refuses to
   part-fund an indivisible line and says so. Kept deliberately small.
4. **Voice probe bucket.** Deleted rather than promoted to production.

### Cleanup — voice probe bucket

**Agent action.** Inspected `shopflow-voiceprobe-675613597178-ap-south-1`
before touching it: region `ap-south-1`, unversioned, **exactly one object** —
`probe/9fc…cf5.mp3`, 45,980 bytes, created 01:47 today by the Polly probe.
Confirmed as the experimental resource, then deleted the object and the bucket.

**Verification.** `head-bucket` returns 404. A full bucket listing confirms the
other seven buckets (SilentSignal, AwsDeployDoctor, Amplify, CDK assets) are
untouched.

### Infrastructure created

**Region.** `ap-south-1` (Mumbai). **Tooling.** `aws-cdk-lib` 2.270.0 (Python),
CDK CLI 2.1142.0 via `npx`. CDK bootstrap already existed and was reused.

**Command.**

```
cd infrastructure
npx aws-cdk@2 deploy --require-approval never \
  -c alertEmail=<email> -c monthlyBudgetUsd=25
```

> **`alertEmail` is not optional in practice.** The cost budget is gated on
> `if alert_email:`, so deploying without that context silently *destroys*
> the existing budget alarm. See the Stage 5 failure notes.

**Stack** `ShopFlowStack` → `CREATE_COMPLETE` in **213.9s**.
ARN `arn:aws:cloudformation:ap-south-1:675613597178:stack/ShopFlowStack/de4d66a0-…`

| Resource | Name |
| --- | --- |
| DynamoDB table | `shopflow-demo` (PK/SK, GSI1, on-demand, PITR, AWS-managed encryption) |
| Uploads bucket | `shopflow-uploads-675613597178` (private, AES256, TLS-only, 30-day expiry) |
| Site bucket | `shopflow-site-675613597178` (private, AES256, CloudFront OAC only) |
| CloudFront | `EKAOMJLJWM7VZ` — OAC to S3, `/api/*` to API Gateway |
| HTTP API | `shopflow-api`, route `GET /api/health`, throttled 20 rps / 40 burst |
| Lambda | `shopflow-health` (Python 3.13, 256 MB, 10s) |
| Log groups | `/aws/lambda/shopflow-health`, `/aws/apigateway/shopflow-api`, 14-day retention |
| Budget | `shopflow-monthly`, USD 25, alerts at 50/80/100% |

All tagged `Project=ShopFlow`, `ManagedBy=CDK`, `Environment=prod`.

**Architecture note — why `/api/*` goes through CloudFront.** The API is served
from the same domain as the site rather than its own. One origin means no CORS
configuration, one URL for a judge to remember, and the option to put WAF or
caching in front of both later without changing the frontend.

### Public URL

**https://d3m3lwn03zb2eu.cloudfront.net**

Health: **https://d3m3lwn03zb2eu.cloudfront.net/api/health**

### Verification — performed, not assumed

| Check | Result |
| --- | --- |
| Site HTTP status | `200 OK`, 8,426 bytes |
| Served by CloudFront | `Via: 1.1 …cloudfront.net (CloudFront)`, `Server: AmazonS3` |
| Security headers | HSTS `max-age=31536000; includeSubDomains`, `X-Frame-Options: DENY` |
| Landing page content | 8/8 required strings present (title, tagline, category, lane, Bedrock, deterministic engine, workflow, CTA) |
| Health via CloudFront | `200`, `{"status":"ok","checks":{"dynamodb":{"status":"ok","latencyMs":234.4}}}` |
| Health at API origin | `200`, DynamoDB `ok` in 4.4 ms |
| Site bucket direct access | **403 Forbidden** — private, reachable only through CloudFront |
| Uploads bucket | all four public-access blocks `true` |
| Lambda IAM scope | read-only, `shopflow-demo` table + its indexes only |
| CloudWatch | real invocations logged: 236.72 ms cold (484 ms init), 9.10 ms warm |
| Budget | `shopflow-monthly`, USD 25, MONTHLY — confirmed via `describe-budgets` |
| Isolation | `AwsDeployDoctorStack` last updated 2026-09-11, `CDKToolkit` 2026-08-20 — neither touched |

### Errors encountered and fixed

1. **CDK CLI absent.** Not installed globally; used `npx aws-cdk@2` and
   installed `aws-cdk-lib` for Python rather than adding a global dependency.
2. **Route/path mismatch, caught at review before deploying.** The API route
   was first written as `/health`, but CloudFront forwards the `/api/*` prefix
   unchanged, so the origin would have received `/api/health` and returned 404.
   Route corrected to `/api/health`. Found by reading the synthesised template,
   not by a failed deployment.
3. **Wrong origin request policy, same review.** `CORS_S3_ORIGIN` was initially
   applied to the API behaviour. It forwards the `Host` header, which API
   Gateway rejects. Changed to `ALL_VIEWER_EXCEPT_HOST_HEADER`.
4. **Deprecated CDK properties.** `point_in_time_recovery` and Lambda
   `log_retention` replaced with `point_in_time_recovery_specification` and an
   explicit `LogGroup`, avoiding a deprecated log-retention custom resource.
5. **PowerShell here-string mangling.** `git commit -m @'…'@` with quotes in the
   body was parsed as pathspecs; switched to `git commit -F <file>`.

### Coding-agent evidence captured

1. **Agent inspecting the AWS environment** — identity, region, bootstrap,
   existing stacks/buckets/tables enumerated read-only before any change.
2. **Agent probing Bedrock** — every candidate model actually invoked, not just
   listed; access limits discovered and recorded.
3. **Agent creating CDK infrastructure** — `infrastructure/` authored from
   scratch.
4. **Agent reviewing its own synthesised template** — bucket public-access
   blocks, encryption, throttling, route key and IAM policy scope inspected
   before deploying; two defects found and fixed at this step.
5. **Agent deploying the stack** — `CREATE_COMPLETE` in 213.9s.
6. **Agent verifying the deployment** — HTTP status, headers, page content,
   both health paths, bucket privacy, budget, CloudWatch log events.
7. **Agent performing safe destructive work** — voice-probe bucket inspected,
   confirmed, deleted, and the deletion verified without affecting other
   projects.

Screenshots to capture in the AWS console for the submission: CloudFormation
stack view, CloudFront distribution, DynamoDB table, Lambda function,
CloudWatch log stream, and the live URL in a browser.

---

## Day 1, Stage 3 — 2026-09-19 — Order to quote, end to end

**Prompt.** "Turn the existing deterministic engine + AWS foundation into the
first real end-to-end ShopFlow workflow: customer order → understand →
constrained SKU matching → detect ambiguity → ask clarification when required →
check inventory → calculate shortages → generate quotation → show evidence.
The LLM must never invent a SKU, a price or a total."

### The design problem that had to be solved first

The canonical demo order is *"20 Anchor modular switches, 3 coils Finolex 1.5
sq mm red wire, 2 MCB 32 amp."* Against the real 147-SKU catalogue:

- "Anchor modular switches" matches **6** Anchor switch variants
- "Finolex 1.5 red wire" matches **2** coils (90m and 180m)
- "MCB 32 amp" matches **6** MCBs across three brands and two pole types

So the canonical order is, strictly speaking, ambiguous on every line. Asking
three clarifying questions before quoting anything would be technically pure and
practically useless — no shop works that way.

**Decision.** `Product.isDefaultVariant` was added: the variant a shop reaches
for when the customer does not say (the 90m coil, the plain 1-way 10A switch,
the house-brand single-pole MCB). Resolution rules, in `engine/matching.py`:

1. Explicit attributes are **hard filters**. Asking for Finolex can never return
   Polycab — a named brand is never overridden.
2. One candidate → `RESOLVED`.
3. Several candidates, exactly one is the shop default → `RESOLVED` with
   `resolvedBy = "shop-default"`, and **every alternative is returned with it**.
   The UI shows a "Shop default · 5 other variants" badge and lists them under
   "Why?".
4. Several candidates, no single default → `AMBIGUOUS`, with the one attribute
   they differ on. The agent must ask.

This keeps the demo usable without the system ever guessing silently. "Finolex
1.5 sq mm wire" with no colour still resolves to three equally-default
candidates and therefore still asks. **This is a judgement call and is flagged
for review** — it is the one place where ShopFlow chooses on the owner's behalf,
and it is deliberately visible rather than hidden.

### Architecture

The split the product depends on, made structural:

| Layer | Owns | Cannot |
| --- | --- | --- |
| Nova Pro via Bedrock Converse | reading the order, choosing tools, wording the question | produce a SKU, price, total or stock figure |
| `engine/` | matching, inventory, shortages, pricing, totals, evidence | call a model — it does not import one |

Four strict tools, two of them terminal:

- `search_catalog` — structured attributes in, a real resolution out. Returns
  `AMBIGUOUS` with an instruction not to choose.
- `get_inventory` — deterministic stock for known SKUs.
- `calculate_quote` — **terminal.** Engine prices the order. Rejects unknown
  SKUs, non-integer or non-positive quantities, >25 lines, >10,000 per line.
- `request_clarification` — **terminal.** Returns the question plus real options.

Reaching a terminal tool ends the loop. There is no free-form chat path and no
generic chat endpoint: the only outcomes are a completed quotation or one
question. The loop is bounded at 6 turns and 3 tool errors.

**Grounding validator** (`agent/grounding.py`): every number the model writes in
a summary is checked against the numbers the tools actually produced. If any is
unsupported, the model's sentence is discarded and one assembled from the
engine's own figures is used instead. The UI reports which happened.

### Async by design

`POST /api/orders` writes a job and returns `202` with a job id; a second Lambda
runs the agent and the browser polls `GET /api/jobs/{id}`. The agent loop takes
roughly 3–4 seconds against Bedrock, and holding an HTTP connection open for
that on a public demo is a reliability risk. No Step Functions: a DynamoDB job
record and an async Lambda invoke are sufficient.

### Failures encountered and fixed

1. **Seed fixtures were unreachable from Lambda.** They lived in `/data/seed`,
   outside the `backend/` bundle. Moved to `backend/seed_data/`, generated by
   the same generator, so one artifact ships with the code and there is still
   no second source of truth.
2. **Clarification arrived with no options to click.** Against real Bedrock the
   model reliably asked the right question but usually omitted `skuIdOptions`,
   leaving the owner a question and no answer to tap. Fixed by rebuilding the
   option list from the catalogue inside the tool, so the choices are always
   present and always real — not left to the model.
3. **Duplicate `handler` keyword** in the CDK stack — caught at synth.
4. **Construct id collision**: `CfnOutput(self, "WorkerFunction")` clashed with
   the function construct of the same id. Renamed.
5. **Deployment failed and rolled back.** `AWS::ApiGatewayV2::Stage` rejected
   `RouteSettings`:

   > Unrecognized field "throttlingBurstLimit" … (5 known properties:
   > "ThrottlingBurstLimit", …)

   Cause: anything handed to CDK from Python as a map value — a typed
   `RouteSettingsProperty` *or* a plain dict with PascalCase keys — has its keys
   lower-cased crossing the jsii boundary. `add_override` with a whole dict did
   not help either. Fixed by overriding one leaf at a time
   (`Properties.RouteSettings.POST /api/orders.ThrottlingRateLimit`), because
   override *path segments* are preserved verbatim.

6. **Agent mistake worth recording.** While diagnosing (5), the template was
   read three times from a **stale `cdk.out`** and the conclusion "the override
   did not work either" was drawn twice from cached output. Both the override
   fix and the reserved-concurrency setting had in fact applied correctly. The
   correct step — delete `cdk.out` and run a full synth — was taken only on the
   fourth attempt. Reading build output without confirming it was freshly
   generated produced two wrong diagnoses in a row.

7. **Second deploy failure, same resource, different cause.** With the casing
   fixed, API Gateway returned:

   > Unable to find Route by key POST /api/orders within the provided
   > RouteSettings (404)

   CloudFormation was updating the stage before the route existed. A
   `DependsOn` from the stage to all three routes fixed the ordering, and the
   synthesised template was verified to carry it.

8. **The rollback then failed too, wedging the stack.** Rolling back deletes
   routes *before* updating the stage, so the rollback hit the identical 404
   and the stack landed in `UPDATE_ROLLBACK_FAILED`. Recovered with
   `continue-update-rollback --resources-to-skip HttpApiDefaultStage…`.

   **Decision reversed as a result.** Per-route throttling was removed
   entirely, even though the `DependsOn` fix worked, because the construct is
   a standing trap: any future failed deploy would wedge the stack the same
   way, and a public demo has to stay redeployable under deadline pressure.
   The stage now carries a single throttle of 10 rps / 20 burst for every
   route. The genuine cost ceiling was always the worker's reserved
   concurrency of 5, which bounds concurrent Bedrock calls regardless of how
   the request arrived — the per-route limit was the weaker of the two
   protections and the one carrying the operational risk.

   This is the clearest example so far of the agent getting something wrong
   and the *right* correction being to delete the feature rather than to keep
   fixing it. Three deploys were spent before that call was made.

### Demo safety before the public Bedrock path opened

- `reserved_concurrent_executions=5` on the worker — a hard ceiling on
  concurrent Bedrock calls, which caps cost blast radius far more directly than
  request throttling does.
- Stage throttled to 10 rps / 20 burst across every route (see failure 8 for
  why this is one stage-wide limit rather than a tighter per-route one).
- Request body capped at 4 KB, order text at 1,000 characters, both rejected
  before anything is queued.
- Agent loop bounded: 6 turns, 3 tool errors, 1,200 output tokens.
- `retry_attempts=0` on the async invoke, so a failing request is not retried
  into more Bedrock spend.
- IAM `bedrock:InvokeModel` scoped to the one model, via the APAC inference
  profile ARN and that foundation model only. The API Lambda has **no** Bedrock
  permission at all — only the worker does.
- Job records carry a TTL and expire themselves.
- Worker logs a structured line (status, turns, grounded, latency, model) and
  **never logs the customer's order text or model output**.

---

## Day 2 — 2026-09-20 — Stage 3 recovery and deployment

### What the previous session left behind

Stage 3 was written, tested and verified against real Bedrock, but never
reached AWS. The sequence:

1. Deploy attempt 1 failed on the API Gateway stage (camelCase `RouteSettings`).
2. The rollback then failed on the same resource, wedging the stack in
   `UPDATE_ROLLBACK_FAILED`; recovered with `continue-update-rollback
   --resources-to-skip HttpApiDefaultStage…`.
3. Per-route throttling was removed entirely as a result (see failure 8 above).
4. A fresh deploy was launched — and **the session ended while it was still
   running**. The background process was killed mid-run.

**This was not an application failure.** The background job's output file was
empty, and CloudFormation showed the stack had simply returned to
`UPDATE_ROLLBACK_COMPLETE` at 14:54 with the Stage 3 roles and log groups
deleted. Stage 2 remained live and healthy throughout: the public URL returned
200 and `/api/health` reported DynamoDB `ok`.

State confirmed before doing anything: `HEAD` at `e438168`, Stage 3 entirely
uncommitted, 162 tests passing, only `shopflow-health` deployed.

### The CloudFront fallback defect

Verifying the rolled-back stack turned up a real bug that had been shipped in
Stage 2 and would have gone unnoticed:

```
GET /api/demo    -> 200  Content-Type: text/html
GET /api/orders  -> 200  Content-Type: text/html
```

Neither route existed in API Gateway at the time. They returned **200 and the
landing page**. The cause was the SPA error mapping added in Stage 2:

```
403 -> 200 /index.html
404 -> 200 /index.html
```

CloudFront custom error responses are **distribution-wide** — they cannot be
attached to a single cache behaviour — so the `/api/*` behaviour inherited them
and every API 404 was rewritten into a successful HTML page. A broken or
misspelled endpoint would have looked like a working one, to a browser, to a
test, and to a judge.

**Fix.** Drop the 404 mapping and keep only 403. The two origins fail
differently, and that difference does the separation for free:

- S3 behind OAC answers **403** for a missing object, because the bucket policy
  grants `GetObject` but not `ListBucket` — so the SPA fallback still works.
- API Gateway answers **404** for an unknown route — so API 404s now reach the
  caller intact.

No new services, no Lambda@Edge, no CloudFront Function. One line removed.

### Pre-deployment verification

| Check | Result |
| --- | --- |
| Test suite | 162 passed |
| `cdk synth` | exit 0, template written |
| CloudFront error responses | only `403 -> 200 /index.html`; **no 404 mapping** |
| `/api/*` behaviour | CachingDisabled, AllViewerExceptHostHeader, all methods |
| API routes | `GET /api/health`, `POST /api/orders`, `GET /api/jobs/{jobId}`, `GET /api/demo` |
| Stage throttling | 20 rps / 40 burst, `RouteSettings` empty (no per-route) |
| Worker Lambda | 1024 MB, 60 s, **reserved concurrency 5** |
| Worker Bedrock IAM | `bedrock:InvokeModel` on the Nova Pro foundation model and APAC inference profile only |
| API Lambda IAM | DynamoDB + `lambda:InvokeFunction` on the worker — **no Bedrock permission** |
| Health Lambda IAM | read-only, ShopFlow table only |
| S3 buckets | both block all public access, AES256 |
| DynamoDB | TTL on `expiresAt` |
| Log groups | 4, all 14-day retention |
| Resource types | no new AWS services introduced |
| Staged files | 26; no `__pycache__`, `cdk.out`, or secrets |

### Deploy attempt 4 — a fourth failure, and an account limit

Committed as `68cdb02`, then deployed. It failed on a new resource:

> Specified ReservedConcurrentExecutions for function decreases account's
> UnreservedConcurrentExecution below its minimum value of [10].

**Diagnosis before changing anything.** `aws lambda get-account-settings`
returned:

```
ConcurrentExecutions: 10
UnreservedConcurrentExecutions: 10
```

This account's **total** Lambda concurrency is 10, not the usual 1000 — the
default for an account that has not been through a limit increase. AWS requires
at least 10 to remain unreserved, so *any* reservation is rejected outright.
The setting was never going to work here.

**Correction, and why it is the right outcome rather than a compromise.**
`reserved_concurrent_executions` was removed. Reserving 5 of 10 would have left
5 for every other project in the account — SilentSignal, TinaiPoet,
harvest_convoy and AwsDeployDoctor all draw on the same pool — so the setting
would have degraded unrelated projects to protect this one. The 10-wide account
ceiling already bounds concurrent Bedrock calls more tightly than the
reservation would have. It is a shared ceiling rather than a private one, which
is a real difference, and it is recorded in the risks rather than papered over.

This is the second time in Stage 3 that the right fix was to delete a control
rather than to keep repairing it.

### Deployment — succeeded

`ShopFlowStack` → **UPDATE_COMPLETE**, 2026-09-20T08:08:27Z, 1,724s (most of it
CloudFront propagation).

| Output | Value |
| --- | --- |
| SiteUrl | https://d3m3lwn03zb2eu.cloudfront.net |
| BedrockModelId | `apac.amazon.nova-pro-v1:0` |
| WorkerFunctionName | `shopflow-order-worker` |
| TableName | `shopflow-demo` |

### Verification — live, against the public URL

**The routing fix, proven both ways:**

```
GET /api/definitely-not-a-route  -> 404  application/json   (real API 404)
GET /some-frontend-route         -> 200  text/html          (SPA fallback intact)
```

**The canonical order, submitted through the public API:**

```
POST /api/orders -> 202 {"jobId": "a290aa0a…", "status": "QUEUED"}
GET  /api/jobs/a290aa0a… -> DONE

agent status=QUOTED  model=apac.amazon.nova-pro-v1:0  turns=2  grounded=true
   20 x Anchor Modular Switch 1-Way 10A White    Rs  1566.00  onHand=14  short=6
    3 x Finolex 1.5 sqmm FR Wire Red 90m coil    Rs 19824.00  onHand=1   short=2
    2 x Havells MCB SP 32A C-Curve               Rs   916.48  onHand=5   short=0
   QUOTE TOTAL = Rs 22306.48
```

Total and shortages match the engine's seeded scenario exactly — the figures
the tests assert, produced live through Bedrock, Lambda and DynamoDB.

**The ambiguous order:**

```
agent status=NEEDS_CLARIFICATION
question:  Please specify the colour of the Finolex 1.5 sq mm wire you need.
attribute: colour
  Black -> W-FIN-1.5-BLK-90M
  Blue  -> W-FIN-1.5-BLU-90M
  Red   -> W-FIN-1.5-RED-90M
```

It asked rather than guessed, and offered three real SKUs.

**An invented SKU pushed in through the clarification path:** rejected `400`.

| Check | Result |
| --- | --- |
| CloudFormation | `UPDATE_COMPLETE` |
| Public URL | 200, 23,133 bytes, Stage 3 UI present |
| `/api/health` | 200 `application/json`, DynamoDB `ok` 241 ms |
| API 404 | 404 `application/json` — **not** HTML |
| SPA fallback | 200 `text/html` for a frontend route |
| API routes | health, orders, jobs/{jobId}, demo |
| Lambdas | `shopflow-api` 512 MB, `shopflow-order-worker` 1024 MB, `shopflow-health` 256 MB |
| Throttling | 20 rps / 40 burst stage-wide, `RouteSettings` empty |
| CloudWatch | 4 log groups, 14-day retention; worker emitted structured `order_processed` lines |
| Budget | `shopflow-monthly` USD 25 active |
| Tests | 162 passed |
| Isolation | `AwsDeployDoctorStack` last updated 2026-09-11, `CDKToolkit` 2026-08-20 — untouched |

**Privacy check on the logs.** The worker's log line carries status, turns,
grounded, latency, model id and `orderChars` — a character count. The
customer's order text and the model's output are not logged.

---

## Day 2, Stage 3.1 — 2026-09-20 — Variant ambiguity hardened

### The issue

Stage 3 shipped `Product.isDefaultVariant` as a tie-breaker. When several real
products matched the customer's wording, the shop's usual variant was selected
and the alternatives were shown in the UI as "Shop default · 5 other variants".

That was the wrong call, and the reasoning behind it was wrong in a specific
way: **disclosure is not consent.** A badge explains a decision that has
already been made. On a quotation the consequences are concrete — the wrong
variant is a different price, a different stock position and a different
delivery. `SW-ANC-1W10A` at ₹78.30 and `SW-ANC-SKT16A` at ₹199.80 are both
"Anchor modular switches" to a contractor who did not say which.

### New rule

If more than one catalogue product fits the customer's wording, ShopFlow asks.
There is no automatic selection. `resolve_product` now returns exactly
`RESOLVED` (one product fits), `AMBIGUOUS` (several fit), or `NOT_FOUND`.

`isDefaultVariant` is retained on the catalogue as metadata for owner-facing
features later — a "reorder my usual" flow is a legitimate future use, because
there the owner is the one choosing. It takes no part in customer order
processing, and a test asserts that no search result can ever come back
`resolvedBy = "shop-default"`.

Where exactly one attribute separates the candidates, the question is about
that attribute ("which colour?"). Where several do — Finolex 1.5 Red exists in
both a 90m and a 180m coil, so colour *and* length vary at once — there is no
single clean question, so the actual products are offered instead.

### The canonical demo order had to change more than requested

The instruction was to make the switch line explicit:

> "Anna, 20 Anchor modular switches 1-Way 10A, 3 coils Finolex 1.5 sq mm red
> wire, 2 MCB 32 amp."

Checked against the real catalogue, **two of those three lines were still
ambiguous** under the new rule:

| Line | Matching products | Why |
| --- | --- | --- |
| `Anchor modular switches 1-Way 10A` | 1 | fine |
| `Finolex 1.5 sq mm red wire` | 2 | Red exists in 90m and 180m coils |
| `MCB 32 amp` | 6 | Havells/Schneider/Legrand × SP/DP |

Leaving the text as given would have made the headline demo end in a
clarification rather than a quotation. The order text was therefore extended so
every line names its variant:

> "Anna, 20 Anchor modular switches 1-Way 10A, 3 coils Finolex 1.5 sq mm red
> wire 90m, 2 Havells MCB SP 32A."

The SKUs are unchanged, so the deterministic result is unchanged:
**₹22,306.48**, shortages **6 / 2 / 0**. No catalogue products were deleted to
make this work — the 180m coil and the rival MCB brands are realistic stock and
are what make the ambiguity demo meaningful.

### A UI flaw the change exposed

With defaults no longer narrowing the candidate list, the no-colour wire
question came back offering **"Red" twice** — once for the 90m coil and once
for the 180m. Two identical labels are not a choice. Clarification options now
fall back to full product names whenever the attribute labels would collide or
be blank, and keep the short label ("1-Way 10A") when it is unique.

### A worse bug, caught by live verification after deployment

Post-deploy testing of a case the instructions did not ask for — `"Anna, 2 MCB
32 amp."` — came back wrong. The question was about brand, but the options
were **twelve Havells MCBs from 6A to 40A**. The customer's "32 amp" had been
dropped, and the list would have offered a 6A breaker for a 32A request.

Two defects behind it:

1. **The clarification fallback re-derived candidates from free text.** When
   the model omits `skuIdOptions`, the tool rebuilt the list by re-resolving
   `requestedText` with no attributes — discarding the `category=MCB,
   specification=32A` the original search had used. The fix is to remember the
   search that produced the ambiguity, keyed by the customer's wording, and
   reuse its candidates. The orchestrator now carries that context into the
   tool.

2. **`search_catalog` capped candidates at 12.** With 36 MCBs in the catalogue,
   a cap that bites returns an alphabetically-biased subset — every Havells,
   no Schneider or Legrand. A truncated candidate list turns into a
   clarification that omits the right answer. Raised to 40, above the largest
   real product family.

After the fix the same order returns exactly six options, all 32A, across all
three brands.

This one is worth noting because the first three test cases all passed. The
defect only appeared on an order nobody had specified, which is the argument
for testing beyond the scripted demo.

### Verification

Live against Bedrock before deploying:

```
CANONICAL   -> QUOTED, 2 turns, grounded
                20 x Anchor Modular Switch 1-Way 10A   Rs  1566.00  short 6
                 3 x Finolex 1.5 sqmm Wire Red 90m     Rs 19824.00  short 2
                 2 x Havells MCB SP 32A C-Curve        Rs   916.48  short 0
                TOTAL Rs 22306.48

"3 coils Finolex 1.5 sq mm wire"  -> NEEDS_CLARIFICATION (colour), 4 real SKUs
"20 Anchor modular switches"      -> NEEDS_CLARIFICATION (specification), 6 real SKUs
```

Tests: **162 → 174**, all passing.

---

## Day 2, Stage 4 — 2026-09-20 — Supplier price intelligence

**Prompt.** "Supplier price-list image → Nova Pro Vision extraction → structured
supplier price records → match only against existing catalog SKUs →
deterministic price comparison → price-change detection → owner review →
price history. This is NOT an OCR showcase."

### The design decision that shaped the stage

The business outcome is *"tell the owner when a supplier's price has materially
changed, and show the evidence."* That splits cleanly along the line the whole
product is built on:

| Layer | Owns |
| --- | --- |
| Nova Pro | transcription — what does this document say |
| `engine/supplier_prices.py` | which SKU, what we last paid, whether it matters |

The model reads descriptions and printed rates. It never computes a
percentage, never names a catalogue SKU, and never decides materiality.

### A data problem that had to be fixed first

The seed already carried `5,900 → 6,300` as recorded history for the Finolex
wire. So uploading a price list saying ₹6,300 would have compared 6,300 against
6,300 and reported **no change** — the system would have "discovered" something
it had been told in advance.

The 6,300 was removed from the seed. The shop's own record now ends at **5,900**
— the last price it actually paid — and the 6,300 arrives only when the document
is read. The +6.78% is therefore calculated at that moment, from two figures the
shop can point at.

To keep the Stage 1 scenario detector honest, the seeded history was changed to
`5,600 → 5,900` (+5.36%), which is still a material move on its own. Two tests
were updated to the new figures. **The canonical quote is unaffected at
₹22,306.48**, because a quotation is priced from selling price, not supplier
cost. The budget scenario shifted slightly (₹24,993.16 → ₹24,996.56 of ₹25,000)
since the wire now costs less to restock; the budget still binds.

### Ingestion flow

```
browser --base64 image--> POST /api/supplier-price-lists
                             |  validate type, magic bytes, size
                             |  PutObject (private, AES256)
                             |  job record + async worker invoke
                             +--> 202 {jobId}

worker --GetObject--> Nova Pro Vision --> strict JSON --> engine review
                                                            |
browser --poll--> GET /api/jobs/{jobId} <-------------------+
```

The image travels as base64 in the request body rather than through a presigned
URL. That keeps the uploads bucket entirely private: the browser never holds a
credential, a bucket name, or a URL into S3. The cost is a size ceiling — 2.5 MB
of image, 4 MB of body — which is ample for a phone photo of a price sheet.

The existing async job pattern was reused unchanged; the job record simply
carries a `jobType`. No new AWS services.

### Extraction schema

Strict, and rejected rather than repaired:

```
supplier: {name}
document: {date}
items[]:  {description, supplierCode, brand, specification,
           colour, length, unit, price}
```

Validation refuses: non-JSON output, a top-level array, missing or empty
`items`, more than 50 rows, a non-object row, a price that is a string
(`"6,300"`), a non-positive price, and a boolean price. A model that cannot
produce this shape produces an error, not a best guess.

Images are checked before they reach Bedrock: declared content type must be one
of PNG/JPEG/WebP **and** the file's leading bytes must agree. A PDF renamed
`.png` is rejected without a model call.

### Matching rules

Supplier lines go through the **same resolver as customer orders**, so Stage
3.1's rule applies unchanged: several matching products means a question, never
a quiet pick.

- `MATCHED` — exactly one catalogue product fits; price compared.
- `AMBIGUOUS` — several fit; candidates listed, **no comparison performed**,
  because comparing against a SKU we are not sure of would be worse than saying
  nothing.
- `UNMATCHED` — nothing fits; no SKU is invented.

There is no confidence score. A number between 0 and 1 would imply a
calibration that does not exist here, so the status is the honest answer.

### Price calculation rules

```
previousPrice  = the shop's last recorded supplier cost
currentPrice   = the rate printed on the document
absoluteDelta  = current - previous
percentageDelta= (current - previous) / previous x 100
materialChange = |percentageDelta| > 5.0
```

**5% default, configurable per call.** Materiality is judged on magnitude, so a
sharp *fall* is surfaced too — a supplier cutting a price is worth knowing
before the next purchase. A first-ever quote has no previous price and is
reported as such rather than as a rise. The boundary case is pinned by a test:
exactly 5.00% is *not* material, because the rule is "more than 5%".

### Human approval

A confirmed change writes a **decision record** and nothing else.
`build_decision_record` returns `catalogPriceChanged: False`, and a test asserts
the catalogue cost and `current_cost()` are untouched after confirmation.
Applying a confirmed price to stock valuation and purchasing is a separate step
the owner triggers knowingly — it is not a side effect of agreeing that a
document is accurate.

The comparison figures in a decision are read from the **stored job**, never
from the request body, so a caller cannot post a percentage the engine never
calculated. A test covers that.

### Verification against real Bedrock

The canonical price list is generated from the seeded catalogue
(`data/price_list_image.py`), so its rates trace to the same dataset as
everything else. It deliberately carries one of each case. First run, no
retries:

```
supplier: SRI BALAJI ELECTRICALS      date: 15-09-2026
usage: 1,034 input / 465 output tokens

[MATCHED]   Finolex 1.5 sqmm FR Wire RED 90m coil   Rs 6300.0
            -> W-FIN-1.5-RED-90M
            prev=5900.0 new=6300.0 delta=400.0 pct=6.78% INCREASE material=True
            calc: (6300.0 - 5900.0) / 5900.0 x 100 = 6.78%
[MATCHED]   Anchor Modular Switch 1-Way 10A White   Rs 58.99
            prev=58.0  pct=1.71%  material=False
[MATCHED]   Havells MCB SP 32A C-Curve              Rs 358.0
            prev=358.0 pct=0.0%   UNCHANGED
[AMBIGUOUS] Finolex 1.5 sqmm FR Wire 90m coil       attribute=colour, 3 candidates
[UNMATCHED] Kaveri 4-core Armoured Cable 25 sqmm
```

### Security

- Upload capped at 2.5 MB image / 4 MB body; both rejected before S3 or Bedrock.
- MIME allowlist plus magic-byte check.
- Uploads bucket stays private: **API has PutObject only, worker has GetObject
  only**. The API cannot read back a document it stored, so a bug there cannot
  become a disclosure path.
- Bedrock permission remains on the worker alone.
- Stage throttling, budget alarm and 14-day log retention unchanged.
- No per-route throttling added.
- Uploaded objects expire after 30 days via the existing lifecycle rule; job and
  decision records carry a TTL.

### Observability

The worker logs one structured line per price list: job id, type, model, latency,
image size in bytes, and the item/matched/ambiguous/unmatched/material counts,
plus token usage. **No description, price, supplier name or image content is
logged.**

### A production failure the tests could not have caught

Deployed, then ran the whole flow against the public URL. Extraction, matching
and the +6.78% all worked first time. **Confirming the change returned 500.**

CloudWatch gave the cause in one line:

```
ERROR handling POST /api/price-decisions:
TypeError: Float types are not supported. Use Decimal types instead.
```

DynamoDB does not store Python floats, and the decision record carries three of
them — previous price, current price, percentage. The in-memory `FakeTable` in
the tests accepts anything a dict accepts, so every unit test passed against a
store more permissive than the real one.

Fixed by converting floats to `Decimal` on write, via `str()` so 6300.0 is
stored as `6300.0` rather than its binary expansion. Two tests were added: one
asserting the stored fields really are `Decimal`, one asserting they come back
to the browser as ordinary JSON numbers. Neither would have been written
without the failure, which is the honest argument for testing against the
deployed system and not only the fakes.

### Live verification, after the fix

```
GET  /sample-price-list.png        200, 40,016 bytes
POST /api/supplier-price-lists     202  jobType=PRICE_LIST
GET  /api/jobs/{id}                DONE

supplier=SRI BALAJI ELECTRICALS  date=15-09-2026  threshold=5.0%
lines=5 matched=3 ambiguous=1 unmatched=1 material=1

  [MATCHED]   W-FIN-1.5-RED-90M  prev=5900.0 new=6300.0 delta=400.0
              pct=6.78% INCREASE material=True
  [MATCHED]   SW-ANC-1W10A       pct=1.71%  material=False
  [MATCHED]   MCB-HAV-SP-32A-C   pct=0.0%   UNCHANGED
  [AMBIGUOUS] colour, 3 candidates
  [UNMATCHED] Kaveri 4-core Armoured Cable

POST /api/price-decisions          201  CONFIRMED  catalogPriceChanged=False
GET  /api/jobs/{id}                decisions persisted: 1

rejected: invented SKU 400, invalid decision 400, PDF upload 400
```

Worker log line, contents-free as intended:

```json
{"event": "price_list_processed", "jobType": "PRICE_LIST", "status": "DONE",
 "modelId": "apac.amazon.nova-pro-v1:0", "elapsedMs": 3531.4,
 "imageBytes": 40016, "itemCount": 5, "matchedCount": 3, "ambiguousCount": 1,
 "unmatchedCount": 1, "materialChangeCount": 1,
 "inputTokens": 1034, "outputTokens": 465}
```

No regression: the canonical order still quotes **₹22,306.48**, `/api/health`
is 200 JSON, an unknown `/api/` route is still a real 404, and the SPA fallback
still serves for frontend routes.

Tests: **177 → 246**, all passing.

---

## Stage 5 — Cash-constrained purchasing planner

The stage that makes ShopFlow a decision tool rather than a reporting tool. The
owner states the cash they actually have; the engine decides how to spend it,
and shows what it turned down.

### Design: what Stage 5 did and did not build

Almost nothing new was built in the decision path. `engine/budget.py`
—the two-tier allocator written in Stage 1— is unchanged. Stage 5 added
`engine/purchasing.py`, which supplies the two things the allocator
deliberately left out:

1. **Confirmed supplier costs.** Stage 4's `build_decision_record` records an
   owner's ruling and explicitly does not rewrite the catalogue, noting that
   applying it to purchasing is "a separate, later step that they trigger
   knowingly". Asking for a purchase plan *is* that step.
2. **Presentation.** The allocator emits SKU ids and raw figures. An owner
   needs product names, the stock position and the arithmetic written out.

No figure in `purchasing.py` is computed twice. Every number in the response is
lifted from the allocator's own evidence dict.

### Why the deterministic engine owns the decision

The allocation is the commercial core of the product. It is also the part a
language model is least suited to: it is a constrained arithmetic problem where
a plausible-looking wrong answer is worse than no answer, because the owner
would spend real money on it.

The engine owns quantities, inventory, shortage, committed demand, supplier
cost, velocity, coverage, stockout risk, margin, allocation, remaining budget,
deferrals and the numerical explanations. It runs in **3.77 ms** over the full
147-SKU shop and calls no model.

The LLM's role in Stage 5 is *nothing*. It does not appear in the purchase
planning path at all. It collects the order upstream (Stage 3) and reads the
supplier document (Stage 4); it does not touch the budget. This is stricter
than the brief allowed — the LLM *may* explain the result — and it was the
right call: the engine's `reason` and `risk` fields are already plain English
sentences derived from the figures, so a narration layer would have added
latency, cost and a grounding surface for no gain in clarity.

### Selling price vs supplier purchase cost

These are two different numbers and the planner never lets them merge:

    sellingPrice  what the customer pays. Lives on the Product. A confirmed
                  supplier price change NEVER touches it - repricing the shelf
                  is a commercial decision, not an arithmetic consequence.
    unitCost      what the shop pays the supplier. From the supplier price
                  history, and the only number the budget is spent in.

A plan is denominated entirely in `unitCost`. `sellingPrice` appears only
inside `marginPerRupee`, where the gap between the two is the whole point, and
alongside it in the UI so the owner can see they are distinct.

`apply_confirmed_costs` implements this by **appending** the confirmed price to
a copy of the SKU's supplier price history rather than overwriting anything.
Three consequences fall out for free: `current_cost` already reads the latest
entry, so margin, priority and line cost all pick the change up with no extra
plumbing; the prior price survives, so the evidence can still show what
changed; and the seeded dataset is never mutated by having been planned
against. Tests 15b and 15d pin all three.

### The canonical scenario, recomputed

The brief quoted Tier 1 ₹12,948.00 / Tier 2 ₹12,045.16 / remaining ₹6.84 and
warned that the Stage 4 seed change may have moved them. Running the current
engine against the current seed showed both figures are correct — they are two
different points in the demo story:

| ₹25,000 budget      | Before confirming | After confirming +6.78% |
|---------------------|------------------:|------------------------:|
| Tier 1 commitments  |       ₹12,148.00  |             ₹12,948.00  |
| Tier 2 restocking   |       ₹12,848.56  |             ₹12,045.16  |
| Total spend         |       ₹24,996.56  |             ₹24,993.16  |
| Remaining           |            ₹3.44  |                  ₹6.84  |
| Restocks funded     |                 9 |                       9 |
| Restocks deferred   |                49 |                      49 |

Confirming the supplier increase costs the shop **₹803.40 of restocking** — the
₹800 extra on two wire coils, plus ₹3.40 of reshuffling as the greedy allocator
refits what remains. That is the sharpest demonstration of the whole product:
a price rise the owner would otherwise not have noticed, detected from a
photograph, and its exact consequence for what they can afford to stock.

Nothing is hard-coded. The UI reads every figure from `POST /api/purchase-plans`.

### What-if budgets

| Budget   | Commitments | Restocking | Total      | Unspent | Funded | Deferred |
|----------|------------:|-----------:|-----------:|--------:|-------:|---------:|
| ₹20,000  |  ₹12,148.00 |  ₹7,835.56 | ₹19,983.56 |  ₹16.44 |      6 |       52 |
| ₹25,000  |  ₹12,148.00 | ₹12,848.56 | ₹24,996.56 |   ₹3.44 |      9 |       49 |
| ₹30,000  |  ₹12,148.00 | ₹17,850.36 | ₹29,998.36 |   ₹1.64 |     13 |       45 |

Commitments are constant across all three, which is the policy working: the
customer promise is funded first and is not a function of how much cash is
left. All three bind — every commitment funded, and restocking still refused.

Deliberately two fixed alternatives rather than a general scenario engine. The
question an owner actually asks is "what if I had a bit more, or a bit less",
and two concrete answers settle it.

### Budget binding

`budget_is_binding` requires both halves: every commitment funded, **and** at
least one restock refused for lack of cash. Either alone is misleading — an
unfunded commitment means the shop is failing customers, and a plan that buys
everything is not a decision. Asserted in `test_19_canonical_budget_binds`,
which additionally requires the remaining cash to be under 1% of the budget, so
the demo cannot quietly drift into a budget that is not really a constraint.

No vanity metrics. There is no "items saved" or "₹ protected" figure anywhere:
every number displayed is one the allocator actually computed.

### API: why this route is synchronous

`POST /api/purchase-plans` returns **200 with the plan**, not 202 with a job id.

The brief suggested the async job pattern "if required by the existing
architecture". It is not. Orders and price lists are queued because a Bedrock
tool loop takes seconds and holding an HTTP connection open that long on a
public demo is a reliability risk. The planner calls no model: it is 3.77 ms of
arithmetic over an already-cached dataset. Wrapping that in a job record, an
asynchronous Lambda invoke and a browser polling loop would add three round
trips and roughly a second of latency to hide four milliseconds of work, and
would spend some of the account's 10-wide concurrency ceiling doing it.
Measured end-to-end through CloudFront: **1,583 ms** including TLS and cold
path, sub-second warm.

Confirmed prices are read from the stored `DECISION#` rows by `priceListJobId`,
never from the request body — the same rule Stage 4 applied to comparison
figures. A caller cannot price a plan at a number the engine never produced.

### Real DynamoDB smoke test

Stage 4 shipped a 500 that the unit suite could not have caught: the in-memory
`FakeTable` accepts anything a dict accepts, the real table rejects float. The
Stage 5 planner reads confirmed prices back out of that same table, so the same
class of bug would land the same way.

`scripts/smoke_test_planner.py` closes the gap by exercising the real store:

    write a decision record (Decimal) -> read it back -> run the engine
                                      -> assert the expected allocation

It is explicitly **not** part of the pytest suite. Unit tests stay fast and
need no AWS account; this is run before a deploy. It writes only `SMOKE#`-
prefixed keys and deletes them in a `finally` block.

Result — 12/12 checks passed, including that the price returns as `Decimal` and
not float, that the selling price was not rewritten, and that confirming the
rise costs exactly ₹800.00 more in Tier 1.

### Tests

**246 → 303**, all passing in 1.6 s. 45 in `test_purchasing.py`, 12 added to
`test_api.py`.

Coverage: zero budget, negative rejection, non-numeric rejection, sufficient
and insufficient budget, commitments prioritised over restocking, spend never
exceeding budget and remaining never negative across nine budgets on the real
seed, restock ranking, stockout risk, margin-per-rupee measured at supplier
cost rather than catalog cost, deferred-item explanation, PARTIAL_ALLOWED,
ALL_OR_NOTHING, confirmed supplier cost, rejected decision ignored, selling
price untouched, source dataset unmutated, the three canonical budgets,
monotonicity, and the binding assertion.

### Failures and corrections

**Two wrong test expectations of mine, both about greedy allocation.** I
asserted that deferred items always rank below every selected item, and that
the displayed order is globally descending by priority. Both failed on the real
seed, and both were my error, not the engine's. A greedy allocator legitimately
skips an expensive high-priority line that does not fit and funds a cheaper
lower-priority one that does — that is the point of it. The real invariant is
that nothing was deferred while the shop could still afford it, which is what
`test_11b` now asserts, against each line's own `budgetRemainingBefore`.

**The budget alarm nearly got deleted.** `cdk diff` before deploying showed:

    [-] AWS::Budgets::Budget MonthlyBudget destroy

I had not touched it. The budget is gated on `if alert_email:`, and
`alertEmail` is optional CDK context — so synthesising without it silently
drops the resource, and deploying would have destroyed the cost guard the brief
requires be kept. Recovered by reading the deployed template for the address
actually in use and redeploying with
`-c alertEmail=<address> -c monthlyBudgetUsd=25`, after which the diff showed
only the intended Stage 5 changes. This is a standing trap: **every future
deploy must pass `alertEmail`.** Caught only because the diff was read rather
than skipped.

**A bash heredoc lost a large JS patch silently.** A compound Bash command
mixing a Python heredoc with a following `node --check <(...)` failed to parse
as a whole; the Python never ran and the file was unchanged, while the failure
looked like a syntax error in the check step. Confirmed by grepping for the
inserted symbol, which returned 0. Rewritten as a standalone script file in the
scratchpad. Same lesson as the earlier PowerShell here-string problem: for any
substantial patch, write the script to a file rather than inlining it.

### Deployment evidence

Deployed 2026-09-20, 37.5 s, `UPDATE_COMPLETE`.

Change set, read before applying — one route, one permission, three Lambda code
updates, one site redeploy:

```
[+] AWS::ApiGatewayV2::Route      POST /api/purchase-plans
[+] AWS::Lambda::Permission       apigateway -> ApiFunction (that route only)
[~] AWS::Lambda::Function         ApiFunction, WorkerFunction, HealthFunction
[~] Custom::CDKBucketDeployment   SiteDeployment
```

Stage throttle verified in the synthesised template as 20 rps / 40 burst with
`RouteSettings` absent — the Stage 3 per-route trap stays avoided.

Live, through CloudFront:

```
POST /api/purchase-plans  {"budget":25000}   200 application/json  1583 ms
  commitments  Rs 12,148.00  (2 lines, all funded)
  restocking   Rs 12,848.56  (9 selected)
  TOTAL        Rs 24,996.56   remaining Rs 3.44
  deferred     49 items       binding=True

spend <= budget and remaining >= 0 held at
  Rs 0, 1, 250, 5,000, 12,148, 20,000, 25,000, 30,000, 100,000

rejected: negative 400, string 400, missing 400, absurd 400, bad job id 400
```

Full demo arc, end to end on the deployed system:

```
5.  price list read        SRI BALAJI ELECTRICALS, 15-09-2026, 5 lines
6.  increase detected      W-FIN-1.5-RED-90M  5900 -> 6300  +6.78%
                           (6300.0 - 5900.0) / 5900.0 x 100 = 6.78%
7.  owner confirms         CONFIRMED, catalogPriceChanged=False
8.  "I only have Rs 25,000"
9.  allocation             commitments 12,948.00  restock 12,045.16
                           total 24,993.16  remaining 6.84
                           the confirmed rise cost Rs 803.40 of restocking
10. repriced line          supplier cost Rs 6,300.00 (was 5,900.00)
                           selling price Rs 6,608.00  <- UNCHANGED
12. why?                   SW-ANC-BELL: stock 2, uncommitted 2, 3.88/wk,
                           covers 0.52 wks, risk 0.7875,
                           priority = 0.7875 x 0.35 = 0.275617
```

No regression: the canonical order still quotes **₹22,306.48** with
`grounded=true` in 2 turns; `/api/health` 200 JSON; unknown `/api/` route a
real 404 JSON; SPA fallback 200 HTML.

Security posture unchanged: stage-wide throttling only, Bedrock permission on
the worker alone (the planner needs none — it calls no model), uploads bucket
private, three Lambda log groups at 14-day retention, budget alarm intact. The
planner logs nothing about the request; a filter for `purchase` across the API
log group returns no events.

Other projects untouched: `AwsDeployDoctorStack` 2026-09-11, `CDKToolkit`
2026-08-20.

### Data provenance

Every figure above comes from **synthetic seeded demo data** — 147 SKUs
generated by `data/generator.py` at seed 20260919, modelled on a Madurai
electrical retailer but not drawn from a real shop's records. The allocation
logic is exact and tested; its *business* accuracy is unvalidated, and no claim
is made about it. Evaluation against real anonymised shop orders remains open.

---

## Stage 5.1 — Persist confirmed supplier purchase costs

Stage 5 left a confirmed price reachable only through the `priceListJobId` that
produced it. That made the shop's knowledge of what it pays a property of a
*document* rather than of the *shop* — and job records carry a 24-hour TTL, so
the confirmation would have quietly expired overnight. Stage 5.1 makes it
durable, with no new AWS service and no architectural change.

### The record

One item per SKU in the existing table:

    PK  SHOP#demo
    SK  COST#<skuId>

```json
{
  "PK": "SHOP#demo",
  "SK": "COST#W-FIN-1.5-RED-90M",
  "recordType": "CONFIRMED_SUPPLIER_COST",
  "shopId": "demo",
  "skuId": "W-FIN-1.5-RED-90M",
  "productName": "Finolex 1.5 sqmm FR Wire Red 90m coil",
  "supplierId": "SUP-BALAJI",
  "confirmedCost": 6300,
  "currency": "INR",
  "effectiveDate": "15-09-2026",
  "sourceJobId": "a250d9271ba14ddcae184ff1d5f0a099",
  "confirmedAt": 1789915870
}
```

Four decisions worth stating:

**Overwrite is the supersede rule.** The shop has exactly one current purchase
cost for a SKU at any moment and the newest confirmation is it. No history is
accumulated, because the brief asked for the smallest durable solution and a
price history is a different feature. The trail back to the document survives
via `sourceJobId`.

**No TTL.** Job records expire because they are the workings of one document.
This is shop state. `test_1c` and a smoke check both assert its absence — it
would be an easy line to add by copy-paste and a silent data-loss bug.

**The supplier comes from the catalogue, not the document.** A price list must
not be able to reassign a SKU to a different supplier by naming one.

**`currency` is stored explicitly.** A cost without a currency is not a cost,
even in a single-currency shop.

`shopId` is the partition key although the demo is single-tenant, so
multi-tenancy later needs no data migration.

### Who may write one

Only `POST /api/price-decisions` with `decision: CONFIRMED`. Extraction never
writes here: reading a price off a photograph is not agreement to pay it.
`build_cost_record` raises `InvalidCostRecordError` if handed anything but a
confirmation, so a rejection cannot write a cost record even by mistake at the
call site. A rejection returns `purchaseCostPersisted: false` and leaves the
shop planning at the cost it already knows.

The worker never touches these records — it does not handle confirmations — so
it gained no permission. The API function already held `grant_read_write_data`
on the table, so **the IAM model is unchanged**: `cdk diff` showed no IAM
statement changes at all.

Not written, and asserted so: selling price, stock, any quantity.

### Planner behaviour

A plan request now always reads `SHOP#demo` / `begins_with(COST#)` and applies
whatever the owner has confirmed. No job id is needed — that is the whole
point. `priceListJobId` still works and still wins where both exist, because
that case is the owner looking at one specific document and asking what it
would mean; Stage 5 callers are unaffected.

Every line now carries provenance:

| `costSource` | meaning |
|---|---|
| `CONFIRMED_SUPPLIER_PRICE` | the owner agreed this rate; `costProvenance` names the source price list and when |
| `SEEDED_SUPPLIER_PRICE` | the shop's existing supplier cost, nothing newer confirmed |

Provenance is carried from the record, never inferred by comparing the plan's
cost against the seed. `test_provenance_is_not_inferred_from_the_figures` pins
this: a confirmed price that happens to *equal* the seeded one is still
confirmed, and that is exactly the case — the owner agreeing the supplier had
not moved — that a comparison shortcut would mislabel.

### A bug the tests would have hidden, caught before deploying

The planner's new read uses a composite condition:

```python
Key("PK").eq(cost_pk(DEFAULT_SHOP_ID)) & Key("SK").begins_with("COST#")
```

`FakeTable.query` read `KeyConditionExpression._values[1]` and assumed it was
the partition key string. For an `And` condition `_values[1]` is a `BeginsWith`
*object*, so the comparison `k[0] == pk` was never true and the query returned
**nothing** — the planner would have found no confirmed costs at all, and every
unit test still passed.

Found by distrusting a green suite: the tests passed on the first run after
wiring the query, which was too easy for a change of that size. Inspecting the
condition object confirmed it.

This is the Stage 4 lesson recurring in a new shape. Stage 4's fake was too
*permissive* (accepted float where DynamoDB would not); this one was too
*naive* (could not parse a condition the real store handles fine). Both let a
broken path look healthy.

Two fixes. `FakeTable.query` now parses the condition properly — walking the
`And` tree for the PK equality and any SK `begins_with` — and raises rather
than guessing if it cannot find a partition key. And
`test_the_planner_query_really_reaches_the_cost_rows` asserts the fake's own
behaviour directly, including that the prefix is honoured rather than ignored,
so the same silent-empty failure cannot return unnoticed.

### Demo repeatability, and `scripts/reset_confirmed_costs.py`

Durability had an immediate side effect: the demo became one-shot. Once ₹6,300
is confirmed, the "before" state is gone, and the same price list no longer
shows a +6.78% change against it either.

`scripts/reset_confirmed_costs.py` puts the shop back to seeded state. It lists
by default and deletes only with `--confirm`, and touches only
`SHOP#<shopId>` / `COST#...` items.

There is deliberately **no API route, button or UI** for it. Erasing what the
owner agreed to pay is not something a web request should be able to do; it is
an operator action run from a machine with credentials.

Verified: after `--confirm`, the live planner returns the wire at ₹5,900 with
`SEEDED_SUPPLIER_PRICE` and `confirmedCosts: []`.

### Tests

**303 → 336**, passing in 1.9 s. 19 in `test_cost_records.py`, 14 added to
`test_api.py`.

Covering: confirmation persists the price; reload retrieves it; the planner
with no `priceListJobId` uses it; purchase cost stays separate from selling
price; rejection persists nothing; a newer confirmation supersedes an older
one; the source job id is retained end to end; the explicit job-id path still
works; only one row per SKU ever exists; the persisted value is `Decimal` not
float; the record carries no TTL; and the fake store's query really reaches the
rows.

### Smoke test

`scripts/smoke_test_planner.py` extended from 12 to **25 checks**, all passing
against real DynamoDB. New coverage: the cost record is accepted, carries no
TTL, comes back as `Decimal`, retains currency, supplier, effective date and
source job id, holds no selling price, and is found by the *composite* query —
the exact shape the fake got wrong. Then the stored row alone, with no job id,
produces a plan matching the job-id path to the rupee.

It writes to a `SMOKE#SHOP#demo` partition rather than `SHOP#demo`, so a failed
run can never leave a bogus cost sitting in the shop's live state.

### Deployment evidence

Deployed 2026-09-20, `UPDATE_COMPLETE`. `cdk diff` showed code and site only —
no new resources, **no IAM statement changes**, budget alarm preserved (the
`alertEmail` context was passed, per the Stage 5 warning):

```
[~] AWS::Lambda::Function       ApiFunction, WorkerFunction, HealthFunction
[~] Custom::CDKBucketDeployment SiteDeployment
```

Live chain, end to end:

```
0. plan, budget only        wire Rs 5,900.00  SEEDED_SUPPLIER_PRICE
                            commitments Rs 12,148.00  total Rs 24,996.56
1. price list uploaded      SRI BALAJI ELECTRICALS, 15-09-2026
2. change detected          5,900 -> 6,300   +6.78% INCREASE
3. owner confirms           purchaseCostPersisted=true
                            confirmedCost=6300.0 INR
                            catalogPriceChanged=false
4. plan, budget only        NO job id passed
5.   supplier cost          Rs 6,300.00
6.   selling price          Rs 6,608.00   UNCHANGED
7.   commitments            Rs 12,948.00  (+Rs 800.00 exactly)
8.   costSource             CONFIRMED_SUPPLIER_PRICE
     provenance             job a250d9271ba14ddcae184ff1d5f0a099
     other SKUs             SEEDED_SUPPLIER_PRICE
     total Rs 24,993.16     remaining Rs 6.84
backward compatible         explicit priceListJobId -> same Rs 24,993.16
```

No regression: the canonical order still quotes **₹22,306.48**, grounded;
`/api/health` 200 JSON; unknown `/api/` route a real 404 JSON.

### Data provenance

Unchanged from Stage 5: all figures come from **synthetic seeded demo data**
(147 SKUs, `data/generator.py`, seed 20260919). The confirmed cost mechanism is
real and tested; the prices it carries are not from a real shop.

---

### Open items after Stage 5.1

- Anthropic use-case form — **human action**, blocks nothing.
- Real Tamil-English recording — **human action**, needed before any voice
  accuracy claim.
- Console screenshots for the submission evidence pack.
- Seed the `shopflow-demo` table from the generator and expose read APIs.
- Ambiguous supplier price-list lines are surfaced but not resolvable in
  the UI — the owner can see them, not fix them.
- Confirmed costs are now durable (Stage 5.1). Remaining gap: the store keeps
  only the *current* cost per SKU, not a history — reverting a confirmation
  means re-confirming the older price from a document.
- A confirmed cost is shop-wide and permanent, so demo rehearsals must run
  `scripts/reset_confirmed_costs.py --confirm` to return to seeded state.
- Business accuracy of the allocation is unvalidated against real shop data.
