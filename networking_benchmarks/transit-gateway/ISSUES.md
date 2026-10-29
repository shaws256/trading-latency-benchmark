# transit-gateway benchmark: issues, effects, fixes

Scope: `networking_benchmarks/transit-gateway/` at `6b78a43` (branch `new`, same as `main`).
Reference implementation: `networking_benchmarks/af_xdp/` (merged), plus the local af_xdp dev notes.

## How to read this

**Evidence tags**

| Tag | Meaning |
|---|---|
| `[RUN]` | Reproduced by executing the repo's own code on synthetic input (no AWS) |
| `[CODE]` | Read directly in source; not executed |
| `[AFX]` | Behavior of the merged af_xdp code, cited as the reference |
| `[NOTES]` | From the local, gitignored af_xdp dev notes; measured on the af_xdp fleet, not on this stack |

**Priority**
- **P0** - can produce a wrong or misleading headline result, or hide a failed run, with no signal to the operator
- **P1** - systematically biases or invalidates a reported statistic
- **P2** - weakens repeatability, interpretability or test confidence
- **P3** - hygiene, security, docs

**Effort**: S = under half a day, M = about a day, L = multi-day.

**Not verified**: nothing here was run on AWS. Real sockperf output, the CDK synth tests, and every claim about EC2 timing behavior remain unchecked.

---

## Status of P0 and P1

Implemented on this branch. "Verified" means exercised by the tests listed in the README (fake `aws`, stubbed `/sys` and `chronyc`, Linux containers). Nothing has run on EC2.

| Issue | State | Where |
|---|---|---|
| P0-1 clock gate | Done. User-data waits to 100 µs and steps; `preflight.sh` forces a step and gates chrony `System time` (10 µs) before the run; the orchestrator re-reads it after the run; `check_ptp.sh` reports and gates it | `stacks/benchmark_stack.py`, `preflight.sh`, `run_benchmark.sh`, `check_ptp.sh` |
| P0-2 mcast not collected | Done. `collect_results.py` defaults to `mcast`, adds a `Tool` dimension, and refuses invalid runs without `--allow-partial` | `collect_results.py`, `tgw_results.py` |
| P0-3 silent subscriber | Done. Every expected subscriber gets a block (error if none); a missing or errored one fails the run; failed subscribers count as total loss | `run_benchmark.sh`, `tgw_results.py`, `save_report.py` |
| P0-4 clock error in data | Done. Negative samples are counted, never clamped, and fail the run; the software-vs-hardware gap is reported | `native/`, `tgw_results.py` |
| P1-1 aggregates | Done. Percentiles come from merged histograms; without them only exact quantities are reported | `tgw_results.py`, `save_report.py`, `collect_results.py` |
| P1-2 AZ mixing | Done. Subscribers are grouped by same-AZ / cross-AZ and never merged | same |
| P1-3 upper bound | Partly. The native sender stamps after packing; reports and README now say "upper bound". The send path and hardware-stamp reference point are still not measured on EC2, and the RTT/2 subtraction is only caveated | `native/`, `README.md` |
| P1-4 pacing | Done. Absolute-deadline pacing; achieved rate reported and gated at 90 % | `native/mcast_send.cpp`, `tgw_results.py` |
| P1-5 readiness | Done for native (ready file, one retry, then abort). The Python receiver keeps a fixed settle plus a liveness check | `run_benchmark.sh` |
| P1-6 loss accounting | Done. Duplicates, out-of-order and socket drops are counted; 2 % loss gate | `native/`, `tgw_results.py` |
| P1-7 native sender/receiver | Done and wired. Built on the instances from a sparse git clone; failure leaves the Python path usable; no silent fallback | `stacks/benchmark_stack.py`, `run_benchmark.sh` |

Open items that need EC2: whether the hardware stamp path behaves as the code assumes (the shipped receiver refuses to run without a real `SIOCSHWTSTAMP`), the 20 µs software-minus-hardware warning threshold, and whether the default `main` ref contains the native sources (it does only once this is merged).

Found while implementing: the old readiness check treated `NOTREADY` as ready (it matched the substring `READY`).

## P0 - silent wrong or misleading results

