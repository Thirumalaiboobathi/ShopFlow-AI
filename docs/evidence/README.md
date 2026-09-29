# Evidence

| What | Where | Status |
|---|---|---|
| Live checks against the deployed app, reproducible from `scripts/live/` | [live-checks.md](live-checks.md) | run 2026-09-29; results recorded |
| Commercial intelligence (money at risk, buy now vs wait, supplier quotes, reliability) | [commercial-intelligence.md](commercial-intelligence.md) | local run 2026-09-29, 22/22; **not deployed** |
| WhatsApp | [whatsapp-status.md](whatsapp-status.md) | implemented, **not** live-verified with Meta |
| Product screenshots | [screenshots/README.md](screenshots/README.md) | list only — **none captured yet** |
| AWS console / coding-agent screenshots | this file, below | **none captured yet** |
| Agent AWS log evidence | [agent-aws-evidence.md](agent-aws-evidence.md) | captured 2026-09-27 |
| Shop-owner validation sessions | [../validation/README.md](../validation/README.md) | template only — no session recorded |

---

# Evidence pack — capture checklist

Screenshots required for the AWS "Zero to Shipped" submission, in particular
the ship-gate requirement for **documented proof of coding-agent connection to
the AWS console**.

> **Log evidence exists:** [`agent-aws-evidence.md`](agent-aws-evidence.md),
> captured by the agent from the AWS CLI and git on 2026-09-27.
>
> **Status of the screenshots below: NOTHING HAS BEEN CAPTURED YET.**
>
> This directory currently contains only this checklist. Every item below is
> outstanding and must be captured manually by a human from the AWS console.
> No screenshot in this list should be described as existing until the file is
> actually in this folder.

**Save captures into this directory** (`docs/evidence/`) using the suggested
filenames, so the numbering matches this checklist.

---

## Before you start

| | |
|---|---|
| Account | `675613597178` |
| Region | `ap-south-1` (Asia Pacific — Mumbai) |
| Stack | `ShopFlowStack` |
| Live URL | https://d3m3lwn03zb2eu.cloudfront.net |

**Four rules:**

1. **Do not fabricate anything.** If a screen does not show what this checklist
   expects, capture what is actually there and note the discrepancy. Wrong
   evidence is worse than missing evidence.
2. **Redact nothing that matters, hide what does not.** The account id is fine
   to show. Blur any unrelated project names, billing detail beyond the ShopFlow
   budget, and personal browser bookmarks or tabs.
3. **Show the region selector** in console captures where it is visible — it
   proves `ap-south-1` rather than leaving it assumed.
4. **Capture in one session** where possible, so timestamps corroborate each
   other.

---

## 1. Coding-agent connection to AWS

**Open:** your terminal running Claude Code, with a command and its real AWS
output visible.

**Must be visible:**
- The agent session (Claude Code prompt/interface)
- A command such as `aws sts get-caller-identity` **and its output**, showing
  account `675613597178` and the `AI-agent` IAM user ARN
- Ideally an agent-driven AWS action in the same frame, e.g. `cdk diff` output
  or `aws cloudformation describe-stacks`

**Why it matters:** this is the mandatory ship-gate item — direct proof the
coding agent was connected to and operating against AWS, not generating code
for a human to run elsewhere.

**Filename:** `01-agent-aws-connection.png`

---

## 2. AWS account and region

**Open:** AWS Console → any service → top-right account menu expanded.

**Must be visible:** account id `675613597178`, and the region selector showing
**Asia Pacific (Mumbai) ap-south-1**.

**Why it matters:** anchors every other screenshot to one account and region.

**Filename:** `02-account-region.png`

---

## 3. CloudFormation stack — UPDATE_COMPLETE

**Open:** CloudFormation → Stacks → `ShopFlowStack`.

**Must be visible:**
- Status **`UPDATE_COMPLETE`**
- Stack name and most-recent updated timestamp

**Also worth capturing** (second file, `03b-cfn-outputs.png`): the **Outputs**
tab, which lists `SiteUrl`, `ApiEndpoint`, `TableName`, `BedrockModelId`,
`DistributionId`, `UploadsBucket`, `WorkerFunctionName`. This single screen ties
the whole architecture together.

**Why it matters:** proves infrastructure-as-code, deployed and healthy.

**Filename:** `03-cloudformation-update-complete.png`

---

## 4. CloudFront distribution

