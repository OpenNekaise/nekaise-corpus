---
name: go
description: One-command entrypoint for the building-energy corpus — load everything indexed in sources.yaml onto this machine, then (once fully caught up) offer to enable the daily growth cron. Use on a fresh clone or whenever the user just says "go", "start", or "get the data".
---

# go

Canonical instructions: **[`skills/go.md`](../../../skills/go.md)** — read and follow that file
(single source of truth; Codex reads it via `AGENTS.md`).

In short: run `python build_corpus.py` to fetch every missing source from `sources.yaml` into
`raw/` + `text/`, verify (ok vs failed by topic, spot-check text quality). If the loader reports
`0 to fetch` (the corpus is fully materialized, like the maintainer's machine), OFFER to enable the
daily growth job (`bash scripts/install_cron.sh`) — a crontab entry that runs the [`dig`](../../../skills/dig.md)
loop once a day (≤3h), grows the registry, and **commits locally but never pushes.** Never commit
`raw/` or `text/`.
