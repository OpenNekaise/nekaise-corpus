#!/usr/bin/env bash
# Six-hour Codex-first maintenance handoff. It waits for the scheduled growth loop and the
# canonical corpus-round lock, then owns both while Codex and Claude inspect or edit tracked state.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
mkdir -p "$REPO/logs" "$REPO/workspace"

LOG="$REPO/logs/maintainer-$(date +%Y%m%d-%H%M%S).log"
REQUEST="$REPO/workspace/.maintenance-requested"
CONTINUOUS_LOCK="$REPO/workspace/.continuous-dig.lock"
CORPUS_LOCK="$REPO/workspace/.corpus-round.lock"
WAIT="${MAINTAINER_LOCK_WAIT_SECONDS:-11700}"

exec >>"$LOG" 2>&1
echo "[$(date -Is)] maintainer wake; requesting next between-round window"
date -Is > "$REQUEST"
cleanup() {
  rm -f "$REQUEST"
}
trap cleanup EXIT INT TERM

exec 8>"$CONTINUOUS_LOCK"
if ! flock -w "$WAIT" 8; then
  echo "[$(date -Is)] maintainer deferred: no between-round window within ${WAIT}s"
  exit 0
fi
exec 9>>"$CORPUS_LOCK"
if ! flock -w "$WAIT" 9; then
  echo "[$(date -Is)] maintainer deferred: corpus round remained active for ${WAIT}s"
  exit 0
fi
printf '%s' "$$" > "$CORPUS_LOCK"

rm -f "$REQUEST"
echo "[$(date -Is)] maintainer owns the scheduled-growth and corpus-round locks"
set +e
"${PYTHON_BIN:-$REPO/.venv/bin/python}" scripts/maintainer.py
code=$?
set -e
echo "[$(date -Is)] maintainer done (exit $code)"
exit "$code"