**Open:** CloudFront → Distributions → the distribution with domain
`d3m3lwn03zb2eu.cloudfront.net` (id `EKAOMJLJWM7VZ`).

**Must be visible:** distribution id, domain name, status **Enabled/Deployed**.

**Also worth capturing:** the **Origins** tab showing both origins (S3 with
Origin Access Control, and the API Gateway origin) — this is the single-origin
routing design.

**Why it matters:** shows the public entry point and that S3 is reached through
OAC rather than being public.

**Filename:** `04-cloudfront-distribution.png`

---

## 5. Live public URL in a browser

**Open:** https://d3m3lwn03zb2eu.cloudfront.net in a **fresh incognito window**
(no cached session, no logged-in state).

**Must be visible:** the URL bar with the HTTPS padlock, the ShopFlow landing
page, and the **Category: Commercial Potential** / **Lane: Startup** chips.

**Why it matters:** proves the app is publicly reachable with no login — the
core ship-gate requirement, and what both AI scoring and human judges will hit.

**Filename:** `05-live-url-browser.png`

---

## 6. API Gateway

**Open:** API Gateway → APIs → the ShopFlow HTTP API → **Routes**.

**Must be visible:** all eight routes —
`GET /api/health`, `POST /api/orders`, `POST /api/supplier-price-lists`,
`POST /api/price-decisions`, `POST /api/purchase-plans`,
`POST /api/shop-queries`, `GET /api/customers`,
`GET /api/customers/{customerId}`, `POST /api/credit/check`,
`POST /api/voice/transcribe`, `POST /api/whatsapp/send`,
`GET /api/languages`, `GET /api/jobs/{jobId}`, `GET /api/demo`.

**Also worth capturing** (`06b-apigw-throttle.png`): Stages → `$default` →
throttling showing **20 rps / 40 burst**.

**Why it matters:** shows the real API surface and that rate limiting is in
place on a public endpoint.

**Filename:** `06-apigateway-routes.png`

---

## 7. DynamoDB table

**Open:** DynamoDB → Tables → `shopflow-demo`.

**Must be visible:** table name, partition key `PK`, sort key `SK`, the `GSI1`
index, and on-demand billing.

**Why it matters:** shows the single-table design backing jobs, decisions and
confirmed costs.

**Filename:** `07-dynamodb-table.png`

---

## 8. A confirmed supplier cost item

**Open:** DynamoDB → Tables → `shopflow-demo` → **Explore items**, filtered to
`PK = SHOP#demo`.

> **This item only exists after a price has been confirmed in the live app.**
> If the table shows nothing, run steps A–E of
> [`docs/DEMO-RUNBOOK.md`](../DEMO-RUNBOOK.md) first, capture this screenshot,
> and then reset with `python scripts/reset_confirmed_costs.py --confirm`.

**Must be visible:** an item with
`PK = SHOP#demo`, `SK = COST#W-FIN-1.5-RED-90M`,
`recordType = CONFIRMED_SUPPLIER_COST`, `confirmedCost = 6300`,
`currency = INR`, `supplierId = SUP-BALAJI`, `effectiveDate = 15-09-2026`,
plus `sourceJobId` and `confirmedAt`.

**Why it matters:** this is the human-confirmation boundary made concrete —
durable state that exists *only* because an owner explicitly approved it, with
no TTL and with the source document recorded.

**Filename:** `08-dynamodb-confirmed-cost-item.png`

---

## 9. Lambda functions

**Open:** Lambda → Functions, filtered to `shopflow`.

**Must be visible:** `shopflow-api`, `shopflow-order-worker`, `shopflow-health`,
with runtime **Python 3.13**.

**Also worth capturing:** `shopflow-order-worker` configuration showing
**1024 MB / 60 s**, and `shopflow-api` showing **512 MB / 15 s**.

**Why it matters:** shows the compute layer and that sizing was a decision.

**Filename:** `09-lambda-functions.png`

---

## 10. Lambda execution role

**Open:** Lambda → `shopflow-order-worker` → Configuration → Permissions →
click the execution role to open IAM.

**Must be visible:** the role name and its attached inline policy.

**Why it matters:** entry point for the two least-privilege screenshots below.

**Filename:** `10-lambda-iam-role.png`

---

## 11. Worker Bedrock permission — scoped to one model

**Open:** IAM → the worker role → the inline policy → **JSON** view, scrolled to
the `bedrock:InvokeModel` statement.

