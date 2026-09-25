#!/usr/bin/env bash
# install_ops_cron.sh — install (or remove) the monitoring, sweep and backup-retention crontab
# lines of ADR 0001 stage 4 step 5. Idempotent: every line carries a tag and re-running replaces
# them. Additive monitoring only: nothing here changes a round, the store or the live cluster's
# configuration.
#
#   bash scripts/install_ops_cron.sh --print     # show the lines, change nothing
#   bash scripts/install_ops_cron.sh             # install them
#   bash scripts/install_ops_cron.sh --remove    # remove them
#
# Lines (REPO/PY below are this checkout and its Python):
#   */5  ops_health.py check               alerts: archive lag, backup/drill age, capacity, stalled
#                                          runs, promotion latency, materialization lag, review
#                                          backlog, sweep failures (workspace/ops-health.json,
#                                          logs/alerts.jsonl)
#   :23  integrity_sweep.py metadata       a 15-minute slice of the resumable metadata sweep
#   :53  integrity_sweep.py artifacts      a 15-minute slice of artifact re-verification
#   Mon 06:10 artifact_gc.py               the reference-checked GC report (dry run, deletes nothing)
#   03:30 pg_backup.py base + prune --retain-days 35 --daily-days 7
#                                          REPLACES the stage-2 "prune --keep 7" line (tag
#                                          "nekaise-corpus pg base backup"); the Sunday restore
#                                          drill line stays as it is (its code is upgraded)
# The sweeps and the GC report are no-ops under file authority.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if [ -z "${PYTHON_BIN:-}" ]; then
  if [ -x "$REPO/.venv/bin/python" ]; then PYTHON_BIN="$REPO/.venv/bin/python"
  else PYTHON_BIN="$(command -v python3)"; fi
fi
TAG_HEALTH="# nekaise-corpus ops health"
TAG_META="# nekaise-corpus integrity sweep metadata"
TAG_ART="# nekaise-corpus integrity sweep artifacts"
TAG_GC="# nekaise-corpus artifact gc report"
TAG_BASE="# nekaise-corpus pg base backup"

lines() {
  echo "*/5 * * * * cd '$REPO' && /usr/bin/flock -n '$REPO/workspace/.ops-health-cron.lock' '$PYTHON_BIN' scripts/ops_health.py check >> '$REPO/logs/ops-health.log' 2>&1  $TAG_HEALTH"
  echo "23 * * * * cd '$REPO' && '$PYTHON_BIN' scripts/integrity_sweep.py metadata --seconds 900 >> '$REPO/logs/integrity-sweep.log' 2>&1  $TAG_META"
  echo "53 * * * * cd '$REPO' && '$PYTHON_BIN' scripts/integrity_sweep.py artifacts --seconds 900 >> '$REPO/logs/integrity-sweep.log' 2>&1  $TAG_ART"
  echo "10 6 * * 1 cd '$REPO' && '$PYTHON_BIN' scripts/artifact_gc.py >> '$REPO/logs/artifact-gc.log' 2>&1  $TAG_GC"
  echo "30 3 * * * cd '$REPO' && '$PYTHON_BIN' scripts/pg_backup.py base >> '$REPO/logs/pg-backup.log' 2>&1 && '$PYTHON_BIN' scripts/pg_backup.py prune --retain-days 35 --daily-days 7 >> '$REPO/logs/pg-backup.log' 2>&1  $TAG_BASE"
}

without_ours() {
  grep -vF -e "$TAG_HEALTH" -e "$TAG_META" -e "$TAG_ART" -e "$TAG_GC" -e "$TAG_BASE" || true
}

case "${1:-}" in
  --print) lines; exit 0 ;;
  --remove)
    (crontab -l 2>/dev/null || true) | without_ours | crontab -
    echo "removed the nekaise-corpus ops lines (the base backup line too: reinstall it with the"
    echo "stage-2 command or this script)"
    exit 0 ;;
  "") ;;
  *) echo "usage: $0 [--print|--remove]" >&2; exit 2 ;;
esac
mkdir -p "$REPO/logs" "$REPO/workspace"
( (crontab -l 2>/dev/null || true) | without_ours; lines ) | crontab -
echo "installed:"
crontab -l | grep -F -e "$TAG_HEALTH" -e "$TAG_META" -e "$TAG_ART" -e "$TAG_GC" -e "$TAG_BASE"
