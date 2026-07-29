#!/usr/bin/env python3
"""Run one transactional corpus growth round.

This is the single control plane used by humans, cron, and marathon:

    discover -> fetch -> prune -> clean -> check -> stats -> lint/tests -> commit [-> push]

The repository lock prevents concurrent operators. Every required command is fail-closed: a failed
step records a run-ledger event, exits non-zero, and never commits or pushes. Successful finder
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
PIPELINE = (
    ("fetch", "build_corpus.py", ()),
    ("prune", "prune_corpus.py", ("--apply",)),
    ("clean", "clean_corpus.py", ()),
    ("check", "clean_corpus.py", ("--check",)),
    ("stats", "update_readme_stats.py", ()),
    ("index", "corpus_index.py", ("status",)),
    ("lint", "lint_registry.py", ()),
)
COMMIT_PATHS = ("README.md", "registry", "manifest", "pruned_urls.txt")
SNAPSHOT_PATHS = COMMIT_PATHS


def load_backends(path: Path = BACKENDS) -> dict:
    return {k: v for k, v in json.loads(path.read_text()).items() if not k.startswith("_")}


def validate_backends(backends: dict, rotation_state: dict) -> list[str]:
    errors = []
    for name, cfg in backends.items():
        script = SCRIPTS / cfg.get("script", "")
        if not script.is_file():
            errors.append(f"{name}: missing script {script.name}")
        rotates = cfg.get("rotation", True)
        if rotates and name not in rotation_state:
            errors.append(f"{name}: missing rotation entry")
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
            child_env = env.copy()
            child_env["NEKAISE_PROPOSAL_FILE"] = str(proposal)
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
        if failed:
            names = ", ".join(f"{r['name']} ({r['returncode']})" for r in failed)
            raise RuntimeError(f"discovery failed: {names}")

        total, accepted = merge_proposals(results)
        print(f"discovery merge: {total} unique candidates | by backend: {accepted}")
        for name in selected:
            if backends[name].get("rotation", True):
                new_pointer = rotation.advance(name)
                ops.run_event(run_id, "rotation_advanced", backend=name, next=new_pointer)


def git_clean() -> bool:
    return not subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=ROOT, text=True,
    ).strip()


def doc_stats() -> tuple[int, int]:
    rows = registry.load_manifest_rows()
    ok = [r for r in rows if r.get("status") == "ok"]
    return len(ok), sum(r.get("text_chars", 0) for r in ok) // 4


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

            before, _ = doc_stats()
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
            if not args.skip_tests:
                run_command("tests", [sys.executable, "-m", "pytest", "-q"], env, run_id)
            after, tokens = doc_stats()
            committed = commit_snapshot(before, after, tokens, run_id) if args.commit else False
            if args.push:
                run_command(
                    "push", ["git", "push", "origin", f"HEAD:{args.push}"], env, run_id,
                )
            ops.run_event(
                run_id, "run_completed", before_docs=before, after_docs=after,
                tokens=tokens, committed=committed, pushed_to=args.push,
            )
            print(f"\nround {run_id}: {before} -> {after} docs / {tokens // 1_000_000}M tokens")
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
