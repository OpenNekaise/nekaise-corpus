#!/usr/bin/env python3
"""round_recovery.py — the ONE recovery routine for an interrupted or failed round (ADR 0001
stage 3, step 7).

Used by run_round's failure rollback, `run_round.py --recover` and the maintainer's automatic
recovery (maintainer.recover_pending_round), always under the canonical round lock the caller
already holds (its store writer). The order is fixed:

1. stop the processes the round owns — the caller's own new descendants, and any live process
   whose environment carries the round's NEKAISE_RUN_ID (orphans of a killed round: a fetch or a
   prune still moving bytes would race everything below);
2. unstage whatever the round staged in git (`git reset -q -- <tracked paths>`);
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

Under PostgreSQL authority (ADR 0001 stage 4 step 4; selected only by the host authority record)
there is no snapshot and no commit: `recover_staged` is the same routine over durable run status —
ownership (the caller's writer), stop the runs' processes, drain the broker, then the database
decides: a promoted run stands (even after a lost reply) and its completion is finished, an
unpromoted one is aborted (resuming is explicit: run_round.py --resume), an unknown outcome
raises before anything is mutated; temporaries are swept.
"""
from __future__ import annotations

import json
import os
import signal
import stat as stat_mod
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
# Names the run whose coordinator started a process, for recovery only (no pipeline step reads
# it, unlike NEKAISE_RUN_ID, which keys the loader/pruner handoff and batch identities).
OWNER_ENV = "NEKAISE_RUN_OWNER"
_os_stat = os.stat  # repository discovery stats (tests inject I/O errors here)


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


OWNER_MARK = "nekaise-run-owner."


def _holds_mark(proc: Path, mark: str) -> bool:
    """Whether process `proc` holds an open file descriptor on run ownership mark `mark` (the
    file may be deleted meanwhile: its link then ends in " (deleted)")."""
    for fd in os.scandir(proc / "fd"):
        try:
            target = os.readlink(fd.path)
        except FileNotFoundError:
            continue   # closed meanwhile
        name = target.removesuffix(" (deleted)").rsplit("/", 1)[-1]
        if name == mark:
            return True
    return False


def round_processes(run_id: str) -> set[int]:
    """Live processes (this user's) that belong to run `run_id`: those whose environment names
    it — every step, finder and gate run_round starts carries NEKAISE_RUN_ID from exec, and so
    do their children — and those holding its ownership mark (OwnershipMark: a descriptor every
    process forked by the run's coordinator inherits, including fork-only workers whose
    /proc/<pid>/environ still shows the environment of their original exec). A process that
    exits during the scan is skipped; any other error reading one of this user's processes
    raises — ownership that cannot be read is never read as "not the run's"."""
    needles = {f"{RUN_ENV}={run_id}".encode(), f"{OWNER_ENV}={run_id}".encode()}
    mark = OWNER_MARK + run_id
    mine, skip = os.getuid(), _ancestors()
    found = set()
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            pid = int(proc.name)
            if pid in skip or proc.stat().st_uid != mine:
                continue
            if needles & set((proc / "environ").read_bytes().split(b"\0")) \
                    or _holds_mark(proc, mark):
                found.add(pid)
        except (FileNotFoundError, ProcessLookupError):
            continue   # exited during the scan
        except ValueError:
            continue   # not a pid
        except PermissionError:
            # a process of this user made non-dumpable (ssh-agent, gpg-agent, a setuid exec):
            # the kernel hides its environment and descriptors. The run's processes are plain
            # interpreters and tools and stay dumpable; guessing otherwise would make every
            # recovery on such a host fail.
            continue
        except OSError as exc:
            if not _alive(int(proc.name)):
                continue
            raise RecoveryError(f"cannot read process {proc.name} while looking for run "
                                f"{run_id}'s processes: {exc}") from exc
    return found


