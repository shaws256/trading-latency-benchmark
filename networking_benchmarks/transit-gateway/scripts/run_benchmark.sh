#!/bin/bash
set -euo pipefail

###############################################################################
# run_benchmark.sh - Orchestrate a TGW multicast benchmark via SSM Run Command
#
# Usage:
#   scripts/run_benchmark.sh --stack-name <name> [OPTIONS]      (see --help)
#
# The script queries the stack, waits for the instances, runs a clock and NIC
# preflight, runs the requested tools, and prints a raw text stream (see
# tgw_results.py for the format). Pipe or save that stream for save_report.py /
# collect_results.py. A multicast run that fails a validity gate (missing
# subscriber, loss, clock, sender rate) exits with status 3.
#
# Runs on the operator's machine, so it stays compatible with bash 3.2 (macOS):
# no associative arrays, no mapfile, guarded empty-array expansion.
###############################################################################

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
TGW_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------
STACK_NAME=""
GROUP="239.1.1.1"
PORT=5001
RATE=1000
DURATION=60
TOOLS=()
INSTANCE_TYPE=""
PLACEMENT="single-az-cpg"
NOTES=""
SAVE_REPORT=false
IMPL="native"
SENDER_IMPL=""
RECEIVER_IMPL=""
MAX_LOSS_PCT=2
MAX_CLOCK_OFFSET_US=10
DETAIL_DIR=""
RUNS_DIR=""
SENDER_CPU=""
TRANSPORT="ssm"
SSH_KEY=""
SSH_USER="ec2-user"

VALID_TOOLS=(mcast sockperf iperf3)
TOOL_ORDER=(mcast sockperf iperf3)

SSM_POLL_INTERVAL=10
SSM_POLL_MAX_ATTEMPTS=60
READY_POLL_INTERVAL=10
READY_POLL_MAX_ATTEMPTS=60
SSM_WAIT_INTERVAL=1
RECEIVER_SETTLE_SECS=5

NATIVE_BIN=/opt/benchmark-native/bin
EXIT_INVALID_RUN=3

usage() {
  cat <<EOF
Usage: $(basename "$0") --stack-name <name> [OPTIONS]

Orchestrate a TGW multicast benchmark via SSM Run Command.

Required:
  --stack-name NAME    CloudFormation stack name

Options:
  --group      ADDR    Multicast group address        [default: 239.1.1.1]
  --port       PORT    UDP port                        [default: 5001]
  --rate       RATE    Send rate (mps for sockperf,    [default: 1000]
                       pps for mcast, kbps for iperf3)
  --duration   SECS    Benchmark duration in seconds   [default: 60]
  --tool       TOOL[,..] Benchmark tool(s); repeatable or comma-separated.
                         mcast    - TGW multicast one-way latency (hardware RX stamps)
                         sockperf - unicast RTT baseline (no TGW)
                         iperf3   - unicast UDP throughput (no TGW, no latency)
                       [default: mcast,sockperf]  (always run mcast, sockperf, iperf3)
  --impl IMPL          Multicast sender and receiver implementation:
                       native (C++, built on the instances) or python
                       (scripts/mcast_*.py).           [default: native]
  --sender-impl IMPL   Override the sender only  (native|python)
  --receiver-impl IMPL Override the receiver only (native|python)
  --max-loss-pct PCT   Fail the run above this loss per subscriber  [default: 2]
  --max-clock-offset-us US
                       Fail the run when chrony System time exceeds this  [default: 10]
  --instance-type TYPE EC2 instance type (for report)  [default: auto-detect]
  --placement  STRAT   Placement strategy              [default: single-az-cpg]
  --notes      TEXT    Additional notes for report
  --detail-dir DIR     Keep the receivers' histogram files here
  --sender-cpu N       Pin the multicast sender to CPU N (taskset)  [default: unpinned]
  --transport T        How commands reach the instances: ssm (SSM Run Command) or
                       ssh (dev only: EC2 Instance Connect key push + one reused SSH
                       connection per instance; about 5x faster per command, needs
                       public IPs and port 22).               [default: ssm]
  --ssh-key PATH       Private key for --transport ssh (PATH.pub is pushed per run)
                       [default: a throwaway key generated for the run]
  --ssh-user USER      Login user for --transport ssh        [default: ec2-user]
  --runs-dir DIR       Where --save-report writes             [default: ../runs]
  --save-report        Save markdown report to runs/   [default: off]

A native implementation needs the instances to have built it at launch; the
script stops with the build log if they could not. It never falls back to Python.

Examples:
  scripts/run_benchmark.sh --stack-name MyBenchStack --tool mcast --save-report
  scripts/run_benchmark.sh --stack-name MyBenchStack --impl python
  scripts/run_benchmark.sh --stack-name MyBenchStack --sender-impl python --receiver-impl native
EOF
  exit 1
}

# ---------------------------------------------------------------------------
# Parse CLI arguments
# ---------------------------------------------------------------------------
while [[ $# -gt 0 ]]; do
  case "$1" in
    --stack-name)    STACK_NAME="$2";    shift 2 ;;
    --group)         GROUP="$2";         shift 2 ;;
    --port)          PORT="$2";          shift 2 ;;
    --rate)          RATE="$2";          shift 2 ;;
    --duration)      DURATION="$2";      shift 2 ;;
    --impl)          IMPL="$2";          shift 2 ;;
    --sender-impl)   SENDER_IMPL="$2";   shift 2 ;;
    --receiver-impl) RECEIVER_IMPL="$2"; shift 2 ;;
    --max-loss-pct)  MAX_LOSS_PCT="$2";  shift 2 ;;
    --max-clock-offset-us) MAX_CLOCK_OFFSET_US="$2"; shift 2 ;;
    --detail-dir)    DETAIL_DIR="$2";    shift 2 ;;
    --runs-dir)      RUNS_DIR="$2";      shift 2 ;;
    --sender-cpu)    SENDER_CPU="$2";    shift 2 ;;
    --transport)     TRANSPORT="$2";     shift 2 ;;
    --ssh-key)       SSH_KEY="$2";       shift 2 ;;
    --ssh-user)      SSH_USER="$2";      shift 2 ;;
    --tool)
      IFS=',' read -ra _parts <<< "$2"
      for _t in "${_parts[@]}"; do
        _t="$(echo "$_t" | tr -d '[:space:]')"
        [[ -z "$_t" ]] && continue
        TOOLS+=("$_t")
      done
      shift 2
      ;;
    --instance-type) INSTANCE_TYPE="$2"; shift 2 ;;
    --placement)     PLACEMENT="$2";     shift 2 ;;
    --notes)         NOTES="$2";         shift 2 ;;
    --save-report)   SAVE_REPORT=true;   shift ;;
    -h|--help)       usage ;;
    *)               echo "Unknown option: $1" >&2; usage ;;
  esac
