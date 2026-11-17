#!/usr/bin/env bash
# Tear down variants. Idempotent: safe to re-run until the final listing is empty.
#   scripts/teardown.sh <variant> [<variant> ...]
#   scripts/teardown.sh all        # every Terraform workspace that exists
#
# NOTE: AgentCore keeps VPC ENIs for up to 8 hours after a runtime is deleted,
# so VPC/subnet/security-group deletion can fail with DependencyViolation.
# NAT gateways, EIPs and endpoints (the billable parts) are removed first by
# Terraform's dependency order; just re-run this script later for the rest.
set -uo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PY="${PYTHON:-python}"
CFG="$PY scripts/variant_cfg.py"
REGION="$($CFG get region | tr -d '\r')"
PROJECT="$($CFG get project_tag | tr -d '\r')"
RESULTS="$($CFG get results_dir | tr -d '\r')"
FAILED=0

[ $# -ge 1 ] || { echo "usage: $0 <variant>... | all" >&2; exit 2; }

terraform -chdir=infra init -input=false >/dev/null || exit 1

if [ "$1" = "all" ]; then
  mapfile -t VARIANTS < <(terraform -chdir=infra workspace list | tr -d '* \r' | grep -v '^default$' | grep -v '^$')
else
  VARIANTS=("$@")
fi

for V in "${VARIANTS[@]}"; do
  echo "== teardown $V"
  if ! terraform -chdir=infra workspace select "$V" >/dev/null 2>&1; then
    echo "   no workspace for $V (already gone)"
    continue
  fi

  # Needs the same vars as apply (variant_id is required). Skipped if the
  # variant is no longer in config.yaml and no tfvars file survives.
  VARFILE="../build/variants/$V.tfvars.json"
  if ! $CFG tfvars "$V" "build/variants/$V.tfvars.json" 2>/dev/null && [ ! -f "build/variants/$V.tfvars.json" ]; then
    echo "   cannot build vars for $V; add it back to config.yaml" >&2
    FAILED=1
    continue
  fi

  RUNTIME_ID=""
  if [ -f "$RESULTS/$V/outputs.json" ]; then
    RUNTIME_ID="$($PY -c "import json;print(json.load(open('$RESULTS/$V/outputs.json')).get('agent_runtime_id') or '')" | tr -d '\r')"
  fi

  if terraform -chdir=infra destroy -input=false -auto-approve -var-file="$VARFILE"; then
    terraform -chdir=infra workspace select default >/dev/null
    terraform -chdir=infra workspace delete "$V" >/dev/null 2>&1 || true
    $CFG mark "$V" torn_down_at 2>/dev/null || true
  else
    echo "   destroy incomplete for $V (ENIs may still be releasing); re-run later" >&2
    FAILED=1
  fi

  # Runtime log groups are created by the service, not Terraform. (The usage-log
# group, delivery source, destination and delivery are Terraform-managed and go
# with the destroy above; the listing at the end confirms.)
  if [ -n "$RUNTIME_ID" ]; then
    for LG in $(aws logs describe-log-groups --region "$REGION" \
        --log-group-name-prefix "/aws/bedrock-agentcore/runtimes/$RUNTIME_ID" \
        --query 'logGroups[].logGroupName' --output text 2>/dev/null | tr -d '\r'); do
      echo "   deleting log group $LG"
      aws logs delete-log-group --region "$REGION" --log-group-name "$LG" || FAILED=1
    done
  fi
done

# ECR repos (only exist if you moved to container images).
for ARN in $(aws resourcegroupstaggingapi get-resources --region "$REGION" \
    --tag-filters "Key=project,Values=$PROJECT" --resource-type-filters ecr:repository \
    --query 'ResourceTagMappingList[].ResourceARN' --output text 2>/dev/null | tr -d '\r'); do
  NAME="${ARN##*:repository/}"
  echo "== deleting ECR repo $NAME"
  aws ecr delete-repository --region "$REGION" --repository-name "$NAME" --force || FAILED=1
done

echo
echo "== usage-log delivery sources / log groups still present"
aws logs describe-delivery-sources --region "$REGION"   --query "deliverySources[?starts_with(name, 'bill-')].name" --output text 2>&1 | tr '	' '
'
aws logs describe-log-groups --region "$REGION"   --log-group-name-prefix "/aws/vendedlogs/bedrock-agentcore/runtimes/USAGE_LOGS/bill-"   --query 'logGroups[].logGroupName' --output text 2>&1 | tr '	' '
'

# Account-level log resource policy for usage logs (not in any workspace).
if [ "$1" = "all" ]; then
  bash scripts/usage_log_policy.sh delete || FAILED=1
fi

echo
echo "== runtimes still present (name starts with bill_)"
aws bedrock-agentcore-control list-agent-runtimes --region "$REGION" \
  --query "agentRuntimes[?starts_with(agentRuntimeName, 'bill_')].[agentRuntimeName,agentRuntimeId,status]" \
  --output table 2>&1 || true

echo
echo "== resources still carrying project=$PROJECT (tagging API can lag a little)"
aws resourcegroupstaggingapi get-resources --region "$REGION" \
  --tag-filters "Key=project,Values=$PROJECT" \
  --query 'ResourceTagMappingList[].ResourceARN' --output text 2>&1 | tr '\t' '\n'

if [ "$FAILED" -ne 0 ]; then
  echo; echo "Incomplete. Re-run this script (ENIs release up to 8h after runtime deletion)." >&2
  exit 1
fi
echo; echo "Teardown pass finished. Empty lists above mean nothing is left."
