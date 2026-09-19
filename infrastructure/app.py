#!/usr/bin/env python3
"""CDK entry point for ShopFlow.

One stack, one region. `alertEmail` is optional context: pass it to create the
monthly cost budget, e.g.

    npx cdk deploy -c alertEmail=you@example.com
"""

from __future__ import annotations

import os

import aws_cdk as cdk

from shopflow_stack import ShopFlowStack

app = cdk.App()

stack = ShopFlowStack(
    app, "ShopFlowStack",
    alert_email=app.node.try_get_context("alertEmail"),
    monthly_budget_usd=int(app.node.try_get_context("monthlyBudgetUsd") or 25),
    env=cdk.Environment(
        account=os.environ.get("CDK_DEFAULT_ACCOUNT"),
        region=os.environ.get("CDK_DEFAULT_REGION", "ap-south-1"),
    ),
    description="ShopFlow AI - public foundation (site, API, data)",
)

# Tags make ShopFlow resources separable from the other projects in this
# account, for both cost attribution and cleanup.
cdk.Tags.of(stack).add("Project", "ShopFlow")
cdk.Tags.of(stack).add("ManagedBy", "CDK")
cdk.Tags.of(stack).add("Environment", "prod")

app.synth()
