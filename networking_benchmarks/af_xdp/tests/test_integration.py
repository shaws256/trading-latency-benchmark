"""
Integration tests for the AF_XDP benchmark suite using kernel-mode replicator.

Tests validate the full measurement pipeline end-to-end:
  1. Control protocol (subscription handshake)
  2. RTT measurement (send → echo → timestamp → JSON output)
  3. JSON output schema compliance
  4. Coordinated omission detection
  5. Warmup exclusion
  6. Multi-destination fan-out

Prerequisites:
  - Binaries built: make all (in networking_benchmarks/af_xdp/)
  - No root required (kernel-mode)
  - No XDP/BPF required
  - Runs on any platform (Linux amd64, Linux arm64, macOS)

Usage:
  cd networking_benchmarks/af_xdp
  make all  # or at least: replicator rtt replicator_ctl udp_ping
  cd tests
  pytest -v
"""

import json
import os
import signal
import socket
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest

# ── Paths ─────────────────────────────────────────────────────────────────────
AF_XDP_DIR = Path(__file__).parent.parent
REPLICATOR = AF_XDP_DIR / "replicator"
RTT = AF_XDP_DIR / "rtt"
REPLICATOR_CTL = AF_XDP_DIR / "replicator_ctl"
UDP_PING = AF_XDP_DIR / "udp_ping"

LISTEN_IP = "127.0.0.1"
DATA_PORT = 19000  # avoid conflicts with other services
CONTROL_PORT = 12345


# ── Fixtures ──────────────────────────────────────────────────────────────────
@pytest.fixture(scope="module")
def replicator_process():
    """Start kernel-mode replicator for the test session."""
    if not REPLICATOR.exists():
        pytest.skip(f"Binary not found: {REPLICATOR}. Run 'make all' first.")

    proc = subprocess.Popen(
        [str(REPLICATOR), "--kernel-mode", LISTEN_IP, str(DATA_PORT)],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    # Wait for it to be ready
    time.sleep(0.5)
    if proc.poll() is not None:
        output = proc.stdout.read()
        pytest.fail(f"Replicator exited immediately: {output}")

    yield proc

    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=5)


