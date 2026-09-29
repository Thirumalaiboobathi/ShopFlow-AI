#!/usr/bin/env bash
#
# Safe deployment for ShopFlowStack.
#
# WHY THIS SCRIPT EXISTS
# ----------------------
# The monthly cost budget is created inside a conditional:
#
#     if alert_email:
#         budgets.CfnBudget(...)
#
# `alertEmail` is optional CDK context. Synthesising without it therefore does
# not error - it simply produces a template with no budget in it, and deploying
# that template DESTROYS the existing cost guard. This was caught once by
# reading `cdk diff` before a Stage 5 deploy, which showed:
#
#     [-] AWS::Budgets::Budget MonthlyBudget destroy
#
# A prose warning in the development log is not a control. This script is.
# It refuses to run without the alert email, always passes the budget context,
# shows the exact command, and fails the deploy if the diff would remove the
# budget.
#
# USAGE
# -----
#     export SHOPFLOW_ALERT_EMAIL="you@example.com"
#     ./scripts/deploy.sh                 # diff, confirm, deploy
#     ./scripts/deploy.sh --diff-only     # diff and stop
#     ./scripts/deploy.sh --yes           # skip the interactive confirmation
#
# The email is read from the environment rather than committed, so a personal
# address never enters version control. Set it once per shell, or keep it in a
# local untracked file and source that.

set -euo pipefail

MONTHLY_BUDGET_USD="${SHOPFLOW_MONTHLY_BUDGET_USD:-25}"
STACK="ShopFlowStack"
CDK="npx aws-cdk@2"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
INFRA_DIR="$(cd "${SCRIPT_DIR}/../infrastructure" && pwd)"

DIFF_ONLY=0
ASSUME_YES=0
for arg in "$@"; do
  case "$arg" in
    --diff-only) DIFF_ONLY=1 ;;
    --yes|-y)    ASSUME_YES=1 ;;
    --help|-h)   sed -n '2,40p' "${BASH_SOURCE[0]}"; exit 0 ;;
    *) echo "unknown argument: $arg" >&2; exit 2 ;;
  esac
done

red()  { printf '\033[31m%s\033[0m\n' "$*"; }
grn()  { printf '\033[32m%s\033[0m\n' "$*"; }
ylw()  { printf '\033[33m%s\033[0m\n' "$*"; }
bold() { printf '\033[1m%s\033[0m\n' "$*"; }

# ---------------------------------------------------------------------------
# 1. Refuse to deploy without the alert email
# ---------------------------------------------------------------------------
if [[ -z "${SHOPFLOW_ALERT_EMAIL:-}" ]]; then
  red "REFUSING TO DEPLOY: SHOPFLOW_ALERT_EMAIL is not set."
  echo
  echo "The \$${MONTHLY_BUDGET_USD}/month cost budget is only created when the"
  echo "alertEmail context is supplied. Deploying without it would delete the"
  echo "budget alarm that is currently protecting this account."
  echo
  echo "Set it and try again:"
  echo
  echo "    export SHOPFLOW_ALERT_EMAIL=\"thiru260402@gmail.com\""
  echo "    ./scripts/deploy.sh"
  echo
  exit 1
fi

# A missing '@' almost certainly means a shell quoting mistake rather than a
# real address, and AWS Budgets would reject it after the rest of the stack
# had already updated.
if [[ "${SHOPFLOW_ALERT_EMAIL}" != *"@"* ]]; then
  red "REFUSING TO DEPLOY: SHOPFLOW_ALERT_EMAIL does not look like an email address."
  echo "  got: ${SHOPFLOW_ALERT_EMAIL}"
  exit 1
fi

if ! [[ "${MONTHLY_BUDGET_USD}" =~ ^[0-9]+$ ]]; then
  red "REFUSING TO DEPLOY: SHOPFLOW_MONTHLY_BUDGET_USD must be a whole number."
  echo "  got: ${MONTHLY_BUDGET_USD}"
  exit 1
fi

CONTEXT_ARGS=(
  -c "alertEmail=${SHOPFLOW_ALERT_EMAIL}"
  -c "monthlyBudgetUsd=${MONTHLY_BUDGET_USD}"
)

# WhatsApp Cloud API - optional, and off unless these are set. The values are
# Meta ids and a Secrets Manager ARN; no credential is ever passed here. The
# secret itself (accessToken, appSecret, verifyToken) is created by the owner.
#
#     SHOPFLOW_WHATSAPP_ENABLED=true
#     SHOPFLOW_WHATSAPP_PHONE_NUMBER_ID=<phone number id from Meta>
#     SHOPFLOW_WHATSAPP_SECRET_ARN=<arn of the secret>
#     SHOPFLOW_WHATSAPP_TEMPLATE_NAME=<optional approved template>
#     SHOPFLOW_WHATSAPP_API_VERSION=<optional, default v26.0>
[[ -n "${SHOPFLOW_WHATSAPP_ENABLED:-}" ]] && \
  CONTEXT_ARGS+=(-c "whatsappEnabled=${SHOPFLOW_WHATSAPP_ENABLED}")
[[ -n "${SHOPFLOW_WHATSAPP_PHONE_NUMBER_ID:-}" ]] && \
  CONTEXT_ARGS+=(-c "whatsappPhoneNumberId=${SHOPFLOW_WHATSAPP_PHONE_NUMBER_ID}")
[[ -n "${SHOPFLOW_WHATSAPP_SECRET_ARN:-}" ]] && \
  CONTEXT_ARGS+=(-c "whatsappTokenSecretArn=${SHOPFLOW_WHATSAPP_SECRET_ARN}")
