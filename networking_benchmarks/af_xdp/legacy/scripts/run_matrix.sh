#!/usr/bin/env bash
# run_matrix.sh - Full-mesh latency measurement across all nodes in a CPG fleet.
#
# Runs rtt from every node to every other node (NxN - diagonal),
# collecting results into a matrix directory for generate_matrix_report.py.
#
# Usage:
#   ./run_matrix.sh --key-file ~/.ssh/virginia.pem [--stack-name SingleRegionStack] \
#                   [--region us-east-1] [--messages 100000] [--rate 10000] \
#                   [--warmup 10000] [--port 9000]
#
# Fleet discovery: reads CDK outputs (FleetManifest + Node*PrivateIp/PublicIp).
# Falls back to EC2 describe-instances by Role=matrix-node tag.
#
# Prerequisites:
#   - rtt binary built on all nodes (via af_xdp_configure.yaml)
#   - replicator running on each target node (auto-started by this script)
#   - jq, aws CLI, ssh

set -euo pipefail

# ── Defaults ──────────────────────────────────────────────────────────────────
KEY_FILE=""
STACK_NAME="SingleRegionStack"
REGION="${AWS_DEFAULT_REGION:-us-east-1}"
MESSAGES=100000
RATE=10000
WARMUP=10000
PORT=9000
SEND_CPU=1
RECV_CPU=2
REPLICATOR_QUEUES=4
COOLDOWN=3
SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=10 -o LogLevel=ERROR"
REMOTE_BIN="~/gre-benchmark/networking_benchmarks/af_xdp"
PROFILE="${AWS_PROFILE:-}"

usage() {
  cat <<EOF
Usage: $(basename "$0") --key-file <path> [options]

Options:
  --key-file PATH        SSH private key (required)
  --stack-name NAME      CDK stack name (default: SingleRegionStack)
  --region REGION        AWS region (default: us-east-1)
  --messages N           Messages per test (default: 100000)
  --rate N               Messages/sec (default: 10000)
  --warmup N             Warmup messages (default: 10000)
  --port N               UDP port for replicator (default: 9000)
  --send-cpu N           CPU core for sender thread (default: 1)
  --recv-cpu N           CPU core for receiver thread (default: 2)
  --cooldown N           Seconds between tests (default: 3)
  --profile NAME         AWS CLI profile
  -h|--help              Show this help
EOF
  exit 0
}

# ── Parse args ────────────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --key-file)     KEY_FILE="$2"; shift 2 ;;
    --stack-name)   STACK_NAME="$2"; shift 2 ;;
    --region)       REGION="$2"; shift 2 ;;
    --messages)     MESSAGES="$2"; shift 2 ;;
    --rate)         RATE="$2"; shift 2 ;;
    --warmup)       WARMUP="$2"; shift 2 ;;
    --port)         PORT="$2"; shift 2 ;;
    --send-cpu)     SEND_CPU="$2"; shift 2 ;;
    --recv-cpu)     RECV_CPU="$2"; shift 2 ;;
    --cooldown)     COOLDOWN="$2"; shift 2 ;;
    --profile)      PROFILE="$2"; shift 2 ;;
    -h|--help)      usage ;;
    *)              echo "Unknown arg: $1"; usage ;;
  esac
done

[[ -z "$KEY_FILE" ]] && { echo "ERROR: --key-file is required"; usage; }

AWS_CMD="aws"
[[ -n "$PROFILE" ]] && AWS_CMD="aws --profile $PROFILE"

# ── Discover fleet ────────────────────────────────────────────────────────────
echo "=== Discovering fleet from CDK stack: ${STACK_NAME} (${REGION}) ==="

OUTPUTS=$($AWS_CMD cloudformation describe-stacks \
  --stack-name "$STACK_NAME" --region "$REGION" \
  --query 'Stacks[0].Outputs' --output json 2>/dev/null || echo "[]")

