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
# Every supported licence tag (registry.LICENSE_CLASSES); any of them may describe a payload row
# (collect regardless of licence: the tag decides the use view, never whether bytes are held).
LICENSES = set(registry.KNOWN_LICENSES)
# Rights-evidence rule (OFF by default; --require-rights-evidence). Once the licence audit
# (scripts/audit_licence_evidence.py apply) has given every arXiv/OpenAlex row its evidence, this
# keeps new rows from arriving as bare `open`: each such entry and manifest row must carry
# license_evidence and rights_verified_at, and resolve to a concrete licence (never `open`).
# Scope: machine-discovered arXiv/OpenAlex ids and the hand-curated arxiv-* ids; the audit's
# `enumerate` inventories the whole scope (incl. registry-only and failed rows) before enabling.
EVIDENCE_PREFIXES = ("arx-", "ope-", "oa-", "arxiv-")
TOPICS = {"controls_bas", "equipment_systems", "building_energy", "commissioning_fdd",
          "standards_protocols", "structures_civil", "construction", "materials",
          "architecture", "infrastructure", "urban", "simulation_modeling"}
FORMATS = {"pdf", "html", "md", "rst", "txt", "tex", "troff"}
# Scholarly families require copy-specific evidence (no generic `open` fallback). Restricted
# and unverified copies are collectable and use the same segregated views as other sources.
RIGHTS_EVIDENCE_SOURCES = {"openalex_sim", "openalex_ai"}
EVIDENCED_LICENSES = LICENSES - {"open", "proprietary-internal", "unverified"}


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
        evidence = str(e.get("license_evidence", ""))
        redirect_unverified = (
            evidence.startswith("redirect:")
            and re.search(r"; payload sha256=[0-9a-f]{64}; original evidence: ", evidence)
            is not None)
        copy_unverified = bool(e.get("selected_version")) and re.search(
            re.escape(f"[copy {e.get('url')}; version {e.get('selected_version')}; rights ")
            + r"(?:unknown|conflict)\]$", evidence) is not None
        unverified = e.get("license") == "unverified" and (redirect_unverified or copy_unverified)
        if e.get("license") not in EVIDENCED_LICENSES and not unverified:
            errors.append(f"{where}: {eid}: {e.get('source')} requires an evidenced copy classification, "
                          f"got {e.get('license')!r}")
        for key in ("license_evidence", "rights_verified_at", "persistent_id"):
            if not e.get(key):
                errors.append(f"{where}: {eid}: {e.get('source')} entry lacks {key}")
    return errors


def rights_evidence_errors(row: dict, where: str) -> list[str]:
    """The opt-in rights-evidence rule for one arXiv/OpenAlex entry or manifest row."""
    sid = str(row.get("id", ""))
    if not sid.startswith(EVIDENCE_PREFIXES):
        return []
    errors = []
    for key in ("license_evidence", "rights_verified_at"):
        if not row.get(key):
            errors.append(f"{where}: {sid}: no {key} (rights evidence required)")
    if row.get("license") == "open":
        errors.append(f"{where}: {sid}: license 'open' is not a verified licence")
    return errors


def manifest_errors(r: dict, entry: dict | None) -> list[str]:
    """Checks for one manifest row against its registry entry (None = orphaned)."""
    errors = []
    sid = r.get("id")
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


