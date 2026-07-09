#!/usr/bin/env python3
"""registry.py — shared registry I/O for the loader, the pruner, and every discovery backend.

The registry is a directory of YAML shards (registry/*.yaml), each holding a `sources:` list of
entries (id · title · url · source · license · topic · format). Hand-curated entries live in
curated.yaml; every machine backend's output is routed to its own shard by id prefix (SHARDS).

This module is the single home for logic that used to be copy-pasted across five finders — and
bred the same id-truncation-collision bug three times: known-key dedup sets (existing_keys),
id uniquification (uniquify_ids), entry emission/appending (append_entries), and validated
in-place removal (remove_ids). A finder should only contain its query/API logic; everything that
touches the registry goes through here.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import yaml

import blocklist

ROOT = Path(__file__).resolve().parents[1]  # repo root (this file lives in scripts/)
REG_DIR = ROOT / "registry"
MANIFEST = ROOT / "manifest.jsonl"

CURATED = "curated.yaml"
# id prefix -> shard file. Anything not matching is hand-curated (curated.yaml).
SHARDS = {
    "oer-": "books.yaml",      # find_books (OAPEN CC-BY books)
    "arc-": "archive.yaml",    # find_archive (pre-1929 public-domain texts)
    "gh-": "github.yaml",      # find_github (repo docs + source code)
    "crawl-": "crawl.yaml",    # crawl_docs (doc-site pages)
    "ost-": "reports.yaml",    # find_osti + find_sources OSTI backend
    "eud-": "deliverables.yaml",  # find_openaire (EU Horizon/H2020 project deliverables)
    "nst-": "nist.yaml",       # find_nist (NIST/NBS technical series via Crossref DOI prefix)
    "pat-": "patents.yaml",    # find_patents (Google Patents sitemap, building/HVAC CPC classes)
    "ibp-": "ibpsa.yaml",      # find_ibpsa (Building Simulation proceedings)
    "zen-": "zenodo.yaml",     # find_zenodo (open CC-licensed records)
    "ope-": "papers.yaml",     # find_sources OpenAlex backend
    "oa-": "papers.yaml",
    "arx-": "papers.yaml",     # find_sources arXiv backend
}
DISCOVERED_PREFIXES = tuple(SHARDS)  # the pruner's gate: machine-discovered ids
FIELDS = ("id", "title", "url", "source", "license", "topic", "format")
ENTRY_RE = re.compile(r"^  - id:\s*['\"]?(.+?)['\"]?\s*$")
_FIELD_RE = re.compile(r"^    \s*\S")  # continuation lines of one entry


def _entry_span(lines: list[str], i: int) -> int:
    """lines[i] is an entry's `  - id:` line; return one past the entry's last line. Blank lines
    are part of the entry when more field lines follow — yaml allows blank lines INSIDE a quoted
    multi-line scalar (a real OSTI title bit us: the naive parser cut the entry in half)."""
    j = i + 1
    while j < len(lines):
        if _FIELD_RE.match(lines[j]):
            j += 1
        elif lines[j].strip() == "" and j + 1 < len(lines) and _FIELD_RE.match(lines[j + 1]):
            j += 1
        else:
            break
    return j


def discovered(sid: str) -> bool:
    return sid.startswith(DISCOVERED_PREFIXES)


def shard_path(sid: str) -> Path:
    for prefix, fname in SHARDS.items():
        if sid.startswith(prefix):
            return REG_DIR / fname
    return REG_DIR / CURATED


def shard_files() -> list[Path]:
    cur = REG_DIR / CURATED
    rest = sorted(p for p in REG_DIR.glob("*.yaml") if p.name != CURATED)
    return ([cur] if cur.exists() else []) + rest


def slug(s: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", (s or "").lower())).strip("-")


def norm(s: str) -> str:
    return re.sub(r"\W+", " ", (s or "").lower()).strip()


def load_entries() -> list[dict]:
    """Every registry entry, curated shard first."""
    out: list[dict] = []
    for p in shard_files():
        out.extend(yaml.safe_load(p.read_text()).get("sources") or [])
    return out


def load_manifest_rows() -> list[dict]:
    if not MANIFEST.exists():
        return []
    return [json.loads(l) for l in MANIFEST.read_text().splitlines() if l.strip()]


def existing_keys(include_blocklist: bool = True):
    """(urls, titles, ids) already known to the corpus — what every finder dedups against.
    urls includes the pruned-URL blocklist by default so discovery never re-churns."""
    urls, titles, ids = set(), set(), set()
    for r in load_manifest_rows():
        urls.add((r.get("url") or "").rstrip("/"))
        titles.add(norm(r.get("title")))
        ids.add(r.get("id") or "")
    for e in load_entries():
        urls.add((e.get("url") or "").rstrip("/"))
        titles.add(norm(e.get("title")))
        ids.add(e.get("id") or "")
    if include_blocklist:
        urls |= blocklist.load()
    return urls, titles, ids


def uniquify_ids(entries: list[dict], reserved: set) -> None:
    """Suffix -2/-3/… onto any id already in `reserved` (registry + manifest ids) or repeated
    within the batch. Mutates entries and grows `reserved`. Truncated title slugs WILL collide
    across runs — this is the guard that keeps a collision from silently overwriting a doc."""
    for h in entries:
        base, i = h["id"], 2
        while h["id"] in reserved:
            h["id"] = f"{base[:50]}-{i}"
            i += 1
        reserved.add(h["id"])


def emit_entry(e: dict) -> str:
    d = yaml.safe_dump([{k: e[k] for k in FIELDS}], sort_keys=False, allow_unicode=True)
    return "".join(("  " + ln + "\n") if ln else "\n" for ln in d.splitlines())


def append_entries(entries: list[dict]) -> dict[str, int]:
    """Route entries to their shards by id prefix and append, validating each shard afterwards
    (parses + count grew by exactly the group size). Returns {shard filename: appended}."""
    groups: dict[Path, list[dict]] = {}
    for e in entries:
        groups.setdefault(shard_path(e["id"]), []).append(e)
    REG_DIR.mkdir(exist_ok=True)
    counts: dict[str, int] = {}
    for path, group in sorted(groups.items()):
        before = len(yaml.safe_load(path.read_text()).get("sources") or []) if path.exists() else 0
        block = "".join(emit_entry(e) for e in group)
        if path.exists():
            with path.open("a") as f:
                f.write(block)
        else:
            path.write_text(f"# {path.stem} — machine-appended shard (see AGENTS.md); "
                            f"prune_corpus edits it in place\nsources:\n" + block)
        after = yaml.safe_load(path.read_text()).get("sources") or []
        if len(after) != before + len(group):
            raise RuntimeError(f"append corrupted {path.name}: {before}+{len(group)} != {len(after)}")
        counts[path.name] = len(group)
    return counts


def remove_ids(drop: set) -> int:
    """Delete entries by id across all shards IN PLACE, preserving everything else byte-for-byte
    (comments, hand formatting). Position-agnostic — an entry is removed because its id is in
    `drop`, never because of where it sits. Validates each rewritten shard (parses, exactly the
    dropped ids gone, count arithmetic holds) BEFORE writing; raises on any mismatch."""
    removed_total = 0
    for path in shard_files():
        old_text = path.read_text()
        lines = old_text.splitlines(keepends=True)
        out: list[str] = []
        removed = 0
        i = 0
        while i < len(lines):
            m = ENTRY_RE.match(lines[i])
            if not m:
                out.append(lines[i])
                i += 1
                continue
            j = _entry_span(lines, i)
            if m.group(1) in drop:
                removed += 1
            else:
                out.extend(lines[i:j])
            i = j
        if not removed:
            continue
        new_text = "".join(out)
        entries = yaml.safe_load(new_text).get("sources") or []
        old_count = len(yaml.safe_load(old_text).get("sources") or [])
        leftover = {e["id"] for e in entries} & drop
        if leftover:
            raise RuntimeError(f"{path.name}: failed to remove {len(leftover)} ids, "
                               f"e.g. {sorted(leftover)[:3]}")
        if len(entries) != old_count - removed:
            raise RuntimeError(f"{path.name}: count mismatch {old_count} - {removed} != {len(entries)}")
        path.write_text(new_text)
        removed_total += removed
    return removed_total
