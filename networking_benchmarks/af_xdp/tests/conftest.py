"""
Fixtures shared across test modules.
"""

import signal
import subprocess
import time
from pathlib import Path

import pytest

AF_XDP_DIR = Path(__file__).parent.parent
REPLICATOR = AF_XDP_DIR / "replicator"

LISTEN_IP = "127.0.0.1"
DATA_PORT = 19000
CONTROL_PORT = 12345


@pytest.fixture(scope="session", autouse=True)
def check_binaries():
    """Fail fast if binaries aren't built."""
    if not REPLICATOR.exists():
        pytest.skip(
            f"Binaries not found at {AF_XDP_DIR}. "
            f"Run 'make all' in {AF_XDP_DIR} first.",
            allow_module_level=True,
        )
