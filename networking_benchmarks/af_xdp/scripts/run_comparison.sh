#!/usr/bin/env bash
# run_comparison.sh - Run latency benchmark across scenarios and generate comparison report.
#
# Usage: ./run_comparison.sh <server_ip> <client_ip> <local_port> [config_file]
#
# Runs probe and mdp_client back-to-back for comparison,
# at multiple rates, collecting results into results/<timestamp>/.
# Generates a comparison report via generate_comparison_report.py.

set -euo pipefail

SERVER_IP="${1:?Usage: run_comparison.sh <server_ip> <client_ip> <local_port> [config]}"
CLIENT_IP="${2:?}"
LOCAL_PORT="${3:-9001}"
CONFIG="${4:-scenarios.yaml}"

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
RESULTS_DIR="${SCRIPT_DIR}/results/${TIMESTAMP}"
mkdir -p "$RESULTS_DIR"

# Default test parameters (override via scenarios.yaml if present)
MESSAGES=${MESSAGES:-100000}
WARMUP=${WARMUP:-10000}
RATES="${RATES:-1000 10000 50000 100000}"
SEND_CPU=${SEND_CPU:-1}
RECV_CPU=${RECV_CPU:-2}

echo "=== AF_XDP Latency Comparison ==="
echo "Server: ${SERVER_IP}:9000"
echo "Client: ${CLIENT_IP}:${LOCAL_PORT}"
echo "Messages: ${MESSAGES} + ${WARMUP} warmup"
echo "Rates: ${RATES}"
echo "Results: ${RESULTS_DIR}"
echo "=================================="

# Capture environment metadata
cat > "${RESULTS_DIR}/environment.json" <<EOF
{
  "timestamp": "${TIMESTAMP}",
  "server_ip": "${SERVER_IP}",
  "client_ip": "${CLIENT_IP}",
  "kernel": "$(uname -r)",
  "cpu": "$(lscpu | grep 'Model name' | sed 's/.*: *//')",
  "instance_type": "$(cat /sys/devices/virtual/dmi/id/board_name 2>/dev/null || echo unknown)"
}
EOF

run_old_client() {
    local rate=$1
    local label="old_client_${rate}mps"
    echo "[${label}] Running mdp_client @ ${rate} msg/sec..."
    "${SCRIPT_DIR}/mdp_client" \
        "${SERVER_IP}" 9000 "${CLIENT_IP}" "${LOCAL_PORT}" "${MESSAGES}" "${rate}" \
        > "${RESULTS_DIR}/${label}.txt" 2>&1 || true
    echo "[${label}] Done."
    sleep 2  # let replicator settle
}

run_new_client() {
    local rate=$1
    local label="probe_${rate}mps"
    echo "[${label}] Running probe @ ${rate} msg/sec..."
    "${SCRIPT_DIR}/probe" \
        "${SERVER_IP}" 9000 "${CLIENT_IP}" "$((LOCAL_PORT + 1))" \
        "${MESSAGES}" "${rate}" "${WARMUP}" "${SEND_CPU}" "${RECV_CPU}" \
        > "${RESULTS_DIR}/${label}.txt" 2>&1 || true
    # Copy JSON output
    cp /tmp/probe_results.json "${RESULTS_DIR}/${label}.json" 2>/dev/null || true
    echo "[${label}] Done."
    sleep 2
}

# Run tests interleaved (not all old then all new) to decorrelate from transient conditions
for rate in ${RATES}; do
    echo ""
    echo "--- Rate: ${rate} msg/sec ---"
    run_new_client "$rate"
    run_old_client "$rate"
done

echo ""
echo "=== All tests complete ==="
echo "Results in: ${RESULTS_DIR}"
ls -la "${RESULTS_DIR}/"

# Generate comparison report if Python is available
if command -v python3 &>/dev/null; then
    echo ""
    echo "Generating comparison report..."
    python3 "${SCRIPT_DIR}/generate_comparison_report.py" "${RESULTS_DIR}" || true
fi

echo "Done."
