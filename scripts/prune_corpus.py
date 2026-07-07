#!/usr/bin/env python3
"""prune_corpus.py — quality gate. Drop low-value docs after a load.

Next-token CPT memorizes text literally, so junk in = junk out. This removes, from the manifest +
registry + disk, docs that are: failed downloads, thin/empty, garbage extractions (symbol soup from
scanned / math-heavy PDFs), non-English, off-topic (too few built-environment keywords; books use a
length-scaled density gate), or byte-duplicates (same sha256 under two ids). It prunes only
*machine-discovered* sources (id prefix oa-/ope-/ost-/arx-/crawl-/gh-/oer-/arc-); hand-curated originals
are left alone. sources.yaml is edited in place per dropped id — comments, section markers, and
entries of any other id are preserved wherever they sit in the file.

    python scripts/prune_corpus.py            # dry run -- report what would be pruned
    python scripts/prune_corpus.py --apply    # prune (delete files, rewrite manifest + sources.yaml)
"""
from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

import yaml

import blocklist

HERE = Path(__file__).resolve().parents[1]  # repo root (this file lives in scripts/)

DOMAIN = re.compile(
    r"build|hvac|energy|thermal|heat|cool|ventil|chiller|boiler|refriger|damper|ahu|vav|setpoint|"
    r"occupan|comfort|retrofit|envelope|commission|controller|sensor|actuator|bacnet|ashrae|kwh|"
    r"carbon|emission|psychrometr|economizer|fault|diagnos|efficien|insulat|"
    # AEC / built-environment: architecture, engineering, construction, infrastructure, materials
    r"struct|concret|cement|reinforc|rebar|masonr|timber|lumber|steel|weld|beam|column|"
    r"truss|girder|slab|foundation|footing|bearing|geotech|soil|slope|retain|excavat|"
    r"settlement|tunnel|bridge|deck|abutment|pavement|asphalt|aggregat|seismic|earthquake|"
    r"deflection|modulus|stress|strain|shear|bending|axial|construct|contractor|scaffold|"
    r"formwork|demolition|renovat|estimat|architect|facade|roof|floor|durab|corros|"
    r"fatigue|fracture|composite|civil|infrastructur|survey|geomat|\bbim\b|\bifc\b|hydraul|"
    r"drainag|culvert|fire|egress|sprinkler|smoke|osha|material|coating|polymer|elastic|"
    r"plastic|urban|zoning|transport|highway|traffic|wastewater|geolog|hazard|flood|"
    r"coastal|levee", re.I)
EN = re.compile(r"\b(the|and|of|to|in|is|for|that|with|are|this|be|as|by|on|from)\b", re.I)


def body(text: str) -> str:
    return text.split("\n---\n\n", 1)[-1] if "\n---\n\n" in text else text


def quality(text: str, book: bool = True) -> str:
    bod = body(text)
    # Long book-like docs are judged over a 100k-char window: their first 20k chars are front
    # matter (title pages, TOC dot-leaders, OCR noise) that fails alpha-ratio checks and
    # under-represents the content. Their off-topic gate is density-based, calibrated 2026-07-07
    # on a 300-book OAPEN sample: clearly-unrelated books score < 8 DOMAIN hits / 1000 words; real
    # AEC books score 30-150. `book` is decided by the caller (pdf format, or arc- OCR texts) —
    # source code / repo docs keep the 20k/absolute gate, since code identifiers have book-unlike
    # word statistics and the density rule falsely kills them.
    is_book = book and len(bod) > 120_000
    t = bod[:100_000] if is_book else bod[:20_000]
    if len(t.strip()) < 2000:
        return "thin"
    if sum(c.isalpha() for c in t) / len(t) < 0.55:
        return "garbage"
    words = re.findall(r"[A-Za-z]{2,}", t)
    if len(words) < 200:
        return "thin"
    if len(EN.findall(t)) / len(words) < 0.04:
        return "non-english"
    hits = len(DOMAIN.findall(t))
    if (1000 * hits / len(words) < 8.0) if is_book else (hits < 10):
        return "off-topic"
    return "ok"


