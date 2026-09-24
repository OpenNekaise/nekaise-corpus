#!/usr/bin/env python3
"""registry.py — registry vocabulary, policy helpers, the finder proposal protocol, and the
DEPRECATED list/set adapters over the store (ADR 0001 stage 3, step 7).

Tracked state (registry shards, manifest, prune ledger, blocklist, control documents, journal) is
read and written through scripts/store.py only. This module keeps:

* the layout vocabulary every caller shares, re-exported from scripts/state_codec.py (routing,
  normalization, YAML/manifest shard text, eligibility schema) — pure functions, no I/O;
* git-owned policy loaders (eligibility, host policy; located by store.config_path);
* the finder proposal protocol: append_entries() stages a finder's entries (and find_github's
  passes) in NEKAISE_PROPOSAL_FILE during a round's discovery phase, which run_round merges in
  its discovery transaction;
* deprecated compatibility adapters with the old list/set API — load_entries, load_manifest_rows,
  write_manifest_rows, existing_keys, remove_ids, load_prune_ledger_rows, and append_entries
  outside proposal mode — each one store view or one store transaction (store_broker.run_batch:
  through the broker of the round or maintenance window it runs in, else under its own writer).
  No production caller uses the read/rewrite adapters (tests/test_architecture.py); they exist
  for ad-hoc operator scripts and go away with the stage-4 cutover.
"""
from __future__ import annotations

import json
import os
import warnings
from pathlib import Path

import ops
import store
from state_codec import (  # noqa: F401 — the shared vocabulary, re-exported for callers
    CORPUS_FIELDS, CURATED, DISCOVERED_PREFIXES, ENTRY_RE, FIELDS, HASH_BUCKETS, OPTIONAL_FIELDS,
    POINTER_ONLY_LICENSES, PRUNE_LEDGER_BUCKETS, REQUIRED_FIELDS, SHARDS, discovered, emit_entry,
    is_training_eligible, manifest_shard, manifest_shard_text, norm, parse_yaml,
    prune_ledger_name, remove_ids_from_text, restriction_for, shard_filename, shard_header, slug,
    uniquify_ids, validate_eligibility,
)

ROOT = Path(__file__).resolve().parents[1]  # repo root (this file lives in scripts/)
# One-shot registration tools that an eligibility restriction may name in `backends` although
# they have no registry/backends.json entry (they never run in rounds). crawl_docs refuses to
# register pages whose source a restriction covers.
MANUAL_TOOLS = frozenset({"crawl_docs"})
# How long a deprecated adapter waits for a running round (its own writer or read lock).
LOCK_TIMEOUT = 30.0


# Eligibility restrictions and host fetch policy are read ONLY from a store view's pinned
# configuration — store.pinned_policy(view): validated, failing closed, and consistent with the
# data the same view serves (tests/test_architecture.py). Restrictions are an overlay, not a
# destructive registry rewrite: provenance stays intact while policy-blocked material is kept
# out of fetches and the training-ready corpus. This module keeps the pure policy functions.


def suspended_unavailable(row: dict, policy: dict[str, dict], root: Path | None = None) -> bool:
    """A successful row on a fetch-SUSPENDED host whose extracted text is not on this machine.

    Its provenance stays committed and it stays TRAINING-ELIGIBLE (a fetch suspension is not an
    exclusion; committed statistics count it on every machine), but the loader may not re-fetch
    it (e.g. on a fresh clone), so it is "locally unavailable, suspended": the cleaner does not
    expect it in corpus/, local-availability reports list it, and it may never claim a title or
    bytes over an available copy.
    """
    if row.get("status") != "ok" or not policy:
        return False
    url = row.get("url") or ""
    if not any(host in url for host in policy):  # cheap pre-filter over 1.6M rows
        return False
    import host_policy

    if not host_policy.suspended(url, policy):
        return False
    text_path = row.get("text_path")
    return not text_path or not ((root or ROOT) / text_path).exists()


def locally_unavailable_rows(rows: list[dict], policy: dict[str, dict],
                             root: Path | None = None) -> list[dict]:
    """Successful suspended-host rows whose payload is missing on THIS machine (a local
    availability report; never an input to committed statistics). `policy`: the view-pinned
    host policy."""
    return [row for row in rows if suspended_unavailable(row, policy, root)]