### P0-1. Clock convergence is never gated, and the diagnostics read the wrong fields
- **Issue.** User-data runs `chronyc waitsync 30 || true` (`benchmark_stack.py:478`). It sets no `makestep` and no max-correction, so it returns as soon as chrony is "synchronised", whatever the remaining correction. The `run_benchmark.sh` pre-flight only checks that the active source line (`^*`) contains `PHC`. `check_ptp.sh` prints chrony's `Last offset` and `RMS offset`, then calls the node "synced". `[CODE]`
- **Effect.** One-way latency is `rx_hw_PHC - tx_CLOCK_REALTIME`, so a wrong sender system clock goes straight into the number. The af_xdp notes record post-boot offsets of 814-890 ms sitting just under chrony's `makestep 1.0 3` threshold and slewing for ~27 minutes, while `Last offset`/`RMS offset` read nanoseconds. A run started soon after the ready file appears can pass every check with a bad clock. `[NOTES][AFX]` Not reproduced on this stack.
- **Fix.**
  1. In user-data, add a convergence step like af_xdp's `chrony-force-sync.service`: `chronyc waitsync 30 0.0001 0 1 || ...; chronyc makestep`. `[AFX bake-ami.sh]`
  2. Gate on `System time` (absolute outstanding error), as af_xdp's `clockOffsetUs()` does. `[AFX runner.go]`
  3. Apply the gate in the `run_benchmark.sh` pre-flight and in `check_ptp.sh`, and fail the run if it trips.
  4. Use a tight threshold. af_xdp's mcast gate is 10 µs (`max_clock_offset_us`); its 100 µs "broken clock" detector would be about two thirds of this benchmark's ~150 µs signal.
- **Effort.** S-M

### P0-2. The multicast result never reaches S3 or CloudWatch; the metrics describe the unicast baseline
- **Issue.** `collect_results.py` rejects `--tool mcast` (argparse exits 2) and only parses `--- SUBSCRIBER <id> ---` blocks. Phase A prints `<id> multicast loss: {json}` lines instead, which it does not capture. Only the sockperf and iperf3 phases print blocks. `[RUN]`
- **Effect.** `MeanLatencyUs`, `P99LatencyUs` and `PacketLossPct` in CloudWatch, and the JSON in S3, come from sockperf or iperf3. The README says they are the Phase A aggregates. Any alarm or dashboard built on them reports the wrong path.
- **Fix.** Add an `mcast` parser for the JSON lines (or have the orchestrator emit blocks for Phase A). Put the tool in the CloudWatch dimensions so metrics are never ambiguous. Reference: af_xdp stores every run with its mode and params, so a metric cannot lose its kind. `[AFX store.go]`
- **Effort.** S-M

### P0-3. A failed or silent subscriber disappears from the report
- **Issue.** `save_report.parse_phase_a` keeps only lines whose JSON parses. An empty result file is dropped. An error object such as `{"error":"SIOCSHWTSTAMP failed"}` is kept but contributes zeros. Aggregates filter to rows that have latency keys. `[RUN][CODE]`
- **Effect.** I fed it one healthy subscriber, one errored and one empty. The report printed `Total packet loss: 0/60000`, a zeros row for the errored one, and no row for the empty one. Only a "MIXED" label hinted at a problem. A run where most receivers died looks clean.
- **Fix.**
  1. The orchestrator knows the expected subscriber list. Compare it to the parsed results and exit non-zero on any missing or errored subscriber.
  2. Compute loss against `N x total_sent`, not the sum over survivors.
  3. Render error rows as errors, never as zeros.
  4. Add a bounded retry of the whole batch before giving up.
  - Reference: af_xdp records failed pairs as failures instead of publishing partial numbers, and retries via `dispatchRetry`. `[AFX orchestrator.go]`
- **Effort.** S-M

