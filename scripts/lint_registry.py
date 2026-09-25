#!/usr/bin/env python3
"""lint_registry.py — sanity-check the registry shards + manifest before publishing.

Catches the failure modes that have actually bitten this repo: duplicate ids (truncated slugs
silently overwriting each other's files), manifest rows orphaned from the registry (a bad prune
rewrite), entries sitting in the wrong shard (routing drift), unknown license/topic/format values,
and non-http urls. Run by CI on every push/PR; run it locally any time with:

    python scripts/lint_registry.py
"""
from __future__ import annotations

import sys
import re
from collections import Counter
from pathlib import Path


import registry
import store

REQUIRED = registry.REQUIRED_FIELDS
LICENSES = {"public-domain", "cc-by", "cc-by-sa", "cc0", "open", "proprietary-internal"}
TOPICS = {"controls_bas", "equipment_systems", "building_energy", "commissioning_fdd",
          "standards_protocols", "structures_civil", "construction", "materials",
          "architecture", "infrastructure", "urban"}
FORMATS = {"pdf", "html", "md", "rst", "txt", "tex", "troff"}
# Sources whose every entry must carry verified rights evidence for its selected copy (no `open`
# fallback): the scholarly-metadata families resolved by scripts/oa_resolution.py.
RIGHTS_EVIDENCE_SOURCES = {"openalex_sim", "openalex_ai"}
EVIDENCED_LICENSES = {"cc-by", "cc-by-sa", "cc0", "public-domain"}


def entry_errors(e: dict, where: str) -> list[str]:
    """Field checks for one registry entry; `where` prefixes each message (its shard file)."""
    errors = []
    eid = e.get("id", "<no id>")
    for k in REQUIRED:
        if not e.get(k):
            errors.append(f"{where}: {eid}: missing field '{k}'")
    if e.get("license") not in LICENSES:
        errors.append(f"{where}: {eid}: unknown license '{e.get('license')}'")
    if e.get("topic") not in TOPICS:
        errors.append(f"{where}: {eid}: unknown topic '{e.get('topic')}'")
    if e.get("format") not in FORMATS:
        errors.append(f"{where}: {eid}: unknown format '{e.get('format')}'")
    if not str(e.get("url", "")).startswith(("http://", "https://")):
        errors.append(f"{where}: {eid}: url is not http(s): {e.get('url')}")
    if e.get("license_url") and not str(e["license_url"]).startswith(("http://", "https://")):
        errors.append(f"{where}: {eid}: license_url is not http(s)")
    if e.get("source") in RIGHTS_EVIDENCE_SOURCES:
        if e.get("license") not in EVIDENCED_LICENSES:
            errors.append(f"{where}: {eid}: {e.get('source')} requires an evidenced open licence, "
                          f"got {e.get('license')!r}")
        for key in ("license_evidence", "rights_verified_at", "persistent_id"):
            if not e.get(key):
                errors.append(f"{where}: {eid}: {e.get('source')} entry lacks {key}")
    return errors


def manifest_errors(r: dict, entry: dict | None) -> list[str]:
    """Checks for one manifest row against its registry entry (None = orphaned)."""
    errors = []
    sid = r.get("id")
    if r.get("license") in registry.POINTER_ONLY_LICENSES:
        errors.append(f"manifest {sid}: pointer-only license {r.get('license')!r} has a payload row")
    if entry is None:
        errors.append(f"manifest row orphaned from registry: {sid}")
        return errors
    for key in ("url", "source", "license", "topic", "format"):
        if r.get(key) != entry.get(key):
            errors.append(f"manifest/registry drift {sid}.{key}: {r.get(key)!r} != {entry.get(key)!r}")
    for key in ("sha256", "text_sha256", "corpus_sha256"):
        value = r.get(key)
        if value and not re.fullmatch(r"[0-9a-f]{64}", value):
            errors.append(f"manifest {sid}: invalid {key}")
    return errors


def pages(view, table):
    cursor = None
    while True:
        page = view.scan(table, cursor=cursor, limit=store.MAX_PAGE)
        yield page.rows
        if page.next_cursor is None:
            return
        cursor = page.next_cursor


def main(root: Path | None = None) -> int:
    root = Path(root) if root is not None else registry.ROOT
    st = store.open(root=root)
    # Physical layout first (unparsable shards, routing drift, duplicate ids): keyed store reads
    # cannot see these, and a duplicate id would make any logical check ambiguous.
    errors, facts = st.validate_layout()
    n_entries, n_rows = facts["entries"], 0
    if not errors:
        restriction_hits: Counter = Counter()
        with st.read(timeout=60) as view:
            try:  # the eligibility policy pinned with the rows it is checked against
                restrictions, _ = store.pinned_policy(view)
            except store.StoreError as exc:
                print(f"LINT: {exc}")
                return 1
            for batch in pages(view, store.Table.ENTRIES):
                for e in batch:
                    errors.extend(entry_errors(e, registry.shard_filename(e["id"])))
                    if restricted := registry.restriction_for(e, restrictions):
                        restriction_hits[restricted[0]] += 1
            for name in restrictions:
                if not restriction_hits[name]:
                    errors.append(f"eligibility restriction {name!r} matches no registry entries")
            for batch in pages(view, store.Table.MANIFEST):  # joined to entries a page at a time
                entries = view.get_entries(r["id"] for r in batch)
                for r in batch:
                    n_rows += 1
                    errors.extend(manifest_errors(r, entries.get(r.get("id"))))

    if errors:
        for e in errors[:50]:
            print(f"LINT: {e}")
        if len(errors) > 50:
            print(f"LINT: ... and {len(errors) - 50} more")
        print(f"\nFAIL — {len(errors)} problem(s) in {n_entries} entries / {n_rows} manifest rows")
        return 1
    print(f"OK — {n_entries} registry entries in {facts['shards']} shards, "
          f"{n_rows} manifest rows, no problems")
    return 0


if __name__ == "__main__":
    sys.exit(main())
