#!/usr/bin/env bash
# Deploy one or more variants (one Terraform workspace each).
#   scripts/deploy.sh <variant> [<variant> ...]
#   scripts/deploy.sh all
#   PLAN_ONLY=1 scripts/deploy.sh <variant>     # terraform plan only, creates nothing
# Needs config.yaml, AWS credentials in the environment (AWS_PROFILE), terraform, python.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
PY="${PYTHON:-python}"
CFG="$PY scripts/variant_cfg.py"
PLAN_ONLY="${PLAN_ONLY:-0}"

[ $# -ge 1 ] || { echo "usage: $0 <variant>... | all" >&2; exit 2; }
if [ "$1" = "all" ]; then
  mapfile -t VARIANTS < <($CFG variants | tr -d '\r')
else
  VARIANTS=("$@")
fi

RESULTS="$($CFG get results_dir | tr -d '\r')"

if [ ! -f build/agent.zip ]; then
  echo "== packaging agent"
  $PY scripts/package_agent.py
fi

# Extra agents (echo, alloc, ...) each have their own zip, built once when missing.
for V in "${VARIANTS[@]}"; do
  A="$($CFG agent "$V" | tr -d '\r')"
  if [ "$A" != "agent" ] && [ ! -f "build/agent_$A.zip" ]; then
    echo "== packaging $A agent"
    $PY scripts/package_agent.py "$A"
  fi
done

terraform -chdir=infra init -input=false >/dev/null

for V in "${VARIANTS[@]}"; do
  echo "== variant $V"
  VARFILE="../build/variants/$V.tfvars.json"   # relative to infra/
  $CFG tfvars "$V" "build/variants/$V.tfvars.json"
  terraform -chdir=infra workspace select -or-create "$V" >/dev/null

  if [ "$PLAN_ONLY" = "1" ]; then
    terraform -chdir=infra plan -input=false -var-file="$VARFILE"
    continue
  fi

  terraform -chdir=infra apply -input=false -auto-approve -var-file="$VARFILE"
  terraform -chdir=infra output -json | $CFG save-outputs "$V"
  $CFG mark "$V" deployed_at
  echo "   outputs: $RESULTS/$V/outputs.json"
done

echo "== done. Run a smoke test, e.g.:"
echo "   python load/run_load.py --variant ${VARIANTS[0]} --shape idle"