**Must be visible:**

```json
{
  "Action": "bedrock:InvokeModel",
  "Resource": [
    "arn:aws:bedrock:*::foundation-model/amazon.nova-pro-v1:0",
    "arn:aws:bedrock:ap-south-1:675613597178:inference-profile/apac.amazon.nova-pro-v1:0"
  ],
  "Effect": "Allow"
}
```

**Why it matters:** least privilege on the most sensitive permission in the
system — the worker can invoke exactly one model, not Bedrock generally.

**Filename:** `11-worker-bedrock-scoped.png`

---

## 12. API role has NO Bedrock permission

**Open:** IAM → the `shopflow-api` execution role → inline policy → **JSON**
view, showing the **whole** document.

**Must be visible:** the complete policy, with DynamoDB and S3 statements and
**no `bedrock:` action anywhere**. Capture the entire JSON so the absence is
verifiable rather than asserted.

**Why it matters:** this is the strongest single piece of evidence for the core
claim. The language model cannot invent a business number on the purchase-
planning path because **there is no model reachable from it** — enforced by IAM,
not by a prompt.

**Filename:** `12-api-role-no-bedrock.png`

---

## 13. S3 buckets private

**Open:** S3 → `shopflow-site-675613597178` → **Permissions**.

**Must be visible:** **Block all public access: On**, and the bucket policy
granting access to the CloudFront **Origin Access Control** service principal
only.

**Repeat for** `shopflow-uploads-675613597178` (`13b-s3-uploads-private.png`) —
this one should have **no** public access and no CloudFront path at all; it is
reached only by the worker's IAM role.

**Why it matters:** shows customer-uploaded documents are never publicly
reachable and no credential or presigned URL is exposed to the browser.

**Filename:** `13-s3-site-private-oac.png`

---

## 14. CloudWatch logs and retention

**Open:** CloudWatch → Log groups, filtered to `shopflow`.

**Must be visible:** four groups — `/aws/lambda/shopflow-api-fn`,
`/aws/lambda/shopflow-order-worker`, `/aws/lambda/shopflow-health`,
`/aws/apigateway/shopflow-api` — each with retention **2 weeks**.

**Also capture** (`14b-cloudwatch-worker-log-line.png`): open
`/aws/lambda/shopflow-order-worker`, find a `price_list_processed` line, and
show that it contains model id, latency, token counts and result counts — and
**no** supplier names, product descriptions or prices.

**Why it matters:** observability is real, retention is bounded, and logs are
deliberately contents-free.

**Filename:** `14-cloudwatch-log-groups.png`

---

## 15. AWS Budget — $25/month

**Open:** Billing and Cost Management → Budgets → `shopflow-monthly`.

**Must be visible:** budget name, **$25.00 monthly** limit, the alert thresholds
(50% / 80% / 100%), and current month-to-date spend.

> Blur unrelated budgets belonging to other projects in this account.

**Why it matters:** cost control was in place before a public endpoint that can
call Bedrock existed. This is also the resource a conditional CDK deploy would
have silently destroyed — now guarded by `scripts/deploy.sh`.

**Filename:** `15-aws-budget.png`

---

## 8a1. Units on a quotation

**Open:** the live site, run the canonical order, look at the wire line.

**Must be visible:** **3 COIL**, the **270 METER equivalent** beside it, and
the unit price shown **per coil**.

**Why it matters:** the requested unit is preserved rather than replaced. The
equivalence is shown for information; the order is still three coils and the
price is still per coil.

**Filename:** `8a1-uom-quote-line.png`

---

## 8a2. A unit that will not be converted

**Open:** submit *"90 metres Finolex 1.5 sq mm red wire."*

**Must be visible:** the clarification, stating **1 COIL = 90 METER** and
asking how many coils — and **no quotation**.

**Why it matters:** the strongest unit claim. Ninety metres is exactly one
coil, and ShopFlow still refuses to decide that for the owner. Capture this
one; it is the difference between a unit field and unit handling.

**Filename:** `8a2-uom-no-silent-conversion.png`

---

## 8a3. Khata — credit approved

**Open:** choose **Bala Contractors** in **Put this on**, run the canonical
order.

**Must be visible:** the **Khata / Credit** panel showing outstanding, limit,
this order, projected outstanding, remaining credit, and **✓ Credit
approved** — plus the line stating ShopFlow does not perform credit scoring
and that demo records are synthetic.

