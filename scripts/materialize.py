#!/usr/bin/env python3
"""materialize.py — corpus/ as a generation-stamped materialization (ADR 0001 stage 4, step 3).

Under PostgreSQL staging the cleaner never writes corpus/: it writes immutable versions
(artifact_store) and stages their identities. What a training run reads is a *materialization*
of one committed generation G: corpus/<id>.md for every training-eligible row of G (G's pinned
eligibility policy) that claims a cleaned payload, each a hard link to the immutable version of
the claimed identity (a copy only across filesystems), plus a stamp, corpus/.materialization.json,
naming the dataset and G. Membership comes from G's rows only, never from a directory listing.

Refreshing is supervised: `refresh()` takes the exclusive lock (corpus/.materialization.lock),
first stamps the directory "refreshing" (durably), then installs and removes files — each by a
link to a private temporary name and an atomic rename, never by writing into a file — and only
at the end fsyncs the directory and stamps it "complete" at G. A crash anywhere leaves a
"refreshing" stamp, which no consumer accepts; the next refresh converges from it (idempotent).
A file that is replaced or removed is first preserved as an immutable version (adopted by hard
link), so a legacy cleaned file (the only copy of an earlier generation's bytes) is never lost.

Incremental refresh: when the stamp is at (or refreshing from) generation B <= G of the same
dataset under the same pinned configuration, only the ids whose manifest rows a generation in
(B, G] revised are revisited (store_staging.changed_manifest_ids); otherwise every row of G is
visited and every file not in G's membership is removed (full refresh).

Consumers call `acquire()` (or `acquire_current()`), which holds the shared lock for as long as
they read and refuses a directory that is not completely materialized at the generation they
want: they never train on a half-refreshed or stale corpus.
"""
from __future__ import annotations

import fcntl
import json
import os
import re
import secrets
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import artifact_store
import ops
import registry
import store

STAMP = ".materialization.json"
LOCK = ".materialization.lock"
FORMAT = 1
_TMP = ".mat-"
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,254}")
# beyond this many generations since the stamp, refresh visits everything instead
MAX_DIFF_GENERATIONS = 10_000
CHUNK_IDS = 5_000


class MaterializeError(store.StoreError):
    """The materialization cannot be refreshed or acquired."""


def _crash(point: str) -> None:
    """Crash-injection hook (tests replace it)."""


# --- stamp and lock ---------------------------------------------------------------------------------

def read_stamp(corpus_dir: Path) -> dict | None:
    """The stamp, None when there is none; an unreadable stamp reads as {"state": "invalid"}."""
    path = Path(corpus_dir) / STAMP
    try:
        doc = json.loads(path.read_text())
    except FileNotFoundError:
        return None
    except (OSError, ValueError):
        return {"state": "invalid"}
    if not isinstance(doc, dict) or doc.get("format") != FORMAT:
        return {"state": "invalid"}
    return doc


def _write_stamp(corpus_dir: Path, doc: dict) -> None:
    # atomic replace, fsyncing the file and the directory — which also makes every rename and
    # unlink done in the directory before it durable
    ops.atomic_write_text(Path(corpus_dir) / STAMP,
                          json.dumps({"format": FORMAT, **doc}, sort_keys=True) + "\n")