### P0-4. No detection of clock error in the results themselves
- **Issue.** `latencies_ns.append(rx_ns - tx_ns)` (`mcast_recv.py`) has no sign check and no counter. A negative latency flows silently into `min` and `mean`. `[CODE]`
- **Effect.** A wrong clock is indistinguishable from network behavior. The README's example max of 4.6 ms cannot be classified as jitter or a clock excursion.
- **Fix.** Record negative samples signed and never clamp them, and report the count and most-negative value. Reference: af_xdp's `mcast_receive` keeps `n_neg_total` and `min_neg_total_ns` and exports `clock_skew_samples`. `[AFX]`
  - Limit: with ~150 µs latency this only catches offsets larger than the minimum latency. It complements P0-1; it does not replace it.
  - Also keep the kernel software stamp (`CLOCK_REALTIME`) that `mcast_recv.py` already receives and discards (`sw_ns`), and report its gap to the hardware stamp for each run as a receiver-side clock-domain check. A gross gap means the receiver's system clock and its PHC disagree. See P1-7. `[CODE]`
- **Effort.** S

---

## P1 - biases or invalidates reported statistics

### P1-1. Aggregate percentiles are not valid statistics
- **Issue.** `collect_results.compute_aggregate_statistics` takes the mean of per-subscriber p95 and p99, and an unweighted mean of means. `save_report.py` takes the max of p99s instead, so the two reporting paths disagree for the same run. `[CODE]`
- **Effect.** On synthetic data, one subscriber with a 2% tail gave a reported p99 of 1170 µs against a true pooled p99 of 186 µs. A subscriber with only 3k of 60k samples moved the mean to 447 µs against 172 µs pooled. `[RUN]` The direction can go either way, so the README note that this "underestimates the tail" is not reliable. The synthetic data shows the mechanism, not the magnitude on real runs.
- **Fix.** Have each receiver emit a fixed-bucket histogram or an HdrHistogram (the repo already uses HdrHistogram in `hft_client`), pool them, and compute percentiles once. Keep per-subscriber rows. Reference: af_xdp reports per source/destination pair and never averages across pairs. `[AFX]`
- **Effort.** M

### P1-2. Cross-AZ aggregates mix different populations
- **Issue.** The publisher is always in `subnets[0]`; subscriber `i` is in `subnets[i % 3]`. In `cross-az`, subscriber 0 is same-AZ and the others are cross-AZ. `[CODE]`
- **Effect.** Any aggregate across subscribers averages unlike paths. It describes neither.
- **Fix.** Tag each subscriber with its AZ relation to the publisher in the report, aggregate only within a relation, and make the aggregate optional. Reference: af_xdp's scenario ladder (cpg / same-AZ / cross-AZ / cross-region) keeps each as its own result. `[AFX]`
- **Effort.** S

### P1-3. Absolute latency is biased high; the "TGW overhead" subtraction mixes methods
- **Issue.** The sender stamps `CLOCK_REALTIME` before `struct.pack` and `sendto()` in Python. The receiver uses the NIC's raw hardware stamp, which the af_xdp ledger found to be completion-referenced, not wire-referenced on ENA. `[CODE][NOTES]` The README's "TGW overhead = multicast one-way - unicast RTT/2" subtracts a sockperf C-path RTT from a Python-sender number.
- **Effect.** Both biases push the reported latency up. On my macOS loopback proxy the stamp-to-`sendto`-return window was about 14-17 µs; the real EC2 value is unknown. Treat absolute numbers as upper bounds. Comparisons between placements or runs are much sturdier because the biases largely cancel. `[RUN proxy]`
- **Fix.**
  1. Stamp after packing, as close to the send as possible. For absolute claims, port the sender to C++ (see P1-7); af_xdp's kernel-socket sender stamps in place just before `sendto()`. `[AFX mcast_send.cpp]`
  2. Measure the send path with the existing `latency_breakdown.py` and report it next to the headline.
  3. Label results "upper bound", and caveat the RTT/2 subtraction.
- **Effort.** S for labeling; the sender port is tracked in P1-7

### P1-4. Sender pacing drifts and the achieved rate is never reported
- **Issue.** `mcast_send.py` computes `target = tx_ns + interval_ns` from the stamp of each send, so any overshoot carries into the next interval. The receiver derives expected packets from the sender's count and never learns the requested rate. `[CODE]`
- **Effect.** The sender never reports what it achieved, so the offered load can silently differ from the one requested, and af_xdp found tail latency depends on offered rate (hop1 moved ~8 µs across 10k-100k pps). `[NOTES]` Measured shortfall of the Python sender on Linux (aarch64 containers, 10-15 s runs, two full passes): 0.3% at 10k pps, 1.3-1.4% at 50k pps, 3.0-3.5% at 100k pps, and up to 0.9% at 1k pps; the C++ sender held 100.0% at every rate. My earlier macOS proxy figures (-3.7% at 20k, -55% at 100k) overstated this and should not be used. `[RUN]` The default 1,000 pps is fine.
- **Fix.** Pace against an absolute deadline `t0 + seq * interval` (af_xdp uses `clock_nanosleep(TIMER_ABSTIME)`), print achieved vs requested pps over the active window, and flag or reject runs below a shortfall threshold. `[AFX mcast_send.cpp, runner.go achieved_pps]`
- **Effort.** S

