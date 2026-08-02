# control-plane — centralized orchestration + live monitoring for the AF_XDP benchmark

Replaces the fragile SSH/ansible + AWS-creds-in-the-loop orchestration with a
push-button control plane: a Go **agent** on every fleet node, a central Go
**backend** (registry + orchestrator + collector + API), a **NATS** control bus,
and a Svelte/three.js **web** app for live monitoring and launching tests. The
existing offline **report** generators (`gen/`) still turn a results dir into a
heatmap + topology model.

```
control-plane/
├── proto/     # shared wire contract (NATS subjects + message schemas) — Go, no deps
├── agent/     # per-node sidecar: IMDS self-register, run rtt/mcast, stream telemetry
├── backend/   # registry, NxN collector, orchestrator, HTTP+SSE API, serves web/
├── web/       # Svelte + three.js: live 2D/3D topology + shared control panel
└── gen/       # offline: per-pair JSON → heatmap + fleet.json (afxdp.topology/v1)
```

One Go module (`afxdp-cp`); agent + backend + proto share the wire contract. The
agent is merely *deployed* to nodes (via the AMI bake); the backend runs on a
small dedicated EC2 (see Deployment).

---

## Why NATS + agents (vs SSH/ansible)

The benchmark taught us that SSH-driven orchestration is fragile at the exact
moment it matters: an `--xdp-tx` run can grab NIC queue 0 and starve the SSH
session; creds expire mid-campaign; cross-VPC/region fans out into bespoke
inventories. The agent model fixes this:

- **Agent-outbound only.** Agents open ONE persistent NATS connection *outbound*
  to the backend. No inbound ports on fleet nodes, no SSH in the hot path, so a
  runaway XDP program can never lock you out of control.
- **Self-registration via IMDS.** Each agent discovers its own instance-id / IP /
  AZ / PG / role and registers — no hand-built inventory.
- **The agent owns the node's resource lifecycle** (queue-free, clock makestep,
  isolated-core pinning, replicator mode/service). The backend issues *intents*,
  not shell.

### NATS subjects (proto/subjects.go)

| Subject | Direction | Payload |
|---|---|---|
| `fleet.register` | agent → backend | `Registration` (on connect + periodically) |
| `fleet.heartbeat` | agent → backend | `Heartbeat` (liveness/state, every ~3s) |
| `fleet.telemetry` | agent → backend | `Telemetry` (one measurement sample) |
| `fleet.cmd.all` / `fleet.cmd.role.<role>` / `fleet.cmd.agent.<id>` | backend → agent | `Command` |
| `fleet.result.<id>` | agent → backend | `CommandResult` (correlated by `CmdID`) |

---

## Backend components + logic

- **registry.go** — authoritative in-memory fleet keyed by InstanceID. Upsert on
  register, update on heartbeat, staleness → offline. `Online()` / `ByRole()` /
  `AllByRole()` scope campaigns.
- **collector.go** — in-memory NxN matrix. Edges keyed **`kind|variation|src|dst`**
  (+ a p50 history ring). The `kind` in the key is deliberate: mcast fwd-mode
  `kernel` and ucast variation `kernel` share src→dst and would otherwise collide
  (a bug caught live — mcast telemetry silently overwrote the ucast kernel edge).
- **orchestrator.go** — dispatches commands, correlates results by `CmdID`, and
  runs campaigns (below). One campaign at a time (CAS guard).
- **ingest.go** — subscribes register/heartbeat/telemetry → updates registry/
  collector → pushes SSE deltas.
- **hub.go** — SSE fan-out; drops slow clients rather than blocking ingest.
- **api.go / main.go** — HTTP + SSE API, serves `web/`.

### Orchestrator optimizations

