#!/usr/bin/env python3
"""artifact_store.py — immutable local artifact versions (ADR 0001 stage 4, step 3).

A staged round's metadata stays invisible until promotion, so its payload bytes must not change
anything a committed generation G reads. The legacy steps did exactly that: the loader overwrote
raw/<source>/<id>.<ext> and text/<id>.md in place, the cleaner rewrote corpus/<id>.md, and the
pruner moved the dropped documents' files away before its transaction committed. Under the
PostgreSQL-staged path every changed payload is instead written ONCE as an immutable version:

    artifacts/<stage>/<sha[0:2]>/<sha[2:4]>/<sha256>      (read-only, never replaced or deleted)

The identity of an artifact is (stage, sha256 of its bytes); the path above is only its *local
locator*, registered separately in artifact_locators, so stage 5 can move the bytes into packs or
object storage without changing any identity. The manifest row's raw_path / text_path /
corpus_path stay the logical names they always were; a reader resolves a row's claim
(path, sha256) to the immutable version by identity and falls back to the legacy path, which the
staged path never writes (existing committed paths remain readable).

Write protocol (put_*): stream into a private temporary file under artifacts/.incoming/ while
hashing, fsync it, make it read-only, hard-link it to its content address (a link never replaces
an existing name: a concurrent or earlier writer of the same bytes wins and the size is checked),
fsync the address's directory, then drop the temporary name. A crash at any point leaves either no
address or a complete, durable one, plus at most a stray temporary file (sweep_incoming). Only
after put returns may a metadata batch reference the identity; staging (store_staging) re-checks
the file, fsyncs its directory once more as a barrier, and registers identity, locator and the
run's reference in the batch's own transaction, and the database refuses to seal a batch whose
manifest rows claim an identity that was neither registered for the run nor unchanged from the
row it supersedes (schema v6).

`for_view(view, root)` tells a pipeline step which world it is in: None for the legacy
file-authoritative path (FileStore, or a PostgreSQL view that is not a versioned staged run), in
which case the step behaves exactly as before; otherwise a VersionedAccess through which it reads
payloads by claim and writes new versions.
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterable, Mapping, NamedTuple

import store

STAGES = ("raw", "text", "corpus")
DIRNAME = "artifacts"
INCOMING = ".incoming"
CHUNK = 1 << 20
# (path field, identity field) of a manifest row's claim on each stage's payload
CLAIM_FIELDS = {"raw": ("raw_path", "sha256"), "text": ("text_path", "text_sha256"),
                "corpus": ("corpus_path", "corpus_sha256")}
_SHA = re.compile(r"[0-9a-f]{64}")
# a temporary file older than this whose writer is gone is swept (seconds)
STALE_INCOMING = 3600


class ArtifactError(store.StoreError):
    """An artifact could not be written, found or verified."""


def _crash(point: str) -> None:
    """Crash-injection hook (tests replace it); called at every durability boundary of put."""


@dataclass(frozen=True)
class Artifact:
    stage: str
    sha256: str
    size: int
    locator: str          # the local locator, relative to the root


def check_identity(stage: str, sha256) -> tuple[str, str]:
    if stage not in STAGES:
        raise ArtifactError(f"unknown artifact stage {stage!r}")
    if not isinstance(sha256, str) or not _SHA.fullmatch(sha256):
        raise ArtifactError(f"{stage} artifact identity must be a lowercase sha256, not "
                            f"{sha256!r}")
    return stage, sha256


def is_identity(sha256) -> bool:
    return isinstance(sha256, str) and _SHA.fullmatch(sha256) is not None


def local_locator(stage: str, sha256: str) -> str:
    """The content address of (stage, sha256) under the root (schema v6 checks this form)."""
    check_identity(stage, sha256)
    return f"{DIRNAME}/{stage}/{sha256[:2]}/{sha256[2:4]}/{sha256}"


# --- claims: what a manifest row says about each stage's payload ----------------------------------

def claim(row: Mapping | None, stage: str) -> tuple | None:
    """(path, sha256 or None) when the row claims a payload for `stage`, else None. A path field
    that is absent or JSON null is no claim (store_pg.V6_DDL nk_claim is the same rule)."""
    path_f, sha_f = CLAIM_FIELDS[stage]
    if row is None or row.get(path_f) is None:
        return None
    return (row[path_f], row.get(sha_f))


def same_claim(a: tuple | None, b: tuple | None) -> bool:
    """Exact JSON equality (1, 1.0 and true differ), like the stored canonical rows."""
    if a is None or b is None:
        return a is b
    if all(type(x) is str or x is None for x in (*a, *b)):   # the usual case, exact already
        return a == b
    return store.canonical_row({"c": list(a)}) == store.canonical_row({"c": list(b)})


def changed_claims(row: Mapping, before: Mapping | None) -> list[tuple[str, str, object]]:
    """The claims `row` makes that `before` (the row it supersedes) did not make identically:
    [(stage, path, sha256)]. These — and only these — need an immutable version."""
    out = []
    for stage in STAGES:
        now = claim(row, stage)
        if now is not None and not same_claim(now, claim(before, stage)):
            out.append((stage, now[0], now[1]))
    return out


# --- durability helpers ------------------------------------------------------------------------------

def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _mkdir_durable(path: Path) -> None:
    """mkdir -p that fsyncs the parent of every directory it creates."""
    missing = []
    while not path.exists():
        missing.append(path)
        path = path.parent
    for p in reversed(missing):
        try:
            p.mkdir()
        except FileExistsError:
            pass
        _fsync_dir(p.parent)


_SYNCFS = None


def sync_filesystem(path: Path) -> None:
    """syncfs(2) on the filesystem holding `path`: every write and directory entry on it is
    durable when this returns (one call instead of one fsync per file; os.sync() where the C
    library has no syncfs)."""
    global _SYNCFS
    if _SYNCFS is None:
        import ctypes
        _SYNCFS = getattr(ctypes.CDLL(None, use_errno=True), "syncfs", False)
    if not _SYNCFS:
        os.sync()
        return
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        if _SYNCFS(fd) != 0:
            import ctypes
            err = ctypes.get_errno()
            raise OSError(err, f"syncfs {path}: {os.strerror(err)}")
    finally:
        os.close(fd)


BARRIER_DIRS = 64   # above this many directories, one syncfs is cheaper than fsyncing each


def barrier(paths: Iterable[Path], root: Path) -> int:
    """Make the directory entries of `paths` (versions under <root>/artifacts) durable, with
    every ancestor directory's entry up to the root: fsync each distinct leaf directory and its
    chain, or one syncfs when there are many. Returns how many leaf directories. Staging calls
    it before it registers references to versions it did not write itself."""
    dirs = sorted({Path(p).parent for p in paths})
    if len(dirs) > BARRIER_DIRS:
        sync_filesystem(dirs[0])
        return len(dirs)
    local = LocalArtifacts(root)
    for d in dirs:
        local._durable_chain(d)
        _fsync_dir(d)
    return len(dirs)


# Directories (absolute paths) whose own entry, and every ancestor's up to the data root, this
# process has made durable (it fsynced each parent after the directory existed). Directories are
# never removed, so the fact stays true; another process's crash cannot undo it. Entries are added
# only once a whole chain succeeded (LocalArtifacts._durable_chain).
_DURABLE_DIRS: set[str] = set()
_DURABLE_LOCK = threading.Lock()


class Pending(NamedTuple):
    """A version written but not yet committed (write_pending): (stage, sha256, size, tmp);
    tmp is None when the version already existed. Crosses process boundaries (pickled)."""
    stage: str
    sha256: str
    size: int
    tmp: str | None


def write_pending(root: Path, stage: str, data: bytes, owner: int | None = None) -> Pending:
    """The first half of a group commit (LocalArtifacts.commit), safe in worker processes: hash
    `data` and write it, read-only and NOT yet fsynced, to a temporary name owned by process
    `owner` (default: this one) — or nothing, when its version already exists. Nothing refers
    to a temporary name; until the commit, a crash leaves only a stray temporary."""
    if stage not in STAGES:
        raise ArtifactError(f"unknown artifact stage {stage!r}")
    data = bytes(data)
    sha = hashlib.sha256(data).hexdigest()
    local = LocalArtifacts(root)
    if local.size(stage, sha) == len(data):
        return Pending(stage, sha, len(data), None)
    tmp = local._tmp_name(owner)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o444)
    with os.fdopen(fd, "wb", closefd=True) as f:
        f.write(data)
    _crash("written")
    return Pending(stage, sha, len(data), str(tmp))


# --- the local store ----------------------------------------------------------------------------------

class LocalArtifacts:
    """Immutable, content-addressed payload versions under <root>/artifacts/."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.base = self.root / DIRNAME

    def path(self, stage: str, sha256: str) -> Path:
        return self.root / local_locator(stage, sha256)

    def has(self, stage: str, sha256: str) -> bool:
        try:
            return stat.S_ISREG(os.lstat(self.path(stage, sha256)).st_mode)
        except FileNotFoundError:
            return False

    def size(self, stage: str, sha256: str) -> int | None:
        try:
            st = os.lstat(self.path(stage, sha256))   # a symlink is never a version
        except FileNotFoundError:
            return None
        return st.st_size if stat.S_ISREG(st.st_mode) else None

    def _durable_chain(self, d: Path) -> None:
        """Create directory `d` (under the root) if needed and make its entry and every
        ancestor's entry up to the root durable: whether this process created them or found them
        — a directory found existing may have been created by a writer that crashed before any
        sync reached its parent. Each directory is fsynced into its parent once per process.

        The cache records only COMPLETE chains: the uncached part is collected first, every
        parent is fsynced (topmost first), and only when all succeeded are its directories
        cached — so a failure part-way leaves nothing cached and a retry syncs the whole chain.
        Overlapping callers may both sync (harmless); neither caches before its own chain is
        done, and an entry always means "this directory and all its ancestors are durable"."""
        d = Path(d)
        d.mkdir(parents=True, exist_ok=True)
        root = self.root.resolve()
        p = d.resolve()
        if root not in p.parents:
            raise ArtifactError(f"{d} is not under {self.root}")
        todo = []
        while p != root and str(p) not in _DURABLE_DIRS:
            todo.append(p)
            p = p.parent
        for q in reversed(todo):       # ancestors first
            _fsync_dir(q.parent)
        with _DURABLE_LOCK:
            _DURABLE_DIRS.update(str(q) for q in todo)

    def _check_existing(self, stage: str, sha: str, size: int) -> None:
        """An address found in place is accepted only when it holds exactly those bytes (hash,
        not size): a damaged version is never overwritten, and nothing that depends on it — an
        adoption followed by replacing the source — may proceed."""
        if not self.verify(stage, sha, size):
            raise ArtifactError(f"{self.path(stage, sha)} exists but does not hold the bytes of "
                                f"its identity ({size} bytes, sha256 {sha}): a damaged version "
                                "is never overwritten; investigate it")

    def _incoming(self) -> Path:
        d = self.base / INCOMING
        if not d.is_dir():
            _mkdir_durable(d)
        return d

    def _tmp_name(self, owner: int | None = None) -> Path:
        return self._incoming() / f"{owner or os.getpid()}-{secrets.token_hex(8)}"

    def commit(self, pending: Iterable[Pending]) -> list[Artifact]:
        """The second half of a group commit: ONE syncfs makes every pending temporary durable,
        each is hard-linked to its content address (never replacing a name: an existing address
        is size-checked), and a second syncfs makes the addresses durable before this returns —
        the same guarantees as put_* (an address exists only complete) at two syncs per group
        instead of two fsyncs per version. Temporaries are removed last; a crash leaves
        complete addresses and stray temporaries only."""
        pending = list(pending)
        if not pending:
            return []
        found = [p for p in pending if p.tmp is None]   # versions that existed already
        if any(p.tmp is not None for p in pending):
            sync_filesystem(self._incoming())
            _crash("group-synced")
            for p in pending:
                if p.tmp is None:
                    continue
                final = self.path(p.stage, p.sha256)
                final.parent.mkdir(parents=True, exist_ok=True)   # durable with the next sync
                try:
                    os.link(p.tmp, final)
                except FileExistsError:
                    found.append(p)
            _crash("group-linked")
        # always, even when every version already existed: an address (or a directory) found
        # here may come from a writer that crashed before its own final sync
        sync_filesystem(self.base)
        _crash("group-published")
        for p in found:
            self._check_existing(p.stage, p.sha256, p.size)
        out = []
        for p in pending:
            have = self.size(p.stage, p.sha256)
            if have != p.size:
                raise ArtifactError(f"{self.path(p.stage, p.sha256)} has {have} bytes, not "
                                    f"{p.size}: a damaged version is never overwritten")
            out.append(Artifact(p.stage, p.sha256, p.size, local_locator(p.stage, p.sha256)))
        for p in pending:
            if p.tmp is not None:
                try:
                    os.unlink(p.tmp)
                except FileNotFoundError:
                    pass
        return out

    def put_bytes(self, stage: str, data: bytes) -> Artifact:
        return self.put_stream(stage, [bytes(data)])

    def put_file(self, stage: str, src: Path) -> Artifact:
        """Copy an existing file in as a new version (streamed; bounded memory)."""
        with open(src, "rb") as f:
            return self.put_stream(stage, iter(lambda: f.read(CHUNK), b""))

    def put_stream(self, stage: str, chunks: Iterable[bytes]) -> Artifact:
        if stage not in STAGES:
            raise ArtifactError(f"unknown artifact stage {stage!r}")
        tmp = self._tmp_name()
        h, size = hashlib.sha256(), 0
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        try:
            with os.fdopen(fd, "wb", closefd=True) as f:
                for chunk in chunks:
                    h.update(chunk)
                    size += len(chunk)
                    f.write(chunk)
                _crash("written")
                f.flush()
                os.fsync(f.fileno())
                os.fchmod(f.fileno(), 0o444)
            _crash("synced")
            return self._publish(stage, tmp, h.hexdigest(), size)
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass

    def adopt(self, stage: str, src: Path) -> Artifact:
        """Preserve an existing file's bytes as a version WITHOUT copying when possible: hard-link
        its inode into the incoming area, hash it there, make it read-only and link it to its
        address (a copy when the file lives on another filesystem). The source name is left as it
        is; afterwards it may be replaced or removed, the version stays."""
        tmp = self._tmp_name()
        try:
            os.link(src, tmp)
        except OSError as exc:
            if exc.errno != 18:  # EXDEV: another filesystem, copy instead
                raise
            return self.put_file(stage, src)
        try:
            os.chmod(tmp, 0o444)   # first: an in-place writer now fails instead of racing the hash
            h, size = hashlib.sha256(), 0
            with open(tmp, "rb") as f:
                for chunk in iter(lambda: f.read(CHUNK), b""):
                    h.update(chunk)
                    size += len(chunk)
                os.fsync(f.fileno())
            _crash("synced")
            return self._publish(stage, tmp, h.hexdigest(), size)
        finally:
            try:
                tmp.unlink()
            except FileNotFoundError:
                pass

    def _publish(self, stage: str, tmp: Path, sha: str, size: int) -> Artifact:
        final = self.path(stage, sha)
        self._durable_chain(final.parent)   # found or created: the whole chain is durable
        try:
            os.link(tmp, final)          # never replaces an existing name
            _crash("linked")
        except FileExistsError:
            self._check_existing(stage, sha, size)
        _fsync_dir(final.parent)            # the address itself (linked now or found)
        _crash("published")
        return Artifact(stage, sha, size, local_locator(stage, sha))

    def open(self, stage: str, sha256: str) -> BinaryIO:
        return open(self.path(stage, sha256), "rb")

    def read_bytes(self, stage: str, sha256: str) -> bytes:
        return self.path(stage, sha256).read_bytes()

    def verify(self, stage: str, sha256: str, size: int | None = None) -> bool:
        """Re-hash a version (streamed): True when present with exactly those bytes."""
        try:
            h, n = hashlib.sha256(), 0
            with self.open(stage, sha256) as f:
                for chunk in iter(lambda: f.read(CHUNK), b""):
                    h.update(chunk)
                    n += len(chunk)
        except FileNotFoundError:
            return False
        return h.hexdigest() == sha256 and (size is None or n == size)

    def sweep_incoming(self, *, older_than: float = STALE_INCOMING) -> int:
        """Remove temporary files of writers that are gone (their pid is not alive) or that are
        older than `older_than` seconds. A temporary name is never an artifact: nothing refers
        to it, and a live writer's file is left alone."""
        d = self.base / INCOMING
        if not d.is_dir():
            return 0
        removed, now = 0, time.time()
        for entry in os.scandir(d):
            pid = entry.name.split("-", 1)[0]
            alive = pid.isdigit() and _alive(int(pid))
            try:
                old = now - entry.stat(follow_symlinks=False).st_mtime > older_than
            except FileNotFoundError:
                continue
            if not alive or old:
                try:
                    os.unlink(entry.path)
                    removed += 1
                except FileNotFoundError:
                    pass
        return removed


