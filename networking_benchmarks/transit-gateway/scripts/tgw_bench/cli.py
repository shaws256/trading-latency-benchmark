"""Command-line parsing and validation for the run and prepare commands.

Values reach remote shell commands, so every one is checked here, before any AWS call.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from typing import List, Optional

VALID_TOOLS = ("mcast", "sockperf", "iperf3")  # also the order tools run in

TGW_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT_DIR = os.path.join(TGW_DIR, "scripts")

EPILOG = """\
Examples:
  scripts/run_benchmark.sh --stack-name MyBenchStack --tool mcast --save-report
  scripts/run_benchmark.sh --stack-name MyBenchStack --impl python
  scripts/run_benchmark.sh --stack-name MyBenchStack --sender-impl python --receiver-impl native

A native implementation needs the instances to have built it at launch; the run stops with the
build log if they could not. It never falls back to Python.

The first run against a stack (and any run after an instance reboots or a helper file changes)
prepares the instances automatically; scripts/prepare_stack.sh does that step on its own.
"""


class UsageError(Exception):
    pass


def _int(name: str, value: str) -> int:
    if not re.fullmatch(r"[0-9]+", value) or int(value) <= 0:
        raise UsageError(f"{name} must be a positive integer, got '{value}'")
    return int(value)


def _num(name: str, value: str) -> str:
    if not re.fullmatch(r"[0-9]+(\.[0-9]+)?", value):
        raise UsageError(f"{name} must be a number, got '{value}'")
    return value


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--stack-name", required=True, help="CloudFormation stack name")
    p.add_argument("--transport", default="ssm", help="ssm (SSM Run Command) or ssh (dev only: EC2 Instance "
                   "Connect key push + one reused SSH connection per instance; needs public IPs and port 22)")
    p.add_argument("--ssh-key", default=None, help="private key for --transport ssh (PATH.pub is pushed per run); "
                   "default: a throwaway key generated for the run")
    p.add_argument("--ssh-user", default="ec2-user", help="login user for --transport ssh")
    p.add_argument("--state-dir", default=os.path.join(TGW_DIR, ".bench-state"),
                   help="where the prepared-stack manifest is kept")


def _check_common(a: argparse.Namespace) -> None:
    if not re.fullmatch(r"[A-Za-z0-9-]+", a.stack_name):
        raise UsageError("--stack-name has unexpected characters")
    if a.transport not in ("ssm", "ssh"):
        raise UsageError(f"--transport must be ssm or ssh, got '{a.transport}'")
    if not re.fullmatch(r"[a-z_][a-z0-9_-]*", a.ssh_user):
        raise UsageError("--ssh-user has unexpected characters")


def run_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="run_benchmark.sh", description="Orchestrate a TGW multicast benchmark.",
                                epilog=EPILOG, allow_abbrev=False, formatter_class=argparse.RawDescriptionHelpFormatter)
    _add_common(p)
    p.add_argument("--group", default="239.1.1.1", help="multicast group address")
    p.add_argument("--port", default="5001", help="UDP port")
    p.add_argument("--rate", default="1000", help="send rate (mps for sockperf, pps for mcast, kbps for iperf3)")
    p.add_argument("--duration", default="60", help="benchmark duration in seconds")
    p.add_argument("--tool", action="append", default=[], help="benchmark tool(s), repeatable or comma separated: "
                   "mcast (TGW multicast one-way latency), sockperf (unicast RTT baseline), iperf3 (unicast UDP "
                   "throughput). Default: mcast,sockperf")
    p.add_argument("--impl", default="native", help="multicast sender and receiver: native (C++, built on the "
                   "instances) or python (scripts/mcast_*.py)")
    p.add_argument("--sender-impl", default="", help="override the sender only (native|python)")
    p.add_argument("--receiver-impl", default="", help="override the receiver only (native|python)")
    p.add_argument("--max-loss-pct", default="2", help="fail the run above this loss per subscriber")
    p.add_argument("--max-clock-offset-us", default="10", help="fail the run when chrony System time exceeds this")
    p.add_argument("--instance-type", default="", help="instance type for the report (default: auto-detect)")
    p.add_argument("--placement", default="single-az-cpg", help="placement strategy for the report")
    p.add_argument("--notes", default="", help="additional notes for the report")
    p.add_argument("--detail-dir", default="", help="keep the receivers' histogram files here")
    p.add_argument("--sender-cpu", default="", help="pin the multicast sender to this CPU (taskset)")
    p.add_argument("--runs-dir", default="", help="where --save-report writes (default: ../runs)")
    p.add_argument("--save-report", action="store_true", help="save a markdown report to runs/")
    p.add_argument("--force-prepare", action="store_true", help="prepare the instances even if the manifest is valid")
    return p


def parse_run_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    a = run_parser().parse_args(argv)
    try:
        _check_common(a)
        tools: List[str] = []
        for part in a.tool:
            for t in part.split(","):
                t = re.sub(r"\s", "", t)
                if not t:
                    continue
                if t not in VALID_TOOLS:
                    raise UsageError(f"unknown --tool '{t}'. Valid choices: {' '.join(VALID_TOOLS)}")
                tools.append(t)
        if not tools:
            tools = ["mcast", "sockperf"]
        a.run_tools = [t for t in VALID_TOOLS if t in tools]
        a.port = _int("--port", a.port)
        a.rate = _int("--rate", a.rate)
        a.duration = _int("--duration", a.duration)
        _num("--max-loss-pct", a.max_loss_pct)
        _num("--max-clock-offset-us", a.max_clock_offset_us)
        if not re.fullmatch(r"[0-9]{1,3}(\.[0-9]{1,3}){3}", a.group):
            raise UsageError(f"--group must be an IPv4 address, got '{a.group}'")
        if a.sender_cpu and not re.fullmatch(r"[0-9]+", a.sender_cpu):
            raise UsageError(f"--sender-cpu must be a CPU number, got '{a.sender_cpu}'")
        a.sender_impl = a.sender_impl or a.impl
        a.receiver_impl = a.receiver_impl or a.impl
        for impl in (a.sender_impl, a.receiver_impl):
            if impl not in ("native", "python"):
                raise UsageError(f"implementation must be native or python, got '{impl}'")
    except UsageError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise SystemExit(1)
    mcast = "mcast" in a.run_tools
    a.needs_native = mcast and "native" in (a.sender_impl, a.receiver_impl)
    a.runs_dir = a.runs_dir or os.path.join(TGW_DIR, "runs")
    return a


def parse_prepare_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="prepare_stack.sh", description="Provision and verify the benchmark "
                                "instances once, ahead of the first run. Safe to repeat.", allow_abbrev=False)
    _add_common(p)
    a = p.parse_args(argv)
    try:
        _check_common(a)
    except UsageError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        raise SystemExit(1)
    return a
