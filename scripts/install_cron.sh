#!/usr/bin/env bash
# install_cron.sh — install (or remove) the daily crontab entry that runs scripts/dig.sh.
# Idempotent: re-running replaces the existing nekaise-corpus line rather than duplicating it.
# The dig round grows the registry locally and COMMITS, but NEVER pushes — you review + push.
#
#   bash scripts/install_cron.sh            # install at 02:00 local
#   DIG_HOUR=3 bash scripts/install_cron.sh # install at 03:00 local
#   bash scripts/install_cron.sh --remove   # uninstall
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TAG="# nekaise-corpus daily dig"

if [ "${1:-}" = "--remove" ]; then
  crontab -l 2>/dev/null | grep -vF "$TAG" | crontab - || true
  echo "removed the nekaise-corpus dig cron (if it was installed)"
  exit 0
fi

HOUR="${DIG_HOUR:-2}"
CLAUDE_BIN="${CLAUDE_BIN:-$(command -v claude || true)}"
if [ -z "$CLAUDE_BIN" ]; then
  echo "ERROR: claude CLI not found on PATH. Re-run as: CLAUDE_BIN=/path/to/claude bash scripts/install_cron.sh" >&2
  exit 1
fi

chmod +x "$REPO/scripts/dig.sh"
# bake the resolved claude path into the line — cron's PATH is minimal and won't find it otherwise.
LINE="0 $HOUR * * * CLAUDE_BIN='$CLAUDE_BIN' '$REPO/scripts/dig.sh'  $TAG"

# drop any prior nekaise line, then add ours
( crontab -l 2>/dev/null | grep -vF "$TAG" || true; echo "$LINE" ) | crontab -

echo "installed daily dig cron (${HOUR}:00 local):"
crontab -l | grep -F "$TAG"
echo
echo "  logs:    $REPO/logs/dig-*.log"
echo "  remove:  bash scripts/install_cron.sh --remove"
echo "  note:    it commits new sources locally but never pushes — you review + push."
