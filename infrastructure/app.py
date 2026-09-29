#!/usr/bin/env python3
"""CDK entry point for ShopFlow.

One stack, one region. `alertEmail` is optional context: pass it to create the
monthly cost budget, e.g.

    npx cdk deploy -c alertEmail=you@example.com

WhatsApp Business Cloud API sending is off unless it is switched on, and no
credential value is ever passed here - `whatsappTokenSecretArn` names a
Secrets Manager secret, it does not contain one:

    -c whatsappEnabled=true
    -c whatsappPhoneNumberId=<id from Meta>
    -c whatsappTokenSecretArn=<arn of a secret holding the access token>
    -c whatsappTemplateName=<an approved template, if sending outside the
                             24-hour customer service window>
    -c whatsappApiVersion=<Graph API version, default v26.0>

The same secret serves the inbound webhook when it is a JSON document with
`accessToken`, `appSecret` and `verifyToken`. scripts/deploy.sh passes these
from SHOPFLOW_WHATSAPP_* environment variables.

With none of these supplied the API returns the wa.me draft it always has.
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
    whatsapp_enabled=str(
        app.node.try_get_context("whatsappEnabled") or "").lower() == "true",
    whatsapp_phone_number_id=app.node.try_get_context("whatsappPhoneNumberId"),
    whatsapp_token_secret_arn=app.node.try_get_context("whatsappTokenSecretArn"),
    whatsapp_template_name=app.node.try_get_context("whatsappTemplateName"),
    whatsapp_api_version=app.node.try_get_context("whatsappApiVersion"),
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