**Why it matters:** shows the whole decision, with its inputs, on the screen
that uses it.

**Filename:** `8a3-khata-approved.png`

---

## 8a4. Khata — limit exceeded, quotation intact

**Open:** switch to **Ravi Electrical Works** and run the same order.

**Must be visible:** **⚠ Credit limit exceeded**, the negative remaining
credit, *"This quotation stands"* — and the **₹22,306.48 quotation still on
screen above it**, unchanged.

**Why it matters:** the safety claim. A credit result never cancels, reprices
or hides a quotation. Both must be in the same frame, or the screenshot does
not prove it.

**Filename:** `8a4-khata-exceeded.png`

---

## 8a5. Credit is not stored

**Open:** DynamoDB → `shopflow-demo` → Explore items, after running several
credit checks.

**Must be visible:** the item list with **no** `CUSTOMER#` partition key and
no new rows from the credit checks.

**Why it matters:** a credit enquiry records nothing. Customers are seed data
shipped in the bundle, not stored state, and no real customer data exists
anywhere.

**Filename:** `8a5-no-credit-rows.png`

---

## 7b. Amazon Transcribe — a real job

**Open:** Amazon Transcribe → Transcription jobs, immediately after speaking
on the live site (or while `scripts/smoke_test_voice_whatsapp.py` runs).

**Must be visible:** a job named `shopflow-<32 hex>` with its status.

**Why it matters:** proves the transcription is an AWS service call, not
browser recognition relabelled. The `shopflow-` prefix is also what makes the
IAM policy scopeable to this application's jobs.

**Filename:** `7b-transcribe-job.png`

---

## 7c. Voice audio does not persist

**Open:** S3 → `shopflow-uploads-<account>` → the `voice-audio/` prefix, a
minute after a transcription has completed.

**Must be visible:** the prefix **empty**, and the bucket's lifecycle rules
showing `expire-voice-audio` at 1 day.

**Why it matters:** recordings of a customer's voice are deleted as soon as
the transcript is read; the lifecycle rule is only a backstop. Capture both
the empty listing and the rule.

**Filenames:** `7c-voice-audio-empty.png`, `7d-voice-lifecycle-rule.png`

---

## 7e. Transcribe IAM — scoped, not wildcard

**Open:** IAM → the `shopflow-api` function's role → the inline policy.

**Must be visible:** `transcribe:StartTranscriptionJob`,
`GetTranscriptionJob`, `DeleteTranscriptionJob` on
`transcription-job/shopflow-*`, and `s3:GetObject`/`s3:DeleteObject` scoped to
`voice-audio/*`.

**Why it matters:** no `transcribe:*` and no `s3:*`. The audio grant does not
reach `price-lists/`, so the API still cannot read a supplier document.

**Filename:** `7e-transcribe-iam.png`

---

## 8b. Margin protection panel

**Open:** the live site, run the price list, confirm the wire increase
(runbook Step E).

**Must be visible:** the **Margin protection** panel under the confirmed line,
showing selling price ₹6,608.00, previous supplier cost ₹5,900.00, confirmed
supplier cost ₹6,300.00, previous margin ₹708.00, current margin ₹308.00 and
the ₹400.00 reduction.

**Why it matters:** this is the PROTECT job, and the strongest commercial claim
in the product — a 6.78% cost rise measured as a 57% loss of margin. It is the
link between the supplier price list and the purchasing plan.

**Filename:** `8b-margin-protection.png`

---

## 8c. The suggestion, and the human boundary

**Open:** click **Review price** on the same panel.

**Must be visible:** the suggested selling price (₹7,055.66), *"Review and
confirm before applying any price change"*, and *"ShopFlow does not
automatically change your selling price."*

**Why it matters:** proves the recommendation is a recommendation. There is no
control anywhere on the page that applies it, and the selling price on the
quotation and in the catalogue is unchanged.

**Filename:** `8c-margin-suggestion.png`

---

## 8d. Margin protection on the purchase plan

**Open:** run the planner at ₹25,000 after confirming.

**Must be visible:** the **Margin protection** section inside the plan, above
the commitments, alongside the existing ₹803.40 supplier-price-impact block.

**Why it matters:** the same confirmed cost, followed through to both of its
consequences — margin per unit sold, and restocking capacity for the week.
They are different figures and the UI does not conflate them.

**Filename:** `8d-margin-in-plan.png`

---

