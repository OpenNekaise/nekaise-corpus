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

Discovery ends in ONE store transaction (ADR 0001 stage 3, step 5): the merged accepted entries,
rotation pointer moves, find_github's completed passes and finder-reported exhaustion (runtime
backend state, registry/backend_state.json — never registry/backends.json). A backend runs when
its configuration AND its runtime state enable it.
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
import corpus_stats
import dedup
import store
import store_broker

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
BACKENDS = ROOT / "registry" / "backends.json"
GITHUB_PASSES = "github_passes.json"
# Runtime reason prefix for finder-reported exhaustion (registry/backend_state.json).
EXHAUSTED = "exhausted: "
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


def runtime_state_errors(backends: dict, runtime: dict) -> list[str]:
    """Runtime backend state (registry/backend_state.json via the store) may only describe
    configured backends, must be well-formed, and never carries policy: a policy block belongs in
    the git-owned configuration, where check_contracts requires it."""
    errors = []
    for name, state in sorted(runtime.items()):
        if name not in backends:
            errors.append(f"{name}: runtime backend state names an unknown backend")
        if not isinstance(state, store.BackendState):
            errors.append(f"{name}: runtime backend state is malformed: {state!r}")
            continue
        if not isinstance(state.enabled, bool):
            errors.append(f"{name}: runtime enabled must be true or false")
        if state.reason is not None and (not isinstance(state.reason, str)
                                         or not state.reason.strip()):
            errors.append(f"{name}: runtime reason must be a non-empty string or null")
        elif state.enabled is False and state.reason is None:
            errors.append(f"{name}: runtime-disabled backend lacks a reason")
        elif isinstance(state.reason, str) and state.reason.startswith("policy-blocked"):
            errors.append(f"{name}: policy blocks belong in registry/backends.json, not runtime "
                          "state")
    return errors


def validate_backends(backends: dict, rotation_state: dict,
                      runtime: dict | None = None) -> list[str]:
    """Control-plane errors of the backend configuration, its rotation entries and (when given)
    its runtime state ({name: store.BackendState}, e.g. ReadView.backend_state_get())."""
    errors = runtime_state_errors(backends, runtime) if runtime is not None else []
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


def run_verify_parallel(gates: list[tuple[str, list[str]]], env: dict, run_id: str,
                        envs: dict[str, dict] | None = None) -> None:
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
        result = subprocess.run(cmd, cwd=ROOT, env=(envs or {}).get(step, env),
                                capture_output=True, text=True)
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


def _rotation_hold_detail(path: Path) -> str:
    """Read a bounded display note; missing or unreadable notes never invalidate a hold."""
    try:
        with path.open(encoding="utf-8", errors="replace") as handle:
            line = handle.readline(4096)
    except OSError:
        return ""
    return "".join(char for char in line if char.isprintable()).strip()[:512]


def merge_proposals(view, results: list[dict]) -> tuple[list[dict], dict[str, int], dict]:
    """Merge successful finders' proposal files in backend order, deduplicating across concurrent
    finders and against the store.

    `view` is the round's discovery transaction, asked before it writes anything (so the file
    store answers from its index). Membership and id suffixing are exactly the legacy
    existing_keys()/uniquify_ids() semantics (see scripts/dedup.py). Returns the accepted entries
    (ids made unique), the accepted count per finder, and the github passes find_github staged
    ({bucket: {kind: date}}), merged in the same order."""
    keys = dedup.from_view(view)
    merged: list[dict] = []
    accepted: dict[str, int] = {}
    passes: dict = {}
    for result in sorted(results, key=lambda r: r["index"]):
        doc = registry.read_proposal(result["proposal"])
        entries = doc["entries"]
        passes = registry.merge_github_passes(passes, doc.get("github_passes") or {})
        count = 0
        for entry in entries:
            missing = [field for field in registry.REQUIRED_FIELDS if not entry.get(field)]
            if missing:
                raise RuntimeError(
                    f"{result['name']} proposed {entry.get('id', '<no id>')} without "
                    f"{', '.join(missing)}"
                )
        keys.prefetch(**dedup.page_keys(entries), ids=[e["id"] for e in entries])
        for entry in entries:
            url = entry["url"].rstrip("/")
            title = registry.norm(entry["title"])
            if url in keys.urls or title in keys.titles:
                continue
            keys.uniquify_ids([entry])
            keys.urls.add(url)
            keys.titles.add(title)
            merged.append(entry)
            count += 1
        accepted[result["name"]] = count
    return merged, accepted, passes


