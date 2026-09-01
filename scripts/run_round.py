#!/usr/bin/env python3
"""Run one transactional corpus growth round.

This is the single control plane used by humans, cron, and marathon:

    discover -> fetch -> prune -> clean -> stats -> [check | index | lint | contracts | tests] -> commit

The bracketed gates are read-only over the settled round state, so they run concurrently: every
one is awaited, their output is replayed in declared order, and any failure fails the round.

The repository lock prevents concurrent operators. Every required command and finder is fail-closed:
a failure records a run-ledger event, exits non-zero, and never commits or pushes. Backends marked
``required: false`` report degraded discovery without blocking healthy veins. Successful finder
pointers advance only after that finder exits zero.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import ops
import registry
import rotation

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
BACKENDS = ROOT / "registry" / "backends.json"
# Serial prefix: each step mutates state the next one reads.
PIPELINE = (
    ("fetch", "build_corpus.py", ()),
    ("prune", "prune_corpus.py", ("--apply",)),
    ("clean", "clean_corpus.py", ()),
    ("stats", "update_readme_stats.py", ()),
)
# Read-only gates over the settled state (index writes only workspace/corpus-index.sqlite3, under
# its own named lock). They run concurrently via run_verify_parallel; contracts must follow stats
# because it checks the README counts stats just wrote.
VERIFY = (
    ("check", "clean_corpus.py", ("--check",)),
    ("index", "corpus_index.py", ("status",)),
    ("lint", "lint_registry.py", ()),
    ("contracts", "check_contracts.py", ()),
)
COMMIT_PATHS = ("README.md", "registry", "manifest", "pruned_urls.txt")
SNAPSHOT_PATHS = COMMIT_PATHS


def load_backends(path: Path = BACKENDS) -> dict:
    return {k: v for k, v in json.loads(path.read_text()).items() if not k.startswith("_")}


def disable_backend(name: str, reason: str, path: Path | None = None) -> None:
    """Persist finder-reported exhaustion without discarding its resumable rotation state."""
    path = path or BACKENDS
    raw = json.loads(path.read_text())
    if name not in raw:
        raise KeyError(f"unknown backend {name}")
    raw[name]["enabled"] = False
    raw[name]["reason"] = f"exhausted: {reason}"
    ops.atomic_write_text(path, json.dumps(raw, indent=2, ensure_ascii=False) + "\n")


def validate_backends(backends: dict, rotation_state: dict) -> list[str]:
    errors = []
    for name, cfg in backends.items():
        script = SCRIPTS / cfg.get("script", "")
        if not script.is_file():
            errors.append(f"{name}: missing script {script.name}")
        if "required" in cfg and not isinstance(cfg["required"], bool):
            errors.append(f"{name}: required must be true or false")
        rotates = cfg.get("rotation", True)
        if rotates and name not in rotation_state:
            errors.append(f"{name}: missing rotation entry")
        if rotates and name in rotation_state:
            errors.extend(rotation.validate_entry(name, rotation_state[name]))
        if not rotates and name in rotation_state:
            errors.append(f"{name}: rotation entry exists but config says rotation=false")
    for name in rotation_state:
        if name.startswith("_"):
            continue
        if name not in backends:
            errors.append(f"{name}: rotation entry has no backend config")
    return errors


def finder_command(name: str, cfg: dict, rotation_state: dict,
                   python: str = sys.executable) -> list[str]:
    cmd = [python, str(SCRIPTS / cfg["script"]), *map(str, cfg.get("args", []))]
    if cfg.get("rotation", True):
        pointer = rotation_state[name]
        cmd += [pointer["flag"], str(pointer["next"])]
    return [*cmd, "--append"]


def run_command(step: str, cmd: list[str], env: dict, run_id: str) -> None:
    shown = shlex.join(cmd)
    print(f"\n== {step}: {shown}", flush=True)
    ops.run_event(run_id, "step_started", step=step, command=shown)
    started = time.monotonic()
    result = subprocess.run(cmd, cwd=ROOT, env=env)
    elapsed = round(time.monotonic() - started, 3)
    if result.returncode:
        ops.run_event(
            run_id, "step_failed", step=step, returncode=result.returncode,
            elapsed_seconds=elapsed,
        )
        raise RuntimeError(f"{step} failed with exit {result.returncode}: {shown}")
    ops.run_event(run_id, "step_completed", step=step, elapsed_seconds=elapsed)


def run_verify_parallel(gates: list[tuple[str, list[str]]], env: dict, run_id: str) -> None:
    """Run the read-only gates concurrently over the settled round state.

    Same fail-closed contract as run_command, minus the serial wall time: every gate is awaited even
    after another has failed, each records its own ledger events, output is replayed in declared
    order (never interleaved), and the round fails if any gate did. Measured 2026-08-28: check 86 s
    + index 57 s + lint 63 s + contracts 13 s + tests 78 s serially, vs the slowest one together.
    """
    if not gates:
        return
    shown = {step: shlex.join(cmd) for step, cmd in gates}
    print("\n== verify (concurrent): " + " | ".join(step for step, _ in gates), flush=True)

    def execute(step: str, cmd: list[str]) -> tuple[str, subprocess.CompletedProcess, float]:
        ops.run_event(run_id, "step_started", step=step, command=shown[step])
        started = time.monotonic()
        result = subprocess.run(cmd, cwd=ROOT, env=env, capture_output=True, text=True)
        elapsed = round(time.monotonic() - started, 3)
        if result.returncode:
            ops.run_event(
                run_id, "step_failed", step=step, returncode=result.returncode,
                elapsed_seconds=elapsed,
            )
        else:
            ops.run_event(run_id, "step_completed", step=step, elapsed_seconds=elapsed)
        return step, result, elapsed

    with ThreadPoolExecutor(max_workers=len(gates)) as pool:
        futures = [pool.submit(execute, step, cmd) for step, cmd in gates]
        results = [future.result() for future in futures]  # declared order; waits for every gate

    failed = []
    for step, result, elapsed in results:
        print(f"\n== {step}: {shown[step]}  [{elapsed:.0f}s, exit {result.returncode}]", flush=True)
        if result.stdout:
            print(result.stdout, end="" if result.stdout.endswith("\n") else "\n", flush=True)
        if result.stderr:
            print(result.stderr, end="" if result.stderr.endswith("\n") else "\n",
                  file=sys.stderr, flush=True)
        if result.returncode:
            failed.append(f"{step} (exit {result.returncode})")
    if failed:
        raise RuntimeError("verification failed: " + ", ".join(failed))


def _finder_output(text: str) -> str:
    """Keep finder summaries/errors in the round log without replaying their full YAML proposals."""
    return "\n".join(line for line in text.splitlines() if line.startswith("#"))


def merge_proposals(results: list[dict]) -> tuple[int, dict[str, int]]:
    """Merge finder proposal files in backend order, deduplicating across concurrent finders."""
    urls, titles, ids = registry.existing_keys()
    merged = []
    accepted: dict[str, int] = {}
    for result in sorted(results, key=lambda r: r["index"]):
        path = result["proposal"]
        entries = json.loads(path.read_text()) if path.exists() else []
        count = 0
        for entry in entries:
            missing = [field for field in registry.REQUIRED_FIELDS if not entry.get(field)]
            if missing:
                raise RuntimeError(
                    f"{result['name']} proposed {entry.get('id', '<no id>')} without "
                    f"{', '.join(missing)}"
                )
            url = entry["url"].rstrip("/")
            title = registry.norm(entry["title"])
            if url in urls or title in titles:
                continue
            registry.uniquify_ids([entry], ids)
            urls.add(url)
            titles.add(title)
            merged.append(entry)
            count += 1
        accepted[result["name"]] = count
    if merged:
        registry.append_entries(merged)
    return len(merged), accepted


def run_finders_parallel(
    selected: list[str],
    backends: dict,
    rotation_state: dict,
    env: dict,
    run_id: str,
    workers: int,
) -> None:
    """Run finders concurrently against one immutable registry view, then merge serially."""
    ops.WORKSPACE.mkdir(parents=True, exist_ok=True)
    # Warm/rebuild the optional SQLite index once before children perform concurrent read-only
    # lookups. Proposal mode prevents those children from mutating either the index or YAML.
    registry.existing_keys()
    with tempfile.TemporaryDirectory(
        prefix=f"finder-proposals-{run_id}-",
        dir=ops.WORKSPACE,
    ) as temp_name:
        temp = Path(temp_name)

        def execute(index: int, name: str) -> dict:
            cfg = backends[name]
            command = finder_command(name, cfg, rotation_state)
            shown = shlex.join(command)
            proposal = temp / f"{index:03d}-{name}.json"
            rotation_hold = temp / f"{index:03d}-{name}.rotation-hold"
            rotation_next = temp / f"{index:03d}-{name}.rotation-next"
            backend_exhausted = temp / f"{index:03d}-{name}.backend-exhausted"
            child_env = env.copy()
            child_env["NEKAISE_PROPOSAL_FILE"] = str(proposal)
            child_env["NEKAISE_ROTATION_HOLD_FILE"] = str(rotation_hold)
            child_env["NEKAISE_ROTATION_NEXT_FILE"] = str(rotation_next)
            child_env["NEKAISE_BACKEND_EXHAUSTED_FILE"] = str(backend_exhausted)
            step = f"discover:{name}"
            ops.run_event(run_id, "step_started", step=step, command=shown)
            started = time.monotonic()
            result = subprocess.run(
                command,
                cwd=ROOT,
                env=child_env,
                capture_output=True,
                text=True,
            )
            elapsed = round(time.monotonic() - started, 3)
            event = "step_completed" if result.returncode == 0 else "step_failed"
            ops.run_event(
                run_id,
                event,
                step=step,
                returncode=result.returncode,
                elapsed_seconds=elapsed,
            )
            return {
                "index": index,
                "name": name,
                "command": shown,
                "proposal": proposal,
                "rotation_hold": rotation_hold.exists(),
                "rotation_next": rotation_next,
                "backend_exhausted": backend_exhausted,
                "returncode": result.returncode,
                "stdout": result.stdout,
                "stderr": result.stderr,
                "elapsed": elapsed,
            }

        print(
            f"\n== discovery: {len(selected)} backends, "
            f"{min(max(1, workers), max(1, len(selected)))} workers",
            flush=True,
        )
        results = []
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            futures = {
                pool.submit(execute, index, name): name
                for index, name in enumerate(selected)
            }
            for future in as_completed(futures):
                result = future.result()
                results.append(result)
                summary = _finder_output(result["stdout"])
                print(
                    f"  {result['name']}: exit {result['returncode']} "
                    f"in {result['elapsed']:.1f}s",
                    flush=True,
                )
                if summary:
                    print(summary, flush=True)
                if result["stderr"].strip():
                    print(result["stderr"].rstrip(), file=sys.stderr, flush=True)

        failed = [r for r in results if r["returncode"]]
        required_failed = [
            r for r in failed if backends[r["name"]].get("required", True)
        ]
        if required_failed:
            names = ", ".join(
                f"{r['name']} ({r['returncode']})" for r in required_failed
            )
            raise RuntimeError(f"discovery failed: {names}")

        optional_failed = [r for r in failed if r not in required_failed]
        if optional_failed:
            failures = {r["name"]: r["returncode"] for r in optional_failed}
            shown = ", ".join(f"{name} ({code})" for name, code in failures.items())
            print(f"discovery degraded: optional finder failure(s): {shown}", flush=True)
            ops.run_event(run_id, "discovery_degraded", failures=failures)

        successful = [r for r in results if not r["returncode"]]
        for result in successful:
            name = result["name"]
            rotates = backends[name].get("rotation", True)
            dynamic = rotates and rotation_state[name].get("dynamic", False)
            has_next = result["rotation_next"].exists()
            has_exhausted = result["backend_exhausted"].exists()
            if result["rotation_hold"] and (has_next or has_exhausted):
                raise RuntimeError(
                    f"{name}: finder reported both a rotation hold and a next/exhausted cursor"
                )
            if dynamic and not result["rotation_hold"] and not has_next:
                raise RuntimeError(f"{name}: dynamic finder did not report its next cursor")
            if not dynamic and has_next:
                raise RuntimeError(f"{name}: non-dynamic finder reported a next cursor")
            for label, path in (
                ("next cursor", result["rotation_next"]),
                ("exhaustion reason", result["backend_exhausted"]),
            ):
                if not path.exists():
                    continue
                value = path.read_text().strip()
                if not value or "\n" in value or "\r" in value or len(value) > 4096:
                    raise RuntimeError(f"{name}: invalid {label} control value")

        total, accepted = merge_proposals(successful)
        accepted = {name: accepted.get(name, 0) for name in selected}
        print(f"discovery merge: {total} unique candidates | by backend: {accepted}")
        ops.run_event(
            run_id,
            "discovery_merged",
            candidates=total,
            accepted=accepted,
        )
        successful_names = {r["name"] for r in successful}
        results_by_name = {r["name"]: r for r in successful}
        for name in selected:
            if name not in successful_names:
                continue
            if backends[name].get("rotation", True):
                if results_by_name[name]["rotation_hold"]:
                    print(f"rotation held for {name}: finder reported more candidates at this pointer")
                    ops.run_event(
                        run_id,
                        "rotation_held",
                        backend=name,
                        reason="finder_requested",
                    )
                    continue
                next_path = results_by_name[name]["rotation_next"]
                if rotation_state[name].get("dynamic"):
                    new_pointer = rotation.set_next(name, next_path.read_text().strip())
                else:
                    new_pointer = rotation.advance(name)
                ops.run_event(run_id, "rotation_advanced", backend=name, next=new_pointer)
            exhausted_path = results_by_name[name]["backend_exhausted"]
            if exhausted_path.exists():
                reason = exhausted_path.read_text().strip()
                disable_backend(name, reason)
                backends[name]["enabled"] = False
                backends[name]["reason"] = f"exhausted: {reason}"
                print(f"backend disabled for {name}: exhausted: {reason}")
                ops.run_event(run_id, "backend_disabled", backend=name, reason=reason)


def git_clean() -> bool:
    return not subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=ROOT, text=True,
    ).strip()


def doc_stats() -> tuple[int, int, int]:
    """Return training-eligible docs/tokens and successful but excluded provenance rows."""
    rows = registry.load_manifest_rows()
    restrictions = registry.load_eligibility()
    eligible, excluded = registry.partition_manifest_ok_rows(rows, restrictions)
    tokens = sum(int(r.get("text_chars") or 0) for r in eligible) // 4
    return len(eligible), tokens, len(excluded)


def commit_snapshot(before: int, after: int, tokens: int, run_id: str) -> bool:
    subprocess.run(["git", "add", *COMMIT_PATHS], cwd=ROOT, check=True)
    staged = subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT)
    if staged.returncode == 0:
        print("no tracked corpus changes to commit")
        return False
    if staged.returncode != 1:
        raise RuntimeError("git diff --cached failed")
    delta = after - before
    message = f"dig: {delta:+d} docs -> {after} docs / {tokens // 1_000_000}M tokens"
    subprocess.run(
        ["git", "commit", "-m", message, "-m", f"Corpus run: {run_id}"],
        cwd=ROOT,
        check=True,
    )
    return True


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--backend", action="append", default=[],
                    help="run only this discovery backend (repeatable)")
    ap.add_argument("--skip-discovery", action="store_true")
    ap.add_argument(
        "--discovery-workers",
        type=int,
        default=6,
        help="finder subprocesses run concurrently using isolated proposal files (default 6)",
    )
    ap.add_argument("--skip-tests", action="store_true")
    ap.add_argument("--commit", action="store_true", help="commit the validated snapshot locally")
    ap.add_argument("--push", metavar="BRANCH",
                    help="push HEAD directly to this origin branch; requires --commit")
    ap.add_argument("--allow-dirty", action="store_true",
                    help="allow an existing dirty tree (never valid with --commit/--push)")
    ap.add_argument("--lock-timeout", type=float, default=0,
                    help="seconds to wait for another corpus operator; default fail immediately")
    ap.add_argument("--run-id", default="")
    ap.add_argument("--recover", metavar="RUN_ID",
                    help="restore tracked state from an interrupted run snapshot and exit")
    args = ap.parse_args()
    if args.recover:
        run_id = (
            ops.StateSnapshot.pending()[-1]
            if args.recover == "latest" and ops.StateSnapshot.pending()
            else args.recover
        )
        if not run_id:
            print("ERROR: no pending snapshots", file=sys.stderr)
            return 1
        with ops.named_lock("corpus-round", timeout=args.lock_timeout):
            snap = ops.StateSnapshot.open(run_id)
            subprocess.run(
                ["git", "restore", "--staged", "--", *SNAPSHOT_PATHS],
                cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            snap.restore()
            snap.discard()
            ops.run_event(run_id, "run_recovered")
            print(f"restored tracked state from interrupted run {run_id}")
        return 0
    if args.push and not args.commit:
        ap.error("--push requires --commit")
    if (args.commit or args.push) and args.allow_dirty:
        ap.error("--allow-dirty cannot be combined with --commit/--push")
    if args.push:
        branch = subprocess.check_output(
            ["git", "branch", "--show-current"], cwd=ROOT, text=True,
        ).strip()
        if branch != args.push:
            ap.error(
                f"--push {args.push} requires checking out that branch first "
                f"(current: {branch or 'detached'})"
            )

    run_id = args.run_id or time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()) + "-" + uuid.uuid4().hex[:8]
    env = os.environ.copy()
    env.update({"NEKAISE_RUN_ID": run_id, "PYTHONUNBUFFERED": "1"})
    ops.run_event(run_id, "run_started", argv=sys.argv[1:])
    snapshot = None
    committed = False
    try:
        with ops.named_lock("corpus-round", timeout=args.lock_timeout):
            pending = ops.StateSnapshot.pending()
            if pending:
                raise RuntimeError(
                    "interrupted run snapshot(s) pending: "
                    f"{', '.join(pending)}; recover with --recover latest"
                )
            if not args.allow_dirty and not git_clean():
                raise RuntimeError("working tree is dirty; review/commit it before starting a round")
            backends = load_backends()
            rotation_state = rotation.load()
            if errors := validate_backends(backends, rotation_state):
                raise RuntimeError("backend configuration invalid:\n  " + "\n  ".join(errors))
            selected = args.backend or [
                name for name, cfg in backends.items() if cfg.get("enabled", True)
            ]
            unknown = [name for name in selected if name not in backends]
            if unknown:
                raise RuntimeError(f"unknown backend(s): {', '.join(unknown)}")
            disabled = [
                name for name in selected
                if not backends[name].get("enabled", True) and name not in args.backend
            ]
            selected = [name for name in selected if name not in disabled]

            before, _, _ = doc_stats()
            snapshot = ops.StateSnapshot.capture(run_id, SNAPSHOT_PATHS, root=ROOT)
            if not args.skip_discovery:
                run_finders_parallel(
                    selected,
                    backends,
                    rotation_state,
                    env,
                    run_id,
                    args.discovery_workers,
                )

            for step, script, fixed_args in PIPELINE:
                run_command(step, [sys.executable, str(SCRIPTS / script), *fixed_args], env, run_id)
            gates = [
                (step, [sys.executable, str(SCRIPTS / script), *fixed_args])
                for step, script, fixed_args in VERIFY
            ]
            if not args.skip_tests:
                gates.append(("tests", [sys.executable, "-m", "pytest", "-q"]))
            run_verify_parallel(gates, env, run_id)
            after, tokens, excluded = doc_stats()
            committed = commit_snapshot(before, after, tokens, run_id) if args.commit else False
            if args.push:
                run_command(
                    "push", ["git", "push", "origin", f"HEAD:{args.push}"], env, run_id,
                )
            ops.run_event(
                run_id, "run_completed", before_docs=before, after_docs=after,
                tokens=tokens, excluded_docs=excluded,
                committed=committed, pushed_to=args.push,
            )
            print(f"\nround {run_id}: {before} -> {after} training-eligible docs / "
                  f"{tokens // 1_000_000}M tokens ({excluded} provenance rows excluded)")
            snapshot.discard()
            return 0
    except Exception as exc:
        if snapshot is not None and not committed:
            try:
                subprocess.run(
                    ["git", "restore", "--staged", "--", *SNAPSHOT_PATHS],
                    cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                snapshot.restore()
                snapshot.discard()
                ops.run_event(run_id, "state_rolled_back")
            except Exception as rollback_exc:
                ops.run_event(run_id, "rollback_failed", error=str(rollback_exc))
        elif snapshot is not None:
            # A push failure after a successful commit is recoverable with a later git push. The
            # committed state is authoritative; retaining the pre-round snapshot would be harmful.
            snapshot.discard()
        ops.run_event(run_id, "run_failed", error=str(exc))
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
