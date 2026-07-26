#!/usr/bin/env python3
"""
generate_comparison_report.py - Parse benchmark results and produce a comparison table.

Reads all *_client_*.txt and *.json files in the results directory,
extracts percentile data, and generates:
  - A terminal-formatted comparison table
  - An HTML report with pure CSS bar charts
  - A consolidated JSON summary

Usage: python3 generate_comparison_report.py <results_dir>
"""

import json
import os
import re
import sys
from pathlib import Path


def parse_old_client_txt(filepath):
    """Parse market_data_provider_client output for RTT percentiles."""
    data = {"client": "market_data_provider", "file": filepath.name}
    text = filepath.read_text()

    # Extract rate from filename
    m = re.search(r"(\d+)mps", filepath.name)
    if m:
        data["rate_mps"] = int(m.group(1))

    patterns = {
        "messages_sent": r"Total messages sent:\s*(\d+)",
        "messages_received": r"Total messages received:\s*(\d+)",
        "loss_pct": r"Packet loss:.*\(([\d.]+)%\)",
        "min_us": r"Min RTT:\s*(\d+)",
        "avg_us": r"Avg RTT:\s*([\d.]+)",
        "max_us": r"Max RTT:\s*(\d+)",
        "p50_us": r"50%:\s*(\d+)",
        "p90_us": r"90%:\s*(\d+)",
        "p95_us": r"95%:\s*(\d+)",
        "p99_us": r"99%:\s*(\d+)",
        "p999_us": r"99\.9%:\s*(\d+)",
    }

    for key, pattern in patterns.items():
        m = re.search(pattern, text)
        if m:
            val = m.group(1)
            data[key] = float(val) if "." in val else int(val)

    return data


def parse_new_client_json(filepath):
    """Parse latency_client JSON output."""
    try:
        raw = json.loads(filepath.read_text())
    except (json.JSONDecodeError, FileNotFoundError):
        return None

    data = {
        "client": "latency_client",
        "file": filepath.name,
        "rate_mps": raw.get("rate_mps", 0),
        "messages_sent": raw.get("messages", 0),
        "lost": raw.get("lost", 0),
        "loss_pct": raw.get("loss_pct", 0),
        "timestamp_rx": raw.get("timestamp_rx", "unknown"),
        "timestamp_tx": raw.get("timestamp_tx", "unknown"),
    }

    svc = raw.get("service_rtt_us", {})
    if svc:
        data["min_us"] = svc.get("min", 0)
        data["avg_us"] = svc.get("mean", 0)
        data["p50_us"] = svc.get("p50", 0)
        data["p90_us"] = svc.get("p90", 0)
        data["p95_us"] = svc.get("p95", 0)
        data["p99_us"] = svc.get("p99", 0)
        data["p999_us"] = svc.get("p999", 0)
        data["max_us"] = svc.get("max", 0)

    return data


def generate_terminal_table(results):
    """Print a comparison table to stdout."""
    print("\n" + "=" * 90)
    print(f"{'Client':<22} {'Rate':>8} {'Loss%':>6} {'Min':>6} {'p50':>6} "
          f"{'p90':>6} {'p95':>6} {'p99':>6} {'p999':>7} {'Max':>7}")
    print("-" * 90)

    for r in sorted(results, key=lambda x: (x.get("rate_mps", 0), x.get("client", ""))):
        print(f"{r.get('client','?'):<22} "
              f"{r.get('rate_mps',0):>7}  "
              f"{r.get('loss_pct',0):>5.2f} "
              f"{r.get('min_us',0):>6} "
              f"{r.get('p50_us',0):>6} "
              f"{r.get('p90_us',0):>6} "
              f"{r.get('p95_us',0):>6} "
              f"{r.get('p99_us',0):>6} "
              f"{r.get('p999_us',0):>7} "
              f"{r.get('max_us',0):>7}")

    print("=" * 90)
    print("All values in microseconds (us). Lower is better.")


def generate_html_report(results, output_path):
    """Generate a static HTML comparison report with CSS bar charts."""
    max_p99 = max((r.get("p99_us", 1) for r in results), default=1)

    rows_html = ""
    for r in sorted(results, key=lambda x: (x.get("rate_mps", 0), x.get("client", ""))):
        p50 = r.get("p50_us", 0)
        p99 = r.get("p99_us", 0)
        bar_width = int(100 * p99 / max_p99) if max_p99 > 0 else 0
        color = "#4CAF50" if "latency" in r.get("client", "") else "#2196F3"

        rows_html += f"""<tr>
  <td>{r.get('client','')}</td>
  <td>{r.get('rate_mps',0):,}</td>
  <td>{r.get('loss_pct',0):.2f}%</td>
  <td>{r.get('min_us',0)}</td>
  <td><strong>{p50}</strong></td>
  <td>{r.get('p90_us',0)}</td>
  <td>{r.get('p99_us',0)}</td>
  <td>{r.get('p999_us',0)}</td>
  <td>{r.get('max_us',0)}</td>
  <td><div style="background:{color};height:18px;width:{bar_width}%"></div></td>
</tr>\n"""

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>AF_XDP Latency Comparison</title>
<style>
body {{ font-family: -apple-system, sans-serif; margin: 2em; }}
table {{ border-collapse: collapse; width: 100%; }}
th, td {{ border: 1px solid #ddd; padding: 6px 10px; text-align: right; }}
th {{ background: #333; color: white; }}
tr:nth-child(even) {{ background: #f9f9f9; }}
td:first-child, td:nth-child(2) {{ text-align: left; }}
</style></head><body>
<h1>AF_XDP Latency Comparison Report</h1>
<p>Generated: {results[0].get('file','') if results else ''}</p>
<table>
<tr><th>Client</th><th>Rate (msg/s)</th><th>Loss</th><th>Min (us)</th><th>p50</th>
<th>p90</th><th>p99</th><th>p99.9</th><th>Max</th><th>p99 bar</th></tr>
{rows_html}
</table>
<p><span style="color:#4CAF50">Green</span> = latency_client (new),
<span style="color:#2196F3">Blue</span> = market_data_provider_client (original)</p>
</body></html>"""

    output_path.write_text(html)
    print(f"HTML report: {output_path}")


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} <results_dir>")
        sys.exit(1)

    results_dir = Path(sys.argv[1])
    if not results_dir.is_dir():
        print(f"Error: {results_dir} is not a directory")
        sys.exit(1)

    results = []

    # Parse old client text outputs
    for txt in results_dir.glob("old_client_*.txt"):
        data = parse_old_client_txt(txt)
        if data.get("p50_us"):
            results.append(data)

    # Parse new client JSON outputs
    for jf in results_dir.glob("latency_client_*.json"):
        data = parse_new_client_json(jf)
        if data and data.get("p50_us"):
            results.append(data)

    if not results:
        print("No results found to compare.")
        sys.exit(1)

    # Terminal table
    generate_terminal_table(results)

    # HTML report
    html_path = results_dir / "comparison_report.html"
    generate_html_report(results, html_path)

    # Consolidated JSON
    json_path = results_dir / "comparison_summary.json"
    json_path.write_text(json.dumps(results, indent=2))
    print(f"JSON summary: {json_path}")


if __name__ == "__main__":
    main()
