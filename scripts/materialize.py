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

Views (collect-all directive 2026-09-25). A materialization is of one VIEW and one STAGE:

* corpus stage: the default view corpus/ (the open use class — what a training run reads, as
  before) or a classified view collection/<class>/corpus/ (registry.is_corpus_view_member);
* raw / text stages: collection/<class>/{raw,text}/…, rebuildable views over the canonical
  originals (every row of the class holding that payload), hard links only — they never
  duplicate payload storage, and on a file-authoritative store they resolve the canonical
  raw/ and text/ files (no generation: always a full refresh).

The stamp names view, stage, the classification-policy version (registry.CLASS_POLICY_VERSION),
the dataset, the generation and the pinned configuration digest; acquire() checks all of them.
Reclassification changes a row, so an incremental refresh revisits it; a policy change (the
configuration digest) or a new classification-policy version forces a full refresh, so
membership follows the classification even when no artifact hash changed.
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
STAGES = ("corpus", "raw", "text")
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
def acquire(corpus_dir: Path, *, generation: int | None, dataset: str | None = None,
            view: str = registry.DEFAULT_VIEW, stage: str = "corpus",
            timeout: float = 0) -> Iterator[dict]:
    """Hold corpus_dir as a complete materialization of `generation` (of `dataset`, when given)
    of `view` at `stage`, under the current classification policy, for the block: a shared
    lock, so no refresh can change it meanwhile. Refuses a missing, invalid, refreshing,
    other-generation, other-view or other-classification materialization."""
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
        if (stamp.get("view"), stamp.get("stage"), stamp.get("class_policy")) != (
                view, stage, registry.CLASS_POLICY_VERSION):
            raise MaterializeError(
                f"{corpus_dir} materializes view {stamp.get('view')!r} stage "
                f"{stamp.get('stage')!r} under classification policy "
                f"{stamp.get('class_policy')}, not {view!r} {stage!r} under "
                f"{registry.CLASS_POLICY_VERSION}: refresh it")
        yield stamp


@contextmanager
def acquire_current(st, root: Path, *, corpus_dir: Path | None = None,
                    view: str = registry.DEFAULT_VIEW, stage: str = "corpus",
                    timeout: float = 0) -> Iterator[dict]:
    """acquire() at the store's current committed generation."""
    with st.read() as view_:
        prov = _committed_provenance(view_)
    with acquire(corpus_dir or view_dir(root, view, stage), generation=prov["generation"],
                 dataset=prov["dataset"], view=view, stage=stage, timeout=timeout) as stamp:
        yield stamp


def check_target(view: str, stage: str) -> None:
    if stage not in STAGES:
        raise MaterializeError(f"unknown stage {stage!r} (one of {', '.join(STAGES)})")
    if stage == "corpus":
        if view not in registry.VIEWS:
            raise MaterializeError(f"unknown view {view!r} (one of {', '.join(registry.VIEWS)})")
    elif view not in registry.USE_CLASSES:
        raise MaterializeError(f"a {stage} view is of a use class, not {view!r} "
                               f"(one of {', '.join(registry.USE_CLASSES)})")


def view_dir(root: Path, view: str = registry.DEFAULT_VIEW, stage: str = "corpus") -> Path:
    """Where the materialization of (view, stage) lives."""
    check_target(view, stage)
    if stage == "corpus":
        return Path(root) / registry.view_root(view)
    return Path(root) / registry.COLLECTION_DIR / view / stage


def _committed_provenance(view) -> dict:
    if getattr(view, "stage", None) is not None:
        raise MaterializeError("a materialization is of a committed generation, not a staged run")
    prov = artifact_store.run_policy(view)
    if not prov or prov.get("generation") is None:
        raise MaterializeError("nothing is promoted yet: there is no generation to materialize")
    return prov


def _file_provenance(view) -> dict:
    """A file-authoritative store has no generations: its raw/text class views are always
    refreshed in full against the current view (generation None)."""
    import hashlib
    digests = view.config_get().digests
    return {"generation": None, "dataset": "file",
            "config_digest": hashlib.sha256(json.dumps(digests, sort_keys=True).encode())
            .hexdigest(), "cleaning_ruleset": None}


# --- refreshing -----------------------------------------------------------------------------------