@contextmanager
def _locked(corpus_dir: Path, exclusive: bool, timeout: float) -> Iterator[None]:
    corpus_dir.mkdir(parents=True, exist_ok=True)
    f = open(corpus_dir / LOCK, "a+")
    try:
        deadline = time.monotonic() + max(0.0, timeout)
        mode = fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH
        while True:
            try:
                fcntl.flock(f.fileno(), mode | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise MaterializeError(
                        f"{corpus_dir} is {'in use by a consumer' if exclusive else 'being refreshed'}"
                        f" (materialization lock held)") from None
                time.sleep(0.05)
        yield
    finally:
        f.close()


# --- acquiring (consumers) ---------------------------------------------------------------------------

@contextmanager
def acquire(corpus_dir: Path, *, generation: int, dataset: str | None = None,
            timeout: float = 0) -> Iterator[dict]:
    """Hold corpus_dir as a complete materialization of `generation` (of `dataset`, when given)
    for the block: a shared lock, so no refresh can change it meanwhile. Refuses a missing,
    invalid, refreshing or other-generation materialization."""
    corpus_dir = Path(corpus_dir)
    with _locked(corpus_dir, exclusive=False, timeout=timeout):
        stamp = read_stamp(corpus_dir)
        if stamp is None or stamp.get("state") != "complete":
            raise MaterializeError(f"{corpus_dir} is not a complete materialization "
                                   f"({'no stamp' if stamp is None else stamp.get('state')}): "
                                   "refresh it first")
        if stamp.get("generation") != generation or (
                dataset is not None and stamp.get("dataset") != dataset):
            raise MaterializeError(f"{corpus_dir} materializes generation "
                                   f"{stamp.get('generation')} of {stamp.get('dataset')}, not "
                                   f"{generation} of {dataset or 'this dataset'}: refresh it")
        yield stamp


@contextmanager
def acquire_current(st, root: Path, *, corpus_dir: Path | None = None,
                    timeout: float = 0) -> Iterator[dict]:
    """acquire() at the store's current committed generation."""
    with st.read() as view:
        prov = _committed_provenance(view)
    with acquire(corpus_dir or Path(root) / "corpus", generation=prov["generation"],
                 dataset=prov["dataset"], timeout=timeout) as stamp:
        yield stamp


def _committed_provenance(view) -> dict:
    if getattr(view, "stage", None) is not None:
        raise MaterializeError("a materialization is of a committed generation, not a staged run")
    prov = artifact_store.run_policy(view)
    if not prov or prov.get("generation") is None:
        raise MaterializeError("nothing is promoted yet: there is no generation to materialize")
    return prov


# --- refreshing -----------------------------------------------------------------------------------

class _Refresh:
    def __init__(self, root: Path, corpus_dir: Path, view, restrictions):
        self.root, self.dir, self.view = Path(root), Path(corpus_dir), view
        self.restrictions = restrictions
        self.local = artifact_store.LocalArtifacts(self.root)
        self.stats = {"installed": 0, "kept": 0, "removed": 0, "preserved": 0, "members": 0}
        self.missing: list[str] = []

    def wanted(self, row: dict | None) -> tuple | None:
        """The corpus claim `row` contributes to the materialization, or None."""
        if row is None or row.get("status") != "ok":
            return None
        # the full training predicate: a pointer-only license or an eligibility.json
        # restriction keeps a row out whatever its metadata claims
        if not registry.is_training_eligible(row, self.restrictions):
            return None
        return artifact_store.claim(row, "corpus")

    def _dst(self, sid: str) -> Path:
        if not _ID.fullmatch(sid) or sid in (".", ".."):
            raise MaterializeError(f"id {sid!r} is not a safe file name")
        return self.dir / f"{sid}.md"

    def _source(self, sid: str, c: tuple) -> Path | None:
        """The immutable version of the claim, adopting the legacy file the claim names when the
        version does not exist yet (the claim's identity must match its bytes)."""
        path, sha = c
        if isinstance(sha, str) and self.local.has("corpus", sha):
            return self.local.path("corpus", sha)
        legacy = None
        if isinstance(path, str) and path and not os.path.isabs(path) \
                and ".." not in Path(path).parts:
            legacy = self.root / path
        if legacy is None or not legacy.is_file():
            return None
        if not isinstance(sha, str):
            # a legacy claim without an identity: its path is the only locator there is
            return legacy
        art = self.local.adopt("corpus", legacy)
        self.stats["preserved"] += 1
        return self.local.path("corpus", sha) if art.sha256 == sha else None

    def _preserve(self, dst: Path) -> None:
        """Keep the bytes of a file about to be replaced or removed as an immutable version."""
        self.local.adopt("corpus", dst)
        self.stats["preserved"] += 1

    def _link_into_place(self, src: Path, dst: Path) -> None:
        tmp = self.dir / f"{_TMP}{secrets.token_hex(8)}"
        try:
            os.link(src, tmp)
        except OSError as exc:
            if exc.errno != 18:  # EXDEV
                raise
            with open(src, "rb") as a, open(tmp, "wb") as b:
                for chunk in iter(lambda: a.read(artifact_store.CHUNK), b""):
                    b.write(chunk)
                b.flush()
                os.fsync(b.fileno())
        try:
            os.replace(tmp, dst)
        finally:
            if tmp.exists():
                tmp.unlink()

    def apply(self, sid: str, row: dict | None) -> None:
        dst = self._dst(sid)
        c = self.wanted(row)
        exists = dst.exists()
        if c is None:
            if exists:
                self._preserve(dst)
                dst.unlink()
                self.stats["removed"] += 1
            return
        self.stats["members"] += 1
        src = self._source(sid, c)
        if src is None:
            if len(self.missing) < 1000:
                self.missing.append(sid)
            return
        if exists and os.path.samefile(src, dst):
            self.stats["kept"] += 1
            return
        if exists:
            self._preserve(dst)
        self._link_into_place(src, dst)
        self.stats["installed"] += 1
        _crash("installed")

    def full(self, page: int) -> None:
        cursor = None
        while True:
            got = self.view.scan(store.Table.MANIFEST, cursor=cursor, limit=page)
            for row in got.rows:
                if self.wanted(row) is not None:   # everything else: the sweep below
                    self.apply(row["id"], row)
            if got.next_cursor is None:
                break
            cursor = got.next_cursor
        # files not in G's membership (pruned, restricted, renamed ids): checked in chunks
        chunk: list[str] = []
        for entry in os.scandir(self.dir):
            name = entry.name
            if name.startswith("."):
                continue
            if not name.endswith(".md") or not entry.is_file(follow_symlinks=False):
                continue
            chunk.append(name[:-3])
            if len(chunk) >= CHUNK_IDS:
                self._sweep(chunk)
                chunk = []
        if chunk:
            self._sweep(chunk)
        _crash("swept")

    def _sweep(self, names: list[str]) -> None:
        rows = self.view.get_manifest([n for n in names if _ID.fullmatch(n)])
        for sid in names:
            if self.wanted(rows.get(sid)) is None:
                dst = self.dir / f"{sid}.md"
                self._preserve(dst)
                dst.unlink()
                self.stats["removed"] += 1

    def diff(self, base: int | None, generation: int) -> None:
        import store_staging
        after = ""
        while True:
            ids = store_staging.changed_manifest_ids(self.view, base, generation, after=after,
                                                     limit=CHUNK_IDS)
            if not ids:
                return
            rows = self.view.get_manifest(ids)
            for sid in ids:
                self.apply(sid, rows.get(sid))
            after = ids[-1]


def refresh(st, root: Path, *, corpus_dir: Path | None = None, generation: int | None = None,
            full: bool = False, timeout: float = 0, page: int = store.MAX_PAGE) -> dict:
    """Make corpus_dir (default <root>/corpus) a complete materialization of committed
    generation `generation` (default: the current one). Returns the refresh statistics; raises
    MaterializeError, leaving the stamp "refreshing", when a member's payload is missing."""
    import store_staging
    corpus_dir = Path(corpus_dir) if corpus_dir is not None else Path(root) / "corpus"
    with _locked(corpus_dir, exclusive=True, timeout=timeout):
        opened = st.read() if generation is None else st.read_generation(generation)
        with opened as view:
            prov = _committed_provenance(view)
            target = prov["generation"]
            restrictions, _policy = store.pinned_policy(view)
            stamp = read_stamp(corpus_dir) or {}
            base, mode = None, "full"
            if not full and stamp.get("dataset") == prov["dataset"] and \
                    stamp.get("state") in ("complete", "refreshing"):
                base = stamp.get("generation")
                reach = stamp.get("target", base) if stamp.get("state") == "refreshing" else base
                if isinstance(base, int) and isinstance(reach, int) and base <= reach <= target \
                        and target - base <= MAX_DIFF_GENERATIONS \
                        and store_staging.generation_config(view, base) == prov["config_digest"]:
                    mode = "diff"
            if mode == "full":
                base = stamp.get("generation") if isinstance(stamp.get("generation"), int) \
                    else None
            # stale temporaries of a crashed refresh go first
            for entry in os.scandir(corpus_dir):
                if entry.name.startswith(_TMP):
                    os.unlink(entry.path)
            _write_stamp(corpus_dir, {"state": "refreshing", "dataset": prov["dataset"],
                                      "generation": base if mode == "diff" else None,
                                      "target": target, "mode": mode})
            _crash("stamped")
            job = _Refresh(root, corpus_dir, view, restrictions)
            if mode == "diff":
                job.diff(base, target)
            else:
                job.full(page)
            if job.missing:
                raise MaterializeError(f"{len(job.missing)} member(s) of generation {target} "
                                       f"have no local cleaned payload, e.g. {job.missing[:5]}; "
                                       "the materialization stays 'refreshing'")
            _crash("before-complete")
            _write_stamp(corpus_dir, {"state": "complete", "dataset": prov["dataset"],
                                      "generation": target, "config": prov["config_digest"],
                                      "cleaning_ruleset": prov["cleaning_ruleset"],
                                      "refreshed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                                    time.gmtime())})
    return {"mode": mode, "generation": target, **job.stats}
