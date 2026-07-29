#!/usr/bin/env python3
"""
generate_matrix_report.py - Parse NxN latency matrix results and produce reports.

Reads all <src_ip>-<dst_ip>.json files in the results directory,
builds an NxN latency matrix, and generates:
  - A terminal-formatted matrix table (p50, p99)
  - An HTML heatmap report (matrix_report.html)
  - An interactive topology map visualization (topology_map.html)
  - Asymmetry analysis (A→B vs B→A differences)
  - Per-instance-type grouping statistics
  - Consolidated JSON matrix (matrix_summary.json)

Usage: python3 generate_matrix_report.py <results_dir>
"""

import json
import math
import os
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ─── Instance type metadata lookup table ──────────────────────────────────────
# Used when fleet.json doesn't include per-node hardware metadata.
INSTANCE_METADATA: Dict[str, Dict[str, Any]] = {
    # c7i family (Intel Sapphire Rapids, Nitro 5)
    "c7i.xlarge":    {"enis": 4,  "bw_gbps": 12.5, "pps_mpps": 2,   "nitro_gen": 5, "vcpus": 4,   "mem_gb": 8,    "metal": False},
    "c7i.2xlarge":   {"enis": 4,  "bw_gbps": 12.5, "pps_mpps": 2,   "nitro_gen": 5, "vcpus": 8,   "mem_gb": 16,   "metal": False},
    "c7i.4xlarge":   {"enis": 8,  "bw_gbps": 25,   "pps_mpps": 5,   "nitro_gen": 5, "vcpus": 16,  "mem_gb": 32,   "metal": False},
    "c7i.8xlarge":   {"enis": 8,  "bw_gbps": 25,   "pps_mpps": 5,   "nitro_gen": 5, "vcpus": 32,  "mem_gb": 64,   "metal": False},
    "c7i.12xlarge":  {"enis": 8,  "bw_gbps": 37.5, "pps_mpps": 7.5, "nitro_gen": 5, "vcpus": 48,  "mem_gb": 96,   "metal": False},
    "c7i.16xlarge":  {"enis": 15, "bw_gbps": 50,   "pps_mpps": 10,  "nitro_gen": 5, "vcpus": 64,  "mem_gb": 128,  "metal": False},
    "c7i.24xlarge":  {"enis": 15, "bw_gbps": 75,   "pps_mpps": 15,  "nitro_gen": 5, "vcpus": 96,  "mem_gb": 192,  "metal": False},
    "c7i.metal-24xl": {"enis": 15, "bw_gbps": 75,  "pps_mpps": 15,  "nitro_gen": 5, "vcpus": 96,  "mem_gb": 192,  "metal": True},
    "c7i.48xlarge":  {"enis": 15, "bw_gbps": 100,  "pps_mpps": 20,  "nitro_gen": 5, "vcpus": 192, "mem_gb": 384,  "metal": False},
    "c7i.metal-48xl": {"enis": 15, "bw_gbps": 100, "pps_mpps": 20,  "nitro_gen": 5, "vcpus": 192, "mem_gb": 384,  "metal": True},
    # c6in family (Intel Ice Lake, Nitro 4, enhanced networking)
    "c6in.xlarge":   {"enis": 4,  "bw_gbps": 30,   "pps_mpps": 6,   "nitro_gen": 4, "vcpus": 4,   "mem_gb": 8,    "metal": False},
    "c6in.2xlarge":  {"enis": 4,  "bw_gbps": 40,   "pps_mpps": 8,   "nitro_gen": 4, "vcpus": 8,   "mem_gb": 16,   "metal": False},
    "c6in.4xlarge":  {"enis": 8,  "bw_gbps": 50,   "pps_mpps": 10,  "nitro_gen": 4, "vcpus": 16,  "mem_gb": 32,   "metal": False},
    "c6in.8xlarge":  {"enis": 8,  "bw_gbps": 50,   "pps_mpps": 10,  "nitro_gen": 4, "vcpus": 32,  "mem_gb": 64,   "metal": False},
    "c6in.12xlarge": {"enis": 8,  "bw_gbps": 75,   "pps_mpps": 15,  "nitro_gen": 4, "vcpus": 48,  "mem_gb": 96,   "metal": False},
    "c6in.16xlarge": {"enis": 15, "bw_gbps": 100,  "pps_mpps": 20,  "nitro_gen": 4, "vcpus": 64,  "mem_gb": 128,  "metal": False},
    "c6in.24xlarge": {"enis": 15, "bw_gbps": 150,  "pps_mpps": 30,  "nitro_gen": 4, "vcpus": 96,  "mem_gb": 192,  "metal": False},
    "c6in.32xlarge": {"enis": 15, "bw_gbps": 200,  "pps_mpps": 40,  "nitro_gen": 4, "vcpus": 128, "mem_gb": 256,  "metal": False},
    "c6in.metal":    {"enis": 15, "bw_gbps": 200,  "pps_mpps": 40,  "nitro_gen": 4, "vcpus": 128, "mem_gb": 256,  "metal": True},
    # m7i family (Intel Sapphire Rapids, Nitro 5)
    "m7i.xlarge":    {"enis": 4,  "bw_gbps": 12.5, "pps_mpps": 2,   "nitro_gen": 5, "vcpus": 4,   "mem_gb": 16,   "metal": False},
    "m7i.2xlarge":   {"enis": 4,  "bw_gbps": 12.5, "pps_mpps": 2,   "nitro_gen": 5, "vcpus": 8,   "mem_gb": 32,   "metal": False},
    "m7i.4xlarge":   {"enis": 8,  "bw_gbps": 25,   "pps_mpps": 5,   "nitro_gen": 5, "vcpus": 16,  "mem_gb": 64,   "metal": False},
    "m7i.8xlarge":   {"enis": 8,  "bw_gbps": 25,   "pps_mpps": 5,   "nitro_gen": 5, "vcpus": 32,  "mem_gb": 128,  "metal": False},
    "m7i.12xlarge":  {"enis": 8,  "bw_gbps": 37.5, "pps_mpps": 7.5, "nitro_gen": 5, "vcpus": 48,  "mem_gb": 192,  "metal": False},
    "m7i.16xlarge":  {"enis": 15, "bw_gbps": 50,   "pps_mpps": 10,  "nitro_gen": 5, "vcpus": 64,  "mem_gb": 256,  "metal": False},
    "m7i.24xlarge":  {"enis": 15, "bw_gbps": 75,   "pps_mpps": 15,  "nitro_gen": 5, "vcpus": 96,  "mem_gb": 384,  "metal": False},
    "m7i.48xlarge":  {"enis": 15, "bw_gbps": 100,  "pps_mpps": 20,  "nitro_gen": 5, "vcpus": 192, "mem_gb": 768,  "metal": False},
    # r7i family (Intel Sapphire Rapids, Nitro 5, memory-optimized)
    "r7i.xlarge":    {"enis": 4,  "bw_gbps": 12.5, "pps_mpps": 2,   "nitro_gen": 5, "vcpus": 4,   "mem_gb": 32,   "metal": False},
    "r7i.2xlarge":   {"enis": 4,  "bw_gbps": 12.5, "pps_mpps": 2,   "nitro_gen": 5, "vcpus": 8,   "mem_gb": 64,   "metal": False},
    "r7i.4xlarge":   {"enis": 8,  "bw_gbps": 25,   "pps_mpps": 5,   "nitro_gen": 5, "vcpus": 16,  "mem_gb": 128,  "metal": False},
    "r7i.8xlarge":   {"enis": 8,  "bw_gbps": 25,   "pps_mpps": 5,   "nitro_gen": 5, "vcpus": 32,  "mem_gb": 256,  "metal": False},
    "r7i.12xlarge":  {"enis": 8,  "bw_gbps": 37.5, "pps_mpps": 7.5, "nitro_gen": 5, "vcpus": 48,  "mem_gb": 384,  "metal": False},
    "r7i.16xlarge":  {"enis": 15, "bw_gbps": 50,   "pps_mpps": 10,  "nitro_gen": 5, "vcpus": 64,  "mem_gb": 512,  "metal": False},
    "r7i.24xlarge":  {"enis": 15, "bw_gbps": 75,   "pps_mpps": 15,  "nitro_gen": 5, "vcpus": 96,  "mem_gb": 768,  "metal": False},
    # c6i family (Intel Ice Lake, Nitro 4)
    "c6i.xlarge":    {"enis": 4,  "bw_gbps": 12.5, "pps_mpps": 2,   "nitro_gen": 4, "vcpus": 4,   "mem_gb": 8,    "metal": False},
    "c6i.2xlarge":   {"enis": 4,  "bw_gbps": 12.5, "pps_mpps": 2,   "nitro_gen": 4, "vcpus": 8,   "mem_gb": 16,   "metal": False},
    "c6i.4xlarge":   {"enis": 8,  "bw_gbps": 25,   "pps_mpps": 5,   "nitro_gen": 4, "vcpus": 16,  "mem_gb": 32,   "metal": False},
    "c6i.8xlarge":   {"enis": 8,  "bw_gbps": 25,   "pps_mpps": 5,   "nitro_gen": 4, "vcpus": 32,  "mem_gb": 64,   "metal": False},
    "c6i.metal":     {"enis": 15, "bw_gbps": 50,   "pps_mpps": 10,  "nitro_gen": 4, "vcpus": 128, "mem_gb": 256,  "metal": True},
}

# Fallback metadata for unknown instance types (derive from size suffix)
_SIZE_VCPUS = {
    "xlarge": 4, "2xlarge": 8, "4xlarge": 16, "8xlarge": 32,
    "12xlarge": 48, "16xlarge": 64, "24xlarge": 96, "48xlarge": 192,
}


def _default_metadata_for_type(instance_type: str) -> Dict[str, Any]:
    """Generate sensible defaults for an unknown instance type."""
    parts = instance_type.split(".")
    family = parts[0] if parts else "unknown"
    size = parts[1] if len(parts) > 1 else "xlarge"
    vcpus = _SIZE_VCPUS.get(size, 4)
    is_metal = "metal" in size
    return {
        "enis": 8 if vcpus >= 16 else 4,
        "bw_gbps": min(vcpus * 1.5, 100),
        "pps_mpps": min(vcpus * 0.3, 20),
        "nitro_gen": 5 if "7" in family else 4,
        "vcpus": vcpus,
        "mem_gb": vcpus * 4 if "c" in family else vcpus * 8,
        "metal": is_metal,
    }


