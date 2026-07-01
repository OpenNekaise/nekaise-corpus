---
name: dig
description: Run one autonomous corpus-growth round — discover new open building-energy sources (find_sources.py OpenAlex/OSTI/arXiv + find_github.py GitHub repos + a web search for new veins), append the good ones, load + prune, and commit locally (never push). This is the loop the daily cron runs; also runnable by hand to grow the corpus in one shot.
---

# dig

Canonical instructions: **[`skills/dig.md`](../../../skills/dig.md)** — read and follow that file
(single source of truth; Codex reads it via `AGENTS.md`).

In short: `python find_sources.py --per 20 --append` + `python find_github.py --append` to discover
and register new open sources, web-search for new veins to widen coverage, then `python
build_corpus.py` (load) → `python prune_corpus.py --apply` (quality gate) → `git commit` of ONLY
`sources.yaml` + `manifest.jsonl`. **Never `git push`** and never commit `raw/`/`text/` — the
maintainer reviews the commits and pushes. Prefer redistributable licenses; never add
`proprietary-internal` bytes.
