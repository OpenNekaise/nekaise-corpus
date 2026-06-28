# nekaise-corpus

A curated, license-tagged, **reproducible recipe** for assembling a building / HVAC /
**building-energy** corpus for training and evaluating LLMs in the building-energy domain.

## At a glance

| | |
|---|---|
| **Documents** | **613** |
| **Raw originals** | **~1.7 GB** (PDF / HTML) |
| **Extracted text** | **~59 MB** (~59.1M chars, **≈14.8M tokens**) |
| **Topics** | 5 |

**By genre:** research papers (arXiv / OSTI / OpenAlex) 321 · reference manuals, standards & gov guides — PDF (EnergyPlus, UFC, Title 24, 90.1 PRM, NIST, FEMP, IEA EBC) 23 · crawled doc-site pages (Brick / Haystack / ASHRAE 223P / VOLTTRON / Modelica) 64 · encyclopedic + seed (Wikipedia / gov) 205. _Multi-genre on purpose — the 23 reference PDFs are ~28% of the tokens; the crawled ontology / software docs fill out coverage that academic papers miss._

**By topic:** controls_bas 237 · building_energy 103 · equipment_systems 101 · standards_protocols 94 · commissioning_fdd 78

**By source:** OSTI 184 · arXiv 170 · Wikipedia 97 · crawled doc sites (VOLTTRON / Haystack / Brick / open223 / Modelica) 64 · OpenAlex 40 · PNNL 17 · LBNL 15 · NIST 4 · EnergyPlus / IEA EBC / GSA / CEC / etc.

**By license:** open 270 · public-domain (US gov) 228 · cc-by-sa 97 · cc-by 13 · proprietary-internal 5

_Snapshot of the current registry (2026-06-28). The bytes are not shipped — these are what you get
after running the loader. The corpus grows as sources are added to `sources.yaml`._

> This repo ships the **registry + loader + provenance**, NOT the data bytes. The corpus mixes
> licenses (US-gov public-domain, CC-BY-SA, arXiv, and some non-redistributable vendor/standards
> material), so we cannot and do not host the files. You fetch your own copy with the loader and
> respect each source's license. (This is how RedPajama / The Pile-style corpora work.)

## What's here

| File | What it is |
|---|---|
| `sources.yaml` | The curated registry — each source's URL, topic, license, format. **Edit this to grow the corpus.** |
| `build_corpus.py` | The loader — downloads sources into `raw/`, extracts plain text into `text/`, dedups by sha256, writes the manifest. |
| `find_sources.py` | Discovery — queries OpenAlex / OSTI / arXiv for open-access sources (download-friendly hosts only) and proposes registry entries. |
| `crawl_docs.py` | Discovery — BFS-crawls a doc site (sphinx / readthedocs / mkdocs) and registers its pages, so multi-page references (not single PDFs) can be loaded. |
| `prune_corpus.py` | Quality gate — drops thin / garbage / non-English / off-topic discovered & crawled docs. |
| `manifest.jsonl` | Provenance — id, url, license, topic, sha256, bytes for every fetched doc. |
| `skills/` | The **skills** an AI agent runs: `load-corpus` (fetch + verify), `find-sources` (discover papers/reports), `crawl-docs` (add doc sites). Mirrored to `.claude/skills/`. |
| `raw/`, `text/` | **Git-ignored.** Your local copy of the bytes / extracted text. Never committed. |

## Use it

Easiest: ask your AI coding agent (Claude Code / Codex) to **"load the building-energy corpus"** —
the [`load-corpus`](skills/load-corpus.md) skill drives the download and verifies it. Or run it
yourself:

```bash
pip install -r requirements.txt
python build_corpus.py            # fetch missing sources (needs network)
python build_corpus.py --force    # re-fetch everything
python build_corpus.py --only controls_bas
```

## Reproducibility

A clone gets the **same corpus** we have. `manifest.jsonl` is the canonical record -- every doc's
`url` and **`sha256`**. When you run the loader it compares each download to the committed manifest
and reports `reproduced (sha256 match) / drifted (source changed upstream) / new`. To check an
already-downloaded copy without re-fetching:

```bash
python build_corpus.py --verify   # re-hash local raw/ files against the manifest sha256
```

- **Raw bytes** are the strong guarantee: sha256 is version-independent, so fetching the same URL
  yields a byte-identical file (or the run flags drift).
- **Extracted text** (`text/`) is derived via pypdf / beautifulsoup4 -- pin those
  (`requirements.txt`) for byte-identical text too.
- `sources.yaml` and `manifest.jsonl` are kept in sync; stable hosts (arXiv, `*.gov`) reproduce
  reliably, and any dead or changed source is reported, never silently dropped.

## Topics

`controls_bas` · `equipment_systems` · `building_energy` · `commissioning_fdd` · `standards_protocols`

(~550 sources; ~58 MB extracted text / ~1.7 GB raw originals when fully fetched.)

## Licensing — read before you redistribute

Every source carries a `license` in `sources.yaml` / `manifest.jsonl`:

- **`public-domain`** — US government / national-lab reports (DOE, PNNL, LBNL, OSTI). Free to use.
- **`cc-by-sa`** — Wikipedia. Attribution + share-alike.
- **`open`** — arXiv papers. Check each paper's individual license; many are NOT freely
  redistributable.
- **`proprietary-internal`** — copyrighted vendor pages / standards (e.g. ASHRAE). Listed here as
  pointers for your own access only; **do NOT redistribute the bytes.**

`raw/` and `text/` are git-ignored for exactly this reason. What this project publishes is the
registry and manifest (pointers + metadata — our curation), plus the loader.

## Contributing

Add a source: append an entry to `sources.yaml` and open a PR. Prefer openly-licensed material
(public-domain gov reports, CC, arXiv). Keep copyrighted material `proprietary-internal` and never
add its bytes.

## License

The code, registry, and manifest in this repo are MIT. The referenced source documents retain their
own licenses (see above). Part of the [OpenNekaise](https://github.com/OpenNekaise) ecosystem;
consumed by [nekaise-studio](https://github.com/OpenNekaise/nekaise-studio) as domain-ceiling
material.
