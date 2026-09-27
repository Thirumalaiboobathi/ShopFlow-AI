# Coding agent ↔ AWS — log evidence

Text evidence, captured by the coding agent itself from the AWS CLI and git on
**2026-09-27**, read-only. It complements the screenshot checklist in
[`README.md`](README.md), which still has to be captured by a person in the
AWS console. Nothing here is a credential: account ids and ARNs are
identifiers, not secrets.

## 1. The identity the agent uses

```
$ aws sts get-caller-identity --query "[Account,Arn]" --output text
675613597178    arn:aws:iam::675613597178:user/AI-agent
```

A dedicated IAM user, used only by the coding agent (Claude Code) through the
AWS CLI and CDK.

## 2. The agent deploys the stack

CloudTrail `AssumeRole` events for the CDK bootstrap roles during the
2026-09-27 deploys. The caller is the agent's IAM user; `aws-cdk-thiru` is
the role session name CDK gives the deployment:

```
2026-09-27T06:25:16Z  arn:aws:iam::675613597178:user/AI-agent  aws-cdk-thiru  cdk-hnb659fds-file-publishing-role-675613597178-ap-south-1
2026-09-27T06:25:19Z  arn:aws:iam::675613597178:user/AI-agent  aws-cdk-thiru  cdk-hnb659fds-deploy-role-675613597178-ap-south-1
```

Every CloudFormation change set executed on `ShopFlowStack` runs under that
session name (CloudTrail `ExecuteChangeSet`, `ap-south-1`):

```
2026-09-20T20:19:27+05:30  ExecuteChangeSet  aws-cdk-thiru
2026-09-21T10:13:29+05:30  ExecuteChangeSet  aws-cdk-thiru
2026-09-21T10:26:55+05:30  ExecuteChangeSet  aws-cdk-thiru
2026-09-21T20:12:42+05:30  ExecuteChangeSet  aws-cdk-thiru
2026-09-22T14:14:16+05:30  ExecuteChangeSet  aws-cdk-thiru
2026-09-22T17:07:34+05:30  ExecuteChangeSet  aws-cdk-thiru
2026-09-23T08:53:24+05:30  ExecuteChangeSet  aws-cdk-thiru
2026-09-23T17:09:39+05:30  ExecuteChangeSet  aws-cdk-thiru
2026-09-23T19:43:41+05:30  ExecuteChangeSet  aws-cdk-thiru
2026-09-24T10:40:15+05:30  ExecuteChangeSet  aws-cdk-thiru
2026-09-27T11:49:09+05:30  ExecuteChangeSet  aws-cdk-thiru
2026-09-27T11:55:38+05:30  ExecuteChangeSet  aws-cdk-thiru
```

For the two 2026-09-27 deploys the `AssumeRole` above ties the session to the
`AI-agent` user. For the earlier ones this file shows the session name only;
the matching `AssumeRole` events are in CloudTrail event history (90 days).

Reproduce:

```
aws cloudtrail lookup-events --region ap-south-1 \
  --lookup-attributes AttributeKey=EventName,AttributeValue=ExecuteChangeSet
aws cloudtrail lookup-events --region ap-south-1 \
  --lookup-attributes AttributeKey=Username,AttributeValue=AI-agent
```

## 3. What is running now

```
$ aws lambda get-function-configuration --function-name shopflow-order-worker \
    --query "[LastModified,CodeSha256]"
2026-09-27T06:25:43.000+0000   7zg0bfLGURkrv6ZlPMhq6yRYrn7syDa2f8otPLazYso=
```

The API, worker and health functions share one code asset. The deployed
package was downloaded and compared file by file with the repository at the
deployed commit; `engine/whatif.py` and `lambdas/api/handler.py` were
identical.

## 4. Commits written with the agent

Every commit below carries a `Co-Authored-By: Claude …` trailer
(`git log --format='%h %s%n%(trailers:key=Co-Authored-By)'`):

```
067a33f 2026-09-27 fix: ask the unit question when a kg line matches no product
cbfb50a 2026-09-27 feat: GST, What-If and walk-away price in the workspace
2241a5a 2026-09-27 fix: bounded model retries with a safe final state
140cb83 2026-09-27 fix: enforce UOM and quantity safety
48b651f 2026-09-27 fix: secure customer and WhatsApp data boundaries
baa6fe9 2026-09-27 feat: deterministic GST, What-If simulator and walk-away price
c4696f9 2026-09-20 Stage 5.1: persist confirmed supplier costs
b396a78 2026-09-20 Stage 5: cash-constrained purchasing planner
73daa84 2026-09-20 Stage 4: store decision prices as Decimal
4c1c256 2026-09-20 Stage 4: supplier price intelligence
38de25e 2026-09-20 Stage 3.1: keep stated attributes when asking for clarification
4b05f7c 2026-09-20 Stage 3.1: harden variant ambiguity handling
379aa48 2026-09-20 Stage 3: deploy order processing to AWS
68cdb02 2026-09-20 Stage 3: build order processing foundation
e438168 2026-09-19 Stage 2: ship the public foundation to AWS
a2e4e6b 2026-09-19 Day 1: deterministic engine, seeded shop, and 83 tests
```

Seven commits between 2026-09-21 and 2026-09-24 do not carry the trailer; the
work in them is described in [`docs/development-log.md`](../development-log.md).

## 5. What is still missing

- The console screenshots in [`README.md`](README.md) - a person must capture
  them.
- **The repository is private.** An external judge cannot read this file or
  the commit history until it is made public or attached to the submission.
