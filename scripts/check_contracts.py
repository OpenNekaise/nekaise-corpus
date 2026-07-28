#!/usr/bin/env python3
"""Cross-file architecture contracts that unit tests alone cannot see."""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

import registry
import rotation
import run_round

ROOT = Path(__file__).resolve().parents[1]


def main() -> int:
    errors: list[str] = []
    rows = [r for r in registry.load_manifest_rows() if r.get("status") == "ok"]
    readme = (ROOT / "README.md").read_text()
    match = re.search(r"\*\*Documents\*\* \| \*\*([\d,]+)\*\*", readme)
    if not match or int(match.group(1).replace(",", "")) != len(rows):
        shown = match.group(1) if match else "missing"
        errors.append(f"README documents={shown}, manifest={len(rows):,}")
    chars = sum(r.get("text_chars", 0) for r in rows)
    expected_chars = f"{chars / 1e9:.3f}B" if chars >= 1e9 else f"{chars / 1e6:.0f}M"
    if f"~{expected_chars} chars" not in readme:
        errors.append(f"README extracted chars is stale (want {expected_chars})")
    topics = Counter(r.get("topic") for r in rows)
    if f"**Topics** | {len(topics)}" not in readme:
        errors.append("README topic count is stale")

    backends = run_round.load_backends()
    errors.extend(run_round.validate_backends(backends, rotation.load()))
    configured_scripts = {cfg["script"] for cfg in backends.values()}
    actual_finders = {p.name for p in (ROOT / "scripts").glob("find_*.py")}
    for script in sorted(actual_finders - configured_scripts):
        errors.append(f"{script}: finder is missing from registry/backends.json")

    steps = [step for step, _, _ in run_round.PIPELINE]
    required_order = ["fetch", "prune", "clean", "check", "stats", "index", "lint"]
    if steps != required_order:
        errors.append(f"pipeline order {steps!r}, expected {required_order!r}")

    ledger = registry.REG_DIR / "pruned.jsonl"
    if ledger.exists():
        for lineno, line in enumerate(ledger.read_text().splitlines(), 1):
            try:
                row = json.loads(line)
                if not all(row.get(k) for k in ("id", "url", "reason", "pruned_at")):
                    errors.append(f"pruned.jsonl:{lineno}: missing required field")
            except json.JSONDecodeError as exc:
                errors.append(f"pruned.jsonl:{lineno}: {exc}")
            if len(errors) >= 50:
                break

    if errors:
        for error in errors:
            print(f"CONTRACT: {error}")
        print(f"FAIL — {len(errors)} architecture contract violation(s)")
        return 1
    print(f"OK — control-plane contracts hold for {len(rows):,} documents / "
          f"{len(backends)} backends")
    return 0


if __name__ == "__main__":
    sys.exit(main())
