#!/usr/bin/env bash
# marathon.sh — autonomous corpus-growth marathon. Runs growth rounds for HOURS hours, committing +
# pushing each round. Pure-Python growth (no Claude cost while running). Resilient: a failing step is
# logged and the loop continues. find_books (OAPEN CC-BY books — the productive, non-churning vein)
# runs every round; find_sources/find_github run every 4th round (they mostly re-churn now).
#
#   HOURS=12 bash scripts/marathon.sh          # run for 12 hours in the foreground
#   HOURS=12 nohup bash scripts/marathon.sh &  # detached
#
# Stop early: touch STOP_MARATHON in the repo root, or kill the process. On normal exit it re-installs
# the daily dig cron (removed at start to avoid a 02:00 collision with this run).
set -uo pipefail
cd "$(dirname "$0")/.."
REPO="$(pwd)"
HOURS="${HOURS:-12}"
SLEEP="${SLEEP:-90}"
MIN_FREE_KB="${MIN_FREE_KB:-31457280}"   # stop if < 30 GB free
END=$(( $(date +%s) + HOURS*3600 ))
mkdir -p "$REPO/logs"
LOG="$REPO/logs/marathon-$(date +%Y%m%d-%H%M).log"
say(){ echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG"; }

TRAILER1="Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
TRAILER2="Claude-Session: https://claude.ai/code/session_01QgQjLeCAJGzJFXBQ7Xar7q"

say "marathon START — until $(date -d @$END) (HOURS=$HOURS, sleep=${SLEEP}s)"
# avoid colliding with the 02:00 daily dig cron for the duration
bash "$REPO/scripts/install_cron.sh" --remove >>"$LOG" 2>&1 || true
say "daily dig cron removed for the marathon (re-installed on normal exit)"

round=0
while [ "$(date +%s)" -lt "$END" ]; do
  [ -f "$REPO/STOP_MARATHON" ] && { say "STOP_MARATHON found — stopping"; rm -f "$REPO/STOP_MARATHON"; break; }
  round=$((round+1))
  avail_kb=$(df -Pk "$REPO" | awk 'NR==2{print $4}')
  if [ "${avail_kb:-0}" -lt "$MIN_FREE_KB" ]; then
    say "LOW DISK (${avail_kb}KB free < ${MIN_FREE_KB}KB) — stopping"; break
  fi
  say "=== round $round (disk ${avail_kb}KB free) ==="

  # deep OSTI harvest (the scale lever: 200k+ public-domain records/query) — rotate page deeper each round
  python scripts/find_osti.py  --rows 50 --pages 2 --page $(( 1 + (round-1)*2 )) --max 400 --append >>"$LOG" 2>&1 || say "  find_osti FAILED"
  # rotate the OAPEN offset deeper each round (search is ~20s/call → 1 page per subject per round)
  python scripts/find_books.py --per 25 --depth 25 --offset $(( (round-1)*25 )) --max 200 --append >>"$LOG" 2>&1 || say "  find_books FAILED"
  # pre-1929 public-domain engineering texts (Internet Archive) — rotate the search page each round
  python scripts/find_archive.py --rows 30 --page "$round" --max 300 --append >>"$LOG" 2>&1 || say "  find_archive FAILED"
  if [ $(( (round-1) % 4 )) -eq 0 ]; then
    python scripts/find_sources.py --per 20 --append        >>"$LOG" 2>&1 || say "  find_sources FAILED"
    python scripts/find_github.py  --append                 >>"$LOG" 2>&1 || say "  find_github FAILED"
  fi
  python scripts/build_corpus.py                            >>"$LOG" 2>&1 || say "  build_corpus FAILED"
  python scripts/prune_corpus.py --apply                    >>"$LOG" 2>&1 || say "  prune FAILED"
  python scripts/update_readme_stats.py                     >>"$LOG" 2>&1 || true

  stats=$(python3 -c "import json;r=[json.loads(l) for l in open('manifest.jsonl') if l.strip()];ok=[x for x in r if x.get('status')=='ok'];print(len(ok), sum(x.get('text_chars',0) for x in ok)//4)" 2>/dev/null || echo "0 0")
  DOCS=${stats% *}; TOK=${stats#* }; TOKM=$(( TOK/1000000 ))
  git add registry manifest.jsonl pruned_urls.txt README.md 2>>"$LOG"
  if git commit -q -m "marathon r$round: ${DOCS} docs / ${TOKM}M tokens" -m "Autonomous OAPEN-books + papers + repos growth round." -m "$TRAILER1" -m "$TRAILER2" 2>>"$LOG"; then
    if git push origin main >>"$LOG" 2>&1; then say "  round $round PUSHED — ${DOCS} docs / ${TOKM}M tokens"
    else say "  round $round committed LOCAL (push failed)"; fi
  else
    say "  round $round — nothing new to commit"
  fi
  sleep "$SLEEP"
done

# best-effort restore of the daily dig cron
bash "$REPO/scripts/install_cron.sh" >>"$LOG" 2>&1 && say "daily dig cron re-installed" || say "cron re-install failed (run: bash scripts/install_cron.sh)"
say "marathon DONE — $round rounds. final: $(python3 -c "import json;r=[json.loads(l) for l in open('manifest.jsonl') if l.strip()];ok=[x for x in r if x.get('status')=='ok'];print(len(ok),'docs /', sum(x.get('text_chars',0) for x in ok)//4//1000000,'M tokens')" 2>/dev/null)"
