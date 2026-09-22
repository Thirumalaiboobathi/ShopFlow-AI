# AWS-native architecture review

**Written 2026-09-21 as a review. Region: ap-south-1 · Account: 675613597178**

> ### What is deployed, and what is not
>
> This document was written before any of it was built, and its analysis is
> left as written. Since then **Phase 1 has shipped and nothing else has.**
>
> **DEPLOYED NOW** — Amazon Bedrock (Nova Pro), AWS Lambda, Amazon SQS with a
> dead-letter queue, a Lambda event source mapping, Amazon DynamoDB, Amazon
> S3, Amazon CloudFront, Amazon API Gateway, Amazon CloudWatch (logs, EMF
> metrics and three alarms), Amazon Transcribe, AWS CDK, AWS Budgets.
>
> **EVALUATED, NOT DEPLOYED** — Amazon Textract, AWS Step Functions, AWS
> X-Ray, Amazon SNS, Amazon Translate, Amazon Polly, Amazon OpenSearch,
> Bedrock Agents, AWS WAF, Amazon Cognito. Everything this document says about
> them is a proposal or a reasoned rejection, never a claim about the running
> system. Sections 2–8 below discuss them in that spirit; the phase numbers
> there refer to work that has not been done.
>
> The alarms that are deployed **alert only** — there is no SNS topic in the
> stack, so they change state in the console and notify nobody.

The question this review answers is not *"how many AWS services can ShopFlow
use?"* It is *"at which layers is ShopFlow currently doing something by hand
that a managed service would do better, and where would a managed service add
nothing but a logo?"*

Both answers matter. A reviewer who can see why each service is there is
persuaded; a reviewer who spots one that is there for the slide is not, and
they stop trusting the rest of the architecture.

Every factual claim below about what a service supports was checked against
the live API on this account, not from memory. Where a claim could not be
checked it is marked as unverified.

---

## 1. Current AWS architecture

```
                         ┌──────────────┐
  browser ─────────────► │  CloudFront  │  single origin, SPA + /api/*
                         └──────┬───────┘
                                │
                 ┌──────────────┴───────────────┐
                 ▼                              ▼
        ┌─────────────────┐            ┌─────────────────┐
        │ S3  site bucket │            │ API Gateway     │  HTTP API
        │ private, OAC    │            │ stage throttle  │  20 rps / 40 burst
        └─────────────────┘            └────────┬────────┘
                                                │
                                       ┌────────▼────────┐
                                       │ Lambda  api     │  15s, 512MB
                                       └────┬───────┬────┘
                          Event invoke      │       │
                    ┌───────────────────────┘       └──────────┐
                    ▼                                          ▼
           ┌─────────────────┐                        ┌─────────────────┐
           │ Lambda  worker  │──► Bedrock Nova Pro    │ DynamoDB        │
           │ 60s, 1024MB     │    (Converse, 4 tools) │ single table    │
           └────────┬────────┘                        │ TTL on jobs     │
                    │                                 └─────────────────┘
                    ▼
           ┌─────────────────┐        ┌─────────────────┐
           │ S3  uploads     │───────►│ Transcribe      │  batch job
           │ price-lists/    │        │ shopflow-*      │
           │ voice-audio/    │        └─────────────────┘
           │ lifecycle 30d/1d│
           └─────────────────┘

           CloudWatch Logs (4 groups, 14-day retention)
           IAM (least privilege, per-resource ARNs)
           AWS Budgets ($25/month, email alert)
```

### Services in use today

| Service | Role | Depth |
|---|---|---|
| **Lambda** | 3 functions: api, worker, health | Full |
| **API Gateway** | HTTP API, stage-wide throttle | Full |
| **DynamoDB** | Single table, GSI, TTL on job rows, AWS-managed encryption | Full |
| **S3** | Site bucket (private + OAC), uploads bucket with two lifecycle rules | Full |
| **CloudFront** | Single origin for SPA and API, OAC, SPA error routing | Full |
| **Bedrock** | Nova Pro via Converse, bounded 6-turn loop, 4 strict tools; vision for price lists | Full |
| **Transcribe** | Batch job, IAM-scoped to `shopflow-*`, verified end to end | Full |
| **CloudWatch Logs** | 4 explicit log groups, 14-day retention | **Logs only** |
| **IAM** | Per-resource ARNs, no wildcards on any action | Full |
| **AWS Budgets** | $25/month, guarded by a deploy-time check | Full |

