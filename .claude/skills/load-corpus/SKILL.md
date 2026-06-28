---
name: load-corpus
description: Download / refresh the building-energy LLM corpus on this machine from the curated registry (sources.yaml) via build_corpus.py, then verify it (failures, text quality, hashes). Use when asked to load, fetch, build, or refresh the corpus, or after adding sources to sources.yaml.
---

# load-corpus

Canonical instructions: **[`skills/load-corpus.md`](../../../skills/load-corpus.md)** — read and
follow that file (single source of truth; Codex reads the same file via `AGENTS.md`).

In short: run `python build_corpus.py` (reads `sources.yaml` → downloads into `raw/`, extracts
`text/`, writes `manifest.jsonl`; idempotent, dedup by sha256; needs network, run outside a
sandbox). Then VERIFY: report ok/failed counts, investigate 404s (fix `sources.yaml`), spot-check
`text/*.md` quality, optionally re-hash against the manifest. `raw/` and `text/` are git-ignored
copyrighted bytes — never commit them; respect each source's `license`.
