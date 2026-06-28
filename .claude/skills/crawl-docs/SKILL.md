---
name: crawl-docs
description: Add a multi-page documentation site (software/ontology docs like EnergyPlus, VOLTTRON, Brick, Haystack, ASHRAE 223P) to the building-energy corpus. Use when a valuable reference is a doc site, not a single PDF, and the single-URL loader can't reach it.
---

# crawl-docs

Canonical instructions: **[`skills/crawl-docs.md`](../../../skills/crawl-docs.md)** — read and follow
that file (single source of truth; Codex reads it via `AGENTS.md`).

In short: `python crawl_docs.py --seed <url> --prefix <path> --source <tag> --topic <t> --license <l>
--max 80 --append` BFS-crawls a doc site within one domain + path prefix, collects content pages, and
appends them to `sources.yaml` as `html` sources (`crawl-` ids). Then run **load-corpus**
(`build_corpus.py`) to fetch each page and `prune_corpus.py --apply` to drop thin/off-topic pages.
Reproducible: the registry freezes the page list (each page has its own sha256); clones fetch it
rather than re-crawling. Prefer openly-licensed docs; respect robots / ToS.