### Partially implemented

| Service | What exists | What is missing |
|---|---|---|
| **CloudWatch** | Log groups with retention | **No metrics, no alarms, no dashboard.** Nothing tells anyone that Bedrock is failing. |
| **Secrets Manager** | A conditional grant for the WhatsApp token | Never exercised — no secret exists, adds zero IAM by default |
| **CloudTrail** | Account-level Event history (free, 90 days) | No stack-managed trail. See §5 — this is deliberate |

### The reliability gap this review exists to close

`backend/lambdas/api/handler.py:287` invokes the worker with
`InvocationType="Event"`, and `infrastructure/shopflow_stack.py:169` sets
`retry_attempts=0`.

**If that async invoke fails, the job row stays `QUEUED` until its TTL expires
and the browser polls a job that will never complete.** There is no dead-letter
queue, no alarm and no record that anything was lost. The job simply evaporates.

This is a real defect, not a missing logo. It is the single strongest argument
in this document, and it is what §4 opens with.

---

## 2. Proposed AWS-native architecture

Additions are marked `+`. Everything unmarked is unchanged.

```
                         ┌──────────────┐
  browser ─────────────► │  CloudFront  │◄──+ WAF (rate rule, us-east-1) [P4]
                         └──────┬───────┘
                                │
                       ┌────────▼────────┐
                       │ API Gateway     │
                       └────────┬────────┘
                                │
                       ┌────────▼────────┐
                       │ Lambda  api     │──+ X-Ray tracing
                       └────┬───────┬────┘
                            │       └──────────────────┐
                 + SQS      ▼                          ▼
              ┌──────────────────┐             ┌─────────────────┐
              │ orders queue     │             │ DynamoDB        │
              │ + DLQ (redrive)  │             └─────────────────┘
              └────────┬─────────┘
                       ▼
              ┌─────────────────┐
              │ Lambda  worker  │──► Bedrock Nova Pro
              └────────┬────────┘   + EMF metrics
                       │
        + EventBridge  ▼                    + Step Functions  [P3]
       ┌────────────────────────┐      ┌──────────────────────────────┐
       │ shopflow bus           │      │ SupplierPriceIntelligence    │
       │  MarginRiskDetected    │─────►│  Textract → validate → match │
       │  SupplierPriceConfirmed│      │  → detect → margin → PAUSE   │
       │  PurchasePlanGenerated │      │  (waitForTaskToken)          │
       └───────────┬────────────┘      │  → persist → replan → notify │
                   ▼                   └──────────────────────────────┘
          + SNS  owner alerts                      │
            (publish ≠ delivery)      + Textract ──┘ AnalyzeDocument TABLES
                                                    Bedrock as fallback

          + CloudWatch metrics (EMF) · alarms · dashboard
          + X-Ray traces, correlated on the EXISTING job id
```

---

## 3. Service-by-service justification

Each entry answers the same six questions. Judging impact is rated against
the four stated criteria: **TI** Technical Innovation, **IQ** Implementation
Quality, **MI** Market Impact, **CS** Creativity & Storytelling.

---

### 3.1 Amazon SQS + dead-letter queue — **ADD, first**

| | |
|---|---|
| **Why ShopFlow needs it** | A failed async Lambda invoke currently loses an order silently, forever |
| **Workflow improved** | Order submission; supplier price-list processing |
| **Currently performed by** | `lambda.invoke(InvocationType="Event")` with `retry_attempts=0` |
| **AWS replacement** | SQS standard queue as the Lambda event source, with a DLQ and `maxReceiveCount=3` |
| **Judging** | **IQ high.** TI low, CS medium — "what happens when it fails?" is the question every serious reviewer asks |
| **Ops/security** | Encryption at rest with the SQS-managed key; queue policy restricted to the API role; visibility timeout ≥ 6× the worker timeout (360s) |
| **Cost** | **$0.** 1M requests/month free, permanently. Demo volume is ~hundreds |
| **Complexity** | **Reduces** it. Deletes the hand-rolled invoke, and Lambda's SQS event source does the polling, batching and retry |

**Why this is not artificial:** it fixes a defect that exists in the code
today. The DLQ is also *inspectable* — an owner can see the order that failed,
which the current design cannot offer at all.