class OwnershipMark:
    """A run coordinator's process ownership mark: an inheritable read descriptor on
    `<root>/workspace/run-owners/nekaise-run-owner.<run id>`, held for as long as the run's
    lifecycle is open in this process. Every process it forks (a loader's extraction pool, a
    cleaner's workers — without exec) inherits the descriptor, so recovery from ANY process can
    find them after the coordinator died (round_processes), whatever their environment shows.
    Exec'd children are found by their environment: NEKAISE_RUN_ID, or NEKAISE_RUN_OWNER, which
    `tag()` sets in the coordinator so every child it execs from then on carries it."""

    def __init__(self, root: Path, run_id: str):
        store._check_run_id(run_id)
        self.dir = Path(root) / "workspace" / "run-owners"
        self.path = self.dir / (OWNER_MARK + run_id)
        self.run_id = run_id
        self.fd: int | None = None
        self._saved_env: str | None = None
        self._tagged = False

    def __enter__(self) -> "OwnershipMark":
        # the coordinator's identity (pid + start time: pids are reused) — so a later recovery
        # can tell a dead coordinator's orphans from a live coordinator's workers WITHOUT the
        # writer lock, which a dead coordinator's forks may still hold (they inherited its
        # database session: stop_orphans_of_dead_coordinators)
        ops.atomic_write_text(self.path, json.dumps(
            {"run": self.run_id, "pid": os.getpid(), "start": process_start(os.getpid())}) + "\n")
        self.fd = os.open(self.path, os.O_RDONLY)
        os.set_inheritable(self.fd, True)
        return self

    def tag(self) -> None:
        """Also name the run in this process's environment (NEKAISE_RUN_OWNER), so children
        exec'd from here on (subprocesses, spawned workers, an agent and its tools) carry it in
        their initial environment. Restored on exit."""
        if not self._tagged:
            self._saved_env = os.environ.get(OWNER_ENV)
            os.environ[OWNER_ENV] = self.run_id
            self._tagged = True

    def __exit__(self, *exc) -> None:
        if self._tagged:
            if self._saved_env is None:
                os.environ.pop(OWNER_ENV, None)
            else:
                os.environ[OWNER_ENV] = self._saved_env
            self._tagged = False
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        self.path.unlink(missing_ok=True)


def process_start(pid: int) -> str | None:
    """A process's start time (clock ticks since boot, /proc/<pid>/stat field 22), which with
    its pid identifies it across pid reuse; None when there is no such process. Other errors
    raise."""
    try:
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
    except (FileNotFoundError, ProcessLookupError):
        return None


def stop_orphans_of_dead_coordinators(root: Path, grace: float = 2.0) -> dict[str, list]:
    """Before taking the writer: stop the processes of every run whose coordinator is DEAD (its
    ownership mark names a pid/start time that no longer runs). A fork of a dead coordinator
    still holds the coordinator's database session — and with it the writer lock — so ownership
    could never be acquired while it lives. A live coordinator's processes are never touched;
    what the dead run's status becomes is decided afterwards, under the writer
    (recover_staged). A mark that cannot be read raises. Returns {run id: pids stopped}."""
    marks = Path(root) / "workspace" / "run-owners"
    try:
        entries = sorted(os.scandir(marks), key=lambda e: e.name)
    except FileNotFoundError:
        return {}
    out = {}
    for entry in entries:
        if not entry.name.startswith(OWNER_MARK):
            continue
        run_id = entry.name[len(OWNER_MARK):]
        try:
            doc = json.loads(Path(entry.path).read_text())
            pid, start = int(doc["pid"]), doc["start"]
        except FileNotFoundError:
            continue   # its coordinator finished meanwhile
        except (ValueError, KeyError, TypeError) as exc:
            raise RecoveryError(f"ownership mark {entry.path} is unreadable ({exc}): refusing to "
                                "guess whose processes are orphans") from exc
        if pid == os.getpid() or process_start(pid) == start:
            continue   # a live coordinator
        pids = round_processes(run_id)
        out[run_id] = stop_processes(pids, grace) if pids else []
        if out[run_id]:
            ops.run_event(run_id, "round_processes_stopped", pids=out[run_id],
                          coordinator="dead")
    return out


def sweep_owner_mark(root: Path, run_id: str) -> None:
    """Remove a dead coordinator's ownership mark (after its processes were stopped)."""
    (Path(root) / "workspace" / "run-owners" / (OWNER_MARK + run_id)).unlink(missing_ok=True)


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


def _enclosing_git(root: Path) -> Path | None:
    """The nearest `.git` (directory or gitfile) at or above `root`, found WITHOUT asking git:
    whether a repository exists must not depend on git answering. Like git's own discovery it
    stops at a filesystem boundary (GIT_DISCOVERY_ACROSS_FILESYSTEM unset).
    Absence is only ever POSITIVELY established: a stat that fails with ENOENT. Any other error
    (EIO, EACCES, …) while discovering raises RecoveryError — it must not read as "no
    repository"."""
    here = Path(root).resolve()
    device = _stat(here).st_dev
    for d in (here, *here.parents):
        if _stat(d).st_dev != device:
            return None  # a filesystem boundary: git's discovery stops here too
        dot = d / ".git"
        st = _stat(dot, missing_ok=True)
        if st is None:
            continue
        if stat_mod.S_ISREG(st.st_mode):
            return dot  # a gitfile (worktree / submodule)
        if stat_mod.S_ISDIR(st.st_mode):
            # ANY repository part makes it a repository (a corrupt one still counts); only an
            # empty stray `.git` directory, which git ignores too, does not
            if any(_stat(dot / part, missing_ok=True) is not None
                   for part in ("HEAD", "objects", "refs", "config")):
                return dot
            continue
        raise RecoveryError(f"{dot} is neither a directory nor a gitfile; refusing to guess")
    return None


