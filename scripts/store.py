#!/usr/bin/env python3
"""store.py — the storage interface for all tracked corpus state (ADR 0001, stage 1).

docs/decisions/0001-storage-architecture.md moves the registry, manifest, prune ledger, blocklist
and control state out of git into PostgreSQL. This module is the seam that makes the move
incremental: every reader and writer will talk to a Store, and FileStore implements the interface
over today's files (registry/*.yaml, manifest/*.jsonl, registry/pruned-*.jsonl, pruned_urls.txt,
registry/rotation.json, registry/backend_state.json). Stage 2 adds a PostgreSQL store that must
pass tests/test_store_contract.py and produce byte-identical canonical exports.

Contract
--------
Views. Reads happen in a ReadView that is consistent for its whole lifetime: it serves one
generation (Version) and raises once the store moves past it or the view is closed. A view never
opens while an interrupted transaction or round is unrecovered, so readers cannot see a half-written
state. FileStore gets consistency from the canonical round lock; PostgreSQL will use a snapshot.

Transactions. transaction(run_id, expected_version, writer) gives read-your-writes and commits on
clean exit, all-or-nothing across every table; any exception discards everything. Transactions do
not nest. The WriterToken proves ownership (the round lock here, a fencing epoch in PostgreSQL) and
is re-verified before commit.

Replay versus retry. A run id commits at most once. The commit records a digest of the run's
REQUESTS (each mutation call and its arguments), not of its effects, so running the same run id
again with the same requests is a no-op (mutations return 0) even though the effects would now
differ; different requests under a committed run id raise.

Mutations validate the whole batch before changing anything, return the number of rows actually
changed, and are journaled with before/after rows, so every deletion leaves a tombstone.

Values. Rows are JSON objects. Missing and null are distinct: predicates are two-valued and every
leaf is False on a missing field except Exists. URLs normalize as strip() then trailing '/' removed
(blocklist.normalize); titles as registry.norm; empty values are never "known". Aggregates group
missing and null together as None; sums add int/float (never bool) and ignore everything else.

Scans are ordered and keyset-paginated: entries/manifest by id, blocklist by URL, ledger by its
canonical JSON, events by sequence. A cursor is bound to one view and one query.

Runtime backend state (exhaustion, outages) lives apart from git-owned backend configuration. A
backend runs only if both its configuration and its runtime state enable it; runtime state can
never override an operator's pause.

FileStore limitation: it holds every table it touches in memory for a view's lifetime (24.5 GB and
~6 minutes at 1.6M documents, measured 2026-09-24). That is today's cost, not a scalable one; the
PostgreSQL store removes it. known() avoids it through the SQLite index (30 ms per 10k candidates).
Stage 1 converts no production caller.
"""
from __future__ import annotations

import copy
import dataclasses
import fcntl
import hashlib
import json
import os
import re
import shutil
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

import blocklist as blocklist_mod
import ops
import registry

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PAGE = 2000
MAX_PAGE = 10_000
MAX_KNOWN = 10_000
ROUND_LOCK = "corpus-round"
# A parent holding the round lock (run_round, from stage 3 on) sets this to "<pid>:<run_id>" for
# the children it spawns. A child may then open read views without the lock, but only after
# verifying that <pid> is its ancestor and really holds the lock; <run_id>'s own in-flight round
# snapshot does not count as unsettled state for it.
INHERITED_LOCK_ENV = "NEKAISE_STORE_LOCK_INHERITED"
# Git-owned configuration and policy documents; a store never writes them.
CONFIG_FILES = ("backends.json", "eligibility.json", "vendors.json")
BACKEND_STATE_FILE = "backend_state.json"


class Table(str, Enum):
    ENTRIES = "entries"
    MANIFEST = "manifest"
    BLOCKLIST = "blocklist"
    LEDGER = "ledger"
    EVENTS = "events"


class Stage(str, Enum):
    RAW = "raw"
    TEXT = "text"
    CORPUS = "corpus"


MANIFEST_ONLY_FIELDS = (
    "status", "http_status", "sha256", "bytes", "raw_path", "text_path", "text_chars", "error",
    "fetched_at", "text_sha256", "extractor_version", "quality",
) + registry.CORPUS_FIELDS
LEDGER_FIELDS = (
    "id", "url", "title", "reason", "source", "topic", "license", "http_status", "error",
    "sha256", "quality", "blocklisted", "pruned_at", "run_id",
)
EVENT_FIELDS = (
    "seq", "event_id", "run_id", "at", "table", "op", "id", "before", "after", "reason", "digest",
)
TABLE_FIELDS: dict[Table, frozenset[str]] = {
    Table.ENTRIES: frozenset(registry.FIELDS),
    Table.MANIFEST: frozenset(registry.FIELDS + MANIFEST_ONLY_FIELDS),
    Table.BLOCKLIST: frozenset({"url"}),
    Table.LEDGER: frozenset(LEDGER_FIELDS),
    Table.EVENTS: frozenset(EVENT_FIELDS),
}


class StoreError(RuntimeError):
    """Base class for storage contract violations."""


class VersionConflict(StoreError):
    """The store changed since the caller read its expected version."""


class WriterError(StoreError):
    """The writer token does not (or no longer) prove ownership."""


class PendingTransaction(StoreError):
    """An interrupted transaction or round must be recovered first."""


class StaleView(StoreError):
    """The view is closed or the store has moved past its generation."""


# --- predicates --------------------------------------------------------------------------------

@dataclass(frozen=True)
class Eq:
    field: str
    value: Any


@dataclass(frozen=True)
class In:
    field: str
    values: tuple

    def __init__(self, field: str, values: Iterable):
        object.__setattr__(self, "field", field)
        object.__setattr__(self, "values", tuple(values))


@dataclass(frozen=True)
class Prefix:
    field: str
    prefix: str


@dataclass(frozen=True)
class Exists:
    field: str


@dataclass(frozen=True)
class And:
    parts: tuple

    def __init__(self, *parts):
        object.__setattr__(self, "parts", tuple(parts))


@dataclass(frozen=True)
class Or:
    parts: tuple

    def __init__(self, *parts):
        object.__setattr__(self, "parts", tuple(parts))


@dataclass(frozen=True)
class Not:
    part: Any


Predicate = Eq | In | Prefix | Exists | And | Or | Not


def predicate_fields(pred: Predicate) -> set[str]:
    if isinstance(pred, (Eq, In, Prefix, Exists)):
        return {pred.field}
    if isinstance(pred, (And, Or)):
        return set().union(*(predicate_fields(p) for p in pred.parts)) if pred.parts else set()
    if isinstance(pred, Not):
        return predicate_fields(pred.part)
    raise StoreError(f"unsupported predicate {type(pred).__name__}")


def json_equal(a: Any, b: Any) -> bool:
    """Equality of JSON values as PostgreSQL jsonb defines it: numbers compare by value (1 == 1.0),
    but a boolean never equals a number and containers compare element-wise."""
    if isinstance(a, bool) or isinstance(b, bool):
        return isinstance(a, bool) and isinstance(b, bool) and a == b
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return a == b
    if isinstance(a, Mapping) and isinstance(b, Mapping):
        return a.keys() == b.keys() and all(json_equal(a[k], b[k]) for k in a)
    if isinstance(a, (list, tuple)) and isinstance(b, (list, tuple)):
        return len(a) == len(b) and all(json_equal(x, y) for x, y in zip(a, b))
    return type(a) is type(b) and a == b


