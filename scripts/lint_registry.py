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

import yaml

import registry

REQUIRED = registry.REQUIRED_FIELDS
LICENSES = {"public-domain", "cc-by", "cc-by-sa", "cc0", "open", "proprietary-internal"}
TOPICS = {"controls_bas", "equipment_systems", "building_energy", "commissioning_fdd",
          "standards_protocols", "structures_civil", "construction", "materials",
          "architecture", "infrastructure", "urban"}
FORMATS = {"pdf", "html", "md", "rst", "txt"}


def main() -> int:
    errors: list[str] = []
    all_ids: Counter = Counter()
    by_id: dict[str, dict] = {}
    n_entries = 0

    if not registry.REG_DIR.is_dir():
        print("LINT: registry/ directory missing")
        return 1
    for path in registry.shard_files():
        try:
            entries = yaml.safe_load(path.read_text()).get("sources") or []
        except Exception as e:
            errors.append(f"{path.name}: does not parse: {e}")
            continue
        for e in entries:
            n_entries += 1
            eid = e.get("id", "<no id>")
            all_ids[eid] += 1
            by_id[eid] = e
            for k in REQUIRED:
                if not e.get(k):
                    errors.append(f"{path.name}: {eid}: missing field '{k}'")
            if e.get("license") not in LICENSES:
                errors.append(f"{path.name}: {eid}: unknown license '{e.get('license')}'")
            if e.get("topic") not in TOPICS:
                errors.append(f"{path.name}: {eid}: unknown topic '{e.get('topic')}'")
            if e.get("format") not in FORMATS:
                errors.append(f"{path.name}: {eid}: unknown format '{e.get('format')}'")
            if not str(e.get("url", "")).startswith(("http://", "https://")):
                errors.append(f"{path.name}: {eid}: url is not http(s): {e.get('url')}")
            if e.get("license_url") and not str(e["license_url"]).startswith(("http://", "https://")):
                errors.append(f"{path.name}: {eid}: license_url is not http(s)")
            # routing: machine shards hold only their prefixes; curated holds no machine prefixes
            want = registry.shard_path(eid).name
            if want != path.name:
                errors.append(f"{path.name}: {eid}: belongs in {want} (prefix routing)")

    for i, n in all_ids.items():
        if n > 1:
            errors.append(f"duplicate id ({n}x): {i}")

    n_rows = 0
    for r in registry.load_manifest_rows():
        n_rows += 1
        sid = r.get("id")
        if sid not in all_ids:
            errors.append(f"manifest row orphaned from registry: {r.get('id')}")
            continue
        entry = by_id[sid]
        for key in ("url", "source", "license", "topic", "format"):
            if r.get(key) != entry.get(key):
                errors.append(
                    f"manifest/registry drift {sid}.{key}: "
                    f"{r.get(key)!r} != {entry.get(key)!r}"
                )
        for key in ("sha256", "text_sha256", "corpus_sha256"):
            value = r.get(key)
            if value and not re.fullmatch(r"[0-9a-f]{64}", value):
                errors.append(f"manifest {sid}: invalid {key}")

    if errors:
        for e in errors[:50]:
            print(f"LINT: {e}")
        if len(errors) > 50:
            print(f"LINT: ... and {len(errors) - 50} more")
        print(f"\nFAIL — {len(errors)} problem(s) in {n_entries} entries / {n_rows} manifest rows")
        return 1
    print(f"OK — {n_entries} registry entries in {len(registry.shard_files())} shards, "
          f"{n_rows} manifest rows, no problems")
    return 0


if __name__ == "__main__":
    sys.exit(main())
