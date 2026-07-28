# AF_XDP Benchmark — CDK Deployment

Fleet-driven CDK infrastructure for the AF_XDP latency benchmark. Deploys EC2 instances with configurable placement, multi-AZ, and cross-region topologies from JSON scenario files. Includes an AMI builder for pre-baked instances with zero provisioning time.

## Prerequisites

- AWS CDK CLI (`npm install -g aws-cdk`)
- AWS credentials configured (`aws configure` or `ada credentials update`)
- An EC2 key pair in the target region
- Node.js 18+

## Quick Start

```bash
npm install

# Option A: Deploy with stock AL2023 (requires ansible provisioning after)
cdk deploy --context keyPairName=virginia --context scenario=ucast/az-cpg-3

# Option B: Build AMI first, then deploy (fast — no provisioning needed)
cdk deploy --context keyPairName=virginia --context deploymentType=ami-builder
# ... wait ~15 min, get AmiId from output ...
cdk deploy --context keyPairName=virginia --context scenario=ucast/az-cpg-3 --context amiId=ami-xxx
```

## Deployment Types

| Type | Context | Description |
|------|---------|-------------|
| `fleet` (default) | `--context scenario=...` | Deploy EC2 fleet from a scenario/fleet spec |
| `ami-builder` | `--context deploymentType=ami-builder` | Build a pre-baked AMI with all deps + binaries |

## AMI Builder

Launches a temporary instance, installs all dependencies, compiles binaries, writes system configs, creates an AMI, and terminates the builder.

```bash
cdk deploy --context keyPairName=virginia --context deploymentType=ami-builder
```

### What's baked into the AMI

- Build toolchain (gcc, clang, libbpf-devel, etc.)
- xdp-tools built + installed (`/usr/local/lib/libxdp.so`)
- Benchmark binaries at `/opt/af-xdp/` (replicator, rtt, mcast_send, mcast_receive, replicator_ctl, udp_ping)
- ENA PHC hardware timestamping enabled
- chrony refclock PHC configured (±100-500ns clock sync)
- BPF JIT enabled, network sysctl tuned
- systemd units: interrupt coalescing off, ENA queue headroom, MTU 3498

### AMI builder parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `instanceType` | `c7i.xlarge` | Builder instance type (match target fleet class) |
| `gitRepo` | `aws-samples/trading-latency-benchmark` | Source repository |
| `gitRef` | `main` | Branch/tag/commit to build |

### Output

```
AmiId = ami-0abc123def456
```

Use with any fleet deployment: `--context amiId=ami-0abc123def456`

### Reliability

- **Auto-stop:** Builder instance stops itself after bake (success or failure) — no idle cost
- **CFN signal:** Reports success/failure to CloudFormation via `cfn-signal` (triggers rollback on failure)
- **Logs retained:** Bake log pushed to CloudWatch (`/af-xdp/ami-builder/<instance-id>`) before stopping
- **Lambda timeout:** 15 minutes for AMI creation + availability wait
- **Cleanup on failure:** Failed AMIs are deregistered, builder instance terminated

### With baked AMI vs without

| | Stock AL2023 | Baked AMI |
|---|---|---|
| Instance boot → ready | ~7-8 min (ansible provisioning) | ~30s (services start only) |
| Runtime ansible needed | Full: deps, build, configs, services | Minimal: GRE tunnel, start services |
| Iterating on code | rsync new code + rebuild | rsync + rebuild (or use stock for dev) |

## Fleet Specification

All fleet deployments are driven by a **fleet spec** — a JSON array of node entries:

```json
[
  {"count": 2, "role": "replicator", "placement": "cluster"},
  {"type": "c6in.4xlarge", "count": 1, "role": "destination", "placement": "cluster"}
]
```

### FleetEntry Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `type` | string | `c7i.xlarge` | EC2 instance type |
| `count` | number | `1` | Number of instances |
| `role` | string | `replicator` | Logical role: `source`, `replicator`, `destination` |
| `placement` | string | none | `cluster`, `spread`, or `partition` |
| `az` | string | `a` | AZ suffix (`a`, `b`) or full name (`us-east-1a`) |
| `region` | string | stack region | AWS region. Different value triggers cross-region VPC peering |

### Providing the Fleet Spec

Three methods, in priority order:

```bash
# 1. Named scenario (from scenarios/ directory)
cdk deploy --context keyPairName=virginia --context scenario=ucast/az-cpg-3

# 2. External file
cdk deploy --context keyPairName=virginia --context fleet=@path/to/fleet.json

# 3. Inline JSON
cdk deploy --context keyPairName=virginia --context fleet='[{"count":2,"placement":"cluster"}]'
```

