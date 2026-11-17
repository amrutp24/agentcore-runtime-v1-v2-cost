#!/usr/bin/env bash
# Allow CloudWatch Logs delivery to write USAGE_LOGS into the experiment's
# vended log groups. One account-level resource policy covers all variants
# (it is not per-variant, so it lives outside the Terraform workspaces).
#   scripts/usage_log_policy.sh          # create or update
#   scripts/usage_log_policy.sh delete   # remove (teardown.sh all does this too)
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PY="${PYTHON:-python}"
REGION="$($PY scripts/variant_cfg.py get region | tr -d '\r')"
PROJECT="$($PY scripts/variant_cfg.py get project_tag | tr -d '\r')"
NAME="$PROJECT-usage-logs"

if [ "${1:-}" = "delete" ]; then
  aws logs delete-resource-policy --region "$REGION" --policy-name "$NAME" 2>/dev/null \
    && echo "deleted $NAME" || echo "$NAME not present"
  exit 0
fi

ACCOUNT="$(aws sts get-caller-identity --query Account --output text | tr -d '\r')"
POLICY=$(cat <<EOF
{"Version":"2012-10-17","Statement":[{"Sid":"AWSLogDeliveryWrite","Effect":"Allow",
"Principal":{"Service":"delivery.logs.amazonaws.com"},
"Action":["logs:CreateLogStream","logs:PutLogEvents"],
"Resource":"arn:aws:logs:$REGION:$ACCOUNT:log-group:/aws/vendedlogs/bedrock-agentcore/*:log-stream:*",
"Condition":{"StringEquals":{"aws:SourceAccount":"$ACCOUNT"},
"ArnLike":{"aws:SourceArn":"arn:aws:logs:$REGION:$ACCOUNT:*"}}}]}
EOF
)
aws logs put-resource-policy --region "$REGION" --policy-name "$NAME" \
  --policy-document "$POLICY" --query 'resourcePolicy.policyName' --output text
echo "policy in place; usage records should start arriving within the hour"