class _Refresh:
    def __init__(self, root: Path, corpus_dir: Path, view, restrictions,
                 target: str = registry.DEFAULT_VIEW, stage: str = "corpus"):
        self.root, self.dir, self.view = Path(root), Path(corpus_dir), view
        self.restrictions, self.target, self.stage = restrictions, target, stage
        self.local = artifact_store.LocalArtifacts(self.root)
        self.stats = {"installed": 0, "kept": 0, "removed": 0, "preserved": 0, "members": 0}
        self.missing: list[str] = []

    def wanted(self, row: dict | None) -> tuple | None:
        """The claim `row` contributes to this view, or None."""
        if row is None:
            return None
        if self.stage == "corpus":
            # the view predicate: class and policy decide membership, whatever the metadata
            if not registry.is_corpus_view_member(row, self.target, self.restrictions):
                return None
        elif registry.use_class(row, self.restrictions) != self.target:
            return None
        elif self.stage == "text" and row.get("status") != "ok":
            return None   # a raw original counts even when its extraction failed
        return artifact_store.claim(row, self.stage)

    def _dst(self, sid: str, row: dict | None = None) -> Path:
        if not _ID.fullmatch(sid) or sid in (".", ".."):
            raise MaterializeError(f"id {sid!r} is not a safe file name")
        if self.stage == "corpus":
            return self.dir / f"{sid}.md"
        rel = registry.collection_view_path(row or {}, self.stage, self.target)
        prefix = f"{registry.COLLECTION_DIR}/{self.target}/{self.stage}/"
        if rel is None or not rel.startswith(prefix):
            raise MaterializeError(f"{sid}: its {self.stage} claim is not a safe relative path")
        return self.root / rel

    def _source(self, sid: str, c: tuple) -> Path | None:
        """The immutable version of the claim, adopting the legacy file the claim names when the
        version does not exist yet (the claim's identity must match its bytes)."""
        path, sha = c
        if isinstance(sha, str) and self.local.has(self.stage, sha):
            return self.local.path(self.stage, sha)
        legacy = None
        if isinstance(path, str) and path and not os.path.isabs(path) \
                and ".." not in Path(path).parts:
            legacy = self.root / path
        if legacy is None or not legacy.is_file():
            return None
        if not isinstance(sha, str) or self.stage != "corpus":
            # a legacy claim without an identity, or a canonical original (raw/text views are
            # aliases of the canonical files, never re-versioned here): the path is the locator
            return legacy
        art = self.local.adopt("corpus", legacy)
        self.stats["preserved"] += 1
        return self.local.path("corpus", sha) if art.sha256 == sha else None

    def _preserve(self, dst: Path) -> None:
        """Keep the bytes of a file about to be replaced or removed as an immutable version.
        A raw/text view entry is an alias of a canonical file that stays; only a sole copy
        (link count 1) is preserved."""
        if self.stage != "corpus" and dst.stat().st_nlink > 1:
            return
        self.local.adopt(self.stage, dst)
        self.stats["preserved"] += 1

    def _link_into_place(self, src: Path, dst: Path) -> None:
        dst.parent.mkdir(parents=True, exist_ok=True)
        tmp = dst.parent / f"{_TMP}{secrets.token_hex(8)}"
        try:
            os.link(src, tmp)
        except OSError as exc:
            if exc.errno != 18:  # EXDEV
                raise
            if self.stage != "corpus":
                raise MaterializeError(f"{self.stage} views are hard links to the canonical "
                                       "payload and must live on its filesystem") from None
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
        c = self.wanted(row)
        if c is None:
            if self.stage == "corpus":
                self._remove(self._dst(sid))
            # a raw/text entry lives under its row's path; the sweep removes non-members
            elif row is not None and (rel := registry.collection_view_path(
                    row, self.stage, self.target)):
                self._remove(self.root / rel)
            return
        dst = self._dst(sid, row)
        exists = dst.exists()
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

    def _remove(self, dst: Path) -> None:
        if dst.exists():
            self._preserve(dst)
            dst.unlink()
            self.stats["removed"] += 1

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
        # files not in G's membership (pruned, reclassified, renamed ids): checked in chunks
        chunk: list[tuple[str, Path]] = []
        for path in self._files():
            chunk.append((_id_of(path, self.stage), path))
            if len(chunk) >= CHUNK_IDS:
                self._sweep(chunk)
                chunk = []
        if chunk:
            self._sweep(chunk)
        _crash("swept")

    def _files(self) -> Iterator[Path]:
        if self.stage == "corpus":
            for entry in os.scandir(self.dir):
                if not entry.name.startswith(".") and entry.name.endswith(".md") \
                        and entry.is_file(follow_symlinks=False):
                    yield Path(entry.path)
            return
        for dirpath, dirnames, filenames in os.walk(self.dir):
            dirnames[:] = [d for d in dirnames if not d.startswith(".")]
            for name in filenames:
                if not name.startswith("."):
                    yield Path(dirpath) / name

    def _sweep(self, files: list[tuple[str, Path]]) -> None:
        rows = self.view.get_manifest([sid for sid, _ in files if _ID.fullmatch(sid)])
        for sid, path in files:
            row = rows.get(sid)
            if self.wanted(row) is not None and self._dst(sid, row) == path:
                continue
            self._preserve(path)
            path.unlink()
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


def _id_of(path: Path, stage: str) -> str:
    """The row id a view file belongs to: <id>.md, raw/<source>/<id>.<ext>, text/<id>.md."""
    name = path.name
    return name[:-3] if name.endswith(".md") else name.rsplit(".", 1)[0]


