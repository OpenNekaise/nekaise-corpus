---
name: dig
description: Run one autonomous corpus-growth round — discover new open built-environment sources (find_sources.py OpenAlex/OSTI/arXiv + find_github.py GitHub repos + a web search for new veins), append the good ones, load + prune, and commit locally (never push). This is the loop the daily cron runs; also runnable by hand to grow the corpus in one shot.
---

# Skill: dig

One **autonomous growth round**: discover new open built-environment sources across every backend,
add the good ones, load + prune them, and **commit locally (never push).** This is the loop the
daily cron runs headless (see [`go`](../go/SKILL.md) / `scripts/dig.sh`); you can also run it by
hand as `/dig` any time you want to grow the corpus in one shot.

## The round (run outside any sandbox — needs network)

1. **Run the canonical fail-closed round and commit locally:**
   ```
   python scripts/run_round.py --commit
   ```
   `registry/backends.json` is the sole list of finder scripts, fixed arguments, and enabled/paused
   state; `registry/rotation.json` supplies their committed pointers. The runner holds the repo lock,
   advances each pointer only after that finder exits successfully, then performs fetch → prune →
   clean → check → README stats → index refresh → lint → tests. Any required failure rolls tracked
   state back and prevents commit/push. **Never change the cleaning ruleset as part of a dig.**
   **Never `git push`** — the maintainer reviews the commit and pushes.

2. **Review and summarize** what landed (docs by source/topic, new total, failures) so the commit
   log and `logs/run_history.jsonl` tell the story.

3. **Widen separately (judgment — the part a human/agent adds over the scripts):** after the
   mechanical round is committed, spend some budget
   looking for *new veins*, not just more of the head:
   - Web-search for open built-environment collections we don't tap yet (new gov programs, datasets,
     standards bodies, doc sites) and add them — a single PDF/HTML source goes straight into
     `registry/curated.yaml`; a whole doc site goes through [`crawl-docs`](../crawl-docs/SKILL.md); a new
     GitHub repo goes into `find_github.py`'s curated `REPOS` list.
   - Tune `find_sources.py`'s `QUERIES` toward gaps (we're paper-heavy; thin on equipment depth,
     codes, datasets, international).

   Treat a new finder or query family as its own reviewed code/config change; add it to
   `registry/backends.json`, add/update its rotation entry, and run the architecture contracts.

## Notes
- Respect each source's `license`; prefer `public-domain` / `cc-by` / `cc-by-sa` / permissive-`open`.
  Never add `proprietary-internal` bytes.
- The mission is **coverage** — keep widening discovery (new backends, new source types, deeper
  enumeration of known collections), not just re-fetching the popular head.
- One-off helper scripts you write while digging go in `workspace/` (git-ignored), never the repo
  root. Promote anything durable into `scripts/`.