def apply_discovery(tx, successful: list[dict], selected: list[str],
                    backends: dict) -> tuple[int, dict[str, int], list[tuple]]:
    """Record one discovery phase in the round's discovery transaction `tx`: the merged accepted
    entries, find_github's staged passes, every successful rotating finder's pointer move (holds
    keep theirs; failed finders are not in `successful`, so they keep theirs too) and
    finder-reported exhaustion as runtime backend state. Returns (accepted total, accepted per
    finder, [(message or None, run event, fields)]) for the caller to report after commit."""
    merged, accepted, passes = merge_proposals(tx, successful)
    if merged:
        tx.insert_entries(merged)
    if passes:
        current = tx.control_get(GITHUB_PASSES) or {}
        recorded = registry.merge_github_passes(current, passes)
        if recorded != current:
            tx.control_set(GITHUB_PASSES, recorded)
    notes: list[tuple] = []
    by_name = {r["name"]: r for r in successful}
    for name in selected:
        if name not in by_name:
            continue
        result = by_name[name]
        if backends[name].get("rotation", True):
            if result["rotation_hold"]:
                detail = result["rotation_hold_detail"]
                notes.append((
                    f"rotation held for {name}: {detail or 'finder requested hold'}",
                    "rotation_held",
                    {"backend": name, "reason": "finder_requested",
                     **({"detail": detail} if detail else {})},
                ))
                continue
            entry = tx.rotation_get(name)
            if entry.get("dynamic"):
                entry = rotation.with_next(name, entry,
                                           result["rotation_next"].read_text().strip())
            else:
                entry = rotation.advanced(name, entry)
            tx.rotation_set(name, entry)
            notes.append((None, "rotation_advanced",
                          {"backend": name, "next": rotation.pointer_arg(entry)}))
        exhausted_path = result["backend_exhausted"]
        if exhausted_path.exists():
            reason = exhausted_path.read_text().strip()
            # Runtime state, never the git-owned configuration (ADR 0001 section 2).
            tx.backend_state_set(name, store.BackendState(False, f"{EXHAUSTED}{reason}"))
            notes.append((f"backend disabled for {name}: {EXHAUSTED}{reason}",
                          "backend_disabled", {"backend": name, "reason": reason}))
    return len(merged), accepted, notes


def warm_index(st, writer) -> None:
    """Build or refresh the file store's membership index once, before the finders' concurrent
    read-only lookups and the discovery merge, so none of them rebuilds it (or, if it is
    unavailable, parses every shard) on its own."""
    with st.read(writer=writer) as view:
        view.known(urls=["https://index-warmup.invalid"])


def run_finders_parallel(
    selected: list[str],
    backends: dict,
    rotation_state: dict,
    env: dict,
    run_id: str,
    workers: int,
    transaction,
) -> None:
    """Run finders concurrently against one immutable store generation, then record the whole
    discovery phase in ONE store transaction: `transaction()` returns a context manager yielding
    the round's write view (run_round: broker.local_batch("discover", "merge"))."""
    ops.WORKSPACE.mkdir(parents=True, exist_ok=True)
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
                "rotation_hold_detail": _rotation_hold_detail(rotation_hold),
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

        successful = check_finder_results(results, backends, rotation_state, run_id)
        with transaction() as tx:
            total, accepted, notes = apply_discovery(tx, successful, selected, backends)
        accepted = {name: accepted.get(name, 0) for name in selected}
        print(f"discovery merge: {total} unique candidates | by backend: {accepted}")
        ops.run_event(
            run_id,
            "discovery_merged",
            candidates=total,
            accepted=accepted,
        )
        for message, event, fields in notes:
            if message:
                print(message)
            ops.run_event(run_id, event, **fields)


def check_finder_results(results: list[dict], backends: dict, rotation_state: dict,
                         run_id: str) -> list[dict]:
    """Fail on required finder failures and side-channel protocol violations; report optional
    failures as degraded discovery. Returns the successful results."""
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
    return successful


def git_clean() -> bool:
    return not subprocess.check_output(
        ["git", "status", "--porcelain"], cwd=ROOT, text=True,
    ).strip()


def doc_stats(view) -> tuple[int, int, int]:
    """Return training-eligible docs/tokens and successful but excluded provenance rows."""
    stats = corpus_stats.compute(view, registry.load_eligibility())
    return stats.documents, stats.tokens, stats.excluded


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


NESTED_ROUND_HELP = (
    "validate with the read-only gates instead — python scripts/clean_corpus.py --check; "
    "python scripts/lint_registry.py; python scripts/check_contracts.py; "
    "python -m pytest -q tests/ — and leave growth to the next scheduled round"
)


