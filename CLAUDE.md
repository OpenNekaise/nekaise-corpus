# CLAUDE.md

Single source of truth for working in this repo: **[`AGENTS.md`](AGENTS.md)** — read and follow it.

In one line: this is an **agent-operable recipe** for a built-environment LLM corpus that *grows and
curates itself* — it ships the **registry + loader + cleaner + provenance, never the data bytes**.
You are the operator. The loop is **load → find → crawl → prune → clean → repeat**, driven by the
skills in [`.claude/skills/`](.claude/skills/). Machinery lives in [`scripts/`](scripts/); your scratch
space is [`workspace/`](workspace/) (git-ignored) — keep the repo root clean. Never commit `raw/`,
`text/`, or `corpus/`; respect each source's `license`.

The local copy has **three stages**: `raw/` (original bytes, the sha256 anchor) → `text/` (**verbatim**
extraction — never clean it in place) → `corpus/` (cleaned, training-ready; built by
`scripts/clean_corpus.py`). Being the biggest and cleanest AEC corpus is the **by-product**; the
deliverable is a loop that gets there autonomously.