def evaluate(pred: Predicate | None, row: Mapping) -> bool:
    if pred is None:
        return True
    if isinstance(pred, Eq):
        return pred.field in row and json_equal(row[pred.field], pred.value)
    if isinstance(pred, In):
        return pred.field in row and any(json_equal(row[pred.field], v) for v in pred.values)
    if isinstance(pred, Prefix):
        value = row.get(pred.field)
        return isinstance(value, str) and value.startswith(pred.prefix)
    if isinstance(pred, Exists):
        return pred.field in row
    if isinstance(pred, And):
        return all(evaluate(p, row) for p in pred.parts)
    if isinstance(pred, Or):
        return any(evaluate(p, row) for p in pred.parts)
    if isinstance(pred, Not):
        return not evaluate(pred.part, row)
    raise StoreError(f"unsupported predicate {type(pred).__name__}")


def eligibility_where(restrictions: Mapping[str, Mapping]) -> Predicate:
    """registry.is_training_eligible as a predicate: not pointer-only and no restriction matches.
    Selectors inside one restriction are ANDed; restrictions are ORed."""
    blocked: list = [In("license", sorted(registry.POINTER_ONLY_LICENSES))]
    for rule in restrictions.values():
        leaves = []
        for key, value in rule["match"].items():
            if key == "id_prefix":
                leaves.append(Prefix("id", value))
            elif key == "source":
                leaves.append(Eq("source", value))
            else:
                raise StoreError(f"unsupported eligibility selector {key!r}")
        blocked.append(And(*leaves))
    return Not(Or(*blocked))


def _plain(value: Any) -> Any:
    """JSON-compatible form of request arguments (dataclasses, enums, iterables)."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {k: _plain(v) for k, v in dataclasses.asdict(value).items()}
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(k): _plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        items = [_plain(v) for v in value]
        return sorted(items, key=_canonical) if isinstance(value, (set, frozenset)) else items
    return value


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def key_digest(text: str) -> str:
    """Scan key for unbounded values (URLs, ledger rows): their sha256, because the values
    themselves can exceed a B-tree index entry. Blocklist and ledger scans are ordered by it."""
    return hashlib.sha256(text.encode()).hexdigest()


def canonical_row(row: Mapping) -> str:
    """The one serialization of a row every backend stores and exports."""
    return _canonical(row)


def check_group_value(field: str, value: Any) -> Any:
    """Aggregation groups by strings, booleans and None only (missing and null are both None), so
    grouping is identical in every backend; numbers and containers are rejected."""
    if value is None or isinstance(value, (str, bool)):
        return value
    raise StoreError(f"aggregate_manifest cannot group by {field!r}: value {value!r} is not a "
                     "string, boolean or null")


def exact_sum(values: Iterable[Any]) -> int | float:
    """Sum of the int/float values (bools and everything else ignored), computed exactly from each
    value's shortest repr and then rounded once: an int if every value is an int, else a float.
    PostgreSQL's numeric SUM over the same JSON text yields the same result."""
    import decimal
    total, is_float = decimal.Decimal(0), False
    for v in values:
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        is_float |= isinstance(v, float)
        total += decimal.Decimal(repr(v)) if isinstance(v, float) else decimal.Decimal(v)
    return float(total) if is_float else int(total)


def validate_patch(updates: Mapping[str, Mapping], unset: Sequence[str], existing) -> None:
    """update_manifest_fields' batch validation, shared by every backend. `existing(ids)` returns
    the subset of ids that exist."""
    allowed = TABLE_FIELDS[Table.MANIFEST]
    if missing := sorted(set(updates) - set(existing(list(updates)))):
        raise StoreError(f"update_manifest_fields: unknown id(s): {', '.join(missing[:5])}")
    if "id" in unset:
        raise StoreError("update_manifest_fields cannot unset id")
    if unknown := sorted(set(unset) - allowed):
        raise StoreError(f"unknown manifest field(s): {', '.join(unknown)}")
    for sid, patch in updates.items():
        if "id" in patch:
            raise StoreError("update_manifest_fields cannot change id")
        if overlap := sorted(set(patch) & set(unset)):
            raise StoreError(f"{sid}: field(s) both set and unset: {', '.join(overlap)}")
        if unknown := sorted(set(patch) - allowed):
            raise StoreError(f"unknown manifest field(s): {', '.join(unknown)}")


def validate_ledger_rows(rows: Sequence[Mapping]) -> None:
    allowed = TABLE_FIELDS[Table.LEDGER]
    for r in rows:
        if not isinstance(r.get("id"), str) or not r["id"]:
            raise StoreError("ledger rows need a non-empty id")
        if unknown := sorted(set(r) - allowed):
            raise StoreError(f"unknown ledger field(s): {', '.join(unknown)}")


def _sha(data: bytes | None) -> str | None:
    return None if data is None else hashlib.sha256(data).hexdigest()


# --- value types -------------------------------------------------------------------------------

@dataclass(frozen=True)
class Version:
    token: str


@dataclass(frozen=True)
class Cursor:
    view: str
    query: str
    last_key: tuple


@dataclass(frozen=True)
class Page:
    rows: list
    next_cursor: Cursor | None


@dataclass(frozen=True)
class KnownHits:
    urls: frozenset
    titles: frozenset
    ids: frozenset


@dataclass(frozen=True)
class ArtifactRef:
    id: str
    stage: Stage
    locator: str
    sha256: str | None
    size: int | None


@dataclass(frozen=True)
class BackendState:
    """Runtime state of one backend (default: enabled, no reason)."""
    enabled: bool = True
    reason: str | None = None


@dataclass(frozen=True)
class ConfigSnapshot:
    """Git-owned configuration/policy documents (parsed) and the sha256 of each file's bytes."""
    documents: dict
    digests: dict

    @property
    def backends(self) -> dict:
        return self.documents.get("backends.json", {})

    @property
    def eligibility(self) -> dict:
        return self.documents.get("eligibility.json", {})


@dataclass(frozen=True)
class WriterToken:
    kind: str
    owner: str
    epoch: int
    lock_path: str
    nonce: str
    round_id: str | None = None  # the in-flight round this writer owns, if any


@dataclass(frozen=True)
class TransactionInfo:
    run_id: str
    state: str
    path: str


@dataclass(frozen=True)
class RecoveryResult:
    run_id: str
    action: str


@dataclass(frozen=True)
class ExportReport:
    directory: str
    files: dict


def open(*, root: Path = ROOT, backend: str | None = None) -> "FileStore":  # noqa: A001
    """Open the configured store. Unknown or unavailable backends fail explicitly."""
    name = backend or os.environ.get("NEKAISE_STORE") or "file"
    if name == "file":
        return FileStore(root)
    if name == "postgres":
        dsn = os.environ.get("NEKAISE_PG_DSN")
        if not dsn:
            raise StoreError("storage backend 'postgres' needs NEKAISE_PG_DSN")
        import store_pg
        return store_pg.PgStore(root, dsn=dsn, schema=os.environ.get("NEKAISE_PG_SCHEMA", "nekaise"))
    raise StoreError(f"storage backend {name!r} is not available (known: file, postgres)")


def norm_url(url: str | None) -> str:
    return blocklist_mod.normalize(url or "")


def norm_title(title: str | None) -> str:
    return registry.norm(title or "")


# --- durable filesystem primitives ---------------------------------------------------------------

