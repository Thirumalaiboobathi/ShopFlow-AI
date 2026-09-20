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

### Open items carried into Day 2

- Anthropic use-case form — **human action**, blocks nothing.
- Real Tamil-English recording — **human action**, needed before any voice
  accuracy claim.
- Console screenshots for the submission evidence pack.
- Seed the `shopflow-demo` table from the generator and expose read APIs.