def nested_round_owner(st) -> str | None:
    """Why this process must not start a round, or None. A round needs the canonical round lock
    as its writer; a process whose ancestor already holds it (a maintenance window's agent, a
    round's own step) would only wait on its own parent, so it is refused at once. A stale
    inherited entry (no ancestor holds the lock any more) does not count."""
    try:
        run = st._inherited_run()
    except store.WriterError:
        run = None
    holders = [h for h in ops.inherited_holders()
               if h.get("lock") == str((st.workspace / f".{store.ROUND_LOCK}.lock").resolve())]
    if run is None and store_broker.client() is None:
        return None
    owner = next((f"pid {h.get('pid')}" for h in holders), "the parent process")
    return ("run_round.py cannot run nested: the corpus-round lock is already held by an ancestor "
            f"({owner}{', round ' + run if run else ''}; e.g. the maintainer's window) and a round "
            f"needs it as its own writer. {NESTED_ROUND_HELP}.")


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
    if nested := nested_round_owner(store.FileStore(ROOT)):
        print(f"ERROR: {nested}", file=sys.stderr)
        return 2
    if args.recover:
        run_id = (
            ops.StateSnapshot.pending()[-1]
            if args.recover == "latest" and ops.StateSnapshot.pending()
            else args.recover
        )
        if not run_id:
            print("ERROR: no pending snapshots", file=sys.stderr)
            return 1
        st = store.FileStore(ROOT)
        # The recovering writer is the round lock plus a token that may read the restored state
        # while the round's snapshot still exists: the snapshot is discarded only after the
        # prune quarantine is settled against that state, so a failure leaves it recoverable.
        with st.writer(timeout=args.lock_timeout, round_id=run_id, recovering=True) as writer:
            snap = ops.StateSnapshot.open(run_id, root=ROOT)
            subprocess.run(
                ["git", "restore", "--staged", "--", *SNAPSHOT_PATHS],
                cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            try:
                # interrupted store transactions first, while the files still hold their pre-
                # or post-images; the snapshot restore would leave them matching neither
                recover_store_transactions(st, writer, run_id)
                snap.restore()
                settle_prune_quarantine(ROOT, st, writer, run_id)
            except Exception as exc:
                ops.run_event(run_id, "recover_failed", error=str(exc))
                print(f"ERROR: could not recover {run_id} completely (store transactions, "
                      f"snapshot restore, prune quarantine): {exc}; the snapshot is kept — fix "
                      "and re-run --recover", file=sys.stderr)
                return 1
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
    st = store.FileStore(ROOT)
    try:
        # The round's single store writer: the canonical round lock plus the token proving it,
        # declaring this round so its own snapshot is not "unsettled" state (ADR 0001 stage 3).
        # Failure rollback happens inside this scope, i.e. still under the lock.
        with st.writer(timeout=args.lock_timeout, round_id=run_id) as writer:
            return _locked_round(args, st, writer, run_id, env)
    except Exception as exc:
        ops.run_event(run_id, "run_failed", error=str(exc))
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


def recover_store_transactions(st, writer, run_id: str) -> list:
    """Resolve every interrupted store transaction (finalize a committed one, roll back a
    prepared one) BEFORE a round snapshot is restored: FileStore recovery restores a file only
    while it still holds the transaction's pre- or post-image, which the restored pre-round
    state (e.g. after an earlier checkpoint of the same round changed the shard) does not."""
    done = []
    for t in st.pending_transactions():
        result = st.recover(t.run_id, writer=writer)
        ops.run_event(run_id, "store_transaction_recovered", transaction=t.run_id,
                      action=result.action)
        done.append(result)
    return done


def settle_prune_quarantine(root: Path, st, writer, run_id: str | None = None) -> dict:
    """Settle the bytes pruned documents left in workspace/prune-quarantine/ (all, or round
    `run_id`'s) against the store state the writer now reads — the round's final state after
    success, its restored pre-round state after a rollback: a file goes back when its document's
    row exists there, otherwise it is deleted (prune_corpus.settle_quarantine). Raises when it
    cannot finish; callers keep their recovery state until it has."""
    import prune_corpus

    with st.read(writer=writer) as view:
        counts = prune_corpus.settle_quarantines(root, view, run=run_id)
    if counts["quarantines"]:
        ops.run_event(run_id or "-", "prune_quarantine_settled", **counts)
    return counts


# Pipeline steps that may write tracked state receive the round's store broker; verification gates
# and discovery workers only get inherited read access.
MUTATING_STEPS = frozenset({"fetch", "prune", "clean"})


def _locked_round(args, st, writer, run_id: str, env: dict) -> int:
    snapshot = None
    committed = False
    try:
        if (ROOT / "workspace" / ".pg-shadow").exists() and not args.commit:
            raise RuntimeError(
                "a PostgreSQL shadow replicates commits (workspace/.pg-shadow): rounds must "
                "--commit, or the shadow silently misses their changes"
            )
        if store_pending := st.pending_transactions():
            raise RuntimeError(
                "interrupted store transaction(s) pending: "
                + ", ".join(t.run_id for t in store_pending)
                + "; recover them with FileStore.recover() before starting a round"
            )
        pending = ops.StateSnapshot.pending()
        if pending:
            raise RuntimeError(
                "interrupted run snapshot(s) pending: "
                f"{', '.join(pending)}; recover with --recover latest"
            )
        if not args.allow_dirty and not git_clean():
            raise RuntimeError("working tree is dirty; review/commit it before starting a round")
        # bytes an interrupted standalone prune left aside are settled before anything fetches
        settle_prune_quarantine(ROOT, st, writer)
        with st.read(writer=writer) as view:
            backends = {k: v for k, v in view.config_get().backends.items()
                        if not k.startswith("_")}
            rotation_state = view.rotation_get()
            runtime = view.backend_state_get()
            if errors := validate_backends(backends, rotation_state, runtime):
                raise RuntimeError("backend configuration invalid:\n  " + "\n  ".join(errors))
            # Effective enablement: the git-owned configuration AND the runtime state.
            enabled = {name: view.backend_enabled(name) for name in backends}
        selected = args.backend or [name for name in backends if enabled[name]]
        unknown = [name for name in selected if name not in backends]
        if unknown:
            raise RuntimeError(f"unknown backend(s): {', '.join(unknown)}")
        disabled = [
            name for name in selected
            if not enabled[name] and name not in args.backend
        ]
        selected = [name for name in selected if name not in disabled]

        with st.read(writer=writer) as view:
            before, _, _ = doc_stats(view)
        snapshot = ops.StateSnapshot.capture(run_id, SNAPSHOT_PATHS, root=ROOT)
        read_env = ops.with_holder(env, os.getpid(), st.workspace / ".corpus-round.lock", run_id)
        broker = store_broker.Broker(st, writer, run_id)
        with broker.serving():
            write_env = {**read_env, **broker.env()}
            if not args.skip_discovery:
                warm_index(st, writer)
                run_finders_parallel(
                    selected,
                    backends,
                    rotation_state,
                    read_env,
                    run_id,
                    args.discovery_workers,
                    lambda: broker.local_batch("discover", "merge"),
                )

            for step, script, fixed_args in PIPELINE:
                run_command(step, [sys.executable, str(SCRIPTS / script), *fixed_args],
                            write_env if step in MUTATING_STEPS else read_env, run_id)
        gates = [
            (step, [sys.executable, str(SCRIPTS / script), *fixed_args])
            for step, script, fixed_args in VERIFY
        ]
        if not args.skip_tests:
            # tests/ only: stray files elsewhere (e.g. review scratch in workspace/) must never
            # decide a round
            gates.append(("tests", [sys.executable, "-m", "pytest", "-q", "tests/"]))
        # pytest builds throwaway stores of its own; the round's inherited lock is not theirs, so
        # the test gate runs with the plain environment (no inherited lock, no broker).
        run_verify_parallel(gates, read_env, run_id, envs={"tests": env})
        with st.read(writer=writer) as view:
            after, tokens, excluded = doc_stats(view)
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
        try:  # the round stands; a leftover quarantine is settled before the next round fetches
            settle_prune_quarantine(ROOT, st, writer, run_id)
        except Exception as exc:
            ops.run_event(run_id, "prune_quarantine_unsettled", error=str(exc))
            print(f"WARNING: prune quarantine of {run_id} not settled: {exc}", file=sys.stderr)
        return 0
    except Exception:
        if snapshot is not None and not committed:
            try:
                subprocess.run(
                    ["git", "restore", "--staged", "--", *SNAPSHOT_PATHS],
                    cwd=ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                )
                recover_store_transactions(st, writer, run_id)
                snapshot.restore()
                # settle against the restored state BEFORE discarding the snapshot: a failure
                # keeps the round recoverable (run_round --recover) instead of stranding bytes
                settle_prune_quarantine(ROOT, st, writer, run_id)
                snapshot.discard()
                ops.run_event(run_id, "state_rolled_back")
            except Exception as rollback_exc:
                ops.run_event(run_id, "rollback_failed", error=str(rollback_exc))
        elif snapshot is not None:
            # A push failure after a successful commit is recoverable with a later git push. The
            # committed state is authoritative; retaining the pre-round snapshot would be harmful.
            snapshot.discard()
        raise


if __name__ == "__main__":
    raise SystemExit(main())
