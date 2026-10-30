"""python3 -m tgw_bench {run|prepare} [options]"""
import sys

from .run import main, prepare_main

COMMANDS = {"run": main, "prepare": prepare_main}

if len(sys.argv) < 2 or sys.argv[1] not in COMMANDS:
    print(f"usage: python3 -m tgw_bench {{{'|'.join(COMMANDS)}}} [options]", file=sys.stderr)
    sys.exit(2)
sys.exit(COMMANDS[sys.argv[1]](sys.argv[2:]))
