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
# prefer the repo venv (system python on some hosts has no pip/deps)
[ -x "$REPO/.venv/bin/python" ] && export PATH="$REPO/.venv/bin:$PATH"
HOURS="${HOURS:-12}"
SLEEP="${SLEEP:-90}"
MIN_FREE_KB="${MIN_FREE_KB:-31457280}"   # stop if < 30 GB free
END=$(( $(date +%s) + HOURS*3600 ))
mkdir -p "$REPO/logs"
LOG="$REPO/logs/marathon-$(date +%Y%m%d-%H%M).log"
say(){ echo "[$(date +%H:%M:%S)] $*" | tee -a "$LOG"; }

TRAILER1="Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
TRAILER2="Claude-Session: https://claude.ai/code/session_01DxxKfj3Nbcpx65nS4mAS75"

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

  # Every backend's next page/offset/bucket comes from the COMMITTED excavation state
  # (registry/rotation.json via scripts/rotation.py) — any machine resumes where the last stopped.
  run_finder() {  # run_finder <name> <fixed args...>: read pointer, run, advance on success
    local name=$1; shift
    local ptr
    ptr=$(python scripts/rotation.py next "$name" 2>>"$LOG") || { say "  $name: no rotation entry"; return 1; }
    if python "scripts/${name}.py" "$@" $ptr --append >>"$LOG" 2>&1; then
      python scripts/rotation.py advance "$name" >>"$LOG" 2>&1
    else
      say "  $name FAILED (pointer not advanced)"
    fi
  }
  run_finder find_osti     --rows 50 --pages 2 --max 400
  run_finder find_books    --per 25 --depth 25 --max 200
  run_finder find_archive  --rows 30 --max 300
  run_finder find_openaire --rows 50 --max 300
  run_finder find_nist     --rows 50 --max 300
  run_finder find_zenodo   --max 100
  run_finder find_patents  --max 400
  # Chinese patents: same script, separate rotation pointer (multilingual corpus since 07-09)
  if ptr=$(python scripts/rotation.py next find_patents_cn 2>>"$LOG"); then
    if python scripts/find_patents.py $ptr --countries CN --max 400 --append >>"$LOG" 2>&1; then
      python scripts/rotation.py advance find_patents_cn >>"$LOG" 2>&1
    else say "  find_patents_cn FAILED (pointer not advanced)"; fi
  fi
  # find_ibpsa is PAUSED (rate-triggered sgcaptcha) — see registry/rotation.json note
  if [ $(( (round-1) % 4 )) -eq 0 ]; then
    python scripts/find_sources.py --per 20 --append        >>"$LOG" 2>&1 || say "  find_sources FAILED"
    python scripts/find_github.py  --append                 >>"$LOG" 2>&1 || say "  find_github FAILED"
  fi
  python scripts/build_corpus.py                            >>"$LOG" 2>&1 || say "  build_corpus FAILED"
  python scripts/prune_corpus.py --apply                    >>"$LOG" 2>&1 || say "  prune FAILED"
  python scripts/update_readme_stats.py                     >>"$LOG" 2>&1 || true

  stats=$(python3 -c "import json,glob;r=[json.loads(l) for f in sorted(glob.glob('manifest/*.jsonl')) for l in open(f) if l.strip()];ok=[x for x in r if x.get('status')=='ok'];print(len(ok), sum(x.get('text_chars',0) for x in ok)//4)" 2>/dev/null || echo "0 0")
  DOCS=${stats% *}; TOK=${stats#* }; TOKM=$(( TOK/1000000 ))
  git add registry manifest pruned_urls.txt README.md 2>>"$LOG"
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
say "marathon DONE — $round rounds. final: $(python3 -c "import json,glob;r=[json.loads(l) for f in sorted(glob.glob('manifest/*.jsonl')) for l in open(f) if l.strip()];ok=[x for x in r if x.get('status')=='ok'];print(len(ok),'docs /', sum(x.get('text_chars',0) for x in ok)//4//1000000,'M tokens')" 2>/dev/null)"
