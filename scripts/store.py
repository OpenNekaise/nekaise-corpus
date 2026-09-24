#!/usr/bin/env python3
"""store.py — the storage interface for all tracked corpus state (ADR 0001, stage 1).

docs/decisions/0001-storage-architecture.md moves the registry, manifest, prune ledger, blocklist
and control state out of git into PostgreSQL. This module is the seam that makes that move
incremental: every reader and writer will talk to a `Store`, and `FileStore` implements the
interface over today's files (registry/*.yaml, manifest/*.jsonl, registry/pruned-*.jsonl,
pruned_urls.txt, registry/rotation.json, registry/backends.json). Stage 2 adds a PostgreSQL store
that must pass the same conformance suite (tests/test_store_contract.py) and produce byte-identical
canonical exports.

Contract (decided in the stage-1 review, see the ADR):

* Reads happen inside a `ReadView` that stays consistent for its lifetime. FileStore gets that
  from the canonical round lock; PostgreSQL will use a snapshot.
* Writes happen inside `transaction()`: read-your-writes, commit on clean exit, rollback on any
  exception, all-or-nothing across every table, guarded by an expected `Version` and a
  `WriterToken` that proves ownership (the round lock here, a fencing epoch in PostgreSQL).
* Every mutation is validated as a whole batch before anything changes, returns the number of rows
  it actually changed, and is journaled with before/after rows so deletions are tombstones.
* Scans are ordered and keyset-paginated: entries/manifest by id, blocklist by URL, ledger by its
  canonical JSON, events by sequence.
* Filters are a small typed predicate language; an unknown field or operator raises instead of
  silently filtering client-side.

FileStore holds each table fully in memory while a view is open — the same cost as today's
load-everything callers, and the limitation the PostgreSQL store removes. Stage 1 converts no
production caller; registry.py keeps its legacy functions with unchanged behavior.
"""
from __future__ import annotations

import copy
import fcntl
import hashlib
import json
import os
import re
import shutil
import time
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
# Set by a parent that already holds the round lock (run_round, from stage 3 on) so the child
# processes it spawns can open read views without deadlocking on the lock their parent owns.
INHERITED_LOCK_ENV = "NEKAISE_STORE_LOCK_INHERITED"
CONFIG_FILES = ("backends.json", "eligibility.json", "vendors.json")


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
    """An interrupted transaction must be recovered before new writes."""


# --- predicates --------------------------------------------------------------------------------
# Two-valued logic over present/missing fields: every leaf is False on a missing field, except
# Exists, which tests presence (an explicit null is present). Not() negates the result exactly.

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


def evaluate(pred: Predicate | None, row: Mapping) -> bool:
    if pred is None:
        return True
    if isinstance(pred, Eq):
        return pred.field in row and row[pred.field] == pred.value
    if isinstance(pred, In):
        return pred.field in row and row[pred.field] in pred.values
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


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


# --- value types -------------------------------------------------------------------------------

@dataclass(frozen=True)
class Version:
    token: str


@dataclass(frozen=True)
class Cursor:
    version: str
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
    enabled: bool
    reason: str | None = None


@dataclass(frozen=True)
class ConfigSnapshot:
    backends: dict
    eligibility: dict
    digests: dict


@dataclass(frozen=True)
class WriterToken:
    kind: str
    owner: str
    epoch: int
    lock_path: str


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
    raise StoreError(f"storage backend {name!r} is not available (known: file)")


# --- normalization shared with registry.existing_keys / blocklist -----------------------------

def norm_url(url: str | None) -> str:
    return blocklist_mod.normalize(url or "")


def norm_title(title: str | None) -> str:
    return registry.norm(title or "")


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
    backends_raw: dict = field(default_factory=dict)   # backends.json incl. _readme


