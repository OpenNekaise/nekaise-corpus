#!/usr/bin/env bash
# Daily deterministic growth round. The open-ended "find a new vein" judgment remains available
# to an interactive agent; scheduled operation uses the same fail-closed control plane as marathon.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
if [ -z "${PYTHON_BIN:-}" ]; then
  if [ -x "$REPO/.venv/bin/python" ]; then PYTHON_BIN="$REPO/.venv/bin/python"
  else PYTHON_BIN="$(command -v python3 || command -v python)"; fi
fi

mkdir -p "$REPO/logs"
LOG="$REPO/logs/dig-$(date +%Y%m%d-%H%M%S).log"
MAX="${DIG_MAX_SECONDS:-10800}"

# The six-hour AI maintainer requests the gap between rounds by creating this flag, then takes
# the outer continuous-dig lock.  A persistent block means its last action left tracked state that
# is not safe for another corpus round; only the next maintainer pass should clear it.
if [ -e "$REPO/workspace/.maintenance-requested" ]; then
  echo "[$(date -Is)] dig deferred: maintainer requested the next between-round window"
  exit 0
fi
if [ -e "$REPO/workspace/.maintenance-blocked" ]; then
  echo "[$(date -Is)] dig blocked: $(tr '\n' ' ' < "$REPO/workspace/.maintenance-blocked")"
  exit 0
fi

echo "[$(date -Is)] dig start (cap ${MAX}s) -> $LOG"
set +e
timeout "${MAX}s" "$PYTHON_BIN" scripts/run_round.py --commit --lock-timeout 30 >>"$LOG" 2>&1
code=$?
set -e
if [ "$code" -eq 124 ]; then
  echo "[$(date -Is)] dig hit the ${MAX}s cap" | tee -a "$LOG"
else
  echo "[$(date -Is)] dig done (exit $code)" | tee -a "$LOG"
fi
exit "$code"
