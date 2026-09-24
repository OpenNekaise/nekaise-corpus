#!/usr/bin/env python3
"""store_authority.py — which store is authoritative for a data root (ADR 0001 stage 4, step 1).

One host-wide record answers it for every entrypoint, whatever that entrypoint's environment:

    ~/.config/nekaise/store-authority.json   (home directory from the passwd database, not $HOME)
    {"format": 1, "roots": {"/abs/data/root": {"mode": "file" | "postgres", "epoch": N,
        "dataset_uuid": "...", "pg": {"dsn": "...", "schema": "..."},
        "changed_at": "...", "reason": "..."}}}

store.open() consults it before choosing a backend:

* No entry for the root (test roots, worktrees, fresh clones; the live checkout until an operator
  runs `init-file`): today's selection, unchanged — NEKAISE_STORE (default file). A PostgreSQL
  schema whose own authority row says "postgres" still refuses such an unbound opener.
* mode "file": FileStore is the only production store. NEKAISE_STORE / backend="postgres" is
  refused (AuthorityError): PgStore is never a production writer while files are authoritative.
* mode "postgres": NEKAISE_STORE=postgres, NEKAISE_PG_DSN and NEKAISE_PG_SCHEMA must be set and
  equal the record; the schema's dataset row must carry the record's dataset UUID, mode
  "postgres" and the same authority epoch (checked when opened and again inside every write
  transaction). Anything missing or different raises. FileStore refuses to open the root, and
  pg_shadow refuses to replay into the schema. A database failure is an error, never a reason
  to use the files.

Fence. Switching a root to "postgres" also writes `<root>/workspace/.store-authority-fence`
(the dataset UUID and epoch). FileStore refuses a fenced root unless the host record says "file"
with a newer epoch, so a lost or deleted host record fails closed rather than silently
re-enabling the files. Only verified rollback tooling (write_record(..., lift_fence=True)) may
record "file" over a fence; it removes the fence after the record is written.

The record is written atomically (fsync, rename) under an exclusive lock next to it. Records
never disappear through this module: the epoch only grows, so a stale opener is detectable.

Commands (read-only unless named):
    python scripts/store_authority.py show [--root R]
    python scripts/store_authority.py init-file [--root R] [--dsn D --schema S]
        record explicit FileStore authority for R (optionally naming its shadow schema, whose
        dataset UUID is bound into the record so pg_shadow cannot replay into another dataset)
Switching a root to PostgreSQL authority is the stage-4 cutover (step 6), not a command here.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import pwd
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Mapping

import ops
import store
from store import AuthorityError

FORMAT = 1
MODES = ("file", "postgres")
FENCE_NAME = ".store-authority-fence"
ENV_BACKEND = "NEKAISE_STORE"
ENV_DSN = "NEKAISE_PG_DSN"
ENV_SCHEMA = "NEKAISE_PG_SCHEMA"


def _host_record_path() -> Path:
    # Not $HOME: an entrypoint with a different environment must still find the record.
    return Path(pwd.getpwuid(os.getuid()).pw_dir) / ".config" / "nekaise" / "store-authority.json"


HOST_RECORD = _host_record_path()


@dataclass(frozen=True)
class Record:
    root: str
    mode: str
    epoch: int
    dataset_uuid: str | None = None
    dsn: str | None = None
    schema: str | None = None
    changed_at: str | None = None
    reason: str | None = None

    def as_json(self) -> dict:
        out = {"mode": self.mode, "epoch": self.epoch, "changed_at": self.changed_at,
               "reason": self.reason}
        if self.dataset_uuid:
            out["dataset_uuid"] = self.dataset_uuid
        if self.dsn or self.schema:
            out["pg"] = {"dsn": self.dsn, "schema": self.schema}
        return out


@dataclass(frozen=True)
class Selection:
    """What store.open() must construct: backend "file" or "postgres"; `record` is the binding
    entry (None: the root is unbound and selection followed the environment, as before)."""
    backend: str
    record: Record | None = None
    dsn: str | None = None
    schema: str | None = None


def root_key(root: Path | str) -> str:
    return str(Path(root).resolve())


def _record_path(path: Path | None) -> Path:
    return Path(path) if path is not None else HOST_RECORD


def _parse_entry(key: str, raw) -> Record:
    if not isinstance(raw, Mapping):
        raise AuthorityError(f"authority record for {key} is not an object")
    mode, epoch = raw.get("mode"), raw.get("epoch")
    if mode not in MODES:
        raise AuthorityError(f"authority record for {key}: unknown mode {mode!r}")
    if not isinstance(epoch, int) or isinstance(epoch, bool) or epoch < 1:
        raise AuthorityError(f"authority record for {key}: invalid epoch {epoch!r}")
    pg = raw.get("pg") or {}
    if not isinstance(pg, Mapping):
        raise AuthorityError(f"authority record for {key}: 'pg' is not an object")
    rec = Record(key, mode, epoch, raw.get("dataset_uuid"), pg.get("dsn"), pg.get("schema"),
                 raw.get("changed_at"), raw.get("reason"))
    if mode == "postgres" and not (rec.dataset_uuid and rec.dsn and rec.schema):
        raise AuthorityError(f"authority record for {key}: postgres authority needs "
                             "dataset_uuid, pg.dsn and pg.schema")
    if rec.schema is not None and not str(rec.schema).isidentifier():
        raise AuthorityError(f"authority record for {key}: invalid schema {rec.schema!r}")
    return rec


def load(path: Path | None = None) -> dict[str, Record]:
    """Every entry of the host record. A missing file means no entry; an unreadable, corrupt or
    unknown-format file raises (it may hold a postgres authority we cannot see)."""
    p = _record_path(path)
    try:
        data = p.read_bytes()
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise AuthorityError(f"cannot read the authority record {p}: {exc}") from exc
    try:
        doc = json.loads(data)
    except ValueError as exc:
        raise AuthorityError(f"authority record {p} is not valid JSON: {exc}") from exc
    if not isinstance(doc, Mapping) or doc.get("format") != FORMAT \
            or not isinstance(doc.get("roots"), Mapping):
        raise AuthorityError(f"authority record {p} has an unknown format")
    return {k: _parse_entry(k, v) for k, v in doc["roots"].items()}


def record_for(root: Path | str, path: Path | None = None) -> Record | None:
    return load(path).get(root_key(root))


def fence_path(root: Path | str) -> Path:
    return Path(root) / "workspace" / FENCE_NAME


def _read_fence(root: Path | str) -> dict | None:
    p = fence_path(root)
    try:
        data = p.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise AuthorityError(f"cannot read the authority fence {p}: {exc}") from exc
    try:
        doc = json.loads(data)
        return {"dataset_uuid": doc["dataset_uuid"], "epoch": int(doc["epoch"])}
    except (ValueError, KeyError, TypeError) as exc:
        raise AuthorityError(f"authority fence {p} is corrupt: {exc}") from exc


def check_file_access(root: Path | str, path: Path | None = None) -> None:
    """Raise unless FileStore may serve `root`: its record (if any) says "file" and no newer
    PostgreSQL fence exists."""
    rec = record_for(root, path)
    if rec is not None and rec.mode == "postgres":
        raise AuthorityError(f"{root_key(root)} is PostgreSQL-authoritative (epoch {rec.epoch}): "
                             "the file store may not serve it")
    fence = _read_fence(root)
    if fence is not None and (rec is None or rec.epoch <= fence["epoch"]):
        raise AuthorityError(
            f"{fence_path(root)} fences {root_key(root)} for PostgreSQL authority (epoch "
            f"{fence['epoch']}) but the host record "
            + ("is missing" if rec is None else f"says file at epoch {rec.epoch}")
            + ": refusing the file store")


def select(root: Path | str, backend: str | None = None, env: Mapping[str, str] | None = None,
           path: Path | None = None) -> Selection:
    """The backend store.open() must construct for `root`, or AuthorityError."""
    env = os.environ if env is None else env
    requested = backend or env.get(ENV_BACKEND) or None
    rec = record_for(root, path)
    if rec is None:
        name = requested or "file"
        if name == "file":
            check_file_access(root, path)  # a fence without a record fails closed
            return Selection("file")
        if name == "postgres":
            dsn = env.get(ENV_DSN)
            if not dsn:
                raise store.StoreError("storage backend 'postgres' needs NEKAISE_PG_DSN")
            return Selection("postgres", None, dsn, env.get(ENV_SCHEMA, "nekaise"))
        raise store.StoreError(f"storage backend {name!r} is not available (known: file, "
                               "postgres)")
    if rec.mode == "file":
        if requested not in (None, "file"):
            raise AuthorityError(f"{rec.root} is file-authoritative (epoch {rec.epoch}); "
                                 f"refusing storage backend {requested!r}")
        check_file_access(root, path)
        return Selection("file", rec)
    # postgres authority: the environment must say so, exactly
    if requested != "postgres":
        raise AuthorityError(f"{rec.root} is PostgreSQL-authoritative (epoch {rec.epoch}) but "
                             f"{ENV_BACKEND} is {requested!r}: set {ENV_BACKEND}=postgres, "
                             f"{ENV_DSN} and {ENV_SCHEMA} to match the authority record")
    for var, want in ((ENV_DSN, rec.dsn), (ENV_SCHEMA, rec.schema)):
        if env.get(var) != want:
            raise AuthorityError(f"{rec.root} is PostgreSQL-authoritative: {var} is "
                                 f"{env.get(var)!r}, the authority record says {want!r}")
    return Selection("postgres", rec, rec.dsn, rec.schema)


def require_file_authority(st, what: str) -> None:
    """For entrypoints whose logic is still the legacy file-round path (git snapshots, commit
    trailers): refuse any other authority explicitly instead of half-working."""
    if not isinstance(st, store.FileStore):
        raise AuthorityError(f"{what} runs the legacy file-store round path; the store for this "
                             f"root is {type(st).__name__} (PostgreSQL rounds arrive with stage 4)")


def require_file_mode(root: Path | str, what: str, path: Path | None = None) -> None:
    """The same check for tools that never open a store (e.g. the tracked-state backup)."""
    try:
        check_file_access(root, path)
    except AuthorityError as exc:
        raise AuthorityError(f"{what} needs file authority: {exc}") from exc


# --- writing (cutover tooling and tests) -----------------------------------------------------------

@contextmanager
def _locked(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_name(path.name + ".lock"), "a+") as f:
        fcntl.flock(f.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(f.fileno(), fcntl.LOCK_UN)


def write_record(root: Path | str, mode: str, *, reason: str, dataset_uuid: str | None = None,
                 dsn: str | None = None, schema: str | None = None, epoch: int | None = None,
                 path: Path | None = None, lift_fence: bool = False) -> Record:
    """Record `mode` for `root` (epoch = previous + 1 unless given, never lower). A postgres
    record also writes the root's fence before the record. A file record over an existing fence
    is refused unless `lift_fence` — reserved for verified rollback tooling (stage 4 step 6: the
    latest promoted generation exported and verified, the database moved back to file first),
    which removes the fence after the record. The PostgreSQL side (store_pg.set_authority) must
    carry the same epoch."""
    if mode not in MODES:
        raise AuthorityError(f"unknown mode {mode!r}")
    if not reason:
        raise AuthorityError("an authority change needs a reason")
    p = _record_path(path)
    key = root_key(root)
    with _locked(p):
        entries = load(p)
        prev = entries.get(key)
        fence = _read_fence(root)
        if mode == "file" and fence is not None and not lift_fence:
            raise AuthorityError(f"{fence_path(root)} fences {key} for PostgreSQL authority "
                                 f"(epoch {fence['epoch']}): only verified rollback tooling may "
                                 "return it to the files")
        # the epoch grows past both the record and the fence (a lost record cannot reset it)
        floor = max(prev.epoch if prev else 0, fence["epoch"] if fence else 0)
        new_epoch = epoch if epoch is not None else floor + 1
        if new_epoch <= floor:
            raise AuthorityError(f"authority epoch must grow: {key} is at {floor}, "
                                 f"refusing {new_epoch}")
        rec = Record(key, mode, new_epoch, dataset_uuid, dsn, schema,
                     time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), reason)
        _parse_entry(key, rec.as_json())  # validate before anything is written
        if mode == "postgres":
            ops.atomic_write_text(fence_path(root), json.dumps(
                {"dataset_uuid": dataset_uuid, "epoch": new_epoch}) + "\n")
        raw = json.loads(p.read_text()) if p.exists() else {"format": FORMAT, "roots": {}}
        raw["roots"][key] = rec.as_json()
        ops.atomic_write_text(p, json.dumps(raw, indent=2, sort_keys=True) + "\n")
        if mode == "file" and fence_path(root).exists():
            fence_path(root).unlink()
        return rec


# --- CLI -------------------------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("command", choices=("show", "init-file"))
    ap.add_argument("--root", default=str(store.ROOT))
    ap.add_argument("--dsn")
    ap.add_argument("--schema")
    ap.add_argument("--reason", default="explicit FileStore authority (ADR 0001 stage 4 step 1)")
    args = ap.parse_args(argv)
    root = Path(args.root)
    if args.command == "show":
        rec = record_for(root)
        fence = _read_fence(root)
        print(json.dumps({"record_file": str(HOST_RECORD), "root": root_key(root),
                          "record": rec.as_json() if rec else None, "fence": fence,
                          "selection": select(root, env={}).backend
                          if rec is None or rec.mode == "file" else "postgres"}, indent=2))
        return 0
    rec = record_for(root)
    if rec is not None:
        print(f"{root_key(root)} already has an authority record (mode {rec.mode}, epoch "
              f"{rec.epoch}); nothing changed", file=sys.stderr)
        return 1 if rec.mode != "file" else 0
    if (fence := _read_fence(root)) is not None:
        # the host record was lost after a cutover: never re-enable the files from here
        print(f"{fence_path(root)} fences {root_key(root)} for PostgreSQL authority (epoch "
              f"{fence['epoch']}): refusing init-file; only verified rollback tooling may lift "
              "it", file=sys.stderr)
        return 1
    dataset_uuid = None
    if args.dsn or args.schema:
        if not (args.dsn and args.schema):
            ap.error("--dsn and --schema go together")
        import store_pg
        # an existing schema only (create=False: never create or migrate from here)
        auth = store_pg.PgStore(root, dsn=args.dsn, schema=args.schema, create=False).authority()
        if auth["mode"] != "file":
            print(f"schema {args.schema} is PostgreSQL-authoritative (epoch {auth['epoch']}, root "
                  f"{auth['root']}): refusing init-file", file=sys.stderr)
            return 1
        dataset_uuid = auth["dataset_uuid"]
    rec = write_record(root, "file", reason=args.reason, dataset_uuid=dataset_uuid,
                       dsn=args.dsn, schema=args.schema)
    print(json.dumps(rec.as_json(), indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