## 8e. WhatsApp quotation draft

**Open:** click **Send Quote on WhatsApp** under a quotation.

**Must be visible:** WhatsApp (app or web) with the draft message showing the
real line prices, **Total: ₹22,306.48** and both shortages — and the message
still **unsent**.

**Why it matters:** shows the handoff is a draft the owner sends. Capture it
before sending; do **not** send it to anyone.

**Filename:** `8e-whatsapp-quote-draft.png`

---

## 8f. WhatsApp purchase-request draft

**Open:** click **Share Purchase Plan** under a plan.

**Must be visible:** the draft grouped by supplier, the plan's own estimated
cost and budget, and the line *"Draft only — no order has been placed."*

**Why it matters:** no order is placed, no message is sent, and the draft says
so in its own text.

**Filename:** `8f-whatsapp-plan-draft.png`

---

## 8g. WhatsApp — the fallback, told honestly

**Open:** the live site, run an order with a customer selected, press
**Send via WhatsApp**.

**Must be visible:** *"WhatsApp API not configured — open the draft to send it
yourself"* and the **Open WhatsApp draft** link.

**Why it matters:** this is the honest state of the feature. The adapter is
implemented; the credentials are not present; the product says so rather than
claiming a send. **Do not stage a screenshot suggesting a message was
delivered.**

**Filename:** `8g-whatsapp-not-configured.png`

---

## 8h. The customer-facing message

**Open:** the draft that opens in WhatsApp.

**Must be visible:** the quotation total **₹22,306.48**, the wire line reading
**3 coils**, and the credit status as a single word.

**Why it matters:** the separation of internal and customer-facing data, shown
rather than asserted. There is no supplier cost, no margin and no stock level
anywhere in the message.

**Filename:** `8h-whatsapp-customer-message.png`

---

## 8i. WhatsApp configuration is absent, by design

**Open:** Lambda → `shopflow-api` → Configuration → Environment variables.

**Must be visible:** `WHATSAPP_API_ENABLED` = `false`, and **no** token,
phone-number id or secret ARN.

**Why it matters:** the safe default is the deployed default, and no
credential is present to leak.

**Filename:** `8i-whatsapp-env.png`

---

## 0a. The order queue exists

**Open:** SQS → Queues, filtered to `shopflow-`.

**Must be visible:** both `shopflow-orders` and `shopflow-orders-dlq`.

**Why it matters:** this is the durable path that replaced a direct
asynchronous Lambda invoke. Two queues, not one — the dead-letter queue is the
half that makes a failure inspectable.

**Filename:** `0a-sqs-queues.png`

---

## 0b. The redrive policy

**Open:** SQS → `shopflow-orders` → **Dead-letter queue** panel.

**Must be visible:** the DLQ set to `shopflow-orders-dlq` and **Maximum
receives: 3**. Capture the **Visibility timeout: 6 minutes** from the
Configuration panel in the same shot if it fits.

**Why it matters:** the retry budget is explicit and derived — three attempts
because a Bedrock throttle clears in seconds, and 360 seconds because that is
six times the worker's timeout.

**Filename:** `0b-redrive-policy.png`

---

## 0c. Bounded worker concurrency

**Open:** Lambda → `shopflow-order-worker` → Configuration → **Triggers**.

**Must be visible:** the SQS trigger, **Enabled**, with **Maximum concurrency
5** and batch size 1.

**Why it matters:** the account ceiling is 10 concurrent executions across
every project. This is the line that stops a queue burst from starving the
public API. It is the least obvious and most important setting in the change.

**Filename:** `0c-worker-concurrency.png`

---

## 0d. The DLQ is empty, and alarmed

**Open:** CloudWatch → Alarms, filtered to `shopflow-`.

**Must be visible:** three alarms — `shopflow-orders-dlq-not-empty`,
`shopflow-worker-errors-sustained`, `shopflow-orders-queue-backlog` — all in
**OK**, and the DLQ showing 0 messages available.

**Why it matters:** empty is the expected state and the alarm fires on a single
message.

**Caveat to keep with the screenshot:** these alarms have **no actions**. There
is no SNS topic in this stack, so they change state in the console and notify
nobody. Do not present them as a paging setup.

**Filenames:** `0d-alarms.png`, `0e-dlq-empty.png`

---

## 0f. The API cannot invoke the worker

**Open:** IAM → the `shopflow-api` function's role → the inline policy.

