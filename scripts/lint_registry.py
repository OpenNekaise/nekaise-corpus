#!/usr/bin/env python3
"""lint_registry.py — sanity-check sources.yaml + manifest.jsonl before publishing.

Catches the failure modes that have actually bitten this repo: duplicate ids (truncated slugs
silently overwriting each other's files), manifest rows orphaned from the registry (a bad prune
rewrite), unknown license/topic/format values, non-http urls, and a missing `# --- discovered`
marker (find_books/find_sources insertion anchor). Run by CI on every push/PR; run it locally
any time with:

    python scripts/lint_registry.py
"""
from __future__ import annotations

import json
import sys
from collections import Counter
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parents[1]  # repo root (this file lives in scripts/)

REQUIRED = ("id", "title", "url", "source", "license", "topic", "format")
LICENSES = {"public-domain", "cc-by", "cc-by-sa", "cc0", "open", "proprietary-internal"}
TOPICS = {"controls_bas", "equipment_systems", "building_energy", "commissioning_fdd",
          "standards_protocols", "structures_civil", "construction", "materials",
          "architecture", "infrastructure", "urban"}
FORMATS = {"pdf", "html", "md", "rst", "txt"}


def main() -> int:
    errors: list[str] = []
    text = (HERE / "sources.yaml").read_text()
    entries = yaml.safe_load(text)["sources"]

    if "# --- discovered" not in text:
        errors.append("missing '# --- discovered' marker (find_sources/find_books insertion anchor)")

    ids = Counter(e.get("id") for e in entries)
    for i, n in ids.items():
        if n > 1:
            errors.append(f"duplicate id ({n}x): {i}")
    for e in entries:
        eid = e.get("id", "<no id>")
        for k in REQUIRED:
            if not e.get(k):
                errors.append(f"{eid}: missing field '{k}'")
        if e.get("license") not in LICENSES:
            errors.append(f"{eid}: unknown license '{e.get('license')}'")
        if e.get("topic") not in TOPICS:
            errors.append(f"{eid}: unknown topic '{e.get('topic')}'")
        if e.get("format") not in FORMATS:
            errors.append(f"{eid}: unknown format '{e.get('format')}'")
        if not str(e.get("url", "")).startswith(("http://", "https://")):
            errors.append(f"{eid}: url is not http(s): {e.get('url')}")

    mp = HERE / "manifest.jsonl"
    n_rows = 0
    if mp.exists():
        reg_ids = set(ids)
        for line in mp.read_text().splitlines():
            if not line.strip():
                continue
            n_rows += 1
            r = json.loads(line)
            if r.get("id") not in reg_ids:
                errors.append(f"manifest row orphaned from registry: {r.get('id')}")

    if errors:
        for e in errors[:50]:
            print(f"LINT: {e}")
        if len(errors) > 50:
            print(f"LINT: ... and {len(errors) - 50} more")
        print(f"\nFAIL — {len(errors)} problem(s) in {len(entries)} entries / {n_rows} manifest rows")
        return 1
    print(f"OK — {len(entries)} registry entries, {n_rows} manifest rows, no problems")
    return 0


if __name__ == "__main__":
    sys.exit(main())
