#!/usr/bin/env bash
# install_cron.sh — install (or remove) the crontab entry that runs scripts/dig.sh.
# Idempotent: re-running replaces the existing nekaise-corpus line rather than duplicating it.
# The dig round grows the registry locally and COMMITS, but NEVER pushes — you review + push
# (or let the maintainer cron do it).
#
#   bash scripts/install_cron.sh                 # daily at 02:00 local
#   DIG_HOUR=3 bash scripts/install_cron.sh      # daily at 03:00 local
#   DIG_CONTINUOUS=1 bash scripts/install_cron.sh # back-to-back rounds: the tick is every minute,
#                                                 # `flock -n` keeps one round at a time, and dig.sh
#                                                 # steps aside when the maintainer asks for a window
#   bash scripts/install_cron.sh --remove        # uninstall (either mode)
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TAG="# nekaise-corpus daily dig"
CONTINUOUS_TAG="# nekaise-corpus continuous dig"

without_dig_cron() {
  grep -vF -e "$TAG" -e "$CONTINUOUS_TAG" || true
}

if [ "${1:-}" = "--remove" ]; then
  (crontab -l 2>/dev/null || true) | without_dig_cron | crontab -
  echo "removed the nekaise-corpus dig cron (if it was installed)"
  exit 0
fi

HOUR="${DIG_HOUR:-2}"
if [ -z "${PYTHON_BIN:-}" ]; then
  if [ -x "$REPO/.venv/bin/python" ]; then PYTHON_BIN="$REPO/.venv/bin/python"
  else PYTHON_BIN="$(command -v python3 || command -v python || true)"; fi
fi
if [ -z "$PYTHON_BIN" ] || ! "$PYTHON_BIN" -c "import requests,yaml,pypdf,bs4" 2>/dev/null; then
  echo "ERROR: corpus Python dependencies are missing. Create .venv or pass PYTHON_BIN=/path/to/python." >&2
  exit 1
fi
chmod +x "$REPO/scripts/dig.sh"
if [ "${DIG_CONTINUOUS:-0}" = "1" ]; then
  # Every minute, not every few: a finished round otherwise idles until the next tick (avg 2.5 min
  # at */5 — ~20% of a 10-minute round). flock -n makes the extra ticks free no-ops.
  LINE="* * * * * cd '$REPO' && /usr/bin/flock -n '$REPO/workspace/.continuous-dig.lock' /usr/bin/env DIG_MAX_SECONDS=${DIG_MAX_SECONDS:-10800} PYTHON_BIN='$PYTHON_BIN' /usr/bin/bash scripts/dig.sh >/dev/null 2>&1  $CONTINUOUS_TAG"
  WHAT="continuous dig cron (every minute; one round at a time via flock)"
  SHOW="$CONTINUOUS_TAG"
else
  LINE="0 $HOUR * * * /usr/bin/flock -n '$REPO/workspace/.continuous-dig.lock' /usr/bin/env PYTHON_BIN='$PYTHON_BIN' '$REPO/scripts/dig.sh'  $TAG"
  WHAT="daily dig cron (${HOUR}:00 local)"
  SHOW="$TAG"
fi

# drop any prior nekaise dig line (either mode), then add ours
( (crontab -l 2>/dev/null || true) | without_dig_cron; echo "$LINE" ) | crontab -

echo "installed $WHAT:"
crontab -l | grep -F "$SHOW"
echo
echo "  logs:    $REPO/logs/dig-*.log"
echo "  remove:  bash scripts/install_cron.sh --remove"
echo "  note:    it commits new sources locally but never pushes — you review + push."