**Caution:** the account's total Lambda concurrency is 10. The SQS event
source must set `maxConcurrency` (batch size 1, max concurrency ~5) or a queue
burst will starve the API function. This is a real constraint, not a footnote.

---

### 3.2 CloudWatch metrics + alarms — **ADD, first**

| | |
|---|---|
| **Why** | Zero alarms exist. Nothing reports that Bedrock is throttling, that transcription is failing, or that the DLQ is filling |
| **Workflow improved** | All of them |
| **Currently performed by** | `print()` to CloudWatch Logs, read by a human, if anyone looks |
| **AWS replacement** | Embedded Metric Format from the existing log lines, plus alarms |
| **Judging** | **IQ high, CS high.** A dashboard is the single most demo-able addition here |
| **Ops/security** | EMF writes metrics through logs already being written — **no `PutMetricData` permission needed at all**, so this adds no IAM |
| **Cost** | **~$3.50/month.** Custom metrics are $0.30 each; alarms $0.10 each. Ten metrics + five alarms. This is real money against a $25 budget and should be budgeted deliberately |
| **Complexity** | Low. EMF is a JSON shape in a log line |

**Metrics worth having** — each one answers a question someone would actually
ask:

| Metric | The question it answers |
|---|---|
| `OrdersProcessed` | Is anyone using it? |
| `ClarificationRate` | Is the matcher asking too often, or not enough? |
| `QuoteLatencyMs` | Is the Bedrock loop getting slower? |
| `BedrockInvocationErrors` | Is the model path broken? |
| `TranscriptionFailures` | Is voice broken, and for which language? |
| `ExtractionFailures` | Are price-list photos being rejected? |
| `MarginWarningsRaised` | Is the shop's margin eroding — a **business** metric, not a technical one |
| `PurchasePlansGenerated` | Is the planner being used? |
| `DlqDepth` | Has anything been lost? |
| `WhatsAppSendFailures` | Is the channel healthy? |

**Metrics deliberately NOT created:** invocation count, duration and error
rate per function — Lambda publishes those for free, and duplicating them is
exactly the noise this review is meant to avoid.

**Alarms:** DLQ depth > 0 · Bedrock errors > 3 in 5 min · API 5xx rate ·
transcription failure rate · **and a monthly-spend alarm** as a second line
behind AWS Budgets.

---

### 3.3 AWS X-Ray — **ADD, first**

| | |
|---|---|
| **Why** | A request crosses API → worker → Bedrock → DynamoDB → Transcribe with no end-to-end view |
| **Workflow improved** | Debugging, latency attribution, the demo's "why" story |
| **Currently performed by** | Correlating log lines by job id, by hand |
| **AWS replacement** | `tracing=ACTIVE` on all three functions and on the API stage |
| **Judging** | **IQ high, CS high.** A service map is a strong visual |
| **Ops/security** | Adds `xray:PutTraceSegments` to each role. Trace data must not carry order text — the SDK captures metadata, not payloads, by default, and that default must be kept |
| **Cost** | **$0** at demo scale. 100,000 traces/month free, then $5/million |
| **Complexity** | Very low — a CDK flag plus the X-Ray SDK for subsegments |

**The correlation id already exists.** The 32-hex `jobId` flows from the API
through DynamoDB, the worker, the Transcribe job name (`shopflow-<jobId>`) and
the S3 key. Annotating traces with it costs one line and makes the whole chain
searchable. That is the kind of detail that reads as *designed* rather than
*assembled*.

---

### 3.4 Amazon EventBridge — **ADD, narrowly, second**

| | |
|---|---|
| **Why** | Notification is currently entangled with calculation. The planner would have to know about email to send an alert |
| **Workflow improved** | Margin protection, supplier price confirmation, purchase planning |
| **Currently performed by** | Nothing — there are no notifications |
| **AWS replacement** | One custom bus with rules to SNS, and later to Step Functions |
| **Judging** | **TI medium, IQ medium, CS high.** Event-driven architecture is a strong narrative *if the events are real* |
| **Ops/security** | Bus policy limited to this account; no cross-account; no archive needed |
| **Cost** | $1.00 per million custom events → **~$0** |
| **Complexity** | Medium. This is where "events for the sake of events" becomes a genuine risk |

**Only these events, because only these have a real consumer:**