def _alive(pid: int) -> bool:
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# --- which world a pipeline step is in ----------------------------------------------------------------

class VersionedAccess:
    """Payload access for a step of a versioned staged run: reads resolve a row's claim by
    identity (the immutable version, else the legacy path the claim names), writes create
    versions. Nothing under raw/, text/ or corpus/ is ever written through it."""

    versioned = True

    def __init__(self, root: Path):
        self.root = Path(root)
        self.local = LocalArtifacts(self.root)

    def path(self, row: Mapping | None, stage: str) -> Path | None:
        """A readable local file holding the row's `stage` payload, or None."""
        c = claim(row, stage)
        if c is None:
            return None
        path, sha = c
        if isinstance(sha, str) and _SHA.fullmatch(sha) and self.local.has(stage, sha):
            return self.local.path(stage, sha)
        if isinstance(path, str) and path and not os.path.isabs(path) and ".." not in \
                Path(path).parts:
            legacy = self.root / path
            if legacy.is_file():
                return legacy
        return None

    def exists(self, row: Mapping | None, stage: str) -> bool:
        return self.path(row, stage) is not None

    def read_bytes(self, row: Mapping, stage: str) -> bytes:
        p = self.path(row, stage)
        if p is None:
            raise ArtifactError(f"{row.get('id')}: no local {stage} payload for its claim")
        return p.read_bytes()

    def read_text(self, row: Mapping, stage: str) -> str:
        return self.read_bytes(row, stage).decode("utf-8", errors="replace")


