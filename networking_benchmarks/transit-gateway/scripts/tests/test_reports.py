"""End-to-end tests for save_report.py and collect_results.py on multicast raw output."""
from __future__ import annotations

import importlib.util
import json
import pathlib
import subprocess
import sys

import pytest

SCRIPTS_DIR = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SCRIPTS_DIR))
sys.path.insert(0, str(pathlib.Path(__file__).parent))

from test_tgw_results import _hist_json, mcast_block, raw_run, recv  # noqa: E402


def _run(script, *args):
    return subprocess.run([sys.executable, str(SCRIPTS_DIR / script), *args],
                          capture_output=True, text=True)


@pytest.fixture
def valid_raw(tmp_path):
    f = tmp_path / "run.raw.txt"
    f.write_text(raw_run())
    return f


@pytest.fixture
def invalid_raw(tmp_path):
    f = tmp_path / "bad.raw.txt"
    f.write_text(raw_run(subs=("i-a", "i-b"), blocks=[mcast_block("i-a"), mcast_block("i-b", error="boom")]))
    return f


def test_save_report_valid_run(valid_raw, tmp_path):
    out = tmp_path / "out"
    p = _run("save_report.py", "-i", str(valid_raw), "--output-dir", str(out), "--filename", "r.md")
    assert p.returncode == 0, p.stderr
    md = (out / "r.md").read_text()
    assert "Run status: VALID" in md
    assert "Path: same-az" in md
    assert "upper bound" in md
    assert "mean of" not in md.lower().replace("mean of per-subscriber", "")


def test_save_report_surfaces_failed_subscriber_and_exits_3(invalid_raw, tmp_path):
    out = tmp_path / "out"
    p = _run("save_report.py", "-i", str(invalid_raw), "--output-dir", str(out), "--filename", "r.md")
    assert p.returncode == 3
    md = (out / "r.md").read_text()
    assert "Run status: INVALID" in md and "FAILED" in md and "boom" in md
    # the failed subscriber is counted as total loss
    assert "1000/2000" in md


def test_save_report_pooled_with_details(valid_raw, tmp_path):
    d = tmp_path / "det"
    d.mkdir()
    for sid, v in (("i-a", 150_000), ("i-b", 160_000)):
        (d / f"{sid}.detail.json").write_text(json.dumps({"hist_hw_latency": _hist_json([v] * 50)}))
    out = tmp_path / "out"
    p = _run("save_report.py", "-i", str(valid_raw), "--detail-dir", str(d), "--output-dir", str(out), "--filename", "r.md")
    assert p.returncode == 0, p.stderr
    md = (out / "r.md").read_text()
    assert "pooled over 100 samples" in md


def test_save_report_without_details_says_so(valid_raw, tmp_path):
    out = tmp_path / "out"
    _run("save_report.py", "-i", str(valid_raw), "--output-dir", str(out), "--filename", "r.md")
    md = (out / "r.md").read_text()
    assert "Pooled percentiles | not available" in md and "Worst-subscriber P99" in md


def test_collect_accepts_mcast_and_refuses_invalid(valid_raw, invalid_raw, tmp_path):
    cw = pytest.importorskip("boto3")  # collect_results imports boto3
    del cw
    spec = importlib.util.spec_from_file_location("collect_results", SCRIPTS_DIR / "collect_results.py")
    cr = importlib.util.module_from_spec(spec)
    sys.modules["collect_results"] = cr
    spec.loader.exec_module(cr)

    published = []
    cr.publish_mcast_to_cloudwatch = lambda rep, ns, region=None: published.append(rep)
    uploaded = []
    cr.upload_to_s3 = lambda rep, b, k, region=None: uploaded.append((b, k)) or True

    args = cr.parse_args(["--input", str(valid_raw), "--s3-bucket", "b"])
    assert args.tool == "mcast"
    assert cr.run_mcast(args, valid_raw.read_text()) == 0
    assert published and published[0]["valid"] is True and uploaded == [("b", "reports/r1.json")]

    published.clear()
    uploaded.clear()
    args = cr.parse_args(["--input", str(invalid_raw), "--s3-bucket", "b"])
    assert cr.run_mcast(args, invalid_raw.read_text()) == 3
    assert not published and not uploaded

    args = cr.parse_args(["--input", str(invalid_raw), "--s3-bucket", "b", "--allow-partial"])
    assert cr.run_mcast(args, invalid_raw.read_text()) == 0
    assert published[0]["valid"] is False
    assert any(f["code"] == "subscriber-failed" for f in published[0]["findings"])