done

if [[ -z "$STACK_NAME" ]]; then
  echo "ERROR: --stack-name is required" >&2
  usage
fi

[[ ${#TOOLS[@]} -eq 0 ]] && TOOLS=(mcast sockperf)

for _t in "${TOOLS[@]}"; do
  _ok=false
  for _v in "${VALID_TOOLS[@]}"; do
    [[ "$_t" == "$_v" ]] && { _ok=true; break; }
  done
  if [[ "$_ok" != "true" ]]; then
    echo "ERROR: unknown --tool '$_t'. Valid choices: ${VALID_TOOLS[*]}" >&2
    exit 1
  fi
done

# Values reach remote shell commands, so they are checked here.
require_int() { [[ "$2" =~ ^[0-9]+$ ]] && [[ "$2" -gt 0 ]] || { echo "ERROR: $1 must be a positive integer, got '$2'" >&2; exit 1; }; }
require_num() { [[ "$2" =~ ^[0-9]+(\.[0-9]+)?$ ]] || { echo "ERROR: $1 must be a number, got '$2'" >&2; exit 1; }; }
require_int --port "$PORT"
require_int --rate "$RATE"
require_int --duration "$DURATION"
[[ "$TRANSPORT" == "ssm" || "$TRANSPORT" == "ssh" ]] || { echo "ERROR: --transport must be ssm or ssh, got '$TRANSPORT'" >&2; exit 1; }
[[ "$SSH_USER" =~ ^[a-z_][a-z0-9_-]*$ ]] || { echo "ERROR: --ssh-user has unexpected characters" >&2; exit 1; }
[[ -z "$SENDER_CPU" || "$SENDER_CPU" =~ ^[0-9]+$ ]] || { echo "ERROR: --sender-cpu must be a CPU number, got '$SENDER_CPU'" >&2; exit 1; }
require_num --max-loss-pct "$MAX_LOSS_PCT"
require_num --max-clock-offset-us "$MAX_CLOCK_OFFSET_US"
[[ "$GROUP" =~ ^[0-9]{1,3}(\.[0-9]{1,3}){3}$ ]] || { echo "ERROR: --group must be an IPv4 address, got '$GROUP'" >&2; exit 1; }
[[ "$STACK_NAME" =~ ^[A-Za-z0-9-]+$ ]] || { echo "ERROR: --stack-name has unexpected characters" >&2; exit 1; }

[[ -z "$SENDER_IMPL" ]] && SENDER_IMPL="$IMPL"
[[ -z "$RECEIVER_IMPL" ]] && RECEIVER_IMPL="$IMPL"
for _i in "$SENDER_IMPL" "$RECEIVER_IMPL"; do
  [[ "$_i" == "native" || "$_i" == "python" ]] || { echo "ERROR: implementation must be native or python, got '$_i'" >&2; exit 1; }
done

RUN_TOOLS=()
for _v in "${TOOL_ORDER[@]}"; do
  for _t in "${TOOLS[@]}"; do
    if [[ "$_t" == "$_v" ]]; then
      RUN_TOOLS+=("$_v")
      break
    fi
  done
done

tools_has() {
  local needle="$1" t
  for t in "${RUN_TOOLS[@]}"; do
    [[ "$t" == "$needle" ]] && return 0
  done
  return 1
}

NEEDS_NATIVE=false
tools_has mcast && { [[ "$SENDER_IMPL" == "native" || "$RECEIVER_IMPL" == "native" ]] && NEEDS_NATIVE=true; }
NEEDS_PYTHON=false
tools_has mcast && { [[ "$SENDER_IMPL" == "python" || "$RECEIVER_IMPL" == "python" ]] && NEEDS_PYTHON=true; }

[[ -z "$RUNS_DIR" ]] && RUNS_DIR="$TGW_DIR/runs"
RUN_ID="$(date -u +%Y%m%dT%H%M%SZ)-$$"
WORK="$(mktemp -d "${TMPDIR:-/tmp}/tgw_bench.XXXXXXXX")"
trap cleanup EXIT
RAW_OUTPUT_FILE="$WORK/raw.txt"
KEEP_DETAIL=false
if [[ -n "$DETAIL_DIR" ]]; then
  KEEP_DETAIL=true
elif [[ "$SAVE_REPORT" == "true" ]]; then
  DETAIL_DIR="$RUNS_DIR/$RUN_ID.details"
  KEEP_DETAIL=true
else
  DETAIL_DIR="$WORK/details"
fi


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
# stack_value <OutputKey|@StackId>: value from the describe-stacks document saved in $WORK/stack.json.
stack_value() {
  python3 - "$WORK/stack.json" "$1" <<'PY'
import json, sys
d = json.load(open(sys.argv[1]))["Stacks"][0]
key = sys.argv[2]
if key == "@StackId":
    print(d["StackId"])
else:
    print(next((o["OutputValue"] for o in d.get("Outputs", []) if o["OutputKey"] == key), ""))
PY
}

get_stack_output() {
  aws cloudformation describe-stacks \
    --stack-name "$STACK_NAME" \
    --query "Stacks[0].Outputs[?OutputKey=='${1}'].OutputValue" \
    --output text
}

# ssm_send_command <instance> <command> <timeout-secs>  -> prints the command id
# The command travels as a JSON string built by python, so quotes and newlines are safe.
ssm_send_command() {
  local params
  params=$(python3 -c 'import json,sys; print(json.dumps({"commands":[sys.argv[1]],"executionTimeout":[sys.argv[2]]}))' "$2" "$3")
  aws ssm send-command \
    --instance-ids "$1" \
    --document-name "AWS-RunShellScript" \
    --parameters "$params" \
    --query "Command.CommandId" \
    --output text
}

# ssm_exec <instance> <timeout-secs> <command>: the SSM backend of remote_run.
REMOTE_OUT=""
REMOTE_CALLS=0
# phase_mark <name>: one "# timing" line per phase with the elapsed seconds and remote calls since the last mark.
PHASE_T0=$SECONDS
PHASE_CALLS0=0
phase_mark() {
  echo "# timing: $1 took $((SECONDS - PHASE_T0))s, $((REMOTE_CALLS - PHASE_CALLS0)) remote calls"
  PHASE_T0=$SECONDS
  PHASE_CALLS0=$REMOTE_CALLS
}
# One get-command-invocation call per poll returns the status and the output together.
ssm_exec() {
  local id elapsed=0 max=$(($2 + 30)) resp
  REMOTE_OUT=""
  REMOTE_STATUS=""
  id=$(ssm_send_command "$1" "$3" "$2") || return 1
  while [[ $elapsed -lt $max ]]; do
    resp=$(aws ssm get-command-invocation --command-id "$id" --instance-id "$1" --output json 2>/dev/null \
      | python3 -c '
import json, sys
try:
    d = json.load(sys.stdin)
    sys.stdout.write(d.get("Status", "Pending") + "\n" + d.get("StandardOutputContent", ""))
except ValueError:
    sys.stdout.write("Pending")
') || resp="Pending"
    REMOTE_STATUS="${resp%%$'\n'*}"
    if [[ "$resp" == *$'\n'* ]]; then REMOTE_OUT="${resp#*$'\n'}"; else REMOTE_OUT=""; fi
    case "$REMOTE_STATUS" in
      Success) return 0 ;;
      Failed|TimedOut|Cancelled)
        echo "ERROR: SSM command $id on $1 finished with status: $REMOTE_STATUS" >&2
        return 1
        ;;
    esac
    sleep "$SSM_WAIT_INTERVAL"
    elapsed=$((elapsed + SSM_WAIT_INTERVAL))
  done
  REMOTE_STATUS="WaitTimeout"
  echo "ERROR: Timed out waiting for SSM command $id on $1" >&2
  return 1
}

# ---------------------------------------------------------------------------
# SSH backend (dev): EC2 Instance Connect pushes a throwaway public key (valid for 60 s, which
# only has to cover opening the connection) and one SSH connection per instance is reused for
# every command. The command text goes in on stdin to `sudo bash -s`: SSM Run Command runs as
# root, and the scripts need root (clock step, NIC timestamping, /sys). Because the text is on
# stdin, quoting is not an issue and the remote process list never contains it (pkill -f stays safe).
# ---------------------------------------------------------------------------
SSH_CTL_DIR=""
SSH_BASE=()
ssh_setup() {
  local t
  for t in ssh ssh-keygen perl; do
    command -v "$t" >/dev/null 2>&1 || { echo "ERROR: --transport ssh needs '$t' on this machine" >&2; exit 1; }
  done
  SSH_CTL_DIR=$(mktemp -d /tmp/tgwcm.XXXXXX)     # short path: unix socket paths are limited to ~104 bytes
  if [[ -z "$SSH_KEY" ]]; then
    SSH_KEY="$SSH_CTL_DIR/key"
    ssh-keygen -q -t ed25519 -N "" -f "$SSH_KEY" >/dev/null
  elif [[ ! -f "$SSH_KEY" || ! -f "$SSH_KEY.pub" ]]; then
    echo "ERROR: --ssh-key needs both $SSH_KEY and $SSH_KEY.pub" >&2
    exit 1
  fi
  SSH_BASE=(-i "$SSH_KEY" -o IdentitiesOnly=yes -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null
            -o LogLevel=ERROR -o BatchMode=yes -o ConnectTimeout=15 -o ServerAliveInterval=15 -o ServerAliveCountMax=4
            -o ControlMaster=auto -o ControlPersist=300 -o "ControlPath=$SSH_CTL_DIR/%C")
}

ssh_push_key() {
  aws ec2-instance-connect send-ssh-public-key --instance-id "$1" --instance-os-user "$SSH_USER" \
    --ssh-public-key "file://$SSH_KEY.pub" --query Success --output text >/dev/null
}

# ssh_exec <instance> <timeout-secs> <command>: the SSH backend of remote_run.
ssh_exec() {
  local id="$1" ip rc=0 retried=0
  REMOTE_OUT=""
  REMOTE_STATUS=""
  ip=$(inst_field "$id" 6)
  if [[ -z "$ip" || "$ip" == "None" ]]; then
    echo "ERROR: $id has no public IP; use --transport ssm" >&2
    REMOTE_STATUS="Failed"
    return 1
  fi
  while true; do
    rc=0
    REMOTE_OUT=$(printf '%s\n' "$3" | perl -e 'alarm shift; exec @ARGV' "$(($2 + 30))" \
      ssh "${SSH_BASE[@]}" "$SSH_USER@$ip" sudo bash -s 2> "$WORK/ssh.err.$id") || rc=$?
    # 255 is a connection or authentication failure. The pushed key only authorizes a new
    # connection for 60 s, so push again and retry once.
    if [[ $rc -eq 255 && $retried -eq 0 ]]; then
      retried=1
      ssh_push_key "$id" || true
      continue
    fi
    break
  done
  case $rc in
    0) REMOTE_STATUS="Success"; return 0 ;;
    142) REMOTE_STATUS="TimedOut"; echo "ERROR: command on $id timed out" >&2; return 1 ;;
    *) REMOTE_STATUS="Failed"
       echo "ERROR: command on $id failed (exit $rc)" >&2
       [[ -s "$WORK/ssh.err.$id" ]] && sed 's/^/    ssh: /' "$WORK/ssh.err.$id" >&2
       return 1 ;;
  esac
}

