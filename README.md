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
| **Documents** | **1,543,152** |
| **Policy-excluded provenance** | **8,039** rows (not fetched or training-ready) |
| **Raw originals** | **~729G** (PDF / HTML / source code) |
| **Extracted text** | **~79G** (~77.183B chars, **≈19.296B tokens**) |
| **Cleaned corpus** | **~75G** (~73.387B chars, **≈18.347B tokens**, ruleset-cleaned) |
| **Topics** | 11 |

**By topic** (a source gets one at registration): equipment_systems 512,069 · construction 410,638 · building_energy 204,909 · structures_civil 164,460 · materials 106,639 · infrastructure 71,878 · architecture 43,110 · standards_protocols 11,560 · controls_bas 11,040 · urban 6,014 · commissioning_fdd 835.

**By license:** open 1,264,133 · public-domain 258,303 · cc-by-sa 1,735 · cc-by 18,981.
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