| Event | Producer | Consumer | Business purpose |
|---|---|---|---|
| `MarginRiskDetected` | worker, after a confirmed price rise | SNS → owner | The owner needs to know before they sell at a loss |
| `SupplierPriceConfirmed` | api, on a price decision | Step Functions (P3), SNS | Triggers the replan |
| `PurchasePlanGenerated` | api | SNS | "Your plan is ready" |

**Events from the brief that I recommend NOT creating:** `OrderSubmitted` and
`CreditCheckCompleted` have no consumer — SQS already carries the order, and a
credit check is a synchronous read that writes nothing.
`VoiceTranscriptionCompleted` and `WhatsAppMessageRequested` are already
handled by the polling contract and a button press respectively. Creating them
would be five events with no subscribers, which is worse than none: it implies
an architecture that is not there.

---

### 3.5 Amazon SNS — **ADD, second**

| | |
|---|---|
| **Why** | The owner has no way to learn about a margin risk unless they are looking at the page |
| **Workflow improved** | Margin protection, purchase planning, failure reporting |
| **Currently performed by** | Nothing |
| **AWS replacement** | One topic, email subscription, fed by EventBridge rules |
| **Judging** | **MI high** — a shop owner is not at a screen. **IQ medium** |
| **Ops/security** | Subscription confirmation is manual and must be. The email address stays in CDK context, never in source — the existing `SHOPFLOW_ALERT_EMAIL` pattern already does this correctly |
| **Cost** | 1M publishes and 1,000 email notifications free/month → **~$0** |
| **Complexity** | Low |

**Honesty constraint, carried from the WhatsApp work:** `sns:Publish` returns
a message id. It does **not** confirm delivery. The UI must say "alert sent to
the shop's email" and never "the owner has been notified."

---

### 3.6 Amazon Textract — **ADD, third, as a first pass with Bedrock behind it**

| | |
|---|---|
| **Why** | A supplier price list is a **table**. Textract extracts table structure natively; a vision LLM reconstructs it and can hallucinate a row |
| **Workflow improved** | Supplier price-list ingestion |
| **Currently performed by** | `backend/agent/vision.py` — Nova Pro reading a photo, with strict JSON validation |
| **AWS replacement** | `AnalyzeDocument` with the `TABLES` feature as the extractor; Nova Pro stays as fallback and as the interpreter of ambiguous cells |
| **Judging** | **TI high, IQ high, CS high.** "We use the right tool at each layer, and we can show you which one read this document" is a genuinely strong story |
| **Ops/security** | `textract:AnalyzeDocument` scoped to the API role; the S3 grant already exists and stays scoped to `price-lists/*` |
| **Cost** | `AnalyzeDocument` TABLES is **$15 per 1,000 pages**; `DetectDocumentText` is $1.50 per 1,000. A demo is well under 100 pages → **< $1.50**. Free tier covers 1,000 `DetectDocumentText` pages/month for the first 3 months |
| **Complexity** | Medium — a second extraction path and a comparison |

**Verified:** Textract is available in ap-south-1 with `AnalyzeDocument`,
`AnalyzeExpense` and `DetectDocumentText`.

**The deterministic guarantee is unchanged.** Textract produces cells;
`engine.supplier_prices` still decides what a line means, which SKU it matches,
whether the price changed and whether that matters. Swapping the extractor does
not move one gram of business logic.

**Risk to manage:** Textract is weaker on handwriting and on the skewed phone
photos that a Madurai shop will actually produce. The correct design is
**Textract first, Bedrock fallback, and the response says which one read the
document** — never a silent switch.

---

### 3.7 AWS Step Functions — **ADD, last, for one workflow only**

| | |
|---|---|
| **Why** | Supplier price intelligence is genuinely multi-stage and contains a **human decision in the middle** |
| **Workflow improved** | Upload → extract → validate → match → detect → margin → *owner confirms* → persist → replan → notify |
| **Currently performed by** | A Lambda worker, DynamoDB decision rows, and the owner pressing a button; each stage works, but the workflow is implicit |
| **AWS replacement** | A Standard workflow using `waitForTaskToken` for the owner's confirmation |
| **Judging** | **TI high, IQ high, CS high.** A human-in-the-loop callback is the textbook Step Functions case and this is a real instance of it, not a contrived one |
| **Ops/security** | The state machine role needs `lambda:InvokeFunction` on three specific functions and `textract:*` on nothing — Lambda keeps the service calls |
| **Cost** | Standard workflows are $0.025 per 1,000 state transitions. 100 demo runs × ~10 states = **$0.03** |
| **Complexity** | **High.** This is the riskiest item here |

