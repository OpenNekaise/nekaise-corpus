---
name: find-sources
description: Grow the building-energy corpus — run find_sources.py to discover new open-access sources via OpenAlex, review them (relevance, license, direct-PDF), add the good ones to sources.yaml, then load + verify. Use when asked to find more data, enlarge, or grow the corpus.
---

# find-sources

Canonical instructions: **[`skills/find-sources.md`](../../../skills/find-sources.md)** — read and
follow that file (single source of truth; Codex reads it via `AGENTS.md`).

In short: `python find_sources.py --per 20` queries OpenAlex for open-access building-energy works,
dedups vs the manifest/registry, and prints ready-to-paste `sources.yaml` entries. REVIEW each
(on-topic? license redistributable? is the url a direct PDF, not a landing page?), paste the good
ones under `sources:` in `sources.yaml`, then run the **load-corpus** skill (`build_corpus.py`) to
fetch + verify. Prefer `cc-by` / `cc-by-sa` / `cc0` / `public-domain`; never add proprietary bytes.
