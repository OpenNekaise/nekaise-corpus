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
MAX_CONTROL_FILE_BYTES = 80 * 1024 * 1024


def oversized_control_files(root: Path = ROOT) -> list[tuple[Path, int]]:
    """Return registry/manifest files too large to publish safely.

    GitHub rejects individual files above 100 MiB. Fail rounds at 80 MiB so growing shards can be
    split while ordinary commits are still publishable.
    """
    files = [
        *(root / "registry").glob("*.yaml"),
        *(root / "registry").glob("*.jsonl"),
        *(root / "manifest").glob("*.jsonl"),
    ]
    return sorted(
        ((path, path.stat().st_size) for path in files
         if path.stat().st_size > MAX_CONTROL_FILE_BYTES),
        key=lambda item: str(item[0]),
    )


def patent_country_contract_errors(backends: dict) -> list[str]:
    """An enabled patent backend may only request jurisdictions the finder itself accepts.

    Patent policy lives in three places — registry/eligibility.json, registry/backends.json and the
    SUPPORTED_COUNTRIES guard in find_patents.py. On 2026-08-28 the first two were lifted while the
    finder still refused CN, and every round died at discovery. Catch that drift at the contracts
    gate, before commit, instead of in the next cron round.
    """
    import find_patents

    approved = set(find_patents.SUPPORTED_COUNTRIES)
    errors: list[str] = []
    for name, cfg in sorted(backends.items()):
        if cfg.get("script") != "find_patents.py" or not cfg.get("enabled", True):
            continue
        args = [str(a) for a in cfg.get("args", [])]
        countries = {"US"}
        if "--countries" in args and args.index("--countries") + 1 < len(args):
            raw = args[args.index("--countries") + 1]
            countries = {c.strip().upper() for c in raw.split(",") if c.strip()}
        if unsupported := sorted(countries - approved):
            errors.append(
                f"{name}: requests {', '.join(unsupported)} but find_patents.py approves only "
                f"{', '.join(sorted(approved))}"
            )
    return errors


def eligibility_contract_errors(
    rows: list[dict], backends: dict, restrictions: dict[str, dict]
) -> list[str]:
    """Cross-file guarantees that make policy restrictions effective, not documentary."""
    errors: list[str] = []
    covered_backends = {
        backend for rule in restrictions.values() for backend in rule["backends"]
    }
    for backend in sorted(covered_backends - set(backends)):
        errors.append(f"eligibility policy names unknown backend {backend}")
    for backend in sorted(covered_backends & set(backends)):
        cfg = backends[backend]
        if cfg.get("enabled", True):
            errors.append(f"{backend}: eligibility-restricted backend must be disabled")
        reason = cfg.get("reason")
        if not isinstance(reason, str) or not reason.startswith("policy-blocked"):
            errors.append(f"{backend}: eligibility-restricted backend lacks policy-blocked reason")
    for name, cfg in backends.items():
        reason = cfg.get("reason")
        if isinstance(reason, str) and reason.startswith("policy-blocked") \
                and name not in covered_backends:
            errors.append(f"{name}: policy-blocked backend has no eligibility restriction")

    restricted = [r for r in rows if registry.restriction_for(r, restrictions) is not None]
    with_metadata = [
        r for r in restricted if any(field in r for field in registry.CORPUS_FIELDS)
    ]
    if with_metadata:
        errors.append(
            f"{len(with_metadata):,} policy-restricted manifest rows still claim corpus data "
            f"(first: {with_metadata[0]['id']})"
        )
    return errors


def main() -> int:
    errors: list[str] = []
    restrictions = registry.load_eligibility()
    manifest_rows = registry.load_manifest_rows()
    all_rows = [r for r in manifest_rows if r.get("status") == "ok"]
    rows = [r for r in all_rows if registry.is_training_eligible(r, restrictions)]
    excluded = len(all_rows) - len(rows)
    readme = (ROOT / "README.md").read_text()
    match = re.search(r"\*\*Documents\*\* \| \*\*([\d,]+)\*\*", readme)
    if not match or int(match.group(1).replace(",", "")) != len(rows):
        shown = match.group(1) if match else "missing"
        errors.append(f"README documents={shown}, manifest={len(rows):,}")
    chars = sum(r.get("text_chars", 0) for r in rows)
    expected_chars = f"{chars / 1e9:.3f}B" if chars >= 1e9 else f"{chars / 1e6:.0f}M"
    if f"~{expected_chars} chars" not in readme:
        errors.append(f"README extracted chars is stale (want {expected_chars})")
    if f"**{excluded:,}** rows (not fetched or training-ready)" not in readme:
        errors.append(f"README policy-excluded count is stale (want {excluded:,})")
    topics = Counter(r.get("topic") for r in rows)
    if f"**Topics** | {len(topics)}" not in readme:
        errors.append("README topic count is stale")

    backends = run_round.load_backends()
    errors.extend(run_round.validate_backends(backends, rotation.load()))
    errors.extend(eligibility_contract_errors(manifest_rows, backends, restrictions))
    errors.extend(patent_country_contract_errors(backends))
    try:  # vendor-literature config is control plane: schema errors must fail the round, not a fetch
        import find_vendor
        find_vendor.load_vendors()
    except Exception as exc:
        errors.append(f"registry/vendors.json: {exc}")
    configured_scripts = {cfg["script"] for cfg in backends.values()}
    actual_finders = {p.name for p in (ROOT / "scripts").glob("find_*.py")}
    for script in sorted(actual_finders - configured_scripts):
        errors.append(f"{script}: finder is missing from registry/backends.json")

    steps = [step for step, _, _ in run_round.PIPELINE]
    if steps != ["fetch", "prune", "clean", "stats"]:
        errors.append(f"serial pipeline {steps!r}, expected ['fetch', 'prune', 'clean', 'stats']")
    gates = [step for step, _, _ in run_round.VERIFY]
    if sorted(gates) != ["check", "contracts", "index", "lint"] or set(gates) & set(steps):
        errors.append(
            f"verify gates {gates!r}, expected check/index/lint/contracts, disjoint from the "
            "serial pipeline"
        )

    for path, size in oversized_control_files():
        errors.append(
            f"{path.relative_to(ROOT)} is {size / 1024 / 1024:.1f} MiB; "
            f"split before {MAX_CONTROL_FILE_BYTES / 1024 / 1024:.0f} MiB"
        )

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