def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _mkdir_durable(path: Path) -> None:
    """mkdir -p that fsyncs the parent of every directory it creates."""
    path = Path(path)
    missing = []
    while not path.exists():
        missing.append(path)
        path = path.parent
    for p in reversed(missing):
        p.mkdir()
        _fsync_dir(p.parent)


def _unlink_durable(path: Path) -> None:
    if path.exists():
        path.unlink()
        _fsync_dir(path.parent)


def _rmtree_durable(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
        _fsync_dir(path.parent)


def _write_durable(path: Path, data: bytes) -> None:
    _mkdir_durable(path.parent)
    ops.atomic_write_bytes(path, data)  # fsyncs the file and its directory entry


_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


def _check_run_id(run_id: str) -> str:
    """Run ids become directory names; allow only a plain, bounded name (no '', '.', '..', '/')."""
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id) or ".." in run_id:
        raise StoreError(f"invalid run id {run_id!r}")
    return run_id


def uniquify(entries: Sequence[Mapping], is_taken) -> list[dict]:
    """registry.uniquify_ids' suffixing (-2, -3, … on a 50-char base) against `is_taken(id)` and
    the batch itself, without materializing every existing id."""
    out, batch = [copy.deepcopy(dict(e)) for e in entries], set()
    for h in out:
        base, i = h["id"], 2
        while h["id"] in batch or is_taken(h["id"]):
            h["id"] = f"{base[:50]}-{i}"
            i += 1
        batch.add(h["id"])
    return out


def artifact_ref(row: Mapping | None, id: str, stage: "Stage") -> "ArtifactRef | None":  # noqa: A002
    """Where a document's stage payload lives, from its manifest row (shared by every backend)."""
    if row is None:
        return None
    stage = Stage(stage)
    path, sha, size = {
        Stage.RAW: ("raw_path", "sha256", "bytes"),
        Stage.TEXT: ("text_path", "text_sha256", None),
        Stage.CORPUS: ("corpus_path", "corpus_sha256", None),
    }[stage]
    if not row.get(path):
        return None
    return ArtifactRef(id, stage, f"file:{row[path]}", row.get(sha),
                       row.get(size) if size else None)


def _is_ancestor(pid: int) -> bool:
    cur = os.getpid()
    for _ in range(64):
        if cur == pid:
            return True
        try:
            stat = Path(f"/proc/{cur}/stat").read_text()
        except OSError:
            return False
        cur = int(stat.rsplit(")", 1)[1].split()[1])
        if cur <= 1:
            return pid == cur
    return False


# --- in-memory state ---------------------------------------------------------------------------

@dataclass
class _State:
    entries: dict = field(default_factory=dict)        # id -> row (FIELDS only)
    entry_file: dict = field(default_factory=dict)     # id -> shard filename it lives in
    manifest: dict = field(default_factory=dict)       # id -> row
    blocklist: set = field(default_factory=set)        # normalized urls
    ledger: list = field(default_factory=list)         # rows in canonical file order
    events: list = field(default_factory=list)         # journal rows in seq order
    rotation: dict = field(default_factory=dict)
    backend_state: dict = field(default_factory=dict)  # name -> {"enabled", "reason"}


# Transactions in progress in this process, per store root: they must not nest.
_ACTIVE_TRANSACTIONS: set[str] = set()


