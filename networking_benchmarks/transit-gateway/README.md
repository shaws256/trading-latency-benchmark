# `transit-gateway/` — TGW Multicast Benchmark CDK App

Self-contained AWS CDK (Python) app that provisions an environment to measure
**AWS Transit Gateway multicast** one-way latency, packet loss, and throughput.
Copy this folder into another project and it works given AWS credentials and a
CDK bootstrap.

---

## Table of contents

- [What this stack builds](#what-this-stack-builds)
- [Architecture](#architecture)
- [Deployment flow](#deployment-flow)
- [Runtime / measurement flow](#runtime--measurement-flow)
- [CDK context parameters](#cdk-context-parameters)
- [Placement strategies](#placement-strategies)
- [Instance-type gating (PHC requirement)](#instance-type-gating-phc-requirement)
- [User-data bootstrap](#user-data-bootstrap)
- [Stack outputs](#stack-outputs)
- [Setup, deploy and teardown](#setup-deploy-and-teardown)
- [Results](#results)
- [Test setup](#test-setup)
- [Repository layout](#repository-layout)

---

## What this stack builds

Single CloudFormation stack `TgwMulticastBenchmark`:

| Layer         | Resource(s)                                                                 |
|---------------|-----------------------------------------------------------------------------|
| Networking    | VPC `10.0.0.0/16`, IGW, public RT, 1–3 public subnets, VPC flow log → CW Logs |
| Transit GW    | TGW (multicast on), VPC attachment, multicast domain (IGMPv2 **off**, static sources **on**), per-subnet associations |
| Compute       | 1 publisher + N subscribers (default 3, max 256), each with own ENI, gp3 encrypted root |
| Placement     | Cluster placement group (only `single-az-cpg`)                              |
| Multicast     | Publisher ENI = source; each subscriber ENI = member                        |
| IAM           | `AmazonSSMManagedInstanceCore` + `s3:PutObject` (results bucket) + `cloudwatch:PutMetricData` |
| Storage       | Results S3 bucket, `aws:SecureTransport` deny policy                        |
| Security      | One SG: inbound UDP/`<multicast_port>` + TCP/22 from `0.0.0.0/0`, all egress |
| Observability | VPC flow log (7-day retention), user-data log to `/var/log/benchmark-setup.log` |
| Compliance    | `cdk-nag` `AwsSolutionsChecks` aspect at synth time, suppressions inline    |

---

## Architecture

```
                            ┌─────────────────────────────────────────────┐
                            │                Transit Gateway              │
                            │      (multicast_support = "enable")          │
                            │                                             │
                            │   Multicast Domain                          │
                            │   ├─ Igmpv2Support       = disable           │
                            │   └─ StaticSourcesSupport = enable           │
                            │                                             │
                            │   group 239.1.1.1                            │
                            │   ├─ source : Publisher ENI                  │
                            │   └─ members: Subscriber-0..N ENIs           │
                            └──────────────▲──────────────▲───────────────┘
                                           │              │
                          TGW VPC attachment (subnets)    │
                                           │              │
        ┌──────────────────────────────────┴──────────────┴───────────────────┐
        │                              VPC 10.0.0.0/16                        │
        │                                                                     │
        │   ┌────────── Subnet(s) (1 for single-az*, 3 for cross-az) ──────┐  │
        │   │                                                              │  │
        │   │   ┌────────────┐        UDP multicast        ┌────────────┐  │  │
        │   │   │ Publisher  │ ─────────► TGW ───────────► │ Subscriber │  │  │
        │   │   │  EC2 + ENI │   send (mcast_send.py)      │  EC2 + ENI │  │  │
        │   │   └─────┬──────┘                              └─────┬──────┘  │  │
        │   │         │                                           │         │  │
        │   │         │       Phase B: unicast UDP (RTT)          │         │  │
        │   │         └───────────────────────────────────────────┘         │  │
        │   │                                                              │  │
        │   └──────────────────────────────────────────────────────────────┘  │
        │                                                                     │
        │   IGW ──► default route 0.0.0.0/0   (public subnets, public IPs)     │
        └─────────────────────────────────────────────────────────────────────┘
                  │                                          │
                  ▼                                          ▼
            SSM Run Command                      S3 results bucket  +  CloudWatch metrics
            (orchestration plane)                (TLS-only,          (TGWMulticastBenchmark
                                                  scripts/         namespace, written by
                                                  collect_results.py) collect_results.py)
```

Two independent paths between publisher and subscribers: **multicast through
the TGW** (the path under measurement; subnets don't bridge multicast) and
**direct VPC unicast** (used as the baseline that gets subtracted). Control
plane is out-of-band over SSM and AWS APIs.

---

## Deployment flow

`cdk synth/deploy` reads context from `app.py` and runs `BenchmarkStack`,
which provisions resources in this order:

1. Validate instance family (PHC gate, fail-fast)
2. VPC + flow log + IGW + RT
3. Subnets per `placement_strategy` (1 or 3) + cluster PG (cpg only)
4. Security group
5. TGW + multicast domain + VPC attachment + per-subnet associations
6. Validate `num_subscribers ≤ 256`
7. AMI lookup (AL2023 unless `base_ami` given)
8. Results S3 bucket + TLS-only policy
9. EC2 IAM role + instance profile + user-data
10. 1× publisher + N× subscriber EC2/ENI
11. Multicast group source + N members (**explicit `add_dependency` on
    domain associations** — avoids a known race in
    `CreateTransitGatewayMulticastGroupSource`)
12. CfnOutputs + `cdk-nag` suppressions

`AwsSolutionsChecks` runs at synth. EC2 user-data executes after deploy — see
[User-data bootstrap](#user-data-bootstrap).

---

## Runtime / measurement flow

After `/opt/benchmark-env-ready` exists on every host, `scripts/run_benchmark.sh`
drives measurement via SSM:
```
  run_benchmark.sh
         │  (1) describe-stacks → publisher, subscribers, S3 bucket, AZs
         │  (2) wait for SSM, /opt/benchmark-env-ready, and (for native)
         │      /opt/benchmark-native-ready; refuse if the native build
         │      failed or instances built different commits
         │  (3) preflight.sh on every host: PHC, hardware RX stamping,
         │      one PHC refclock, chronyc makestep, System time <= 10 µs
         ▼
  Phase A - Multicast one-way latency + loss (TGW path)
         │     · subscribers: mcast_recv joins the group, enables hardware
         │       RX stamping, signals ready (retried once, else abort)
         │     · publisher : mcast_send paces --rate pps, reports achieved rate
         │     · receiver records hardware latency (primary) and software
         │       latency (for the divergence check), loss, duplicates, drops
         │     · detail histograms go to s3://<bucket>/runs/<Run-ID>/
         │     · chrony System time re-read on every host after the run
         ▼
  Phase B - Unicast RTT baseline (direct VPC, no TGW)
         │     · sockperf under-load from publisher → each subscriber
         ▼
  stdout (raw stream) → RUN VALID / RUN INVALID (exit 3)
         │
         ├─ --save-report → save_report.py → runs/*.md, *.raw.txt, *.details/
         └─ collect_results.py → S3 report + CloudWatch (valid runs only)
```
## CDK context parameters

Pass via `-c key=value` on `cdk synth/deploy`. Defaults in `app.py`.

| Parameter            | Type   | Default            | Notes                                                          |
|----------------------|--------|--------------------|----------------------------------------------------------------|
| `num_subscribers`    | int    | `3`                | Max **256** (TGW group member limit). Synth-time `ValueError` above. |
| `instance_type`      | string | `m7i.large`        | Must be PHC-capable — see below.                              |
| `multicast_group`    | string | `239.1.1.1`        | IPv4 group address.                                            |
| `multicast_port`     | int    | `5001`             | UDP port; opened in SG.                                        |
| `s3_bucket_name`     | string | *(auto)*           | Optional explicit results-bucket name.                         |
| `placement_strategy` | string | `single-az-cpg`    | `single-az-cpg` \| `single-az` \| `cross-az`. Else `ValueError`. |
| `base_ami`           | string | *(latest AL2023)*  | Optional override; resolved at synth otherwise.               |
| `native_repo`        | string | `https://github.com/aws-samples/trading-latency-benchmark.git` | `https://` repo the instances clone the native sender/receiver from. |
| `native_ref`         | string | `main`             | Branch, tag or full commit SHA to build. Must exist in `native_repo`. |
| `build_native`       | bool   | `true`             | `false` skips the native build (Python sender/receiver only).  |

The native sender/receiver is built **on each instance at launch** by a sparse
`git clone` of `native_repo` at `native_ref`, so the sources must be reachable
from the instances: a local, unpushed branch cannot be built. To test a branch
from a fork, push it and pass `-c native_repo=<fork url> -c native_ref=<branch>`.
During development you can instead copy the working tree to a running host and rebuild
(`tar czf` the `scripts/native` directory, `scp` it, `make -B BUILD=/opt/benchmark-native/bin all`; `-B` is needed because the archive keeps old file times and plain `make` can decide the binaries are current);
the instances have no key pair, so push a temporary key with
`aws ec2-instance-connect send-ssh-public-key` first. The orchestrator then reports
`repo=scp-working-tree` in `Native-Build`.
Both values are validated at synth (plain `https://` URL, plain ref) because they
are written into root-run user-data.

```bash
npx cdk deploy \
  -c num_subscribers=10 \
  -c instance_type=c7i.large \
  -c placement_strategy=cross-az \
  -c multicast_port=9999
```

---

## Placement strategies

| Strategy        | Subnets          | Placement group | Intent                                                 |
|-----------------|------------------|-----------------|--------------------------------------------------------|
| `single-az-cpg` | 1 / 1 AZ         | Cluster         | Lowest baseline; isolates pure TGW overhead.           |
| `single-az`     | 1 / 1 AZ         | None            | Quantifies the cluster-PG benefit.                     |
| `cross-az`      | 3 / 3 AZs        | None            | Adds cross-AZ propagation. Subscribers round-robin: `i % 3`. Publisher always in `subnets[0]`. |

---

## Instance-type gating (PHC requirement)

The receiver takes hardware RX timestamps via `SO_TIMESTAMPING` +
`SIOCSHWTSTAMP` against the **ENA PTP Hardware Clock (PHC)**. Without PHC,
software timestamps add microsecond-class noise that swamps the signal.

`BenchmarkStack._validate_phc_instance()` enforces this at synth. Allowed
family prefixes:

```
m7, c7, r7, i7, i8, c8, m8, r8, x8
```

Examples accepted: `m7i`, `m7g`, `m7a`, `c7i`, `c7g`, `r7i`, `r7iz`, `i8g`,
`c8g`, `m8g`, `x8g`. Examples rejected: `c6in`, `m6i`, `m5`, `c5n`, `t3`.

To bypass, edit `PHC_FAMILY_PREFIXES` in `stacks/benchmark_stack.py`.

---

## User-data bootstrap

Same user-data on publisher and subscribers. Logs to
`/var/log/benchmark-setup.log`.

| Phase | What it does                                                                                                                         |
|------:|---------------------------------------------------------------------------------------------------------------------------------------|
| **0** | Sysctl: `rmem_max=wmem_max=26214400`, `rp_filter=0`, `busy_poll=busy_read=50`.                                                       |
| **1** | `yum install` iperf3 + toolchain; build sockperf from `Mellanox/sockperf` to `/usr/local`.                                           |
| **1b**| Sparse `git clone` of `native_repo`@`native_ref`, `make` into `/opt/benchmark-native/bin`, record `BUILD_INFO` (commit). Success touches `/opt/benchmark-native-ready`, failure touches `/opt/benchmark-native-failed` (log: `/var/log/benchmark-native-build.log`). A failure does not stop the instance becoming ready for the Python path. |
| **2** | Load `ptp` module, write `options ena phc_enable=1`, reload ENA driver, find `ena-ptp` PHC, create `/dev/ptp_ena` via udev, configure `chrony` (`refclock PHC /dev/ptp_ena prefer`), then `chronyc waitsync 30 0.0001 0 1` (within 100 µs) and `chronyc makestep`. Falls back to NTP via `169.254.169.123` if no PHC. |
| **2b**| `ethtool -T <iface>` for diagnostic.                                                                                                  |
| **3** | `touch /opt/benchmark-env-ready` — gate polled by `run_benchmark.sh`.                                                                |

Phase 1 runs before Phase 2 so the ENA reload's brief network drop can't starve `yum`.

---

## Stack outputs

Used by `run_benchmark.sh` (`describe-stacks`):

| Output                  | Description                                  |
|-------------------------|----------------------------------------------|
| `PublisherInstanceId`   | Publisher EC2 instance ID.                   |
| `SubscriberInstanceIds` | Comma-separated subscriber instance IDs.     |
| `S3BucketName`          | Results bucket (auto-generated unless overridden). |
| `MulticastGroup`        | Multicast group address.                     |
| `MulticastPort`         | UDP port.                                    |

---

## Setup, deploy and teardown

```bash
cd transit-gateway

# One-time
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt -r requirements-dev.txt
npx cdk bootstrap

# Synth / deploy
npx cdk synth
npx cdk deploy
npx cdk deploy -c num_subscribers=5 -c placement_strategy=single-az-cpg -c instance_type=m7i.4xlarge

# Diagnose PTP
scripts/check_ptp.sh --stack-name TgwMulticastBenchmark

# Run (native sender/receiver by default; --impl python for the reference scripts)
scripts/run_benchmark.sh --stack-name TgwMulticastBenchmark \
  --rate 1000 --duration 60 --save-report

# Tear down
npx cdk destroy
```

Runtime deps (`requirements.txt`): `aws-cdk-lib`, `constructs`, `cdk-nag`
(the stack calls `NagSuppressions` with the 2.x API, so `cdk-nag<3` is needed),
and `boto3` for `collect_results.py`.
Dev deps (`requirements-dev.txt`): `pytest`, `hypothesis`.

---

## Results

Each invocation of `scripts/run_benchmark.sh` produces results in **three
places**, depending on how it was invoked:

### 1. stdout (always)

`run_benchmark.sh` prints a raw text stream; save it, it is the canonical record.
`scripts/tgw_results.py` documents the format and is the one parser behind
`save_report.py` and `collect_results.py`. For the multicast tool the stream has:

| Section | Content |
|---|---|
| header | `Run-ID`, `Impl: sender=… receiver=…`, `Instance-Type`, `Native-Build` (commit) |
| `--- SENDER <id> ---` | sender summary JSON: sent, achieved rate, send lateness, send errors |
| `--- MCAST <id> ---` | one JSON per subscriber, **always present**: AZ, same/cross-AZ, and the receiver result (or why there is none) |
| `--- CLOCK <id> pre\|post ---` | chrony `System time` on every instance before and after the run |
| `--- CONTEXT <id> ---` | host and NIC state in effect: kernel, ENA driver, AMI, CPU model, idle states, coalescing, channels, GRO, NAPI, IRQ balance, sysctls, chrony refclock line |

Receiver result fields: `total_expected`, `total_received`, `packet_loss_count`,
`duplicate_count`, `socket_drops`, `negative_hw_samples`, and
`min/median/mean/p95/p99/p999/max_latency_us` (hardware receive stamp minus
publisher user-space send time). The native receiver also records the software
receive stamp (`sw_*`) and the software-minus-hardware gap (`div_*`), and
writes full histograms to a detail file (see below).

**Warm-up and sample size.** The native receiver leaves the first 2 s of each run out of every
latency statistic (`MCAST_WARMUP_SECS`, `0` disables) while still counting those packets for
loss. P99.9 is reported only with 100,000 or more pooled samples. The sender summary includes
the time spent inside `sendto()` after the timestamp (`sendto_median_us`, `sendto_p99_us`,
`sendto_max_us`), which bounds how much of the number is the send path.

Phase B (sockperf) keeps its `--- SUBSCRIBER <id> ---` blocks: round-trip
unicast, **no TGW in the path**.

**Validity.** After a multicast run the script prints `RUN VALID` or
`RUN INVALID` with the reasons, and exits `3` if invalid. A run is invalid when
a subscriber returned nothing, loss exceeds `--max-loss-pct` (2 %), chrony
`System time` exceeds `--max-clock-offset-us` (10 µs) before or after, the
sender delivered less than 90 % of the requested rate or was interrupted, the
receiver and sender packet counts disagree, or any packet has a receive time
earlier than its send time. A software-minus-hardware median above 20 µs only
warns (provisional threshold).

**Interpreting the number.** The latency is an **upper bound** on Transit
Gateway transit: it also contains the publisher's send path and the clock
error between two hosts. `multicast one-way − unicast RTT/2` is not a clean
TGW overhead (the paths differ); a negative result means clock error or path
asymmetry, not a faster TGW.

### 2. `runs/` (if `--save-report`)

`--save-report` writes `runs/<N…>_<type>_<UTC>.{md,raw.txt}` and keeps the
receivers' histogram files in `runs/<Run-ID>.details/`.

| File         | Contents |
|--------------|----------|
| `*.md`       | Verdict and findings at the top; per-subscriber tables grouped by path (same-AZ vs cross-AZ, never merged); aggregate per path; clock state. |
| `*.raw.txt`  | Verbatim copy of the stream. Re-parseable by `save_report.py` / `collect_results.py`. |
| `*.details/` | `<instance>.detail.json`: per-receiver histograms, outliers, per-second series. |

Reports for invalid runs are still written (with the failures at the top) so a
bad run is visible. These files are machine-local; `runs/.gitkeep` is checked in.

**Aggregation.** Percentiles across subscribers are taken from the **merged
histograms** (not an average of per-subscriber percentiles), per path. Without
the detail files the report says so and gives only exact quantities: counts, a
sample-weighted mean, and the worst subscriber's p99.

### Transport: SSM or SSH

By default every command reaches the instances through SSM Run Command, which is what a
production deployment should use (no inbound port, IAM-audited, works in private subnets).
Each SSM command costs about 4 s of round trip. For development, `--transport ssh` sends the
same commands over SSH instead:

```bash
scripts/run_benchmark.sh --stack-name TgwMulticastBenchmark --transport ssh ...
```

- A throwaway key is generated per run (or pass `--ssh-key PATH`), pushed to each instance with
  EC2 Instance Connect (`ec2-instance-connect:SendSSHPublicKey`), and one SSH connection per
  instance is reused for every command. Commands run as root through `sudo bash -s`, like SSM.
- It needs public IPs and inbound port 22 (the stack's security group opens it), so it is a
  development option only.
- Both transports run the same scripts and produce the same raw stream. On an m7a.xlarge pair a
  10 s run took 48 s over SSM and 37 s over SSH (the send is 10 s of that); the difference is
  mostly per-command round trips. A run needs five command rounds: readiness, preflight and
  context, receiver start (stop, start and ready-wait together), the send, and collection (wait,
  result, detail upload and clock together).

### Comparing runs

One run says little about whether two configurations differ; the run-to-run spread on identical
hosts has been larger than the effects of interest. Summarise repeats with:

```bash
python3 scripts/compare_runs.py \
  --run native:runs/a.raw.txt --run native:runs/b.raw.txt ... \
  --run python:runs/c.raw.txt --run python:runs/d.raw.txt ...
```

It reports each run's pooled median/P95/P99, the spread, and a bootstrap interval over runs
(5+ runs per group; fewer gives the per-run values and min-max only), skips runs that fail a
gate, and for two groups gives the interval of the difference.

### 3. S3 + CloudWatch (`collect_results.py`)

```bash
python3 scripts/collect_results.py --input runs/<file>.raw.txt \
  --detail-dir runs/<Run-ID>.details --s3-bucket <S3BucketName>
```

`--tool` defaults to `mcast` (also `sockperf`, `iperf3`). An invalid mcast run
is **refused** (nothing uploaded or published, exit 3) unless `--allow-partial`,
which publishes it marked `"valid": false` with the findings listed.

- **Uploads** the JSON report to `s3://<bucket>/reports/<Run-ID>.json`
  (local `./benchmark-results/` fallback).
- **Publishes** to the **`TGWMulticastBenchmark`** namespace, dimensions
  `BenchmarkId`, `Tool`, and (mcast) `Path` (`same-az` / `cross-az`):
  `MeanLatencyUs` (sample-weighted), `MedianLatencyUs` / `P99LatencyUs` /
  `P999LatencyUs` (pooled; only when every subscriber supplied a histogram),
  `WorstSubscriberP99Us`, `PacketLossPct`.

For `sockperf` / `iperf3` the aggregate p99 remains the mean of per-subscriber
values (those tools report no histogram). sockperf prints no p95, so none is
stored. `sockperf under-load` is run with `--reply-every 1` (its default replies
to every 100th message, which also made sent minus received look like loss).

---

## Test setup

All tests in `tests/` are pure CDK synthesis tests — no AWS calls. They build
a `Template` from synthesized JSON and assert on it.

```bash
cd transit-gateway && source .venv/bin/activate
python -m pytest tests/ -v
```

Coverage:

- TGW: `MulticastSupport=enable`.
- Multicast domain: `Igmpv2Support=disable`, `StaticSourcesSupport=enable`
  (PascalCase enforced via `add_property_override`).
- SG: inbound UDP/`<multicast_port>`, TCP/22, all egress.
- User-data substrings: `chrony`, `/dev/ptp_ena`, `phc_enable=1`, `ethtool -T`,
  `sockperf`, `iperf3`; native build wiring (clone/ref/failure marker, 16 KB
  limit, bash syntax, injection rejection); the generated build script is run
  against a local repo.
- PHC gate: parametrized accept/reject lists.
- `num_subscribers=257` → `ValueError` matching `256`.
- `base_ami` used verbatim.
- Placement: subnet/PG counts, invalid-strategy rejection.
- Hypothesis property test: `num_subscribers ∈ [1,10]` → exactly N subscriber
  instances + N TGW members + N+1 total instances.

Orchestration scripts have their own tests under `scripts/tests/`: result
parsing and validity gates, the Python histogram port against the C++ one,
`preflight.sh` against stubbed `/sys` and `chronyc`, report/collector output,
and `run_benchmark.sh` end to end against a fake `aws` CLI (`fake_aws.py`).
They check control flow and output, not real instances or NICs.

```bash
python -m pytest scripts/tests -q          # run separately from tests/ (both are `tests` packages)
make -C scripts/native stats_test           # C++ unit tests
python3 scripts/native/tests/docker/harness.py   # sender/receiver load profiles in containers
```

---

## Repository layout

```
transit-gateway/
├── app.py                                  CDK entry point; reads context, applies AwsSolutionsChecks
├── cdk.json
├── requirements.txt / requirements-dev.txt
├── stacks/
│   └── benchmark_stack.py                  VPC, TGW, multicast, EC2, IAM, S3, user-data, PHC gate
├── scripts/
│   ├── run_benchmark.sh                    Orchestrator via SSM; emits the raw stream + verdict
│   ├── preflight.sh                        On-instance PHC / NIC / chrony System-time gate
│   ├── check_ptp.sh                        PTP diagnostic (reports chrony System time)
│   ├── tgw_results.py                      Shared parsing, histogram pooling, validity gates
│   ├── compare_runs.py                     Spread and bootstrap interval across repeated runs
│   ├── native/                             C++ sender/receiver (built on the instances); see its README
│   ├── mcast_recv.py                       Reference receiver (Python), kept for A/B
│   ├── mcast_send.py                       Reference sender (Python), kept for A/B
│   ├── collect_results.py                  Aggregate → S3 + CloudWatch
│   ├── save_report.py                      Markdown report → runs/
│   ├── latency_breakdown.py                Three-mode latency decomposition
│   ├── run_latency_breakdown.sh
│   └── tests/
│       ├── test_mcast_recv_smoke.py
│       └── test_property_aggregate_stats.py
├── runs/                                   Saved benchmark reports
├── tests/
│   ├── test_benchmark_stack.py
│   ├── test_placement_strategy.py
│   └── test_property_subscriber_count.py
└── cdk.out/                                Synth artifacts (generated)
```