**Why last:** the confirmation flow is currently correct and tested. Moving it
into a state machine touches the one path that writes durable confirmed costs —
the path that produces ₹24,993.16 and the ₹803.40 impact figure. It must be
done additively, behind the existing API contract, with the canonical figures
re-verified before and after.

**Explicitly NOT moved to Step Functions:** the voice order path. It is
audio → Transcribe → existing workflow, and the browser already polls
`GET /api/jobs/{id}`. Wrapping two steps in a state machine adds a service, a
role and a poll without improving anything. The brief warned against exactly
this, and it is right.

---

### 3.8 Amazon Polly — **DO NOT ADD**

**Verified on this account, ap-south-1:** Polly offers **three Indian-locale
voices, all `en-IN`** — Aditi (standard), Kajal (neural), Raveena (standard).
There is **no Tamil voice.** There is no `ta-IN`, `te-IN`, `kn-IN`, `ml-IN`,
`bn-IN` or `mr-IN` voice in the catalogue at all.

ShopFlow is a product whose primary shop speaks Tamil and which claims support
across 22 Scheduled Languages. Adding Polly would give **English-Indian speech
only** — and would be a *downgrade* for Tamil, because the current browser
`speechSynthesis` path can at least use a device-installed Tamil voice where
one exists, and says so honestly when it cannot.

Adding a speech service that cannot speak the product's own language, in a
release whose headline is multilingual support, would be the exact failure mode
this review is meant to prevent.

**Reconsider if:** Polly adds Indic neural voices. The check is one
`describe_voices` call and belongs in the multilingual smoke test.

---

### 3.9 Amazon Translate — **DO NOT ADD at runtime**

**Verified on this account:** Translate supports **10 of the 22** Scheduled
Languages — Bengali, Gujarati, Hindi, Kannada, Malayalam, Marathi, Punjabi,
Tamil, Telugu, Urdu. It does **not** support Assamese, Bodo, Dogri, Kashmiri,
Konkani, Maithili, Manipuri, Nepali, Odia, Sanskrit, Santali or Sindhi.

Three reasons this is a clear no, in order of weight:

1. **It cannot fix the gap it would be added to fix.** The three partially
   translated languages — Bodo, Manipuri, Santali — are precisely the three
   Translate does not support.
2. **It would break guarantees that are currently tested.** The resource files
   are deterministic, cached, and asserted to preserve every placeholder and
   every rupee figure. Machine translation at runtime is non-deterministic
   across model versions, so a quotation's wording could differ between two
   identical requests, and `{total}` could be dropped or reordered. The
   placeholder-rejection rule exists because that failure sends a customer a
   quotation with no amount in it.
3. **It would put customer business text on the wire** for no gain — a
   quotation contains a customer name, items and a total.

**The legitimate narrow use, if any:** a **build-time** tool to draft
translations for human review, for the 10 supported languages. That does not
add a runtime dependency and does not touch the tested guarantees. Even then
the output needs review before shipping, which is the same constraint the
current resources already carry.

---

### 3.10 Amazon OpenSearch Serverless — **DO NOT ADD**

| | |
|---|---|
| **Cost** | Minimum billed capacity is **~1 OCU** at ~$0.24/OCU-hour ≈ **$175/month, always on.** The budget is **$25/month** |
| **Fit** | The catalogue is **147 SKUs**. The deterministic matcher answers in microseconds from memory |
| **Correctness** | Exact, constrained matching is *authoritative by design*. Vector similarity returns a nearest neighbour — which is precisely the "confidently wrong" behaviour the product exists to refuse |

This is the clearest no in the document. It would be an always-on cluster that
costs seven times the entire budget, to make a 147-row lookup worse at the one
thing the product is built not to do.

**The irony worth stating in the writeup:** the §3.11 defect found last turn —
`20` matching `ACC-CONDUIT-20` — is exactly what fuzzy retrieval does by
default. ShopFlow's answer was to *ask a question*. That is the architectural
position, and vector search contradicts it.

---