def refresh(st, root: Path, *, corpus_dir: Path | None = None, generation: int | None = None,
            full: bool = False, timeout: float = 0, page: int = store.MAX_PAGE,
            view: str = registry.DEFAULT_VIEW, stage: str = "corpus") -> dict:
    """Make the (view, stage) directory (default: <root>/corpus, the default view) a complete
    materialization of committed generation `generation` (default: the current one). Returns
    the refresh statistics; raises MaterializeError, leaving the stamp "refreshing", when a
    member's payload is missing."""
    import store_staging
    check_target(view, stage)
    corpus_dir = Path(corpus_dir) if corpus_dir is not None else view_dir(root, view, stage)
    with _locked(corpus_dir, exclusive=True, timeout=timeout):
        opened = st.read() if generation is None else st.read_generation(generation)
        with opened as view_:
            if stage != "corpus" and artifact_store.run_policy(view_) is None:
                prov = _file_provenance(view_)       # file authority: no generations
            else:
                prov = _committed_provenance(view_)
            target = prov["generation"]
            restrictions, _policy = store.pinned_policy(view_)
            stamp = read_stamp(corpus_dir) or {}
            base, mode = None, "full"
            if not full and target is not None and stamp.get("dataset") == prov["dataset"] \
                    and stamp.get("state") in ("complete", "refreshing") \
                    and (stamp.get("view"), stamp.get("stage"), stamp.get("class_policy")) == (
                        view, stage, registry.CLASS_POLICY_VERSION):
                base = stamp.get("generation")
                reach = stamp.get("target", base) if stamp.get("state") == "refreshing" else base
                if isinstance(base, int) and isinstance(reach, int) and base <= reach <= target \
                        and target - base <= MAX_DIFF_GENERATIONS \
                        and store_staging.generation_config(view_, base) == prov["config_digest"]:
                    mode = "diff"
            if mode == "full":
                base = stamp.get("generation") if isinstance(stamp.get("generation"), int) \
                    else None
            # stale temporaries of a crashed refresh go first
            for dirpath, _dirs, files in os.walk(corpus_dir):
                for name in files:
                    if name.startswith(_TMP):
                        os.unlink(os.path.join(dirpath, name))
            ident = {"dataset": prov["dataset"], "view": view, "stage": stage,
                     "class_policy": registry.CLASS_POLICY_VERSION}
            _write_stamp(corpus_dir, {"state": "refreshing", **ident,
                                      "generation": base if mode == "diff" else None,
                                      "target": target, "mode": mode})
            _crash("stamped")
            job = _Refresh(root, corpus_dir, view_, restrictions, view, stage)
            if mode == "diff":
                job.diff(base, target)
            else:
                job.full(page)
            if job.missing:
                raise MaterializeError(f"{len(job.missing)} member(s) of generation {target} "
                                       f"have no local {stage} payload, e.g. {job.missing[:5]}; "
                                       "the materialization stays 'refreshing'")
            _crash("before-complete")
            _write_stamp(corpus_dir, {"state": "complete", **ident,
                                      "generation": target, "config": prov["config_digest"],
                                      "cleaning_ruleset": prov["cleaning_ruleset"],
                                      "refreshed_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                                    time.gmtime())})
    return {"mode": mode, "generation": target, "view": view, "stage": stage, **job.stats}


# The views a promoted generation must keep current (staged_runs.after_promotion): every cleaned
# view, classified ones first — a reclassified file is installed in its new view before the old
# view drops it (both link the same immutable version anyway). Raw/text class views are
# rebuildable aliases, refreshed on demand (main below).
REQUIRED_VIEWS = tuple((v, "corpus") for v in (*registry.CLASSIFIED_VIEWS, registry.DEFAULT_VIEW))


def refresh_required(st, root: Path, **kw) -> dict:
    """Refresh every REQUIRED_VIEWS materialization; {view: statistics}."""
    return {view: refresh(st, root, view=view, stage=stage, **kw)
            for view, stage in REQUIRED_VIEWS}


def current(st, root: Path) -> bool:
    """Every required view is a complete materialization of the current generation under the
    current classification policy (or nothing is promoted yet)."""
    with st.read() as view_:
        generation = getattr(view_, "generation", None)
        dataset = (artifact_store.run_policy(view_) or {}).get("dataset")
    if generation is None:
        return True
    for view, stage in REQUIRED_VIEWS:
        stamp = read_stamp(view_dir(root, view, stage)) or {}
        if (stamp.get("state"), stamp.get("generation"), stamp.get("dataset"), stamp.get("view"),
                stamp.get("stage"), stamp.get("class_policy")) != (
                "complete", generation, dataset, view, stage, registry.CLASS_POLICY_VERSION):
            return False
    return True


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="refresh a materialized view")
    ap.add_argument("--view", default=registry.DEFAULT_VIEW,
                    help="default or a use class (raw/text views: a use class)")
    ap.add_argument("--stage", default="corpus", choices=STAGES)
    ap.add_argument("--full", action="store_true")
    ap.add_argument("--timeout", type=float, default=0)
    args = ap.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    out = refresh(store.open(root=root), root, view=args.view, stage=args.stage,
                  full=args.full, timeout=args.timeout)
    print(json.dumps(out, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
