# Evidence pack — capture checklist

Screenshots required for the AWS "Zero to Shipped" submission, in particular
the ship-gate requirement for **documented proof of coding-agent connection to
the AWS console**.

> **Status: NOTHING HAS BEEN CAPTURED YET.**
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

**Must be visible:** all seven routes —
`GET /api/health`, `POST /api/orders`, `POST /api/supplier-price-lists`,
`POST /api/price-decisions`, `POST /api/purchase-plans`,
`GET /api/jobs/{jobId}`, `GET /api/demo`.

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

## 16. Final deployed state

**Open:** run and capture, in one terminal frame:

```bash
git rev-parse HEAD
git status --short
python -m pytest
python scripts/smoke_test_planner.py
```

**Must be visible:** the commit SHA, a clean working tree, **336 passed**, and
the smoke test's **25 checks / SMOKE TEST PASSED**.

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
- [ ] `16-final-state-tests.png`
