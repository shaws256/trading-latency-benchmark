"""run_benchmark.sh end to end against a fake `aws` CLI (see fake_aws.py).

These check the orchestrator's control flow and the raw stream it produces. They do not
exercise a real instance, the SSM service or the NIC.
"""
from __future__ import annotations

import json
import os
import pathlib
import stat
import subprocess
import sys

import pytest

HERE = pathlib.Path(__file__).resolve().parent
SCRIPTS_DIR = HERE.parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(SCRIPTS_DIR))

import tgw_results as tr  # noqa: E402
from test_tgw_results import _hist_json, recv  # noqa: E402

PUB = "i-pub0000000000001"
SUBS = ["i-sub0000000000001", "i-sub0000000000002"]
SENDER_OK = {"tool": "mcast_send", "impl": "cpp", "requested_pps": 1000, "duration_s": 1, "sent": 1000,
             "send_errors": 0, "achieved_pps": 1000.0, "achieved_pct": 100.0, "late_max_us": 40.0,
             "resyncs": 0, "interrupted": False}


class Lab:
    def __init__(self, tmp: pathlib.Path):
        self.tmp = tmp
        self.state = tmp / "state"
        self.state.mkdir()
        self.bin = tmp / "bin"
        self.bin.mkdir()
        self.runs = tmp / "runs"
        self.scenario = {
            "outputs": {"PublisherInstanceId": PUB, "SubscriberInstanceIds": ",".join(SUBS),
                        "S3BucketName": "bkt", "MulticastGroup": "239.1.1.1", "MulticastPort": "5001"},
            "az": {PUB: "us-east-1a", SUBS[0]: "us-east-1a", SUBS[1]: "us-east-1a"},
            "ips": {PUB: "10.0.0.5"},
            "sender": json.dumps(SENDER_OK),
            "results": {s: json.dumps(recv()) for s in SUBS},
            "details": {s: {"hist_hw_latency": _hist_json([150_000 + 1000 * i for i in range(200)])} for s in SUBS},
        }
        for name, body in (
            ("aws", f'#!/bin/sh\nexec "{sys.executable}" "{HERE / "fake_aws.py"}" "$@"\n'),
            ("sleep", "#!/bin/sh\nexit 0\n"),
        ):
            p = self.bin / name
            p.write_text(body)
            p.chmod(p.stat().st_mode | stat.S_IEXEC)
        (self.bin / "python3").symlink_to(sys.executable)

    def run(self, *args, tools="mcast"):
        sc = self.tmp / "scenario.json"
        sc.write_text(json.dumps(self.scenario))
        env = dict(os.environ, PATH=f"{self.bin}:{os.environ['PATH']}", FAKE_AWS_SCENARIO=str(sc),
                   FAKE_AWS_STATE=str(self.state), TMPDIR=str(self.tmp))
        cmd = ["bash", str(SCRIPTS_DIR / "run_benchmark.sh"), "--stack-name", "S", "--tool", tools,
               "--duration", "1", "--runs-dir", str(self.runs), *args]
        return subprocess.run(cmd, env=env, capture_output=True, text=True)


    def calls(self):
        f = self.state / "calls.jsonl"
        return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []

    def commands(self, needle):
        return [c for c in self.calls() if needle in c["command"]]


@pytest.fixture
def lab(tmp_path):
    return Lab(tmp_path)


def verdict(stdout):
    f, _ = tr.evaluate(stdout)
    return f


def test_happy_path_native(lab):
    p = lab.run()
    assert p.returncode == 0, p.stdout + (p.stdout + p.stderr)
    out = p.stdout
    assert "Impl:     sender=native receiver=native" in out
    assert "Native-Build: commit=abc123" in out
    assert out.count("--- MCAST ") == 2 and out.count("--- CLOCK ") == 3 * 2
    assert "RUN VALID" in out
    subs = tr.parse_subscribers(out)
    assert [s.az_relation for s in subs] == ["same-az", "same-az"]
    assert tr.parse_sender(out)["sent"] == 1000
    # native binaries, not Python, were started
    assert lab.commands("/opt/benchmark-native/bin/mcast_recv 239.1.1.1 5001")
    assert not lab.commands("mcast_recv.py")
    assert lab.commands("/opt/benchmark-native/bin/mcast_send 239.1.1.1 5001 1000 1 10.0.0.5")


