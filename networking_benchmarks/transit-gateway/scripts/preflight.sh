#!/bin/bash
# preflight.sh - clock and NIC readiness check, run ON a benchmark instance.
#
# Modes:
#   preflight.sh [--max-offset-us N]   full check; forces a clock step first. Exit 1 on failure.
#   preflight.sh --clock               read-only snapshot of the chrony state (no step)
#
# Output is key=value lines, then PASS or one FAIL:<code> line per failed check.
#
# The gate is chrony's "System time" (how far the system clock is from the PHC-disciplined
# time right now), not "Last offset" (the previous measurement) or "RMS offset" (a long
# average): only System time bounds the error of a timestamp taken now.
#
# Test seams: SYSROOT prefixes /sys paths; chronyc, ethtool and ip come from PATH;
# PREFLIGHT_SETTLE is the seconds to wait after the step (default 3); IFACE overrides detection.
set -u

MAX_OFFSET_US=10
MODE=full
while [ $# -gt 0 ]; do
  case "$1" in
    --max-offset-us) MAX_OFFSET_US="$2"; shift 2 ;;
    --clock) MODE=clock; shift ;;
    -h|--help) sed -n 2,16p "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
done

SYSROOT="${SYSROOT:-}"
SETTLE="${PREFLIGHT_SETTLE:-3}"
FAILS=""
fail() { FAILS="$FAILS FAIL:$1"; }

# chrony "System time : 0.000000062 seconds slow of NTP time" -> signed microseconds
system_time_us() {
  chronyc tracking 2>/dev/null | awk '/^System time/ {
    v = $4 + 0; if ($6 == "slow") v = -v; printf "%.3f", v * 1e6; found = 1 }
    END { if (!found) print "unreadable" }'
}

if [ "$MODE" = clock ]; then
  echo "system_time_us=$(system_time_us)"
  chronyc tracking 2>/dev/null | awk -F': *' '/^Reference ID/ {print "reference=" $2} /^Leap status/ {print "leap=" $2}'
  exit 0
fi

# 1. ENA PHC support loaded
PHC_ENABLE=$(cat "$SYSROOT/sys/module/ena/parameters/phc_enable" 2>/dev/null || echo 0)
echo "phc_enable=$PHC_ENABLE"
[ "$PHC_ENABLE" = "1" ] || fail phc_not_enabled

# 2. The ENA PTP device exists
PTP_DEV=""
for f in "$SYSROOT"/sys/class/ptp/*/clock_name; do
  [ -f "$f" ] || continue
  if grep -q ena-ptp "$f"; then PTP_DEV="/dev/$(basename "$(dirname "$f")")"; break; fi
done
echo "ptp_device=${PTP_DEV:-none}"
[ -n "$PTP_DEV" ] || fail no_ena_ptp_device

# 3. The NIC advertises hardware receive stamping
IFACE="${IFACE:-$(ip -o -4 route show to default 2>/dev/null | awk '{print $5}' | head -1)}"
echo "iface=${IFACE:-unknown}"
if ethtool -T "${IFACE:-eth0}" 2>&1 | grep -q hardware-receive; then
  echo "hw_receive=yes"
else
  echo "hw_receive=no"; fail no_hw_receive
fi

# 4. Exactly one PHC refclock, and it is the selected source
SOURCES=$(chronyc sources 2>&1)
REFCLOCKS=$(echo "$SOURCES" | grep -c '^#')
SELECTED=$(echo "$SOURCES" | awk '/^[#^=][*]/ {print $2; exit}')
echo "refclocks=$REFCLOCKS"
echo "selected=${SELECTED:-none}"
[ "$REFCLOCKS" = "1" ] || fail refclock_count_not_1
echo "$SOURCES" | grep -q '^#\*' || fail phc_not_selected

# 5. Step away any residual offset, then require the clock to be close
chronyc makestep >/dev/null 2>&1 || fail makestep_failed
chronyc burst 4/4 >/dev/null 2>&1 || true
sleep "$SETTLE"
OFFSET_US=$(system_time_us)
echo "system_time_us=$OFFSET_US"
echo "max_offset_us=$MAX_OFFSET_US"
if [ "$OFFSET_US" = "unreadable" ]; then
  fail clock_unreadable
elif ! awk -v v="$OFFSET_US" -v m="$MAX_OFFSET_US" 'BEGIN { if (v < 0) v = -v; exit !(v <= m) }'; then
  fail "clock_offset_exceeds_${MAX_OFFSET_US}us"
fi
chronyc tracking 2>/dev/null | awk -F': *' '/^Leap status/ {print "leap=" $2}'

if [ -z "$FAILS" ]; then
  echo PASS
  exit 0
fi
for f in $FAILS; do echo "$f"; done
exit 1
