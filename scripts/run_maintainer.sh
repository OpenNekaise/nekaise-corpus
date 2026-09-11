#!/usr/bin/env bash
# Python owns lock windows and process cleanup; exec forwards cron signals to that owner.
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
mkdir -p "$REPO/logs" "$REPO/workspace"
LOG="$REPO/logs/maintainer-$(date +%Y%m%d-%H%M%S).log"
exec >>"$LOG" 2>&1
echo "[$(date -Is)] maintainer wake"
exec "${PYTHON_BIN:-$REPO/.venv/bin/python}" scripts/maintainer.py