def discovered(i: str) -> bool:
    return i.startswith(("oa-", "ope-", "ost-", "arx-", "crawl-", "gh-", "oer-", "arc-"))


ENTRY_RE = re.compile(r"^  - id:\s*['\"]?(.+?)['\"]?\s*$")


def rewrite_sources(drop: set) -> int:
    """Delete dropped entries from sources.yaml IN PLACE, preserving everything else byte-for-byte
    (comments, section markers, hand formatting). Position-agnostic: an entry is removed because
    its id is in `drop`, never because of where it sits relative to a marker — the old
    head/marker-split rewrite wiped non-prefixed veins living below the marker (2026-07-06).
    Validates the result (parses, exactly the dropped ids gone) before writing; raises otherwise."""
    path = HERE / "sources.yaml"
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
        j = i + 1
        while j < len(lines) and re.match(r"^    \s*\S", lines[j]):  # entry field lines
            j += 1
        if m.group(1) in drop:
            removed += 1
        else:
            out.extend(lines[i:j])
        i = j
    new_text = "".join(out)
    entries = yaml.safe_load(new_text)["sources"]
    old_count = len(yaml.safe_load(old_text)["sources"])
    leftover = {e["id"] for e in entries} & drop
    if leftover:
        raise RuntimeError(f"rewrite failed to remove {len(leftover)} ids, e.g. {sorted(leftover)[:3]}")
    if len(entries) != old_count - removed:
        raise RuntimeError(f"rewrite entry-count mismatch: {old_count} - {removed} != {len(entries)}")
    path.write_text(new_text)
    return removed


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()

    manifest = [json.loads(l) for l in (HERE / "manifest.jsonl").read_text().splitlines() if l.strip()]
    # near-dup gate: seed titles are always kept; a discovered doc whose normalized title already
    # exists (same paper from another source, v1/v2, etc.) is dropped so CPT doesn't over-weight it.
    tkey = lambda t: re.sub(r"\W+", " ", (t or "").lower()).strip()
    seen_titles = {tkey(r.get("title")) for r in manifest
                   if not discovered(r["id"]) and r.get("status") == "ok"}
    drop: dict[str, str] = {}
    for r in manifest:
        if not discovered(r["id"]):
            continue
        if r["status"] != "ok":
            drop[r["id"]] = "failed"
            continue
        tp = r.get("text_path")
        if not tp or not (HERE / tp).exists():
            drop[r["id"]] = "no-text"
            continue
        q = quality((HERE / tp).read_text(),
                    book=r.get("format", "pdf") == "pdf" or r["id"].startswith("arc-"))
        if q != "ok":
            drop[r["id"]] = q
            continue
        tk = tkey(r.get("title"))
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
        twins.sort(key=lambda r: (discovered(r["id"]), r["id"]))  # curated first, then stable
        for r in twins[1:]:
            if discovered(r["id"]):
                drop[r["id"]] = "dup-bytes"
            else:
                print(f"  note: same bytes, both hand-curated (kept): {twins[0]['id']} == {r['id']}")

    disc_total = sum(1 for r in manifest if discovered(r["id"]))
    print(f"discovered docs: {disc_total} | would prune: {dict(Counter(drop.values()))} "
          f"(total {len(drop)})")
    if not args.apply:
        print("dry run -- pass --apply to prune")
        return

    blocked = blocklist.add(r.get("url") for r in manifest if r["id"] in drop)
    removed = rewrite_sources(set(drop))  # validate/rewrite the registry BEFORE touching files
    for r in manifest:
        if r["id"] in drop:
            for p in (r.get("raw_path"), r.get("text_path")):
                if p and (HERE / p).exists():
                    (HERE / p).unlink()
    keep = [r for r in manifest if r["id"] not in drop]
    (HERE / "manifest.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in keep))
    good_disc = sum(1 for r in keep if discovered(r["id"]) and r["status"] == "ok")
    print(f"pruned {len(drop)} docs ({removed} registry entries removed, {blocked} urls blocklisted); "
          f"kept {good_disc} good discovered docs, {len(keep)} manifest rows")


if __name__ == "__main__":
    main()
