#!/usr/bin/env bash
# dig.sh — daily corpus-growth round, run headless by the crontab entry that scripts/install_cron.sh
# installs. It drives Claude Code through the `dig` loop (.claude/skills/dig/SKILL.md): discover new open
# building-energy sources, append + load + prune, and COMMIT LOCALLY. It never pushes — you review
# the commits and push. Bounded by a wall-clock cap (default 3h). Logs to logs/dig-<ts>.log.
#
#   Manual test:            bash scripts/dig.sh
#   Override the cap:       DIG_MAX_SECONDS=7200 bash scripts/dig.sh
#   Override claude path:   CLAUDE_BIN=/path/to/claude bash scripts/dig.sh
set -uo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO" || exit 1

# cron runs with a minimal PATH — make sure common user bin dirs are reachable.
export PATH="$HOME/.local/bin:$HOME/bin:/usr/local/bin:/usr/bin:/bin:$PATH"
CLAUDE="${CLAUDE_BIN:-$(command -v claude || true)}"
MAX="${DIG_MAX_SECONDS:-10800}"   # 3h

mkdir -p "$REPO/logs"
LOG="$REPO/logs/dig-$(date +%Y%m%d-%H%M%S).log"

if [ -z "$CLAUDE" ]; then
  echo "[$(date -Is)] ERROR: claude CLI not found — set CLAUDE_BIN in the crontab line" | tee -a "$LOG"
  exit 127
fi

PROMPT='Run ONE nekaise-corpus growth round, following .claude/skills/dig/SKILL.md exactly.
Steps: (1) python scripts/find_sources.py --per 20 --append ; (2) python scripts/find_github.py --append ;
(3) briefly web-search for one or two NEW open building-energy veins we do not tap yet and add them;
(4) python scripts/build_corpus.py ; (5) python scripts/prune_corpus.py --apply ;
(6) git add registry/ manifest.jsonl pruned_urls.txt && git commit with a message summarizing what landed.
Do NOT git push. Never commit raw/ or text/. Investigate and report any load failures. End with a
one-paragraph summary of what was added this round.'

echo "[$(date -Is)] dig start (cap ${MAX}s, claude=$CLAUDE) -> $LOG"
timeout "${MAX}s" "$CLAUDE" -p "$PROMPT" --dangerously-skip-permissions >>"$LOG" 2>&1
code=$?
if [ "$code" -eq 124 ]; then
  echo "[$(date -Is)] dig hit the ${MAX}s cap and was stopped" | tee -a "$LOG"
else
  echo "[$(date -Is)] dig done (exit $code)" | tee -a "$LOG"
fi
exit 0
