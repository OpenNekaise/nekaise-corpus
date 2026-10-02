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