def partition_manifest_ok_rows(
    rows: list[dict], restrictions: dict[str, dict]
) -> tuple[list[dict], list[dict]]:
    """Split successful provenance rows into training-eligible and excluded records.

    Purely manifest-based and therefore identical on every machine: local file availability
    (see locally_unavailable_rows) never changes these counts.
    """
    eligible: list[dict] = []
    excluded: list[dict] = []
    for row in rows:
        if row.get("status") != "ok":
            continue
        target = eligible if is_training_eligible(row, restrictions) else excluded
        target.append(row)
    return eligible, excluded


def is_fetchable(entry: dict, restrictions: dict[str, dict]) -> bool:
    """Compatibility name for the loader's license + eligibility decision (`restrictions`: the
    view-pinned eligibility policy)."""
    return is_training_eligible(entry, restrictions)


PROPOSAL_ENV = "NEKAISE_PROPOSAL_FILE"


def read_proposal(path: Path) -> dict:
    """A finder's staged proposal: {"entries": [...]} plus, when staged, "github_passes"
    ({bucket: {doc kind: date}}, see stage_github_passes). The file is a plain JSON list of
    entries when nothing else was staged, the format every earlier round wrote."""
    data = json.loads(path.read_text()) if path.exists() else []
    if isinstance(data, list):
        return {"entries": data}
    if not isinstance(data, dict) or not isinstance(data.get("entries"), list):
        raise ValueError(f"{path}: not a finder proposal")
    if unknown := set(data) - {"entries", "github_passes"}:
        raise ValueError(f"{path}: unknown proposal section(s) {sorted(unknown)}")
    return data