- **Round-based parallel NxN scheduler** (`scheduleRounds`). The contention rule
  is: a node may be in only one live measurement at a time (as sender *or* echo
  target). So all ordered pairs are packed into **node-disjoint rounds** — within
  a round no node appears twice, so every pair runs **concurrently**; rounds run
  serially behind a barrier. This turns the naive O(N²) serial matrix into
  ~2(N−1) rounds of up to N/2 concurrent pairs (≈O(N) wall-clock). (At N≤3 there
  is no concurrency to be had — any pair uses 2 of 3 nodes.)
- **Per-pair retry** (`dispatchRetry`, 2 attempts). Core NATS is at-most-once, so
  a dropped command/result is transient; a retry fills the matrix hole instead of
  leaving a gap (this is what turned a 23/24 run into 24/24).
- **mcast setup as explicit barriers** (`RunMcastMatrix`): per fwd mode →
  replicator `set_mode mcast+fwd` → destinations `join_group` → **clock gate**
  (`clock_sync`) → start receivers concurrently, fire the source send, await →
  queue-free cleanup. The whole run phase is itself retryable.
- **Serial-vs-concurrent correctness**: the agent **serializes command execution**
  (one AF_XDP queue + fixed `/tmp` result files), so even if two commands are
  delivered on different subscriptions they never race.
- **Self-healing**: agents re-register every ~5th heartbeat, so a backend restart
  repopulates the fleet within ~15s; an unknown-node heartbeat also gets an
  instant `reregister` nudge. Heartbeats that change nothing material do **not**
  trigger an SSE broadcast (avoids a broadcast storm at high node counts).

---

## Test variations + datapath semantics

Driven from the web panel or `POST /api/run`.

**ucast (round-trip through the echo replicator), variations:**

| variation | client TX | client RX | what it measures |
|---|---|---|---|
| `kernel` | `sendto()` | kernel busy-poll socket, kernel-SW RX ts | tuned kernel path (the honest floor) |
| `xdp-tx` | AF_XDP **zero-copy** | kernel busy-poll socket | removes the kernel TX stack |
| `xdp-rx` | `sendto()` | kernel socket, **XDP-stamped** ingress ts | instrumented kernel RX (NOT a bypass) |
| `xdp-txrx` | AF_XDP zero-copy | kernel socket + XDP-stamped ts | both |

> **Why xdp is not dramatically faster at QD=1 — and a bug that made it look
> worse.** This is a single-packet ping-pong, so AF_XDP's batching win is nil,
> and `--xdp-rx` does **not** bypass the RX stack (it only reads an earlier
> timestamp). The tuned kernel path (`SO_BUSY_POLL` + SCHED_FIFO + isolated cores
> + IRQ affinity + `gro_flush_timeout=10µs`) is ~36µs RT and hard to beat.
> Separately, the TX path was silently binding **copy/SKB** mode because the bind
> flags never requested `XDP_ZEROCOPY` — that added ~17µs and made xdp look
> consistently worse. Forcing zero-copy (see `tools/rtt.cpp`) brings `--xdp-tx`
> to **~35µs (min 28) vs kernel ~36µs (min 31)** — parity, with a lower floor.
> The startup line now prints `(zero-copy)` vs `(COPY/SKB fallback)`. Full detail
> in the `tools/rtt.cpp` header.

**mcast (one-way source → replicator fan-out → dest), fwd modes:** `copy`,
`inplace`, `kernel` — set on the replicator per mode; one-way latency uses the
XDP/PHC ingress stamp on the destination, gated on clock convergence.

---

## Security (NATS auth + TLS)

- **Token auth** everywhere: agent (`AGENT_NATS_TOKEN`), backend (`-nats-token`),
  `nats-server` `authorization { token }`. A wrong token is rejected with
  `Authorization Violation`.
- **Optional TLS**: agent (`AGENT_NATS_CA` to validate, or `AGENT_NATS_INSECURE`
  for self-signed), backend (`-nats-insecure`), `nats-server tls {}` with a
  self-signed cert generated on the host (`-c natsTls=true`).