class FileStore:
    """Store over the git-tracked file layout. Authoritative until the stage-4 cutover."""

    def __init__(self, root: Path = ROOT):
        self.root = Path(root)
        self.reg = self.root / "registry"
        self.man = self.root / "manifest"
        self.blocklist_path = self.root / "pruned_urls.txt"
        self.rotation_path = self.reg / "rotation.json"
        self.backend_state_path = self.reg / BACKEND_STATE_FILE
        self.journal_dir = self.reg / "journal"
        self.workspace = self.root / "workspace"
        self.txn_dir = self.workspace / "store-transactions"
        self.round_snapshots = self.workspace / "round-snapshots"
        self._live_tokens: set[str] = set()

    # -- versions, ownership, settledness --------------------------------------------------------

    def _tracked_files(self) -> list[Path]:
        files = [p for p in self.reg.rglob("*") if p.is_file()] if self.reg.exists() else []
        files += sorted(self.man.glob("*.jsonl")) if self.man.exists() else []
        if self.blocklist_path.exists():
            files.append(self.blocklist_path)
        return sorted(files)

    def version(self) -> Version:
        """Generation token over every authoritative file and pinned config document."""
        rows = []
        for path in self._tracked_files():
            st = path.stat()
            rows.append((str(path.relative_to(self.root)), st.st_size, st.st_mtime_ns, st.st_ino))
        return Version(_digest(rows))

    @contextmanager
    def writer(self, timeout: float = 0, *, round_id: str | None = None) -> Iterator[WriterToken]:
        """Hold the canonical round lock and yield the token that proves it. The token dies with
        this context, even if the same process takes the lock again later. `round_id` declares
        the round this writer is about to run: its snapshot must not exist yet, so ownership is
        only ever granted for a round started under this lock, never for an abandoned one."""
        if round_id is not None:
            _check_run_id(round_id)
        with ops.named_lock(ROUND_LOCK, timeout=timeout, workspace=self.workspace) as path:
            if round_id is not None and round_id in self._legacy_snapshots():
                raise PendingTransaction(f"round {round_id} already has a snapshot: it was "
                                         "interrupted and must be recovered, not resumed")
            token = WriterToken("file-lock", str(os.getpid()), 0, str(path), uuid.uuid4().hex,
                                round_id)
            self._live_tokens.add(token.nonce)
            try:
                yield token
            finally:
                self._live_tokens.discard(token.nonce)

    def _lock_holder(self) -> str | None:
        """PID recorded in the round lock if the lock is currently held, else None."""
        lock = self.workspace / f".{ROUND_LOCK}.lock"
        if not lock.exists():
            return None
        with lock.open("a+") as f:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                f.seek(0)
                return f.read().strip() or "unknown"
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            return None

    def _check_writer(self, writer: WriterToken) -> None:
        if not isinstance(writer, WriterToken) or writer.kind != "file-lock":
            raise WriterError("FileStore requires a file-lock writer token from FileStore.writer()")
        if writer.lock_path != str(self.workspace / f".{ROUND_LOCK}.lock"):
            raise WriterError("writer token belongs to another store")
        if writer.nonce not in self._live_tokens:
            raise WriterError("writer token is stale: its lock context has ended")
        if self._lock_holder() != writer.owner or writer.owner != str(os.getpid()):
            raise WriterError("writer token is stale: this process no longer holds the round lock")

    def _inherited_run(self) -> str | None:
        """Verified INHERITED_LOCK_ENV run id, or None when unset. Raises if it is set but false."""
        value = os.environ.get(INHERITED_LOCK_ENV)
        if not value:
            return None
        pid, _, run_id = value.partition(":")
        if not pid.isdigit() or not _is_ancestor(int(pid)) or self._lock_holder() != pid:
            raise WriterError(f"{INHERITED_LOCK_ENV}={value!r} does not name an ancestor holding "
                              "the round lock")
        return run_id

    def _legacy_snapshots(self) -> list[str]:
        if not self.round_snapshots.exists():
            return []
        return sorted(p.name for p in self.round_snapshots.iterdir()
                      if (p / "snapshot.json").exists())

    def _require_settled(self, *, allow_rounds: Iterable[str] = ()) -> None:
        if pending := self.pending_transactions():
            raise PendingTransaction("recover interrupted store transaction(s) first: "
                                     + ", ".join(t.run_id for t in pending))
        allowed = set(allow_rounds)
        if rounds := [r for r in self._legacy_snapshots() if r not in allowed]:
            raise PendingTransaction("interrupted round snapshot(s) pending: "
                                     + ", ".join(rounds) + "; run_round.py --recover first")

    # -- loading -------------------------------------------------------------------------------

    def _load(self, state: _State, table: str) -> None:
        if table == "entries":
            names = [registry.CURATED] + sorted(
                p.name for p in self.reg.glob("*.yaml") if p.name != registry.CURATED)
            for name in names:
                path = self.reg / name
                if not path.exists():
                    continue
                for e in registry.parse_yaml(path.read_text()).get("sources") or []:
                    if e["id"] in state.entries:
                        raise StoreError(f"duplicate registry id {e['id']} in {name}")
                    state.entries[e["id"]] = e
                    state.entry_file[e["id"]] = name
        elif table == "manifest":
            for path in sorted(self.man.glob("*.jsonl")) if self.man.exists() else []:
                for line in path.read_text().splitlines():
                    if line.strip():
                        row = json.loads(line)
                        state.manifest[row["id"]] = row
        elif table == "blocklist":
            if self.blocklist_path.exists():
                state.blocklist = {norm_url(l) for l in self.blocklist_path.read_text().splitlines()
                                   if l.strip()}
        elif table == "ledger":
            legacy = self.reg / "pruned.jsonl"
            paths = [legacy] if legacy.exists() else self._ledger_files()
            for path in paths:
                state.ledger.extend(json.loads(l) for l in path.read_text().splitlines() if l.strip())
        elif table == "events":
            for path in sorted(self.journal_dir.glob("*.jsonl")) if self.journal_dir.exists() else []:
                state.events.extend(json.loads(l) for l in path.read_text().splitlines() if l.strip())
            state.events.sort(key=lambda e: e["seq"])
        elif table == "rotation":
            if self.rotation_path.exists():
                state.rotation = json.loads(self.rotation_path.read_text())
        elif table == "backend_state":
            if self.backend_state_path.exists():
                state.backend_state = json.loads(self.backend_state_path.read_text())
        else:
            raise StoreError(f"unknown table {table}")

    def _ledger_files(self) -> list[Path]:
        found = []
        for path in self.reg.glob("pruned-*.jsonl"):
            m = re.fullmatch(r"pruned-(\d+)\.jsonl", path.name)
            if m:
                found.append((int(m.group(1)), path))
        return [p for _, p in sorted(found)]

    def _config(self) -> ConfigSnapshot:
        documents, digests = {}, {}
        for name in CONFIG_FILES:
            path = self.reg / name
            if path.exists():
                data = path.read_bytes()
                documents[name] = json.loads(data)
                digests[name] = _sha(data)
        return ConfigSnapshot(documents, digests)

    # -- views ---------------------------------------------------------------------------------

    @contextmanager
    def read(self, *, timeout: float = 0, writer: WriterToken | None = None) -> Iterator["ReadView"]:
        """A consistent read view. Holds the round lock for its lifetime unless the caller passes
        a writer token it holds (whose declared round_id may be in flight), or runs under a verified
        INHERITED_LOCK_ENV parent."""
        if writer is not None:
            self._check_writer(writer)
            self._require_settled(allow_rounds=[writer.round_id] if writer.round_id else [])
            yield from self._view()
        elif (run_id := self._inherited_run()) is not None:
            self._require_settled(allow_rounds=[run_id])
            yield from self._view()
        else:
            with ops.named_lock(ROUND_LOCK, timeout=timeout, workspace=self.workspace):
                self._require_settled()
                yield from self._view()

    def _view(self) -> Iterator["ReadView"]:
        view = ReadView(self, self.version())
        view._config_cache = self._config()  # pinned when the view opens, like its data
        try:
            yield view
        finally:
            view._closed = True

    def pending_transactions(self) -> list[TransactionInfo]:
        if not self.txn_dir.exists():
            return []
        out = []
        for path in sorted(self.txn_dir.iterdir()):
            meta = path / "meta.json"
            if meta.exists():
                out.append(TransactionInfo(path.name, json.loads(meta.read_text())["state"], str(path)))
        return out

    @contextmanager
    def transaction(self, run_id: str, *, expected_version: Version,
                    writer: WriterToken) -> Iterator["WriteView"]:
        _check_run_id(run_id)
        key = str(self.root.resolve())
        if key in _ACTIVE_TRANSACTIONS:
            raise StoreError("transactions do not nest")
        self._check_writer(writer)
        self._require_settled()
        current = self.version()
        committed = self._commit_digest(run_id)
        # A retry of a committed run (e.g. after a lost commit response) cannot know the newer
        # version, so the replay check, not the version check, governs it.
        if committed is None and current != expected_version:
            raise VersionConflict("store changed since the expected version was read")
        view = WriteView(self, current, run_id, replay=committed is not None)
        _ACTIVE_TRANSACTIONS.add(key)
        try:
            yield view  # an exception here discards every buffered change
            view._closed = True
            self._check_writer(writer)
            if self.version() != current:
                raise VersionConflict("store changed during the transaction")
            digest = _digest(view._requests)
            if committed is not None:
                if committed != digest:
                    raise StoreError(f"run {run_id} already committed different requests")
                return  # identical retry of a committed run: no-op
            self._commit(view, digest)
        finally:
            view._closed = True
            _ACTIVE_TRANSACTIONS.discard(key)

    def _commit_digest(self, run_id: str) -> str | None:
        state = _State()
        self._load(state, "events")
        for event in state.events:
            if event.get("run_id") == run_id and event.get("op") == "commit":
                return event.get("digest")
        return None

    def recover(self, run_id: str, *, writer: WriterToken) -> RecoveryResult:
        """Finish an interrupted transaction: finalize it if it committed, otherwise restore its
        files — but only if nothing else has written them since (never restore an older generation
        over newer work)."""
        _check_run_id(run_id)
        self._check_writer(writer)
        path = self.txn_dir / run_id
        meta_path = path / "meta.json"
        if not meta_path.exists():
            if path.exists():  # preparation died before publishing its marker: nothing applied
                _rmtree_durable(path)
                return RecoveryResult(run_id, "discarded")
            raise StoreError(f"no pending store transaction {run_id}")
        meta = json.loads(meta_path.read_text())
        if meta["state"] == "committed":
            _rmtree_durable(path)
            return RecoveryResult(run_id, "finalized")
        if meta["state"] == "rolled_back":  # restored earlier; only cleanup was interrupted
            _rmtree_durable(path)
            return RecoveryResult(run_id, "rolled_back")
        self._rollback(path, meta)
        return RecoveryResult(run_id, "rolled_back")

    def _rollback(self, txn: Path, meta: dict) -> None:
        for item in meta["files"]:
            target = self.root / item["rel"]
            now = _sha(target.read_bytes()) if target.exists() else None
            if now not in (item["pre_sha"], item["post_sha"]):
                raise StoreError(f"{item['rel']} changed after interrupted transaction "
                                 f"{meta['run_id']}; refusing to restore over newer work")
        for item in meta["files"]:
            target = self.root / item["rel"]
            if item["pre_sha"] is None:
                _unlink_durable(target)
            else:
                data = (txn / "state" / item["backup"]).read_bytes()
                if _sha(data) != item["pre_sha"]:
                    raise StoreError(f"backup of {item['rel']} is corrupt")
                _write_durable(target, data)
        # Publish "restored" before deleting the backups, so a crash during cleanup leaves a
        # marker recover() can finish instead of a "prepared" one whose backups are gone.
        meta = {**meta, "state": "rolled_back"}
        _write_durable(txn / "meta.json", (json.dumps(meta, indent=2) + "\n").encode())
        _rmtree_durable(txn)

    # -- commit --------------------------------------------------------------------------------

    def _commit(self, view: "WriteView", digest: str) -> None:
        if not view._requests:
            return  # nothing was requested: there is no identity to record
        # A run whose requests changed nothing still commits its identity (the journal's commit
        # row), so a later retry of it stays a no-op instead of re-applying over newer runs.
        writes: dict[Path, bytes | None] = {}
        self._render_entries(view, writes)
        self._render_manifest(view, writes)
        self._render_appends(view, writes)
        self._render_journal(view, digest, writes)

        # 1. prepare: durable backups of every file we will touch, then the recovery marker
        txn = self.txn_dir / view.run_id
        if (txn / "meta.json").exists():
            raise PendingTransaction(f"transaction {view.run_id} is pending recovery")
        # A directory without a published marker is an interrupted preparation: no data file was
        # touched before the marker, so it is always safe to discard.
        if txn.exists():
            _rmtree_durable(txn)
        try:
            _mkdir_durable(txn / "state")
            files = []
            for n, path in enumerate(sorted(writes)):
                pre = path.read_bytes() if path.exists() else None
                item = {"rel": str(path.relative_to(self.root)), "pre_sha": _sha(pre),
                        "post_sha": _sha(writes[path]), "backup": None}
                if pre is not None:
                    item["backup"] = f"{n}.bin"
                    _write_durable(txn / "state" / item["backup"], pre)
                files.append(item)
            meta = {"run_id": view.run_id, "state": "prepared", "files": files}
            _write_durable(txn / "meta.json", (json.dumps(meta, indent=2) + "\n").encode())
        except BaseException:
            if not (txn / "meta.json").exists():
                try:
                    _rmtree_durable(txn)
                except Exception:
                    pass  # an unmarked directory is discarded by the next commit or recover()
            raise
        # 2. apply; an ordinary failure rolls back at once, a crash leaves "prepared" for recover()
        try:
            for path, data in sorted(writes.items()):
                if data is None:
                    _unlink_durable(path)
                else:
                    _write_durable(path, data)
            meta["state"] = "committed"
            _write_durable(txn / "meta.json", (json.dumps(meta, indent=2) + "\n").encode())
        except BaseException:
            if meta["state"] == "prepared":
                try:
                    self._rollback(txn, meta)
                except Exception:
                    pass  # stays pending: reads and writes are refused until recover()
            raise
        # 3. finalize
        _rmtree_durable(txn)

    def _render_entries(self, view: "WriteView", writes: dict) -> None:
        if "entries" not in view._dirty:
            return
        base, state = view._base, view._state
        removed = {sid for sid in base.entries
                   if sid not in state.entries or state.entries[sid] != base.entries[sid]}
        added = [state.entries[sid] for sid in sorted(state.entries)
                 if sid not in base.entries or state.entries[sid] != base.entries[sid]]
        texts: dict[str, str] = {}
        drop_by_file: dict[str, set] = {}
        for sid in removed:
            drop_by_file.setdefault(base.entry_file[sid], set()).add(sid)
        for name, drop in sorted(drop_by_file.items()):
            texts[name], n = registry.remove_ids_from_text(
                (self.reg / name).read_text(), drop, name)
            if n != len(drop):
                raise StoreError(f"{name}: removed {n} of {len(drop)} entries")
        for e in added:
            name = registry.shard_filename(e["id"])
            if name not in texts:
                path = self.reg / name
                texts[name] = path.read_text() if path.exists() else registry.shard_header(
                    Path(name).stem)
            texts[name] += registry.emit_entry(e)
        targets: dict[str, list] = {}
        for sid, name in view._entry_files().items():
            targets.setdefault(name, []).append(sid)
        for name, text in texts.items():
            got = sorted(e["id"] for e in registry.parse_yaml(text).get("sources") or [])
            if got != sorted(targets.get(name, [])):
                raise StoreError(f"{name}: rendered shard does not match the transaction state")
            writes[self.reg / name] = text.encode()

    def _render_manifest(self, view: "WriteView", writes: dict) -> None:
        if "manifest" not in view._dirty:
            return
        base, state = view._base, view._state
        changed = {sid for sid in set(base.manifest) | set(state.manifest)
                   if base.manifest.get(sid) != state.manifest.get(sid)}
        groups: dict[str, list] = {registry.manifest_shard(sid): [] for sid in changed}
        for sid, row in state.manifest.items():
            stem = registry.manifest_shard(sid)
            if stem in groups:
                groups[stem].append(row)
        for stem, rows in groups.items():
            writes[self.man / f"{stem}.jsonl"] = (
                registry.manifest_shard_text(rows).encode() if rows else None)

    def _render_appends(self, view: "WriteView", writes: dict) -> None:
        base, state = view._base, view._state
        if "blocklist" in view._dirty:
            new = sorted(state.blocklist - base.blocklist)
            old = self.blocklist_path.read_text() if self.blocklist_path.exists() else ""
            if old and not old.endswith("\n"):
                old += "\n"
            writes[self.blocklist_path] = (old + "".join(f"{u}\n" for u in new)).encode()
        if "ledger" in view._dirty:
            by_file: dict[Path, list] = {}
            for row in state.ledger[len(base.ledger):]:
                by_file.setdefault(self.reg / registry.prune_ledger_name(row["id"]), []).append(row)
            for path, rows in by_file.items():
                old = path.read_bytes() if path.exists() else b""
                writes[path] = old + "".join(
                    json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows).encode()
        if "rotation" in view._dirty:
            writes[self.rotation_path] = (
                json.dumps(state.rotation, indent=2, ensure_ascii=False) + "\n").encode()
        if "backend_state" in view._dirty:
            writes[self.backend_state_path] = (json.dumps(
                state.backend_state, indent=2, ensure_ascii=False, sort_keys=True) + "\n").encode()

    def _render_journal(self, view: "WriteView", digest: str, writes: dict) -> None:
        state = _State()
        self._load(state, "events")
        seq = state.events[-1]["seq"] if state.events else 0
        at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        rows = []
        for n, op in enumerate(view._ops, 1):
            seq += 1
            rows.append({"seq": seq, "event_id": f"{view.run_id}:{n}", "run_id": view.run_id,
                         "at": at, **op})
        rows.append({"seq": seq + 1, "event_id": f"{view.run_id}:commit", "run_id": view.run_id,
                     "at": at, "table": None, "op": "commit", "id": None, "digest": digest})
        path = self.journal_dir / f"{at[:7]}.jsonl"
        old = path.read_bytes() if path.exists() else b""
        writes[path] = old + "".join(
            json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows).encode()

    def export(self, directory: Path, *, view: "ReadView") -> ExportReport:
        return export(directory, view=view)


