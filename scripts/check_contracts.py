#!/usr/bin/env python3
"""Cross-file architecture contracts that unit tests alone cannot see."""
from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

import corpus_stats
import registry
import run_round
import store

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
        *(root / "registry").glob("*.json"),
        *(root / "registry" / "journal").glob("*.jsonl"),
        *(root / "manifest").glob("*.jsonl"),
    ]
    return sorted(
        ((path, path.stat().st_size) for path in files
         if path.stat().st_size > MAX_CONTROL_FILE_BYTES),
        key=lambda item: str(item[0]),
    )


def prune_ledger_contract_errors(root: Path = ROOT) -> list[str]:
    """Validate every decision-provenance shard and reject the retired monolithic layout."""
    errors: list[str] = []
    legacy = root / "registry" / "pruned.jsonl"
    if legacy.exists():
        errors.append("registry/pruned.jsonl: legacy monolith must be migrated to pruned-*.jsonl")
    files = sorted((root / "registry").glob("pruned-*.jsonl"))
    valid_names = {
        f"pruned-{bucket}.jsonl" for bucket in range(registry.PRUNE_LEDGER_BUCKETS)
    }
    for path in files:
        rel = path.relative_to(root)
        if path.name not in valid_names:
            errors.append(f"{rel}: unexpected prune-ledger shard name")
        for lineno, line in enumerate(path.read_text().splitlines(), 1):
            try:
                row = json.loads(line)
                if not all(row.get(k) for k in ("id", "url", "reason", "pruned_at")):
                    errors.append(f"{rel}:{lineno}: missing required field")
                elif path.name != registry.prune_ledger_name(row["id"]):
                    expected = registry.prune_ledger_name(row["id"])
                    errors.append(f"{rel}:{lineno}: id belongs in registry/{expected}")
            except json.JSONDecodeError as exc:
                errors.append(f"{rel}:{lineno}: {exc}")
            if len(errors) >= 50:
                return errors
    return errors


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
    restricted_metadata: tuple[int, str | None], backends: dict, restrictions: dict[str, dict]
) -> list[str]:
    """``restricted_metadata`` = corpus_stats.restricted_with_corpus_data(view, restrictions)."""
    """Cross-file guarantees that make policy restrictions effective, not documentary."""
    errors: list[str] = []
    covered_backends = {
        backend for rule in restrictions.values() for backend in rule["backends"]
        if backend not in registry.MANUAL_TOOLS  # one-shot tools enforce restrictions themselves
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

    count, first = restricted_metadata
    if count:
        errors.append(
            f"{count:,} policy-restricted manifest rows still claim corpus data (first: {first})"
        )
    return errors


def host_policy_contract_errors(backends: dict, policy: dict) -> list[str]:
    """A suspended host's backends must be disabled. `policy` is the validated host policy
    pinned in the same view as `backends` (store.pinned_policy)."""
    errors = []
    for host, rule in sorted(policy.items()):
        for name in rule.get("backends", []):
            if name not in backends:
                errors.append(f"host policy {host} names unknown backend {name}")
            elif rule["status"] == "suspended" and backends[name].get("enabled", True):
                errors.append(f"{name}: backend for suspended host {host} must be disabled")
    return errors


def runtime_backend_state(view) -> tuple[dict, list[str]]:
    """The view's runtime backend state, or ({}, [error]) when it cannot be read as BackendState
    records (e.g. a hand-edited registry/backend_state.json with unknown fields)."""
    try:
        return view.backend_state_get(), []
    except Exception as exc:
        return {}, [f"registry/{store.BACKEND_STATE_FILE}: unreadable runtime state: {exc}"]


def effective_backends(backends: dict, runtime: dict) -> dict:
    """Backend configs whose `enabled` is the effective enablement: configuration AND runtime."""
    return {name: {**cfg, "enabled": bool(cfg.get("enabled", True))
                   and runtime.get(name, store.BackendState()).enabled is not False}
            for name, cfg in backends.items()}


def readme_stats_errors(readme: str, stats) -> list[str]:
    """Validate every README statistic against ONE manifest-derived view (corpus_stats of one
    store view). Local file availability never enters it, so the committed numbers are identical
    on every machine and fields cannot be satisfied by different views."""
    errors = []
    match = re.search(r"\*\*Documents\*\* \| \*\*([\d,]+)\*\*", readme)
    if not match or int(match.group(1).replace(",", "")) != stats.documents:
        shown = match.group(1) if match else "missing"
        errors.append(f"README documents={shown}, manifest={stats.documents:,}")
    chars = stats.text_chars
    excluded = stats.excluded
    expected_chars = f"{chars / 1e9:.3f}B" if chars >= 1e9 else f"{chars / 1e6:.0f}M"
    if f"~{expected_chars} chars" not in readme:
        errors.append(f"README extracted chars is stale (want {expected_chars})")
    if f"**{excluded:,}** rows (not fetched or training-ready)" not in readme:
        errors.append(f"README policy-excluded count is stale (want {excluded:,})")
    if f"**Topics** | {len(stats.topics)}" not in readme:
        errors.append("README topic count is stale")
    return errors


def main() -> int:
    errors: list[str] = []
    st = store.open(root=ROOT)
    with st.read(timeout=60) as view:
        # eligibility and host policy pinned with the data they are checked against; invalid or
        # missing policy is a contract failure (fail closed)
        try:
            restrictions, policy = store.pinned_policy(view)
        except store.StoreError as exc:
            print(f"CONTRACT: {exc}")
            return 1
        stats = corpus_stats.compute(view, restrictions)
        restricted_metadata = corpus_stats.restricted_with_corpus_data(view, restrictions)
        unavailable = corpus_stats.local_unavailable(view, ROOT, restrictions, policy)
        backends = {k: v for k, v in view.config_get().backends.items() if not k.startswith("_")}
        rotation_state = view.rotation_get()
        runtime, runtime_errors = runtime_backend_state(view)
    import staged_runs
    if not staged_runs.staged_authority(st):
        # README statistics are a per-round git artifact of the file-authoritative loop; under
        # PostgreSQL authority rounds promote generations and write no README (ADR 0001 stage 4)
        readme = (ROOT / "README.md").read_text()
        errors.extend(readme_stats_errors(readme, stats))
    if unavailable:
        print(f"local availability: {unavailable:,} eligible rows on a fetch-suspended host "
              "have no local payload here (README counts are manifest-based)")

    errors.extend(runtime_errors)
    errors.extend(run_round.validate_backends(backends, rotation_state, runtime))
    # Policy lives in configuration: an eligibility-restricted backend must be DISABLED IN CONFIG
    # with a policy-blocked reason; runtime exhaustion never satisfies it.
    errors.extend(eligibility_contract_errors(restricted_metadata, backends, restrictions))
    # What may run is the effective enablement (configuration AND runtime state).
    effective = effective_backends(backends, runtime)
    errors.extend(patent_country_contract_errors(effective))
    try:  # vendor-literature config is control plane: schema errors must fail the round, not a fetch
        import find_vendor
        find_vendor.load_vendors()
    except Exception as exc:
        errors.append(f"registry/vendors.json: {exc}")
    errors.extend(host_policy_contract_errors(effective, policy))
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

    errors.extend(prune_ledger_contract_errors())

    if errors:
        for error in errors:
            print(f"CONTRACT: {error}")
        print(f"FAIL — {len(errors)} architecture contract violation(s)")
        return 1
    print(f"OK — control-plane contracts hold for {stats.documents:,} documents / "
          f"{len(backends)} backends")
    return 0


if __name__ == "__main__":
    sys.exit(main())
