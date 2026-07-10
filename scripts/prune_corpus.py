#!/usr/bin/env python3
"""prune_corpus.py — quality gate. Drop low-value docs after a load.

Next-token CPT memorizes text literally, so junk in = junk out. This removes, from the manifest +
registry + disk, docs that are: failed downloads, thin/empty, garbage extractions (symbol soup from
scanned / math-heavy PDFs), non-English, off-topic (too few built-environment keywords; books use a
length-scaled density gate), or byte-duplicates (same sha256 under two ids). It prunes only
*machine-discovered* sources (registry.DISCOVERED_PREFIXES); hand-curated originals are left alone.
Verdicts come from the quality metrics build_corpus stored in each manifest row (scripts/quality.py)
— no text re-reading. Registry edits are per-id, in place, and validated (registry.remove_ids).
Dropped URLs land in pruned_urls.txt so discovery never re-churns them.

    python scripts/prune_corpus.py            # dry run -- report what would be pruned
    python scripts/prune_corpus.py --apply    # prune (delete files, rewrite manifest + registry)
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

import blocklist
import quality
import registry

HERE = Path(__file__).resolve().parents[1]  # repo root (this file lives in scripts/)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    manifest = registry.load_manifest_rows()
    # near-dup gate: seed titles are always kept; a discovered doc whose normalized title already
    # exists (same paper from another source, v1/v2, etc.) is dropped so CPT doesn't over-weight it.
    seen_titles = {registry.norm(r.get("title")) for r in manifest
                   if not registry.discovered(r["id"]) and r.get("status") == "ok"}
    drop: dict[str, str] = {}
    for r in manifest:
        if not registry.discovered(r["id"]):
            continue
        if r["status"] != "ok":
            drop[r["id"]] = "failed"
            continue
        tp = r.get("text_path")
        if not tp or not (HERE / tp).exists():
            drop[r["id"]] = "no-text"
            continue
        m = r.get("quality")
        if not m:  # pre-metrics row: compute once from the file; persisted on --apply
            m = r["quality"] = quality.metrics(quality.body((HERE / tp).read_text()))
        q = quality.verdict(m, quality.is_booklike(r["id"], r.get("format", "pdf")))
        if q != "ok":
            drop[r["id"]] = q
            continue
        tk = registry.norm(r.get("title"))
        if tk and tk in seen_titles:
            drop[r["id"]] = "dup-title"
        else:
            seen_titles.add(tk)

    # byte-dup gate: identical sha256 under two ids (same file reached via two URLs) would be
    # double-weighted by CPT. Drop the discovered copy, keep curated; curated==curated is reported.
    by_sha: dict[str, list] = {}
    for r in manifest:
        if r.get("status") == "ok" and r.get("sha256") and r["id"] not in drop:
            by_sha.setdefault(r["sha256"], []).append(r)
    for twins in by_sha.values():
        if len(twins) < 2:
            continue
        twins.sort(key=lambda r: (registry.discovered(r["id"]), r["id"]))  # curated first, stable
        for r in twins[1:]:
            if registry.discovered(r["id"]):
                drop[r["id"]] = "dup-bytes"
            else:
                print(f"  note: same bytes, both hand-curated (kept): {twins[0]['id']} == {r['id']}")

    disc_total = sum(1 for r in manifest if registry.discovered(r["id"]))
    print(f"discovered docs: {disc_total} | would prune: {dict(Counter(drop.values()))} "
          f"(total {len(drop)})")
    if not args.apply:
        print("dry run -- pass --apply to prune")
        return

    # Blocklist policy: quality-fails and HARD fetch failures (404/410, fake PDFs) never return.
    # TRANSIENT walls (429/202/503, timeouts, connection errors) are NOT blocklisted — the entry
    # leaves the registry but a later finder run may rediscover it once the wall lifts
    # (IBPSA's sgcaptcha and Wikimedia rate limits taught us this the hard way).
    TRANSIENT = (202, 429, 503)
    def _blocklistable(r) -> bool:
        if drop.get(r["id"]) != "failed":
            return True
        if r.get("http_status") in TRANSIENT:
            return False
        err = (r.get("error") or "").lower()
        return not any(k in err for k in ("timeout", "timed out", "connection", "too many requests"))
    blocked = blocklist.add(r.get("url") for r in manifest if r["id"] in drop and _blocklistable(r))
    removed = registry.remove_ids(set(drop))  # validate/rewrite the registry BEFORE touching files
    for r in manifest:
        if r["id"] in drop:
            for p in (r.get("raw_path"), r.get("text_path")):
                if p and (HERE / p).exists():
                    (HERE / p).unlink()
    keep = [r for r in manifest if r["id"] not in drop]
    (HERE / "manifest.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in keep))
    good_disc = sum(1 for r in keep if registry.discovered(r["id"]) and r["status"] == "ok")
    print(f"pruned {len(drop)} docs ({removed} registry entries removed, {blocked} urls "
          f"blocklisted); kept {good_disc} good discovered docs, {len(keep)} manifest rows")


if __name__ == "__main__":
    main()
