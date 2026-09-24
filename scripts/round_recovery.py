#!/usr/bin/env python3
"""round_recovery.py — the ONE recovery routine for an interrupted or failed round (ADR 0001
stage 3, step 7).

Used by run_round's failure rollback, `run_round.py --recover` and the maintainer's automatic
recovery (maintainer.recover_pending_round), always under the canonical round lock the caller
already holds (its store writer). The order is fixed:

1. stop the processes the round owns — the caller's own new descendants, and any live process
   whose environment carries the round's NEKAISE_RUN_ID (orphans of a killed round: a fetch or a
   prune still moving bytes would race everything below);
2. unstage whatever the round staged in git (`git restore --staged` of the tracked paths);
3. resolve interrupted store transactions (finalize committed, roll back prepared) while the
   files still hold their pre- or post-images — a snapshot restore would leave them matching
   neither;
4. detect an ALREADY-COMMITTED round before touching tracked state: a first-parent commit near
   HEAD whose message carries the round's `Corpus run: <run id>` trailer (run_round's commit
   message). A committed round stands: its snapshot is never restored over it, only discarded
   (and its tracked files must still equal that commit, else nothing is changed and the
   snapshot is kept for an operator). Otherwise restore the snapshot;
5. settle the round's prune quarantine against the state the store now serves (the committed
   round's final state, or the restored pre-round state);
6. discard the snapshot — last, so any failure above leaves the round recoverable: the routine
   raises and the snapshot, the quarantine and the evidence stay for the next attempt.
"""
from __future__ import annotations

import os
import signal
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

import ops
import store

# How far back from HEAD (first parent) a committed round's commit is looked for. While a round
# snapshot is pending no other round can start, so its commit, if any, is at or near HEAD (the
# maintainer may have added publication commits since).
COMMIT_SEARCH_DEPTH = 200
TRAILER = "Corpus run: "
RUN_ENV = "NEKAISE_RUN_ID"


class RecoveryError(RuntimeError):
    pass


@dataclass
class Outcome:
    run_id: str
    action: str                                   # "restored" | "kept_committed"
    commit: str | None = None                     # the round's commit, when it had committed
    transactions: list = field(default_factory=list)   # (transaction id, action)
    quarantine: dict = field(default_factory=dict)
    stopped: list = field(default_factory=list)   # pids stopped


# --- owned processes ------------------------------------------------------------------------------

def _children_map() -> dict[int, list[int]]:
    children: dict[int, list[int]] = {}
    for stat in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = stat.read_text().rsplit(") ", 1)[1].split()
            children.setdefault(int(fields[1]), []).append(int(stat.parent.name))
        except (OSError, ValueError, IndexError):
            continue  # Processes can exit during enumeration.
    return children


def descendants(parent: int) -> set[int]:
    children = _children_map()
    found: set[int] = set()
    pending = list(children.get(parent, []))
    while pending:
        pid = pending.pop()
        if pid not in found:
            found.add(pid)
            pending.extend(children.get(pid, []))
    return found


def _ancestors() -> set[int]:
    out, cur = set(), os.getpid()
    for _ in range(128):
        out.add(cur)
        try:
            cur = int(Path(f"/proc/{cur}/stat").read_text().rsplit(")", 1)[1].split()[1])
        except (OSError, ValueError, IndexError):
            break
        if cur <= 1:
            break
    return out


def round_processes(run_id: str) -> set[int]:
    """Live processes (this user's) whose environment names round `run_id`: every step, finder
    and gate run_round starts carries NEKAISE_RUN_ID, and so do their children."""
    needle = f"{RUN_ENV}={run_id}".encode()
    mine, skip = os.getuid(), _ancestors()
    found = set()
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            pid = int(proc.name)
            if pid in skip or proc.stat().st_uid != mine:
                continue
            if needle in (proc / "environ").read_bytes().split(b"\0"):
                found.add(pid)
        except (OSError, ValueError):
            continue
    return found


def _alive(pid: int) -> bool:
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0]
    except (OSError, IndexError):
        return False
    return state != "Z"


def _reap(pids) -> None:
    for pid in pids:
        try:
            os.waitpid(pid, os.WNOHANG)
        except (ChildProcessError, OSError):
            pass  # not our child (an adopted orphan's parent reaps it)


def stop_processes(pids: set[int], grace: float = 2.0) -> list[int]:
    """SIGTERM, then SIGKILL after `grace` seconds, every pid in `pids`; wait until none is alive
    (at most `grace` more seconds after SIGKILL). Returns the pids signalled."""
    pids = {p for p in pids if p != os.getpid()}
    signalled = []
    for pid in sorted(pids):
        try:
            os.kill(pid, signal.SIGTERM)
            signalled.append(pid)
        except ProcessLookupError:
            pass
    deadline = time.monotonic() + grace
    killed = False
    while True:
        _reap(pids)
        alive = {p for p in pids if _alive(p)}
        if not alive:
            break
        if time.monotonic() >= deadline:
            if killed:
                raise RecoveryError(f"owned process(es) still alive after SIGKILL: {sorted(alive)}")
            for pid in alive:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            killed, deadline = True, time.monotonic() + grace
        time.sleep(0.05)
    return signalled


