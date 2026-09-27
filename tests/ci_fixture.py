#!/usr/bin/env python3
"""A small, self-contained data root for CI (ADR 0001 stage 4 step 5): the repository's own
configuration (backends, eligibility, vendors, host policy) and control state, with a synthetic
registry, manifest, blocklist and ledger that satisfy every file-store contract — so CI keeps
running `lint_registry.py --root` and `check_contracts.py --root` when the tracked data leaves
git in stage 6 (until then CI checks the tracked data too).

    python tests/ci_fixture.py DIR      # DIR must not exist

Every eligibility restriction gets one matching registry entry (lint requires each restriction
to match something) and no corpus data; README statistics are rendered by
update_readme_stats.py's own code over the fixture.
"""
from __future__ import annotations

import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
sys.path.insert(0, str(REPO / "scripts"))
sys.path.insert(0, str(HERE))

# control state that is still tracked in git until stage 6 (then CI needs a committed copy)
CONTROL = ("rotation.json", "backend_state.json", "github_passes.json")
TOPICS = ("building_energy", "structures_civil", "construction", "materials", "architecture",
          "infrastructure", "urban")


def _entry(sid: str, **kw) -> dict:
    return {"id": sid, "title": f"CI fixture document {sid}",
            "url": f"https://fixture.example/{sid}.pdf", "source": "osti",
            "license": "public-domain", "topic": "building_energy", "format": "pdf", **kw}


def _row(e: dict, i: int) -> dict:
    import hashlib
    return {**e, "status": "ok", "http_status": 200, "bytes": 1000 + i,
            "sha256": hashlib.sha256(e["id"].encode()).hexdigest(),
            "raw_path": f"raw/{e['source']}/{e['id']}.pdf", "text_path": f"text/{e['id']}.md",
            "text_chars": 20_000 + 7 * i, "error": None,
            "fetched_at": "2026-09-25T00:00:00Z", "extractor_version": "fixture"}


def build(root: Path) -> Path:
    import json

    import store
    import update_readme_stats
    from pipeline_repo import write_repo
    root = Path(root)
    if root.exists():
        raise SystemExit(f"{root} exists")
    eligibility = json.loads((REPO / "registry" / "eligibility.json").read_text())
    entries = [_entry(f"ost-ci-{i:02d}", topic=TOPICS[i % len(TOPICS)],
                      license=("public-domain", "cc-by", "open")[i % 3]) for i in range(9)]
    manifest = [_row(e, i) for i, e in enumerate(entries)]
    for n, rule in enumerate(eligibility.get("restrictions", {}).values()):
        match = rule["match"]
        sid = f"{match.get('id_prefix', 'ost-')}ci-restricted-{n}"
        extra = {"license": match["license"]} if "license" in match else {}
        entries.append(_entry(sid, source=match.get("source", "osti"), **extra))
    gone = "ost-ci-pruned"
    ledger = [{"id": gone, "url": f"https://fixture.example/{gone}.pdf", "reason": "thin",
               "pruned_at": "2026-09-25T00:00:00Z", "blocklisted": True}]
    write_repo(root, entries=sorted(entries, key=lambda e: e["id"]), manifest=manifest,
               blocklist=[f"https://fixture.example/{gone}.pdf"], ledger=ledger, policy=None)
    for name in (*store.CONFIG_FILES, *CONTROL):
        src = REPO / "registry" / name
        if src.exists():
            shutil.copy2(src, root / "registry" / name)
    shutil.copy2(REPO / ".gitignore", root / ".gitignore")   # payload dirs are git-ignored
    (root / "README.md").write_text("# CI fixture\n\n<!-- STATS:START -->\n<!-- STATS:END -->\n")
    saved = update_readme_stats.HERE, update_readme_stats.README
    update_readme_stats.HERE, update_readme_stats.README = root, root / "README.md"
    try:
        update_readme_stats.main([])
    finally:
        update_readme_stats.HERE, update_readme_stats.README = saved
    return root


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    print(build(Path(sys.argv[1])))
