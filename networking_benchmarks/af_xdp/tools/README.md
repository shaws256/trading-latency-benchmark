# tools/

Measurement instruments and control utilities.

## Binaries

| File | Binary | Description |
|------|--------|-------------|
| `rtt.cpp` | `rtt` | High-precision RTT measurement client. Subscribes to replicator, sends UDP packets, measures round-trip via SO_TIMESTAMP (kernel RX) + TSC (TX). Outputs JSON with p50/p90/p95/p99/p999/max. |
| `mcast_send.cpp` | `mcast_send` | Multicast sender — timestamps packets, sends to GRE tunnel or multicast group. Used as the "exchange" in multicast scenarios. |
| `mcast_receive.cpp` | `mcast_receive` | Multicast receiver — captures packets with kernel RX timestamps, computes one-way latency from sender timestamp. Requires PHC clock sync between hosts. |
| `replicator_ctl.cpp` | `replicator_ctl` | Control protocol client. Sends ADD/REMOVE/LIST commands to replicator's control port (12345). |
| `udp_ping.cpp` | `udp_ping` | Simple UDP connectivity probe. Sends packets to a target and reports reachability. Supports multicast groups. |

## rtt usage

```bash
./rtt <replicator_ip> <data_port> <listen_ip> <listen_port> \
      <count> <rate_per_sec> <warmup> <tx_cpu> <rx_cpu>
```

Output: JSON at `/tmp/rtt_results.json` with `service_rtt_us` and `response_rtt_us` percentiles.

Timestamp modes:
- Kernel software (`SO_TIMESTAMP`) — default, ~30µs UTC accuracy
- Hardware PHC (`SOF_TIMESTAMPING_RX_HARDWARE`) — requires PHC-enabled ENA

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
