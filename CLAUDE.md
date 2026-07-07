# CLAUDE.md

Single source of truth for working in this repo: **[`AGENTS.md`](AGENTS.md)** — read and follow it.

In one line: this is an **agent-operable recipe** for assembling *and continuously growing* a
built-environment LLM corpus — it ships the **registry + loader + provenance, never the data bytes**.
You are the operator. The loop is **load → find → crawl → prune → repeat**, driven by the skills in
[`.claude/skills/`](.claude/skills/). Machinery lives in [`scripts/`](scripts/); your scratch space
is [`workspace/`](workspace/) (git-ignored) — keep the repo root clean. Never commit `raw/` or
`text/`; respect each source's `license`.