### P1-5. Receiver readiness is not verified before sending
- **Issue.** Receivers are launched through SSM, then the script sleeps a fixed 5 s. The sockperf phase verifies its servers are listening; the multicast phase does not. `[CODE]`
- **Effect.** A receiver that is late (SSM latency, Python start, `SIOCSHWTSTAMP`) loses the first packets, and they are counted as TGW loss. With many subscribers the sequential launches make this worse. The ENA README says enabling or disabling hardware packet timestamping briefly disrupts networking. The receiver enables it at start and disables it at exit, and the fixed 5 s settle is the only guard around that. `[CODE]`
- **Fix.** Have the receiver write a ready marker after bind, join and the hardware-timestamp ioctl; poll for all of them before the sender starts; retry the batch on failure. Reference: af_xdp's `McastRxReady` probe and batch retry. `[AFX orchestrator.go]`
- **Effort.** S-M

### P1-6. Loss accounting hides duplicates and receiver-side drops, and there is no loss gate
- **Issue.** `loss = max(expected - received, 0)`. There is no sequence dedupe, no out-of-order count and no socket-drop counter. `[CODE]`
- **Effect.** Duplicates offset real loss. A Python receiver that falls behind at high rates drops at the socket buffer and reports it as network loss. Percentiles are computed from survivors only, so a lossy run reports a non-random subset.
- **Fix.** Track a seen-bitmap and count duplicates and out-of-order packets (af_xdp counts `ooo`). Read `UdpRcvbufErrors` or `SO_RXQ_OVFL` to separate receiver drops. Adopt af_xdp's loss gate (`max_loss_pct`, default 2%): reject rather than publish. `[AFX]`
- **Effort.** S-M

### P1-7. Native (C++) sender and receiver
- **Issue.** Both ends are Python. The sender busy-waits at 100% of a core and the receiver does a `recvmsg`, a `struct.unpack` and a list append per packet. The limits behind P0-4, P1-1, P1-3, P1-4 and P1-6 are partly set by this implementation. `[CODE]`
- **Donor code, sender.** `run_kernel_send()` in `af_xdp/tools/mcast_send.cpp` (lines 247-325) is a plain-UDP sender with the loop this benchmark needs: a preallocated buffer, only seq and timestamp rewritten per packet, the stamp taken immediately before `sendto()`, absolute-deadline pacing, and achieved vs requested pps. `[AFX]`
  - Change the destination from the replicator IP plus tunnel header to the multicast group, and add `IP_MULTICAST_IF`, `IP_MULTICAST_TTL` and the 4 MB send buffer the Python sender sets (`udp_send.cpp` has the multicast socket setup).
  - Keep the 12-byte `seq(4) + tx_ns(8)` payload and the end marker sent three times, so the existing receiver keeps working and tail loss stays exact.
  - Extract the function into a standalone file. `mcast_send.cpp` includes `xdp/xsk.h` and libbpf and the Makefile links `-lxdp`, which the TGW instances do not have. The user-data already installs `gcc-c++`, so build on the instance, or upload the source through the existing SSM path.
- **Donor code, receiver.** There is no hardware-stamp donor in af_xdp: its tools and replicator read no hardware RX stamp (no `SIOCSHWTSTAMP` outside a comment). That code comes from this stack's `mcast_recv.py`. From `af_xdp/tools/mcast_receive.cpp` take the statistics side: signed negative-sample counters, out-of-order count, achieved rate and JSON output. `[AFX]`
- **Expected benefits.**

