#!/usr/bin/env python3
"""run_ownership.py — which processes belong to a staged run's coordinator attempt, and how
recovery stops them (ADR 0001 stage 4 step 4; PostgreSQL authority only).

The legacy file-store recovery (round_recovery.round_processes / stop_owned) is untouched: it
matches NEKAISE_RUN_ID exactly as before. Staged recovery opts into THIS scanner, which is scoped
to one coordinator ATTEMPT of one run of one data root:

* the ownership MARK: `<root>/workspace/run-owners/nekaise-run-owner.<run id>`, written atomically
  by the coordinator when its run's lifecycle opens (`Mark`). It records the attempt — the root
  key, the run id, a random nonce — and the coordinator's identity: pid, start time, boot id and
  PID namespace. The coordinator keeps an inheritable descriptor open on it, so every process it
  forks (fork-only workers keep their parent's ORIGINAL environment in /proc/<pid>/environ)
  holds the same file; a process belongs to the attempt when one of its descriptors is that very
  file, compared by (st_dev, st_ino) — a resumed attempt replaces the mark with a new inode, so
  the old attempt's holders never match the new one;
* the TAG: NEKAISE_RUN_OWNER=<root key>:<run id>:<nonce>, which the coordinator puts in the
  environment of every child it execs (`Owner.env()`; standalone commands and the maintainer's
  window also set it in their own environment before they exec anything), matched exactly.

Liveness of a coordinator is read from /proc/<pid>/stat as ONE record: alive only when the start
time matches the mark AND the state is not zombie or dead; the recording boot must be this boot
(another boot: dead) and the PID namespace this one (another namespace: refused — its processes
cannot be judged from here). Signals go through pidfds: a pid is opened, re-verified as the
attempt's, and signalled only while that same process is alive, so the check-to-signal window
cannot hit a reused pid.

The LIFECYCLE LOCK (`lifecycle`, an flock under the root) serializes the pre-writer sweep of dead
coordinators' orphans with coordinator adoption and mark replacement: it is taken before the
database writer and held across sweep -> writer -> mark installation, so a sweep that judged an
attempt dead can never act while a new attempt of the same run installs itself.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import secrets
import select
import signal
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

import ops
import store

OWNER_ENV = "NEKAISE_RUN_OWNER"
MARK_PREFIX = "nekaise-run-owner."
MARKS = Path("workspace") / "run-owners"
LOCK_NAME = ".lifecycle.lock"


class OwnershipError(RuntimeError):
    """Ownership cannot be established: recovery refuses rather than guesses."""


def _pause(point: str) -> None:
    """Test hook between checking a process and signalling it."""


def root_key(root: Path) -> str:
    return hashlib.sha256(str(Path(root).resolve()).encode()).hexdigest()[:16]


def boot_id() -> str:
    return Path("/proc/sys/kernel/random/boot_id").read_text().strip()


def pid_namespace(pid: int | str = "self") -> str:
    return os.readlink(f"/proc/{pid}/ns/pid")


def proc_state(pid: int) -> tuple[str, str] | None:
    """(state, start time) of `pid` from ONE read of /proc/<pid>/stat, or None when there is no
    such process. Other errors raise."""
    try:
        fields = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    except (FileNotFoundError, ProcessLookupError):
        return None
    return fields[0], fields[19]


# --- the mark -----------------------------------------------------------------------------------------

@dataclass(frozen=True)
class Owner:
    """One coordinator attempt of one run of one root."""
    root_key: str
    run_id: str
    nonce: str
    dev: int
    ino: int

    @property
    def tag(self) -> str:
        return f"{self.root_key}:{self.run_id}:{self.nonce}"

    def env(self) -> dict[str, str]:
        return {OWNER_ENV: self.tag}


@dataclass(frozen=True)
class MarkRecord:
    owner: Owner
    pid: int
    start: str
    boot: str
    pidns: str
    path: Path


def mark_path(root: Path, run_id: str) -> Path:
    store._check_run_id(run_id)
    return Path(root) / MARKS / (MARK_PREFIX + run_id)


def read_mark(root: Path, run_id: str) -> MarkRecord | None:
    """The run's mark, or None when there is none. A mark that cannot be read or parsed raises
    (it may name processes still running)."""
    path = mark_path(root, run_id)
    try:
        st = os.stat(path)
        doc = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise OwnershipError(f"ownership mark {path} is unreadable ({exc})") from exc
    try:
        if doc["run"] != run_id or doc["root"] != root_key(root):
            raise KeyError("run/root")
        return MarkRecord(Owner(doc["root"], run_id, str(doc["nonce"]), st.st_dev, st.st_ino),
                          int(doc["pid"]), str(doc["start"]), str(doc["boot"]),
                          str(doc["pidns"]), path)
    except (KeyError, TypeError, ValueError) as exc:
        raise OwnershipError(f"ownership mark {path} is malformed ({exc})") from exc


def coordinator_alive(rec: MarkRecord) -> bool:
    """Whether the mark's coordinator still runs. Another boot: dead. Another PID namespace:
    OwnershipError (its processes cannot be judged from here — an operator decides)."""
    if rec.boot != boot_id():
        return False
    if rec.pidns != pid_namespace():
        raise OwnershipError(f"run {rec.owner.run_id}'s coordinator ran in PID namespace "
                             f"{rec.pidns}, this is {pid_namespace()}: refusing to judge its "
                             "processes — recover it from that namespace or by hand")
    state = proc_state(rec.pid)
    return state is not None and state[1] == rec.start and state[0] not in ("Z", "X")


class Mark:
    """The coordinator side: write the run's mark (a new attempt: new nonce, new inode — an
    earlier attempt's mark is replaced), hold an inheritable descriptor on it, optionally tag
    this process's environment. On exit the descriptor is closed and the tag restored; the file
    is removed unless `keep` was set (a run left unfinished: a later recovery must still find
    its survivors)."""

    def __init__(self, root: Path, run_id: str):
        self.root, self.run_id = Path(root), run_id
        self.path = mark_path(root, run_id)
        self.fd: int | None = None
        self.owner: Owner | None = None
        self.keep = False
        self._saved: str | None = None
        self._tagged = False

    def __enter__(self) -> "Mark":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        me = proc_state(os.getpid())
        nonce = secrets.token_hex(8)
        ops.atomic_write_text(self.path, json.dumps({
            "run": self.run_id, "root": root_key(self.root), "nonce": nonce,
            "pid": os.getpid(), "start": me[1], "boot": boot_id(),
            "pidns": pid_namespace()}) + "\n")
        self.fd = os.open(self.path, os.O_RDONLY)
        os.set_inheritable(self.fd, True)
        st = os.fstat(self.fd)
        self.owner = Owner(root_key(self.root), self.run_id, nonce, st.st_dev, st.st_ino)
        return self

    def tag(self) -> None:
        if not self._tagged:
            self._saved = os.environ.get(OWNER_ENV)
            os.environ[OWNER_ENV] = self.owner.tag
            self._tagged = True

    def __exit__(self, *exc) -> None:
        if self._tagged:
            if self._saved is None:
                os.environ.pop(OWNER_ENV, None)
            else:
                os.environ[OWNER_ENV] = self._saved
            self._tagged = False
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
        if not self.keep:
            try:   # only our own attempt's file (a newer attempt may have replaced it)
                if (os.stat(self.path).st_dev, os.stat(self.path).st_ino) == (
                        self.owner.dev, self.owner.ino):
                    self.path.unlink()
            except FileNotFoundError:
                pass


# --- which processes -------------------------------------------------------------------------------------

def _ancestors() -> set[int]:
    import round_recovery
    return round_recovery._ancestors()


def _belongs(pid: int, owner: Owner) -> bool:
    """Whether process `pid` belongs to attempt `owner` (tag or a descriptor on its mark).
    Raises FileNotFoundError/ProcessLookupError when it exited, PermissionError when it is not
    dumpable."""
    proc = Path(f"/proc/{pid}")
    needle = f"{OWNER_ENV}={owner.tag}".encode()
    if needle in (proc / "environ").read_bytes().split(b"\0"):
        return True
    for fd in os.scandir(proc / "fd"):
        try:
            if not os.readlink(fd.path).removesuffix(" (deleted)").rsplit("/", 1)[-1] \
                    .startswith(MARK_PREFIX):
                continue
            st = os.stat(fd.path)   # follows to the open file, deleted or not
        except (FileNotFoundError, ProcessLookupError):
            continue   # closed meanwhile
        if (st.st_dev, st.st_ino) == (owner.dev, owner.ino):
            return True
    return False


def owner_processes(owner: Owner) -> set[int]:
    """This user's live processes belonging to attempt `owner` (this process and its ancestors
    excepted). A process that exits during the scan, or that the kernel made non-dumpable (its
    environment and descriptors hidden; the run's processes are never such), is skipped; any
    other error raises — ownership that cannot be read is never read as "not the run's"."""
    mine, skip = os.getuid(), _ancestors()
    found = set()
    for proc in Path("/proc").glob("[0-9]*"):
        try:
            pid = int(proc.name)
        except ValueError:
            continue
        try:
            if pid in skip or proc.stat().st_uid != mine:
                continue
            if _belongs(pid, owner):
                found.add(pid)
        except (FileNotFoundError, ProcessLookupError, PermissionError):
            continue
        except OSError as exc:
            if proc_state(pid) is None:
                continue
            raise OwnershipError(f"cannot read process {pid} while looking for run "
                                 f"{owner.run_id}'s processes: {exc}") from exc
    return found


# --- stopping ---------------------------------------------------------------------------------------------

def _exited(pidfd: int) -> bool:
    return bool(select.select([pidfd], [], [], 0)[0])


def stop(pids: set[int], owner: Owner | None, grace: float = 2.0) -> list[int]:
    """SIGTERM, then SIGKILL after `grace`, each pid — through a pidfd: opened first, then the
    process re-verified as attempt `owner`'s (when given; the caller's own descendants are passed
    with owner None) while that pidfd shows it alive, and only then signalled — so a pid reused
    between the scan and the signal is never hit. Waits until every one exited; raises
    OwnershipError naming survivors. Returns the pids signalled."""
    fds: dict[int, int] = {}
    try:
        for pid in sorted(pids):
            if pid == os.getpid():
                continue
            try:
                fd = os.pidfd_open(pid)
            except ProcessLookupError:
                continue
            fds[pid] = fd
            try:
                ours = owner is None or _belongs(pid, owner)
            except (FileNotFoundError, ProcessLookupError):
                ours = False
            _pause("verified")
            if not ours or _exited(fd):
                os.close(fds.pop(pid))
        signalled = []
        for pid, fd in fds.items():
            try:
                signal.pidfd_send_signal(fd, signal.SIGTERM)
                signalled.append(pid)
            except ProcessLookupError:
                pass
        deadline, killed = time.monotonic() + grace, False
        while True:
            for pid in list(fds):
                try:
                    os.waitpid(pid, os.WNOHANG)
                except (ChildProcessError, OSError):
                    pass
            alive = [pid for pid, fd in fds.items() if not _exited(fd)]
            if not alive:
                return signalled
            if time.monotonic() >= deadline:
                if killed:
                    raise OwnershipError(f"process(es) still alive after SIGKILL: {alive}")
                for pid in alive:
                    try:
                        signal.pidfd_send_signal(fds[pid], signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                killed, deadline = True, time.monotonic() + grace
            time.sleep(0.05)
    finally:
        for fd in fds.values():
            os.close(fd)


def stop_attempt(root: Path, run_id: str, *, existing: set[int] | None = None,
                 current: Owner | None = None, grace: float = 2.0) -> list[int]:
    """Stop run `run_id`'s processes for staged recovery: the attempt `current` (the caller is
    its coordinator) or the attempt its mark records, plus the caller's descendants started
    after `existing`. A mark whose coordinator is alive and is not this process raises."""
    import round_recovery
    owner = current
    if owner is None:
        rec = read_mark(root, run_id)
        if rec is not None:
            if coordinator_alive(rec) and rec.pid != os.getpid():
                raise OwnershipError(f"run {run_id}'s coordinator (pid {rec.pid}) is alive: "
                                     "refusing to stop its processes")
            owner = rec.owner
    pids = owner_processes(owner) if owner is not None else set()
    if existing is not None:
        mine = round_recovery.descendants(os.getpid()) - set(existing)
        return sorted(set(stop(pids, owner, grace)) | set(stop(mine - pids, None, grace)))
    return stop(pids, owner, grace)


def sweep_dead(root: Path, grace: float = 2.0) -> dict[str, list]:
    """Before the writer is requested (the caller holds the lifecycle lock): stop the processes
    of every run whose mark names a DEAD coordinator — a fork of it may still hold its database
    session, and with it the writer lock — then remove that mark. A live coordinator's attempt
    is never touched; an unreadable mark or another PID namespace raises. The run's status is
    decided afterwards, under the writer. Returns {run id: pids stopped}."""
    marks = Path(root) / MARKS
    try:
        names = sorted(e.name for e in os.scandir(marks) if e.name.startswith(MARK_PREFIX))
    except FileNotFoundError:
        return {}
    out = {}
    for name in names:
        run_id = name[len(MARK_PREFIX):]
        rec = read_mark(root, run_id)
        if rec is None or coordinator_alive(rec):
            continue
        out[run_id] = stop(owner_processes(rec.owner), rec.owner, grace)
        if out[run_id]:
            ops.run_event(run_id, "round_processes_stopped", pids=out[run_id],
                          coordinator="dead")
        try:   # confirmed stopped: the mark of that dead attempt goes
            if (os.stat(rec.path).st_dev, os.stat(rec.path).st_ino) == (rec.owner.dev,
                                                                        rec.owner.ino):
                rec.path.unlink()
        except FileNotFoundError:
            pass
    return out


# --- the lifecycle lock -----------------------------------------------------------------------------------

class Lifecycle:
    """The held lifecycle lock; release() is idempotent (a coordinator releases it once its mark
    is installed)."""

    def __init__(self, fd: int):
        self._fd: int | None = fd

    @property
    def held(self) -> bool:
        return self._fd is not None

    def release(self) -> None:
        if self._fd is not None:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
            os.close(self._fd)
            self._fd = None


@contextmanager
def lifecycle(root: Path, timeout: float = 0) -> Iterator[Lifecycle]:
    """Take the root's lifecycle lock (waiting at most `timeout` seconds; OwnershipError when
    another sweep or coordinator start holds it). Always released on exit."""
    path = Path(root) / MARKS / LOCK_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_CLOEXEC, 0o600)
    deadline = time.monotonic() + max(0.0, timeout)
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if time.monotonic() >= deadline:
                os.close(fd)
                raise OwnershipError(f"the run lifecycle lock {path} is held (a recovery sweep "
                                     "or a coordinator starting)") from None
            time.sleep(0.05)
    held = Lifecycle(fd)
    try:
        yield held
    finally:
        held.release()
