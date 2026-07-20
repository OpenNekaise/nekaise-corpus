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
| `manifest/` | The **provenance + reproducibility record** — url, license, topic, sha256, bytes for every fetched doc. Sharded like the registry (`manifest/<shard>.jsonl`, patents split by country) so no file nears GitHub's 100MB push limit; all I/O via `registry.py` (`load_manifest_rows` / `write_manifest_rows`). |
| `pruned_urls.txt` | **Blocklist** of URLs the quality gate dropped — finders dedup against it so discovery never re-churns pruned material. |
| `registry/rotation.json` | **Excavation state** — the next page/offset/bucket per backend, advanced by `scripts/rotation.py` after each successful run. Committed, so the growth loop is resumable by anyone. |
| `scripts/` | The **machinery** — loader, discovery backends, quality gate, cron/marathon runners. All run from the repo root: `python scripts/<x>.py`. |
| `.claude/skills/` | The **skills** — step-by-step playbooks for each loop (`go` · `load-corpus` · `find-sources` · `crawl-docs` · `dig`). Claude Code picks them up natively; Codex: read the `SKILL.md` files directly. |
| `workspace/` | **Your scratch space** (git-ignored). One-off helper scripts, notes, dumps go here — never the repo root. Promote durable tools into `scripts/`. |
| `raw/` + `text/` | Your local copy of the bytes / extracted text. **Git-ignored. Never committed.** |
| `logs/` | Headless dig/marathon run logs (git-ignored). |

**Keep the root clean.** The root holds docs + the registry + the manifest, nothing else. New
durable code goes in `scripts/`; experiments go in `workspace/`.

## The machinery

`scripts/build_corpus.py` is the **loader**: reads the registry → downloads into `raw/<source>/` →
extracts plain text into `text/<id>.md` → records sha256 + metadata in the manifest. Idempotent;
dedups by sha256; parallel (`--workers`, ≤2 in-flight per host); PDF downloads are magic-byte
checked, and a curl fallback rides over WAF/TLS-fingerprint walls (403/429/503). The discovery
backends (`find_sources.py` OpenAlex/OSTI/arXiv · `find_github.py` curated repos + source code ·
`find_osti.py` deep OSTI · `find_books.py` OAPEN books, all languages · `find_archive.py` pre-1929
public-domain texts (Internet Archive) · `find_openaire.py` EU project deliverables · `find_nist.py`
NIST/NBS via Crossref · `find_zenodo.py` CC-licensed records · `find_patents.py` US patents via the
Google Patents sitemap (the biggest open vein) · `find_wiki.py` multilingual Wikipedia ·
`find_ibpsa.py` (paused — see rotation.json) · `crawl_docs.py` doc sites) propose registry entries;
`prune_corpus.py --apply` is the quality gate (logic in `scripts/quality.py`, golden-tested in
`tests/`). URLs the pruner drops land in `pruned_urls.txt` (committed) and every finder skips them —
rounds never re-churn pruned material.

## The operating loop

Run in a network-enabled shell (outside any sandbox). Each step has a skill that drives it.

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
   off-topic discovered & crawled docs (hand-curated sources are left alone).

Then re-load and repeat. **The mission is coverage** — keep *widening* discovery (new backends, new
source types, deeper enumeration of known collections), not just re-fetching the popular head.

**Grow on autopilot.** [`dig`](.claude/skills/dig/SKILL.md) runs one full growth round (find_sources +
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