def _stat(path: Path, *, missing_ok: bool = False) -> os.stat_result | None:
    """os.stat that returns None ONLY for ENOENT (when `missing_ok`) and turns every other error
    into RecoveryError."""
    try:
        return _os_stat(path)
    except FileNotFoundError:
        if missing_ok:
            return None
        raise RecoveryError(f"{path} vanished during repository discovery") from None
    except OSError as exc:
        raise RecoveryError(f"cannot stat {path} during repository discovery ({exc}); "
                            "refusing to decide whether the round committed") from exc


def _no_refs_on_disk(gitdir: Path) -> bool:
    """No loose ref file under refs/ and no ref line in packed-refs (errors raise)."""
    packed = gitdir / "packed-refs"
    if _stat(packed, missing_ok=True) is not None:
        try:
            lines = packed.read_text().splitlines()
        except OSError as exc:
            raise RecoveryError(f"cannot read {packed}: {exc}") from exc
        if any(line and not line.startswith(("#", "^")) for line in lines):
            return False
    refs = gitdir / "refs"
    if _stat(refs, missing_ok=True) is None:
        return True

    def onerror(exc):
        raise RecoveryError(f"cannot list {refs}: {exc}") from exc
    for _, _, files in os.walk(refs, onerror=onerror):
        if files:
            return False
    return True


def repo_state(root: Path) -> str:
    """"none" (no repository encloses `root`: nothing can have been committed), "unborn" (a
    repository without any commit or ref: likewise), or "head" (HEAD names a commit). Every other
    outcome raises RecoveryError — git failing in any other way, a corrupt or dangling HEAD, a
    missing branch while refs exist: an unknown answer must never lead to a restore.

    "unborn" requires the exact outcome of an unborn branch and nothing else: `rev-parse
    --verify -q HEAD` exits 1 with no output at all, `symbolic-ref -q HEAD` names a branch with
    no stderr, `for-each-ref` succeeds silently with no ref, `.git` is a directory, and neither
    loose refs nor packed-refs hold any ref."""
    gitdir = _enclosing_git(root)
    if gitdir is None:
        return "none"
    head = _git(root, "rev-parse", "--verify", "-q", "HEAD^{commit}")
    if head.returncode == 0 and head.stdout.strip() and not head.stderr.strip():
        return "head"
    sym = _git(root, "symbolic-ref", "-q", "HEAD")
    refs = _git(root, "for-each-ref", "--count=1", "--format=%(refname)")
    unborn = (head.returncode == 1 and not head.stdout.strip() and not head.stderr.strip()
              and sym.returncode == 0 and sym.stdout.strip().startswith("refs/heads/")
              and not sym.stderr.strip()
              and refs.returncode == 0 and not refs.stdout.strip() and not refs.stderr.strip()
              and stat_mod.S_ISDIR(_stat(gitdir).st_mode) and _no_refs_on_disk(gitdir))
    if unborn:
        return "unborn"
    detail = " / ".join(f"{name} exit {r.returncode}{': ' + r.stderr.strip() if r.stderr.strip() else ''}"
                        for name, r in (("rev-parse", head), ("symbolic-ref", sym),
                                        ("for-each-ref", refs)))
    raise RecoveryError(f"cannot establish git state at {root} ({detail}); refusing to decide "
                        "whether the round committed — the snapshot is kept")


def committed_round(root: Path, run_id: str) -> str | None:
    """The commit that recorded round `run_id` (its `Corpus run: <run_id>` trailer) on HEAD's
    first-parent history within COMMIT_SEARCH_DEPTH, or None. Only a root outside any
    repository, or an unborn repository, answers None without history; any git failure raises
    (repo_state) — an unknown answer must never lead to a restore."""
    root = Path(root)
    if repo_state(root) != "head":
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
    if repo_state(root) != "none":  # raises when git cannot be inspected
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
        # every snapshot path, present or not: git diff reports a deleted tracked path too
        if _tracked_differ(root, out.commit, list(snapshot_paths)):
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


# --- staged runs (PostgreSQL authority, ADR 0001 stage 4 step 4) ------------------------------------

@dataclass
class StagedOutcome:
    run_id: str
    status: str | None            # the durable status found: open | frozen | promoted | aborted,
                                  # None: the run was never opened (nothing was staged)
    action: str                   # kept_promoted | aborted | already_aborted | not_opened
    stopped: list = field(default_factory=list)
    detail: dict = field(default_factory=dict)


