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
| **Training tokens** | **≈20.257B** (cleaned, training-ready) |
| **Documents** | **1,663,959** |
| **Extracted text** | ≈21.261B tokens (~85.045B chars) |
| **Raw originals** | ~1.5T (PDF / HTML / source code) |
| **Topics** | 12 |

**By topic:** equipment_systems 536,835 · construction 441,455 · building_energy 220,526 · structures_civil 177,233 · materials 116,666 · infrastructure 77,455 · architecture 51,148 · standards_protocols 12,995 · controls_bas 10,666 · urban 10,093 · simulation_modeling 8,467 · commissioning_fdd 420.

_The default training view, auto-generated from the manifest. Another **121,007** rows (collected and kept) stay outside it: policy-held or restricted licences. The bytes are not shipped; run the loader to fetch your own copy._
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

For a capacity investigation that must outlive a maintenance window, write its reason and release
conditions to `workspace/.capacity-hold` while holding the canonical round lock. New rounds then
exit 75 without starting work; recovery and resume remain available. The maintainer does not clear
this operator hold automatically. Remove it under the lock only after checking its release
conditions. Independently of the marker, new rounds require free space strictly above the larger
of 4% of filesystem capacity and 40 GB. `NEKAISE_MIN_FREE_GB` overrides the absolute floor;
it cannot lower the 4% floor. Set it higher when assessed round peak storage plus 40 GB and
explicit background/backup reserves require more headroom. These are preflight checks, not a
reservation against other writers. Keep the operator hold while peak needs or reserves remain
unknown; clearing 4% alone does not establish safe operating capacity. Health thresholds are unchanged.

Rounds record `disk_usage` summaries in `logs/run_history.jsonl` for each command/finder/gate
and the whole locked round (including snapshots, index warmup and rollback). The runner samples
the repository filesystem at scope boundaries and every 10 seconds, recording start/end/minimum
free bytes, maximum used bytes, and `peak_drop_bytes` (start free minus minimum free, floored at
zero). Summaries also survive ordinary failures; sample errors mark them incomplete. These are
observed filesystem peaks: other writers contribute, overlapping step peaks must not be summed,
and short spikes between samples can be missed. Separate payload mounts need separate assessment.
Hard kills can lose the unfinished scope's summary. Sampling neither releases a hold nor changes
the floor; a supervised round and explicit reserves are still required before sustained growth.

Health monitoring reports cleaned-backup freshness separately from original-byte recovery:
the current backup scope excludes `raw/` and `text/`, so `original_backup` remains a warning
until their coverage and restore verification are established. The daily PostgreSQL shadow
comparison writes `workspace/pg-shadow-verify.json`; health reports failures and receipts older
than 26 hours (critical after 50 hours). A passing receipt covers its recorded commit watermark,
not subsequent commits. Nested domain configurations participate in both replication and comparison.

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