### 3.11 Amazon Bedrock Agents / AgentCore — **DO NOT ADD**

The existing orchestrator is a bounded Converse loop: **6 turns maximum, 3 tool
errors maximum, 4 strict tools, temperature 0**, every tool a deterministic
engine call, and a terminal tool required to finish. The loop cannot return
prose as an outcome.

Bedrock Agents would move that orchestration into a managed service and give
back *less* control over turn count, tool-error handling and termination. The
product thesis is "the model never writes a business number," and the current
loop is the mechanism that enforces it.

Moving to Agents would be a lateral change that weakens the guarantee. **This
is a differentiator worth saying out loud in the submission**, not an omission
to apologise for: *we evaluated Bedrock Agents and chose a bounded tool loop,
because we need a hard ceiling on what the model is allowed to decide.*

---

### 3.12 AWS WAF — **DEFER (P4), and note a technical constraint**

**WAF cannot be attached to an API Gateway HTTP API.** WAFv2 supports
CloudFront, ALB, API Gateway **REST** stages, AppSync, Cognito and App Runner.
Since the API is served through CloudFront, the only attachment point is the
distribution — which requires a web ACL in **us-east-1** with `CLOUDFRONT`
scope, i.e. a second region in the stack.

| | |
|---|---|
| **Value** | A rate-based rule on a public no-login endpoint is genuinely useful |
| **Already mitigated by** | Stage-wide throttling at 20 rps / 40 burst, request-size limits, and strict input validation on every route |
| **Cost** | **~$6–7/month** — $5.00 per web ACL + $1.00 per rule + $0.60/million requests. That is a quarter of the budget |
| **Verdict** | The marginal protection over existing throttling does not justify a quarter of the budget plus a cross-region construct. Revisit if the demo is publicised |

---

### 3.13 AWS KMS customer-managed keys — **DO NOT ADD**

DynamoDB uses `AWS_MANAGED` encryption; both S3 buckets use `S3_MANAGED`. Data
is already encrypted at rest with AWS-managed keys.

A CMK adds $1/month per key plus per-request charges, plus key-policy
management, plus a new way to lock yourself out of your own data. It buys key
rotation control and CloudTrail visibility of key usage — **neither of which
any requirement here asks for.** There is no compliance driver, no
cross-account sharing and no customer-managed key requirement.

**Add only if** a real requirement appears. "Encryption at rest" is already
true and can be said honestly today.

---

### 3.14 AWS CloudTrail — **DO NOT create a stack-managed trail**

CloudTrail **management events are already recorded** in Event history for the
last 90 days at no charge, on every account including this one. That is the
auditable control-plane trail the brief asks for, and it exists.

Creating a trail delivering to S3 adds storage cost and duplicates the first
free copy. Data events (S3 object-level, Lambda invoke-level) are billed at
$0.10 per 100,000 events and would be the only genuine addition — and there is
no audit requirement here that needs them.

**Recommendation:** document that the trail exists, show Event history in the
evidence pack, and do not pay for a second copy.

---

### 3.15 Secrets Manager — **KEEP as-is**

Already correct. The grant is conditional, so a default deploy adds **zero**
IAM statements, and `WHATSAPP_TOKEN_SECRET_ARN` is the documented preferred
path. No change recommended.

---

## 4. Summary: add, and do not add

### Add — in this order

| # | Service | Why, in one line | Cost/mo | Risk |
|---|---|---|---|---|
| 1 | **SQS + DLQ** | Fixes a defect: failed orders vanish today | $0 | Low |
| 2 | **CloudWatch metrics + alarms** | Nothing currently reports failure | ~$3.50 | Low |
| 3 | **X-Ray** | One trace across API → Bedrock → DynamoDB | $0 | Very low |
| 4 | **EventBridge** (3 events) | Decouples alerting from calculation | ~$0 | Medium |
| 5 | **SNS** | The owner is not sitting at a screen | ~$0 | Low |
| 6 | **Textract** | A price list is a table; read it as one | <$1.50 | Medium |
| 7 | **Step Functions** (1 workflow) | Human-in-the-loop, genuinely | ~$0 | **High** |

**Total added run cost: ~$5/month**, against a $25 budget. The only line that
costs real money is CloudWatch custom metrics, and it is the one that most
improves Implementation Quality.

### Do not add — with the reason, not an apology

