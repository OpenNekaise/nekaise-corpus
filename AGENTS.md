# AGENTS.md — nekaise-corpus

**Mission:** find *all* the open **built-environment / AEC** knowledge on the internet — architecture,
engineering & construction, plus civil infrastructure, structures, geotechnical, building materials,
building-energy/HVAC, transportation, water, fire, and urban systems — **in ANY language** (the
quality gate's DOMAIN vocabulary covers zh/ja/ko/de/fr/es/pt/it/nl/nordic/ru alongside English) —
and make it reproducibly fetchable for LLM training & evaluation. This repo is the **curation + the
machinery + the provenance** — it never holds the data bytes. You, a coding agent (Claude Code /
Codex), are the **operator**: you run the loop that fetches the seed corpus and grows it. The loop's
excavation state (which page/bucket each backend mines next) is COMMITTED in
`registry/rotation.json` — read/advance it via `scripts/rotation.py`, so any operator on any
machine resumes exactly where the last one stopped.

## Repo layout

| Path | What it is |
|---|---|
| `registry/` | The **registry** — one entry per source (`id` · `title` · `url` · `source` · `license` · `topic` · `format`), sharded per vein: `curated.yaml` (hand-picked — edit this to grow) + machine shards (`books` · `papers` · `reports` · `github` · `archive` · `crawl`), routed by id prefix (`scripts/registry.py`). |
| `manifest/` | The **provenance + reproducibility record** — url, license, topic, sha256, bytes for every fetched doc. Sharded like the registry (`manifest/<shard>.jsonl`, patents split by country, heavy countries split again into hash buckets like `patents-cn-0…7`) so no file nears GitHub's 100MB push limit; all I/O via `registry.py` (`load_manifest_rows` / `write_manifest_rows`). |
| `pruned_urls.txt` | **Blocklist** of URLs the quality gate dropped — finders dedup against it so discovery never re-churns pruned material. |
| `registry/rotation.json` | **Excavation state** — the next page/offset/bucket per backend, advanced by `scripts/rotation.py` after each successful run. Committed, so the growth loop is resumable by anyone. |
| `registry/backends.json` | **Control-plane config** — finder script, fixed arguments, enabled/paused state. `run_round.py` validates it against `rotation.json`, so new backends cannot silently miss automation. |
| `registry/pruned.jsonl` | **Decision provenance** for future prunes — id/url/reason/metrics/run id. `pruned_urls.txt` remains the fast compatibility blocklist. |
| `scripts/` | The **machinery** — loader, discovery backends, quality gate, cron/marathon runners. All run from the repo root: `python scripts/<x>.py`. |
| `.claude/skills/` | The **skills** — step-by-step playbooks for each loop (`go` · `load-corpus` · `find-sources` · `crawl-docs` · `clean-corpus` · `dig`). Claude Code picks them up natively; Codex: read the `SKILL.md` files directly. |
| `workspace/` | **Your scratch space** (git-ignored). One-off helper scripts, notes, dumps go here — never the repo root. Promote durable tools into `scripts/`. |
| `raw/` · `text/` · `corpus/` | Your local copy, in three stages: original bytes → verbatim extraction → **cleaned, training-ready text**. **All git-ignored. Never committed.** See *The three stages* below. |
| `logs/` | Headless dig/marathon run logs (git-ignored). |

`workspace/corpus-index.sqlite3` is a git-ignored, automatically invalidated acceleration index
over registry + manifest + blocklist. It is never authoritative and can always be rebuilt with
`python scripts/corpus_index.py rebuild`.

**Keep the root clean.** The root holds docs + the registry + the manifest, nothing else. New
durable code goes in `scripts/`; experiments go in `workspace/`.

## The three stages

Your local copy is a pipeline, not one directory. Each stage is derived from the one before it and
all three are git-ignored:

| Stage | Written by | What it holds | Why it exists |
|---|---|---|---|
| `raw/<source>/<id>.<ext>` | `build_corpus.py` | Original downloaded bytes, unmodified | The **reproducibility anchor** — the manifest's `sha256` is over these bytes, so a later re-fetch is provably `reproduced` / `DRIFTED` / `new`. Also the dedup key. |
| `text/<id>.md` | `build_corpus.py` | **Verbatim** extraction + provenance header | The re-clean substrate. Never edited in place. |
| `corpus/<id>.md` | `clean_corpus.py` | **Cleaned, training-ready** text | What a training run reads. |

**Why `text/` and `corpus/` are separate rather than one cleaned directory:** a cleaning ruleset is
never right the first time. Re-running an improved cleaner over `corpus/` reads `text/` and takes
minutes; folding cleaning into extraction would mean re-parsing 104k PDFs (CPU-hours) for every rule
tweak and would make `raw/` permanently undeletable. The extra ~11GB buys cheap iteration.

`corpus/` is built **from the manifest**, never from a directory listing, so it can only contain docs
that have a provenance row — a training run over `corpus/*` cannot pick up unprovenanced text, and
ids whose `text_path` drifted from their id get canonical `corpus/<id>.md` names automatically.

## The machinery

`scripts/build_corpus.py` is the **loader**: reads the registry → downloads into `raw/<source>/` →
extracts plain text into `text/<id>.md` → records sha256 + metadata in the manifest. Idempotent;
dedups by sha256; fairly interleaves hosts (`--workers`, conservative host-specific caps) and runs
extraction in a separate process pool (`--extract-workers`), so parsing never holds a network slot.
PDF downloads are magic-byte checked, and a curl fallback rides over WAF/TLS-fingerprint walls
(403/429/503). The discovery
backends (`find_sources.py` OpenAlex/OSTI/arXiv · `find_github.py` curated repos + source code ·
`find_osti.py` deep OSTI · `find_books.py` OAPEN books, all languages · `find_archive.py` pre-1929
public-domain texts (Internet Archive) · `find_openaire.py` EU project deliverables · `find_nist.py`
NIST/NBS via Crossref · `find_zenodo.py` CC-licensed records · `find_patents.py` US patents via the
Google Patents sitemap (the biggest open vein) · `find_wiki.py` multilingual Wikipedia ·
`find_scielo.py` SciELO Brazil's CC-BY AEC journals (the biggest Portuguese built-environment
vein) · `find_ibpsa.py` (paused — see rotation.json) · `crawl_docs.py` doc sites) propose registry entries;
`prune_corpus.py --apply` is the quality gate (logic in `scripts/quality.py`, golden-tested in
`tests/`). URLs the pruner drops land in `pruned_urls.txt` (committed) and every finder skips them —
rounds never re-churn pruned material.

