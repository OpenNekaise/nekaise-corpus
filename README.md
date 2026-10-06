# nekaise-corpus

An [OpenNekaise](https://github.com/OpenNekaise) corpus of multilingual architecture, engineering,
and construction knowledge for LLM training and evaluation. Sources include papers, technical
reports, books, patents, product literature, and software documentation.

This repository contains the source registry, provenance, and tools. Documents are downloaded
locally and retain their original licenses.

## Corpus

<!-- STATS:START -->
| | |
|---|---|
| **Documents** | **1,627,755** |
| **Outside the default view** | **16,547** rows (collected and kept): policy-held 8,059 · restricted licence classes 8,488 |
| **Collection (all use classes)** | **1,644,302** held originals · by class: open 1,627,755 · policy-held 8,059 · arxiv-nonexclusive 5,377 · unverified 2,076 · nc-nd 607 · nc 318 · publisher-oa 110 |
| **Raw originals** | **~779G** on disk, every class (PDF / HTML / source code) |
| **Extracted text** | **~83G** on disk, every class (default view: ~80.314B chars, **≈20.079B tokens**) |
| **Cleaned corpus** | **~78G** (~76.357B chars, **≈19.089B tokens**, ruleset-cleaned) |
| **Topics** | 12 |

**By topic** (a source gets one at registration): equipment_systems 534,414 · construction 431,289 · building_energy 219,832 · structures_civil 171,721 · materials 111,604 · infrastructure 75,375 · architecture 45,105 · standards_protocols 12,981 · controls_bas 10,528 · simulation_modeling 8,468 · urban 6,025 · commissioning_fdd 413.

**By license:** open 1,329,954 · public-domain 263,817 · cc-by-sa 9,900 · cc-by 24,026 · cc0 58.
<!-- STATS:END -->

## Quick start

```bash
git clone --depth 1 https://github.com/OpenNekaise/nekaise-corpus.git
cd nekaise-corpus
```

Open the repository in Claude Code or Codex and say **“go”**, or run it yourself:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt pytest
python scripts/run_round.py --skip-discovery --commit
```

The runner fetches the registered sources, gates document quality, builds `corpus/`, and verifies
the result before committing provenance locally. Budget disk space for all three stages shown above.

## How it works

```text
Discover → Fetch → Quality gate → Clean → Verify → Commit
```

`registry/` records sources and discovery progress; `manifest/` records URLs, licenses, hashes,
and processing results. Local files move through three stages:

| Directory | Contents |
|---|---|
| `raw/` | Original downloads |
| `text/` | Verbatim extraction with provenance |
| `corpus/` | Training text from eligible manifest entries |

All three are git-ignored. Cleaning reuses the selected ruleset; a fresh checkout defaults to
pass-through. Policy-excluded sources stay out of `corpus/`.

To discover more sources and run another verified round:

```bash
python scripts/run_round.py --commit
```

For scheduling, recovery, and curation, see [AGENTS.md](AGENTS.md).

## Contribute

Add sources to [`registry/curated.yaml`](registry/curated.yaml), improve a discovery backend,
or help with quality checks. Prefer public-domain and clearly licensed material. Pull requests
are welcome.

## License

Code, registry, and manifest: [MIT](LICENSE). Referenced documents retain their own terms;
`open` means available to fetch, not unrestricted reuse. Document bytes are never published here.