def sweep_run_temporaries(root: Path, run_id: str) -> int:
    """Remove the temporary directories an interrupted round's process left in the workspace
    (finder proposal directories: TemporaryDirectory cannot clean up after SIGKILL) — only the
    named run's. Returns how many went; errors other than a missing workspace raise."""
    import shutil
    workspace = Path(root) / "workspace"
    try:
        entries = list(os.scandir(workspace))
    except FileNotFoundError:
        return 0
    removed = 0
    prefix = f"finder-proposals-{run_id}-"
    for entry in entries:
        if entry.name.startswith(prefix) and entry.is_dir(follow_symlinks=False):
            shutil.rmtree(entry.path)
            removed += 1
    return removed


def recover_staged(st, writer, run_id: str | None = None, *, root: Path,
                   existing_descendants: set[int] | None = None, broker=None,
                   grace: float = 2.0, reason: str = "recovered: its coordinator is gone",
                   stop: bool = True, finish: bool = True) -> list[StagedOutcome]:
    """Recover staged runs under `writer` (the caller holds the PostgreSQL writer: ownership is
    acquired). The order is fixed:

    1. which runs: `run_id`, or every open or frozen run (a durable query; a failure raises
       before anything is touched);
    2. stop their processes — the caller's new descendants and every live process tagged with
       one of their run ids (orphans still downloading or cleaning) — unless `stop` is False
       (the caller already did);
    3. drain `broker` (the caller's, when it served the run) — always, even when stopping
       failed: after this nothing can stage;
    4. query each run's DURABLE status afresh and decide by it alone — a promoted run stands
       (even when the promotion's reply was lost), an unpromoted one is aborted (the default:
       resuming is explicit, run_round.py --resume), an aborted one needs nothing. A status that
       cannot be read raises: an unknown database outcome never leads to a mutation;
    5. sweep what the stopped processes left: artifact temporaries of dead writers and the
       runs' finder proposal directories;
    6. `finish`: complete the current generation (staged_runs.after_promotion: the corpus/
       materialization and bounded fold/purge housekeeping) — idempotent, so a promoted run
       whose completion was interrupted converges here.

    Returns one outcome per run; emits run-ledger events. Any failure raises; the runs then keep
    their durable status for the next attempt."""
    import artifact_store
    import staged_runs
    import store_staging

    root = Path(root)
    if run_id is not None:
        targets = [store._check_run_id(run_id)]
    else:
        targets = [r["run_id"] for r in store_staging.unfinished_runs(st, writer)]
    stopped: dict[str, list] = {}
    try:
        if stop:
            owned = set(existing_descendants) if existing_descendants is not None else None
            for rid in targets:
                stopped[rid] = stop_owned(rid, owned, grace)
                owned = None   # the caller's descendants are stopped once
                if stopped[rid]:
                    ops.run_event(rid, "round_processes_stopped", pids=stopped[rid])
            if not targets and owned is not None:
                stop_processes(descendants(os.getpid()) - owned, grace)
    finally:
        if broker is not None:
            broker.drain()
    outcomes = []
    for rid in targets:
        run = store_staging.run_status(st, writer, rid)   # raises on any database failure
        out = StagedOutcome(rid, None if run is None else run["status"], "not_opened",
                            stopped.get(rid, []))
        if run is None:
            pass   # its opening never committed: nothing was staged under this id
        elif run["status"] == "promoted":
            out.action = "kept_promoted"
            out.detail["generation"] = run["promoted_generation"]
            ops.run_event(rid, "staged_run_kept", generation=run["promoted_generation"])
        elif run["status"] == "aborted":
            out.action = "already_aborted"
        elif run["status"] in ("open", "frozen"):
            store_staging.abort_run(st, writer, rid, reason=reason[:500])
            after = store_staging.run_status(st, writer, rid)
            if after is None or after["status"] != "aborted":
                raise RecoveryError(f"run {rid} is {None if after is None else after['status']} "
                                    "after aborting it")
            out.action = "aborted"
            ops.run_event(rid, "staged_run_aborted", was=run["status"], reason=reason[:500])
        else:
            raise RecoveryError(f"run {rid} has an unknown status {run['status']!r}")
        out.detail["swept_proposals"] = sweep_run_temporaries(root, rid)
        if stop:
            sweep_owner_mark(root, rid)
        outcomes.append(out)
    swept = artifact_store.LocalArtifacts(root).sweep_incoming()
    finished = staged_runs.after_promotion(st, writer, root) if finish else None
    for out in outcomes:
        out.detail["swept_incoming"] = swept
        if finished is not None:
            out.detail["finish"] = finished
    return outcomes
