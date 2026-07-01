# AGENTS.md — nekaise-corpus

**Mission:** find *all* the open **built-environment / AEC** knowledge on the internet — architecture,
engineering & construction, plus civil infrastructure, structures, geotechnical, building materials,
building-energy/HVAC, transportation, water, fire, and urban systems — and make it reproducibly
fetchable for LLM training & evaluation. (Started as a building-energy corpus; scope widened to the
whole built environment in round 7.) This repo is the **curation + the machinery + the provenance** —
it never holds the data bytes. You, a coding agent (Claude Code / Codex), are the **operator**: you
run the loop that fetches the seed corpus and grows it.

## The data model

| Thing | What it is |
|---|---|
| `sources.yaml` | The **registry** — one entry per source (`id` · `title` · `url` · `source` · `license` · `topic` · `format`). Edit this to grow the corpus. |
| `build_corpus.py` | The **loader** — reads the registry → downloads into `raw/<source>/` → extracts plain text into `text/<id>.md` → records sha256 + metadata in `manifest.jsonl`. Idempotent; dedups by sha256. |
| `raw/` + `text/` | Your local copy of the bytes / extracted text. **Git-ignored. Never committed.** |
| `manifest.jsonl` | The **provenance + reproducibility record** — url, license, topic, sha256, bytes for every fetched doc. |

## The operating loop

Run in a network-enabled shell (outside any sandbox). Each step has a skill that drives it.

**Just cloned? Say `go`.** [`skills/go.md`](skills/go.md) is the one-command entrypoint: it loads
everything indexed (below), and — once the machine is fully caught up — offers to enable the **daily
growth cron**.

1. **load** — [`skills/load-corpus.md`](skills/load-corpus.md): `python build_corpus.py` → fetch /
   refresh from the registry, then **verify** (ok vs failed by topic, investigate every 404,
   spot-check `text/*.md` quality, optionally re-hash against the manifest).
2. **find** — [`skills/find-sources.md`](skills/find-sources.md): `python find_sources.py` →
   discover new open-access papers/reports (OpenAlex / OSTI / arXiv). `python find_github.py` →
   discover README / `docs/*.md` / `*.rst` from a curated list of permissive building-sim GitHub
   repos (Modelica Buildings, EnergyPlus, OpenStudio, ResStock, …). You judge relevance + license
   and keep the good ones.
3. **crawl** — [`skills/crawl-docs.md`](skills/crawl-docs.md): `python crawl_docs.py` → add a
   multi-page documentation site (software / ontology docs that aren't a single PDF).
4. **prune** — `python prune_corpus.py --apply` → drop thin / garbage / non-English / off-topic
   discovered & crawled docs (hand-curated sources are left alone).

Then re-load and repeat. **The mission is coverage** — keep *widening* discovery (new backends, new
source types, deeper enumeration of known collections), not just re-fetching the popular head.

**Grow on autopilot.** [`skills/dig.md`](skills/dig.md) runs one full growth round (find_sources +
find_github + web-search a new vein → append → load → prune → **commit locally, never push**).
`bash scripts/install_cron.sh` wires it to a daily crontab entry (≤3h, only when the machine is on);
new sources land as local commits for you to review + push. Remove with
`bash scripts/install_cron.sh --remove`.

## Hard rules

- **Never commit `raw/` or `text/`** — copyrighted content under mixed licenses. Only the registry,
  manifest, code, and docs are tracked.
- **Respect each source's `license`:** `public-domain` (US gov) · `cc-by` / `cc-by-sa` (attribute) ·
  `open` (arXiv / OA — check per-source terms) · `proprietary-internal` (vendor / standards —
  **pointers only, never add the bytes**).
- **Prefer openly-licensed sources.** Grow the corpus by editing `sources.yaml`; high-value paywalled
  items go in as pointers only.
- **Report failures, never hide them.** A 404 = fix or drop the entry; never leave a known-dead URL
  silently failing in the registry.

## Topics

Building-energy vein: `controls_bas` · `equipment_systems` · `building_energy` · `commissioning_fdd` ·
`standards_protocols`

Built-environment / AEC vein (added round 7): `structures_civil` · `construction` · `materials` ·
`architecture` · `infrastructure` · `urban`

Topics are just a **radar label** for coverage — they don't gate anything except `coverage.py`. The
real relevance gate is `prune_corpus.py`'s `DOMAIN` regex (widened in round 7 to AEC/built-env
vocabulary). `find_github.py` can also pull **source code** (not just docs) from a repo via an opt-in
`code: [ext]` + `cap` on its `REPOS` entry — used for Modelica `.mo` physics models and pedagogical
structural/FEA `.py`.

> The skills are also exposed to Claude Code as **pointer-stubs** in [`.claude/skills/`](.claude/skills/);
> the files in [`skills/`](skills/) are the single source of truth.
