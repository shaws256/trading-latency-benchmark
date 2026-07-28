#!/bin/bash
# AMI Bake Script — runs as UserData on the builder instance.
# Installs all static dependencies, builds binaries, writes configs, then signals CFN.
#
# Environment variables (set by CDK UserData):
#   STACK_NAME, REGION, WAIT_HANDLE_URL, GIT_REPO, GIT_REF
set -uo pipefail
exec > /var/log/bake-ami.log 2>&1

echo "=== AMI Bake started at $(date -u) ==="

# ── 0. Self-stop + CFN signal on exit (success or failure) ────────────────────
INSTANCE_ID=$(TOKEN=$(curl -s -X PUT "http://169.254.169.254/latest/api/token" -H "X-aws-ec2-metadata-token-ttl-seconds: 60") && curl -s -H "X-aws-ec2-metadata-token: $TOKEN" http://169.254.169.254/latest/meta-data/instance-id)
BAKE_EXIT=1  # assume failure unless explicitly set to 0

cleanup() {
  echo "=== Signalling CFN (exit=$BAKE_EXIT) and stopping instance ==="
  # Push bake log to CloudWatch before stopping (use put-log-events with simple JSON)
  aws logs create-log-group --log-group-name "/af-xdp/ami-builder" --region "$REGION" 2>/dev/null || true
  aws logs create-log-stream --log-group-name "/af-xdp/ami-builder" --log-stream-name "$INSTANCE_ID" --region "$REGION" 2>/dev/null || true
  # Convert log to JSON events (python handles escaping correctly)
  python3 -c "
import json, time, sys
events = []
ts = int(time.time() * 1000)
with open('/var/log/bake-ami.log') as f:
    for i, line in enumerate(f):
        events.append({'timestamp': ts + i, 'message': line.rstrip()})
if events:
    print(json.dumps(events))
" > /tmp/log-events.json 2>/dev/null && \
  aws logs put-log-events --log-group-name "/af-xdp/ami-builder" --log-stream-name "$INSTANCE_ID" --region "$REGION" \
    --log-events "file:///tmp/log-events.json" 2>/dev/null || true

  # Signal CFN via WaitConditionHandle URL (not --resource)
  TAIL_LOG=$(grep -i "error\|fail\|fatal\|denied\|===\|Step" /var/log/bake-ami.log 2>/dev/null | tail -20 | tr '"' "'" | tr '\n' '|' | cut -c1-1000)
  if [ "$BAKE_EXIT" -eq 0 ]; then
    curl -s -X PUT -H "Content-Type:" --data-binary "{\"Status\":\"SUCCESS\",\"UniqueId\":\"bake\",\"Data\":\"complete\"}" "$WAIT_HANDLE_URL" || true
  else
    curl -s -X PUT -H "Content-Type:" --data-binary "{\"Status\":\"FAILURE\",\"UniqueId\":\"bake\",\"Reason\":\"${TAIL_LOG:-no log}\"}" "$WAIT_HANDLE_URL" || true
  fi
  aws ec2 stop-instances --instance-ids "$INSTANCE_ID" --region "$REGION" || true
}
trap cleanup EXIT

set -e  # fail-fast after trap is registered

# ── 1. Build dependencies ─────────────────────────────────────────────────────
dnf install -y \
  git clang llvm libbpf-devel elfutils-libelf-devel \
  kernel-headers kernel-devel iproute ethtool \
  make gcc gcc-c++ pkgconfig m4 libpcap-devel rsync \
  python3 python3-pip

# ── 2. xdp-tools ─────────────────────────────────────────────────────────────
if [ ! -f /usr/local/lib/libxdp.so ]; then
  git clone --depth 1 https://github.com/xdp-project/xdp-tools.git /opt/xdp-tools
  cd /opt/xdp-tools

  # AL2023 gcc default is pre-C23: xdp-tools uses bare bool/true/false
  grep -rlZ --include='*.c' --include='*.h' \
      -e '\bbool\b' -e '\btrue\b' -e '\bfalse\b' lib headers 2>/dev/null \
    | while IFS= read -r -d '' f; do
        grep -q 'stdbool.h' "$f" || sed -i '1i #include <stdbool.h>' "$f"
      done

  ./configure
  echo 'CFLAGS += -Wno-error' >> config.mk
  make -j"$(nproc)"
  make install > /dev/null
  ldconfig

  mkdir -p /usr/lib64/bpf
  cp -f lib/libxdp/xdp-dispatcher.o /usr/lib64/bpf/ 2>/dev/null || true
fi

# ── 3. Benchmark binaries ─────────────────────────────────────────────────────
REPO_URL="${GIT_REPO:-https://github.com/shaws256/trading-latency-benchmark.git}"
REF="${GIT_REF:-test}"

echo "=== Step 3: Clone + build benchmark ==="
echo "Cloning $REPO_URL (ref: $REF)..."
if ! git clone --depth 1 --branch "$REF" "$REPO_URL" /tmp/build-src 2>&1; then
  echo "ERROR: git clone failed. Repo may not be public or ref may not exist."
  echo "Skipping binary build — AMI will have xdp-tools but no benchmark binaries."
  echo "Use ansible rsync to deploy binaries at runtime."
  mkdir -p /opt/af-xdp/xdp
else
  cd /tmp/build-src/networking_benchmarks/af_xdp
  make full
  mkdir -p /opt/af-xdp/xdp
  cp -f replicator rtt mcast_send mcast_receive replicator_ctl udp_ping /opt/af-xdp/ 2>/dev/null || true
  cp -f src/xdp/*.o /opt/af-xdp/xdp/ 2>/dev/null || true
fi

# ── 4. System configs ─────────────────────────────────────────────────────────

# ENA PHC (hardware timestamping)
cat > /etc/modprobe.d/ena-phc.conf <<'EOF'
options ena enable_llq=1 phc_enable=1
EOF

# chrony refclock PHC
mkdir -p /etc/chrony.d
cat > /etc/chrony.d/aws-phc.conf <<'EOF'
refclock PHC /dev/ptp0 poll 0 dpoll -2 trust prefer
EOF

# BPF JIT
cat > /etc/sysctl.d/99-bpf-xdp.conf <<'EOF'
net.core.bpf_jit_enable = 1
net.core.bpf_jit_harden = 0
net.core.bpf_jit_kallsyms = 1
EOF

# Network tuning (feeder-safe, harmless on other roles)
cat > /etc/sysctl.d/99-network-bench.conf <<'EOF'
net.ipv4.conf.all.mc_forwarding = 0
net.ipv4.conf.default.mc_forwarding = 0
net.ipv4.igmp_qrv = 1
net.ipv4.conf.all.rp_filter = 0
net.ipv4.conf.default.rp_filter = 0
net.core.netdev_max_backlog = 10000
EOF

# LIBXDP_OBJECT_PATH for xdp-dispatcher.o lookup
cat > /etc/profile.d/af-xdp.sh <<'EOF'
export LIBXDP_OBJECT_PATH=/usr/lib64/bpf
export PATH=/opt/af-xdp:$PATH
EOF

# ── 5. Systemd units ──────────────────────────────────────────────────────────

# Interrupt coalescing (rx-usecs=0 tx-usecs=0)
cat > /etc/systemd/system/ena-coalescing.service <<'EOF'
[Unit]
Description=Disable ENA interrupt coalescing for minimum latency
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/bash -c 'IFACE=$(ip -4 route show default | awk "{print $5}" | head -1); ethtool -C "${IFACE:-eth0}" rx-usecs 0 tx-usecs 0 || true'

[Install]
WantedBy=multi-user.target
EOF

# ENA queue headroom (combined queues = max/2 for XDP TX room)
cat > /etc/systemd/system/ena-xdp-queues.service <<'EOF'
[Unit]
Description=Set ENA combined queues for XDP headroom
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/bash -c 'IFACE=$(ip -4 route show default | awk "{print $5}" | head -1); MAX_Q=$(ethtool -l "${IFACE:-eth0}" 2>/dev/null | awk "/Combined:/{print $2; exit}"); [ -z "$MAX_Q" ] || [ "$MAX_Q" -lt 2 ] && exit 0; ethtool -L "${IFACE:-eth0}" combined $(( MAX_Q / 2 ))'

[Install]
WantedBy=multi-user.target
EOF

# MTU 3498 for native XDP (ENA single-page frame requirement)
cat > /etc/systemd/system/ena-mtu.service <<'EOF'
[Unit]
Description=Set MTU 3498 for ENA native XDP
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/bash -c 'IFACE=$(ip -4 route show default | awk "{print $5}" | head -1); ip link set "${IFACE:-eth0}" mtu 3498'

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable ena-coalescing.service ena-xdp-queues.service ena-mtu.service

# ── 6. Cleanup ────────────────────────────────────────────────────────────────
rm -rf /tmp/build-src /opt/xdp-tools
dnf clean all
rm -rf /var/cache/dnf

echo "=== AMI Bake complete at $(date -u) ==="

# ── 8. Mark success (trap handles CFN signal + instance stop) ─────────────────
BAKE_EXIT=0