def get_instance_metadata(instance_type: str) -> Dict[str, Any]:
    """Look up or infer hardware metadata for an instance type."""
    if instance_type in INSTANCE_METADATA:
        return INSTANCE_METADATA[instance_type]
    return _default_metadata_for_type(instance_type)


# ─── Core data loading and parsing ────────────────────────────────────────────


def load_fleet_metadata(results_dir: Path) -> dict:
    """Load fleet metadata for the run.

    Prefers a consolidated ``fleet.json`` (``{"nodes": [...]}``) when present.
    Otherwise assembles the fleet from the per-node ``<ip>_metadata.json`` files
    that ``run_ucast.yaml`` writes (fields: ``instance_type``, ``az``, ``region``,
    ``pg_name``, ``pg_type``, ``private_ip``, ``hostname`` and — for newer runs —
    ``account``/``vpc_id``).

    An account id may be injected via the ``AFXDP_ACCOUNT`` env var to stamp runs
    whose metadata predates account capture (used only to draw the account
    boundary; it does not alter measured data).
    """
    account_override = os.environ.get("AFXDP_ACCOUNT")

    fleet_path = results_dir / "fleet.json"
    if fleet_path.exists():
        try:
            fleet = json.loads(fleet_path.read_text())
            if account_override:
                fleet.setdefault("account", account_override)
                for nd in fleet.get("nodes", []):
                    nd.setdefault("account", account_override)
            return fleet
        except json.JSONDecodeError:
            print(f"  Warning: could not parse {fleet_path}")

    # Assemble from per-node <ip>_metadata.json files.
    nodes: List[Dict[str, Any]] = []
    for mf in sorted(results_dir.glob("*_metadata.json")):
        try:
            m = json.loads(mf.read_text())
        except json.JSONDecodeError:
            print(f"  Warning: could not parse {mf}")
            continue
        ip = m.get("private_ip") or mf.stem.replace("_metadata", "")
        nodes.append({
            "name": ip,
            "private_ip": ip,
            "public_ip": m.get("hostname") or m.get("public_ip") or "",
            "ec2_name": m.get("ec2_name") or ip,
            "type": m.get("instance_type") or "unknown",
            "az": m.get("az") or "unknown",
            "region": m.get("region") or "unknown",
            "account": m.get("account") or account_override or "unknown",
            "vpc_id": m.get("vpc_id") or "unknown",
            "cpg_name": m.get("pg_name") or "unknown",
            "pg_type": m.get("pg_type") or "unknown",
        })

    if not nodes:
        return {"account": account_override} if account_override else {}

    return {
        "nodes": nodes,
        "region": nodes[0]["region"],
        "account": nodes[0]["account"],
    }


def parse_result_json(filepath: Path) -> Optional[dict]:
    """Parse a rtt JSON result file."""
    try:
        raw = json.loads(filepath.read_text())
        svc = raw.get("service_rtt_us", {})
        if not svc:
            return None
        return {
            "min_us": svc.get("min", 0),
            "p50_us": svc.get("p50", 0),
            "p90_us": svc.get("p90", 0),
            "p95_us": svc.get("p95", 0),
            "p99_us": svc.get("p99", 0),
            "p999_us": svc.get("p999", 0),
            "max_us": svc.get("max", 0),
            "mean_us": svc.get("mean", 0),
            "messages": raw.get("messages", 0),
            "lost": raw.get("lost", 0),
            "loss_pct": raw.get("loss_pct", 0.0),
            "timestamp_rx": raw.get("timestamp_rx", "unknown"),
            "timestamp_tx": raw.get("timestamp_tx", "unknown"),
        }
    except (json.JSONDecodeError, FileNotFoundError, KeyError):
        return None


def parse_result_txt(filepath: Path) -> Optional[dict]:
    """Fallback: parse rtt text output if JSON is missing."""
    try:
        text = filepath.read_text()
    except FileNotFoundError:
        return None
    patterns = {
        "p50_us": r"p50\s*[=:]\s*(\d+)",
        "p90_us": r"p90\s*[=:]\s*(\d+)",
        "p95_us": r"p95\s*[=:]\s*(\d+)",
        "p99_us": r"p99\s*[=:]\s*(\d+)",
        "p999_us": r"p99\.9\s*[=:]\s*(\d+)",
        "min_us": r"min\s*[=:]\s*(\d+)",
        "max_us": r"max\s*[=:]\s*(\d+)",
        "mean_us": r"mean\s*[=:]\s*([\d.]+)",
    }
    data: Dict[str, Any] = {}
    for key, pattern in patterns.items():
        m = re.search(pattern, text, re.IGNORECASE)
        if m:
            val = m.group(1)
            data[key] = float(val) if "." in val else int(val)
    return data if data.get("p50_us") else None


def build_matrix(results_dir: Path, fleet: dict) -> Tuple[List[str], Dict[Tuple[str, str], dict]]:
    """Build the NxN matrix from result files.

    Supports both naming conventions:
      - New: <src_ip>-<dst_ip>.json (e.g. 10.61.0.197-10.61.0.205.json)
      - Legacy: <src>_to_<dst>.json (e.g. node0_to_node1.json)

    Returns (node_names, matrix_dict) where matrix_dict[(src,dst)] = latency_data.
    """
    nodes = fleet.get("nodes", [])
    node_names = [n["name"] for n in nodes] if nodes else []
    matrix: Dict[Tuple[str, str], dict] = {}

    # Try new naming first: <src_ip>-<dst_ip>.json (IP addresses contain dots)
    ip_pattern = re.compile(r"^(\d+\.\d+\.\d+\.\d+)-(\d+\.\d+\.\d+\.\d+)$")
    found_new = False
    for jf in sorted(results_dir.glob("*.json")):
        if jf.stem.endswith("_metadata") or jf.stem in ("matrix_summary", "fleet"):
            continue
        m = ip_pattern.match(jf.stem)
        if m:
            found_new = True
            src_name, dst_name = m.group(1), m.group(2)
            data = parse_result_json(jf)
            if data:
                matrix[(src_name, dst_name)] = data
                if src_name not in node_names:
                    node_names.append(src_name)
                if dst_name not in node_names:
                    node_names.append(dst_name)

    # Fallback: legacy *_to_*.json naming
    if not found_new:
        for jf in sorted(results_dir.glob("*_to_*.json")):
            stem = jf.stem
            m = re.match(r"(.+?)_to_(.+)", stem)
            if not m:
                continue
            src_name, dst_name = m.group(1), m.group(2)

            data = parse_result_json(jf)
            if not data:
                txt = jf.with_suffix(".txt")
                if txt.exists():
                    data = parse_result_txt(txt)
            if data:
                matrix[(src_name, dst_name)] = data
                if src_name not in node_names:
                    node_names.append(src_name)
                if dst_name not in node_names:
                    node_names.append(dst_name)

    node_names = sorted(set(node_names))
    return node_names, matrix


# ─── Terminal output ──────────────────────────────────────────────────────────


def generate_terminal_table(node_names: List[str], matrix: dict, metric: str = "p50_us") -> None:
    """Print NxN matrix table to stdout."""
    n = len(node_names)
    short_names = []
    for name in node_names:
        parts = name.split("-")
        idx = parts[1] if len(parts) > 1 else "?"
        itype = "-".join(parts[2:3]) if len(parts) > 2 else "?"
        short_names.append(f"n{idx}:{itype}")

    col_width = max(10, max(len(s) for s in short_names) + 2)
    header_metric = metric.replace("_us", "").upper()

    print(f"\n{'=' * (col_width * (n + 1) + 4)}")
    print(f"  LATENCY MATRIX ({header_metric}, microseconds) — src→dst")
    print(f"{'─' * (col_width * (n + 1) + 4)}")

    print(f"{'FROM \\ TO':<{col_width}}", end="")
    for name in short_names:
        print(f"{name:>{col_width}}", end="")
    print()
    print("─" * (col_width * (n + 1) + 4))

    for i, src in enumerate(node_names):
        print(f"{short_names[i]:<{col_width}}", end="")
        for j, dst in enumerate(node_names):
            if i == j:
                print(f"{'—':>{col_width}}", end="")
            else:
                data = matrix.get((src, dst))
                if data:
                    val = data.get(metric, 0)
                    print(f"{val:>{col_width}}", end="")
                else:
                    print(f"{'N/A':>{col_width}}", end="")
        print()

    print(f"{'=' * (col_width * (n + 1) + 4)}")


# ─── Analysis helpers ─────────────────────────────────────────────────────────


def compute_asymmetry(node_names: List[str], matrix: dict) -> List[dict]:
    """Compute A→B vs B→A asymmetry for all pairs."""
    asymmetries = []
    seen: set = set()
    for i, a in enumerate(node_names):
        for j, b in enumerate(node_names):
            if i >= j:
                continue
            pair_key = (min(a, b), max(a, b))
            if pair_key in seen:
                continue
            seen.add(pair_key)

            ab = matrix.get((a, b), {}).get("p50_us")
            ba = matrix.get((b, a), {}).get("p50_us")
            if ab and ba:
                diff = abs(ab - ba)
                pct = (diff / min(ab, ba)) * 100 if min(ab, ba) > 0 else 0
                asymmetries.append({
                    "pair": f"{a} ↔ {b}",
                    "a_to_b_p50": ab,
                    "b_to_a_p50": ba,
                    "diff_us": diff,
                    "diff_pct": round(pct, 1),
                })
    return sorted(asymmetries, key=lambda x: x["diff_us"], reverse=True)


def compute_type_stats(fleet: dict, matrix: dict) -> dict:
    """Group results by instance type pair and compute aggregate stats."""
    nodes = {n["name"]: n["type"] for n in fleet.get("nodes", [])}
    type_pairs: Dict[str, List[int]] = {}

    for (src, dst), data in matrix.items():
        src_type = nodes.get(src, "unknown")
        dst_type = nodes.get(dst, "unknown")
        pair_label = f"{src_type} → {dst_type}"
        if pair_label not in type_pairs:
            type_pairs[pair_label] = []
        p50 = data.get("p50_us", 0)
        if p50 > 0:
            type_pairs[pair_label].append(p50)

    stats = {}
    for label, values in sorted(type_pairs.items()):
        if values:
            stats[label] = {
                "count": len(values),
                "min_p50": min(values),
                "max_p50": max(values),
                "mean_p50": round(sum(values) / len(values), 1),
                "spread": max(values) - min(values),
            }
    return stats