| Benefit | Mechanism | Expected size | Status |
|---|---|---|---|
| Lower absolute latency | Removes interpreter, `struct.pack` allocation and GC from the stamp-to-send window | Unmeasured. The Python figure of 14-17 µs came from a macOS proxy and is not a Linux number. The syscall and driver submit (~1-3 µs) remain | Hypothesis; needs the EC2 A/B test below. Containers cannot answer it, because veth latency is dominated by the shared VM |
| Rate fidelity | Absolute-deadline pacing plus an achieved-rate report | Python shortfall 0.3% / 1.3-1.4% / 3.0-3.5% at 10k / 50k / 100k pps; C++ 100.0% at all. The port makes any shortfall visible | Measured (containers, two passes) |
| More samples per run | Higher usable send rates | Small: unpaced, C++ sent ~190k pps vs ~158k pps for Python (1.2x in both passes), because the kernel UDP path is the limit. My earlier 10-50x estimate was wrong | Measured (containers) |
| Receiver capacity | No per-packet Python objects | Both kept up at 100k pps with no loss in the container runs; C++ used 25-36% less CPU (2.5 vs 3.4 CPU-s at 50k pps, 2.9 vs 4.5 CPU-s at 100k pps, over 10 s). The larger gain is attribution, not capacity: see "Loss attribution" | Measured (containers) |
| Loss attribution | Sequence bitmap, `SO_RXQ_OVFL` and `UdpRcvbufErrors` | Under 2% loss and 1% duplication, Python reported about 720-740 lost packets and C++ about 1,460 on the same stream, because duplicates offset real loss. Unique + duplicates + end markers equals exactly what left the netem qdisc (74,278 = 74,278 in one run). With a CPU-starved receiver, C++ attributed all ~410,000 losses (407,106 in the final pass) to socket-buffer drops, and `SO_RXQ_OVFL` and `UdpRcvbufErrors` agreed to the packet | Measured (containers) |
| Constant memory, poolable statistics | Stream into a fixed-size histogram instead of storing every sample | Memory stays flat on long or high-rate runs; histograms merge across subscribers, which fixes P1-1 | By design |
| Clock-domain self-check | Read the kernel software stamp (`CLOCK_REALTIME`) and the hardware stamp (PHC) per packet; report both distributions and their gap | af_xdp measured that gap at 107 ns to 2.5 µs median on healthy hosts, so a gross value flags non-convergence on every run. The software distribution is also directly comparable to af_xdp's kernel-mode numbers | The Python receiver already receives both stamps and discards the software one, so this part does not need the port |
| Built-in diagnostics | Negative counter, out-of-order and duplicate bitmap, socket-drop counters, top-K outliers with sequence numbers, ready marker | Closes P0-4, P1-5, P1-6 and makes the repeated ~5 ms max (see `test_0.md`) diagnosable | By design |
| Comparable with af_xdp | Same loop, stamp placement, pacing and output fields | A like-for-like kernel-path vs TGW comparison | By design |

- **What it does not change.** ENA has no hardware TX stamp, the hardware RX stamp is completion-referenced rather than wire-referenced, the PHC error bound (14-30 µs, ~0.3 µs common-mode) is untouched, and clock convergence (P0-1) is still required. The port reduces our own instrumentation error and removes rate, CPU and memory limits. It does not lower the accuracy floor. `[NOTES]`
- **Design note: which receive stamp.** af_xdp uses software stamps on both ends for reasons that mostly do not apply here: AF_XDP has no socket cmsg path and the tested ENA driver lacked the XDP RX-metadata hook; its goal is endpoint cost, which a hardware stamp would erase; and matching stamp kinds on both ends keeps the bias cancellation symmetric. This benchmark has plain sockets and wants transit time, so the hardware RX stamp is the better primary. But with a software TX stamp the result depends on the sender's `CLOCK_REALTIME` matching the receiver's PHC, which is the P0-1 condition. Report both receive stamps. `[AFX][NOTES]`
- **Acceptance test.** On one fleet and placement, run at least 3 interleaved repeats.
  1. Python vs C++ sender with the receiver fixed. The median difference is the Python sender overhead.
  2. Python vs C++ receiver with the sender fixed. At 1,000 pps the difference should be about zero, because the NIC takes the stamp. A non-zero difference means the receiver is perturbing the run. Keep the Python implementations as the reference.
