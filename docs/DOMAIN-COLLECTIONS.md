# Domain collections

The operator authorized corpus-owned Modelica and energy/physical-modeling collections
on 2026-10-02. A collection groups independently useful material without moving a
document out of other domains or duplicating its bytes per domain. Studio training
remains paused; this publication does not activate a teaching curriculum.

## Ownership and layout

- `registry/domains/*.json`: versioned definitions and bounded source/discovery recipe.
- `workspace/domains/_store/artifacts/{raw,text}/…`: shared immutable payload versions.
- `workspace/domains/_imports/<digest>/`: verified source inventories and evidence.
- `workspace/domains/<domain>/snapshots/<digest>/`: frozen members, source aliases,
  definition, browse links and accounting.
- `workspace/domains/<domain>/latest.json`: atomic publication pointer; `current/`
  is a rebuildable convenience link to the snapshot.

`modelica` covers the Modelica ecosystem and its physical-modeling foundations.
`energy-physical-modeling` also includes EnergyPlus, IDA ICE, other building-energy
tools, related physics, numerical methods, controls and calibration. Membership may
overlap. It does not change the publisher's single source-level topic labels.

This is an explicit **local domain raw-source dataset**, separate from global default
`corpus/` admission. Importing a bundle does not insert global manifest rows, reclassify
source use, run the production cleaner, or advance discovery/training cursors. Complete
Modelica annotations and original code are preserved. The existing global pipeline
continues to use its own store and policies. Source declarations remain provenance;
an operator acquisition policy is not evidence of an open license.

The collection layer reuses `artifact_store.LocalArtifacts` durability in its own
root. Global artifact GC does not scan this root and cannot mistake domain-only
references for garbage. Inventories and snapshots are permanent reference roots
until an explicit dependency-aware retirement is implemented. Include the entire
`workspace/domains/` tree in payload backups; it is durable data, not disposable
scratch. A backup of global manifests alone does not back up these collections.

## Import, freeze, browse and export

```bash
python scripts/domain_collections.py import-bundle /path/to/verified-preparation
python scripts/domain_collections.py build modelica \
  workspace/domains/_imports/IMPORT_DIGEST
python scripts/domain_collections.py verify \
  workspace/domains/modelica/snapshots/SNAPSHOT_DIGEST
python scripts/domain_collections.py export \
  workspace/domains/modelica/snapshots/SNAPSHOT_DIGEST \
  --role core --output workspace/modelica-core.jsonl
```

Imports require a verified readiness descriptor and recheck every declared export,
body hash and payload hash before publishing an inventory. Original repository
archives, PDF bytes, HTTP bodies/receipts and publisher-manifest evidence are copied
to corpus-owned storage. Existing-corpus documents preserve the original publisher's
raw hash and pinned manifest: those already-held originals remain owned by the
publisher. The collection's prepared text version is independently pinned. Active
member references have no dependency on Studio's preparation directories.

A failed import/build does not advance the publication pointer; unreferenced pending
files may remain for inspection. An existing inventory/snapshot is verified before
reuse. No original preparation bundle is removed automatically. The domain lock
serializes collection publications, without holding the global writer lock for long
payload copies. Changes to the shared store/config code are deployed only between
normal rounds under the maintenance locks.

The frozen snapshot ID hashes the definition, ordered inventory identities and
builder/dedup contract. An inventory ID pins its preparation descriptor; its receipt
pins the imported document catalog. Each member carries a stable source document ID,
content hash, relative immutable locator and source provenance. Aliases retain
alternate occurrences of identical normalized bodies. File, library and version
identity do not disappear during deduplication.

`browse/` is organized by role and source, with relative links to the shared text
objects. It can be rebuilt from the members; it is not the source of membership.
Use the snapshot ID for training, not a moving `current/` link. A future Studio
loader must verify that snapshot and report real consumed targets/exposure through
normal continuation paths. No such automatic training activation is performed here.

## Roles and token counts

The roles are `core`, `research_candidates`, `patent_supplement`, `reference_pdf`,
`reference_history` and `reference_assets`. Research/title selection does not certify
every document's scientific value. PDF extraction may lose equations, figures or
layout. Legacy decoding hypotheses and numerical weather/reference data are kept
separate from the primary core. Exact hashes do not remove namespace-renamed Modelica
families, translations or other semantic near-duplicates.

```bash
PATH_TO_TOKENIZERS_PYTHON scripts/domain_tokens.py \
  --root "$PWD" --snapshot workspace/domains/modelica/snapshots/SNAPSHOT_DIGEST \
  --tokenizer /path/to/Kai/tokenizer.json
```

By default accounting measures `core` and `reference_pdf`; `--roles` selects any of
the six roles explicitly. Uncounted roles are reported as uncounted, never zero tokens.
Accounting verifies bytes and uses the pinned native tokenizer on each full selected
text, without truncation or special tokens. It loads no model weights. Reports split
tokens by role/source and retain the tokenizer and membership hashes. The shared
token-count cache is only a local accelerator. Exact prepared text tokens are not
unique knowledge, training exposure, packed causal targets, or a learning result.
Raw PDF/archive bytes are never counted as text tokens.

## Coverage workflow

The source recipe starts from official Modelica/OpenModelica catalogs, known building
simulation sources and explicit operator keywords. Freeze the source list and commit
identities before counting coverage. `domain_archives.py` inspects pinned tar archives
without executing downloaded code, records every regular member's disposition,
extracts supported text/PDFs, and emits `uses(...)` and submodule discovery leads.

```bash
python scripts/domain_archives.py --plan workspace/pinned-archive-plan.json \
  --out workspace/domain-acquisition/expanded
```