def test_preflight_runs_before_any_receiver_starts(lab):
    lab.run()
    order = [c["command"] for c in lab.calls()]
    first_recv = next(i for i, c in enumerate(order) if "nohup" in c and "mcast_recv" in c)
    last_preflight = max(i for i, c in enumerate(order) if "preflight.sh --max-offset-us" in c)
    assert last_preflight < first_recv


def test_cross_az_is_labelled(lab):
    lab.scenario["az"][SUBS[1]] = "us-east-1b"
    p = lab.run()
    rel = {s.subscriber_id: s.az_relation for s in tr.parse_subscribers(p.stdout)}
    assert rel == {SUBS[0]: "same-az", SUBS[1]: "cross-az"}


def test_details_are_downloaded_for_pooling(lab, tmp_path):
    d = tmp_path / "det"
    p = lab.run("--detail-dir", str(d))
    assert p.returncode == 0, p.stdout
    assert sorted(x.name for x in d.iterdir()) == sorted(f"{s}.detail.json" for s in SUBS)


def test_native_build_failure_aborts_loudly_and_does_not_fall_back(lab):
    lab.scenario["native"] = {SUBS[0]: "failed"}
    p = lab.run()
    assert p.returncode != 0
    assert "no native build" in (p.stdout + p.stderr) and "--impl python" in (p.stdout + p.stderr)
    assert "make: *** [mcast_recv] Error 1" in (p.stdout + p.stderr)
    assert not lab.commands("nohup")


def test_python_impl_does_not_need_native(lab):
    lab.scenario["native"] = {PUB: "failed", SUBS[0]: "absent", SUBS[1]: "failed"}
    lab.scenario["sender"] = "Sent 1000 packets in 1s at 1000 pps"
    p = lab.run("--impl", "python")
    assert p.returncode == 0, p.stdout + (p.stdout + p.stderr)
    assert lab.commands("python3 /tmp/mcast_recv.py 239.1.1.1 5001")
    assert lab.commands("python3 /tmp/mcast_send.py 239.1.1.1 5001 1000 1 10.0.0.5")
    assert len(lab.commands("base64 -d")) >= 3 * 3  # preflight + two scripts, per instance


def test_mixed_implementations(lab):
    lab.scenario["sender"] = "Sent 1000 packets in 1s at 1000 pps"
    p = lab.run("--sender-impl", "python", "--receiver-impl", "native")
    assert p.returncode == 0, p.stdout + (p.stdout + p.stderr)
    assert "sender=python receiver=native" in p.stdout
    assert lab.commands("/opt/benchmark-native/bin/mcast_recv")
    assert lab.commands("python3 /tmp/mcast_send.py")


def test_commit_mismatch_between_instances_aborts(lab):
    lab.scenario["commit"] = {SUBS[1]: "def456"}
    p = lab.run()
    assert p.returncode != 0 and "different native commits" in (p.stdout + p.stderr)


def test_preflight_failure_stops_before_receivers(lab):
    lab.scenario["preflight"] = {SUBS[0]: "phc_enable=1\nsystem_time_us=55.0\nFAIL:clock_offset_exceeds_10us"}
    p = lab.run()
    assert p.returncode != 0 and "preflight failed" in (p.stdout + p.stderr)
    assert not lab.commands("nohup")


def test_receiver_gets_one_retry(lab):
    lab.scenario["recv_ready"] = {SUBS[0]: ["RECV=DEAD\nbind failed", "RECV=READY"]}
    p = lab.run()
    assert p.returncode == 0, p.stdout + (p.stdout + p.stderr)
    assert len([c for c in lab.commands("nohup") if c["instance"] == SUBS[0]]) == 2


def test_receiver_that_never_starts_aborts(lab):
    lab.scenario["recv_ready"] = {SUBS[0]: ["RECV=DEAD\nno hw timestamps"]}
    p = lab.run()
    assert p.returncode != 0 and "did not become ready" in (p.stdout + p.stderr)
    assert not lab.commands("mcast_send")


