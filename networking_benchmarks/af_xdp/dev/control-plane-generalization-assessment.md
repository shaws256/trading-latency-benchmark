# Control-Plane Generalization Assessment

Can the af_xdp control plane (agents + backend + web) become a shared harness for
other benchmark backends in this repo - `mcast2ucast`, `open_onload` - and what
would that cost on each side?

Verdict: feasible, and the seam is unusually clean for code that was not designed
for it. The reason is structural rather than lucky: the transport and state layers
never learned what a measurement is. What needs real work is the *choreography*,
which is genuinely different per backend and should not be unified.

Companion to `pair-selection-lineage-design.md`. Written after the backend package
split (`refactor(af_xdp/cp): Split the backend into logical packages`), which is
what made the coupling measurable.

---

## 1. Where the coupling actually is

The package split turned "the backend is opinionated" into a number. Counting
references to af_xdp-specific concepts (`replicator`, `rtt`, `xdp`, `mcast`) per
file:

| Layer | Non-test lines | af_xdp refs | Reusable? |
|---|---:|---:|---|
| `hub` - SSE fan-out | 55 | **0** | as-is |
| `pairs` - target set + scope math | 206 | **0** | as-is |
| `ingest` - NATS subscriptions | 62 | 1 | as-is |
| `errorreg` - per-node error ring | 74 | 1 | as-is |
| `collector` - NxN matrix + history | 79 | 4 | near as-is |
| `api` - HTTP/SSE surface | 198 | 7 | near as-is |
| `store` - SQLite lineage | 472 | 9 | near as-is |
| `registry` - fleet state | 139 | 12 (comments/field names) | near as-is |
| `proto/messages.go` - wire contract | 246 | 51 | split needed |
| `orchestrator` - campaign choreography | 797 | **90** | split needed |

Roughly **1,285 lines are reusable close to as-is**; the opinion is concentrated
in two places: the command vocabulary in `proto`, and the choreography in
`orchestrator`.

The enabling property: everything in the top group only ever handles
`Telemetry{Kind, Variation, SrcIP, DstIP, Metrics}`, and `Kind`/`Variation` are
already free-form strings. Nothing in the fan-out, matrix, persistence or HTTP
layers interprets them.

---

## 2. The design decision: cut at the campaign, not the command

The tempting seam is a universal command vocabulary. It is the wrong cut.

Of the 13 command types, most are meaningless outside af_xdp: `set_fwd_mode`,
`join_group`, `purge_dests`, `replicator_svc`, `cleanup` (free the AF_XDP queue),
`ensure_host` (converge to an af_xdp host profile). Unifying these produces a
lowest-common-denominator RPC in which every backend smuggles its real needs
through a `map[string]any` - coupling with none of the benefit.

What differs between backends is not the data, it is the **algorithm**. af_xdp
groups its NxN by source because the measuring node's replicator must be *stopped*
first, which costs exactly two host-state transitions per node; that constraint is
why the loop is shaped the way it is. `open_onload` has no such constraint. That is
not a parameter, it is a different procedure.

### Proposed contract

```go
// A Driver owns everything backend-specific: what a campaign means, how hosts are
// prepared, and which tools run.
type Driver interface {
    Name() string
    Capabilities() Capabilities   // kinds, variations, roles, clock basis, metric semantics
    Plan(fleet []registry.Node, req RunRequest) (Plan, error)  // topology -> ordered pair set
    Execute(ctx context.Context, p Plan, d Dispatcher, e Emitter) error
}
```

The generic core retains `registry`, `hub`, `collector`, `store`, `pairs`, `api`,
`ingest`, plus a **campaign runner** owning what is genuinely shared: progress
events, cancellation, phase timing, the loss gate, and run lineage.

`pairs` generalizes because it already models an ordered `(src, dst)` axis with
scopes (`among` / `fanout` / `fanin`) and has no backend knowledge. The driver
decides what a pair *means*; `pairs` only decides which pairs exist.

---

## 3. Rework on the af_xdp side

Moderate, mostly mechanical, roughly 800 lines touched.

- **Split `orchestrator` (797 lines)** into a generic runner (~250: dispatch and
  retry, progress emission, cancel, phase timers, loss gate, run rows) and an
  af_xdp driver (~550: host profiles, replicator lifecycle, XDP attach/detach,
  purge, the source-grouped loop, mcast fwd-mode switching).
- **Split `proto`** into a generic envelope (registration, heartbeat, telemetry,
  command envelope, result) plus driver-owned command payloads.
- **Agent** already has the right shape: `runner.go` unmarshals tool output into
  `rttJSON` and adapts via `toMetrics()`. "Driver owns its tool -> Metrics adapter"
  is therefore an existing seam, not a new invention.