def run_policy(view) -> dict | None:
    """The staged run's provenance a view serves (PgReadView.provenance), or None."""
    prov = getattr(view, "provenance", None)
    return prov() if callable(prov) else None


def for_view(view, root: Path) -> VersionedAccess | None:
    """VersionedAccess when `view` reads a staged run whose artifact policy is versioned; None
    (the legacy, file-authoritative behaviour, unchanged) otherwise."""
    if getattr(view, "stage", None) is None:
        return None
    prov = run_policy(view)
    if not prov or prov.get("artifact_policy") != "versioned":
        return None
    return VersionedAccess(root)


# --- verification (a gate over what a run introduced) --------------------------------------------------

def verify_run(st, writer, run_id: str, *, page: int = 1000) -> dict:
    """Re-hash every artifact version run `run_id` referenced that no earlier verification
    covered (bounded pages, streamed hashing), marking each verified locator. Returns
    {"verified": n, "failed": [(stage, sha256, why), ...] (at most 100)}; the caller records the
    gate verdict (StagedRound.record_gate("artifacts", passed=not failed))."""
    local = LocalArtifacts(st.root)
    out = {"verified": 0, "failed": []}
    after = ("", "")
    while True:
        rows = st.unverified_run_artifacts(writer, run_id, after=after, limit=page)
        if not rows:
            return out
        ok = []
        for stage, sha, size, locator in rows:
            if locator is None:
                why = "no readable local locator"
            elif locator != local_locator(stage, sha):
                why = f"unexpected locator {locator}"
            elif not local.verify(stage, sha, size):
                why = "missing or damaged"
            else:
                ok.append((stage, sha, locator))
                continue
            if len(out["failed"]) < 100:
                out["failed"].append((stage, sha, why))
        st.mark_verified(writer, ok)
        out["verified"] += len(ok)
        after = rows[-1][:2]