def test_silent_subscriber_makes_the_run_invalid(lab):
    lab.scenario["results"][SUBS[1]] = ""
    p = lab.run()
    assert p.returncode == 3, p.stdout + (p.stdout + p.stderr)
    assert "RUN INVALID" in p.stdout
    # the block is present, as an error, rather than the subscriber disappearing
    subs = {s.subscriber_id: s for s in tr.parse_subscribers(p.stdout)}
    assert not subs[SUBS[1]].ok and subs[SUBS[0]].ok


def test_whole_batch_is_retried_once_when_a_result_is_missing(lab):
    good = json.dumps(recv())
    lab.scenario["results"][SUBS[1]] = ["", good]
    p = lab.run()
    assert p.returncode == 0, p.stdout + p.stderr
    assert "rerunning the whole batch once" in p.stdout
    assert all(s.ok for s in tr.parse_subscribers(p.stdout))
    assert p.stdout.count("--- SENDER ") == 1          # only the final attempt is reported
    assert len(lab.commands("/opt/benchmark-native/bin/mcast_send")) == 2


def test_batch_retry_is_bounded_to_one(lab):
    lab.scenario["results"][SUBS[1]] = ""
    p = lab.run()
    assert p.returncode == 3
    assert len(lab.commands("/opt/benchmark-native/bin/mcast_send")) == 2


def test_context_is_recorded_per_instance(lab):
    p = lab.run()
    ctx = tr.parse_context(p.stdout)
    assert set(ctx) == {PUB, *SUBS}
    assert ctx[PUB]["ami"] == "ami-0123456789abcdef0" and ctx[PUB]["cpuidle_states"].endswith("C2:800us")


def test_loss_over_threshold_invalid_and_threshold_configurable(lab):
    lab.scenario["results"][SUBS[0]] = json.dumps(recv(total_received=900, packet_loss_count=100))
    assert lab.run().returncode == 3
    assert lab.run("--max-loss-pct", "15").returncode == 0


def test_sender_failure_is_invalid(lab):
    lab.scenario["sender_status"] = "Failed"
    p = lab.run()
    assert p.returncode == 3
    assert "sender-failed" in p.stdout


def test_post_run_clock_drift_is_caught(lab):
    lab.scenario["post_clock"] = {SUBS[0]: "88.0"}
    p = lab.run()
    assert p.returncode == 3 and "clock-offset-post" in p.stdout


def test_save_report_writes_report_with_pooled_percentiles(lab):
    p = lab.run("--save-report")
    assert p.returncode == 0, p.stdout + (p.stdout + p.stderr)
    md = next(lab.runs.glob("*.md")).read_text()
    assert "pooled over 400 samples" in md and "Run status: VALID" in md
    assert any(lab.runs.glob("*.details"))


def test_invalid_run_still_saves_a_report_and_exits_3(lab):
    lab.scenario["results"][SUBS[1]] = ""
    p = lab.run("--save-report")
    assert p.returncode == 3
    assert "Run status: INVALID" in next(lab.runs.glob("*.md")).read_text()


@pytest.mark.parametrize("args", [
    ["--port", "5001;id"], ["--rate", "0"], ["--duration", "x"], ["--group", "239.1.1.1;id"],
    ["--impl", "rust"], ["--max-loss-pct", "-1"], ["--stack-name", "a b"],
])
def test_bad_arguments_rejected_before_any_aws_call(lab, args):
    p = lab.run(*args)
    assert p.returncode != 0
    assert not lab.calls()


def test_sender_can_be_pinned_to_a_cpu(lab):
    p = lab.run("--sender-cpu", "1")
    assert p.returncode == 0, p.stdout + p.stderr
    assert lab.commands("taskset -c 1 /opt/benchmark-native/bin/mcast_send 239.1.1.1")
    assert not lab.run("--sender-cpu", "x").returncode == 0


def test_unicast_tools_still_produce_subscriber_blocks(lab):
    p = lab.run(tools="sockperf")
    assert p.returncode == 0, p.stdout + (p.stdout + p.stderr)
    assert p.stdout.count("--- SUBSCRIBER ") == 2 and "avg-rtt=80.0" in p.stdout
