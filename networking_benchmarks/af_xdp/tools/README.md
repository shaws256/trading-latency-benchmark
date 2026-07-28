# tools/

Measurement instruments and control utilities.

## Binaries

| File | Binary | Description |
|------|--------|-------------|
| `rtt_kernel.cpp` | `rtt_kernel` | High-precision RTT measurement client. Subscribes to replicator, sends UDP packets, measures round-trip via SO_TIMESTAMP (kernel RX) + TSC (TX). Outputs JSON with p50/p90/p95/p99/p999/max. |
| `mcast_send.cpp` | `mcast_send` | Multicast sender — timestamps packets, sends to GRE tunnel or multicast group. Used as the "exchange" in multicast scenarios. |
| `mcast_receive.cpp` | `mcast_receive` | Multicast receiver — captures packets with kernel RX timestamps, computes one-way latency from sender timestamp. Requires PHC clock sync between hosts. |
| `replicator_ctl.cpp` | `replicator_ctl` | Control protocol client. Sends ADD/REMOVE/LIST commands to replicator's control port (12345). |
| `udp_send.cpp` | `udp_send` | Simple UDP connectivity probe. Sends packets to a target and reports reachability. Supports multicast groups. |

## rtt_kernel usage

```bash
sudo ./rtt_kernel <replicator_ip> <data_port> <listen_ip> <listen_port> \
           <count> <rate_per_sec> <warmup> <tx_cpu> <rx_cpu>
```

Requires `sudo` for SCHED_FIFO and mlockall. Runs without root but degrades gracefully
(warnings printed, continues with SCHED_OTHER).

Output: JSON at `/tmp/rtt_results.json` with `service_rtt_us` and `response_rtt_us` percentiles.

### Latency optimizations

The `rtt_kernel` binary implements the following optimizations to minimize measurement overhead
and isolate the true network/replicator latency from OS noise:

| Optimization | Mechanism | Impact |
|---|---|---|
| **SCHED_FIFO (priority 80)** | `sched_setscheduler(SCHED_FIFO, 80)` on both sender and receiver threads | Eliminates preemption by other processes. Removes 5-15µs scheduler jitter. |
| **mlockall** | `mlockall(MCL_CURRENT \| MCL_FUTURE)` | Prevents page faults during measurement. First-touch faults can add 50-100µs spikes. |
| **CPU pinning** | `sched_setaffinity()` — sender and receiver on separate isolated cores | Eliminates cross-core cache migration. Works best with `isolcpus` kernel boot param. |
| **SO_BUSY_POLL (100µs)** | `setsockopt(SO_BUSY_POLL, 100)` on receive socket | Kernel spins polling NIC queue instead of sleeping for IRQ wakeup. Removes 8-12µs IRQ→schedule→wakeup path. |
| **SO_PREFER_BUSY_POLL** | Forces busy-poll path even under moderate load | Prevents kernel from falling back to interrupt-driven receive. |
| **SO_BUSY_POLL_BUDGET (256)** | Packets processed per busy-poll NAPI cycle | Prevents early exit from poll loop when multiple packets queued. |
| **TSC TX timestamp** | `rdtsc()` calibrated against CLOCK_MONOTONIC | Sub-nanosecond precision, no syscall overhead on send path. |
| **Kernel RX timestamp** | `SO_TIMESTAMPING` with `SOF_TIMESTAMPING_RX_SOFTWARE` | Timestamp taken at NIC interrupt handler (before socket queue), not at `recvmsg()` return. |
| **Lock-free slot array** | Pre-allocated array indexed by sequence ID | No allocation, no mutex, no map lookup in hot path. Cache-line aligned (64B). |
| **Spin-wait receive** | `recvmsg(MSG_DONTWAIT)` + `_mm_pause()` loop | No `poll()`/`select()` syscall overhead. `_mm_pause` hints CPU for SMT-friendly spinning. |
| **Safety alarm** | `alarm(runtime + 30s)` with `_exit(2)` handler | Prevents SCHED_FIFO busy-loop from locking up the system if the benchmark hangs. |
| **Absolute-time pacing** | `clock_nanosleep(TIMER_ABSTIME)` | Prevents drift accumulation — each send targets exact wall-clock deadline. |

### Expected latency by optimization level

| Configuration | p50 (CPG, c7i.xlarge) | Notes |
|---|---|---|
| Default (no RT, no tuning) | ~35-46µs | OS scheduler + IRQ wakeup dominate |
| + SO_BUSY_POLL only | ~28-35µs | Removes IRQ wakeup, still has scheduler jitter |
| + SCHED_FIFO + CPU pin | ~22-28µs | No preemption, deterministic scheduling |
| + mlockall + isolcpus | ~20-25µs | No page faults, no kernel housekeeping on measurement cores |
| All combined (current) | ~18-24µs | Kernel UDP path floor |

### Limitations (kernel path floor)

Even with all optimizations, the kernel UDP path has an irreducible ~15-20µs floor:
- `sendto()` syscall: ~3-5µs (context switch + kernel lock + skb alloc + copy)
- `recvmsg()` kernel→user copy: ~2-3µs
- NIC DMA + wire propagation: ~3-5µs

To go below this floor, the sender would need AF_XDP TX (bypass kernel entirely),
which would reduce p50 to ~12-15µs.

### Timestamp modes

| Mode | Source | Precision | Clock domain |
|---|---|---|---|
| Kernel software (`SO_TIMESTAMP`) | NIC interrupt handler | ~1µs | CLOCK_REALTIME |
| Hardware PHC (`SOF_TIMESTAMPING_RX_HARDWARE`) | Nitro timestamping engine | ~15ns | PHC epoch (needs phc2sys for UTC) |
| Userspace (fallback) | `clock_gettime` after `recvmsg` | ~1µs + queue delay | CLOCK_MONOTONIC |

For RTT measurement (same host sends and receives), kernel software timestamps are optimal —
both TX (TSC/CLOCK_REALTIME) and RX are in the same clock domain. Hardware PHC timestamps
require `phc2sys` to align the PHC epoch with system clock.

## replicator_ctl usage

```bash
./replicator_ctl <replicator_ip> add <dest_ip> <dest_port>
./replicator_ctl <replicator_ip> remove <dest_ip> <dest_port>
./replicator_ctl <replicator_ip> list
./replicator_ctl <replicator_ip> mcast <multicast_group>
./replicator_ctl <replicator_ip> mcast-leave <multicast_group>
```

## Dependencies

All tools link only `-lpthread` in kernel-mode builds. Full builds add `-lxdp -lbpf -lelf` (unused at runtime by tools, but pulled in by the shared Makefile LDLIBS).
