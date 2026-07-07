---
name: go
description: One-command entrypoint for the corpus — load everything indexed in the registry onto this machine, then (once fully caught up) offer to enable the daily growth cron. Use on a fresh clone or whenever the user just says "go", "start", or "get the data".
---

# Skill: go

The **one-command entrypoint**. A fresh clone runs this to materialize the corpus, and — once
everything indexed is already on disk — it offers to turn on the **daily growth job** that keeps
digging for more. This is what a new user does after cloning and opening Claude Code / Codex: just
say **"go"**.

## What to do

### 1. Load everything indexed
Make sure deps are present, then run the loader (needs network; run outside any sandbox):
```
pip install -r requirements.txt      # first run only
python scripts/build_corpus.py       # fetch every missing source from the registry
```
`build_corpus.py` is idempotent — it only fetches what is missing, so on a caught-up machine it
fetches nothing. Read the printed summary (`ok` vs `failed` by topic), investigate every failure
(a 404 = a moved/dead URL to fix or drop in the registry; a DNS error may be a sandbox), and
spot-check a few `text/*.md` for real content. Report failures — never hide them.

### 2. Decide: is the corpus fully materialized?
Look at the loader's opening line, `sources: N total, K to fetch`:
- **K > 0** — it just fetched new docs. Summarize what came in (doc count, MB, topics) and stop;
  the user can run `go` again later or grow the corpus.
- **K == 0** (nothing to fetch — everything indexed is already on disk, *like the maintainer's
  machine*) — the seed is fully materialized. **Offer the daily growth job** (next step).

### 3. Offer the daily "dig" cron (only when caught up)
Ask the user, in plain language:

> The corpus is fully loaded. Want me to enable the **daily growth job**? Once a day (≤3h) it runs
> `find_sources.py` + `find_github.py` + a web search for new veins, reviews the hits, appends the
> good ones, loads + prunes them, and **commits locally — it never pushes.** You review the commits
> and push when you're happy. It runs via your machine's crontab (only when the machine is on).

If **yes**:
```
bash scripts/install_cron.sh          # DIG_HOUR=2 by default (02:00 local); bakes the claude path
crontab -l | grep nekaise             # confirm it's installed
```
Tell them how to inspect (`logs/dig-*.log`), pause (`bash scripts/install_cron.sh --remove`), and
that new sources land as **local commits** for their review. The loop itself lives in the
[`dig`](../dig/SKILL.md) skill — the cron just runs it headless.

If **no**: leave it off; mention they can enable it later with `go` or `bash scripts/install_cron.sh`,
or grow the corpus by hand anytime with the [`dig`](../dig/SKILL.md) skill (one full round), or the
individual [`find-sources`](../find-sources/SKILL.md) / [`crawl-docs`](../crawl-docs/SKILL.md) skills
and `python scripts/find_github.py`.

## Notes
- **License discipline:** `raw/` and `text/` are git-ignored and never committed. The daily job only
  ever commits `registry/` + `manifest.jsonl` + `pruned_urls.txt` (pointers + provenance), never the bytes.
- The daily job **commits but does not push** — growth is unattended, publishing is your call.
