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
if [ -z "${PYTHON_BIN:-}" ]; then
  if [ -x "$REPO/.venv/bin/python" ]; then PYTHON_BIN="$REPO/.venv/bin/python"
  else PYTHON_BIN="$(command -v python3 || command -v python || true)"; fi
fi
if [ -z "$PYTHON_BIN" ] || ! "$PYTHON_BIN" -c "import requests,yaml,pypdf,bs4" 2>/dev/null; then
  echo "ERROR: corpus Python dependencies are missing. Create .venv or pass PYTHON_BIN=/path/to/python." >&2
  exit 1
fi
chmod +x "$REPO/scripts/dig.sh"
LINE="0 $HOUR * * * PYTHON_BIN='$PYTHON_BIN' '$REPO/scripts/dig.sh'  $TAG"

# drop any prior nekaise line, then add ours
( crontab -l 2>/dev/null | grep -vF "$TAG" || true; echo "$LINE" ) | crontab -

echo "installed daily dig cron (${HOUR}:00 local):"
crontab -l | grep -F "$TAG"
echo
echo "  logs:    $REPO/logs/dig-*.log"
echo "  remove:  bash scripts/install_cron.sh --remove"
echo "  note:    it commits new sources locally but never pushes — you review + push."
