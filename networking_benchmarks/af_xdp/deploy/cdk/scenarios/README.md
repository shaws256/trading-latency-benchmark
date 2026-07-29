# scenarios/

Pre-built fleet specifications for common benchmark topologies.

Load via: `--context scenario=<subdir>/<name>` (e.g. `--context scenario=ucast/az-cpg-2`)

## ucast/ — Unicast RTT benchmarks

All nodes act as peers (role defaults to `replicator`). Measures point-to-point latency.

Cost/hr figures are rough and assume the default instance type (`c7i.2xlarge`);
use the AWS pricing calculator for current rates.

| File | Topology | Instances | Cost/hr |
|------|----------|-----------|---------|
| `az-cpg-2` | Same AZ, single cluster PG | 2 | ~$0.71 |
| `xaz-xcpg-14` | Cross AZ (a+b), 2 CPGs + 1 SPG per AZ | 14 | ~$5.00 |
| `xregion-2` | Cross region (us-east-1 ↔ eu-west-2), 1 per region | 2 | ~$0.71 + transfer |

## mcast/ — Multicast fan-out benchmarks

Explicit roles: source → replicator → destination(s). Measures fan-out latency.

| File | Topology | Instances | Cost/hr |
|------|----------|-----------|---------|
| `az-cpg-3` | Same AZ, CPG, source→replicator→destination | 3 | ~$1.07 |
| `az-spg-3` | Same AZ, spread, roles on different hardware | 3 | ~$1.07 |
| `xregion-3` | Cross region: source+replicator CPG, destination in eu-west-2 | 3 | ~$1.07 + transfer |
| `xregion-8` | Cross region: source+replicator us-east-1, 2 CPGs + 1 SPG in eu-west-2 | 8 | ~$2.85 + transfer |

## Custom scenarios

Create any JSON file with the `FleetEntry` schema:

```json
[
  {"count": 2, "pgType": "cluster", "pgName": "group-a"},
  {"count": 2, "pgType": "cluster", "pgName": "group-b", "az": "b"},
  {"count": 3, "pgType": "spread", "region": "eu-west-2"}
]
```

### FleetEntry fields

| Field | Default | Description |
|-------|---------|-------------|
| `type` | `c7i.2xlarge` | EC2 instance type |
| `count` | `1` | Number of instances |
| `role` | `replicator` | `source`, `replicator`, `destination` |
| `az` | `a` | AZ suffix or full name |
| `pgType` | none | `cluster`, `spread`, `partition` |
| `pgName` | auto | Named group — same name shares a PG |
| `region` | stack region | Triggers cross-region VPC peering |

Load via `--context fleet=@path/to/custom.json`