def changed_lint(view, restrictions: dict | None = None, *,
                 require_rights_evidence: bool = False) -> tuple[list[str], int, int]:
    """The lint checks over what a staged run changed (PostgreSQL authority, a gate over the
    frozen state): every changed registry entry, and the manifest row of every id changed in
    either table against its entry (both directions: an entry removed under a surviving
    manifest row is an orphan); and every eligibility restriction a changed entry matched
    before the run must still match some entry (the full lint's "matches no registry entries",
    for the restrictions the run could have emptied). Bounded by the run's revisions. Returns
    (errors, entries checked, manifest rows checked)."""
    import verify_generation
    scope = verify_generation.scope_of(view)
    if restrictions is None:   # the policy pinned with the frozen state
        restrictions, _ = store.pinned_policy(view)
    errors: list[str] = []
    n_entries = n_rows = 0
    touched: set[str] = set()
    for tbl in ("entries", "manifest"):
        for ids in verify_generation.changed_keys(view, scope, tbl):
            entries = view.get_entries(ids)
            manifest = view.get_manifest(ids)
            if tbl == "entries":
                for e in entries.values():
                    n_entries += 1
                    errors.extend(entry_errors(e, registry.shard_filename(e["id"])))
                    if require_rights_evidence:
                        errors.extend(rights_evidence_errors(e, registry.shard_filename(e["id"])))
                before = verify_generation._rows(
                    view, verify_generation._parent_src(view, scope, "entries"), ids)
                for e in before.values():
                    if hit := registry.restriction_for(e, restrictions):
                        touched.add(hit[0])
            for sid, r in manifest.items():
                n_rows += 1
                errors.extend(manifest_errors(r, entries.get(sid)))
                if require_rights_evidence:
                    errors.extend(rights_evidence_errors(r, "manifest"))
    for name in sorted(touched):
        if not view.scan(store.Table.ENTRIES, where=store.restriction_where(
                {name: restrictions[name]}), fields=("id",), limit=1).rows:
            errors.append(f"eligibility restriction {name!r} matches no registry entries")
    return errors, n_entries, n_rows


def main(root: Path | None = None, *, full: bool = False,
         require_rights_evidence: bool = False) -> int:
    root = Path(root) if root is not None else registry.ROOT
    st = store.open(root=root)
    # Physical layout first (unparsable shards, routing drift, duplicate ids): keyed store reads
    # cannot see these, and a duplicate id would make any logical check ambiguous.
    errors, facts = st.validate_layout()
    n_entries, n_rows = facts["entries"], 0
    import staged_runs
    if not errors and staged_runs.staged_authority(st):
        with st.read(timeout=60) as view:
            try:  # the eligibility policy pinned with the rows it is checked against
                restrictions, _ = store.pinned_policy(view)
            except store.StoreError as exc:
                print(f"LINT: {exc}")
                return 1
            if getattr(view, "stage", None) is not None and not full:
                import verify_generation
                scope = verify_generation.scope_of(view)
                if not (scope.config_changed or scope.parent is None):
                    # a run under unchanged policy: its changes; the whole generation is the
                    # integrity sweep's (scripts/integrity_sweep.py metadata)
                    errors, n_e, n_rows = changed_lint(
                        view, restrictions, require_rights_evidence=require_rights_evidence)
                    return _report(errors, f"{n_e} changed registry entries, {n_rows} changed "
                                           "manifest rows")
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
                    if require_rights_evidence:
                        errors.extend(rights_evidence_errors(e, registry.shard_filename(e["id"])))
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
                    if require_rights_evidence:
                        errors.extend(rights_evidence_errors(r, "manifest"))

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


def _report(errors: list[str], what: str) -> int:
    if errors:
        for e in errors[:50]:
            print(f"LINT: {e}")
        if len(errors) > 50:
            print(f"LINT: ... and {len(errors) - 50} more")
        print(f"\nFAIL — {len(errors)} problem(s) in {what}")
        return 1
    print(f"OK — {what}, no problems")
    return 0


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--root", default=None, help="the data root (default: this checkout)")
    ap.add_argument("--full", action="store_true",
                    help="PostgreSQL authority: lint the whole state even inside a run's gate")
    ap.add_argument("--require-rights-evidence", action="store_true",
                    help="also require license_evidence + rights_verified_at and a concrete "
                         "licence on arXiv/OpenAlex rows (arx-/ope-/oa-); off by default until "
                         "the licence-audit migration is applied")
    cli = ap.parse_args()
    sys.exit(main(cli.root, full=cli.full, require_rights_evidence=cli.require_rights_evidence))