FLEET_SIZE=$(echo "$OUTPUTS" | jq -r '.[] | select(.OutputKey=="FleetSize") | .OutputValue // empty')

if [[ -z "$FLEET_SIZE" || "$FLEET_SIZE" == "null" ]]; then
  echo "No FleetManifest found — falling back to EC2 tag discovery..."
  # Fallback: discover by tag
  INSTANCES=$($AWS_CMD ec2 describe-instances --region "$REGION" \
    --filters "Name=tag:Role,Values=matrix-node" "Name=instance-state-name,Values=running" \
    --query 'Reservations[].Instances[].{InstanceId:InstanceId,PrivateIp:PrivateIpAddress,PublicIp:PublicIpAddress,Type:InstanceType}' \
    --output json)
  FLEET_SIZE=$(echo "$INSTANCES" | jq length)
  echo "Found ${FLEET_SIZE} instances via EC2 tags"
else
  echo "Fleet size: ${FLEET_SIZE}"
fi

# Build arrays of public IPs, private IPs, and instance types
declare -a PUBLIC_IPS PRIVATE_IPS INSTANCE_TYPES NODE_NAMES

if [[ -n "$(echo "$OUTPUTS" | jq -r '.[] | select(.OutputKey=="FleetManifest") | .OutputValue // empty')" ]]; then
  # CDK outputs mode
  for i in $(seq 0 $((FLEET_SIZE - 1))); do
    PUBLIC_IPS[$i]=$(echo "$OUTPUTS" | jq -r ".[] | select(.OutputKey==\"Node${i}PublicIp\") | .OutputValue")
    PRIVATE_IPS[$i]=$(echo "$OUTPUTS" | jq -r ".[] | select(.OutputKey==\"Node${i}PrivateIp\") | .OutputValue")
    MANIFEST=$(echo "$OUTPUTS" | jq -r '.[] | select(.OutputKey=="FleetManifest") | .OutputValue')
    INSTANCE_TYPES[$i]=$(echo "$MANIFEST" | jq -r ".[] | select(.index==$i) | .instanceType")
    NODE_NAMES[$i]="node-${i}-${INSTANCE_TYPES[$i]//./-}"
  done
else
  # EC2 tag discovery mode
  for i in $(seq 0 $((FLEET_SIZE - 1))); do
    PUBLIC_IPS[$i]=$(echo "$INSTANCES" | jq -r ".[$i].PublicIp")
    PRIVATE_IPS[$i]=$(echo "$INSTANCES" | jq -r ".[$i].PrivateIp")
    INSTANCE_TYPES[$i]=$(echo "$INSTANCES" | jq -r ".[$i].Type")
    NODE_NAMES[$i]="node-${i}-${INSTANCE_TYPES[$i]//./-}"
  done
fi

echo ""
echo "Fleet:"
for i in $(seq 0 $((FLEET_SIZE - 1))); do
  echo "  [${i}] ${NODE_NAMES[$i]} | public=${PUBLIC_IPS[$i]} private=${PRIVATE_IPS[$i]}"
done

# ── Setup results directory ───────────────────────────────────────────────────
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
RESULTS_DIR="$(cd "$(dirname "$0")" && pwd)/results/matrix_${TIMESTAMP}"
mkdir -p "$RESULTS_DIR"

# Save fleet metadata
cat > "${RESULTS_DIR}/fleet.json" <<EOF
{
  "timestamp": "${TIMESTAMP}",
  "region": "${REGION}",
  "stack": "${STACK_NAME}",
  "messages": ${MESSAGES},
  "rate": ${RATE},
  "warmup": ${WARMUP},
  "port": ${PORT},
  "nodes": [
$(for i in $(seq 0 $((FLEET_SIZE - 1))); do
  COMMA=""
  [[ $i -lt $((FLEET_SIZE - 1)) ]] && COMMA=","
  echo "    {\"index\": ${i}, \"name\": \"${NODE_NAMES[$i]}\", \"type\": \"${INSTANCE_TYPES[$i]}\", \"private_ip\": \"${PRIVATE_IPS[$i]}\", \"public_ip\": \"${PUBLIC_IPS[$i]}\"}${COMMA}"
done)
  ]
}
EOF

