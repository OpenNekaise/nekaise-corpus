# CLAUDE.md

Read [AGENTS.md](AGENTS.md): it is the single source of truth for the mission, operator contract,
permissions, pipeline and data policy. The deliverable is an automatic multilingual built-environment
data factory with reproducible provenance and verified training-ready output.

Routine growth: `python scripts/run_round.py --commit`.
Materialize the existing recipe: `python scripts/run_round.py --skip-discovery --commit`.
Both run the full required pipeline and validation; mechanical rounds commit locally, never push.
The separately authorized maintainer owns reviewed publication.

Read only the relevant playbooks in `.claude/skills/`. Durable code goes in `scripts/`, scratch work
in git-ignored `workspace/`. Never commit data bytes from `raw/`, `text/` or `corpus/`; never edit
verbatim `text/` in place. Honor the user's existing authorization without redundant questions.