class FileStore:
    """Store over the git-tracked file layout. Authoritative until the stage-4 cutover."""

    def __init__(self, root: Path = ROOT):
        self.root = Path(root)
        self.reg = self.root / "registry"
        self.man = self.root / "manifest"
        self.blocklist_path = self.root / "pruned_urls.txt"
        self.rotation_path = self.reg / "rotation.json"
        self.backends_path = self.reg / "backends.json"
        self.journal_dir = self.reg / "journal"
        self.workspace = self.root / "workspace"
        self.txn_dir = self.workspace / "store-transactions"

    # -- versions and locking ------------------------------------------------------------------

    def _tracked_files(self) -> list[Path]:
        files = [p for p in self.reg.rglob("*") if p.is_file()] if self.reg.exists() else []
        files += sorted(self.man.glob("*.jsonl")) if self.man.exists() else []
        if self.blocklist_path.exists():
            files.append(self.blocklist_path)
        return sorted(files)

    def version(self) -> Version:
        rows = []
        for path in self._tracked_files():
            st = path.stat()
            rows.append((str(path.relative_to(self.root)), st.st_size, st.st_mtime_ns, st.st_ino))
        return Version(_digest(rows))

    @contextmanager
    def writer(self, timeout: float = 0) -> Iterator[WriterToken]:
        """Hold the canonical round lock and yield the token that proves it."""
        with ops.named_lock(ROUND_LOCK, timeout=timeout, workspace=self.workspace) as path:
            yield WriterToken("file-lock", str(os.getpid()), 0, str(path))

    def _check_writer(self, writer: WriterToken) -> None:
        if not isinstance(writer, WriterToken) or writer.kind != "file-lock":
            raise WriterError("FileStore requires a file-lock writer token from FileStore.writer()")
        lock = Path(writer.lock_path)
        if lock != self.workspace / f".{ROUND_LOCK}.lock" or writer.owner != str(os.getpid()):
            raise WriterError("writer token belongs to another store or process")
        # Ownership check: if a fresh descriptor can take the lock, nobody (in particular not this
        # token's holder) owns it any more.
        with lock.open("a+") as f:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                f.seek(0)
                if f.read().strip() != writer.owner:
                    raise WriterError("round lock is held by a different process")
                return
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)
            raise WriterError("writer token is stale: the round lock is not held")

    # -- loading -------------------------------------------------------------------------------

    def _load(self, state: _State, table: str) -> None:
        if table == "entries":
            for path in (registry.CURATED, *sorted(
                    p.name for p in self.reg.glob("*.yaml") if p.name != registry.CURATED)):
                full = self.reg / path
                if not full.exists():
                    continue
                for e in registry.parse_yaml(full.read_text()).get("sources") or []:
                    if e["id"] in state.entries:
                        raise StoreError(f"duplicate registry id {e['id']} in {path}")
                    state.entries[e["id"]] = e
                    state.entry_file[e["id"]] = path
        elif table == "manifest":
            for path in sorted(self.man.glob("*.jsonl")) if self.man.exists() else []:
                for line in path.read_text().splitlines():
                    if line.strip():
                        row = json.loads(line)
                        state.manifest[row["id"]] = row
        elif table == "blocklist":
            state.blocklist = self._load_blocklist()
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
            state.rotation = json.loads(self.rotation_path.read_text()) \
                if self.rotation_path.exists() else {}
        elif table == "backends":
            state.backends_raw = json.loads(self.backends_path.read_text()) \
                if self.backends_path.exists() else {}
        else:
            raise StoreError(f"unknown table {table}")

    def _load_blocklist(self) -> set[str]:
        if not self.blocklist_path.exists():
            return set()
        return {norm_url(l) for l in self.blocklist_path.read_text().splitlines() if l.strip()}

    def _ledger_files(self) -> list[Path]:
        found = []
        for path in self.reg.glob("pruned-*.jsonl"):
            m = re.fullmatch(r"pruned-(\d+)\.jsonl", path.name)
            if m:
                found.append((int(m.group(1)), path))
        return [p for _, p in sorted(found)]

    # -- views ---------------------------------------------------------------------------------

    @contextmanager
    def read(self, *, timeout: float = 0, writer: WriterToken | None = None) -> Iterator["ReadView"]:
        """Consistent read view. Holds the round lock for its lifetime unless the caller passes
        the writer token it already holds, or a parent holding the lock set INHERITED_LOCK_ENV."""
        if writer is not None:
            self._check_writer(writer)
            yield ReadView(self, _State(), self.version())
        elif os.environ.get(INHERITED_LOCK_ENV) == "1":
            yield ReadView(self, _State(), self.version())
        else:
            with ops.named_lock(ROUND_LOCK, timeout=timeout, workspace=self.workspace):
                yield ReadView(self, _State(), self.version())

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
        if not run_id or "/" in run_id or run_id.startswith("."):
            raise StoreError(f"invalid run id {run_id!r}")
        self._check_writer(writer)
        if pending := self.pending_transactions():
            raise PendingTransaction(
                "recover interrupted store transaction(s) first: "
                + ", ".join(t.run_id for t in pending))
        current = self.version()
        if current != expected_version:
            raise VersionConflict("store changed since the expected version was read")
        view = WriteView(self, _State(), current, run_id)
        yield view  # an exception propagates here and discards the buffered changes
        self._check_writer(writer)
        if self.version() != current:
            raise VersionConflict("store changed during the transaction")
        self._commit(view)

    def recover(self, run_id: str, *, writer: WriterToken) -> RecoveryResult:
        self._check_writer(writer)
        path = self.txn_dir / run_id
        meta_path = path / "meta.json"
        if not meta_path.exists():
            raise StoreError(f"no pending store transaction {run_id}")
        meta = json.loads(meta_path.read_text())
        if meta["state"] == "committed":
            shutil.rmtree(path)
            return RecoveryResult(run_id, "finalized")
        for item in meta["files"]:
            target = self.root / item["rel"]
            if item["existed"]:
                ops.atomic_write_bytes(target, (path / "state" / item["rel"]).read_bytes())
            elif target.exists():
                target.unlink()
        shutil.rmtree(path)
        return RecoveryResult(run_id, "rolled_back")

    # -- commit --------------------------------------------------------------------------------

    def _commit(self, view: "WriteView") -> None:
        if not view._ops:
            return
        state, base = view._state, view._base
        digest = _digest(view._ops)
        for event in base.events if view._loaded("events") else self._events():
            if event.get("run_id") == view.run_id and event.get("op") == "commit":
                if event.get("digest") == digest:
                    return  # identical retry of a committed run: idempotent no-op
                raise StoreError(f"run {view.run_id} already committed different changes")
        writes: dict[Path, bytes | None] = {}
        self._render_entries(view, writes)
        self._render_manifest(view, writes)
        if "blocklist" in view._dirty:
            new = sorted(state.blocklist - base.blocklist)
            old = self.blocklist_path.read_text() if self.blocklist_path.exists() else ""
            if old and not old.endswith("\n"):
                old += "\n"
            writes[self.blocklist_path] = (old + "".join(f"{u}\n" for u in new)).encode()
        if "ledger" in view._dirty:
            appended = state.ledger[len(base.ledger):]
            by_file: dict[Path, list] = {}
            for row in appended:
                by_file.setdefault(self.reg / registry.prune_ledger_name(row["id"]), []).append(row)
            for path, rows in by_file.items():
                old = path.read_bytes() if path.exists() else b""
                writes[path] = old + "".join(
                    json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows).encode()
        if "rotation" in view._dirty:
            writes[self.rotation_path] = (
                json.dumps(state.rotation, indent=2, ensure_ascii=False) + "\n").encode()
        if "backends" in view._dirty:
            writes[self.backends_path] = (
                json.dumps(state.backends_raw, indent=2, ensure_ascii=False) + "\n").encode()
        self._render_journal(view, digest, writes)

        # Prepare: save every file we are about to touch, then publish the recovery marker.
        txn = self.txn_dir / view.run_id
        txn.mkdir(parents=True)
        files = []
        for path in sorted(writes):
            rel = str(path.relative_to(self.root))
            files.append({"rel": rel, "existed": path.exists()})
            if path.exists():
                saved = txn / "state" / rel
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, saved)
        meta = {"run_id": view.run_id, "state": "prepared", "files": files}
        ops.atomic_write_text(txn / "meta.json", json.dumps(meta, indent=2) + "\n")
        for path, data in sorted(writes.items()):
            if data is None:
                path.unlink(missing_ok=True)
            else:
                ops.atomic_write_bytes(path, data)
        meta["state"] = "committed"
        ops.atomic_write_text(txn / "meta.json", json.dumps(meta, indent=2) + "\n")
        shutil.rmtree(txn)

    def _events(self) -> list[dict]:
        state = _State()
        self._load(state, "events")
        return state.events

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
            expected = sorted(targets.get(name, []))
            got = sorted(e["id"] for e in registry.parse_yaml(text).get("sources") or [])
            if got != expected:
                raise StoreError(f"{name}: rendered shard does not match the transaction state")
            writes[self.reg / name] = text.encode()

    def _render_manifest(self, view: "WriteView", writes: dict) -> None:
        if "manifest" not in view._dirty:
            return
        base, state = view._base, view._state
        changed = {sid for sid in set(base.manifest) | set(state.manifest)
                   if base.manifest.get(sid) != state.manifest.get(sid)}
        stems = {registry.manifest_shard(sid) for sid in changed}
        groups: dict[str, list] = {stem: [] for stem in stems}
        for sid, row in state.manifest.items():
            stem = registry.manifest_shard(sid)
            if stem in groups:
                groups[stem].append(row)
        for stem, rows in groups.items():
            path = self.man / f"{stem}.jsonl"
            writes[path] = registry.manifest_shard_text(rows).encode() if rows else None

    def _render_journal(self, view: "WriteView", digest: str, writes: dict) -> None:
        existing = view._base.events if view._loaded("events") else self._events()
        seq = (existing[-1]["seq"] if existing else 0)
        at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        rows = []
        for n, op in enumerate(view._ops, 1):
            seq += 1
            rows.append({"seq": seq, "event_id": f"{view.run_id}:{n}", "run_id": view.run_id,
                         "at": at, **op})
        seq += 1
        rows.append({"seq": seq, "event_id": f"{view.run_id}:commit", "run_id": view.run_id,
                     "at": at, "table": None, "op": "commit", "id": None, "digest": digest})
        path = self.journal_dir / f"{at[:7]}.jsonl"
        old = path.read_bytes() if path.exists() else b""
        writes[path] = old + "".join(
            json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows).encode()

    # -- export --------------------------------------------------------------------------------

    def export(self, directory: Path, *, view: "ReadView") -> ExportReport:
        return export(directory, view=view)