# remote_run <instance> <timeout-secs> <command>: run a shell script on an instance over the
# selected transport. Stdout goes to REMOTE_OUT, the outcome to REMOTE_STATUS; returns 1 unless
# it succeeded. Callers that tolerate failure append `|| true`.
remote_run() {
  REMOTE_CALLS=$((REMOTE_CALLS + 1))
  if [[ "$TRANSPORT" == "ssh" ]]; then ssh_exec "$@"; else ssm_exec "$@"; fi
}

cleanup() {
  local sock
  if [[ -n "$SSH_CTL_DIR" && -d "$SSH_CTL_DIR" ]]; then
    for sock in "$SSH_CTL_DIR"/*; do
      [[ -S "$sock" ]] && ssh -o "ControlPath=$sock" -O exit placeholder >/dev/null 2>&1
    done
    rm -rf "$SSH_CTL_DIR"
  fi
  rm -rf "$WORK"
}

# par_run <function> <id...>: run "<function> <id>" for every id at the same time. Each one's
# output is shown in id order afterwards; returns 1 if any failed. Workers run in subshells,
# so they hand results back through files under $WORK.
par_run() {
  local fn="$1" id rc=0 i
  local pids=() ids=()
  shift
  for id in "$@"; do
    ( "$fn" "$id" ) > "$WORK/par.$fn.$id" 2>&1 &
    pids+=($!)
    ids+=("$id")
  done
  for i in "${!pids[@]}"; do
    wait "${pids[$i]}" || rc=1
    cat "$WORK/par.$fn.${ids[$i]}"
  done
  return $rc
}

# remote_upload <instance> <local file> <remote path>
remote_upload() {
  local b64
  b64=$(python3 -c 'import base64,sys; sys.stdout.write(base64.b64encode(open(sys.argv[1],"rb").read()).decode())' "$2")
  remote_run "$1" 30 "echo '$b64' | base64 -d > '$3' && chmod +x '$3' && echo UPLOADED" || return 1
  [[ "$REMOTE_OUT" == *UPLOADED* ]]
}

# One describe-instances call fills $WORK/inst.tsv: id, availability zone, private IP,
# AMI, instance type, public IP (tab separated).
load_instances() {
  aws ec2 describe-instances --instance-ids "$@" \
    --query "Reservations[].Instances[].[InstanceId,Placement.AvailabilityZone,PrivateIpAddress,ImageId,InstanceType,PublicIpAddress]" \
    --output text > "$WORK/inst.tsv"
}
inst_field() { awk -F'\t' -v id="$1" -v c="$2" '$1 == id {print $c; exit}' "$WORK/inst.tsv"; }
get_private_ip() { inst_field "$1" 3; }
get_az() { inst_field "$1" 2; }

# emit_clock_block <instance> <pre|post> <file with key=value lines>
emit_clock_block() {
  echo "--- CLOCK $1 $2 ---"
  grep '=' "$3" || true
  echo "--- END CLOCK ---"
}

# result_ok <file>: true when the file holds one JSON object without an "error" key.
result_ok() {
  python3 - "$1" <<'PY'
import json, sys
try:
    d = json.loads(open(sys.argv[1]).read())
    sys.exit(0 if isinstance(d, dict) and d and not d.get("error") else 1)
except Exception:
    sys.exit(1)
PY
}

# emit_context_block <instance> <ami> <file with key=value lines>
emit_context_block() {
  echo "--- CONTEXT $1 ---"
  echo "ami=$2"
  grep '=' "$3" || true
  echo "--- END CONTEXT ---"
}

# emit_mcast_block <instance> <az> <az-relation> <file with the receiver's stdout>
emit_mcast_block() {
  echo "--- MCAST $1 ---"
  python3 - "$1" "$2" "$3" "$4" <<'PY'
import json, sys
sid, az, rel, path = sys.argv[1:5]
raw = open(path).read().strip()
doc = {"subscriber_id": sid, "az": az, "az_relation": rel}
if not raw:
    doc["error"] = "no result returned"
else:
    try:
        doc["result"] = json.loads(raw)
    except ValueError:
        doc["error"] = "result is not valid JSON: " + raw[:200]
print(json.dumps(doc))
PY
  echo "--- END MCAST $1 ---"
}

# ---------------------------------------------------------------------------
# Per-instance workers for par_run. They read run_all's variables (bash scopes locals
# dynamically) and write what the caller needs to files under $WORK.
# ---------------------------------------------------------------------------
ssh_open_worker() {
  local id="$1"
  ssh_push_key "$id" || { echo "ERROR: could not push an SSH key to $id (needs ec2-instance-connect:SendSSHPublicKey)" >&2; return 1; }
  remote_run "$id" 30 "echo SSH=OK" || return 1
  [[ "$REMOTE_OUT" == *SSH=OK* ]] && echo "  $id - SSH connected"
}

ready_worker() {
  local id="$1" attempt=0
  while true; do
    remote_run "$id" 30 "$READY_CMD" || true
    if [[ "$REMOTE_OUT" == *"ENV=READY"* ]]; then
      printf '%s\n' "$REMOTE_OUT" > "$WORK/ready.$id"
      echo "  $id - environment ready"
      return 0
    fi
    attempt=$((attempt + 1))
    if [[ $attempt -ge $READY_POLL_MAX_ATTEMPTS ]]; then
      echo "ERROR: $id environment not ready after $((READY_POLL_MAX_ATTEMPTS * READY_POLL_INTERVAL))s" >&2
      return 1
    fi
    sleep "$READY_POLL_INTERVAL"
  done
}

# Upload preflight.sh, run the gate, then record the host state, in one SSM call per instance.
pre_worker() {
  local id="$1" b64 out
  b64=$(python3 -c 'import base64,sys; sys.stdout.write(base64.b64encode(open(sys.argv[1],"rb").read()).decode())' "$SCRIPT_DIR/preflight.sh")
  remote_run "$id" 90 "echo '$b64' | base64 -d > /tmp/preflight.sh && chmod +x /tmp/preflight.sh && bash /tmp/preflight.sh --max-offset-us $MAX_CLOCK_OFFSET_US; echo '=====CONTEXT====='; bash /tmp/preflight.sh --context" || true
  out="$REMOTE_OUT"
  printf '%s\n' "${out%%=====CONTEXT=====*}" > "$WORK/pre.$id"
  if [[ "$out" == *"=====CONTEXT====="* ]]; then
    printf '%s\n' "${out#*=====CONTEXT=====}" > "$WORK/ctx.$id"
  else
    : > "$WORK/ctx.$id"
  fi
  inst_field "$id" 4 > "$WORK/ami.$id"
}

py_upload_worker() {
  local id="$1"
  remote_upload "$id" "$SCRIPT_DIR/mcast_recv.py" /tmp/mcast_recv.py || { echo "ERROR: upload to $id failed" >&2; return 1; }
  remote_upload "$id" "$SCRIPT_DIR/mcast_send.py" /tmp/mcast_send.py || { echo "ERROR: upload to $id failed" >&2; return 1; }
}

# Start a subscriber's receiver and wait until it is listening; one retry, then fail.
recv_worker() {
  local id="$1" attempt=0 status
  while true; do
    attempt=$((attempt + 1))
    remote_run "$id" 90 "$RECV_START_CMD" || true
    status=$(echo "$REMOTE_OUT" | grep -m1 '^RECV=' || true)
    if [[ "$status" == "RECV=READY" ]]; then
      echo "  $id - multicast receiver ready"
      return 0
    fi
    if [[ $attempt -ge 2 ]]; then
      echo "ERROR: receiver on $id did not become ready after $attempt attempts:" >&2
      echo "$REMOTE_OUT" | sed 's/^/    /' >&2
      return 1
    fi
    echo "  $id - receiver not ready (${status:-no status}); retrying once"
  done
}

# After the send, one command per instance: a subscriber waits for its receiver to finish, prints
# its result, uploads its detail file and reads the clock; the publisher only reads the clock.
collect_worker() {
  local id="$1" rest res det
  if [[ "$id" == "$PUBLISHER_ID" ]]; then
    remote_run "$id" 30 "bash /tmp/preflight.sh --clock" || true
    printf '%s\n' "$REMOTE_OUT" > "$WORK/post.$id"
    return 0
  fi
  remote_run "$id" 120 "${COLLECT_CMD//@ID@/$id}" || true
  [[ "$REMOTE_OUT" == *"RECV=INTERRUPTED"* ]] && echo "  $id - receiver had to be interrupted (end marker never arrived?)"
  rest="${REMOTE_OUT#*=====RESULT=====}"
  printf '%s\n' "${rest%%=====DETAIL=====*}" > "$WORK/res.$id"
  rest="${rest#*=====DETAIL=====}"
  det="${rest%%=====CLOCK=====*}"
  printf '%s\n' "${rest#*=====CLOCK=====}" > "$WORK/post.$id"
  if [[ "$RECEIVER_IMPL" == "native" && "$det" != *"DETAIL=UPLOADED"* ]]; then
    echo "  WARN $id - detail file not uploaded; pooled percentiles will be unavailable"
  fi
  return 0
}

# ---------------------------------------------------------------------------
# The run itself. Its output is the raw stream; the verdict is computed from it afterwards.
# ---------------------------------------------------------------------------
run_all() {
  set -eo pipefail
  local SENDER_TIMEOUT=$((DURATION + 30))
  local instance_id sub_id

  echo "=== TGW Multicast Benchmark ==="
  echo "Stack:    $STACK_NAME"
  echo "Run-ID:   $RUN_ID"
  echo "Tools:    ${RUN_TOOLS[*]}"
  echo "Group:    $GROUP"
  echo "Port:     $PORT"
  echo "Rate:     $RATE"
  echo "Duration: ${DURATION}s"
  echo "Impl:     sender=$SENDER_IMPL receiver=$RECEIVER_IMPL"
  echo ""

  # --- Step 1: stack outputs ---
  echo ">>> Querying CloudFormation stack outputs..."
  local PUBLISHER_ID SUBSCRIBER_IDS_CSV S3_BUCKET STACK_ID REGION STACK_GROUP STACK_PORT
  # One describe-stacks call carries every output and the stack id.
  aws cloudformation describe-stacks --stack-name "$STACK_NAME" --output json > "$WORK/stack.json"
  PUBLISHER_ID=$(stack_value PublisherInstanceId)
  SUBSCRIBER_IDS_CSV=$(stack_value SubscriberInstanceIds)
  S3_BUCKET=$(stack_value S3BucketName)
  STACK_GROUP=$(stack_value MulticastGroup)
  STACK_PORT=$(stack_value MulticastPort)
  STACK_ID=$(stack_value @StackId)
  REGION=$(echo "$STACK_ID" | cut -d: -f4)
  echo "  Stack multicast group: $STACK_GROUP"
  echo "  Stack multicast port:  $STACK_PORT"
  local SUBSCRIBER_IDS ALL_INSTANCE_IDS
  IFS=',' read -ra SUBSCRIBER_IDS <<< "$SUBSCRIBER_IDS_CSV"
  ALL_INSTANCE_IDS=("$PUBLISHER_ID" "${SUBSCRIBER_IDS[@]}")

  load_instances "${ALL_INSTANCE_IDS[@]}"
  local ITYPE="$INSTANCE_TYPE"
  [[ -z "$ITYPE" ]] && ITYPE=$(inst_field "$PUBLISHER_ID" 5)
  echo "Instance-Type: $ITYPE"
  echo "Transport: $TRANSPORT"
  echo "  Publisher:   $PUBLISHER_ID"
  echo "  Subscribers: ${SUBSCRIBER_IDS[*]}"
  echo "  S3 Bucket:   $S3_BUCKET"
  echo ""

  phase_mark "stack query"
  # --- Step 2: SSM online ---
  echo ">>> Waiting for all instances to be SSM-ready..."
  local attempt online missing
  if [[ "$TRANSPORT" == "ssh" ]]; then
    par_run ssh_open_worker "${ALL_INSTANCE_IDS[@]}" || exit 1
  else
      attempt=0
    while true; do
      online=$(aws ssm describe-instance-information \
        --filters "Key=InstanceIds,Values=$(IFS=,; echo "${ALL_INSTANCE_IDS[*]}")" \
        --query "InstanceInformationList[?PingStatus=='Online'].InstanceId" --output text 2>/dev/null || echo "")
      missing=""
      for instance_id in "${ALL_INSTANCE_IDS[@]}"; do
        [[ " ${online//[$'\t']/ } " == *" $instance_id "* ]] || missing="$missing $instance_id"
      done
      if [[ -z "$missing" ]]; then
        for instance_id in "${ALL_INSTANCE_IDS[@]}"; do echo "  $instance_id - SSM online"; done
        break
      fi
      attempt=$((attempt + 1))
      if [[ $attempt -ge $SSM_POLL_MAX_ATTEMPTS ]]; then
        echo "ERROR:$missing did not become SSM-ready after $((SSM_POLL_MAX_ATTEMPTS * SSM_POLL_INTERVAL))s" >&2
        exit 1
      fi
      sleep "$SSM_POLL_INTERVAL"
    done
  fi
  echo ""

  phase_mark "transport check"
  # --- Step 3: environment readiness, and native build state ---
  echo ">>> Waiting for benchmark environment readiness on all instances..."
  # shellcheck disable=SC2016
  local READY_CMD='if [ -f /opt/benchmark-env-ready ] && command -v sockperf >/dev/null 2>&1 && command -v iperf3 >/dev/null 2>&1; then echo ENV=READY; else echo ENV=NOTREADY; fi
if [ -f /opt/benchmark-native-ready ]; then echo NATIVE=ready; echo "NATIVE_BUILD=$(cat /opt/benchmark-native/BUILD_INFO 2>/dev/null)"
elif [ -f /opt/benchmark-native-failed ]; then echo NATIVE=failed; else echo NATIVE=absent; fi'
  local native_commit first_commit="" first_build="" native_state build_line
  par_run ready_worker "${ALL_INSTANCE_IDS[@]}" || exit 1
  for instance_id in "${ALL_INSTANCE_IDS[@]}"; do
    REMOTE_OUT=$(cat "$WORK/ready.$instance_id")
    if [[ "$NEEDS_NATIVE" == "true" ]]; then
      native_state=$(echo "$REMOTE_OUT" | sed -n 's/^NATIVE=//p' | head -1)
      if [[ "$native_state" != "ready" ]]; then
        echo "ERROR: $instance_id has no native build (state: ${native_state:-unknown})." >&2
        echo "       Refusing to continue: a silent fallback to Python would change what is measured." >&2
        if remote_run "$instance_id" 30 "tail -n 20 /var/log/benchmark-native-build.log 2>/dev/null; true"; then
          echo "$REMOTE_OUT" | sed 's/^/       | /' >&2
        fi
        echo "       Fix the build (native_repo / native_ref must be reachable and contain the sources)" >&2
        echo "       or run with --impl python." >&2
        exit 1
      fi
      build_line=$(echo "$REMOTE_OUT" | sed -n 's/^NATIVE_BUILD=//p' | head -1)
      native_commit=$(echo "$build_line" | sed -n 's/^commit=\([0-9a-f]*\).*/\1/p')
      if [[ -z "$first_commit" ]]; then
        first_commit="$native_commit"
        first_build="$build_line"
      elif [[ "$native_commit" != "$first_commit" ]]; then
        echo "ERROR: instances built different native commits ($first_commit vs $native_commit)." >&2
        exit 1
      fi
    fi
  done
  [[ -n "$first_build" ]] && echo "Native-Build: $first_build"
  echo ""

  phase_mark "environment readiness"
  # --- Step 4: preflight (clock + NIC), identical script on every instance ---
  echo ">>> Preflight: PHC, hardware timestamping and clock offset on all instances..."
  local PTP_FAIL=0
  par_run pre_worker "${ALL_INSTANCE_IDS[@]}" || true
  for instance_id in "${ALL_INSTANCE_IDS[@]}"; do
    if grep -q '^PASS' "$WORK/pre.$instance_id"; then
      echo "  $instance_id - clock and NIC ready ($(grep '^system_time_us=' "$WORK/pre.$instance_id" | head -1))"
    else
      echo "  FAIL $instance_id - preflight failed:" >&2
      sed 's/^/    /' "$WORK/pre.$instance_id" >&2
      PTP_FAIL=1
    fi
  done
  if [[ $PTP_FAIL -ne 0 ]]; then
    echo "" >&2
    echo "ERROR: preflight failed. Either hardware timestamps are unavailable or the clock is not" >&2
    echo "       disciplined to the local PHC; the one-way measurement would be invalid." >&2
    echo "       Run: scripts/check_ptp.sh --stack-name $STACK_NAME for diagnostics." >&2
    exit 1
  fi
  echo "  All instances passed preflight."
  echo ""
  for instance_id in "${ALL_INSTANCE_IDS[@]}"; do
    emit_clock_block "$instance_id" pre "$WORK/pre.$instance_id"
  done
  echo ""
  phase_mark "preflight"
  # The host and NIC state the numbers were taken under (read-only; collected with the preflight).
  for instance_id in "${ALL_INSTANCE_IDS[@]}"; do
    emit_context_block "$instance_id" "$(cat "$WORK/ami.$instance_id")" "$WORK/ctx.$instance_id"
  done
  echo ""
  phase_mark "context"

  # --- mcast ---
  if tools_has mcast; then
    local send_bin recv_cmd start_cmd
    if [[ "$NEEDS_PYTHON" == "true" ]]; then
      echo ">>> Uploading Python multicast helpers..."
      par_run py_upload_worker "${ALL_INSTANCE_IDS[@]}" || exit 1
      echo "  Scripts uploaded."
      echo ""
    fi

    local PUB_AZ PUB_IP
    PUB_AZ=$(get_az "$PUBLISHER_ID")
    PUB_IP=$(get_private_ip "$PUBLISHER_ID")

    echo ">>> Phase A: Multicast one-way latency (${RATE} pps, ${DURATION}s, sender=$SENDER_IMPL receiver=$RECEIVER_IMPL)..."

    if [[ "$RECEIVER_IMPL" == "native" ]]; then
      recv_cmd="$NATIVE_BIN/mcast_recv $GROUP $PORT $SENDER_TIMEOUT"
    else
      recv_cmd="python3 /tmp/mcast_recv.py $GROUP $PORT $SENDER_TIMEOUT"
    fi
    # The stop command is separate from the start command so that neither script text contains
    # both the pkill pattern and the receiver path (pkill -f could otherwise match its own shell).
    stop_cmd="pkill -f '[m]cast_recv' 2>/dev/null || true; sleep 1; rm -f /tmp/mcast_recv.ready /tmp/mcast_result.json /tmp/mcast_recv_detail.json /tmp/mcast_recv.err; echo STOPPED"
    start_cmd="nohup $recv_cmd > /tmp/mcast_result.json 2> /tmp/mcast_recv.err < /dev/null & echo STARTED"
    # Wait for the receiver to be listening. Native receivers write a ready file after they
    # join the group and enable stamping; the Python receiver has no such signal, so it gets
    # a fixed settle time and a liveness check.
    local READY_WAIT_CMD RECV_START_CMD COLLECT_CMD
    if [[ "$RECEIVER_IMPL" == "native" ]]; then
      READY_WAIT_CMD="for i in \$(seq 1 50); do [ -f /tmp/mcast_recv.ready ] && { echo RECV=READY; exit 0; }; pgrep -f '[m]cast_recv' >/dev/null || { echo RECV=DEAD; cat /tmp/mcast_result.json /tmp/mcast_recv.err 2>/dev/null; exit 0; }; sleep 0.2; done; echo RECV=TIMEOUT"
    else
      READY_WAIT_CMD="sleep $RECEIVER_SETTLE_SECS; pgrep -f '[m]cast_recv' >/dev/null && echo RECV=READY || { echo RECV=DEAD; cat /tmp/mcast_result.json /tmp/mcast_recv.err 2>/dev/null; }"
    fi

    # Stop any old receiver, start this one and wait until it is listening: one command, one round trip.
    RECV_START_CMD="$stop_cmd"$'\n'"$start_cmd"$'\n'"$READY_WAIT_CMD"
    # The whole batch (start receivers, send, collect) runs again once if the sender or any
    # subscriber produced no usable result; the second outcome is the one reported.
    local batch=0 incomplete s_id sender_out sender_ok
    while true; do
    batch=$((batch + 1))
    par_run recv_worker "${SUBSCRIBER_IDS[@]}" || exit 1
    echo "  Sending ${RATE} pps multicast for ${DURATION}s..."
    if [[ "$SENDER_IMPL" == "native" ]]; then
      send_bin="$NATIVE_BIN/mcast_send"
    else
      send_bin="python3 /tmp/mcast_send.py"
    fi
    [[ -n "$SENDER_CPU" ]] && send_bin="taskset -c $SENDER_CPU $send_bin"
    sender_ok=true
    if remote_run "$PUBLISHER_ID" "$SENDER_TIMEOUT" "$send_bin $GROUP $PORT $RATE $DURATION $PUB_IP"; then
      sender_out="$REMOTE_OUT"
    else
      sender_ok=false
      sender_out="{\"error\":\"sender command did not complete (SSM status: ${REMOTE_STATUS:-unknown})\"}"
    fi
    printf '%s\n' "$sender_out" > "$WORK/sender.out"
    [[ "$sender_ok" == "true" ]] && echo "  Multicast send complete." || echo "  Multicast send FAILED." >&2
    echo ""

    # Receivers finish on their own after the end marker. Give them a moment, then interrupt
    # any that are still running so they write what they have.
    local DETAIL_PREFIX="s3://$S3_BUCKET/runs/$RUN_ID"
    local DETAIL_CMD="echo DETAIL=SKIPPED"
    if [[ "$RECEIVER_IMPL" == "native" ]]; then
      DETAIL_CMD="if [ -f /tmp/mcast_recv_detail.json ]; then aws s3 cp /tmp/mcast_recv_detail.json $DETAIL_PREFIX/@ID@.detail.json --region $REGION --only-show-errors && echo DETAIL=UPLOADED; else echo DETAIL=MISSING; fi"
    fi
    COLLECT_CMD="for i in \$(seq 1 30); do pgrep -f '[m]cast_recv' >/dev/null || break; sleep 1; done
if pgrep -f '[m]cast_recv' >/dev/null; then pkill -INT -f '[m]cast_recv'; sleep 3; echo RECV=INTERRUPTED; else echo RECV=DONE; fi
echo '=====RESULT====='
cat /tmp/mcast_result.json 2>/dev/null
echo '=====DETAIL====='
$DETAIL_CMD
echo '=====CLOCK====='
bash /tmp/preflight.sh --clock"
    par_run collect_worker "${ALL_INSTANCE_IDS[@]}" || true
    if [[ "$RECEIVER_IMPL" == "native" ]]; then
      mkdir -p "$DETAIL_DIR"
      aws s3 cp "$DETAIL_PREFIX/" "$DETAIL_DIR/" --recursive --region "$REGION" --only-show-errors || echo "  WARN could not download detail files from $DETAIL_PREFIX/"
    fi
    incomplete=0
    [[ "$sender_ok" == "true" ]] || incomplete=1
    for s_id in "${SUBSCRIBER_IDS[@]}"; do
      result_ok "$WORK/res.$s_id" || incomplete=1
    done
    if [[ $incomplete -eq 0 || $batch -ge 2 ]]; then break; fi
    echo "  Phase A incomplete (the sender or a subscriber produced no usable result); rerunning the whole batch once"
    done
    phase_mark "receiver start, send and collect (includes the ${DURATION}s send)"
    echo "--- SENDER $PUBLISHER_ID ---"
    cat "$WORK/sender.out"
    echo "--- END SENDER $PUBLISHER_ID ---"

    local sub_az rel
    for sub_id in "${SUBSCRIBER_IDS[@]}"; do
      sub_az=$(get_az "$sub_id")
      rel="cross-az"
      [[ "$sub_az" == "$PUB_AZ" ]] && rel="same-az"
      emit_mcast_block "$sub_id" "$sub_az" "$rel" "$WORK/res.$sub_id"
    done
    echo ""

    # Clock state after the run was read together with the results.
    for instance_id in "${ALL_INSTANCE_IDS[@]}"; do
      emit_clock_block "$instance_id" post "$WORK/post.$instance_id"
    done
    echo ""
  fi

  phase_mark "post-run clock state"
  # --- sockperf: unicast RTT baseline (no TGW) ---
  if tools_has sockperf; then
    echo ">>> Phase B: Unicast latency measurement (sockperf under-load)..."
    local sub_ip result_output
    for sub_id in "${SUBSCRIBER_IDS[@]}"; do
      sub_ip=$(get_private_ip "$sub_id")
      remote_run "$sub_id" 60 "pkill -f 'sockperf server' 2>/dev/null || true; pkill -f '[m]cast_recv' 2>/dev/null || true; fuser -k ${PORT}/udp 2>/dev/null || true; sleep 2; nohup sockperf server --ip ${sub_ip} --port ${PORT} > /tmp/sockperf_server.txt 2>&1 < /dev/null &" || true
      echo "  $sub_id ($sub_ip) - sockperf server started"
    done
    sleep "$RECEIVER_SETTLE_SECS"
    for sub_id in "${SUBSCRIBER_IDS[@]}"; do
      remote_run "$sub_id" 30 "cat /tmp/sockperf_server.txt 2>/dev/null; ss -ulnp | grep ${PORT}" || true
      echo "  $sub_id server status: $REMOTE_OUT"
    done
    for sub_id in "${SUBSCRIBER_IDS[@]}"; do
      sub_ip=$(get_private_ip "$sub_id")
      echo "  Running sockperf under-load -> $sub_id ($sub_ip)..."
      result_output=""
      if remote_run "$PUBLISHER_ID" "$SENDER_TIMEOUT" "sockperf under-load --ip ${sub_ip} --port ${PORT} --mps ${RATE} --time ${DURATION} --reply-every 1 --full-rtt 2>&1 | tee /tmp/sockperf_result.txt"; then
        remote_run "$PUBLISHER_ID" 60 "cat /tmp/sockperf_result.txt" || true
        result_output="$REMOTE_OUT"
      else
        result_output="ERROR: sockperf did not complete (SSM status: ${REMOTE_STATUS:-unknown})"
      fi
      echo "--- SUBSCRIBER $sub_id ---"
      echo "$result_output"
      echo "--- END $sub_id ---"
      echo ""
      remote_run "$sub_id" 30 "pkill -f 'sockperf server' || true" || true
    done
  fi

  # --- iperf3: unicast UDP throughput (no TGW, no latency) ---
  if tools_has iperf3; then
    echo ">>> Phase C: Unicast UDP throughput (iperf3)..."
    local sub_ip result_output
    for sub_id in "${SUBSCRIBER_IDS[@]}"; do
      sub_ip=$(get_private_ip "$sub_id")
      remote_run "$sub_id" 60 "pkill -f 'iperf3 -s' 2>/dev/null || true; fuser -k ${PORT}/udp 2>/dev/null || true; sleep 2; nohup iperf3 -s -p ${PORT} > /tmp/benchmark_result.txt 2>&1 < /dev/null &" || true
      echo "  $sub_id ($sub_ip) - iperf3 server started"
    done
    sleep "$RECEIVER_SETTLE_SECS"
    for sub_id in "${SUBSCRIBER_IDS[@]}"; do
      sub_ip=$(get_private_ip "$sub_id")
      echo "  Running iperf3 -> $sub_id ($sub_ip)..."
      remote_run "$PUBLISHER_ID" "$SENDER_TIMEOUT" "iperf3 -c ${sub_ip} -u -p ${PORT} -b ${RATE}k -t ${DURATION} 2>&1 | tee /tmp/iperf3_result.txt" || true
      if remote_run "$sub_id" 60 "pkill -f 'iperf3 -s' || true; sleep 1; cat /tmp/benchmark_result.txt"; then
        result_output="$REMOTE_OUT"
      else
        result_output="ERROR: iperf3 did not complete (SSM status: ${REMOTE_STATUS:-unknown})"
      fi
      echo "--- SUBSCRIBER $sub_id ---"
      echo "$result_output"
      echo "--- END $sub_id ---"
      echo ""
    done
  fi

  echo "=== Benchmark complete ==="
  echo "S3 bucket for reports: $S3_BUCKET"
}