Dependency leads exclude comments and quoted tutorials. They remain lexical evidence,
not proof of compiler compatibility or exact-version closure. Public clone/archive
acquisition is explicit and bounded; the inspector itself performs no network calls.
Binary payloads, LFS pointers, inaccessible sources, unsupported/oversized members,
encoding hypotheses and extraction errors remain in the inventory. A completed
archive inspection does not mean every member became readable text.

Track separately: declared-source acquisition; file extraction/dispositions;
dependency/version gaps; topic × artifact coverage; and new relevant sources found
per discovery wave. A finite frozen source list can be checked exhaustively. There
is no measured denominator for all internet knowledge. Commercial private source,
unavailable versions and unrendered formulas must never be called acquired. Ordinary
public access limits remain; private evaluation banks are not consulted.

Tests: `pytest tests/test_domain_collections.py tests/test_domain_archives.py
tests/test_architecture.py tests/test_artifact_store.py`.

## SSD operation (2026-11-07)

The active host stores domain payloads at `/media/zengp/ssd/nekaise-domains`.
`workspace/domains` is a compatibility symlink: old content addresses and frozen
snapshot identities remain valid. The host-local binding in
`workspace/domain-storage.json` pins the mount UUID. `domain_storage.py` refuses
reads/writes through the domain API if that volume is missing or replaced, and
new acquisition requires 128 GiB free for the other SSD workloads.
The repository's code and curation recipe remain in Git on the workstation.

The migration compares every file by checksum and every symlink, before switching
the path. The original directory is retained until source inventories, snapshots
and the independent backup have verified. Historical Studio preparation bundles
are retained under `_legacy/studio-domain-prep`, with their former path linked to
the SSD. Private simulation experiment runs are a separate project and are not
admitted into these collections.

`_custody/latest.json` records independent copies of the publisher originals used
by historical domain imports. Each original must match the historical SHA256;
missing versions and drift remain explicit. Original inventory/snapshot records
are never rewritten to pretend that a historical provenance gap did not exist.

### Continue collection

```bash
.venv/bin/python scripts/domain_acquire.py --repositories 6 --websites 6
```

This deterministic runner acquires the scheduled-growth and canonical round locks,
then the domain and accounting locks. It resumes its state under
`workspace/domains/_acquisition/`, fetches a bounded batch, imports verified
archives/pages, publishes all three domain views, materializes classified cleaned
views, and verifies an incremental backup on a second physical disk. It can run
while the global corpus's capacity hold remains in place; that hold is not removed.
No model calls, translation, source-code execution, or automatic training occurs.

The original recipe is `registry/domains/sources.json`; the controls/energy-rating
expansion is `registry/domains/expansion.json`. This adds IEC 61131 PLC code,
BACnet, Modbus, OPC UA, KNX, VOLTTRON, Brick/Haystack, OpenStudio ERI/HPXML and HEM.
All languages and rights classes are collected. New repository records retain the
archive and path-specific evidence but remain `unverified` until a rights audit;
a repository-level license guess never admits all nested files to the open view.
The archive inspector now recognizes PLC source formats, Ruby energy models,
Haystack definitions, RDF schemas, weather and simulation input files.

Each round also runs one paginated GitHub repository discovery query, rotating
across domain topics and language queries. Results retain the API payload and
selection reason; evaluation banks and unrelated/fork results are excluded.
Accepted leads join the next acquisition queue. GitHub versions are frozen at an
explicit commit; already imported versions are not repeatedly downloaded. Public
website queues resume within declared prefixes and page caps. HTTP hops use the
existing host suspension, robots, pacing, redirect, challenge and byte-limit
checks. A failed source receives a 24-hour cooldown and stays visible in state.
Catalog and dependency leads that lack a reviewed adapter remain explicit gaps.

### Cleaned views and recoverability

`<domain>/clean-current/<use-class>/<role>/` is the classified materialization.
The initial cleaning policy is faithful pass-through, with exact-body duplicates
already merged in the source snapshot and aliases retained. No new prose rule is
applied to source code, equations, multilingual text or numeric tables. The default
training selection is `open/core`; reference PDFs, numerical assets, research
candidates and other use classes need explicit selection. The global corpus's
cleaning rules and admissions are unchanged. Every view has a hashed membership
receipt and verified links to immutable text.

`domain_backup.py` mirrors the entire domain tree to the UUID-bound second device
configured in `workspace/domain-storage.json` (this host:
`/mnt/data1/nekaise-domain-backups/replica`). It does not delete old immutable
versions; replaced operational metadata goes into a dated history directory.
A checksum/link comparison precedes its successful receipt. The backup covers
raw artifacts, extracted text, all views, custody evidence, acquisition state,
logs and legacy preparation. The existing October SSD archives remain historical
backups, but sharing the active SSD makes them unsuitable as its only backup.

Inspect `_acquisition/latest.json`, `_acquisition/job-status.json`,
`_custody/latest.json` and `backup-status.json` for compact operational evidence.
`_acquisition/cron.log` holds detailed failures. A successful publication reports
its residual source/extraction failures; it does not assert complete internet
coverage, validated simulations, or that every retained byte is training-ready.

Install the authorized six-hour continuation with
`.venv/bin/python scripts/install_domain_cron.py` (remove with `--remove`).
It runs at 01:47, 07:47, 13:47 and 19:47 in the crontab's configured timezone,
uses a three-hour process-group timeout, and preserves all other cron entries.
Unpinned repository heads are rechecked weekly; immutable commit-pinned recipes
remain reproducible. Websites rotate by their last attempt, so one large manual
cannot indefinitely prevent the remaining languages from being sampled.

Automatically discovered repositories enter `research_candidates` pending source
relevance review; they never become core merely because a search result matched.
Curated sources retain their declared core/reference separation. Search-selection
rules and native-language queries are recorded separately from cleaning policy.