[[ -n "${SHOPFLOW_WHATSAPP_TEMPLATE_NAME:-}" ]] && \
  CONTEXT_ARGS+=(-c "whatsappTemplateName=${SHOPFLOW_WHATSAPP_TEMPLATE_NAME}")
[[ -n "${SHOPFLOW_WHATSAPP_API_VERSION:-}" ]] && \
  CONTEXT_ARGS+=(-c "whatsappApiVersion=${SHOPFLOW_WHATSAPP_API_VERSION}")

# The same trap as the budget: WhatsApp context is optional, so a deploy from
# a shell without it would quietly switch a live WhatsApp integration off.
if [[ -z "${SHOPFLOW_WHATSAPP_ENABLED:-}" ]]; then
  LIVE_WA="$(aws lambda get-function-configuration --function-name shopflow-api \
    --query 'Environment.Variables.WHATSAPP_API_ENABLED' --output text 2>/dev/null || true)"
  if [[ "${LIVE_WA}" == "true" ]]; then
    red "REFUSING TO DEPLOY: WhatsApp is switched on in the deployed stack,"
    echo "and SHOPFLOW_WHATSAPP_ENABLED is not set in this shell. Deploying"
    echo "would switch it off. Export the SHOPFLOW_WHATSAPP_* variables first,"
    echo "or set SHOPFLOW_WHATSAPP_ENABLED=false to switch it off on purpose."
    exit 1
  fi
fi

# ---------------------------------------------------------------------------
# 2. Show exactly what is about to run
# ---------------------------------------------------------------------------
bold "ShopFlow deployment"
echo "  stack           ${STACK}"
echo "  alert email     ${SHOPFLOW_ALERT_EMAIL}"
echo "  monthly budget  \$${MONTHLY_BUDGET_USD} USD"
echo "  whatsapp        ${SHOPFLOW_WHATSAPP_ENABLED:-false (not configured)}"
echo "  working dir     ${INFRA_DIR}"
echo
bold "Command to be run:"
echo
echo "    cd ${INFRA_DIR}"
echo "    ${CDK} deploy --require-approval never ${CONTEXT_ARGS[*]}"
echo

cd "${INFRA_DIR}"

# ---------------------------------------------------------------------------
# 3. Diff first, and read it
# ---------------------------------------------------------------------------
DIFF_FILE="$(mktemp)"
trap 'rm -f "${DIFF_FILE}"' EXIT

bold "Running cdk diff..."
echo
# cdk diff exits non-zero when differences exist, which is the normal case
# here, so its status is deliberately not treated as failure.
set +e
${CDK} diff "${CONTEXT_ARGS[@]}" 2>&1 | tee "${DIFF_FILE}"
set -e
echo

# ---------------------------------------------------------------------------
# 4. Fail on budget deletion
# ---------------------------------------------------------------------------
# The exact line CloudFormation prints when the budget would be removed:
#     [-] AWS::Budgets::Budget MonthlyBudget destroy
if grep -qE '^\[-\].*AWS::Budgets::Budget' "${DIFF_FILE}"; then
  red "ABORTING: this deployment would DESTROY the cost budget."
  echo
  grep -E '^\[-\].*AWS::Budgets::Budget' "${DIFF_FILE}"
  echo
  echo "This should be impossible with the alert email set, so something else"
  echo "is wrong - check infrastructure/shopflow_stack.py before proceeding."
  exit 1
fi
grn "OK: the diff does not remove the cost budget."

# Any other deletion is legitimate sometimes, but never silently.
if grep -qE '^\[-\]' "${DIFF_FILE}"; then
  echo
  ylw "NOTE: this deployment deletes resources:"
  grep -E '^\[-\]' "${DIFF_FILE}" | sed 's/^/    /'
  echo
  ylw "Read the list above carefully before continuing."
fi

if [[ "${DIFF_ONLY}" -eq 1 ]]; then
  echo
  bold "--diff-only requested. Stopping without deploying."
  exit 0
fi

# ---------------------------------------------------------------------------
# 5. Confirm, then deploy
# ---------------------------------------------------------------------------
if [[ "${ASSUME_YES}" -eq 0 ]]; then
  echo
  read -r -p "Proceed with deploy? [y/N] " reply
  case "${reply}" in
    y|Y|yes|YES) ;;
    *) echo "Cancelled."; exit 0 ;;
  esac
fi

echo
bold "Deploying..."
${CDK} deploy --require-approval never "${CONTEXT_ARGS[@]}"

# ---------------------------------------------------------------------------
# 6. Confirm the budget actually survived
# ---------------------------------------------------------------------------
echo
bold "Post-deploy check: is the budget still there?"
ACCOUNT_ID="$(aws sts get-caller-identity --query Account --output text)"
if aws budgets describe-budgets --account-id "${ACCOUNT_ID}" \
     --query "Budgets[?BudgetName=='shopflow-monthly'].BudgetName" \
     --output text 2>/dev/null | grep -q 'shopflow-monthly'; then
  grn "OK: budget 'shopflow-monthly' is present."
else
  red "WARNING: budget 'shopflow-monthly' was NOT found after deploy."
  echo "Investigate before leaving the stack in this state."
  exit 1
fi

echo
grn "Deployment complete."
echo
echo "Next: run the post-deploy verification in docs/DEMO-RUNBOOK.md"
echo "  python -m pytest"
echo "  python scripts/smoke_test_planner.py"