# ─── HTML heatmap report (original) ──────────────────────────────────────────


def color_for_value(value: float, vmin: float, vmax: float) -> str:
    """Generate a CSS color from green (low latency) to red (high latency)."""
    if vmax == vmin:
        return "#4CAF50"
    t = min(1.0, max(0.0, (value - vmin) / (vmax - vmin)))
    if t < 0.5:
        r = int(76 + (255 - 76) * (t * 2))
        g = int(175 + (235 - 175) * (t * 2))
        b = int(80 - 80 * (t * 2))
    else:
        t2 = (t - 0.5) * 2
        r = 255
        g = int(235 - 235 * t2)
        b = 0
    return f"#{r:02x}{g:02x}{b:02x}"


def fmt_lat(us) -> str:
    """Format microseconds: >=500us -> ms (0.5 ms), >=500ms -> s (0.5 s)."""
    try:
        v = float(us)
    except (TypeError, ValueError):
        return "\u2014"
    if v <= 0:
        return "\u2014"
    def trim(x: float) -> str:
        return f"{x:.2f}".rstrip("0").rstrip(".")
    if v >= 500000:
        return f"{trim(v / 1e6)} s"
    if v >= 500:
        return f"{trim(v / 1e3)} ms"
    return f"{int(round(v))} \u03bcs"