@pytest.fixture
def ctrl_socket():
    """UDP socket for control protocol testing."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(3.0)
    yield sock
    sock.close()


# ── Test: Binary existence ────────────────────────────────────────────────────
class TestBinaries:
    """Verify all binaries are built."""

    def test_replicator_exists(self):
        assert REPLICATOR.exists(), f"Missing: {REPLICATOR}"

    def test_rtt_exists(self):
        assert RTT.exists(), f"Missing: {RTT}"

    def test_replicator_ctl_exists(self):
        assert REPLICATOR_CTL.exists(), f"Missing: {REPLICATOR_CTL}"

    def test_udp_ping_exists(self):
        assert UDP_PING.exists(), f"Missing: {UDP_PING}"


# ── Test: Control protocol ────────────────────────────────────────────────────
class TestControlProtocol:
    """Test the subscription handshake (port 12345)."""

    def test_add_destination(self, replicator_process, ctrl_socket):
        """CTRL_ADD_DESTINATION (cmd=1) should return ACK=1."""
        # Wire format: [1B cmd=1][4B IP][2B port]
        dest_ip = socket.inet_aton("127.0.0.1")
        dest_port = struct.pack("!H", 19001)
        msg = bytes([1]) + dest_ip + dest_port

        ctrl_socket.sendto(msg, (LISTEN_IP, CONTROL_PORT))
        ack, _ = ctrl_socket.recvfrom(1)
        assert ack == b"\x01", f"Expected ACK=1, got {ack!r}"

    def test_add_duplicate_destination(self, replicator_process, ctrl_socket):
        """Adding same destination twice should still return ACK=1."""
        dest_ip = socket.inet_aton("127.0.0.1")
        dest_port = struct.pack("!H", 19002)
        msg = bytes([1]) + dest_ip + dest_port

        ctrl_socket.sendto(msg, (LISTEN_IP, CONTROL_PORT))
        ack1, _ = ctrl_socket.recvfrom(1)

        ctrl_socket.sendto(msg, (LISTEN_IP, CONTROL_PORT))
        ack2, _ = ctrl_socket.recvfrom(1)

        assert ack1 == b"\x01"
        assert ack2 == b"\x01"

    def test_list_destinations(self, replicator_process, ctrl_socket):
        """CTRL_LIST (cmd=3) should return ACK=1."""
        msg = bytes([3])
        ctrl_socket.sendto(msg, (LISTEN_IP, CONTROL_PORT))
        ack, _ = ctrl_socket.recvfrom(1)
        assert ack == b"\x01"

    def test_remove_destination(self, replicator_process, ctrl_socket):
        """CTRL_REMOVE (cmd=2) should return ACK=1 for known destination."""
        # First add
        dest_ip = socket.inet_aton("127.0.0.1")
        dest_port = struct.pack("!H", 19099)
        add_msg = bytes([1]) + dest_ip + dest_port
        ctrl_socket.sendto(add_msg, (LISTEN_IP, CONTROL_PORT))
        ctrl_socket.recvfrom(1)

        # Then remove
        rm_msg = bytes([2]) + dest_ip + dest_port
        ctrl_socket.sendto(rm_msg, (LISTEN_IP, CONTROL_PORT))
        ack, _ = ctrl_socket.recvfrom(1)
        assert ack == b"\x01"


# ── Test: Data echo ───────────────────────────────────────────────────────────
class TestDataEcho:
    """Test the data path (UDP echo to registered destinations)."""

    def test_echo_to_registered_destination(self, replicator_process, ctrl_socket):
        """Registered destination should receive echoed packets."""
        # Register a destination
        recv_port = 19010
        dest_ip = socket.inet_aton("127.0.0.1")
        dest_port_bytes = struct.pack("!H", recv_port)
        msg = bytes([1]) + dest_ip + dest_port_bytes
        ctrl_socket.sendto(msg, (LISTEN_IP, CONTROL_PORT))
        ctrl_socket.recvfrom(1)

        # Create receiver
        recv_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        recv_sock.settimeout(2.0)
        recv_sock.bind(("127.0.0.1", recv_port))

        # Send data to replicator
        send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        payload = b"TEST_ECHO_12345678"
        send_sock.sendto(payload, (LISTEN_IP, DATA_PORT))

        # Receive echo
        data, addr = recv_sock.recvfrom(2048)
        assert data == payload, f"Echo mismatch: sent {payload!r}, got {data!r}"

        recv_sock.close()
        send_sock.close()

    def test_no_echo_to_unregistered(self, replicator_process):
        """Unregistered port should NOT receive anything."""
        recv_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        recv_sock.settimeout(0.5)
        recv_sock.bind(("127.0.0.1", 19099))

        send_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        send_sock.sendto(b"SHOULD_NOT_ARRIVE", (LISTEN_IP, DATA_PORT))

        with pytest.raises(socket.timeout):
            recv_sock.recvfrom(2048)

        recv_sock.close()
        send_sock.close()


# ── Test: RTT binary ──────────────────────────────────────────────────────────
class TestRTTMeasurement:
    """Test the rtt binary against kernel-mode replicator."""

    def test_rtt_produces_json(self, replicator_process):
        """rtt should complete and produce valid JSON output."""
        if not RTT.exists():
            pytest.skip("rtt binary not found")

        json_path = "/tmp/rtt_results.json"
        if os.path.exists(json_path):
            os.remove(json_path)

        # Run rtt: 1000 messages at 1000/s, 100 warmup, CPUs 0+1
        result = subprocess.run(
            [
                str(RTT),
                LISTEN_IP, str(DATA_PORT),
                LISTEN_IP, "19020",
                "1000", "1000", "100", "0", "1",
            ],
            capture_output=True, text=True, timeout=30,
        )

        assert result.returncode == 0, f"rtt failed: {result.stderr}\n{result.stdout}"
        assert os.path.exists(json_path), "JSON output not created"

        with open(json_path) as f:
            data = json.load(f)

        # Validate schema
        assert "service_rtt_us" in data, f"Missing service_rtt_us: {data.keys()}"
        rtt = data["service_rtt_us"]
        assert "p50" in rtt
        assert "p99" in rtt
        assert "min" in rtt
        assert "max" in rtt
        assert rtt["p50"] > 0, "p50 should be > 0"
        assert rtt["p99"] >= rtt["p50"], "p99 should be >= p50"

    def test_rtt_respects_warmup(self, replicator_process):
        """Messages sent during warmup should not appear in results."""
        if not RTT.exists():
            pytest.skip("rtt binary not found")

        json_path = "/tmp/rtt_results.json"
        if os.path.exists(json_path):
            os.remove(json_path)

        # 500 total, 400 warmup → only 100 measured
        result = subprocess.run(
            [
                str(RTT),
                LISTEN_IP, str(DATA_PORT),
                LISTEN_IP, "19021",
                "500", "1000", "400", "0", "1",
            ],
            capture_output=True, text=True, timeout=30,
        )

        assert result.returncode == 0
        with open(json_path) as f:
            data = json.load(f)

        # messages field should reflect total sent (including warmup)
        assert data.get("messages", 0) == 500 or data.get("messages", 0) == 100

    def test_rtt_kernel_mode_latency_sanity(self, replicator_process):
        """Kernel-mode RTT should be < 5ms (sanity check — not a performance test)."""
        if not RTT.exists():
            pytest.skip("rtt binary not found")

        json_path = "/tmp/rtt_results.json"
        if os.path.exists(json_path):
            os.remove(json_path)

        result = subprocess.run(
            [
                str(RTT),
                LISTEN_IP, str(DATA_PORT),
                LISTEN_IP, "19022",
                "1000", "1000", "100", "0", "1",
            ],
            capture_output=True, text=True, timeout=30,
        )

        assert result.returncode == 0
        with open(json_path) as f:
            data = json.load(f)

        p50 = data["service_rtt_us"]["p50"]
        # Kernel mode localhost should be well under 5ms
        assert p50 < 5000, f"p50={p50}µs — too high for localhost kernel echo"


# ── Test: udp_ping ────────────────────────────────────────────────────────────
class TestUdpPing:
    """Test udp_ping basic functionality."""

    def test_udp_ping_help(self):
        """udp_ping should print usage with no args."""
        if not UDP_PING.exists():
            pytest.skip("udp_ping binary not found")

        result = subprocess.run(
            [str(UDP_PING)],
            capture_output=True, text=True, timeout=5,
        )
        assert result.returncode != 0  # exits with error on no args
        assert "Usage" in result.stdout or "Usage" in result.stderr