# --- canonical export (public API only, so every backend shares it) ------------------------------

class _StreamFile:
    """Write-through file that hashes and counts lines, published atomically on close."""

    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        self.tmp = Path(tmp)
        self.f = os.fdopen(fd, "wb")
        self.hash = hashlib.sha256()
        self.lines = 0

    def write(self, text: str) -> None:
        data = text.encode()
        self.f.write(data)
        self.hash.update(data)
        self.lines += text.count("\n")

    def close(self) -> dict:
        self.f.flush()
        os.fsync(self.f.fileno())
        self.f.close()
        os.replace(self.tmp, self.path)
        return {"sha256": self.hash.hexdigest(), "rows": self.lines}


def export(directory: Path, *, view: "ReadView") -> ExportReport:
    """Deterministic canonical export of every table, the runtime state and the pinned
    configuration, streamed page by page through the public view API. Two stores holding the same
    state produce byte-identical directories."""
    directory = Path(directory)
    files: dict[str, dict] = {}

    def dump(obj) -> str:
        return json.dumps(obj, ensure_ascii=False, sort_keys=True, indent=2) + "\n"

    for name, table in (("entries.jsonl", Table.ENTRIES), ("manifest.jsonl", Table.MANIFEST),
                        ("blocklist.txt", Table.BLOCKLIST), ("ledger.jsonl", Table.LEDGER),
                        ("events.jsonl", Table.EVENTS)):
        out = _StreamFile(directory / name)
        cursor = None
        while True:
            page = view.scan(table, cursor=cursor, limit=MAX_PAGE)
            for row in page.rows:
                out.write(f"{row['url']}\n" if table is Table.BLOCKLIST else
                          json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
        files[name] = out.close()
    config = view.config_get()
    singles = {
        "rotation.json": view.rotation_get(),
        "backend_state.json": {k: _plain(v) for k, v in view.backend_state_get().items()},
        **{f"config/{name}": doc for name, doc in config.documents.items()},
    }
    for name, obj in singles.items():
        out = _StreamFile(directory / name)
        out.write(dump(obj))
        files[name] = {**out.close(), "rows": None}
    summary = _StreamFile(directory / "EXPORT.json")
    summary.write(dump({"schema": 1, "files": files, "config_digests": config.digests}))
    summary.close()
    return ExportReport(str(directory), files)


# --- read view -----------------------------------------------------------------------------------

class ReadView:
    def __init__(self, store: FileStore, version: Version):
        self._store = store
        self._state = _State()
        self._version = version
        self._id = uuid.uuid4().hex
        self._closed = False
        self._loaded_tables: set[str] = set()
        self._known_cache: tuple[set, set, set] | None = None
        self._config_cache: ConfigSnapshot | None = None

    def _check_open(self) -> None:
        if self._closed:
            raise StaleView("view is closed")

    def _check_generation(self) -> None:
        # Under the round lock the generation can only move if this same process wrote through
        # another view (e.g. read(writer=w) across a transaction committed with w).
        if self._store.version() != self._version:
            raise StaleView("store moved past this view's generation")

    def _get(self, table: str) -> _State:
        self._check_open()
        if table not in self._loaded_tables:
            self._check_generation()
            self._store._load(self._state, table)
            self._loaded_tables.add(table)
        return self._state

    def version(self) -> Version:
        self._check_open()
        return self._version

    def _keyed(self, table: Table) -> list[tuple[tuple, dict]]:
        """Each table's scan key and order — the single definition every backend matches. Cached
        per view (a write view drops the cache on every mutation) so paging stays linear."""
        cache = self.__dict__.setdefault("_keyed_cache", {})
        if table not in cache:
            cache[table] = self._keyed_uncached(table)
        return cache[table]

    def _keyed_uncached(self, table: Table) -> list[tuple[tuple, dict]]:
        if table is Table.ENTRIES:
            rows = self._get("entries").entries
            return [((sid,), rows[sid]) for sid in sorted(rows)]
        if table is Table.MANIFEST:
            rows = self._get("manifest").manifest
            return [((sid,), rows[sid]) for sid in sorted(rows)]
        if table is Table.BLOCKLIST:
            return sorted(((key_digest(u),), {"url": u}) for u in self._get("blocklist").blocklist)
        if table is Table.LEDGER:
            out, seen = [], {}
            for digest, row in sorted(((key_digest(_canonical(r)), r)
                                       for r in self._get("ledger").ledger), key=lambda t: t[0]):
                n = seen[digest] = seen.get(digest, -1) + 1  # identical rows stay distinct
                out.append(((digest, n), row))
            return out
        if table is Table.EVENTS:
            return [((e["seq"],), e) for e in self._get("events").events]
        raise StoreError(f"unknown table {table}")

    def _validate(self, table: Table, where: Predicate | None, fields) -> None:
        allowed = TABLE_FIELDS[Table(table)]
        used = (predicate_fields(where) if where is not None else set()) | set(fields or ())
        if unknown := sorted(used - allowed):
            raise StoreError(f"unknown {Table(table).value} field(s): {', '.join(unknown)}")

    def _cursor_scope(self) -> str:
        return self._id

    def scan(self, table: Table, *, where: Predicate | None = None,
             fields: tuple[str, ...] | None = None, cursor: Cursor | None = None,
             limit: int = DEFAULT_PAGE) -> Page:
        table = Table(table)
        self._validate(table, where, fields)
        if not 1 <= limit <= MAX_PAGE:
            raise StoreError(f"limit must be within 1..{MAX_PAGE}")
        query = _digest([table.value, repr(where), list(fields) if fields else None])
        if cursor is not None and (cursor.view != self._cursor_scope() or cursor.query != query):
            raise StoreError("cursor belongs to a different view, generation or query")
        rows, last = [], None
        keyed = self._keyed(table)
        start = 0
        if cursor is not None:
            import bisect
            start = bisect.bisect_right(keyed, cursor.last_key, key=lambda kr: kr[0])
        for key, row in keyed[start:] if start else keyed:
            if not evaluate(where, row):
                continue
            if len(rows) == limit:
                return Page(rows, Cursor(self._cursor_scope(), query, last))
            rows.append(copy.deepcopy({f: row[f] for f in fields if f in row} if fields else row))
            last = key
        return Page(rows, None)

    def get_manifest(self, ids: Iterable[str]) -> dict[str, dict]:
        rows = self._get("manifest").manifest
        return {sid: copy.deepcopy(rows[sid]) for sid in dict.fromkeys(ids) if sid in rows}

    def _known_sets(self) -> tuple[set, set, set]:
        if self._known_cache is None:
            urls, titles, ids = set(), set(), set()
            for rows in (self._get("manifest").manifest, self._get("entries").entries):
                for row in rows.values():
                    if u := norm_url(row.get("url")):
                        urls.add(u)
                    if t := norm_title(row.get("title")):
                        titles.add(t)
                    if row.get("id"):
                        ids.add(row["id"])
            self._known_cache = (urls, titles, ids)
        return self._known_cache

    def known(self, *, urls: Iterable[str] = (), titles: Iterable[str] = (),
              ids: Iterable[str] = (), include_blocklist: bool = True) -> KnownHits:
        """Which candidates are already known (normalized as in the module contract). At most
        MAX_KNOWN candidates per kind. Uncommitted local additions participate."""
        self._check_open()
        cand_u = {u for u in map(norm_url, urls) if u}
        cand_t = {t for t in map(norm_title, titles) if t}
        cand_i = {i for i in ids if i}
        for label, values in (("urls", cand_u), ("titles", cand_t), ("ids", cand_i)):
            if len(values) > MAX_KNOWN:
                raise StoreError(f"known(): at most {MAX_KNOWN} {label} per call")
        if indexed := self._known_indexed(cand_u, cand_t, cand_i, include_blocklist):
            return indexed
        known_u, known_t, known_i = self._known_sets()
        hit_u = cand_u & known_u
        if include_blocklist:
            hit_u |= cand_u & self._get("blocklist").blocklist
        return KnownHits(frozenset(hit_u), frozenset(cand_t & known_t), frozenset(cand_i & known_i))

    def _known_indexed(self, urls, titles, ids, include_blocklist) -> KnownHits | None:
        """Answer from the SQLite acceleration index when it can: it covers registry + manifest +
        blocklist together, so it serves include_blocklist=True reads with no uncommitted local
        changes. Parsing every shard instead costs minutes (246 s at 1.6M docs, 2026-09-24)."""
        if (not include_blocklist or self._dirty_keys()
                or os.environ.get("NEKAISE_DISABLE_INDEX") == "1"):
            return None
        self._check_generation()
        try:
            import corpus_index
            args = (self._store.reg, self._store.man, self._store.blocklist_path)
            return KnownHits(frozenset(corpus_index.lookup(*args, "url", urls)),
                             frozenset(corpus_index.lookup(*args, "title", titles)),
                             frozenset(corpus_index.lookup(*args, "id", ids)))
        except Exception:
            return None  # the index is a cache, never a correctness dependency

    def _dirty_keys(self) -> bool:
        return False

    def aggregate_manifest(self, *, group_by: tuple[str, ...], where: Predicate | None = None,
                           sums: tuple[str, ...] = (), count: bool = True) -> Iterator[dict]:
        """Group manifest rows by string/boolean/None fields (missing and null are both None; any
        other value raises). Sums follow exact_sum. Output: the group_by fields, then "count" and
        "sum_<field>", ordered by the canonical JSON of the group key."""
        self._validate(Table.MANIFEST, where, tuple(group_by) + tuple(sums))
        groups: dict[str, tuple[tuple, int, dict]] = {}
        for row in self._get("manifest").manifest.values():
            if not evaluate(where, row):
                continue
            key = tuple(check_group_value(f, row.get(f)) for f in group_by)
            k = _canonical(key)
            _, n, values = groups.get(k) or (key, 0, {f: [] for f in sums})
            for f in sums:
                values[f].append(row.get(f))
            groups[k] = (key, n + 1, values)
        for k in sorted(groups):
            key, n, values = groups[k]
            yield {**dict(zip(group_by, key)), **({"count": n} if count else {}),
                   **{f"sum_{f}": exact_sum(values[f]) for f in sums}}

    def iter_duplicate_sha256(self, *, where: Predicate | None = None,
                              batch_size: int = DEFAULT_PAGE) -> Iterator[dict]:
        """Rows whose non-empty sha256 is shared with another matching row, ordered (sha256, id)."""
        self._validate(Table.MANIFEST, where, ("sha256",))
        if not 1 <= batch_size <= MAX_PAGE:
            raise StoreError(f"batch_size must be within 1..{MAX_PAGE}")
        rows = self._get("manifest").manifest
        by_sha: dict[str, list] = {}
        for sid, row in rows.items():
            sha = row.get("sha256")
            if sha and evaluate(where, row):
                by_sha.setdefault(sha, []).append(sid)
        for sha in sorted(by_sha):
            if len(by_sha[sha]) > 1:
                for sid in sorted(by_sha[sha]):
                    yield copy.deepcopy(rows[sid])

    def rotation_get(self, name: str | None = None) -> dict:
        rotation = self._get("rotation").rotation
        return copy.deepcopy(rotation if name is None else rotation[name])

    def config_get(self) -> ConfigSnapshot:
        self._check_open()
        if self._config_cache is None:  # write views pin it on first use, generation-checked
            self._check_generation()
            self._config_cache = self._store._config()
        return copy.deepcopy(self._config_cache)

    def backend_state_get(self, name: str | None = None):
        """Runtime state (never the git-owned configuration) of every configured backend, or of
        one; backends without recorded runtime state are enabled."""
        runtime = self._get("backend_state").backend_state
        names = [k for k in self.config_get().backends if not k.startswith("_")]
        states = {k: BackendState(**runtime[k]) if k in runtime else BackendState()
                  for k in sorted(set(names) | set(runtime))}
        return states if name is None else states.get(name, BackendState())

    def backend_enabled(self, name: str) -> bool:
        """Effective enablement: the configuration AND the runtime state must both allow it."""
        cfg = self.config_get().backends.get(name)
        if cfg is None or name.startswith("_"):
            raise StoreError(f"unknown backend {name}")
        return bool(cfg.get("enabled", True)) and self.backend_state_get(name).enabled

    def resolve_artifact(self, id: str, stage: Stage) -> ArtifactRef | None:  # noqa: A002
        """Where a document's stage payload lives, from its manifest row. Reads no payload and
        grants no eligibility."""
        return artifact_ref(self._get("manifest").manifest.get(id), id, stage)


# --- write view -----------------------------------------------------------------------------------

def _materialize(value: Any) -> Any:
    """Lists for one-shot or view iterables (generators, dict views, map objects), so a request
    can be both recorded and applied; values _plain already understands pass through."""
    if isinstance(value, (str, bytes, Mapping, list, tuple, set, frozenset, Enum)) or (
            dataclasses.is_dataclass(value) and not isinstance(value, type)):
        return value
    if isinstance(value, Iterable):
        return list(value)
    return value


def _mutation(fn):
    """Record the call as a request (the replay identity), then apply it unless replaying."""
    def wrapper(self, *args, **kwargs):
        self._check_open()
        args = [_materialize(a) for a in args]
        kwargs = {k: _materialize(v) for k, v in kwargs.items()}
        self._requests.append({"call": fn.__name__, "args": _plain(args), "kwargs": _plain(kwargs)})
        if self._replay:
            return None if fn.__name__ in ("rotation_set", "backend_state_set") else 0
        return fn(self, *args, **kwargs)
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


class WriteView(ReadView):
    """Buffered mutations over a copy of the loaded state; nothing touches disk until commit.
    A cursor from this view is invalidated by any later mutation in it."""

    def __init__(self, store: FileStore, version: Version, run_id: str, *, replay: bool = False):
        super().__init__(store, version)
        self.run_id = run_id
        self._replay = replay
        self._base = _State()
        self._dirty: set[str] = set()
        self._ops: list[dict] = []
        self._requests: list[dict] = []

    def _get(self, table: str) -> _State:
        self._check_open()
        if table not in self._loaded_tables:
            self._check_generation()
            self._store._load(self._base, table)
            # Shallow copy: rows are only ever replaced, never mutated in place, so base rows stay
            # the pre-transaction truth without deep-copying the whole manifest.
            setattr(self._state, table, copy.copy(getattr(self._base, table)))
            if table == "entries":
                self._state.entry_file = dict(self._base.entry_file)
            self._loaded_tables.add(table)
        return self._state

    def _cursor_scope(self) -> str:
        return f"{self._id}:{len(self._ops)}"

    def _dirty_keys(self) -> bool:
        return bool(self._dirty & {"entries", "manifest", "blocklist"})

    def _entry_files(self) -> dict[str, str]:
        state = self._state  # commit-time only: entries are loaded whenever they are dirty
        return {sid: registry.shard_filename(sid) if (
                    sid not in self._base.entries or state.entries[sid] != self._base.entries[sid])
                else self._base.entry_file[sid] for sid in state.entries}

    def _touch(self, table: str) -> None:
        self._dirty.add(table)
        self._known_cache = None
        self.__dict__.pop("_keyed_cache", None)

    def _record(self, table: str, op: str, sid, before=None, after=None, reason=None) -> None:
        self._ops.append({"table": table, "op": op, "id": sid, "before": copy.deepcopy(before),
                          "after": copy.deepcopy(after), "reason": reason})

    @staticmethod
    def _unique_ids(rows: Sequence[Mapping], what: str) -> None:
        ids = [r.get("id") for r in rows]
        if any(not isinstance(i, str) or not i for i in ids):
            raise StoreError(f"{what}: every row needs a non-empty string id")
        if len(set(ids)) != len(ids):
            raise StoreError(f"{what}: duplicate ids in one batch")

    @staticmethod
    def _entry_row(e: Mapping) -> dict:
        missing = [f for f in registry.REQUIRED_FIELDS if not e.get(f)]
        if missing:
            raise StoreError(f"entry {e.get('id')!r} lacks {', '.join(missing)}")
        return copy.deepcopy({k: e[k] for k in registry.FIELDS if k in e and e[k] not in (None, "")})

    def uniquify_ids(self, entries: Sequence[Mapping]) -> list[dict]:
        """Copies of `entries` with registry.uniquify_ids' suffixing against every known id and the
        batch itself. Computes names only; insert_entries remains the collision authority."""
        taken = self._known_sets()[2]
        return uniquify(entries, taken.__contains__)

    @_mutation
    def insert_entries(self, entries: Iterable[Mapping]) -> int:
        rows = [self._entry_row(e) for e in entries]
        self._unique_ids(rows, "insert_entries")
        state = self._get("entries")
        if clash := sorted(r["id"] for r in rows if r["id"] in state.entries):
            raise StoreError(f"insert_entries: id(s) already exist: {', '.join(clash[:5])}")
        for r in rows:
            state.entries[r["id"]] = r
            self._record("entries", "insert", r["id"], after=r)
        if rows:
            self._touch("entries")
        return len(rows)

    @_mutation
    def upsert_entries(self, entries: Iterable[Mapping]) -> int:
        rows = [self._entry_row(e) for e in entries]
        self._unique_ids(rows, "upsert_entries")
        state = self._get("entries")
        changed = 0
        for r in rows:
            before = state.entries.get(r["id"])
            if before == r:
                continue
            state.entries[r["id"]] = r
            self._record("entries", "upsert", r["id"], before=before, after=r)
            changed += 1
        if changed:
            self._touch("entries")
        return changed

    @_mutation
    def delete_entries(self, ids: Iterable[str], *, reason: str) -> int:
        if not reason:
            raise StoreError("delete_entries requires a reason")
        state = self._get("entries")
        changed = 0
        for sid in dict.fromkeys(ids):
            if sid in state.entries:
                self._record("entries", "delete", sid, before=state.entries.pop(sid), reason=reason)
                changed += 1
        if changed:
            self._touch("entries")
        return changed

    def _upsert_manifest(self, rows: list[dict]) -> int:
        self._unique_ids(rows, "upsert_manifest")
        state = self._get("manifest")
        changed = 0
        for r in rows:
            before = state.manifest.get(r["id"])
            if before == r:
                continue
            state.manifest[r["id"]] = r
            self._record("manifest", "upsert", r["id"], before=before, after=r)
            changed += 1
        if changed:
            self._touch("manifest")
        return changed

    def _delete_manifest(self, ids: Iterable[str], reason: str) -> int:
        if not reason:
            raise StoreError("delete_manifest requires a reason")
        state = self._get("manifest")
        changed = 0
        for sid in dict.fromkeys(ids):
            if sid in state.manifest:
                self._record("manifest", "delete", sid, before=state.manifest.pop(sid), reason=reason)
                changed += 1
        if changed:
            self._touch("manifest")
        return changed

    @_mutation
    def upsert_manifest(self, rows: Iterable[Mapping]) -> int:
        return self._upsert_manifest([copy.deepcopy(dict(r)) for r in rows])

    @_mutation
    def replace_manifest(self, rows: Iterable[Mapping], *, reason: str) -> int:
        """write_manifest_rows semantics: `rows` becomes the whole manifest; omitted ids are
        deleted with tombstones carrying `reason`."""
        if not reason:
            raise StoreError("replace_manifest requires a reason")
        rows = [copy.deepcopy(dict(r)) for r in rows]
        self._unique_ids(rows, "replace_manifest")
        keep = {r["id"] for r in rows}
        gone = [sid for sid in self._get("manifest").manifest if sid not in keep]
        return self._delete_manifest(gone, reason) + self._upsert_manifest(rows)

    @_mutation
    def update_manifest_fields(self, updates: Mapping[str, Mapping[str, Any]], *,
                               unset: tuple[str, ...] = ()) -> int:
        state = self._get("manifest")
        validate_patch(updates, unset, lambda ids: [i for i in ids if i in state.manifest])
        changed = 0
        for sid, patch in updates.items():
            before = state.manifest[sid]
            after = {k: v for k, v in before.items() if k not in unset}
            after.update(copy.deepcopy(dict(patch)))
            if after == before:
                continue
            state.manifest[sid] = after
            self._record("manifest", "update", sid, before=before, after=after)
            changed += 1
        if changed:
            self._touch("manifest")
        return changed

    @_mutation
    def delete_manifest(self, ids: Iterable[str], *, reason: str) -> int:
        return self._delete_manifest(ids, reason)

    @_mutation
    def blocklist_add(self, urls: Iterable[str]) -> int:
        state = self._get("blocklist")
        new = sorted({u for u in map(norm_url, urls) if u} - state.blocklist)
        for u in new:
            state.blocklist.add(u)
            self._record("blocklist", "insert", u, after={"url": u})
        if new:
            self._touch("blocklist")
        return len(new)

    @_mutation
    def ledger_append(self, rows: Iterable[Mapping]) -> int:
        rows = [copy.deepcopy(dict(r)) for r in rows]
        validate_ledger_rows(rows)
        state = self._get("ledger")
        for r in rows:
            state.ledger.append(r)
            self._record("ledger", "insert", r["id"], after=r)
        if rows:
            self._touch("ledger")
        return len(rows)

    @_mutation
    def rotation_set(self, name: str, value: Mapping) -> None:
        if not isinstance(value, Mapping):
            raise StoreError("rotation value must be a mapping")
        state = self._get("rotation")
        before = state.rotation.get(name)
        after = copy.deepcopy(dict(value))
        if before == after:
            return
        state.rotation[name] = after
        self._record("rotation", "upsert", name, before=before, after=after)
        self._touch("rotation")

    @_mutation
    def backend_state_set(self, name: str, value: BackendState) -> None:
        """Record runtime state (e.g. exhaustion). Never touches the git-owned configuration."""
        if name.startswith("_") or name not in self.config_get().backends:
            raise StoreError(f"unknown backend {name}")
        state = self._get("backend_state")
        before = state.backend_state.get(name)
        after = {"enabled": bool(value.enabled), "reason": value.reason}
        if (before or {"enabled": True, "reason": None}) == after:
            return
        state.backend_state[name] = after
        self._record("backend_state", "upsert", name, before=before, after=after)
        self._touch("backend_state")