- **Container test results (implementation exists; see `scripts/native/`).** `tests/docker/harness.py` runs both implementations on real multicast between containers, with Python and C++ receivers on the same stream. Hardware stamps do not exist on veth, so both use a synthetic one (hw = sw - 700 ns): this validates the pipeline, accounting and capacity, not real latency and not the real ENA stamp. 12 profiles (1k to 100k pps, unpaced, 1,400 B payload, loss/duplication/reordering injection, 4 subscribers, a CPU-starved receiver, late join, missing end marker, SIGTERM). Checks include ground truth that does not come from the implementations: the interface packet counter equals receiver-raw plus socket drops, and unique + duplicates + end markers equals exactly what left the netem qdisc. Also verified: the shipped receiver refuses to run without a real hardware stamp, the unit tests pass on aarch64 and under x86-64 emulation, and the existing `save_report.py` parses the C++ receiver's output.
- **Not covered by container tests.** The real `SIOCSHWTSTAMP` path and raw hardware stamp, ENA behavior, real x86 hardware timing, and any real latency number. These need the EC2 run.
- **Effort.** Sender S-M, receiver M-L. Do the sender first.

---

## P2 - repeatability and interpretability

### P2-1. One short run, no warm-up, no repeats, no uncertainty
- **Issue.** The default is one 60 s run at 1,000 pps, which is 60k samples. All packets count, including the first. `[CODE]`
- **Effect.** p99 rests on ~600 samples, p99.9 on ~60, and max on one. The af_xdp notes show per-host variation larger than many placement effects (`phc_error_bound` 14-30 µs across hosts), so a single launch cannot separate placement from host luck. `[NOTES]`
- **Fix.** Discard the first 1-2 s; run at least 3 repeats and report the spread; report p99.9 only with at least 100k samples; add a bootstrap interval; repeat across separate launches. Note: af_xdp's `rtt` excludes warm-up (default 10,000), but its `mcast_receive` does not, so this is not a gap af_xdp has already closed. `[AFX rtt.cpp]`
- **Effort.** M

### P2-2. Clock uncertainty is not quantified
- **Issue.** No wander measurement or band. The only clock evidence is the active-source check. `[CODE]`
- **Effect.** The reader cannot tell how much of ~150 µs could be clock noise. By analogy with af_xdp (scaled MAD ~1.9 µs, worst pairwise 6.9 µs), it is probably a few percent here, but that is an inference from a different fleet. `[NOTES]`
- **Fix.** Optionally run `phcsample` (merged under `af_xdp/tools/`) beside Phase A and report the band; at minimum, record `chronyc tracking` before and after and apply P0-1's gate to both. `[AFX ACCURACY.md]`
- **Effort.** M

### P2-3. Report metadata and parser bugs
- **Issue.** (a) `save_report` searches for `Tool:` but the orchestrator prints `Tools:` (`[RUN]` regex check). (b) `--instance-type` says "auto-detect" but nothing detects it, so reports and filenames say `unknown`. (c) `collect_results.py` defaults `--instance-type` to `c6in.large`, which the stack's own PHC gate rejects. (d) The two sockperf parsers expect different text (`avg-rtt=`, `<MIN> observation` vs `Summary: Latency is … usec`, `min=`); `collect_results` extracted only a mean from text shaped like `save_report`'s patterns. `[RUN]`
- **Effect.** Stored reports carry wrong or missing config. Baseline numbers can silently parse as 0.0. Which sockperf parser matches real output is unverified.
- **Fix.** Capture real sockperf output as test fixtures; use one shared parser module; fill instance type from `describe-instances`; drop the stale default.
- **Effort.** S-M

### P2-4. Run context is not recorded
- **Issue.** Only `ethtool -T` is logged, and only to the instance. Missing from the report: kernel and ENA driver versions, AMI id, coalescing and offload settings, chrony state, CPU model. `[CODE]`
- **Effect.** Results cannot be compared across AMIs or instance sizes, and a later change cannot be traced to its cause.
- **Fix.** Capture the NIC and clock state into the report header, as af_xdp's `nic_tuning` command does so percentiles can be read against the state in effect. `[AFX runner.go]`
- **Effort.** S