**Must be visible:** `sqs:SendMessage` on the `shopflow-orders` ARN, and **no
`lambda:InvokeFunction` anywhere**.

**Why it matters:** removing the call site is a decision; removing the
permission is a guarantee. The queue is the only route into the worker.

**Filename:** `0f-api-iam-sqs-only.png`

---

## 9a. The language selector, and what it admits

**Open:** the live site, top of the page.

**Must be visible:** the selector showing native names, and the capability
line beside it.

**Why it matters:** the list is not hard-coded in the page — it comes from
`GET /api/languages`, which reports what each language can actually do. Capture
one language with voice and one without, so both states are on record.

**Filenames:** `9a-language-selector.png`, `9b-language-voice-unavailable.png`

---

## 9c. Same order, three languages, one total

**Open:** run the canonical order in Tamil, then Hindi, then Telugu.

**Must be visible:** **₹22,306.48** in all three, and **3 coils** in all three.

**Why it matters:** this is the whole claim in one image. Capture the three
screenshots at the same scroll position so the total lines up across them.

**Filenames:** `9c-quote-tamil.png`, `9d-quote-hindi.png`, `9e-quote-telugu.png`

---

## 9f. Right-to-left

**Open:** switch the language to **اردو**.

**Must be visible:** the page right-aligned, and the rupee figures still
left-to-right and still reading ₹22,306.48.

**Why it matters:** direction comes from the language registry, not from a
guess, and identifiers and money stay LTR because they are not prose.

**Filename:** `9f-urdu-rtl.png`

---

## 9g. Partial translation, stated

**Open:** switch the language to **ᱥᱟᱱᱛᱟᱲᱤ** (Santali).

**Must be visible:** the capability line reading **21% translated — the rest is
shown in English**, and a page that is part Santali and part English with no
broken text anywhere.

**Why it matters:** this is the fallback working, and the product being honest
about an incomplete translation instead of implying a finished one. **Do not
stage a screenshot suggesting Santali is complete.**

**Filename:** `9g-santali-partial.png`

---

## 15b. Voice assistant — microphone UI

**Open:** the live site in Chrome, scroll to **Voice assistant**.

**Must be visible:** the microphone button, the language selector (Auto /
Tamil / English), and the status line. Capture once **idle** and once
**listening** (red, "Listening… speak naturally").

**Why it matters:** shows voice is a first-class input on the real deployed
app, not a mock.

**Filenames:** `15b-voice-idle.png`, `15c-voice-listening.png`

---

## 15d. Tamil / Tanglish transcript

**Open:** speak *"Anna, 20 Anchor modular switch 1-Way 10 amp, 3 coil Finolex
1.5 sq mm red wire 90m, 2 Havells MCB SP 32 amp."*

**Must be visible:** the recognised transcript in the editable **You said**
box, and any "Adjusted for speech" note.

**Why it matters:** proves Tanglish input reaches the system, and that the
owner sees and can correct what was heard before anything is acted on.

**Filename:** `15d-voice-transcript.png`

---

## 15e. Voice order result

**Must be visible:** the quotation produced from the spoken order, showing
**₹22,306.48** — the same total as the typed path.

**Why it matters:** the single strongest voice claim. Voice changed the input
channel; the deterministic engine still produced the number.

**Filename:** `15e-voice-order-quote.png`

---

## 15f. Voice clarification

**Open:** speak *"Anchor switch stock la evlo irukku?"*

**Must be visible:** the assistant asking which variant, with real options —
**no number invented**.

**Why it matters:** the refusal-to-guess rule holds over voice too.

**Filename:** `15f-voice-clarification.png`

---

## 15g. Spoken quotation / stock answer

**Open:** speak *"Havells MCB SP 32 amp irukka?"*

**Must be visible:** the assistant's reply with the real stock figure, and the
fact strip showing stock, price and SKU.

**Why it matters:** shows spoken answers carry engine values, not paraphrase.

**Filename:** `15g-voice-stock-answer.png`

---

## 15h. Fallback behaviour

**Open:** either a browser without the Web Speech API, or deny the microphone
permission deliberately.

**Must be visible:** the plain message ("…you can type the order instead") and
the typed order box still fully usable below.

**Why it matters:** the business workflow never depends on speech working.

**Filename:** `15h-voice-fallback.png`

---

## 15i. Voice architecture (no AWS change)

**Open:** API Gateway → Routes, showing `POST /api/shop-queries`.

