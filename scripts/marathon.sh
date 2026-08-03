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
TARGET_TOKENS="${TARGET_TOKENS:-0}"
case "$TARGET_TOKENS" in
  ''|*[!0-9]*)
    echo "TARGET_TOKENS must be a non-negative integer" >&2
    exit 2
    ;;
esac
if [ -z "${HOURS:-}" ]; then
  if [ "$TARGET_TOKENS" -gt 0 ]; then HOURS=168
  else HOURS=12
  fi
fi
SLEEP="${SLEEP:-90}"
MIN_FREE_KB="${MIN_FREE_KB:-31457280}"
END=$(( $(date +%s) + HOURS*3600 ))
mkdir -p "$REPO/logs"
LOG="$REPO/logs/marathon-$(date +%Y%m%d-%H%M).log"
say(){ echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG"; }
# TARGET_TOKENS is measured on the CLEANED corpus/ (manifest corpus_chars — what a training run
# actually consumes), not the raw text/ stage; each round's clean step keeps it current.
current_tokens(){ "$PYTHON_BIN" scripts/update_readme_stats.py --print-corpus-tokens; }

say "marathon START — until $(date -d "@$END")"
if [ "$TARGET_TOKENS" -gt 0 ]; then
  say "token target: $(current_tokens) / $TARGET_TOKENS"
fi
round=0
failures=0
while [ "$(date +%s)" -lt "$END" ]; do
  if [ "$TARGET_TOKENS" -gt 0 ]; then
    tokens=$(current_tokens)
    if [ "$tokens" -ge "$TARGET_TOKENS" ]; then
      say "TARGET REACHED — $tokens / $TARGET_TOKENS tokens"
      break
    fi
  fi
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
    if [ "$TARGET_TOKENS" -gt 0 ]; then
      tokens=$(current_tokens)
      say "token progress: $tokens / $TARGET_TOKENS"
      if [ "$tokens" -ge "$TARGET_TOKENS" ]; then
        say "TARGET REACHED — $tokens / $TARGET_TOKENS tokens"
        break
      fi
    fi
  else
    failures=$((failures+1))
    say "round $round FAILED — nothing committed or pushed"
  fi
  sleep "$SLEEP"
done
say "marathon DONE — $round rounds / $failures failures"
test "$failures" -eq 0