def export(directory: Path, *, view: "ReadView") -> ExportReport:
    """Deterministic canonical export of every table plus pinned config. Backend-independent, so
    two stores holding the same state produce byte-identical directories."""
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)

    def jsonl(rows):
        return "".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in rows)

    config = view.config_get()
    content = {
        "entries.jsonl": jsonl(view._all(Table.ENTRIES)),
        "manifest.jsonl": jsonl(view._all(Table.MANIFEST)),
        "blocklist.txt": "".join(f"{r['url']}\n" for r in view._all(Table.BLOCKLIST)),
        "ledger.jsonl": jsonl(view._all(Table.LEDGER)),
        "events.jsonl": jsonl(view._all(Table.EVENTS)),
        "rotation.json": json.dumps(view.rotation_get(), ensure_ascii=False, sort_keys=True,
                                    indent=2) + "\n",
        "config/backends.json": json.dumps(config.backends, ensure_ascii=False, sort_keys=True,
                                           indent=2) + "\n",
        "config/eligibility.json": json.dumps(config.eligibility, ensure_ascii=False,
                                              sort_keys=True, indent=2) + "\n",
    }
    files = {}
    for name, text in content.items():
        data = text.encode()
        ops.atomic_write_bytes(directory / name, data)
        files[name] = {"sha256": hashlib.sha256(data).hexdigest(),
                       "rows": text.count("\n") if name.endswith((".jsonl", ".txt")) else None}
    ops.atomic_write_text(directory / "EXPORT.json", json.dumps(
        {"schema": 1, "files": files}, sort_keys=True, indent=2) + "\n")
    return ExportReport(str(directory), files)


