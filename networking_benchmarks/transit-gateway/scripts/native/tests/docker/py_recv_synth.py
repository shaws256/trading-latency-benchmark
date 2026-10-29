#!/usr/bin/env python3
# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""TEST ONLY: run the unmodified scripts/mcast_recv.py where no hardware stamps exist.

Containers (veth) cannot do SIOCSHWTSTAMP, so the production Python receiver exits
immediately there. This wrapper imports it unchanged and patches only the two
hardware-specific functions, mirroring the C++ test build (-DMCAST_RECV_SYNTH_HW):

    enable_hw_timestamping -> no-op
    parse_scm_timestamps   -> (sw_ns, sw_ns - 700): the hardware stamp is synthesized

Everything else (socket setup, receive loop, statistics, output) is the real code, so
the two receivers can be compared on identical traffic.
"""
import sys

sys.path.insert(0, "/src")  # scripts/ (mcast_recv.py)
import mcast_recv as m  # noqa: E402

SYNTH_HW_OFFSET_NS = 700
_orig_parse = m.parse_scm_timestamps


def _synth_parse(ancdata):
    sw_ns, _hw_ns = _orig_parse(ancdata)
    return sw_ns, (sw_ns - SYNTH_HW_OFFSET_NS if sw_ns else 0)


m.parse_scm_timestamps = _synth_parse
m.enable_hw_timestamping = lambda *a, **k: None

if __name__ == "__main__":
    sys.exit(m.main())
