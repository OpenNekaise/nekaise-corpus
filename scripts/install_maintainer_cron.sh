#!/usr/bin/env bash
# Install/remove the Codex-first, Claude-reviewed maintainer cron. Idempotent.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TAG="# nekaise-corpus ai maintainer"

if [ "${1:-}" = "--remove" ]; then
  (crontab -l 2>/dev/null || true) | grep -vF "$TAG" | crontab - || true
  echo "removed the nekaise-corpus AI maintainer cron (if installed)"
  exit 0
fi

if [ -z "${PYTHON_BIN:-}" ]; then
  if [ -x "$REPO/.venv/bin/python" ]; then
    PYTHON_BIN="$REPO/.venv/bin/python"
  else
    PYTHON_BIN="$(command -v python3 || command -v python || true)"
  fi
fi
if [ -z "$PYTHON_BIN" ] || ! "$PYTHON_BIN" -c "import json" 2>/dev/null; then
  echo "ERROR: Python is unavailable; pass PYTHON_BIN=/path/to/python." >&2
  exit 1
fi

MINUTE="${MAINTAINER_CRON_MINUTE:-17}"
LINE="$MINUTE */6 * * * cd '$REPO' && /usr/bin/flock -n '$REPO/workspace/.maintainer-cron.lock' /usr/bin/env PYTHON_BIN='$PYTHON_BIN' /usr/bin/bash scripts/run_maintainer.sh >/dev/null 2>&1  $TAG"
(crontab -l 2>/dev/null | grep -vF "$TAG" || true; echo "$LINE") | crontab -

echo "installed six-hour AI maintainer cron (minute $MINUTE):"
crontab -l | grep -F "$TAG"
echo "logs: $REPO/logs/maintainer-*"
echo "remove: bash scripts/install_maintainer_cron.sh --remove"