### P2-5. Tests cover arithmetic, not validity or failure paths
- **Issue.** The 9 script tests pass (`[RUN]`, in a throwaway venv, since Hypothesis is not in the default environment). The aggregation property test asserts ordering and sum invariants against the implementation's own definition. `[CODE]`
- **Effect.** Nothing catches P0-2, P0-3, P1-1 or P2-3.
- **Fix.** Add fixture-based tests: error JSON, missing subscribers, mixed AZ, real sockperf text, and a pooled-vs-aggregated check.
- **Effort.** M

### P2-6. Chrony configuration parity and hazards
- **Issue.** The refclock line is appended to `/etc/chrony.conf` as `poll 0 delay 0.000010 prefer`. It omits the `dpoll -2` that af_xdp's AMI uses, there is no check that exactly one PHC refclock exists, and there is no `makestep`. `[CODE][AFX bake-ami.sh]`
- **Effect.** af_xdp hit a duplicate-refclock defect (two PHC sources that nodes could disagree on selecting). The same class of fault would pass this stack's `^*` check. The effect of `dpoll` on wander here is unmeasured.
- **Fix.** Add "exactly one PHC refclock" to the pre-flight (af_xdp gate G5) and match the AMI's refclock line, then measure whether it changes anything. `[AFX ACCURACY.md]`
- **Effort.** S

---

## P3 - hygiene, security, docs

### P3-1. Security posture
- **Issue.** The security group allows SSH (22) and the UDP port from `0.0.0.0/0` on public-IP instances; `cdk-nag` suppresses it. `cloudwatch:PutMetricData` is granted on `Resource: "*"`, and the suppression says that is required. `run_benchmark.sh` interpolates `--group` and `--port` into root commands sent over SSM without validation. `[CODE]`
- **Effect.** SSM already provides access, so open SSH is unnecessary exposure. The wildcard is broader than needed, and an in-repo example shows it can be scoped. The unvalidated arguments are low risk because the operator already holds SSM privileges.
- **Fix.** Remove the SSH rule or limit it to an admin CIDR (af_xdp auto-detects one). `[AFX afxdpctl up]` Add the `cloudwatch:namespace` condition used in `clock_bound_measure/stacks/clock_bound_stack.py`. Validate the group as an IPv4 multicast address and the port as an integer.
- **Effort.** S

### P3-2. Documentation drift
- **Issue.** The README references `scripts/plot_runs.py` (missing) and a "Measurement caveats" section (missing); describes CloudWatch metrics as Phase A (see P0-2); and says percentile averaging "underestimates" (see P1-1). `mcast_recv.py` uses two indexing conventions for the same field: its docstring and error text say `ts[2]` (third `timespec`), while the code reads flat `ts[4]`/`ts[5]`. The code is correct; the comments are confusing. `[CODE]`
- **Fix.** Correct the README after the code fixes, and write the real "Measurement caveats" section from P1-3.
- **Effort.** S

### P3-3. Scope limits worth stating
- **Issue.** A fixed 12-byte payload, one group, one sender and one flow. Phases A and B run minutes apart, so the baseline and multicast numbers cannot be paired. `--rate` means pps, mps or kbps depending on the tool. `[CODE]`
- **Fix.** Document these limits. Add a payload-size parameter (af_xdp has `size`) if size sensitivity matters.
- **Effort.** S

---

## Suggested order of work

1. **P0-1, P0-3, P0-4** together: they share the pre-flight and report plumbing and remove the silent failure modes.
2. **P0-2** with **P2-3**: the same parser and report code.
3. **P1-1 and P1-2**: change what is emitted by the receiver, then pool.
4. **P1-4, P1-5, P1-6**: the sender and receiver hardening. Specify them first, then fold them into the native port.
5. **P1-7**: port the sender (small, extracted from af_xdp's `run_kernel_send`), A/B it against the Python sender, then do the receiver.
6. **P1-3 and P2-1**: methodology, labeling and repeats.
7. The rest as time allows; **P3-1** is quick and worth doing early.

What af_xdp has that this benchmark lacks is mostly in two places: a reject-instead-of-publish discipline (convergence gate, loss gate, failed-pair records) and measured error terms (wander band, negative-sample counters). Porting those habits, not the AF_XDP datapath, accounts for most of the list.
