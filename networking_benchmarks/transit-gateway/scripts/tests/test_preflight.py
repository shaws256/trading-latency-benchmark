"""preflight.sh against stubbed /sys, chronyc, ethtool and ip."""
from __future__ import annotations

import os
import pathlib
import stat
import subprocess

import pytest

SCRIPT = pathlib.Path(__file__).resolve().parents[1] / "preflight.sh"

SOURCES_OK = """MS Name/IP address         Stratum Poll Reach LastRx Last sample
===============================================================================
#* PHC0                          0   0   377     1    +12ns[  +15ns] +/- 1823ns
^- 169.254.169.123               3   4   377    10   +120us[ +120us] +/-  900us
"""
TRACKING = """Reference ID    : 50484330 (PHC0)
Stratum         : 1
System time     : {sys} seconds {dir} of NTP time
Last offset     : +0.000000012 seconds
RMS offset      : 0.000000030 seconds
Leap status     : Normal
"""


@pytest.fixture
def lab(tmp_path):
    root = tmp_path / "root"
    (root / "sys/module/ena/parameters").mkdir(parents=True)
    (root / "sys/module/ena/parameters/phc_enable").write_text("1\n")
    (root / "sys/class/ptp/ptp0").mkdir(parents=True)
    (root / "sys/class/ptp/ptp0/clock_name").write_text("ena-ptp-00\n")
    fx = tmp_path / "fx"
    fx.mkdir()
    (fx / "sources").write_text(SOURCES_OK)
    (fx / "tracking").write_text(TRACKING.format(sys="0.000000062", dir="fast"))
    (fx / "ethtool").write_text("Capabilities:\n  hardware-transmit\n  hardware-receive\n  hardware-raw-clock\n")
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    log = tmp_path / "calls.log"

    def stub(name, body):
        p = bin_ / name
        p.write_text("#!/bin/bash\n" + body)
        p.chmod(p.stat().st_mode | stat.S_IEXEC)

    stub("chronyc", f'echo "chronyc $*" >> {log}\n'
         f'case "$1" in sources) cat {fx}/sources;; tracking) cat {fx}/tracking;; *) exit 0;; esac\n')
    stub("ethtool", f'cat {fx}/ethtool\n')
    stub("ip", 'echo "default via 10.0.0.1 dev ens5 proto dhcp src 10.0.0.5 metric 100"\n')
    env = dict(os.environ, SYSROOT=str(root), PATH=f"{bin_}:{os.environ['PATH']}", PREFLIGHT_SETTLE="0")
    return {"root": root, "fx": fx, "env": env, "log": log}


def run(lab, *args):
    return subprocess.run(["bash", str(SCRIPT), *args], env=lab["env"], capture_output=True, text=True)


def test_pass_and_forces_step_and_burst(lab):
    p = run(lab)
    assert p.returncode == 0, p.stdout
    assert "PASS" in p.stdout and "system_time_us=0.062" in p.stdout
    calls = lab["log"].read_text()
    assert "chronyc makestep" in calls and "chronyc burst" in calls
    assert calls.index("makestep") < calls.index("tracking")


def test_phc_disabled(lab):
    (lab["root"] / "sys/module/ena/parameters/phc_enable").write_text("0\n")
    p = run(lab)
    assert p.returncode == 1 and "FAIL:phc_not_enabled" in p.stdout and "PASS" not in p.stdout


def test_no_ena_ptp_device(lab):
    (lab["root"] / "sys/class/ptp/ptp0/clock_name").write_text("other\n")
    assert "FAIL:no_ena_ptp_device" in run(lab).stdout


def test_no_hw_receive(lab):
    (lab["fx"] / "ethtool").write_text("Capabilities:\n  software-receive\n")
    assert "FAIL:no_hw_receive" in run(lab).stdout


def test_two_refclocks(lab):
    (lab["fx"] / "sources").write_text(SOURCES_OK + "#- PHC1       0   0   377     1   +1ns[+1ns] +/- 1us\n")
    assert "FAIL:refclock_count_not_1" in run(lab).stdout


def test_phc_not_selected(lab):
    (lab["fx"] / "sources").write_text(SOURCES_OK.replace("#* PHC0", "#- PHC0").replace("^- 169", "^* 169"))
    assert "FAIL:phc_not_selected" in run(lab).stdout


def test_offset_over_threshold_either_direction(lab):
    (lab["fx"] / "tracking").write_text(TRACKING.format(sys="0.000025000", dir="slow"))
    p = run(lab)
    assert p.returncode == 1 and "clock_offset_exceeds_10us" in p.stdout and "system_time_us=-25.000" in p.stdout


def test_threshold_is_configurable(lab):
    (lab["fx"] / "tracking").write_text(TRACKING.format(sys="0.000025000", dir="fast"))
    assert run(lab, "--max-offset-us", "50").returncode == 0


def test_unreadable_tracking(lab):
    (lab["fx"] / "tracking").write_text("503 Not authorised\n")
    assert "FAIL:clock_unreadable" in run(lab).stdout


def test_clock_mode_is_read_only(lab):
    p = run(lab, "--clock")
    assert p.returncode == 0 and "system_time_us=0.062" in p.stdout and "leap=Normal" in p.stdout
    assert "makestep" not in lab["log"].read_text()


def test_multiple_failures_are_all_reported(lab):
    (lab["root"] / "sys/module/ena/parameters/phc_enable").write_text("0\n")
    (lab["fx"] / "ethtool").write_text("nothing\n")
    out = run(lab).stdout
    assert "FAIL:phc_not_enabled" in out and "FAIL:no_hw_receive" in out