def generate_html_report(node_names: List[str], matrix: dict, fleet: dict,
                         asymmetries: list, type_stats: dict, output_path: Path) -> None:
    """Generate a full HTML heatmap report (matrix_report.html)."""
    n = len(node_names)
    nodes_meta = {nd["name"]: nd for nd in fleet.get("nodes", [])}

    all_p50 = [d.get("p50_us", 0) for d in matrix.values() if d.get("p50_us", 0) > 0]
    all_p99 = [d.get("p99_us", 0) for d in matrix.values() if d.get("p99_us", 0) > 0]
    vmin_p50 = min(all_p50) if all_p50 else 0
    vmax_p50 = max(all_p50) if all_p50 else 100
    vmin_p99 = min(all_p99) if all_p99 else 0
    vmax_p99 = max(all_p99) if all_p99 else 100

    def short_name(name: str) -> str:
        meta = nodes_meta.get(name, {})
        idx = meta.get("index", "?")
        itype = meta.get("type", name.split("-", 2)[-1] if "-" in name else name)
        return f"n{idx}<br><small>{itype}</small>"

    def build_heatmap(metric: str, vmin: float, vmax: float) -> str:
        rows = ""
        for i, src in enumerate(node_names):
            cells = f"<td class='row-hdr'>{short_name(src)}</td>"
            for j, dst in enumerate(node_names):
                if i == j:
                    cells += "<td class='diag'>—</td>"
                else:
                    data = matrix.get((src, dst))
                    if data and data.get(metric, 0) > 0:
                        val = data[metric]
                        color = color_for_value(val, vmin, vmax)
                        tooltip = (f"{src} → {dst}\\n"
                                   f"p50={fmt_lat(data.get('p50_us',0))}  "
                                   f"p99={fmt_lat(data.get('p99_us',0))}  "
                                   f"p99.9={fmt_lat(data.get('p999_us',0))}  "
                                   f"max={fmt_lat(data.get('max_us',0))}  "
                                   f"loss={data.get('loss_pct',0):.2f}%")
                        cells += (f"<td style='background:{color}' title='{tooltip}'>"
                                  f"<strong>{fmt_lat(val)}</strong></td>")
                    else:
                        cells += "<td class='na'>N/A</td>"
            rows += f"<tr>{cells}</tr>\n"
        header_cells = "<th></th>" + "".join(f"<th>{short_name(n)}</th>" for n in node_names)
        return f"<table class='matrix'><tr>{header_cells}</tr>\n{rows}</table>"

    asym_rows = ""
    for a in asymmetries[:10]:
        asym_rows += (f"<tr><td>{a['pair']}</td><td>{fmt_lat(a['a_to_b_p50'])}</td>"
                      f"<td>{fmt_lat(a['b_to_a_p50'])}</td><td>{fmt_lat(a['diff_us'])}</td>"
                      f"<td>{a['diff_pct']}%</td></tr>\n")

    type_rows = ""
    for label, s in type_stats.items():
        type_rows += (f"<tr><td>{label}</td><td>{s['count']}</td><td>{fmt_lat(s['min_p50'])}</td>"
                      f"<td>{fmt_lat(s['max_p50'])}</td><td>{fmt_lat(s['mean_p50'])}</td><td>{fmt_lat(s['spread'])}</td></tr>\n")

    pairs_measured = len(matrix)
    total_pairs = n * (n - 1)
    median_p50 = sorted(all_p50)[len(all_p50) // 2] if all_p50 else 0
    median_p99 = sorted(all_p99)[len(all_p99) // 2] if all_p99 else 0

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>AF_XDP Latency Matrix Report</title>
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 2em; background: #fafafa; color: #333; }}
h1 {{ color: #1a1a1a; border-bottom: 2px solid #ff9900; padding-bottom: 0.5em; }}
h2 {{ color: #232f3e; margin-top: 2em; }}
.summary {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 1em; margin: 1em 0; }}
.stat-card {{ background: white; border: 1px solid #e0e0e0; border-radius: 8px; padding: 1em; text-align: center; }}
.stat-card .value {{ font-size: 2em; font-weight: bold; color: #232f3e; }}
.stat-card .label {{ font-size: 0.85em; color: #666; margin-top: 0.3em; }}
table.matrix {{ border-collapse: collapse; margin: 1em 0; }}
table.matrix th, table.matrix td {{ border: 1px solid #ccc; padding: 8px 12px; text-align: center; min-width: 60px; }}
table.matrix th {{ background: #232f3e; color: white; font-size: 0.8em; }}
table.matrix td.diag {{ background: #f0f0f0; color: #999; }}
table.matrix td.na {{ background: #f5f5f5; color: #aaa; }}
table.matrix td.row-hdr {{ background: #232f3e; color: white; font-size: 0.8em; text-align: left; }}
table.matrix td strong {{ font-size: 0.9em; }}
table.data {{ border-collapse: collapse; margin: 1em 0; width: auto; }}
table.data th, table.data td {{ border: 1px solid #ddd; padding: 6px 12px; text-align: right; }}
table.data th {{ background: #232f3e; color: white; }}
table.data td:first-child {{ text-align: left; }}
.legend {{ display: flex; align-items: center; gap: 0.5em; margin: 0.5em 0; }}
.legend-bar {{ width: 200px; height: 20px; border-radius: 4px;
  background: linear-gradient(to right, #4CAF50, #FFEB3B, #FF0000); }}
.meta {{ font-size: 0.85em; color: #666; margin-top: 2em; padding-top: 1em; border-top: 1px solid #ddd; }}
</style></head><body>
<h1>🔥 AF_XDP Latency Matrix</h1>
<div class="summary">
  <div class="stat-card"><div class="value">{n}</div><div class="label">Nodes</div></div>
  <div class="stat-card"><div class="value">{pairs_measured}/{total_pairs}</div><div class="label">Pairs Measured</div></div>
  <div class="stat-card"><div class="value">{fmt_lat(median_p50)}</div><div class="label">Median p50 RTT</div></div>
  <div class="stat-card"><div class="value">{fmt_lat(median_p99)}</div><div class="label">Median p99 RTT</div></div>
  <div class="stat-card"><div class="value">{fmt_lat(vmax_p50 - vmin_p50)}</div><div class="label">p50 Spread (max−min)</div></div>
</div>
<h2>p50 Heatmap</h2>
<div class="legend"><span>Low</span><div class="legend-bar"></div><span>High</span></div>
{build_heatmap("p50_us", vmin_p50, vmax_p50)}
<h2>p99 Heatmap</h2>
<div class="legend"><span>Low</span><div class="legend-bar"></div><span>High</span></div>
{build_heatmap("p99_us", vmin_p99, vmax_p99)}
<h2>Asymmetry Analysis (top 10)</h2>
<p>Significant A→B vs B→A differences suggest physical path asymmetry.</p>
<table class="data">
<tr><th>Pair</th><th>A→B p50 (μs)</th><th>B→A p50 (μs)</th><th>Δ (μs)</th><th>Δ (%)</th></tr>
{asym_rows}
</table>
<h2>Per Instance-Type Pair Statistics</h2>
<table class="data">
<tr><th>Type Pair</th><th>Samples</th><th>Min p50</th><th>Max p50</th><th>Mean p50</th><th>Spread</th></tr>
{type_rows}
</table>
<div class="meta">
  <p>Generated by <code>generate_matrix_report.py</code> | Region: {fleet.get('region', '?')} |
     {fleet.get('messages', '?')} msgs @ {fleet.get('rate', '?')} msg/s |
     Timestamp: {fleet.get('timestamp', '?')}</p>
</div>
</body></html>"""

    output_path.write_text(html)
    print(f"  HTML heatmap: {output_path}")


# ─── Topology map visualization ───────────────────────────────────────────────


def _build_topology_fleet_json(node_names: List[str], matrix: dict, fleet: dict) -> str:
    """Build the JavaScript fleet data object for topology_map.html.

    Reads per-node metadata from fleet.json (az, region, vpc_id, cpg_name,
    enis, bw_gbps, etc). Falls back to global fleet values or instance-type
    lookup table for missing fields.
    """
    nodes_meta = {nd["name"]: nd for nd in fleet.get("nodes", [])}
    n = len(node_names)

    # Build per-node JS objects
    js_nodes = []
    for idx, name in enumerate(node_names):
        meta = nodes_meta.get(name, {})
        itype = meta.get("type", "unknown")
        hw = get_instance_metadata(itype)

        node_obj = {
            "index": idx,
            "name": name,
            "ec2_name": meta.get("ec2_name", meta.get("name", name)),
            "type": itype,
            "private_ip": meta.get("private_ip", meta.get("ip", f"10.0.0.{idx + 10}")),
            "public_ip": meta.get("public_ip", ""),
            # Per-node topology fields (fall back to global fleet values)
            "az": meta.get("az", fleet.get("az", "unknown")),
            "region": meta.get("region", fleet.get("region", "unknown")),
            "account": meta.get("account", fleet.get("account", "unknown")),
            "vpc_id": meta.get("vpc_id", fleet.get("vpc_id", "unknown")),
            "cpg_name": meta.get("cpg_name", fleet.get("cpg_name", "unknown")),
            "pg_type": meta.get("pg_type", "unknown"),
            # Hardware metadata (per-node overrides > lookup table)
            "enis": meta.get("enis", hw["enis"]),
            "bw_gbps": meta.get("bw_gbps", hw["bw_gbps"]),
            "pps_mpps": meta.get("pps_mpps", hw["pps_mpps"]),
            "nitro_gen": meta.get("nitro_gen", hw["nitro_gen"]),
            "vcpus": meta.get("vcpus", hw["vcpus"]),
            "mem_gb": meta.get("mem_gb", hw["mem_gb"]),
            "metal": meta.get("metal", hw["metal"]),
        }
        js_nodes.append(node_obj)

    # Build NxN matrix (indexed by position)
    js_matrix: List[List[Optional[Dict[str, Any]]]] = []
    for i, src in enumerate(node_names):
        row: List[Optional[Dict[str, Any]]] = []
        for j, dst in enumerate(node_names):
            if i == j:
                row.append(None)
            else:
                data = matrix.get((src, dst))
                if data:
                    row.append({
                        "p50": data.get("p50_us", 0),
                        "p90": data.get("p90_us", 0),
                        "p99": data.get("p99_us", 0),
                        "p999": data.get("p999_us", 0),
                        "max": data.get("max_us", 0),
                        "loss": data.get("loss_pct", 0),
                    })
                else:
                    row.append(None)
        js_matrix.append(row)

    fleet_obj = {
        "region": fleet.get("region", "unknown"),
        "nodes": js_nodes,
        "matrix": js_matrix,
    }

    return json.dumps(fleet_obj, indent=2)


def _build_contour_js() -> str:
    """Generate JavaScript for dynamic contour rendering based on per-node topology.

    Supports:
    - Intra-CPG: all nodes share cpg/az/vpc/region → one nested set of contours
    - Cross-AZ: nodes in different AZs → per-AZ contours, shared VPC/Region
    - Cross-Region: nodes in different regions → per-region contours
    """
    return """
// ─── Dynamic contour rendering ───────────────────────────────────────────────
(function renderContours() {
  const container = document.getElementById('container');
  const maxR = Math.max(...fleet.nodes.map(n => nodeRadius(n)));
  const vpcBoxes = [];

  // Group nodes by topology dimensions
  function groupBy(key) {
    const groups = {};
    fleet.nodes.forEach((node, i) => {
      const val = node[key] || 'unknown';
      if (!groups[val]) groups[val] = [];
      groups[val].push(i);
    });
    return groups;
  }

  const regionGroups = groupBy('region');
  const azGroups = groupBy('az');
  const vpcGroups = groupBy('vpc_id');
  const accountGroups = groupBy('account');

  const PAD_BASE = 12;
  // CPG is intentionally NOT drawn as a contour — it is shown as a per-node
  // badge instead, so dense multi-PG scenarios stay readable.
  // Strict nesting (inner → outer), by grouping granularity so boxes never
  // overlap: AZ ⊂ VPC ⊂ Region ⊂ Account. (A VPC spans its AZs, a Region holds
  // its VPCs, an Account holds its Regions.)
  const STEP = 18;
  const contourDefs = [
    { groups: azGroups,      cls: 'az',      prefix: 'AZ',      pad: PAD_BASE },
    { groups: vpcGroups,     cls: 'vpc',     prefix: 'VPC',     pad: PAD_BASE + STEP },
    { groups: regionGroups,  cls: 'region',  prefix: 'Region',  pad: PAD_BASE + STEP * 2 },
    { groups: accountGroups, cls: 'account', prefix: 'Account', pad: PAD_BASE + STEP * 3 },
  ];

  contourDefs.forEach(def => {
    const keys = Object.keys(def.groups);
    // Only render if the dimension has values
    if (keys.length === 0) return;

    keys.forEach(key => {
      if (key === 'unknown') return;
      const nodeIndices = def.groups[key];
      if (nodeIndices.length === 0) return;

      // Compute bounding box for this group's nodes
      const xs = nodeIndices.map(i => positions[i].x);
      const ys = nodeIndices.map(i => positions[i].y);
      const radii = nodeIndices.map(i => nodeRadius(fleet.nodes[i]));

      const left = Math.min(...xs.map((x, k) => x - radii[k])) - def.pad;
      const right = Math.max(...xs.map((x, k) => x + radii[k])) + def.pad;
      const top = Math.min(...ys.map((y, k) => y - radii[k])) - def.pad;
      const bottom = Math.max(...ys.map((y, k) => y + radii[k])) + def.pad;

      const el = document.createElement('div');
      el.className = 'contour ' + def.cls;
      el.style.left = left + 'px';
      el.style.top = top + 'px';
      el.style.width = (right - left) + 'px';
      el.style.height = (bottom - top) + 'px';

      // Label: show key name (abbreviate if only one group)
      const label = keys.length === 1 ? def.prefix + ': ' + key : key;
      el.innerHTML = '<span class="label">' + label + '</span>';
      container.appendChild(el);
      if (def.cls === 'vpc') {
        vpcBoxes.push({ cx: (left + right) / 2, cy: (top + bottom) / 2, hw: (right - left) / 2, hh: (bottom - top) / 2 });
      }
    });
  });

  // Cross-region VPC peering: connect the VPC boundaries with a labeled line.
  if (vpcBoxes.length >= 2) {
    const svg = document.getElementById('edges');
    function edgePoint(P, dx, dy) {
      const adx = Math.abs(dx) || 1e-6, ady = Math.abs(dy) || 1e-6;
      const t = Math.min(P.hw / adx, P.hh / ady);
      return { x: P.cx + dx * t, y: P.cy + dy * t };
    }
    for (let a = 0; a < vpcBoxes.length; a++)
      for (let b = a + 1; b < vpcBoxes.length; b++) {
        const A = vpcBoxes[a], B = vpcBoxes[b];
        const dx = B.cx - A.cx, dy = B.cy - A.cy;
        const p1 = edgePoint(A, dx, dy), p2 = edgePoint(B, -dx, -dy);
        const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
        line.setAttribute('x1', p1.x); line.setAttribute('y1', p1.y);
        line.setAttribute('x2', p2.x); line.setAttribute('y2', p2.y);
        line.setAttribute('class', 'peering-line');
        svg.appendChild(line);
        const lbl = document.createElement('div');
        lbl.className = 'peering-label';
        lbl.style.left = ((p1.x + p2.x) / 2) + 'px';
        lbl.style.top = ((p1.y + p2.y) / 2) + 'px';
        lbl.textContent = 'VPC Peering';
        lbl.style.display = 'none';
        container.appendChild(lbl);
        // Transparent wide hit-line: keeps the peering edge in the background
        // (visually) yet hoverable; hovering it reveals the label.
        const hit = document.createElementNS('http://www.w3.org/2000/svg', 'line');
        hit.setAttribute('x1', p1.x); hit.setAttribute('y1', p1.y);
        hit.setAttribute('x2', p2.x); hit.setAttribute('y2', p2.y);
        hit.setAttribute('class', 'peering-hit');
        hit.addEventListener('mouseenter', () => { lbl.style.display = ''; });
        hit.addEventListener('mouseleave', () => { lbl.style.display = 'none'; });
        svg.appendChild(hit);
      }
  }
})();
"""


def generate_topology_map(node_names: List[str], matrix: dict, fleet: dict,
                          output_path: Path) -> None:
    """Generate the interactive topology map HTML visualization.

    Self-contained HTML with all CSS/JS inline. Features:
    - SMACOF MDS proportional positioning
    - Node hover with full percentile breakdown (outbound + inbound)
    - Edge-label hover with directional tooltip + asymmetry
    - Edge highlighting on node hover
    - Dynamic contours based on per-node topology metadata
    - Instance-type legend with clickable links
    - Composite capability-based node sizing
    """
    fleet_json = _build_topology_fleet_json(node_names, matrix, fleet)
    contour_js = _build_contour_js()

    # Determine the primary region for links (use first node's region)
    primary_region = fleet.get("region", "us-east-1")
    if fleet.get("nodes"):
        primary_region = fleet["nodes"][0].get("region", fleet.get("region", "us-east-1"))

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>AF_XDP Latency Topology Map</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0d1117; color: #e6edf3; overflow: hidden; }}
.container {{ width: 100vw; height: 100vh; position: relative; }}
svg.edges {{ position: absolute; top: 0; left: 0; width: 100%; height: 100%; pointer-events: none; z-index: 1; }}
.edge-label {{
  position: absolute; z-index: 40;
  font-family: 'SF Mono', 'Fira Code', monospace;
  font-size: 13px; font-weight: 700;
  background: rgba(13, 17, 23, 0.94);
  padding: 2px 7px; border-radius: 4px;
  border: 1px solid rgba(255,255,255,0.1);
  white-space: nowrap; cursor: default;
  transform: translate(-50%, -50%);
  transition: border-color 0.15s, background 0.15s;
}}
.edge-label:hover {{ border-color: rgba(88,166,255,0.5); background: rgba(22, 27, 34, 0.98); }}
.node {{
  position: absolute; z-index: 20; border-radius: 50%;
  display: flex; flex-direction: column; align-items: center; justify-content: center;
  text-align: center; border: 2.5px solid rgba(255,255,255,0.25);
  box-shadow: 0 4px 24px rgba(0,0,0,0.6); cursor: pointer;
}}
.node .instance-type {{ font-size: 11px; font-weight: 700; color: #fff; white-space: nowrap; }}
.node .ec2-name {{ font-size: 9px; color: #79c0ff; white-space: nowrap; margin-top: 1px; }}
.node .ip {{ font-size: 9px; color: #b1bac4; font-family: 'SF Mono', monospace; }}
.node .ip-public {{ color: #79c0ff; font-weight: 700; margin-top: 1px; }}
.node .ip-private {{ color: #8b949e; }}
.panel-caret {{ display: inline-block; width: 12px; margin-right: 4px; font-size: 10px; color: #8b949e; }}
.stats h3, .vis-legend h3, .instance-legend h3 {{ cursor: move; user-select: none; }}
.node .specs {{ font-size: 8px; color: #8b949e; margin-top: 2px; white-space: nowrap; }}
.node .pg-badge {{
  position: absolute; top: -9px; left: 50%;
  background: #f0883e; color: #fff; font-size: 11px; font-weight: 700;
  min-width: 22px; height: 21px; padding: 0 9px; border-radius: 11px;
  display: flex; align-items: center; justify-content: center; border: 2px solid #0d1117;
  white-space: nowrap; letter-spacing: 0.2px;
}}
.node-tooltip {{
  position: absolute; z-index: 100;
  background: rgba(22, 27, 34, 0.97); border: 1px solid #30363d;
  border-radius: 8px; padding: 12px 14px; font-size: 11px;
  pointer-events: none; backdrop-filter: blur(8px);
  box-shadow: 0 8px 32px rgba(0,0,0,0.6); white-space: nowrap;
  opacity: 0; transition: opacity 0.15s; min-width: 260px;
}}
.node-tooltip.visible {{ opacity: 1; }}
.node-tooltip h4 {{ font-size: 12px; color: #58a6ff; margin-bottom: 6px; }}
.node-tooltip table {{ border-collapse: collapse; width: 100%; }}
.node-tooltip th {{ text-align: center; font-size: 10px; color: #8b949e; padding: 2px 6px; border-bottom: 1px solid #21262d; }}
.node-tooltip td {{ text-align: center; font-family: 'SF Mono', monospace; font-size: 11px; padding: 3px 6px; color: #e6edf3; }}
.node-tooltip td.peer-name {{ text-align: left; color: #79c0ff; font-family: inherit; }}
.node-tooltip td.highlight {{ color: #f0883e; font-weight: 600; }}
.node-tooltip tr.pg-group td {{ text-align: left; color: #8b949e; font-weight: 700; font-size: 10px; letter-spacing: 0.3px; padding: 6px 6px 2px; border-bottom: 1px solid #30363d; }}
.node-tooltip .direction {{ font-size: 9px; color: #6e7681; }}
.edge-tooltip {{
  position: absolute; z-index: 100;
  background: rgba(22, 27, 34, 0.97); border: 1px solid #30363d;
  border-radius: 8px; padding: 12px 14px; font-size: 11px;
  pointer-events: none; backdrop-filter: blur(8px);
  box-shadow: 0 8px 32px rgba(0,0,0,0.6); white-space: nowrap;
  opacity: 0; transition: opacity 0.15s; min-width: 240px;
}}
.edge-tooltip.visible {{ opacity: 1; }}
.edge-tooltip h4 {{ font-size: 12px; color: #58a6ff; margin-bottom: 8px; }}
.edge-tooltip .dir-block {{ margin-bottom: 8px; }}
.edge-tooltip .dir-label {{ font-size: 10px; color: #8b949e; margin-bottom: 3px; }}
.edge-tooltip .dir-values {{ display: grid; grid-template-columns: repeat(6, auto); gap: 2px 10px; }}
.edge-tooltip .metric-label {{ font-size: 9px; color: #6e7681; }}
.edge-tooltip .metric-val {{ font-family: 'SF Mono', monospace; font-size: 12px; color: #e6edf3; }}
.edge-tooltip .metric-val.highlight {{ color: #f0883e; font-weight: 700; }}
.edge-tooltip .asymmetry {{ font-size: 10px; color: #f0883e; margin-top: 4px; padding-top: 4px; border-top: 1px solid #21262d; }}
svg.edges line.edge-line {{ transition: opacity 0.15s, stroke-width 0.15s; }}
svg.edges line.edge-line.dimmed {{ opacity: 0.12 !important; }}
svg.edges line.edge-line.highlighted {{ opacity: 1 !important; filter: drop-shadow(0 0 6px currentColor) brightness(1.3); }}
.contour {{
  position: absolute; z-index: 0; border-radius: 24px; border: 1.5px dashed; pointer-events: none;
}}
.contour .label {{
  position: absolute; top: -10px; left: 16px;
  font-size: 10px; font-weight: 600; padding: 1px 8px; border-radius: 4px; white-space: nowrap;
}}
.contour.region {{ border-color: rgba(88,166,255,0.25); }}
.contour.region .label {{ background: rgba(88,166,255,0.15); color: #58a6ff; }}
.contour.az {{ border-color: rgba(163,113,247,0.25); }}
.contour.az .label {{ background: rgba(163,113,247,0.15); color: #a371f7; }}
.contour.vpc {{ border-color: rgba(57,211,83,0.2); }}
.contour.vpc .label {{ background: rgba(57,211,83,0.12); color: #39d353; }}
.contour.cpg {{ border-color: rgba(240,136,62,0.3); }}
.contour.cpg .label {{ background: rgba(240,136,62,0.15); color: #f0883e; }}
.contour.account {{ border-color: rgba(248,81,73,0.28); border-style: solid; }}
.contour.account .label {{ background: rgba(248,81,73,0.15); color: #f85149; }}
svg.edges line.peering-line {{ stroke: #58a6ff; stroke-width: 2.5; stroke-dasharray: 7 5; opacity: 0.75; }}
svg.edges line.peering-hit {{ stroke: transparent; stroke-width: 18; pointer-events: stroke; cursor: help; }}
.peering-label {{
  position: absolute; z-index: 1; transform: translate(-50%, -50%);
  font-size: 10px; font-weight: 700; color: #58a6ff;
  background: rgba(13,17,23,0.9); border: 1px solid rgba(88,166,255,0.45);
  border-radius: 4px; padding: 1px 7px; white-space: nowrap;
}}
.instance-legend {{
  position: fixed; bottom: 20px; left: 20px; z-index: 1000;
  background: rgba(22, 27, 34, 0.96); border: 1px solid #30363d;
  border-radius: 8px; padding: 16px 18px; font-size: 12px;
  backdrop-filter: blur(8px); max-width: 360px;
}}
.instance-legend h3 {{ font-size: 13px; margin-bottom: 10px; color: #58a6ff; }}
.instance-legend .type-row {{ display: flex; align-items: center; gap: 12px; margin: 6px 0; padding: 5px 0; border-bottom: 1px solid rgba(48,54,61,0.5); }}
.instance-legend .type-row:last-child {{ border-bottom: none; }}
.instance-legend .type-dot {{ border-radius: 50%; flex-shrink: 0; }}
.instance-legend .type-info {{ flex: 1; }}
.instance-legend .type-name {{ font-weight: 600; color: #e6edf3; font-size: 12px; }}
.instance-legend .type-specs {{ font-size: 11px; color: #8b949e; }}
.instance-legend a {{ color: #58a6ff; text-decoration: none; font-size: 11px; border: 1px solid rgba(88,166,255,0.3); border-radius: 3px; padding: 2px 6px; white-space: nowrap; }}
.instance-legend a:hover {{ background: rgba(88,166,255,0.15); }}
.vis-legend {{
  position: fixed; top: 20px; right: 20px; z-index: 1000;
  background: rgba(22, 27, 34, 0.96); border: 1px solid #30363d;
  border-radius: 8px; padding: 16px 18px; font-size: 12px; backdrop-filter: blur(8px);
}}
.vis-legend h3 {{ font-size: 13px; margin-bottom: 10px; color: #58a6ff; }}
.vis-legend .row {{ display: flex; align-items: center; gap: 10px; margin: 5px 0; }}
.vis-legend .swatch {{ width: 32px; height: 5px; border-radius: 2px; }}
.vis-legend .contour-samples {{ margin-top: 8px; display: flex; flex-wrap: wrap; gap: 8px; }}
.vis-legend .contour-samples span {{ border-radius: 4px; padding: 2px 8px; font-size: 11px; }}
.vis-legend .ux-hint {{ margin-top: 10px; padding-top: 8px; border-top: 1px solid #30363d; font-size: 10px; color: #8b949e; line-height: 1.6; max-width: 300px; }}
.vis-legend .ux-hint b {{ color: #e6edf3; }}
.stats {{
  position: fixed; top: 20px; left: 20px; z-index: 1000;
  background: rgba(22, 27, 34, 0.96); border: 1px solid #30363d;
  border-radius: 8px; padding: 18px; font-size: 13px; backdrop-filter: blur(8px); min-width: 220px;
}}
.stats h3 {{ font-size: 14px; margin-bottom: 10px; color: #58a6ff; }}
.stats .stat {{ display: flex; justify-content: space-between; margin: 3px 0; }}
.stats .stat .val {{ color: #f0883e; font-weight: 600; font-family: 'SF Mono', monospace; font-size: 13px; }}
.stats .stress {{ margin-top: 8px; padding-top: 8px; border-top: 1px solid #30363d; font-size: 11px; color: #8b949e; }}
.stats .stress .val {{ color: #39d353; }}
</style>
</head><body>
<div class="container" id="container">
  <svg class="edges" id="edges"></svg>
  <div class="stats" id="stats"></div>
</div>

<script>
// ─── Fleet data (dynamically generated from matrix results) ──────────────────
const fleet = {fleet_json};

const W = window.innerWidth;
const H = window.innerHeight;
const N = fleet.nodes.length;
const CX = W / 2;
const CY = H / 2;

// Latency unit formatter: >=500us -> ms (e.g. 0.5 ms), >=500ms -> s (e.g. 0.5 s).
function fmtLat(us) {{
  if (us === null || us === undefined || us === '') return '\\u2014';
  const v = +us;
  if (!isFinite(v)) return '\\u2014';
  const trim = (x) => (Math.round(x * 100) / 100).toString();
  if (v >= 500000) return trim(v / 1000000) + ' s';
  if (v >= 500) return trim(v / 1000) + ' ms';
  return Math.round(v) + ' \\u03bcs';
}}

// ─── Node sizing (composite capability score) ────────────────────────────────
function computeNodeScore(node) {{
  let s = 0;
  s += node.metal ? 40 : 0;
  s += (node.bw_gbps / 200) * 25;
  s += (node.pps_mpps / 30) * 20;
  s += (node.enis / 15) * 10;
  s += (node.nitro_gen / 6) * 15;
  s += (node.vcpus / 192) * 8;
  s += (node.mem_gb / 768) * 2;
  return s;
}}
function nodeRadius(node) {{ return 30 + computeNodeScore(node) * 0.6; }}

// ─── Node colors by instance family ──────────────────────────────────────────
const familyColors = {{
  'c7i':  {{ bg: '#1a2a40', border: '#58a6ff' }},
  'c6in': {{ bg: '#261a3d', border: '#a371f7' }},
  'c6i':  {{ bg: '#1a2e1a', border: '#39d353' }},
  'm7i':  {{ bg: '#2e2415', border: '#f0883e' }},
  'r7i':  {{ bg: '#2e1515', border: '#da3633' }},
  'm6i':  {{ bg: '#2e2a15', border: '#d29922' }},
  'r6i':  {{ bg: '#2e1a1a', border: '#f85149' }},
}};
function getNodeColors(type) {{
  const family = type.split('.')[0];
  return familyColors[family] || {{ bg: '#1a2a40', border: '#58a6ff' }};
}}

// ─── SMACOF MDS positioning ──────────────────────────────────────────────────
function computePositions() {{
  if (N < 2) return {{ positions: [{{x: CX, y: CY}}], stress: 0 }};

  const SCALE = 7.5;
  const D = Array.from({{length: N}}, () => Array(N).fill(0));
  for (let i = 0; i < N; i++)
    for (let j = 0; j < N; j++)
      if (i !== j) {{
        const ab = fleet.matrix[i][j]?.p50 || 35;
        const ba = fleet.matrix[j]?.[i]?.p50 || 35;
        D[i][j] = ((ab + ba) / 2) * SCALE;
      }}

  let pos = fleet.nodes.map((_, i) => ({{
    x: CX + 120 * Math.cos(2 * Math.PI * i / N - Math.PI/2),
    y: CY + 120 * Math.sin(2 * Math.PI * i / N - Math.PI/2),
  }}));

  for (let iter = 0; iter < 800; iter++) {{
    const newPos = pos.map(() => ({{x: 0, y: 0}}));
    for (let i = 0; i < N; i++) {{
      let wx = 0, wy = 0, wsum = 0;
      for (let j = 0; j < N; j++) {{
        if (i === j) continue;
        const dx = pos[i].x - pos[j].x;
        const dy = pos[i].y - pos[j].y;
        const dist = Math.sqrt(dx*dx + dy*dy) || 0.001;
        const target = D[i][j];
        const w = 1.0 / (target * target);
        wx += w * (pos[j].x + target * (dx / dist));
        wy += w * (pos[j].y + target * (dy / dist));
        wsum += w;
      }}
      newPos[i].x = wx / wsum;
      newPos[i].y = wy / wsum;
    }}
    pos = newPos;
  }}

  const PAD = 180;
  let minX = Infinity, maxX = -Infinity, minY = Infinity, maxY = -Infinity;
  for (const p of pos) {{ minX = Math.min(minX, p.x); maxX = Math.max(maxX, p.x); minY = Math.min(minY, p.y); maxY = Math.max(maxY, p.y); }}
  const rangeX = maxX - minX || 1;
  const rangeY = maxY - minY || 1;
  const scale = Math.min((W - 2*PAD) / rangeX, (H - 2*PAD) / rangeY);

  const result = pos.map(p => ({{
    x: CX + (p.x - (minX + rangeX/2)) * scale,
    y: CY + (p.y - (minY + rangeY/2)) * scale,
  }}));

  let stressNum = 0, stressDen = 0;
  for (let i = 0; i < N; i++)
    for (let j = i+1; j < N; j++) {{
      const dx = result[i].x - result[j].x;
      const dy = result[i].y - result[j].y;
      const dij = Math.sqrt(dx*dx + dy*dy);
      const target = D[i][j] * scale;
      stressNum += (dij - target) ** 2;
      stressDen += target ** 2;
    }}
  const stress = stressDen > 0 ? Math.sqrt(stressNum / stressDen) : 0;

  return {{ positions: result, stress }};
}}

const {{ positions, stress }} = computePositions();

// ─── Edge encoding: color + width = latency jitter (variance / std) ──────────
// Distance already encodes p50 (SMACOF), so edges carry a second dimension:
// dispersion. σ is estimated from percentiles assuming a normal tail
// (σ ≈ (p99 − p50) / 2.326), averaged over both directions of a pair.
function dirSigma(d) {{ return (d && d.p99 > d.p50) ? (d.p99 - d.p50) / 2.326 : 0; }}
function edgeSigma(ab, ba) {{
  const vals = [ab, ba].filter(Boolean).map(dirSigma);
  return vals.length ? vals.reduce((a, b) => a + b, 0) / vals.length : 0;
}}

let allP50 = [], allP99 = [], allSigma = [];
for (let i = 0; i < N; i++)
  for (let j = 0; j < N; j++)
    if (fleet.matrix[i] && fleet.matrix[i][j]) {{
      allP50.push(fleet.matrix[i][j].p50);
      allP99.push(fleet.matrix[i][j].p99);
    }}
for (let i = 0; i < N; i++)
  for (let j = i + 1; j < N; j++) {{
    const ab = fleet.matrix[i]?.[j], ba = fleet.matrix[j]?.[i];
    if (!ab && !ba) continue;
    allSigma.push(edgeSigma(ab, ba));
  }}
const minP50 = allP50.length ? Math.min(...allP50) : 0;
const maxP50 = allP50.length ? Math.max(...allP50) : 100;
const minP99 = allP99.length ? Math.min(...allP99) : 0;
const maxP99 = allP99.length ? Math.max(...allP99) : 100;
const minSigma = allSigma.length ? Math.min(...allSigma) : 0;
const maxSigma = allSigma.length ? Math.max(...allSigma) : 1;

// green (steady, low jitter) → red (high jitter)
function jitterColor(sigma) {{
  const t = (maxSigma === minSigma) ? 0.5 : (sigma - minSigma) / (maxSigma - minSigma);
  // attractive 3-stop palette: teal (#2dd4bf) -> amber (#fbbf24) -> rose (#fb7185)
  const stops = [[45,212,191],[251,191,36],[251,113,133]];
  const seg = t <= 0.5 ? 0 : 1;
  const lt = t <= 0.5 ? t * 2 : (t - 0.5) * 2;
  const a = stops[seg], b = stops[seg + 1];
  return 'rgb(' + Math.round(a[0]+(b[0]-a[0])*lt) + ',' + Math.round(a[1]+(b[1]-a[1])*lt) + ',' + Math.round(a[2]+(b[2]-a[2])*lt) + ')';
}}
const EDGE_WIDTH = 2.5; // static edge width — only COLOR encodes jitter σ
function edgeWidthFromSigma(sigma) {{
  return EDGE_WIDTH;
}}

// ─── Render contours (dynamic, per-node topology) ────────────────────────────
{contour_js}

// ─── Render edges ────────────────────────────────────────────────────────────
const edgeElements = [];
(function renderEdges() {{
  const svgEl = document.getElementById('edges');
  for (let i = 0; i < N; i++)
    for (let j = i + 1; j < N; j++) {{
      const ab = fleet.matrix[i]?.[j], ba = fleet.matrix[j]?.[i];
      if (!ab && !ba) continue;
      const avgP50 = Math.round(((ab?.p50||0) + (ba?.p50||0)) / 2);
      const sigma = edgeSigma(ab, ba);
      const line = document.createElementNS('http://www.w3.org/2000/svg', 'line');
      line.setAttribute('x1', positions[i].x); line.setAttribute('y1', positions[i].y);
      line.setAttribute('x2', positions[j].x); line.setAttribute('y2', positions[j].y);
      line.setAttribute('stroke', jitterColor(sigma));
      line.setAttribute('stroke-width', edgeWidthFromSigma(sigma));
      line.setAttribute('opacity', '0.55');
      line.classList.add('edge-line');
      svgEl.appendChild(line);
      edgeElements.push({{line, i, j, avgP50, ab, ba}});
    }}
}})();

// ─── Edge hover tooltip ──────────────────────────────────────────────────────
const edgeTooltip = document.createElement('div');
edgeTooltip.className = 'edge-tooltip';
document.getElementById('container').appendChild(edgeTooltip);

function showEdgeTooltip(i, j) {{
  const ab = fleet.matrix[i]?.[j], ba = fleet.matrix[j]?.[i];
  const nodeA = fleet.nodes[i], nodeB = fleet.nodes[j];
  const diff = ab && ba ? Math.abs(ab.p50 - ba.p50) : 0;
  const diffPct = ab && ba && Math.min(ab.p50, ba.p50) > 0 ? ((diff / Math.min(ab.p50, ba.p50)) * 100).toFixed(1) : '0';
  let html = '<h4>' + nodeA.ec2_name + ' \\u2194 ' + nodeB.ec2_name + '</h4>';
  if (ab) {{
    html += '<div class="dir-block"><div class="dir-label">\\u2192 ' + nodeA.ec2_name + ' \\u2192 ' + nodeB.ec2_name + '</div>';
    html += '<div class="dir-values"><span class="metric-label">p50</span><span class="metric-label">p90</span><span class="metric-label">p99</span><span class="metric-label">p99.9</span><span class="metric-label">max</span><span class="metric-label">loss</span>';
    html += '<span class="metric-val highlight">' + fmtLat(ab.p50) + '</span><span class="metric-val">' + (ab.p90 ? fmtLat(ab.p90) : '\\u2014') + '</span><span class="metric-val">' + fmtLat(ab.p99) + '</span><span class="metric-val">' + (ab.p999 ? fmtLat(ab.p999) : '\\u2014') + '</span><span class="metric-val">' + (ab.max ? fmtLat(ab.max) : '\\u2014') + '</span><span class="metric-val">' + (ab.loss !== undefined ? ab.loss+'%' : '\\u2014') + '</span></div></div>';
  }}
  if (ba) {{
    html += '<div class="dir-block"><div class="dir-label">\\u2190 ' + nodeB.ec2_name + ' \\u2192 ' + nodeA.ec2_name + '</div>';
    html += '<div class="dir-values"><span class="metric-label">p50</span><span class="metric-label">p90</span><span class="metric-label">p99</span><span class="metric-label">p99.9</span><span class="metric-label">max</span><span class="metric-label">loss</span>';
    html += '<span class="metric-val highlight">' + fmtLat(ba.p50) + '</span><span class="metric-val">' + (ba.p90 ? fmtLat(ba.p90) : '\\u2014') + '</span><span class="metric-val">' + fmtLat(ba.p99) + '</span><span class="metric-val">' + (ba.p999 ? fmtLat(ba.p999) : '\\u2014') + '</span><span class="metric-val">' + (ba.max ? fmtLat(ba.max) : '\\u2014') + '</span><span class="metric-val">' + (ba.loss !== undefined ? ba.loss+'%' : '\\u2014') + '</span></div></div>';
  }}
  if (diff > 0) html += '<div class="asymmetry">Asymmetry: \\u0394' + diff + '\\u03bcs (' + diffPct + '%)</div>';
  edgeTooltip.innerHTML = html;
  edgeTooltip.classList.add('visible');
}}
function positionEdgeTooltip(e) {{
  let tx = e.clientX + 16, ty = e.clientY - 10;
  const tw = edgeTooltip.offsetWidth || 260, th = edgeTooltip.offsetHeight || 150;
  if (tx + tw > W - 20) tx = e.clientX - tw - 16;
  if (ty + th > H - 20) ty = H - th - 20;
  if (ty < 10) ty = 10;
  edgeTooltip.style.left = tx + 'px'; edgeTooltip.style.top = ty + 'px';
}}
function hideEdgeTooltip() {{ edgeTooltip.classList.remove('visible'); }}

// ─── Edge labels (hidden until node hover / pinned by click) ─────────────────
const edgeLabelEls = [];
(function renderEdgeLabels() {{
  const container = document.getElementById('container');
  for (let i = 0; i < N; i++)
    for (let j = i + 1; j < N; j++) {{
      const ab = fleet.matrix[i]?.[j], ba = fleet.matrix[j]?.[i];
      if (!ab && !ba) continue;
      const avgP50 = Math.round(((ab?.p50||0) + (ba?.p50||0)) / 2);
      const x1 = positions[i].x, y1 = positions[i].y;
      const x2 = positions[j].x, y2 = positions[j].y;
      const mx = (x1+x2)/2, my = (y1+y2)/2;
      let ang = Math.atan2(y2 - y1, x2 - x1) * 180 / Math.PI;
      if (ang > 90) ang -= 180; else if (ang < -90) ang += 180; // keep text upright
      const el = document.createElement('div');
      el.className = 'edge-label';
      el.style.left = mx + 'px'; el.style.top = my + 'px';
      el.style.transform = 'translate(-50%, -50%) rotate(' + ang + 'deg)';
      el.style.color = jitterColor(edgeSigma(ab, ba));
      el.textContent = fmtLat(avgP50) + ' \\u00b1' + fmtLat(edgeSigma(ab, ba));
      el.style.display = 'none';
      const ci = i, cj = j;
      edgeLabelEls.push({{ el, i: ci, j: cj }});
      el.addEventListener('mouseenter', () => showEdgeTooltip(ci, cj));
      el.addEventListener('mousemove', positionEdgeTooltip);
      el.addEventListener('mouseleave', hideEdgeTooltip);
      container.appendChild(el);
    }}
}})();

// Edge-label visibility: shown only for the hovered node's edges, plus any
// nodes the user has pinned by clicking. Clicking a pinned node un-pins it.
const pinnedNodes = new Set();
function updateEdgeLabels(hover) {{
  for (const it of edgeLabelEls) {{
    const show = pinnedNodes.has(it.i) || pinnedNodes.has(it.j) || it.i === hover || it.j === hover;
    it.el.style.display = show ? '' : 'none';
  }}
}}

// ─── Render nodes + hover tooltips ───────────────────────────────────────────
const tooltip = document.createElement('div');
tooltip.className = 'node-tooltip';
document.getElementById('container').appendChild(tooltip);

// Per-node latency table: peers grouped by placement group, sorted by p50.
function buildPeerTable(i, inbound) {{
  const rows = [];
  for (let j = 0; j < N; j++) {{
    if (i === j) continue;
    const data = inbound ? (fleet.matrix[j] && fleet.matrix[j][i]) : (fleet.matrix[i] && fleet.matrix[i][j]);
    if (!data) continue;
    rows.push({{ peer: fleet.nodes[j], data: data }});
  }}
  const groups = {{}};
  rows.forEach(r => {{
    const pg = (r.peer.cpg_name && r.peer.cpg_name !== 'unknown') ? r.peer.cpg_name : 'no PG';
    (groups[pg] = groups[pg] || []).push(r);
  }});
  const keys = Object.keys(groups).sort((a, b) =>
    Math.min(...groups[a].map(r => r.data.p50)) - Math.min(...groups[b].map(r => r.data.p50)));
  let h = '<table><tr><th>Peer</th><th>p50</th><th>p90</th><th>p99</th><th>p99.9</th><th>max</th><th>loss</th></tr>';
  keys.forEach(pg => {{
    h += '<tr class="pg-group"><td colspan="7">' + pg + '</td></tr>';
    groups[pg].sort((a, b) => a.data.p50 - b.data.p50).forEach(r => {{
      const d = r.data;
      h += '<tr><td class="peer-name">' + r.peer.ec2_name + '</td><td class="highlight">' + fmtLat(d.p50) + '</td><td>' + (d.p90 ? fmtLat(d.p90) : '\\u2014') + '</td><td>' + fmtLat(d.p99) + '</td><td>' + (d.p999 ? fmtLat(d.p999) : '\\u2014') + '</td><td>' + (d.max ? fmtLat(d.max) : '\\u2014') + '</td><td>' + (d.loss !== undefined ? d.loss + '%' : '\\u2014') + '</td></tr>';
    }});
  }});
  h += '</table>';
  return h;
}}

fleet.nodes.forEach((node, i) => {{
  const container = document.getElementById('container');
  const r = nodeRadius(node);
  const colors = getNodeColors(node.type);
  const el = document.createElement('div');
  el.className = 'node';
  el.style.width = el.style.height = (r*2) + 'px';
  el.style.left = (positions[i].x - r) + 'px';
  el.style.top = (positions[i].y - r) + 'px';
  el.style.background = colors.bg;
  el.style.borderColor = colors.border;
  el.innerHTML = '<span class="instance-type">' + node.type + '</span>'
    + '<span class="ip ip-public">' + (node.public_ip || '\\u2014') + '</span>'
    + '<span class="ip ip-private">' + node.private_ip + '</span>'
    + ((node.cpg_name && node.cpg_name !== 'unknown') ? '<span class="pg-badge" title="Placement group">' + node.cpg_name + '</span>' : '');
  container.appendChild(el);

  el.addEventListener('mouseenter', (e) => {{
    updateEdgeLabels(i);
    edgeElements.forEach(({{line, i: ei, j: ej}}) => {{
      if (ei === i || ej === i) {{ line.classList.add('highlighted'); line.classList.remove('dimmed'); }}
      else {{ line.classList.add('dimmed'); line.classList.remove('highlighted'); }}
    }});
    let html = '<h4>' + node.ec2_name + ' \\u2192 peers</h4>' + buildPeerTable(i, false);
    html += '<div class="direction" style="margin-top:6px">\\u2190 Inbound (peers \\u2192 this node):</div>' + buildPeerTable(i, true);
    tooltip.innerHTML = html;
    tooltip.classList.add('visible');
  }});

  el.addEventListener('mousemove', (e) => {{
    let tx = e.clientX + 16, ty = e.clientY - 10;
    const tw = tooltip.offsetWidth || 280, th = tooltip.offsetHeight || 200;
    if (tx + tw > W - 20) tx = e.clientX - tw - 16;
    if (ty + th > H - 20) ty = H - th - 20;
    if (ty < 10) ty = 10;
    tooltip.style.left = tx + 'px'; tooltip.style.top = ty + 'px';
  }});

  el.addEventListener('mouseleave', () => {{
    tooltip.classList.remove('visible');
    edgeElements.forEach(({{line}}) => {{ line.classList.remove('highlighted', 'dimmed'); }});
    updateEdgeLabels(null);
  }});

  el.addEventListener('click', () => {{
    if (pinnedNodes.has(i)) pinnedNodes.delete(i); else pinnedNodes.add(i);
    updateEdgeLabels(i);
  }});
}});

// ─── Instance type legend ────────────────────────────────────────────────────
(function renderInstanceLegend() {{
  const container = document.getElementById('container');
  const seen = new Map();
  fleet.nodes.forEach(n => {{ if (!seen.has(n.type)) seen.set(n.type, n); }});
  const region = fleet.region || 'us-east-1';
  let rows = '';
  for (const [type, node] of seen) {{
    const colors = getNodeColors(type);
    const r = Math.round(nodeRadius(node) * 0.35);
    const family = type.split('.')[0];
    const specUrl = 'https://instances.vantage.sh/?selected=' + type + '&region=' + region;
    const awsUrl = 'https://aws.amazon.com/ec2/instance-types/' + family + '/';
    rows += '<div class="type-row">'
      + '<div class="type-dot" style="width:' + (r*2) + 'px;height:' + (r*2) + 'px;background:' + colors.bg + ';border:2px solid ' + colors.border + '"></div>'
      + '<div class="type-info"><div class="type-name">' + type + '</div>'
      + '<div class="type-specs">' + node.vcpus + 'vCPU \\u00b7 ' + node.mem_gb + 'GB \\u00b7 ' + node.bw_gbps + 'Gbps \\u00b7 ' + node.pps_mpps + 'Mpps \\u00b7 ' + node.enis + ' ENIs \\u00b7 Nitro ' + node.nitro_gen + '</div></div>'
      + '<a href="' + specUrl + '" target="_blank">specs\\u2197</a>'
      + '<a href="' + awsUrl + '" target="_blank">family\\u2197</a></div>';
  }}
  const el = document.createElement('div');
  el.className = 'instance-legend';
  el.innerHTML = '<h3>Instance Types</h3>' + rows;
  container.appendChild(el);
}})();

// ─── Visual encoding legend ──────────────────────────────────────────────────
(function renderVisLegend() {{
  const container = document.getElementById('container');
  const el = document.createElement('div');
  el.className = 'vis-legend';
  el.innerHTML = '<h3>Legend</h3>'
    + '<div class="row"><div class="swatch" style="background:linear-gradient(to right,#2dd4bf,#fbbf24,#fb7185)"></div><span>Edge color = jitter \\u03c3 (' + fmtLat(minSigma) + ' \\u2192 ' + fmtLat(maxSigma) + ')</span></div>'
    + '<div class="row"><span>Node size = f(BW, PPS, ENIs, Nitro, CPU, Mem, metal)</span></div>'
    + '<div class="row"><span>Distance \\u221d p50 latency (SMACOF stress: ' + (stress*100).toFixed(1) + '%)</span></div>'
    + '<div class="row"><span style="color:#79c0ff;font-weight:700">Public IP</span><span style="color:#8b949e">&nbsp;/&nbsp;</span><span style="color:#8b949e">Private IP</span><span>&nbsp;\\u2014 shown on each node</span></div>'
    + '<div class="contour-samples">'
    + '<span style="border:1.5px dashed rgba(57,211,83,0.3);color:#39d353">VPC</span>'
    + '<span style="border:1.5px dashed rgba(163,113,247,0.3);color:#a371f7">AZ</span>'
    + '<span style="border:1.5px dashed rgba(88,166,255,0.3);color:#58a6ff">Region</span>'
    + '<span style="border:1.5px solid rgba(248,81,73,0.5);color:#f85149">Account</span></div>'
    + '<div class="ux-hint"><b>Hover</b> a node \\u2014 reveal its edge latencies &amp; highlight. '
    + '<b>Click</b> a node \\u2014 pin/unpin those labels (click again to clear). '
    + '<b>Drag</b> a panel\\u2019s title to move it; <b>click</b> the title to fold.</div>';
  container.appendChild(el);
}})();

// ─── Stats panel ─────────────────────────────────────────────────────────────
const statsEl = document.getElementById('stats');
const medP50 = allP50.length ? [...allP50].sort((a,b)=>a-b)[Math.floor(allP50.length/2)] : 0;
const medP99 = allP99.length ? [...allP99].sort((a,b)=>a-b)[Math.floor(allP99.length/2)] : 0;

// Determine topology scope for stats display
const uniqueRegions = [...new Set(fleet.nodes.map(n => n.region))].filter(v => v !== 'unknown');
const uniqueAZs = [...new Set(fleet.nodes.map(n => n.az))].filter(v => v !== 'unknown');
const uniqueCPGs = [...new Set(fleet.nodes.map(n => n.cpg_name))].filter(v => v !== 'unknown');
const uniqueAccounts = [...new Set(fleet.nodes.map(n => n.account))].filter(v => v !== 'unknown');

let scopeHtml = '';
if (uniqueCPGs.length === 1) scopeHtml += '<div class="stat"><span>Placement Group</span><span class="val">' + uniqueCPGs[0] + '</span></div>';
else if (uniqueCPGs.length > 1) scopeHtml += '<div class="stat"><span>Placement Groups</span><span class="val">' + uniqueCPGs.length + '</span></div>';
if (uniqueAZs.length === 1) scopeHtml += '<div class="stat"><span>AZ</span><span class="val">' + uniqueAZs[0] + '</span></div>';
else if (uniqueAZs.length > 1) scopeHtml += '<div class="stat"><span>AZs</span><span class="val">' + uniqueAZs.join(', ') + '</span></div>';
if (uniqueRegions.length === 1) scopeHtml += '<div class="stat"><span>Region</span><span class="val">' + uniqueRegions[0] + '</span></div>';
else if (uniqueRegions.length > 1) scopeHtml += '<div class="stat"><span>Regions</span><span class="val">' + uniqueRegions.join(', ') + '</span></div>';
if (uniqueAccounts.length === 1) scopeHtml += '<div class="stat"><span>Account</span><span class="val">' + uniqueAccounts[0] + '</span></div>';
else if (uniqueAccounts.length > 1) scopeHtml += '<div class="stat"><span>Accounts</span><span class="val">' + uniqueAccounts.length + '</span></div>';

statsEl.innerHTML = '<h3>Summary</h3>'
  + '<div class="stat"><span>Nodes</span><span class="val">' + N + '</span></div>'
  + '<div class="stat"><span>Pairs</span><span class="val">' + allP50.length + '</span></div>'
  + '<div class="stat"><span>p50 range</span><span class="val">' + fmtLat(minP50) + '\\u2013' + fmtLat(maxP50) + '</span></div>'
  + '<div class="stat"><span>p99 range</span><span class="val">' + fmtLat(minP99) + '\\u2013' + fmtLat(maxP99) + '</span></div>'
  + '<div class="stat"><span>Jitter \\u03c3</span><span class="val">' + fmtLat(minSigma) + '\\u2013' + fmtLat(maxSigma) + '</span></div>'
  + '<div class="stat"><span>Median p50</span><span class="val">' + fmtLat(medP50) + '</span></div>'
  + '<div class="stat"><span>Spread</span><span class="val">' + fmtLat(maxP50-minP50) + '</span></div>'
  + '<div style="margin-top:8px;border-top:1px solid #30363d;padding-top:6px">' + scopeHtml + '</div>'
  + '<div class="stress">Layout fidelity (SMACOF): <span class="val">' + (100 - stress*100).toFixed(1) + '%</span> \\u2014 stress ' + (stress*100).toFixed(1) + '%</div>';

// ─── Foldable + draggable panels ─────────────────────────────────────────────
function enhancePanel(el) {{
  const h = el.querySelector('h3');
  if (!h) return;
  const body = document.createElement('div');
  body.className = 'panel-body';
  while (h.nextSibling) body.appendChild(h.nextSibling);
  el.appendChild(body);
  const caret = document.createElement('span');
  caret.className = 'panel-caret';
  caret.textContent = '\\u25be';
  h.insertBefore(caret, h.firstChild);
  let collapsed = false, dragging = false, moved = false, sx = 0, sy = 0, ox = 0, oy = 0;
  h.addEventListener('mousedown', (e) => {{
    dragging = true; moved = false; sx = e.clientX; sy = e.clientY;
    const r = el.getBoundingClientRect(); ox = r.left; oy = r.top;
    el.style.right = 'auto'; el.style.bottom = 'auto';
    el.style.left = ox + 'px'; el.style.top = oy + 'px';
    e.preventDefault();
  }});
  window.addEventListener('mousemove', (e) => {{
    if (!dragging) return;
    const dx = e.clientX - sx, dy = e.clientY - sy;
    if (Math.abs(dx) + Math.abs(dy) > 3) moved = true;
    el.style.left = (ox + dx) + 'px'; el.style.top = (oy + dy) + 'px';
  }});
  window.addEventListener('mouseup', () => {{ dragging = false; }});
  h.addEventListener('click', () => {{
    if (moved) {{ moved = false; return; }}
    collapsed = !collapsed;
    body.style.display = collapsed ? 'none' : '';
    caret.textContent = collapsed ? '\\u25b8' : '\\u25be';
  }});
}}
document.querySelectorAll('.stats, .vis-legend, .instance-legend').forEach(enhancePanel);
</script>
</body></html>"""

    output_path.write_text(html)
    print(f"  Topology map: {output_path}")


# ─── Main ─────────────────────────────────────────────────────────────────────


def main() -> None:
    """Entry point: parse results, generate all outputs."""
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <results_dir>")
        sys.exit(1)

    results_dir = Path(sys.argv[1])
    if not results_dir.is_dir():
        print(f"Error: {results_dir} is not a directory")
        sys.exit(1)

    fleet = load_fleet_metadata(results_dir)
    node_names, matrix = build_matrix(results_dir, fleet)

    if not matrix:
        print("No valid matrix results found.")
        sys.exit(1)

    print(f"\nMatrix: {len(node_names)} nodes, {len(matrix)} directed pairs measured")

    # Terminal output
    generate_terminal_table(node_names, matrix, "p50_us")
    print()
    generate_terminal_table(node_names, matrix, "p99_us")

    # Asymmetry
    asymmetries = compute_asymmetry(node_names, matrix)
    if asymmetries:
        print(f"\nTop asymmetries (p50):")
        for a in asymmetries[:5]:
            print(f"  {a['pair']}: {a['a_to_b_p50']}↔{a['b_to_a_p50']} (Δ{a['diff_us']}μs, {a['diff_pct']}%)")

    # Type stats
    type_stats = compute_type_stats(fleet, matrix)
    if type_stats:
        print(f"\nPer-type pair stats (p50):")
        for label, s in type_stats.items():
            print(f"  {label}: mean={s['mean_p50']}μs, spread={s['spread']}μs ({s['count']} samples)")

    # HTML heatmap report (original)
    html_path = results_dir / "matrix_report.html"
    generate_html_report(node_names, matrix, fleet, asymmetries, type_stats, html_path)

    # Topology map visualization (new)
    topo_path = results_dir / "topology_map.html"
    generate_topology_map(node_names, matrix, fleet, topo_path)

    # JSON export
    json_export = {
        "fleet": fleet,
        "node_names": node_names,
        "matrix": {f"{src}-{dst}": data for (src, dst), data in matrix.items()},
        "asymmetries": asymmetries,
        "type_stats": type_stats,
        "summary": {
            "nodes": len(node_names),
            "pairs_measured": len(matrix),
            "total_pairs": len(node_names) * (len(node_names) - 1),
            "p50_range": [min(d["p50_us"] for d in matrix.values() if d.get("p50_us")),
                          max(d["p50_us"] for d in matrix.values() if d.get("p50_us"))] if matrix else [0, 0],
            "p99_range": [min(d["p99_us"] for d in matrix.values() if d.get("p99_us")),
                          max(d["p99_us"] for d in matrix.values() if d.get("p99_us"))] if matrix else [0, 0],
        },
    }
    json_path = results_dir / "matrix_summary.json"
    json_path.write_text(json.dumps(json_export, indent=2))
    print(f"  JSON summary: {json_path}")


if __name__ == "__main__":
    main()