**Must be visible:** the route alongside the others.

**Why it matters:** voice added exactly one deterministic route and **no new
AWS service**. Speech recognition is client-side; the business workflow stays
on AWS. The route has no Bedrock access — it is catalogue lookup only.

**Filename:** `15i-voice-api-route.png`

---

## 16. Final deployed state

**Open:** run and capture, in one terminal frame:

```bash
git rev-parse HEAD
git status --short
python -m pytest
python scripts/smoke_test_planner.py
```

**Must be visible:** the commit SHA, a clean working tree, **1490 passed**,
and the smoke test's **36 checks / SMOKE TEST PASSED**.

**Why it matters:** ties the deployed system to an exact commit, with the full
test suite and real-AWS smoke checks passing at that commit.

**Filename:** `16-final-state-tests.png`

---

## Capture order (suggested)

Doing it in this order avoids re-running the demo:

1. **2, 3, 3b, 4, 6, 6b, 7, 9, 10, 11, 12, 13, 13b, 14, 15** — static console
   state, any time.
2. **1** — agent terminal session.
3. **5** — live URL, incognito.
4. **Run the demo** (`docs/DEMO-RUNBOOK.md` steps A–E) → capture **8** and
   **14b**.
5. **Reset**: `python scripts/reset_confirmed_costs.py --confirm`.
6. **16** — final verification.

---

## Completion tracker

Tick only when the file actually exists in this directory.

- [ ] `01-agent-aws-connection.png`
- [ ] `02-account-region.png`
- [ ] `03-cloudformation-update-complete.png`
- [ ] `03b-cfn-outputs.png`
- [ ] `04-cloudfront-distribution.png`
- [ ] `05-live-url-browser.png`
- [ ] `06-apigateway-routes.png`
- [ ] `06b-apigw-throttle.png`
- [ ] `07-dynamodb-table.png`
- [ ] `08-dynamodb-confirmed-cost-item.png`
- [ ] `09-lambda-functions.png`
- [ ] `10-lambda-iam-role.png`
- [ ] `11-worker-bedrock-scoped.png`
- [ ] `12-api-role-no-bedrock.png`
- [ ] `13-s3-site-private-oac.png`
- [ ] `13b-s3-uploads-private.png`
- [ ] `14-cloudwatch-log-groups.png`
- [ ] `14b-cloudwatch-worker-log-line.png`
- [ ] `15-aws-budget.png`
- [ ] `0a-sqs-queues.png`
- [ ] `0b-redrive-policy.png`
- [ ] `0c-worker-concurrency.png`
- [ ] `0d-alarms.png`
- [ ] `0e-dlq-empty.png`
- [ ] `0f-api-iam-sqs-only.png`
- [ ] `9a-language-selector.png`
- [ ] `9b-language-voice-unavailable.png`
- [ ] `9c-quote-tamil.png`
- [ ] `9d-quote-hindi.png`
- [ ] `9e-quote-telugu.png`
- [ ] `9f-urdu-rtl.png`
- [ ] `9g-santali-partial.png`
- [ ] `7b-transcribe-job.png`
- [ ] `7c-voice-audio-empty.png`
- [ ] `7d-voice-lifecycle-rule.png`
- [ ] `7e-transcribe-iam.png`
- [ ] `8g-whatsapp-not-configured.png`
- [ ] `8h-whatsapp-customer-message.png`
- [ ] `8i-whatsapp-env.png`
- [ ] `8a1-uom-quote-line.png`
- [ ] `8a2-uom-no-silent-conversion.png`
- [ ] `8a3-khata-approved.png`
- [ ] `8a4-khata-exceeded.png`
- [ ] `8a5-no-credit-rows.png`
- [ ] `8b-margin-protection.png`
- [ ] `8c-margin-suggestion.png`
- [ ] `8d-margin-in-plan.png`
- [ ] `8e-whatsapp-quote-draft.png`
- [ ] `8f-whatsapp-plan-draft.png`
- [ ] `15b-voice-idle.png`
- [ ] `15c-voice-listening.png`
- [ ] `15d-voice-transcript.png`
- [ ] `15e-voice-order-quote.png`
- [ ] `15f-voice-clarification.png`
- [ ] `15g-voice-stock-answer.png`
- [ ] `15h-voice-fallback.png`
- [ ] `15i-voice-api-route.png`
- [ ] `16-final-state-tests.png`