`scripts/clean_corpus.py` is the **cleaning stage**: `text/` → `corpus/`. The pruner is a
*document-level* gate (keep or drop a whole doc); the cleaner works *within* a document, stripping
PDF artefacts that survive any doc-level test — running headers, contents dot-leaders, bare page
numbers, OCR punctuation debris, patent identifier blocks, Modelica diagram geometry. Measured over
the **full 103,931-doc corpus**: all rules together remove **966.9M of 13,089.4M chars (7.39%)**,
`repeated_boilerplate` alone accounting for 529.4M.

Which rules run is **opt-in and currently `none`** — the default is a faithful pass-through, so
`corpus/` is complete and byte-identical to `text/` until a ruleset is chosen. Cleaning *policy* is a
separate decision from this machinery.

```
python scripts/clean_corpus.py --list-rules          # what's available
python scripts/clean_corpus.py --report --sample 40  # measure each rule, write nothing
python scripts/clean_corpus.py --check               # verify corpus/ agrees with the manifest
python scripts/clean_corpus.py --rules all           # apply everything
python scripts/clean_corpus.py --rules toc_leaders,page_markers
```

Timings on a 40-core box: pass-through rebuild **6s**, full ruleset **46s** (11 min CPU), `--check`
**14s**. Process-parallel, not thread-parallel — the rules are regex-bound and a thread pool pins at
~1 core.

**Operational hazards, both hit while building this:**

- **Interrupting a run leaves cleaned files whose mtime beats their `text/` source**, which the
  incremental check would accept as up-to-date. Guarded: the stamp is written `IN-PROGRESS <ruleset>`
  *before* any file is touched and replaced with the real ruleset only after the manifest is written,
  so any interruption forces a full rebuild. Never hand-edit `corpus/.ruleset`.
- **`kill <pid>` on a run orphans its 16 worker processes**, which keep writing to `corpus/` after the
  parent is gone. Use `pkill -9 -f clean_corpus.py`, then re-run — and if `corpus/` and the manifest
  ever disagree, `--check` names the drift and `--force` repairs it.

Every rule is **structural** (repetition- or shape-based), never a letters-per-character threshold.
That is deliberate: an alpha-fraction rule reads real Japanese prose interleaved with figures
(`測定は 2019 年 3 月 14（暖房期）に`) as number soup — the trap `quality.py` already hit once
(`MIN_ALPHA_CJK`). Structural rules are script-agnostic by construction, and `tests/test_clean.py`
pins both directions: what each rule must drop, and the CJK prose / numeric data tables / Modelica
equations it must never touch. **Numeric tables are content, not noise** — `Asphalt workers 2.81
(1.11-7.13)` and `Concrete C25/30 25 30 2400 31` are real AEC knowledge and no rule may eat them.

## The operating loop

Run in a network-enabled shell (outside any sandbox). Each step has a skill that drives it.

**Canonical automation:** `python scripts/run_round.py --commit`. It holds a repo-level advisory
lock and fail-closes the entire required pipeline:

```
discover → fetch → prune → clean → check → README stats → index → lint → tests → commit
```