| Service | Reason |
|---|---|
| **OpenSearch Serverless** | ~$175/month always-on, for 147 SKUs, to make matching *less* exact |
| **Bedrock Agents** | Weakens the bounded-loop guarantee that is the product's core claim |
| **Amazon Translate** | Supports 10 of 22, **excludes all three partial languages**, and breaks tested determinism |
| **Amazon Polly** | **No Tamil voice exists.** Would be a downgrade in the product's own language |
| **AWS WAF** | Cannot attach to an HTTP API; ~$7/month for marginal gain over existing throttling |
| **KMS CMK** | Encryption at rest is already true; adds cost and lockout risk for no requirement |
| **CloudTrail trail** | The free management-event trail already exists |
| **Aurora / RDS** | No relational requirement. DynamoDB access patterns are single-key |
| **Cognito** | No-login is a deliberate demo property |
| **ECS / EC2 / NAT** | Nothing here needs a VPC. Adding one adds a NAT Gateway at ~$32/month |
| **Provisioned concurrency** | The account ceiling is 10 concurrent executions total |

---

## 5. Migration plan

Four phases. Each ends with the full suite green, all four canonical figures
re-verified, a CDK diff inspected, and the budget confirmed present.

**The four canonical figures are the gate at every phase:**
₹22,306.48 · ₹24,996.56 · ₹24,993.16 · ₹803.40

### Phase 1 — Reliability and observability *(low risk, highest value)*
1. SQS queue + DLQ; worker moves to an SQS event source with `maxConcurrency`
2. API publishes to the queue instead of `lambda.invoke`
3. EMF metrics from existing log lines; 10 metrics, no new IAM
4. 5 alarms → SNS
5. X-Ray on all three functions and the API stage, annotated with `jobId`
6. CloudWatch dashboard

*No business engine touched. No API contract changed. The browser polls the
same route.*

### Phase 2 — Events and notification *(medium risk)*
7. One custom EventBridge bus; the three events with real consumers
8. SNS topic + email subscription via CDK context
9. Rules: `MarginRiskDetected` → SNS, `PurchasePlanGenerated` → SNS

*Additive. Nothing that exists begins depending on an event.*

### Phase 3 — Document intelligence *(medium risk)*
10. Textract `AnalyzeDocument` TABLES as the first-pass extractor
11. Bedrock vision retained as fallback; the response names which read it
12. `engine.supplier_prices` validation unchanged and re-tested against both

### Phase 4 — Orchestration *(high risk, do last or not at all)*
13. Step Functions Standard for supplier price intelligence,
    `waitForTaskToken` for the owner's confirmation
14. The existing API contract preserved as the façade
15. Canonical figures re-verified before and after, by smoke test

**Phase 4 is the one to drop if time runs short.** Phases 1–3 already tell a
complete AWS-native story; Phase 4 makes it a better one and risks the figure
the whole demo turns on.

---

## 6. Risks

| Risk | Likelihood | Mitigation |
|---|---|---|
| **SQS event source starves the API function** — account concurrency is 10 | **High if unmanaged** | `maxConcurrency=5`, batch size 1; verify by load test before the demo |
| **Step Functions changes the confirmed-cost path** and moves ₹24,993.16 | Medium | Phase 4, additive, behind the existing contract, smoke-verified both sides |
| **Textract reads a skewed phone photo worse than Nova Pro** | Medium | Keep Bedrock as fallback; report which extractor ran; never switch silently |
| **Custom metrics quietly cost more than expected** | Low | 10 metrics is a hard cap; a spend alarm behind the Budget |
| **EventBridge events accumulate without consumers** | Medium | Three events, each with a named consumer; delete any that loses one |
| **CDK diff removes the Budget** | Low — but this has happened before on this project | The existing deploy-time budget check stays, and runs every phase |
| **More services obscure rather than demonstrate** | Medium | Every service must be explainable in one line in the README; if it cannot be, it goes |

---

## 7. Cost considerations

Approximate, ap-south-1, at demo scale. Free-tier-dependent lines are marked.