echo ""
echo "=== Matrix benchmark: ${FLEET_SIZE}x${FLEET_SIZE} = $((FLEET_SIZE * (FLEET_SIZE - 1))) directed pairs ==="
echo "Messages: ${MESSAGES} @ ${RATE} msg/sec + ${WARMUP} warmup"
echo "Results: ${RESULTS_DIR}"
echo ""

# ── Helper: SSH command ───────────────────────────────────────────────────────
ssh_cmd() {
  local ip="$1"; shift
  ssh $SSH_OPTS -i "$KEY_FILE" "ec2-user@${ip}" "$@"
}

# ── Start replicator on all nodes ─────────────────────────────────────────────
echo "--- Starting replicator on all nodes ---"
for i in $(seq 0 $((FLEET_SIZE - 1))); do
  echo "  [${i}] Starting replicator on ${NODE_NAMES[$i]}..."
  # Kill any existing replicator, then start fresh in background
  ssh_cmd "${PUBLIC_IPS[$i]}" "sudo pkill -f replicator 2>/dev/null || true; sleep 1; \
    cd ${REMOTE_BIN} && sudo nohup ./replicator eth0 ${PRIVATE_IPS[$i]} ${PORT} true --queues ${REPLICATOR_QUEUES} \
    > /tmp/replicator.log 2>&1 &" &
done
wait
echo "  Waiting for replicators to initialize..."
sleep 5

# ── Run full mesh ─────────────────────────────────────────────────────────────
PAIR_COUNT=0
TOTAL_PAIRS=$((FLEET_SIZE * (FLEET_SIZE - 1)))

for src in $(seq 0 $((FLEET_SIZE - 1))); do
  for dst in $(seq 0 $((FLEET_SIZE - 1))); do
    [[ $src -eq $dst ]] && continue
    PAIR_COUNT=$((PAIR_COUNT + 1))

    LABEL="${NODE_NAMES[$src]}_to_${NODE_NAMES[$dst]}"
    echo "[${PAIR_COUNT}/${TOTAL_PAIRS}] ${LABEL}"

    # Run rtt on src, targeting dst's private IP
    # Client listens on PORT+1 to avoid conflict with local replicator
    CLIENT_PORT=$((PORT + 1 + dst))
    ssh_cmd "${PUBLIC_IPS[$src]}" "cd ${REMOTE_BIN}/clients && \
      ./rtt ${PRIVATE_IPS[$dst]} ${PORT} ${PRIVATE_IPS[$src]} ${CLIENT_PORT} \
      ${MESSAGES} ${RATE} ${WARMUP} ${SEND_CPU} ${RECV_CPU}" \
      > "${RESULTS_DIR}/${LABEL}.txt" 2>&1 || true

    # Fetch JSON results
    ssh_cmd "${PUBLIC_IPS[$src]}" "cat /tm./rtt_results.json 2>/dev/null" \
      > "${RESULTS_DIR}/${LABEL}.json" 2>/dev/null || true

    sleep "$COOLDOWN"
  done
done

echo ""
echo "=== All ${TOTAL_PAIRS} pairs complete ==="

# ── Stop replicators ──────────────────────────────────────────────────────────
echo "Stopping replicators..."
for i in $(seq 0 $((FLEET_SIZE - 1))); do
  ssh_cmd "${PUBLIC_IPS[$i]}" "sudo pkill -f replicator 2>/dev/null || true" &
done
wait

# ── Generate report ───────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
if command -v python3 &>/dev/null; then
  echo ""
  echo "Generating matrix report..."
  python3 "${SCRIPT_DIR}/generate_matrix_report.py" "${RESULTS_DIR}" || true
fi

echo ""
echo "Done. Results: ${RESULTS_DIR}"
ls -la "${RESULTS_DIR}/"