def stop_owned(run_id: str, existing: set[int] | None = None, grace: float = 2.0) -> list[int]:
    """Stop the round's processes: this process's descendants started after `existing` was
    captured (None: none of this process's descendants are the round's), plus every process
    tagged with the round's run id."""
    owned = round_processes(run_id)
    if existing is not None:
        owned |= descendants(os.getpid()) - set(existing)
    return stop_processes(owned, grace) if owned else []


# --- committed-round evidence ---------------------------------------------------------------------

def _git(root: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=root, capture_output=True, text=True)


def _in_repo_with_head(root: Path) -> bool:
    """`root` is the top of a git work tree that has at least one commit."""
    top = _git(root, "rev-parse", "--show-toplevel")
    if top.returncode or Path(top.stdout.strip()).resolve() != Path(root).resolve():
        return False
    return _git(root, "rev-parse", "--verify", "-q", "HEAD").returncode == 0


def committed_round(root: Path, run_id: str) -> str | None:
    """The commit that recorded round `run_id` (its `Corpus run: <run_id>` trailer) on HEAD's
    first-parent history within COMMIT_SEARCH_DEPTH, or None. A root that is not the top of a
    git work tree, or has no commit yet, cannot hold a committed round. Raises when git cannot
    answer — an unknown answer must never lead to a restore."""
    root = Path(root)
    if not _in_repo_with_head(root):
        return None
    log = _git(root, "log", "--first-parent", f"--max-count={COMMIT_SEARCH_DEPTH}",
               "--format=%H%x00%B%x1e", "HEAD")
    if log.returncode:
        raise RecoveryError(f"cannot read git history to check whether {run_id} committed: "
                            f"{log.stderr.strip()}")
    for record in log.stdout.split("\x1e"):
        sha, _, body = record.strip("\n").partition("\x00")
        if any(line.strip() == f"{TRAILER}{run_id}" for line in body.splitlines()):
            return sha
    return None


def _tracked_differ(root: Path, commit: str, paths) -> bool:
    diff = _git(root, "diff", "--quiet", commit, "--", *paths)
    if diff.returncode not in (0, 1):
        raise RecoveryError(f"git diff failed: {diff.stderr.strip()}")
    return diff.returncode == 1


# --- the routine ----------------------------------------------------------------------------------

def recover_round(st, writer, run_id: str, *, root: Path, snapshot_paths,
                  existing_descendants: set[int] | None = None, known_committed: bool = False,
                  grace: float = 2.0) -> Outcome:
    """Recover round `run_id` under `writer` (the caller holds the round lock): see the module
    docstring for the order. Raises (RecoveryError, StoreError, OSError, …) on any failure,
    keeping the snapshot. Emits run-ledger events for each part; the caller records the
    outcome's final event."""
    import prune_corpus

    root = Path(root)
    snap = ops.StateSnapshot.open(run_id, root=root)
    out = Outcome(run_id, "restored")
    out.stopped = stop_owned(run_id, existing_descendants, grace)
    if out.stopped:
        ops.run_event(run_id, "round_processes_stopped", pids=out.stopped)
    if _in_repo_with_head(root):
        # `git reset -- paths` (unlike `git restore --staged`) tolerates paths git does not know
        unstage = _git(root, "reset", "-q", "--", *snapshot_paths)
        if unstage.returncode:
            raise RecoveryError(f"could not unstage the round's tracked state: "
                                f"{unstage.stderr.strip()}")
    for t in st.pending_transactions():
        result = st.recover(t.run_id, writer=writer)
        ops.run_event(run_id, "store_transaction_recovered", transaction=t.run_id,
                      action=result.action)
        out.transactions.append((t.run_id, result.action))
    out.commit = committed_round(root, run_id)
    if known_committed and out.commit is None:
        raise RecoveryError(f"round {run_id} reports its commit but no commit near HEAD carries "
                            f"'{TRAILER}{run_id}': refusing to restore its snapshot")
    if out.commit is not None:
        if _tracked_differ(root, out.commit, [p for p in snapshot_paths
                                               if (root / p).exists()]):
            raise RecoveryError(
                f"round {run_id} committed as {out.commit[:12]}, but its tracked files have "
                "changed since: refusing to restore its pre-round snapshot over a committed "
                "round or to discard it — inspect the working tree")
        out.action = "kept_committed"
        ops.run_event(run_id, "round_already_committed", commit=out.commit)
    else:
        snap.restore()
    with st.recovering(writer, run_id) as token, st.read(writer=token) as view:
        out.quarantine = prune_corpus.settle_quarantines(root, view, run=run_id)
    if out.quarantine.get("quarantines"):
        ops.run_event(run_id, "prune_quarantine_settled", **out.quarantine)
    snap.discard()
    return out