def _write_proposal(path: Path, doc: dict) -> None:
    payload = doc["entries"] if set(doc) == {"entries"} else doc
    ops.atomic_write_text(path, json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")


def merge_github_passes(current: dict, records: dict) -> dict:
    """find_github's completed doc-markup passes plus `records`; a recorded pass keeps its
    first date. Both are {bucket: {doc kind: ISO date}}."""
    merged = {bucket: dict(kinds) for bucket, kinds in (current or {}).items()}
    for bucket, kinds in records.items():
        if not isinstance(kinds, dict) or not all(
                isinstance(k, str) and isinstance(v, str) for k, v in kinds.items()):
            raise ValueError(f"malformed github pass record for {bucket!r}")
        for kind, day in kinds.items():
            merged.setdefault(bucket, {}).setdefault(kind, day)
    return merged


def stage_github_passes(records: dict) -> bool:
    """In proposal mode (a round's discovery phase), stage find_github's completed passes next to
    its proposed entries, so run_round records them only if the finder succeeded, and return
    True. Outside proposal mode return False: the caller writes them itself."""
    if not (proposal_name := os.environ.get(PROPOSAL_ENV)):
        return False
    proposal = Path(proposal_name)
    doc = read_proposal(proposal)
    staged = merge_github_passes(doc.get("github_passes") or {}, records)
    if staged:
        doc["github_passes"] = staged
    _write_proposal(proposal, doc)
    return True


def append_entries(entries: list[dict]) -> dict[str, int]:
    """A finder's output. During a round's discovery phase run_round sets NEKAISE_PROPOSAL_FILE
    separately for each finder: entries are then atomically staged there as JSON and the round
    merges every successful proposal in its discovery transaction (the protocol; not deprecated).

    Outside proposal mode (a standalone `--append`) this is a deprecated adapter: ONE store
    transaction inserting the entries (store insert_entries: routed, validated, appended exactly
    as the legacy writer did; an id that already exists is refused). Returns {shard filename:
    appended}."""
    if proposal_name := os.environ.get(PROPOSAL_ENV):
        proposal = Path(proposal_name)
        doc = read_proposal(proposal)
        doc["entries"].extend(entries)
        _write_proposal(proposal, doc)
        return {"proposal.json": len(entries)}
    entries = [dict(e) for e in entries]
    if entries:
        _batch("append", lambda view, batch: batch.insert_entries(entries))
    counts: dict[str, int] = {}
    for e in entries:
        name = shard_filename(e["id"])
        counts[name] = counts.get(name, 0) + 1
    return dict(sorted(counts.items()))


# --- deprecated list/set adapters over the store ---------------------------------------------------

def _deprecated(name: str, instead: str) -> None:
    warnings.warn(f"registry.{name} is a deprecated adapter over the store; use {instead}",
                  DeprecationWarning, stacklevel=3)


def _store():
    return store.open(root=ROOT)


def _batch(step: str, body):
    import store_broker  # store_broker imports store only; kept lazy for finder start-up time

    return store_broker.run_batch(_store(), step, body, timeout=LOCK_TIMEOUT)


def _scan_all(table: "store.Table", *, order: str = "key") -> list[dict]:
    with _store().read(timeout=LOCK_TIMEOUT) as view:
        out, cursor = [], None
        while True:
            page = view.scan(table, cursor=cursor, limit=store.MAX_PAGE, order=order)
            out.extend(page.rows)
            if (cursor := page.next_cursor) is None:
                return out


def load_entries() -> list[dict]:
    """DEPRECATED: every registry entry, in id order (the store keeps no registry file order;
    the legacy reader returned curated.yaml first, then shard files by name)."""
    _deprecated("load_entries", "a store view's scan(Table.ENTRIES)")
    return _scan_all(store.Table.ENTRIES)


def load_manifest_rows() -> list[dict]:
    """DEPRECATED: every manifest row in the legacy order (shard files by name, (topic, id)
    within one) — scan(Table.MANIFEST, order="legacy")."""
    _deprecated("load_manifest_rows", "a store view's scan(Table.MANIFEST, order='legacy')")
    return _scan_all(store.Table.MANIFEST, order="legacy")


def write_manifest_rows(rows, *, reason: str = "write_manifest_rows (deprecated adapter)") -> None:
    """DEPRECATED: `rows` becomes the WHOLE manifest — replacement semantics, never an upsert
    (ADR 0001 section 7, stage 3): one store transaction replace_manifest(rows, reason=reason);
    rows left out are deleted with tombstones carrying `reason`."""
    _deprecated("write_manifest_rows", "upsert_manifest / update_manifest_fields / "
                "delete_manifest (or replace_manifest) in a store transaction")
    rows = [dict(r) for r in rows]
    _batch("manifest", lambda view, batch: batch.replace_manifest(rows, reason=reason))


def remove_ids(drop: set, *, reason: str = "remove_ids (deprecated adapter)") -> int:
    """DEPRECATED: delete registry entries by id (one store transaction delete_entries; the file
    store cuts their blocks in place, every other byte kept). Returns how many existed."""
    _deprecated("remove_ids", "delete_entries in a store transaction")
    ids = sorted(i for i in drop if isinstance(i, str) and i)
    if not ids:
        return 0
    _, results = _batch("remove", lambda view, batch: batch.delete_entries(ids, reason=reason))
    return results[0] if results else 0


def load_prune_ledger_rows() -> list[dict]:
    """DEPRECATED: every prune decision (the store's ledger table in its scan order — canonical
    JSON, not file order)."""
    _deprecated("load_prune_ledger_rows", "a store view's scan(Table.LEDGER)")
    return _scan_all(store.Table.LEDGER)


def existing_keys(include_blocklist: bool = True):
    """DEPRECATED: (urls, titles, ids) already known to the corpus, as whole in-memory sets —
    normalized URLs and titles of every manifest row and registry entry, plus the pruned-URL
    blocklist by default. Finders ask scripts/dedup.py (batched store known()) instead."""
    _deprecated("existing_keys", "scripts/dedup.py (store known())")
    urls, titles, ids = set(), set(), set()
    with _store().read(timeout=LOCK_TIMEOUT) as view:
        for table in (store.Table.MANIFEST, store.Table.ENTRIES):
            cursor = None
            while True:
                page = view.scan(table, fields=("id", "url", "title"), cursor=cursor,
                                 limit=store.MAX_PAGE)
                for r in page.rows:
                    urls.add(store.norm_url(r.get("url")))
                    titles.add(norm(r.get("title")))
                    ids.add(r.get("id") or "")
                if (cursor := page.next_cursor) is None:
                    break
        if include_blocklist:
            cursor = None
            while True:
                page = view.scan(store.Table.BLOCKLIST, cursor=cursor, limit=store.MAX_PAGE)
                urls.update(r["url"] for r in page.rows)
                if (cursor := page.next_cursor) is None:
                    break
    return urls, titles, ids