| Service | Monthly | Note |
|---|---:|---|
| SQS | $0.00 | 1M requests free permanently |
| SNS | $0.00 | 1M publishes, 1,000 emails free |
| EventBridge | ~$0.00 | $1.00/million custom events |
| Step Functions | ~$0.03 | $0.025/1,000 state transitions |
| X-Ray | $0.00 | 100,000 traces/month free |
| CloudWatch metrics | **$3.00** | 10 × $0.30 — the real cost |
| CloudWatch alarms | **$0.50** | 5 × $0.10 |
| Textract | <$1.50 | $15/1,000 pages TABLES; free tier for 3 months |
| **Total added** | **~$5/month** | Against a $25 budget |

Rejected on cost: OpenSearch Serverless ~$175 · WAF ~$7 · NAT Gateway ~$32 ·
KMS CMK ~$1–2 plus complexity.

**The Budget itself is not modified by any phase**, and the existing
`scripts/deploy.sh` check that refuses a diff removing
`AWS::Budgets::Budget` stays in place and runs on every one.

---

## 8. Hackathon judging impact

| Criterion | What moves it | Phase |
|---|---|---|
| **Technical Innovation & Originality** | Textract + Bedrock chosen per layer with a stated reason; Step Functions human-in-the-loop callback; **the documented decision to reject Bedrock Agents** | 3, 4 |
| **Implementation Quality** | DLQ, alarms, X-Ray, least privilege, budget guard, the test suite | 1 |
| **Community / Market Impact** | SNS alerts reach a shop owner who is not at a screen; the multilingual layer already shipped | 2 |
| **Creativity & Storytelling** | A service map, a dashboard, and a one-line reason for every service on it | 1, 3 |

**The strongest line available after Phase 1–3 is not a count.** It is:

> ShopFlow uses AWS managed services at every appropriate layer — AI, speech,
> document processing, event orchestration, storage, compute, security,
> messaging and observability — and rejects four of them on the record, with
> reasons.

The rejections are an asset. Being able to say *"we did not add OpenSearch
because vector search returns a nearest neighbour, and this product exists to
ask a question instead of guessing"* demonstrates judgement in a way that
adding it never could.

---

## 9. Exact implementation order

Each step is independently shippable and independently revertible.

| Step | Change | Files | Tests |
|---|---|---|---|
| 1 | SQS queue + DLQ in CDK | `shopflow_stack.py` | CDK diff, budget check |
| 2 | API enqueues; worker reads from SQS | `api/handler.py`, `worker/handler.py` | `test_api.py` + new `test_queue.py` |
| 3 | `maxConcurrency` tuned to the account ceiling | `shopflow_stack.py` | load check before demo |
| 4 | EMF metric emitter module | new `backend/engine/telemetry.py` | new `test_telemetry.py` |
| 5 | Metric calls at 10 named points | the 3 handlers | assert no business value is ever emitted |
| 6 | 5 alarms + SNS alarm topic | `shopflow_stack.py` | CDK diff |
| 7 | X-Ray tracing + `jobId` annotation | `shopflow_stack.py`, handlers | structural test: no order text in a trace |
| 8 | CloudWatch dashboard | `shopflow_stack.py` | visual |
| 9 | EventBridge bus + 3 events | `shopflow_stack.py`, worker, api | new `test_events.py` |
| 10 | SNS owner topic + rules | `shopflow_stack.py` | assert publish ≠ delivery claim |
| 11 | Textract extractor | new `backend/agent/textract_reader.py` | new `test_textract.py` |
| 12 | Extractor selection + fallback + provenance | `worker/handler.py` | both paths, same validation |
| 13 | Step Functions state machine | `shopflow_stack.py`, new task handlers | new `test_workflow.py` |
| 14 | Callback token for owner confirmation | `api/handler.py` | canonical figures re-verified |

**Invariant for every step:** the nine deterministic engines — `budget`,
`pricing`, `quote`, `margin`, `credit`, `uom`, `purchasing`, `matching`,
`cost_records` — are not modified. If a step appears to require it, the step
stops and the reason is written down first.

---

## 10. Recommendation

**Do Phases 1–3. Treat Phase 4 as optional.**

Phase 1 alone fixes a real defect and adds the operational surface the
architecture is currently missing, for about $3.50 a month. Phase 2 makes the
product useful to someone who is not looking at it. Phase 3 is the one that
genuinely improves an AI capability rather than decorating it.

Phase 4 is excellent if there is time and dangerous if there is not, because it
touches the path that produces the figure the entire demo rests on.

**Nothing in this document has been implemented. Awaiting a decision on scope
before any code is written.**
