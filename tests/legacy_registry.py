"""The pre-step-7 file implementations of registry.py's list/set API and of the blocklist and
rotation readers (scripts/registry.py at edfb838ba5), frozen as the REFERENCE for equivalence
tests. Production code reaches tracked state only through scripts/store.py
(tests/test_architecture.py); registry's names of the same functions are now deprecated store
adapters, so tests that compare the store against the legacy bytes use this module instead.

Paths follow registry.ROOT at call time (tests point it at a throwaway repository)."""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

import ops
import registry
from state_codec import (emit_entry, manifest_shard, manifest_shard_text, norm, normalize_url,
                         parse_yaml, prune_ledger_name, remove_ids_from_text, shard_filename,
                         shard_header, CURATED)


def reg_dir() -> Path:
    return Path(registry.ROOT) / "registry"


def man_dir() -> Path:
    return Path(registry.ROOT) / "manifest"


def blocklist_path() -> Path:
    return Path(registry.ROOT) / "pruned_urls.txt"


def shard_path(sid: str) -> Path:
    return reg_dir() / shard_filename(sid)


def shard_files() -> list[Path]:
    cur = reg_dir() / CURATED
    rest = sorted(p for p in reg_dir().glob("*.yaml") if p.name != CURATED)
    return ([cur] if cur.exists() else []) + rest


def load_entries() -> list[dict]:
    """Every registry entry, curated shard first."""
    out: list[dict] = []
    for p in shard_files():
        out.extend(parse_yaml(p.read_text()).get("sources") or [])
    return out


def manifest_files() -> list[Path]:
    return sorted(man_dir().glob("*.jsonl")) if man_dir().exists() else []


def load_manifest_rows() -> list[dict]:
    out: list[dict] = []
    for p in manifest_files():
        out.extend(json.loads(l) for l in p.read_text().splitlines() if l.strip())
    return out


def write_manifest_rows(rows) -> None:
    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(manifest_shard(r["id"]), []).append(r)
    man_dir().mkdir(exist_ok=True)
    live = {f"{stem}.jsonl" for stem in groups}
    for stem, group in groups.items():
        text = manifest_shard_text(group)
        p = man_dir() / f"{stem}.jsonl"
        if not p.exists() or p.read_text() != text:
            ops.atomic_write_text(p, text)
    for p in manifest_files():
        if p.name not in live:
            p.unlink()


def prune_ledger_path(sid: str) -> Path:
    return reg_dir() / prune_ledger_name(sid)


def prune_ledger_files() -> list[Path]:
    paths = []
    for path in reg_dir().glob("pruned-*.jsonl"):
        match = re.fullmatch(r"pruned-(\d+)\.jsonl", path.name)
        if match:
            paths.append((int(match.group(1)), path))
    return [path for _, path in sorted(paths)]


def load_prune_ledger_rows() -> list[dict]:
    legacy = reg_dir() / "pruned.jsonl"
    paths = [legacy] if legacy.exists() else prune_ledger_files()
    out: list[dict] = []
    for path in paths:
        out.extend(json.loads(line) for line in path.read_text().splitlines() if line.strip())
    return out


def write_prune_ledger_rows(rows) -> dict[str, int]:
    groups: dict[Path, list[dict]] = {}
    for row in rows:
        groups.setdefault(prune_ledger_path(row.get("id")), []).append(row)
    reg_dir().mkdir(exist_ok=True)
    live = set(groups)
    for path, group in sorted(groups.items()):
        text = "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n" for row in group)
        ops.atomic_write_text(path, text)
    for path in prune_ledger_files():
        if path not in live:
            path.unlink()
    legacy = reg_dir() / "pruned.jsonl"
    if legacy.exists():
        legacy.unlink()
    return {path.name: len(group) for path, group in sorted(groups.items())}


def blocklist_load() -> set[str]:
    path = blocklist_path()
    if not path.exists():
        return set()
    return {normalize_url(l) for l in path.read_text().splitlines() if l.strip()}


def blocklist_add(urls) -> int:
    """The legacy blocklist writer: append new URLs (deduped, sorted) to pruned_urls.txt."""
    new = sorted({normalize_url(u) for u in urls if u and normalize_url(u)} - blocklist_load())
    if new:
        path = blocklist_path()
        old = path.read_text() if path.exists() else ""
        if old and not old.endswith("\n"):
            old += "\n"
        ops.atomic_write_text(path, old + "".join(f"{u}\n" for u in new))
    return len(new)


def rotation_load() -> dict:
    path = reg_dir() / "rotation.json"
    return json.loads(path.read_text()) if path.exists() else {}


def existing_keys(include_blocklist: bool = True):
    if include_blocklist and os.environ.get("NEKAISE_DISABLE_INDEX") != "1":
        try:
            import corpus_index
            return corpus_index.existing_keys(reg_dir(), man_dir(), blocklist_path())
        except Exception as exc:
            if os.environ.get("NEKAISE_INDEX_DEBUG") == "1":
                print(f"index fallback: {exc}", file=sys.stderr)
    urls, titles, ids = set(), set(), set()
    for r in load_manifest_rows():
        urls.add(normalize_url(r.get("url") or ""))
        titles.add(norm(r.get("title")))
        ids.add(r.get("id") or "")
    for e in load_entries():
        urls.add(normalize_url(e.get("url") or ""))
        titles.add(norm(e.get("title")))
        ids.add(e.get("id") or "")
    if include_blocklist:
        urls |= blocklist_load()
    return urls, titles, ids


def append_entries(entries: list[dict]) -> dict[str, int]:
    if proposal_name := os.environ.get(registry.PROPOSAL_ENV):
        return registry.append_entries(entries)  # the proposal protocol is unchanged
    groups: dict[Path, list[dict]] = {}
    for e in entries:
        groups.setdefault(shard_path(e["id"]), []).append(e)
    reg_dir().mkdir(exist_ok=True)
    counts: dict[str, int] = {}
    for path, group in sorted(groups.items()):
        before = len(parse_yaml(path.read_text()).get("sources") or []) if path.exists() else 0
        block = "".join(emit_entry(e) for e in group)
        if path.exists():
            ops.atomic_write_text(path, path.read_text() + block)
        else:
            ops.atomic_write_text(path, shard_header(path.stem) + block)
        after = parse_yaml(path.read_text()).get("sources") or []
        if len(after) != before + len(group):
            raise RuntimeError(f"append corrupted {path.name}: {before}+{len(group)} != {len(after)}")
        counts[path.name] = len(group)
    return counts


def remove_ids(drop: set) -> int:
    removed_total = 0
    for path in shard_files():
        new_text, removed = remove_ids_from_text(path.read_text(), drop, path.name)
        if not removed:
            continue
        ops.atomic_write_text(path, new_text)
        removed_total += removed
    return removed_total
