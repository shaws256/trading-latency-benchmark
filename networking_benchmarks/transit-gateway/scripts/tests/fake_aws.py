#!/usr/bin/env python3
"""A stand-in for the `aws` CLI that answers the calls run_benchmark.sh makes.

It does not run the instance-side commands. It recognises each by a stable token
and answers from a scenario file, and it records every SSM command so a test can
assert on what the orchestrator asked the instances to do.

Environment:
  FAKE_AWS_SCENARIO  path to the scenario JSON
  FAKE_AWS_STATE     directory for the call log, counters and the fake S3 bucket
"""
import json
import os
import re
import shutil
import sys

STATE = os.environ["FAKE_AWS_STATE"]
SC = json.load(open(os.environ["FAKE_AWS_SCENARIO"]))
BUCKET_DIR = os.path.join(STATE, "bucket")


def arg(name, default=None):
    a = sys.argv
    return a[a.index(name) + 1] if name in a else default


def counter(key):
    path = os.path.join(STATE, "count." + re.sub(r"\W", "_", key))
    n = int(open(path).read()) + 1 if os.path.exists(path) else 1
    open(path, "w").write(str(n))
    return n


def out(text):
    sys.stdout.write(text if text.endswith("\n") or not text else text + "\n")


def per_instance(table, inst, default):
    return SC.get(table, {}).get(inst, default)


def respond(inst, cmd):
    """Output of one SSM command on one instance."""
    if "ENV=READY" in cmd:
        state = per_instance("native", inst, "ready")
        lines = ["ENV=" + per_instance("env", inst, "READY"), "NATIVE=" + state]
        if state == "ready":
            lines.append("NATIVE_BUILD=commit=%s repo=r ref=main" % per_instance("commit", inst, "abc123"))
        return "\n".join(lines)
    if "benchmark-native-build.log" in cmd:
        return SC.get("build_log", "make: *** [mcast_recv] Error 1")
    if "base64 -d" in cmd:
        return "UPLOADED"
    if "preflight.sh --max-offset-us" in cmd:
        return per_instance("preflight", inst, "phc_enable=1\nsystem_time_us=0.1\nPASS")
    if "preflight.sh --context" in cmd:
        return per_instance("context", inst, "kernel=6.1.0\ncpu_model=EPYC\ncpuidle_states=POLL:0us C1:1us C2:800us")
    if "preflight.sh --clock" in cmd:
        return "system_time_us=%s\nleap=Normal" % per_instance("post_clock", inst, "0.2")
    if "nohup" in cmd and "mcast_recv" in cmd:
        return "STARTED"
    if "RECV=READY" in cmd:
        seq = SC.get("recv_ready", {}).get(inst, ["RECV=READY"])
        n = counter("ready." + inst)
        return seq[min(n, len(seq)) - 1]
    if "RECV=DONE" in cmd:
        return per_instance("recv_done", inst, "RECV=DONE")
    if "cat /tmp/mcast_result.json 2>/dev/null; true" in cmd:
        res = per_instance("results", inst, "")
        if isinstance(res, list):  # one entry per collection, the last repeats
            n = counter("result." + inst)
            return res[min(n, len(res)) - 1]
        return res
    if "mcast_recv_detail.json" in cmd and "s3 cp" in cmd:
        if inst in SC.get("detail_missing", []):
            return "DETAIL=MISSING"
        m = re.search(r"s3://\S+/(\S+\.detail\.json)", cmd)
        os.makedirs(BUCKET_DIR, exist_ok=True)
        with open(os.path.join(BUCKET_DIR, os.path.basename(m.group(1))), "w") as f:
            json.dump(SC.get("details", {}).get(inst, {}), f)
        return "DETAIL=UPLOADED"
    if re.search(r"mcast_send(\.py)? \d", cmd):
        return SC.get("sender", "")
    if "sockperf under-load" in cmd or "cat /tmp/sockperf_result.txt" in cmd:
        return "sockperf: Summary: Latency is 80.0 usec\navg-rtt=80.0\npercentile 50.000 = 79.0\npercentile 99.000 = 120.0\n<MAX> observation = 300.0\n<MIN> observation = 70.0\n"
    return ""


def status_for(inst, cmd):
    if re.search(r"mcast_send(\.py)? \d", cmd) and SC.get("sender_status"):
        return SC["sender_status"]
    return "Success"


svc, op = sys.argv[1], sys.argv[2]
query = arg("--query", "")

if svc == "cloudformation":
    if "StackId" in query:
        out("arn:aws:cloudformation:us-east-1:123456789012:stack/S/guid")
    else:
        key = re.search(r"OutputKey=='(\w+)'", query).group(1)
        out(SC["outputs"][key])
elif svc == "ec2":
    inst = arg("--instance-ids")
    if "ImageId" in query:
        out("ami-0123456789abcdef0")
    elif "PrivateIpAddress" in query:
        out(SC["ips"].get(inst, "10.0.0.9"))
    elif "AvailabilityZone" in query:
        out(SC["az"].get(inst, "us-east-1a"))
    else:
        out(SC.get("instance_type", "m7i.large"))
elif svc == "ssm" and op == "describe-instance-information":
    out("Online")
elif svc == "ssm" and op == "send-command":
    n = counter("cmdid")
    cmd = json.loads(arg("--parameters"))["commands"][0]
    inst = arg("--instance-ids")
    with open(os.path.join(STATE, "calls.jsonl"), "a") as f:
        f.write(json.dumps({"id": n, "instance": inst, "command": cmd}) + "\n")
    with open(os.path.join(STATE, "cmd.%d.json" % n), "w") as f:
        json.dump({"instance": inst, "command": cmd}, f)
    out("cmd-%d" % n)
elif svc == "ssm" and op == "get-command-invocation":
    n = int(arg("--command-id").split("-")[1])
    rec = json.load(open(os.path.join(STATE, "cmd.%d.json" % n)))
    if query == "Status":
        out(status_for(rec["instance"], rec["command"]))
    else:
        out(respond(rec["instance"], rec["command"]))
elif svc == "s3" and op == "cp":
    dest = sys.argv[4]
    os.makedirs(dest, exist_ok=True)
    if os.path.isdir(BUCKET_DIR):
        for name in os.listdir(BUCKET_DIR):
            shutil.copy(os.path.join(BUCKET_DIR, name), dest)
else:
    sys.stderr.write("fake aws: unhandled %s\n" % sys.argv[1:])
    sys.exit(2)
