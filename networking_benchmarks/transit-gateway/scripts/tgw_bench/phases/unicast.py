"""Phases B and C: unicast baselines that do not cross the TGW (sockperf RTT, iperf3 throughput)."""
from __future__ import annotations

from ..context import Ctx
from ..model import Stack
from ..util import sleep
from .mcast import RECEIVER_SETTLE_SECS


def _block(ctx: Ctx, sub_id: str, text: str) -> None:
    ctx.console.out(f"--- SUBSCRIBER {sub_id} ---")
    ctx.console.out(text)
    ctx.console.out(f"--- END {sub_id} ---")
    ctx.console.out("")


def run_sockperf(ctx: Ctx, stack: Stack) -> None:
    cfg, c, tp = ctx.cfg, ctx.console, ctx.transport
    timeout = cfg.duration + 30
    c.out(">>> Phase B: Unicast latency measurement (sockperf under-load)...")
    for s in stack.subscribers:
        tp.run(s.id, 60, f"pkill -f 'sockperf server' 2>/dev/null || true; pkill -f '[m]cast_recv' 2>/dev/null || true; "
                         f"fuser -k {cfg.port}/udp 2>/dev/null || true; sleep 2; "
                         f"nohup sockperf server --ip {s.private_ip} --port {cfg.port} > /tmp/sockperf_server.txt 2>&1 < /dev/null &")
        c.out(f"  {s.id} ({s.private_ip}) - sockperf server started")
    sleep(RECEIVER_SETTLE_SECS)
    for s in stack.subscribers:
        r = tp.run(s.id, 30, f"cat /tmp/sockperf_server.txt 2>/dev/null; ss -ulnp | grep {cfg.port}")
        c.out(f"  {s.id} server status: {r.out}")
    for s in stack.subscribers:
        c.out(f"  Running sockperf under-load -> {s.id} ({s.private_ip})...")
        r = tp.run(stack.publisher.id, timeout,
                   f"sockperf under-load --ip {s.private_ip} --port {cfg.port} --mps {cfg.rate} "
                   f"--time {cfg.duration} --reply-every 1 --full-rtt 2>&1 | tee /tmp/sockperf_result.txt")
        if r.ok:
            text = tp.run(stack.publisher.id, 60, "cat /tmp/sockperf_result.txt").out
        else:
            text = f"ERROR: sockperf did not complete (SSM status: {r.status or 'unknown'})"
        _block(ctx, s.id, text)
        tp.run(s.id, 30, "pkill -f 'sockperf server' || true")


def run_iperf3(ctx: Ctx, stack: Stack) -> None:
    cfg, c, tp = ctx.cfg, ctx.console, ctx.transport
    timeout = cfg.duration + 30
    c.out(">>> Phase C: Unicast UDP throughput (iperf3)...")
    for s in stack.subscribers:
        tp.run(s.id, 60, f"pkill -f 'iperf3 -s' 2>/dev/null || true; fuser -k {cfg.port}/udp 2>/dev/null || true; "
                         f"sleep 2; nohup iperf3 -s -p {cfg.port} > /tmp/benchmark_result.txt 2>&1 < /dev/null &")
        c.out(f"  {s.id} ({s.private_ip}) - iperf3 server started")
    sleep(RECEIVER_SETTLE_SECS)
    for s in stack.subscribers:
        c.out(f"  Running iperf3 -> {s.id} ({s.private_ip})...")
        tp.run(stack.publisher.id, timeout,
               f"iperf3 -c {s.private_ip} -u -p {cfg.port} -b {cfg.rate}k -t {cfg.duration} 2>&1 | tee /tmp/iperf3_result.txt")
        r = tp.run(s.id, 60, "pkill -f 'iperf3 -s' || true; sleep 1; cat /tmp/benchmark_result.txt")
        text = r.out if r.ok else f"ERROR: iperf3 did not complete (SSM status: {r.status or 'unknown'})"
        _block(ctx, s.id, text)
