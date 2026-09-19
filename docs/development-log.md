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

### Open items carried into Day 2

- Anthropic use-case form — **human action**, blocks nothing.
- Real Tamil-English recording — **human action**, needed before any voice
  accuracy claim.
- Console screenshots for the submission evidence pack.
- Seed the `shopflow-demo` table from the generator and expose read APIs.
