# workspace/ — agent scratch space

This is **your** (the coding agent's) sandbox. Write one-off helper scripts, analysis notebooks,
intermediate dumps, experiment notes — anything that helps you operate the corpus — **here**, not in
the repo root.

Rules:

- Everything in this directory except this README is **git-ignored**. Nothing here is published.
- Keep the repo root clean: if a script proves durable and generally useful, promote it into
  [`scripts/`](../scripts/) (tracked) and reference it from the skills / AGENTS.md.
- Don't put downloaded corpus bytes here — those belong in `raw/` + `text/` (also git-ignored),
  managed by the loader.