---

## 4. Rework per additional backend

### mcast2ucast

More tractable than it first appears. It ships `benchmarks/latency_sender` +
`latency_receiver` alongside the daemon, which is structurally the **same split**
as af_xdp's `replicator` (fabric) + `rtt` / `mcast_send` / `mcast_receive`
(measurement tools). It maps onto the existing model directly: the daemon is a
fabric to converge, the tools are what a pair runs.

Its host preparation differs in kind (DPDK `igb_uio` binding, TAP `mcast0`), so it
needs its own agent-side host-state logic. The registration/heartbeat/telemetry
half of the agent is reusable; the command-execution half is not.

### open_onload

The one that stresses the contract, and therefore the one worth integrating first
after af_xdp:

- **Two-role and one-way** (Feed Relay Server -> EC2 receiver), and the sender does
  not need onload at all. One endpoint may not be a fleet agent, which the registry
  currently assumes.
- **ENA PHC hardware RX timestamps**, a different clock epoch from af_xdp's
  `CLOCK_REALTIME`. `tools/rtt.cpp` states this explicitly: PHC timestamps "live in
  a separate epoch".

Both siblings need machine-readable result output for an agent to adapt.

---

## 5. Two existing leaks worth fixing regardless of this work

1. **`proto.Metrics.ServiceRTT`** (JSON `service_rtt_us`) already carries mcast
   **one-way** values. A misnomer today; actively wrong once `open_onload` reports
   one-way PHC latency. It wants a neutral name plus an explicit `Semantics` field
   (`rtt` | `one_way`) - the same distinction the HTML report already has to state
   in prose.

2. **Clock basis is hard-coded into the UI.** `report.js` contains af_xdp chrony and
   ENA-PHC facts as literals. With three backends on different clock bases the
   report must ask the driver, so clock basis belongs in `Capabilities()`.

### UI caveat, easy to underestimate

The web layer knows backend *semantics*, not just strings: 24 hardcoded
`ucast`/`mcast`/`kernel`/`xdp`/role literals across `App.svelte`, `controls.js`,
`live.js`, `report.js` - and worse, the mcast two-hop attribution
(`matrix[replIdx][di]`, reading the leg that carries the end-to-end value) is
driver knowledge living in `report.js`.

Generalizing needs a `/api/capabilities` endpoint so the UI renders what a driver
declares, plus a way for a driver to describe how its topology maps onto matrix
cells.

---

## 6. Sequencing

Each step independently shippable:

| # | Step | Why here |
|---|---|---|
| 1 | Extract the generic campaign runner; af_xdp becomes the first `Driver` | Forces the interface to be honest while only one implementation must keep working |
| 2 | Fix metric semantics + clock basis in the contract | Cheap, and both are wrong today |
| 3 | `/api/capabilities`; drive the UI from it | Removes the hardcoded vocabulary before a second vocabulary exists |
| 4 | Add `open_onload` as the second driver | Two-role / one-way / PHC is the case most likely to expose a wrong abstraction |
| 5 | Add `mcast2ucast` | Reuses the fabric+tools pattern af_xdp already establishes |

**Main risk: doing step 1 and step 4 in the wrong order.** Validating the interface
only against af_xdp bakes in NxN-mesh and shared-realtime-clock assumptions that
neither sibling satisfies. A design that survives `open_onload` will survive
`mcast2ucast`.

### Other risks

| Risk | Mitigation |
|---|---|
| Lowest-common-denominator command vocabulary | Cut at the campaign, not the command (section 2) |
| Registry assumes every endpoint is an agent | `open_onload`'s sender may be external; model non-agent endpoints before step 4 |
| Cross-epoch clock comparison | Clock basis in `Capabilities()`; the report states it per mode |
| Tool output not machine-readable | Per-driver adapter, as `toMetrics()` already does |

---

## 7. Confidence and what was not verified

Verified by reading source: the reference counts in section 1, the 13 command
types, the agent's `json.Unmarshal` + `toMetrics()` adapter path, the 24 hardcoded
UI literals, the mcast two-hop attribution in `report.js`, and `rtt.cpp`'s
statement about PHC living in a separate epoch.

Inferred from READMEs, not source, and therefore to confirm before step 4 or 5:

- `open_onload`'s exact result output (percentile stats to stdout plus optional
  `--output-csv`) and whether the sender can be driven by an agent at all.
- `mcast2ucast`'s `benchmarks/latency_*` output format, and whether its daemon can
  be converged idempotently the way `EnsureHost` does for af_xdp.

Line counts are current as of the package split and will drift.