# ---------------------------------------------------------------------------
# Run, then judge. run_all's output is both shown and kept as the raw stream.
# ---------------------------------------------------------------------------
if [[ "$TRANSPORT" == "ssh" ]]; then ssh_setup; fi
set +e
run_all 2>&1 | tee "$RAW_OUTPUT_FILE"
RUN_RC=${PIPESTATUS[0]}
set -e
if [[ $RUN_RC -ne 0 ]]; then
  echo "ERROR: benchmark aborted (exit $RUN_RC); no results to judge." >&2
  exit "$RUN_RC"
fi

FINAL_RC=0
if tools_has mcast; then
  echo ""
  echo ">>> Run validity:"
  set +e
  if ! python3 "$SCRIPT_DIR/tgw_results.py" verdict --input "$RAW_OUTPUT_FILE" \
    --max-loss-pct "$MAX_LOSS_PCT" --max-clock-offset-us "$MAX_CLOCK_OFFSET_US"; then
    FINAL_RC=$EXIT_INVALID_RUN
  fi
  set -e
fi

if [[ "$SAVE_REPORT" == "true" ]]; then
  REPORT_ARGS=(--input "$RAW_OUTPUT_FILE" --placement "$PLACEMENT" --output-dir "$RUNS_DIR"
               --max-loss-pct "$MAX_LOSS_PCT" --max-clock-offset-us "$MAX_CLOCK_OFFSET_US")
  [[ -n "$INSTANCE_TYPE" ]] && REPORT_ARGS+=(--instance-type "$INSTANCE_TYPE")
  [[ -n "$NOTES" ]] && REPORT_ARGS+=(--notes "$NOTES")
  [[ -d "$DETAIL_DIR" ]] && REPORT_ARGS+=(--detail-dir "$DETAIL_DIR")
  echo ""
  echo ">>> Generating markdown report..."
  set +e
  python3 "$SCRIPT_DIR/save_report.py" "${REPORT_ARGS[@]}"
  set -e
fi

if [[ "$KEEP_DETAIL" == "true" && -d "$DETAIL_DIR" ]]; then
  echo "Histogram detail files kept in: $DETAIL_DIR"
  echo "  (pass --detail-dir to collect_results.py / save_report.py for pooled percentiles)"
fi

exit "$FINAL_RC"