# --- read view -----------------------------------------------------------------------------------

class ReadView:
    def __init__(self, store: FileStore, state: _State, version: Version):
        self._store = store
        self._state = state
        self._version = version
        self._loaded_tables: set[str] = set()
        self._known_cache: tuple[set, set, set] | None = None

    def _loaded(self, table: str) -> bool:
        return table in self._loaded_tables

    def _get(self, table: str) -> _State:
        if table not in self._loaded_tables:
            self._store._load(self._state, table)
            self._loaded_tables.add(table)
        return self._state

    def version(self) -> Version:
        return self._version

    # ordered full-table iteration; the single place that defines each table's key and order
    def _keyed(self, table: Table) -> list[tuple[tuple, dict]]:
        if table is Table.ENTRIES:
            rows = self._get("entries").entries
            return [((sid,), rows[sid]) for sid in sorted(rows)]
        if table is Table.MANIFEST:
            rows = self._get("manifest").manifest
            return [((sid,), rows[sid]) for sid in sorted(rows)]
        if table is Table.BLOCKLIST:
            return [((u,), {"url": u}) for u in sorted(self._get("blocklist").blocklist)]
        if table is Table.LEDGER:
            keyed = sorted((_canonical(r), r) for r in self._get("ledger").ledger)
            out, seen = [], {}
            for text, row in keyed:  # identical rows stay distinct via an occurrence counter
                n = seen[text] = seen.get(text, -1) + 1
                out.append(((text, n), row))
            return out
        if table is Table.EVENTS:
            return [((e["seq"],), e) for e in self._get("events").events]
        raise StoreError(f"unknown table {table}")

    def _all(self, table: Table) -> list[dict]:
        return [row for _, row in self._keyed(table)]

    def _validate(self, table: Table, where: Predicate | None, fields) -> None:
        allowed = TABLE_FIELDS[Table(table)]
        used = (predicate_fields(where) if where is not None else set()) | set(fields or ())
        if unknown := sorted(used - allowed):
            raise StoreError(f"unknown {Table(table).value} field(s): {', '.join(unknown)}")

    def scan(self, table: Table, *, where: Predicate | None = None,
             fields: tuple[str, ...] | None = None, cursor: Cursor | None = None,
             limit: int = DEFAULT_PAGE) -> Page:
        table = Table(table)
        self._validate(table, where, fields)
        if not 1 <= limit <= MAX_PAGE:
            raise StoreError(f"limit must be within 1..{MAX_PAGE}")
        query = _digest([table.value, repr(where), list(fields) if fields else None])
        if cursor is not None and (cursor.version != self._version.token or cursor.query != query):
            raise StoreError("cursor belongs to a different view or query")
        rows, last = [], None
        for key, row in self._keyed(table):
            if cursor is not None and key <= cursor.last_key:
                continue
            if not evaluate(where, row):
                continue
            if len(rows) == limit:
                return Page(rows, Cursor(self._version.token, query, last))
            rows.append({f: row[f] for f in fields if f in row} if fields else copy.deepcopy(row))
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
        """Which candidates are already known. Candidates are normalized exactly like
        registry.existing_keys (URL: strip + trailing '/'; title: registry.norm); empty values are
        never known. At most MAX_KNOWN candidates per kind."""
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
        """Group manifest rows. Missing and null group together as None. Sums add int/float
        values (bools excluded), ignore missing/null/non-numeric, and are 0 for an empty group.
        Output keys: the group_by fields, then "count" and "sum_<field>"; ordered by group key."""
        self._validate(Table.MANIFEST, where, tuple(group_by) + tuple(sums))
        groups: dict[str, dict] = {}
        for row in self._get("manifest").manifest.values():
            if not evaluate(where, row):
                continue
            key = tuple(row.get(f) for f in group_by)
            k = _canonical(key)
            g = groups.get(k)
            if g is None:
                g = groups[k] = {**dict(zip(group_by, key)),
                                 **({"count": 0} if count else {}),
                                 **{f"sum_{f}": 0 for f in sums}}
            if count:
                g["count"] += 1
            for f in sums:
                v = row.get(f)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    g[f"sum_{f}"] += v
        for k in sorted(groups):
            yield groups[k]

    def iter_duplicate_sha256(self, *, where: Predicate | None = None,
                              batch_size: int = DEFAULT_PAGE) -> Iterator[dict]:
        """Rows whose non-empty sha256 is shared with another matching row, ordered (sha256, id)."""
        self._validate(Table.MANIFEST, where, ("sha256",))
        if not 1 <= batch_size <= MAX_PAGE:
            raise StoreError(f"batch_size must be within 1..{MAX_PAGE}")
        by_sha: dict[str, list] = {}
        for sid, row in self._get("manifest").manifest.items():
            sha = row.get("sha256")
            if sha and evaluate(where, row):
                by_sha.setdefault(sha, []).append(sid)
        rows = self._get("manifest").manifest
        for sha in sorted(by_sha):
            if len(by_sha[sha]) > 1:
                for sid in sorted(by_sha[sha]):
                    yield copy.deepcopy(rows[sid])

    def rotation_get(self, name: str | None = None) -> dict:
        rotation = self._get("rotation").rotation
        return copy.deepcopy(rotation if name is None else rotation[name])

    def config_get(self) -> ConfigSnapshot:
        digests = {}
        for name in CONFIG_FILES:
            path = self._store.reg / name
            if path.exists():
                digests[name] = hashlib.sha256(path.read_bytes()).hexdigest()
        backends = copy.deepcopy(self._get("backends").backends_raw)
        eligibility_path = self._store.reg / "eligibility.json"
        eligibility = json.loads(eligibility_path.read_text()) if eligibility_path.exists() else {}
        return ConfigSnapshot(backends, eligibility, digests)

    def backend_state_get(self, name: str | None = None):
        raw = self._get("backends").backends_raw
        states = {k: BackendState(bool(v.get("enabled", True)), v.get("reason"))
                  for k, v in raw.items() if not k.startswith("_")}
        return states if name is None else states[name]

    def resolve_artifact(self, id: str, stage: Stage) -> ArtifactRef | None:  # noqa: A002
        """Where a document's stage payload lives, from its manifest row. Reads no payload and
        grants no eligibility."""
        row = self._get("manifest").manifest.get(id)
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
        return ArtifactRef(id, stage, f"file:{row[path]}", row.get(sha), row.get(size) if size else None)