Cron and marathon call this same runner. A required step failure never commits, pushes, or advances
rotation pointers. Finders run concurrently against one immutable registry view and stage isolated
proposal files; only after every finder succeeds does the runner deduplicate, merge, and advance
pointers serially. Normal failures roll tracked state back; state files are replaced atomically.
Local run events live in `logs/run_history.jsonl`. A hard kill leaves a durable pre-round snapshot
and the next operator restores it explicitly with
`python scripts/run_round.py --recover latest`.

**Cloning: use `git clone --depth 1`.** The full history carries every past manifest/registry
revision (~1.7GB); the recipe never needs it to operate — a shallow clone is ~10× smaller and
works with every loop below (only deep `git log` archaeology needs `--unshallow`).

**Just cloned? Say `go`.** [`go`](.claude/skills/go/SKILL.md) is the one-command entrypoint: it
loads everything indexed (below), and — once the machine is fully caught up — offers to enable the
**daily growth cron**.

1. **load** — [`load-corpus`](.claude/skills/load-corpus/SKILL.md): `python scripts/build_corpus.py`
   → fetch / refresh from the registry, then **verify** (ok vs failed by topic, investigate every
   404, spot-check `text/*.md` quality, optionally re-hash against the manifest).
2. **find** — [`find-sources`](.claude/skills/find-sources/SKILL.md): `python scripts/find_sources.py`
   → discover new open-access papers/reports (OpenAlex / OSTI / arXiv). `python scripts/find_github.py`
   → discover README / `docs/*.md` / `*.rst` from a curated list of permissive building-sim GitHub
   repos (Modelica Buildings, EnergyPlus, OpenStudio, ResStock, …). You judge relevance + license
   and keep the good ones.
3. **crawl** — [`crawl-docs`](.claude/skills/crawl-docs/SKILL.md): `python scripts/crawl_docs.py` →
   add a multi-page documentation site (software / ontology docs that aren't a single PDF).
4. **prune** — `python scripts/prune_corpus.py --apply` → drop thin / garbage / non-English /
   off-topic discovered & crawled docs (hand-curated sources are left alone). *Document-level.*
5. **clean** — [`clean-corpus`](.claude/skills/clean-corpus/SKILL.md): `python scripts/clean_corpus.py`
   → build `corpus/` from `text/`, then `--check`. *Within-document.* Run it after every prune so
   `corpus/` mirrors the manifest; it is incremental (only changed docs are rewritten) unless the
   ruleset changed, in which case it rebuilds everything.

Then re-load and repeat. **The mission is the loop itself** — an autonomous grower *and curator*.
Keep *widening* discovery (new backends, new source types, deeper enumeration of known collections)
and keep *sharpening* curation (the gate, the cleaner). Being the biggest and the cleanest AEC
corpus is the by-product; the deliverable is a loop that gets there without a human in it.

**Grow on autopilot.** [`dig`](.claude/skills/dig/SKILL.md) runs one full growth round (find_sources +
find_github + web-search a new vein → append → load → prune → **commit locally, never push**).
`bash scripts/install_cron.sh` wires it to a daily crontab entry (≤3h, only when the machine is on);
new sources land as local commits for you to review + push. Remove with
`bash scripts/install_cron.sh --remove`.

## Hard rules

- **Never commit `raw/`, `text/`, or `corpus/`** — copyrighted content under mixed licenses. Only the
  registry, manifest, code, and docs are tracked.
- **`text/` is verbatim — never clean it in place.** Cleaning writes `corpus/`. Editing `text/` throws
  away the ability to re-clean, and `raw/` is the only way back.
- **Respect each source's `license`:** `public-domain` (US gov) · `cc-by` / `cc-by-sa` (attribute) ·
  `open` (arXiv / OA — check per-source terms) · `proprietary-internal` (vendor / standards —
  **pointers only, never add the bytes**).
- **Prefer openly-licensed sources.** Grow the corpus by editing `registry/curated.yaml`; high-value paywalled
  items go in as pointers only.
- **Report failures, never hide them.** A 404 = fix or drop the entry; never leave a known-dead URL
  silently failing in the registry.

## Topics

Building-energy vein: `controls_bas` · `equipment_systems` · `building_energy` · `commissioning_fdd` ·
`standards_protocols`

Built-environment / AEC vein (added round 7): `structures_civil` · `construction` · `materials` ·
`architecture` · `infrastructure` · `urban`

Topics are just a **radar label** for coverage — they don't gate anything except `scripts/coverage.py`.
The real relevance gate is the `DOMAIN` regex in `scripts/quality.py` (widened in round 7 to AEC/built-env
vocabulary). `find_github.py` can also pull **source code** (not just docs) from a repo via an opt-in
`code: [ext]` + `cap` on its `REPOS` entry — used for Modelica `.mo` physics models and pedagogical
structural/FEA `.py`.