## Scenarios

Pre-built scenarios in `scenarios/`:

### Unicast (`scenarios/ucast/`)

| File | Topology | Instances | Cost/hr |
|------|----------|-----------|---------|
| `az-cpg-3` | Same AZ, cluster placement group | 3 | ~$0.35 |
| `az-spg-7` | Same AZ, spread group | 7 | ~$0.83 |
| `xaz-spg-10` | Cross AZ (a+b), 3 cluster in A + 7 spread in B | 10 | ~$1.19 |
| `xregion-2` | Cross region (us-east-1 ↔ eu-west-2) | 2 | ~$0.24 + transfer |

### Multicast (`scenarios/mcast/`)

| File | Topology | Instances | Cost/hr |
|------|----------|-----------|---------|
| `az-cpg-3` | Same AZ, CPG, source→replicator→destination | 3 | ~$0.35 |
| `az-spg-3` | Same AZ, spread, all roles on different hardware | 3 | ~$0.35 |
| `xaz-3` | Cross AZ, source+replicator in AZ-a, destination in AZ-b | 3 | ~$0.35 |
| `xregion-3` | Cross region, destination in eu-west-2 | 3 | ~$0.35 + transfer |

## Multiple Simultaneous Stacks

Deploy multiple scenarios as independent CloudFormation stacks:

```bash
cdk deploy --context keyPairName=virginia --context scenario=ucast/az-cpg-3 --context stackName=cpg-bench
cdk deploy --context keyPairName=virginia --context scenario=ucast/az-spg-7 --context stackName=spg-bench

# Tear down one without affecting the other
cdk destroy --context stackName=cpg-bench
```

Default stack name: `XdpStack`.

## Placement Validation

The stack validates placement constraints at synth time:

- **Cluster**: All cluster-placed instances must share one AZ (fails with clear error if not)
- **Spread**: Max 7 instances per AZ per group (AWS hard limit)
- **Partition**: No count restriction (up to 7 partitions per AZ)

## Cross-Region

When any fleet entry has a `region` field different from the stack's primary region:

1. A secondary VPC is created in that region
2. VPC peering is established (same-account, auto-accepted)
3. Routes added on both sides
4. Security groups open UDP 5000 (data) and TCP 12345 (control) bidirectionally

```bash
cdk deploy --context keyPairName=virginia --context scenario=ucast/xregion-2 \
  --context secondaryKeyPairName=london
```

Only one secondary region is supported per stack.

## Context Parameters

| Parameter | Default | Description |
|-----------|---------|-------------|
| `keyPairName` | (required) | SSH key pair name |
| `deploymentType` | `fleet` | `fleet` or `ami-builder` |
| `region` | `us-east-1` | Primary AWS region |
| `stackName` | `XdpStack` | CloudFormation stack name |
| `scenario` | — | Scenario path (e.g. `ucast/az-cpg-3`) |
| `fleet` | — | Inline JSON or `@file.json` |
| `amiId` | AL2023 latest | Custom/baked AMI for primary region |
| `secondaryAmiId` | AL2023 latest | Custom AMI for secondary region |
| `secondaryKeyPairName` | same as `keyPairName` | Key pair in secondary region |
| `vpcCidr` | `10.61.0.0/16` | Primary VPC CIDR |
| `secondaryVpcCidr` | `10.62.0.0/16` | Secondary VPC CIDR |
| `dataPort` | `5000` | UDP data port for SG rules |
| `instanceType` | `c7i.xlarge` | AMI builder instance type |
| `gitRepo` | github.com/aws-samples/... | AMI builder source repo |
| `gitRef` | `main` | AMI builder git ref |

## Stack Outputs

### Fleet deployment

- `FleetManifest` — JSON array: `[{index, instanceType, role, az, region, placement, outputPrefix}]`
- `FleetSize` — total instance count
- `Node{N}InstanceId`, `Node{N}PublicIp`, `Node{N}PrivateIp` — per-instance
- `VpcId`, `AvailabilityZones`, `PlacementGroups` (when applicable)
- `PeeringConnectionId`, `SecondaryVpcId`, `SecondaryRegion` (cross-region only)

### AMI builder

- `AmiId` — the baked AMI ID
- `BuilderInstanceId` — temporary instance (terminated after AMI creation)

## Cleanup

```bash
cdk destroy --context keyPairName=virginia --context stackName=<name>
```

All resources have `RemovalPolicy.DESTROY` — `cdk destroy` removes everything cleanly.
