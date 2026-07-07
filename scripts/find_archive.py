#!/usr/bin/env python3
"""find_archive.py — discover pre-1929 public-domain built-environment books on the Internet Archive.

archive.org holds tens of thousands of scanned engineering handbooks, trade manuals, and textbooks
whose US copyright has expired (published <= 1928): steam heating, ventilation, carpentry, masonry,
reinforced concrete, bridges, plumbing, surveying, town planning, ... Dense period-practice prose —
CPT gold, and unambiguously redistributable (public-domain).

Searches advancedsearch.php per subject (English texts, date-capped), keeps items with a plain-text
OCR derivative ({identifier}_djvu.txt — fetched as format `txt`), dedups against manifest /
registry / pruned-URL blocklist, and appends `arc-` entries to registry/archive.yaml. OCR quality varies;
prune_corpus gates the garbage afterwards — always load + prune after appending.

    python scripts/find_archive.py                       # propose (page 1)
    python scripts/find_archive.py --rows 50 --page 2 --max 300 --append
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import requests
import yaml

import registry

HERE = Path(__file__).resolve().parents[1]  # repo root (this file lives in scripts/)
SEARCH = "https://archive.org/advancedsearch.php"
UA = {"User-Agent": "nekaise-corpus/find_archive"}

# (query phrase -> topic). Subject OR title match; the prune DOMAIN gate is the real filter.
QUERIES = [
    ("heating and ventilation", "equipment_systems"), ("steam heating", "equipment_systems"),
    ("hot water heating", "equipment_systems"), ("plumbing", "equipment_systems"),
    ("refrigeration", "equipment_systems"), ("boilers", "equipment_systems"),
    ("electric wiring", "equipment_systems"), ("ventilation of buildings", "building_energy"),
    ("building construction", "construction"), ("carpentry", "construction"),
    ("estimating building", "construction"), ("concrete construction", "construction"),
    ("masonry", "materials"), ("building materials", "materials"),
    ("brickwork", "materials"), ("timber", "materials"),
    ("reinforced concrete", "structures_civil"), ("structural engineering", "structures_civil"),
    ("strength of materials", "structures_civil"), ("bridge engineering", "structures_civil"),
    ("foundations engineering", "structures_civil"), ("steel construction", "structures_civil"),
    ("architecture handbook", "architecture"), ("architectural drawing", "architecture"),
    ("fire protection", "architecture"),
    ("road construction", "infrastructure"), ("highway engineering", "infrastructure"),
    ("railway engineering", "infrastructure"), ("water supply engineering", "infrastructure"),
    ("sewerage", "infrastructure"), ("hydraulics", "infrastructure"),
    ("surveying", "infrastructure"), ("irrigation engineering", "infrastructure"),
    ("town planning", "urban"),
]


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=50, help="results per query page")
    ap.add_argument("--page", type=int, default=1, help="search page (rotate deeper each round)")
    ap.add_argument("--max", type=int, default=300, help="cap on new entries this run")
    ap.add_argument("--append", action="store_true", help="append into the registry (registry/archive.yaml)")
    args = ap.parse_args()

    urls, titles, reg_ids = registry.existing_keys()
    out = []
    for term, topic in QUERIES:
        if len(out) >= args.max:
            break
        q = (f'(subject:"{term}" OR title:"{term}") AND mediatype:texts '
             f'AND date:[1800-01-01 TO 1928-12-31] AND language:(eng OR English)')
        try:
            r = requests.get(SEARCH, params={
                "q": q, "fl[]": ["identifier", "title", "year"], "rows": args.rows,
                "page": args.page, "output": "json", "sort[]": "downloads desc"},
                headers=UA, timeout=45)
            r.raise_for_status()
            docs = r.json().get("response", {}).get("docs", [])
        except Exception as e:
            print(f"# search '{term}' failed: {e}", file=sys.stderr)
            continue
        for d in docs:
            if len(out) >= args.max:
                break
            ident, title, year = d.get("identifier"), d.get("title"), d.get("year")
            if isinstance(title, list):  # advancedsearch returns multi-valued fields as lists
                title = title[0] if title else ""
            if isinstance(year, list):
                year = year[0] if year else None
            title = (title or "").strip()
            if not ident or not isinstance(ident, str) or not title:
                continue
            # trade catalogs / pure number-tables OCR to symbol soup — skip at discovery
            if re.search(r"catalog|catalogue|price.?book|price list|tables of", title, re.I):
                continue
            url = f"https://archive.org/download/{ident}/{ident}_djvu.txt"
            if url.rstrip("/") in urls or registry.norm(title) in titles:
                continue
            sid = f"arc-{registry.slug(ident)[:52]}"
            urls.add(url.rstrip("/"))
            titles.add(registry.norm(title))
            out.append({"id": sid, "title": f"{title[:130]} ({year})" if year else title[:150],
                        "url": url, "source": "internet_archive", "license": "public-domain",
                        "topic": topic, "format": "txt"})
        time.sleep(1)  # politeness between queries

    registry.uniquify_ids(out, reg_ids)
    by_topic: dict = {}
    for h in out:
        by_topic[h["topic"]] = by_topic.get(h["topic"], 0) + 1
    print(f"# {len(out)} NEW pre-1929 public-domain texts (page {args.page}; deduped vs "
          f"manifest + registry + blocklist)")
    print(f"# by topic: {by_topic}")
    print("# --- review, then --append, then scripts/build_corpus.py ---")
    print(yaml.safe_dump(out, sort_keys=False, allow_unicode=True))

    if args.append and out:
        counts = registry.append_entries(out)
        print(f"# appended {len(out)} entries to the registry: {counts}", file=sys.stderr)


if __name__ == "__main__":
    main()
