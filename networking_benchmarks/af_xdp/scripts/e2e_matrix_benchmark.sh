#!/usr/bin/env bash
# e2e_matrix_benchmark.sh — Full E2E latency matrix benchmark across 3 scenarios.
#
# Terminates old instances, deploys CDK fleets, provisions, runs matrix tests,
# generates reports (heatmap + topology map), uploads to S3, destroys stacks.
#
# Usage:
#   ./e2e_matrix_benchmark.sh
#
# Prerequisites:
#   - AWS CLI configured (profile Groot, us-east-1)
#   - SSH key at ~/.ssh/virginia.pem
#   - CDK bootstrapped in account 038899712431/us-east-1
#   - jq, python3, ansible-playbook installed

set -euo pipefail

# ─── Config ───────────────────────────────────────────────────────────────────
export AWS_PROFILE="Groot"
export AWS_DEFAULT_REGION="us-east-1"
KEY_FILE="$HOME/.ssh/virginia.pem"
KEY_PAIR_NAME="virginia"
REPO_ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
CDK_DIR="${REPO_ROOT}/deployment/cdk"
SCRIPTS_DIR="${REPO_ROOT}/networking_benchmarks/af_xdp/scripts"
ANSIBLE_DIR="${REPO_ROOT}/deployment/ansible"
S3_BUCKET="cdk-hnb659fds-assets-038899712431-us-east-1"  # reuse CDK assets bucket
S3_PREFIX="benchmark-results/matrix"
LOCAL_RESULTS="${REPO_ROOT}/results/matrix_$(date +%Y%m%d)"
MESSAGES=100000
RATE=10000
WARMUP=10000

mkdir -p "$LOCAL_RESULTS"

echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  AF_XDP Latency Matrix — E2E Benchmark Suite                ║"
echo "╠══════════════════════════════════════════════════════════════╣"
echo "║  Account:  038899712431 (Groot)                             ║"
echo "║  Region:   us-east-1                                        ║"
echo "║  Key:      virginia                                         ║"
echo "║  Messages: ${MESSAGES} @ ${RATE}/s                          ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""

# ─── Step 0: Terminate old instances ─────────────────────────────────────────
echo "=== Step 0: Cleaning up old instances ==="
OLD_INSTANCES=$(aws ec2 describe-instances \
  --filters "Name=instance-state-name,Values=running,stopped" \
  --query 'Reservations[].Instances[].InstanceId' --output text)
if [[ -n "$OLD_INSTANCES" ]]; then
  echo "  Terminating: $OLD_INSTANCES"
  aws ec2 terminate-instances --instance-ids $OLD_INSTANCES --output text
  echo "  Waiting for termination..."
  aws ec2 wait instance-terminated --instance-ids $OLD_INSTANCES
  echo "  ✓ Old instances terminated"
else
  echo "  No existing instances to clean up"
fi

# ─── Helper functions ─────────────────────────────────────────────────────────
deploy_fleet() {
  local scenario_name="$1"
  local fleet_json="$2"
  echo ""
  echo "━━━ Deploying: ${scenario_name} ━━━"
  echo "  Fleet: ${fleet_json}"
  cd "$CDK_DIR"
  npx cdk deploy --app 'npx ts-node --prefer-ts-exts bin/af-xdp.ts' SingleRegionStack \
    --context keyPairName="$KEY_PAIR_NAME" \
    --context region=us-east-1 \
    --context "fleet=${fleet_json}" \
    --require-approval never --outputs-file /tmp/cdk-outputs.json 2>&1 | tail -5
  echo "  ✓ Stack deployed"
}

provision_fleet() {
  echo "  Provisioning (build + configure)..."
  cd "$ANSIBLE_DIR"
  # Discover hosts via dynamic inventory
  ansible-playbook af_xdp_configure.yaml \
    --key-file "$KEY_FILE" \
    -i inventory/af_xdp_inventory.aws_ec2.yml \
    2>&1 | tail -10
  echo "  ✓ Fleet provisioned"
}

