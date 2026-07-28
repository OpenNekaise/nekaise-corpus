#!/usr/bin/env bash
# Repeated fail-closed growth rounds. Every round uses run_round.py, so clean/check/tests are never
# skipped and a failed discovery/build/prune cannot be committed or pushed.
set -euo pipefail

cd "$(dirname "$0")/.."
REPO="$PWD"
if [ -z "${PYTHON_BIN:-}" ]; then
  if [ -x "$REPO/.venv/bin/python" ]; then PYTHON_BIN="$REPO/.venv/bin/python"
  else PYTHON_BIN="$(command -v python3 || command -v python)"; fi
fi
HOURS="${HOURS:-12}"
SLEEP="${SLEEP:-90}"
MIN_FREE_KB="${MIN_FREE_KB:-31457280}"
END=$(( $(date +%s) + HOURS*3600 ))
mkdir -p "$REPO/logs"
LOG="$REPO/logs/marathon-$(date +%Y%m%d-%H%M).log"
say(){ echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG"; }

say "marathon START — until $(date -d "@$END")"
round=0
failures=0
while [ "$(date +%s)" -lt "$END" ]; do
  if [ -f "$REPO/STOP_MARATHON" ]; then
    rm -f "$REPO/STOP_MARATHON"
    say "STOP_MARATHON found — stopping"
    break
  fi
  avail_kb=$(df -Pk "$REPO" | awk 'NR==2{print $4}')
  if [ "${avail_kb:-0}" -lt "$MIN_FREE_KB" ]; then
    say "LOW DISK (${avail_kb}KB free < ${MIN_FREE_KB}KB) — stopping"
    break
  fi
  round=$((round+1))
  say "round $round start"
  if "$PYTHON_BIN" scripts/run_round.py --commit --push main --lock-timeout 30 >>"$LOG" 2>&1; then
    say "round $round validated and pushed"
  else
    failures=$((failures+1))
    say "round $round FAILED — nothing committed or pushed"
  fi
  sleep "$SLEEP"
done
say "marathon DONE — $round rounds / $failures failures"
test "$failures" -eq 0
