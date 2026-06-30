# AGENTS.md — nekaise-corpus

**Mission:** find *all* the open building / HVAC / **building-energy** knowledge on the internet and
make it reproducibly fetchable for LLM training & evaluation. This repo is the **curation + the
machinery + the provenance** — it never holds the data bytes. You, a coding agent (Claude Code /
Codex), are the **operator**: you run the loop that fetches the seed corpus and grows it.

## The data model

| Thing | What it is |
|---|---|
| `sources.yaml` | The **registry** — one entry per source (`id` · `title` · `url` · `source` · `license` · `topic` · `format`). Edit this to grow the corpus. |
| `build_corpus.py` | The **loader** — reads the registry → downloads into `raw/<source>/` → extracts plain text into `text/<id>.md` → records sha256 + metadata in `manifest.jsonl`. Idempotent; dedups by sha256. |
| `raw/` + `text/` | Your local copy of the bytes / extracted text. **Git-ignored. Never committed.** |
| `manifest.jsonl` | The **provenance + reproducibility record** — url, license, topic, sha256, bytes for every fetched doc. |

## The operating loop

Run in a network-enabled shell (outside any sandbox). Each step has a skill that drives it:

1. **load** — [`skills/load-corpus.md`](skills/load-corpus.md): `python build_corpus.py` → fetch /
   refresh from the registry, then **verify** (ok vs failed by topic, investigate every 404,
   spot-check `text/*.md` quality, optionally re-hash against the manifest).
2. **find** — [`skills/find-sources.md`](skills/find-sources.md): `python find_sources.py` →
   discover new open-access sources (OpenAlex / OSTI / arXiv). You judge relevance + license and keep
   the good ones.
3. **crawl** — [`skills/crawl-docs.md`](skills/crawl-docs.md): `python crawl_docs.py` → add a
   multi-page documentation site (software / ontology docs that aren't a single PDF).
4. **prune** — `python prune_corpus.py --apply` → drop thin / garbage / non-English / off-topic
   discovered & crawled docs (hand-curated sources are left alone).

Then re-load and repeat. **The mission is coverage** — keep *widening* discovery (new backends, new
source types, deeper enumeration of known collections), not just re-fetching the popular head.

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

`controls_bas` · `equipment_systems` · `building_energy` · `commissioning_fdd` · `standards_protocols`

> The skills are also exposed to Claude Code as **pointer-stubs** in [`.claude/skills/`](.claude/skills/);
> the files in [`skills/`](skills/) are the single source of truth.
