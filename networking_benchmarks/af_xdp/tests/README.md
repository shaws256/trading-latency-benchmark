# tests/

Integration tests for the AF_XDP benchmark using pytest.

## Running

```bash
# Build first (kernel-mode works in containers)
make kernel-mode   # or: make all (on EC2)

# Run all tests
pytest -v

# Run specific test class
pytest -v -k TestRTTMeasurement
```

## Test Classes (14 tests)

### TestBinaries (4 tests)
Verifies all expected binaries exist after `make`.

### TestControlProtocol (4 tests)
Starts replicator in `--kernel-mode`, exercises the binary control protocol:
- ADD destination → ACK
- ADD duplicate → handled gracefully
- LIST → returns registered destinations
- REMOVE → removes destination

### TestDataEcho (2 tests)
Verifies packet echo behavior:
- Registered destination receives echoed packets
- Unregistered address does NOT receive packets

### TestRTTMeasurement (3 tests)
Runs the `rtt` client against kernel-mode replicator:
- Produces valid JSON at `/tmp/rtt_results.json`
- Warmup messages are excluded from results
- Kernel-mode latency sanity check (p50 < 5ms)

### TestUdpPing (1 test)
Verifies `udp_ping` CLI help output.

## Fixtures

| Fixture | Scope | Description |
|---------|-------|-------------|
| `build_dir` | session | Path to af_xdp directory |
| `bin_dir` | session | Path to built binaries |
| `replicator_process` | function | Starts/stops replicator in `--kernel-mode` |

## Requirements

- Python 3.9+
- pytest (`pip install pytest`)
- Built binaries (tests skip gracefully if missing)

## Container testing

```bash
docker run --platform linux/amd64 -v $(pwd):/src -w /src amazonlinux:2023 bash -c '
  dnf install -y gcc-c++ make python3-pip
  pip3 install pytest
  make kernel-mode
  pytest -v
'
```

All 14 tests pass in an AL2023 container with kernel-mode build.
