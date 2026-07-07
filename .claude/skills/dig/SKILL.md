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

1. **Discover — every backend, append straight in:**
   ```
   python scripts/find_sources.py --per 20 --append   # OpenAlex / OSTI / arXiv papers + gov reports
   python scripts/find_github.py --append             # curated GitHub repos: READMEs + docs/*.md + *.rst
   ```
   Both dedup against `manifest.jsonl` + `sources.yaml` before appending, so re-running is safe.

2. **Widen (judgment — the part a human/agent adds over the scripts):** spend a little of the budget
   looking for *new veins*, not just more of the head:
   - Web-search for open built-environment collections we don't tap yet (new gov programs, datasets,
     standards bodies, doc sites) and add them — a single PDF/HTML source goes straight into
     `sources.yaml`; a whole doc site goes through [`crawl-docs`](../crawl-docs/SKILL.md); a new
     GitHub repo goes into `find_github.py`'s curated `REPOS` list.
   - Tune `find_sources.py`'s `QUERIES` toward gaps (we're paper-heavy; thin on equipment depth,
     codes, datasets, international).

3. **Load the new bytes:**
   ```
   python scripts/build_corpus.py                # fetch only the newly-appended sources
   ```
   Read the summary; investigate failures (fix or drop dead URLs — never leave a known-404 entry).

4. **Prune (the quality gate):**
   ```
   python scripts/prune_corpus.py --apply        # drop thin / garbage / non-English / off-topic
   ```
   The pruner only touches *machine-discovered* docs (id prefixes `oa-`/`ope-`/`ost-`/`arx-`/
   `crawl-`/`gh-`/`oer-`), never hand-curated ones; it edits `sources.yaml` in place and validates
   the result before writing.

5. **Commit locally — do NOT push:**
   ```
   git add sources.yaml manifest.jsonl
   git commit -m "dig: +<N> docs -> <total> docs / <tokens> tokens (<what landed>)"
   ```
   Only `sources.yaml` + `manifest.jsonl` are committed (pointers + provenance). `raw/` and `text/`
   are git-ignored and must never be committed. **Never `git push`** — the maintainer reviews the
   commits and pushes.

6. **Summarize** what landed this round (docs added by source/topic, new total, any failures) so the
   commit log and `logs/dig-*.log` tell the story.

## Notes
- Respect each source's `license`; prefer `public-domain` / `cc-by` / `cc-by-sa` / permissive-`open`.
  Never add `proprietary-internal` bytes.
- The mission is **coverage** — keep widening discovery (new backends, new source types, deeper
  enumeration of known collections), not just re-fetching the popular head.
- One-off helper scripts you write while digging go in `workspace/` (git-ignored), never the repo
  root. Promote anything durable into `scripts/`.
