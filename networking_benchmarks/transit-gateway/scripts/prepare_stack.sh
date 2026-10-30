#!/bin/bash
# One-time instance preparation: scripts/prepare_stack.sh --stack-name NAME [--help]
PYTHONPATH="$(cd "$(dirname "$0")" && pwd)${PYTHONPATH:+:$PYTHONPATH}" exec python3 -m tgw_bench prepare "$@"
