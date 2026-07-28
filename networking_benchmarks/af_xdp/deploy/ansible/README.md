# ansible/

Runtime configuration and provisioning playbooks for the AF_XDP benchmark.

## Two usage modes

### 1. With CDK FleetStack (default)

CDK deploys instances with `Role` tags. Ansible discovers them automatically via dynamic inventory:

```bash
ansible-playbook -i inventory.aws_ec2.yml byoi/provision.yaml        # stock AL2023 only
ansible-playbook -i inventory.aws_ec2.yml configure_mcast.yaml  # mcast topology
```

### 2. Without CDK — self-hosted instances (BYOI)

Use these playbooks on **any** EC2 instances you manage yourself (existing fleet, shared accounts, on-prem-like setups). Requirements:

1. **Tag your instances** with the `Role` tag so inventory can discover them:
   - `Role: source` — market data origin
   - `Role: replicator` — packet replicator node
   - `Role: destination` — latency measurement endpoint

2. **Ensure connectivity:**
   - SSH access from your control machine (port 22 or SSM)
   - Intra-fleet UDP 5000 + TCP 12345 open between nodes
   - Public IP or bastion for ansible to reach them

3. **Run playbooks** with a static or dynamic inventory:

```bash
# Option A: Use the dynamic inventory (discovers by Role tag)
export AWS_DEFAULT_REGION=us-east-1
ansible-playbook -i inventory.aws_ec2.yml byoi/provision.yaml

# Option B: Use a static inventory file
ansible-playbook -i hosts.ini provision.yaml
```

Example static inventory (`hosts.ini`):
```ini
[source]
10.0.1.10 ansible_user=ec2-user

[replicator]
10.0.1.20 ansible_user=ec2-user

[destination]
10.0.1.30 ansible_user=ec2-user
10.0.1.31 ansible_user=ec2-user
```

After provisioning, binaries are installed to `/opt/af-xdp/` and the `replicator.service` systemd unit is active (ucast-mode by default).

## Files

| File | Purpose | When to use |
|------|---------|-------------|
| `configure_mcast.yaml` | Multicast runtime config | After provisioning — GRE, replicator mode, registration |
| `run_ucast.yaml` | Run unicast NxN RTT benchmark | After provisioning — measures every pair |
| `run_mcast.yaml` | Run multicast fan-out benchmark | After configure_mcast — source→replicator→destinations |
| `report.yaml` | Generate HTML report from collected results | After any test run — heatmap, topology map, summary |
| `inventory.aws_ec2.yml` | Dynamic EC2 inventory by Role tag | With CDK-deployed or manually-tagged instances |
| `report/` | Python report generator + templates | Called by report.yaml |
| **`byoi/`** | | |
| `byoi/provision.yaml` | Full install from scratch | Stock AL2023 — self-hosted or CDK without baked AMI |

## Inventory

Uses `amazon.aws.aws_ec2` plugin. Groups instances by `Role` tag:

| Tag `Role` | Ansible group |
|------------|---------------|
| `source` | `source` |
| `replicator` | `replicator` |
| `destination` | `destination` |

Requires:
```bash
export SSH_KEY_FILE=~/.ssh/your-key.pem
export AWS_DEFAULT_REGION=us-east-1
```

## Workflows

### Unicast (no ansible needed after provisioning)

Instances boot with `replicator.service` in unicast-mode. Run RTT tests directly:

```bash
ssh ec2-user@<nodeA> '/opt/af-xdp/rtt <nodeB_ip> 5000 <nodeA_ip> 19020 1000 1000 100 0 1'
```

### Multicast

```bash
ansible-playbook -i inventory.aws_ec2.yml configure_mcast.yaml \
  -e replicator_private_ip=10.0.1.20
```

### Rebuild binaries (after code change)

```bash
ansible-playbook -i inventory.aws_ec2.yml byoi/provision.yaml -e rebuild=true
```

## configure_mcast.yaml — Plays

| Play | Hosts | What it does |
|------|-------|--------------|
| 1 | `source` | Creates GRE tunnel to replicator + NM dispatcher persistence |
| 2 | `replicator` | Writes `/etc/default/replicator` (mcast mode) + restarts service |
| 3 | `destination` | Registers each destination with replicator via control protocol |

### Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `replicator_private_ip` | (required) | Private IP of the replicator instance |
| `mcast_group` | `224.0.31.50` | Base multicast group |
| `base_mcast_group` | `224.0.31.50` | Per-destination group base (last octet incremented) |
| `data_port` | `5000` | UDP data port |

## provision.yaml — What it installs

- Build deps (gcc, clang, libbpf-devel, kernel-headers)
- xdp-tools from source (with AL2023 stdbool.h patch)
- Benchmark binaries → `/opt/af-xdp/`
- ENA PHC + chrony refclock PHC (±50-500ns sync)
- BPF JIT + network sysctl tuning
- systemd units (coalescing, queue headroom, MTU, replicator)
- Reboot for PHC activation

Pass `-e rebuild=true` to skip deps/configs and only rsync + rebuild binaries.

## Supported platforms

- Amazon Linux 2023 (x86_64) — primary target
- Any RHEL 9 / Fedora derivative with `dnf` should work (untested)
- Requires ENA NIC for AF_XDP mode; kernel-mode works on any Linux



## Dev - Rsync + Instance upgrade

# 1. Push to fork (from your laptop)
git push fork feature/afxdp-latency-improvements --force

# Check logs
ansible all -i inventory.aws_ec2.yml -b -m shell \
-a 'journalctl -u replicator -n 10 --no-pager' \
--ssh-extra-args="-o StrictHostKeyChecking=no"

# Reset
# Reboot if doesn't help
ansible all -i inventory.aws_ec2.yml -b -m reboot --ssh-extra-args="-o StrictHostKeyChecking=no"

ansible all -i inventory.aws_ec2.yml -b -m shell -a 'systemctl restart replicator && sleep 3 && \
systemctl is-active replicator' --ssh-extra-args="-o StrictHostKeyChecking=no"

# Dev playbook
ansible-playbook -i inventory.aws_ec2.yml byoi/dev.yaml
ansible-playbook -i inventory.aws_ec2.yml run_ucast.yaml

# For host restart - SSH host key check 
 \ --ssh-extra-args="-o StrictHostKeyChecking=no"