- The `ControlPlaneStack` generates the token, writes `nats.conf`, and publishes
  the endpoint + token to SSM (`/af-xdp/nats-url`, `/af-xdp/nats-token`); baked
  agents fetch both at boot.
- **Known gap (lab-grade):** the token is a String SSM param (IAM-scoped to
  `af-xdp/*`), not `SecureString`; `clientCidr` defaults to `0.0.0.0/0`. Tighten
  both for anything beyond a lab (`-c clientCidr=…`, SecureString + KMS).

---

## Deployment (dedicated EC2 + CDK)

NATS needs a raw TCP endpoint, so a small dedicated EC2 beats ECS here. Fleets
live in separate VPCs/regions, so the control plane gets a **public EIP** and
agents connect *outbound* — no VPC peering.

```bash
# 1) Control plane (EC2 + EIP + SG + nats-server + backend; publishes SSM endpoint)
cdk deploy --context deploymentType=control-plane \
  --context keyPairName=<key> --context gitRepo=<repo> --context gitRef=<branch> \
  [--context clientCidr=1.2.3.4/32] [--context natsTls=true] \
  [--context hostedZoneId=Z... --context zoneName=example.com --context recordName=bench.example.com]

# 2) Fleet AMI: `bake-ami.sh` builds + installs the agent (systemd afxdp-agent.service)
#    and each fleet node stamps its AGENT_ROLE + fetches the NATS url/token from SSM.

# 3) Backend serves the web app; open http://<eip>:8080  (or the Route53 name)
```

> The `ControlPlaneStack` and the bake clone the repo to build, so the
> control-plane code must be committed + pushed to `gitRepo@gitRef` to deploy.

### HTTP + SSE API

| Endpoint | Purpose |
|---|---|
| `GET /api/fleet` | full snapshot `{nodes, edges}` |
| `GET /api/events` | SSE: `snapshot` on connect, then `node` / `edge` / `job` deltas |
| `POST /api/run` | start a campaign — `{"kind":"ucast","variation":…}` or `{"kind":"mcast","modes":[…]}` |
| `POST /api/cmd` | ad-hoc command to one agent `{"instance_id":…,"command":{…}}` |
| `GET /healthz` | liveness |

---

## Scale characteristics + limits

- Great for **tens** of nodes. The round scheduler makes ucast ~O(N) wall-clock;
  telemetry/heartbeat rates are trivial for NATS.
- Past hundreds of nodes: `Snapshot()` copies the whole NxN per `/api/fleet` and
  per new SSE client, collector history rings dominate memory, and the NxN viz
  itself becomes unusable — all need pagination / bounded snapshots / sampling.
- The web fully remounts the topology per live update (debounced 500ms); fine at
  tens of nodes, janky for very large N (prefer in-place edge updates there).

---

## Offline report pipeline (`gen/`)

Independently of the live path, a results dir of per-pair JSON can be turned into
a heatmap + topology model (used by `run_ucast.yaml` / `run_mcast.yaml`, and
reusable on any saved run):

```bash
python3 control-plane/gen/report.py      results/<date>/<run>
python3 control-plane/gen/fleet_json.py  results/<date>/<run>
```

| File | Produced by | Contents |
|---|---|---|
| `<src_ip>-<dst_ip>.json` | tools (`-j`) | per-pair `service_rtt_us` (+ `hop1_us`/`hop2_us` for mcast) |
| `matrix_report.html` | `report.py` | heatmap; hover → p50/p99/loss (+ hop split for mcast) |
| `matrix_summary.json` | `report.py` | full NxN incl. hop percentiles |
| `fleet.json` | `fleet_json.py` | `afxdp.topology/v1` model for the web viewer |

The same `fleet.json` schema is what the live backend adapts to, so the 2D/3D
viewer renders both saved runs and the live stream identically.

---

## Build + test

```bash
cd control-plane
go build ./... && go vet ./... && go test -race ./...   # backend + agent + proto
cd web && npm ci && npm run build                        # web/dist (served by the backend)
```