# --- write view -----------------------------------------------------------------------------------

class WriteView(ReadView):
    """Buffered mutations over a copy of the loaded state; nothing touches disk until commit."""

    def __init__(self, store: FileStore, state: _State, version: Version, run_id: str):
        super().__init__(store, state, version)
        self.run_id = run_id
        self._base = _State()
        self._dirty: set[str] = set()
        self._ops: list[dict] = []

    def _get(self, table: str) -> _State:
        if table not in self._loaded_tables:
            self._store._load(self._base, table)
            # The working copy starts as a shallow copy of the base. Rows are only ever replaced,
            # never mutated in place, so base rows stay the pre-transaction truth without paying
            # for a deep copy of the whole manifest.
            attr = "backends_raw" if table == "backends" else table
            setattr(self._state, attr, copy.copy(getattr(self._base, attr)))
            if table == "entries":
                self._state.entry_file = dict(self._base.entry_file)
            self._loaded_tables.add(table)
        return self._state

    def _dirty_keys(self) -> bool:
        return bool(self._dirty & {"entries", "manifest", "blocklist"})

    def _entry_files(self) -> dict[str, str]:
        state = self._get("entries")
        return {sid: registry.shard_filename(sid) if (
                    sid not in self._base.entries or state.entries[sid] != self._base.entries[sid])
                else self._base.entry_file[sid] for sid in state.entries}

    def _touch(self, table: str) -> None:
        self._dirty.add(table)
        self._known_cache = None

    def _record(self, table: str, op: str, sid, before=None, after=None, reason=None) -> None:
        self._ops.append({"table": table, "op": op, "id": sid, "before": before,
                          "after": after, "reason": reason})

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
        return {k: e[k] for k in registry.FIELDS if k in e and e[k] not in (None, "")}

    def uniquify_ids(self, entries: Sequence[Mapping]) -> list[dict]:
        """Copies of `entries` with registry.uniquify_ids' suffixing against every known id and the
        batch itself. Computes names only; insert_entries remains the collision authority."""
        out = [dict(e) for e in entries]
        registry.uniquify_ids(out, set(self._known_sets()[2]))
        return out

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

    def upsert_manifest(self, rows: Iterable[Mapping]) -> int:
        rows = [copy.deepcopy(dict(r)) for r in rows]
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

    def replace_manifest(self, rows: Iterable[Mapping], *, reason: str) -> int:
        """write_manifest_rows semantics: `rows` becomes the whole manifest; omitted ids are
        deleted with tombstones carrying `reason`."""
        if not reason:
            raise StoreError("replace_manifest requires a reason")
        rows = [copy.deepcopy(dict(r)) for r in rows]
        self._unique_ids(rows, "replace_manifest")
        keep = {r["id"] for r in rows}
        state = self._get("manifest")
        gone = [sid for sid in state.manifest if sid not in keep]
        return self.delete_manifest(gone, reason=reason) + self.upsert_manifest(rows)

    def update_manifest_fields(self, updates: Mapping[str, Mapping[str, Any]], *,
                               unset: tuple[str, ...] = ()) -> int:
        state = self._get("manifest")
        allowed = TABLE_FIELDS[Table.MANIFEST]
        if missing := sorted(sid for sid in updates if sid not in state.manifest):
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

    def delete_manifest(self, ids: Iterable[str], *, reason: str) -> int:
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

    def blocklist_add(self, urls: Iterable[str]) -> int:
        state = self._get("blocklist")
        new = sorted({u for u in map(norm_url, urls) if u} - state.blocklist)
        for u in new:
            state.blocklist.add(u)
            self._record("blocklist", "insert", u, after={"url": u})
        if new:
            self._touch("blocklist")
        return len(new)

    def ledger_append(self, rows: Iterable[Mapping]) -> int:
        rows = [copy.deepcopy(dict(r)) for r in rows]
        allowed = TABLE_FIELDS[Table.LEDGER]
        for r in rows:
            if not isinstance(r.get("id"), str) or not r["id"]:
                raise StoreError("ledger rows need a non-empty id")
            if unknown := sorted(set(r) - allowed):
                raise StoreError(f"unknown ledger field(s): {', '.join(unknown)}")
        state = self._get("ledger")
        for r in rows:
            state.ledger.append(r)
            self._record("ledger", "insert", r["id"], after=r)
        if rows:
            self._touch("ledger")
        return len(rows)

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

    def backend_state_set(self, name: str, value: BackendState) -> None:
        state = self._get("backends")
        if name.startswith("_") or name not in state.backends_raw:
            raise StoreError(f"unknown backend {name}")
        before = state.backends_raw[name]
        after = dict(before, enabled=bool(value.enabled))
        if value.reason is None:
            after.pop("reason", None)
        else:
            after["reason"] = value.reason
        if after == before:
            return
        state.backends_raw[name] = after
        self._record("backends", "update", name,
                     before={k: before.get(k) for k in ("enabled", "reason")},
                     after={"enabled": after["enabled"], "reason": after.get("reason")})
        self._touch("backends")