run_matrix_test() {
  local scenario_name="$1"
  local result_dir="${LOCAL_RESULTS}/${scenario_name}"
  mkdir -p "$result_dir"

  echo "  Running matrix benchmark..."
  cd "$SCRIPTS_DIR"
  # Use the matrix runner (it auto-discovers fleet from CDK stack)
  ./run_matrix.sh \
    --key-file "$KEY_FILE" \
    --stack-name SingleRegionStack \
    --region us-east-1 \
    --messages "$MESSAGES" \
    --rate "$RATE" \
    --profile Groot 2>&1 | tee "${result_dir}/run.log" | tail -20

  # Copy results from the timestamped dir to our scenario dir
  LATEST_RESULT=$(ls -td "${SCRIPTS_DIR}/results/matrix_"* 2>/dev/null | head -1)
  if [[ -n "$LATEST_RESULT" ]]; then
    cp -r "$LATEST_RESULT"/* "$result_dir/"
    echo "  ✓ Results collected: ${result_dir}"
  else
    echo "  ⚠ No results directory found"
  fi
}

generate_reports() {
  local scenario_name="$1"
  local result_dir="${LOCAL_RESULTS}/${scenario_name}"
  echo "  Generating reports..."
  python3 "${SCRIPTS_DIR}/generate_matrix_report.py" "$result_dir" 2>&1 | tail -5
  echo "  ✓ Reports: ${result_dir}/matrix_report.html + topology_map.html"
}

upload_to_s3() {
  local scenario_name="$1"
  local result_dir="${LOCAL_RESULTS}/${scenario_name}"
  local s3_path="s3://${S3_BUCKET}/${S3_PREFIX}/$(date +%Y%m%d)/${scenario_name}"
  echo "  Uploading to S3: ${s3_path}"
  aws s3 cp "$result_dir" "$s3_path" --recursive --quiet
  echo "  ✓ Uploaded"
  # Generate presigned URLs for HTML reports
  for html in matrix_report.html topology_map.html; do
    if [[ -f "${result_dir}/${html}" ]]; then
      URL=$(aws s3 presign "${s3_path}/${html}" --expires-in 604800)
      echo "    ${html}: ${URL}"
    fi
  done
}

destroy_stack() {
  echo "  Destroying stack..."
  cd "$CDK_DIR"
  npx cdk destroy --app 'npx ts-node --prefer-ts-exts bin/af-xdp.ts' SingleRegionStack \
    --force 2>&1 | tail -3
  echo "  ✓ Stack destroyed"
}

# ─── Scenario 1: Simple (3× homogeneous) ─────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Scenario 1/3: SIMPLE — 3× c7i.xlarge (homogeneous CPG)    ║"
echo "╚══════════════════════════════════════════════════════════════╝"
FLEET_SIMPLE='[{"type":"c7i.xlarge","count":3}]'
deploy_fleet "simple" "$FLEET_SIMPLE"
provision_fleet
run_matrix_test "simple"
generate_reports "simple"
upload_to_s3 "simple"
destroy_stack

# ─── Scenario 2: Mediocre (6× heterogeneous) ─────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Scenario 2/3: MEDIOCRE — 6× heterogeneous (CPG)           ║"
echo "╚══════════════════════════════════════════════════════════════╝"
FLEET_MEDIOCRE='[{"type":"c7i.4xlarge","count":2},{"type":"c6in.4xlarge","count":2},{"type":"c7i.xlarge","count":2}]'
deploy_fleet "mediocre" "$FLEET_MEDIOCRE"
provision_fleet
run_matrix_test "mediocre"
generate_reports "mediocre"
upload_to_s3 "mediocre"
destroy_stack

# ─── Scenario 3: Advanced (12× heterogeneous medium) ──────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  Scenario 3/3: ADVANCED — 12× heterogeneous medium (CPG)   ║"
echo "╚══════════════════════════════════════════════════════════════╝"
FLEET_ADVANCED='[{"type":"c7i.4xlarge","count":3},{"type":"c6in.4xlarge","count":3},{"type":"c7i.xlarge","count":3},{"type":"m7i.xlarge","count":3}]'
deploy_fleet "advanced" "$FLEET_ADVANCED"
provision_fleet
run_matrix_test "advanced"
generate_reports "advanced"
upload_to_s3 "advanced"
destroy_stack

# ─── Summary ──────────────────────────────────────────────────────────────────
echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║  ALL SCENARIOS COMPLETE                                     ║"
echo "╠══════════════════════════════════════════════════════════════╣"
echo "║  Local results: ${LOCAL_RESULTS}                            "
echo "║  S3 results:    s3://${S3_BUCKET}/${S3_PREFIX}/$(date +%Y%m%d)/"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "Generated files per scenario:"
echo "  - matrix_report.html    (heatmap table)"
echo "  - topology_map.html     (interactive topology visualization)"
echo "  - matrix_summary.json   (machine-readable results)"
echo "  - fleet.json            (fleet metadata)"
echo "  - *_to_*.json           (per-pair raw results)"
echo ""
ls -la "$LOCAL_RESULTS"/*/topology_map.html 2>/dev/null
echo ""
echo "Done! Total time: $SECONDS seconds"
