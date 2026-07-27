# AF_XDP Network Latency Benchmark

High-performance network latency measurement suite using AF_XDP kernel bypass.
Measures round-trip and one-way latency between EC2 instances at microsecond precision.

## Use Cases

### 1. Unicast RTT Matrix (primary)

Measure NxN pairwise latency across a fleet of instances.

```
┌──────────┐   UDP   ┌─────────────┐  echo   ┌──────────┐
│   rtt    │ ──────► │  replicator │ ──────► │   rtt    │
│ (node A) │ ◄────── │  (node B)   │ ◄────── │ (node A) │
└──────────┘         └─────────────┘         └──────────┘
       RTT = RX_timestamp - TX_timestamp
```

Each node runs its own `replicator` (echo server). `rtt` on node A sends to B's
replicator, which echoes back. Kernel SO_TIMESTAMP captures RX time; TSC captures TX time.

```bash
# On every node: start replicator
sudo ./replicator eth0 <private_ip> 9000 true --queues 4

# Measure A→B:
./rtt <B_private_ip> 9000 <A_private_ip> 9001 100000 10000 10000 1 2
#      target        port  local_ip       port msgs   rate  warmup TX_cpu RX_cpu
```

**Measured results** (c7i.xlarge, CPG, us-east-1): p50=32µs, p99=37µs, p50-p99 spread=5µs.

### 2. Multicast Fan-Out (GRE tunnel)

Measure one-way latency from source through replicator to N destinations — the path
real market data takes in a trading architecture. Uses GRE encapsulation because
AWS VPC doesn't support IP multicast natively.

```
┌──────────┐  GRE(mcast)   ┌─────────────┐  unicast UDP   ┌────────────┐
│ source │ ────────────► │   replicator    │ ─────────────► │ destination │
│ (mcast_  │               │ (replicator │                │ (mcast_    │
│  sender) │               │   --gre)    │                │  receiver) │
└──────────┘               └─────────────┘                └────────────┘
```

```bash
# Replicator: GRE mode, intercepts encapsulated multicast
sudo ./replicator eth0 224.0.31.50 5000 true --gre

# Source: AF_XDP zero-copy GRE sender with TX timestamps
sudo ./mcast_send -I eth0 -D <replicator_ip> -g 224.0.31.50 -p 5000 -c 100000

# Destination: AF_XDP receiver with RX timestamps
sudo ./mcast_receive -i eth0 -B ./src/xdp/mcast.o -c 100000
```

### 3. Kernel Mode (containers / local testing)

Run the replicator as a standard UDP echo server — no AF_XDP, no root, no BPF.
Works in Docker containers, on macOS (arm64), and any Linux without XDP support.

```bash
# Start kernel-mode replicator (no sudo needed)
./replicator --kernel-mode 127.0.0.1 9000

# In another terminal: run RTT measurement against it
./rtt 127.0.0.1 9000 127.0.0.1 9001 10000 1000 1000 0 1
```

Latency will be ~200-500µs (kernel path) vs ~30-40µs on AF_XDP, but the entire
subscription + measurement + JSON output pipeline is validated end-to-end.

### 4. Connectivity Verification

Quick check that packets reach the replicator (no measurement, no root required):

```bash
./udp_ping <target_ip> 9000              # 1 packet/sec, default payload
./udp_ping 224.0.31.50 5000 100 "test" --iface eth0  # multicast, 100ms interval
```

---

## Build

```bash
# Dependencies (Amazon Linux 2023)
sudo dnf install -y gcc-c++ clang llvm libbpf-devel elfutils-libelf-devel \
    kernel-headers make ethtool git

# Build xdp-tools from source (AL2023 has no libxdp package)
git clone --depth 1 https://github.com/xdp-project/xdp-tools.git
cd xdp-tools && ./configure && make && sudo make install && sudo ldconfig
# If xdp-dispatcher.o not found: export LIBXDP_OBJECT_PATH=/usr/local/lib/bpf

# Build binaries
make all       # replicator, rtt, replicator_ctl, udp_ping + ucast XDP program
make mcast     # mcast_send, mcast_receive + mcast XDP program
make full      # all of the above
```

### Binaries

| Binary | Category | Purpose |
|--------|----------|---------|
| `replicator` | Engine | AF_XDP zero-copy echo/fan-out server |
| `rtt` | Measurement | Precision RTT (SO_TIMESTAMP + TSC, busy-poll, lock-free, CPU-pinned) |
| `mcast_send` | Measurement | One-way TX with nanosecond timestamps (AF_XDP zero-copy GRE) |
| `mcast_receive` | Measurement | One-way RX via AF_XDP with per-hop breakdown |
| `replicator_ctl` | Admin | Register/deregister destinations, mcast join/leave |
| `udp_ping` | Debug | Fire-and-forget UDP sender (connectivity smoke test) |

