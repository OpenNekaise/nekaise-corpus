# AGENTS.md — nekaise-corpus

A curated, license-tagged, reproducible **recipe** for a building / HVAC / building-energy corpus
for LLM training. It ships the registry + loader, NOT the copyrighted bytes — you fetch your own
copy.

## How to work here

- **[`skills/load-corpus.md`](skills/load-corpus.md)** — download/refresh the corpus from
  `sources.yaml` via `build_corpus.py`, then verify (failures, text quality, hashes). Mirrored to
  `.claude/skills/` for Claude Code.

## Hard rules

- **Never commit `raw/` or `text/`** — they hold copyrighted content under mixed licenses. Only the
  registry (`sources.yaml`), provenance (`manifest.jsonl`), loader (`build_corpus.py`), and docs are
  tracked.
- Respect each source's `license`: `public-domain` (US gov) / `cc-by-sa` (Wikipedia — attribute) /
  `open` (arXiv — per-paper) / `proprietary-internal` (vendor/standards — pointers only, do NOT
  redistribute the bytes).
- Grow the corpus by editing `sources.yaml`; prefer openly-licensed sources.
