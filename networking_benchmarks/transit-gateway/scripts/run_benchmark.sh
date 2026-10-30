#!/bin/bash
# Run a TGW multicast benchmark: scripts/run_benchmark.sh --stack-name NAME [--help]
PYTHONPATH="$(cd "$(dirname "$0")" && pwd)${PYTHONPATH:+:$PYTHONPATH}" exec python3 -m tgw_bench run "$@"