---

## Control Protocol (port 12345)

The replicator's destination table is managed via `replicator_ctl`:

```bash
./replicator_ctl <replicator_ip> add <dest_ip> <dest_port>    # register destination
./replicator_ctl <replicator_ip> remove <dest_ip> <dest_port> # deregister
./replicator_ctl <replicator_ip> list                         # show all destinations
./replicator_ctl <replicator_ip> mcast <group>                # join multicast group (GRE mode)
./replicator_ctl <replicator_ip> mcast-leave <group>          # leave group
```

> **Note:** `rtt` auto-subscribes before measurement — no manual `ctl add` needed for unicast RTT.

---

## Directory Layout

```
networking_benchmarks/af_xdp/
├── Makefile
├── README.md
├── src/                          # Replicator engine
│   ├── Replicator.cpp/hpp        # Multi-queue fan-out, cached MAC, generation-gated cache
│   ├── ReplicatorMain.cpp        # CLI (--gre, --queues, --ctrl, --producer)
│   ├── XdpSocket.cpp/hpp         # XSK socket lifecycle, UMEM, frame management
│   ├── NicConfig.cpp/hpp         # NIC prep for XDP (queues, headroom, driver detect)
│   └── xdp/                      # eBPF XDP filter programs
│       ├── ucast.c               # Unicast UDP → AF_XDP redirect
│       └── mcast.c               # GRE + inner multicast → AF_XDP redirect
├── tools/                        # Binaries
│   ├── rtt.cpp                   # RTT measurement (lock-free, kernel timestamps, busy-poll)
│   ├── mcast_send.cpp          # AF_XDP TX zero-copy GRE
│   ├── mcast_receive.cpp        # AF_XDP RX with timestamps
│   ├── replicator_ctl.cpp        # Destination management CLI
│   └── udp_ping.cpp              # Simple UDP sender
├── legacy/                       # Deprecated
│   ├── MarketDataProviderClient.cpp
│   └── scripts/                  # Old test harness (superseded by deploy/)
└── deploy/                       # Infrastructure (CDK + Ansible + scripts + reports)
    ├── cdk/                      # Standalone CDK project (fleet mode, CPG, cross-region)
    ├── ansible/                  # configure.yaml, tune_replicator.yaml, inventory
    ├── scripts/                  # test.sh, run_matrix.sh, deploy.sh, ptp.sh
    └── reports/                  # generate_matrix_report.py, topology_map_sample.html
```

---

## Performance Characteristics

| Metric | Value | Conditions |
|--------|-------|-----------|
| RTT p50 | 32 µs | c7i.xlarge, CPG, us-east-1, 10K msg/s |
| RTT p99 | 37 µs | same |
| p50–p99 spread | 5 µs | same |
| Max throughput | 1M+ pps | AF_XDP zero-copy TX, single queue |
| Stamp-to-wire gap | ~1-2 µs | mcast_send (vs ~3-10µs kernel path) |

### Key Optimizations

- AF_XDP zero-copy (bypasses kernel network stack entirely)
- SO_TIMESTAMP kernel RX timestamps (captured at NIC interrupt, not userspace)
- TSC-calibrated TX timestamps (rdtsc, ~0.4ns/tick)
- Busy-poll RX (SO_BUSY_POLL=50µs, no poll()/select() wakeup jitter)
- Lock-free slot array indexed by sequence ID (no mutex in hot path)
- CPU core pinning for send and receive threads
- Single driver kick per TX batch (not per-destination)
- Coordinated omission tracking (intended vs actual send time)

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Permission denied | Run `replicator` with `sudo` (AF_XDP needs CAP_NET_ADMIN) |
| XDP program conflict | `sudo ip link set eth0 xdp off` or `legacy/scripts/cleanup.sh` |
| Zero-copy fails | Automatic fallback to copy mode; check `ethtool -i eth0` for XDP support |
| Subscription failed | Replicator not running or XDP program didn't load — check `/tmp/replicator.log` |
| GRE packets not arriving | Security group: allow IP protocol 47 from source to replicator |

## Requirements

- Linux kernel 6.1+ (Amazon Linux 2023); minimum 5.10 for AF_XDP zero-copy
- AF_XDP compatible NIC (ENA on AWS, i40e, ixgbe, mlx5_core)
- Root privileges for AF_XDP / XDP program loading
- clang with BPF target for eBPF compilation
