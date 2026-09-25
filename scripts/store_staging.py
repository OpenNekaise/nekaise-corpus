#!/usr/bin/env python3
"""store_staging.py — run-scoped staging and constant-size promotion over PostgreSQL
(ADR 0001 stage 4, step 2; schema v5, store_pg.V5_DDL).

A round (a *run*) never writes the projection tables (entries, manifest, blocklist, ledger,
rotation, control_docs, backend_state). Each of its metadata batches is ONE short transaction that
stores the batch's immutable request (canonical JSON text + digest), applies it as immutable
revisions of the run (puts and tombstones keyed by the stable identity (table, key)) at the run's
next staging sequence, and seals the batch with the digests the database computes. Promotion then
writes a constant number of rows — the generation, the run's status, the dataset's current
generation, one outbox reference and the version counter — however many revisions the run staged.

Reading. Every view is one REPEATABLE READ snapshot over the projection plus an overlay of
revisions (Visibility):

* ordinary views pin the committed generation G: the projection (which materializes generation
  P <= G, projection_state.generation; NULL is the pre-generation base) plus the revisions of the
  runs promoted in (P, G], later generations superseding earlier ones;
* a run's pipeline children (authorized by its access token, NEKAISE_STORE_STAGE) and its
  coordinator (its writer) read G plus the run's own revisions up to a staging sequence k, the run
  superseding G: discovery workers share the sequence the round started at, later steps see every
  batch that completed before they opened their view, gates read the frozen sequence;
* the writer applying a batch reads the same plus that batch's own revisions (read-your-writes).

A revision is visible exactly when its run is (and, for the staging run, its batch sequence is at
most k); the visible revision of a key with the highest (rank, batch sequence) is its value
(rank = the run's promoted generation, G + 1 for the staging run), a tombstone hides it, and a
projection row with any visible revision is superseded. Every read method of PgReadView runs
unchanged over those "sources" (store_pg.PgReadView._src), so lookups, keyset pagination,
membership, aggregates and duplicate detection agree with a store where the same mutations were
applied directly (tests/test_store_pg_staging.py).

Folding. The projection consumer (outbox consumer "projection") folds promoted generations into
the projection tables in bounded, idempotent batches (fold()), one generation at a time, never
past an active retention pin. While generation P+1 is being folded, every view still overlays it,
so a partially folded key is superseded by the same value: folding changes where rows live, not
what any generation contains.

Payloads (stage 4 step 3, schema v6). A run's artifact policy is fixed when it opens. In a
"versioned" run (the default) a manifest put that claims a payload (raw/text/corpus path +
sha256) the superseded row did not claim identically must name an immutable version already
written under artifacts/ (artifact_store): the batch's own transaction checks the file,
registers its identity and local locator and records the run's reference (run_artifacts), and
the database refuses to seal a batch with any other new claim. Such a run freezes only with the
"artifacts" gate required (artifact_store.verify_run). "unchecked" runs keep the step-2 rules.

Writing requires the run's owner: the writer whose epoch opened it, or — after an explicit resume
(adopt_run, schema v7) — the writer that adopted it; the adoption is logged and the database
allows it only for an unchanged parent generation, commit, configuration and extractor with every
referenced artifact verified. Recovery reads the durable run status (run_status, unfinished_runs)
and aborts; aborted runs are queued for bounded purging (purge_due, purge_run). The database
enforces the rest (V5_DDL): batches apply exactly after the
sequence they were computed at, a committed applied batch is sealed, revisions of a sealed batch
never change, a run freezes only with no requested batch and with its chain digest, gate receipts
bind to the frozen sequence and digest, and a generation needs every required gate passed.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterable, Iterator, Mapping, Sequence

import psycopg
from psycopg import sql

import store
import store_pg
from store import (BackendState, ConfigSnapshot, StaleView, StoreError, Version, VersionConflict,
                   WriteView, WriterError, WriterToken, canonical_row)

# A pipeline child reads its run's overlay when this names the run, the staging sequence to pin
# ("live": the run's staged sequence when the view opens) and an access token of the run:
# "<run id>:<seq|live>:<token>". Only a staged broker exports it (store_broker.Broker.env).
STAGE_ENV = "NEKAISE_STORE_STAGE"
# Visibility is spelled as literal CASE expressions for up to this many promoted-but-unfolded runs
# (order-preserving index scans); beyond it, as correlated lookups in `runs`.
LITERAL_RUNS = 64
FOLD_BATCH = 20_000
# stage_batch refreshes the staging tables' planner statistics every this many batches of a run
ANALYZE_EVERY = 1000
# Overlay rows fetched per scan window (None: max(page size, 256)); tests shrink it.
SCAN_CHUNK: int | None = None
LEDGER_N_DIGITS = 10
_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")
_TOKEN = re.compile(r"[0-9a-f]{64}")


# --- value types ---------------------------------------------------------------------------------

@dataclass(frozen=True)
class StagedRun:
    run_id: str
    token: str                    # authorizes the run's pipeline children to read its overlay
    parent_generation: int | None
    artifact_policy: str = "versioned"
    attempt: int = 1              # 1 + the run's adoptions: a resumed run's steps name their
                                  # batches "a<attempt>-<batch>" (store_broker.StepSession)
    status: str = "open"          # open | frozen (a resumed frozen run only records gates)


@dataclass(frozen=True)
class StagePin:
    run_id: str
    seq: int | None               # None: the run's staged sequence when the view opens
    token: str


@dataclass(frozen=True)
class Frozen:
    run_id: str
    seq: int
    digest: str


@dataclass(frozen=True)
class StageResult:
    results: list
    version: Version              # the run's staging version after this batch
    seq: int | None               # the sequence the batch produced (None: persisted only)
    status: str                   # requested | applied
    retried: bool                 # True: an existing receipt answered (exact retry)


@dataclass(frozen=True)
class Receipt:
    run_id: str
    step: str
    batch: str
    request_digest: str
    request_text: str
    status: str
    basis_seq: int | None
    seq: int | None
    sealed: bool
    chain_digest: str | None
    counts_text: str | None

    @property
    def requests(self) -> list:
        return json.loads(self.request_text)

    @property
    def results(self) -> list:
        return json.loads(self.counts_text)["results"] if self.counts_text else []


@dataclass(frozen=True)
class FoldProgress:
    generation: int | None        # the generation being (or just) folded; None: nothing to fold
    rows: int                     # revisions applied by this call
    done: bool                    # the generation is now the projection's
    blocked: bool = False         # a retention pin holds the fold back


def stage_version(run_id: str, seq: int) -> Version:
    """A staging version: distinct in form from committed versions ("pg:<n>")."""
    return Version(f"pg:stage:{run_id}:{seq}")


def pin_from_env(env: Mapping[str, str] | None = None) -> StagePin | None:
    """The stage pin this process was given (STAGE_ENV), or None. Malformed values raise: a
    child that should read its run's overlay must never silently read committed state."""
    raw = (os.environ if env is None else env).get(STAGE_ENV)
    if not raw:
        return None
    parts = raw.split(":")
    if len(parts) != 3 or not _NAME.fullmatch(parts[0]) or not _TOKEN.fullmatch(parts[2]) or not (
            parts[1] == "live" or re.fullmatch(r"[0-9]{1,9}", parts[1])):
        raise StoreError(f"{STAGE_ENV} is malformed (want <run>:<seq|live>:<token>)")
    return StagePin(parts[0], None if parts[1] == "live" else int(parts[1]), parts[2])


def pin_env(run_id: str, token: str, seq: int | None = None) -> dict[str, str]:
    return {STAGE_ENV: f"{run_id}:{'live' if seq is None else seq}:{token}"}


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


# --- visibility and overlay sources ------------------------------------------------------------------

class Visibility:
    """Which revisions a view sees and how they supersede each other (see the module docstring).

    `lo` is the projection generation P (None: the pre-generation base), `hi` the pinned
    committed generation G (None: no generation yet), `runs` the (run id, generation) pairs
    promoted in (lo, hi] as read in the view's snapshot, `own` the staging run and sequence."""

    def __init__(self, lo: int | None, hi: int | None, runs: Sequence[tuple[str, int]],
                 own: tuple[str, int] | None = None):
        self.lo, self.hi, self.runs, self.own = lo, hi, list(runs), own

    @classmethod
    def read(cls, conn, lo, hi, own=None) -> "Visibility":
        runs = [] if hi is None else conn.execute(
            "SELECT run_id, promoted_generation FROM runs WHERE promoted_generation > %s AND "
            "promoted_generation <= %s ORDER BY promoted_generation",
            [-1 if lo is None else lo, hi]).fetchall()
        return cls(lo, hi, [tuple(r) for r in runs], own)

    @property
    def empty(self) -> bool:
        return not self.runs and self.own is None

    @property
    def own_rank(self) -> int:
        return (-1 if self.hi is None else self.hi) + 1

    def _literal(self) -> bool:
        return len(self.runs) <= LITERAL_RUNS

    def vis(self, a: str) -> sql.Composable:
        """Whether revision alias `a` is visible."""
        a = sql.Identifier(a)
        if self._literal():
            whens = [sql.SQL("WHEN {} THEN true").format(sql.Literal(r)) for r, _ in self.runs]
            if self.own is not None:
                whens.append(sql.SQL("WHEN {} THEN {}.batch_seq <= {}").format(
                    sql.Literal(self.own[0]), a, sql.Literal(self.own[1])))
            return sql.SQL("(CASE {}.run_id {} ELSE false END)").format(a, sql.SQL(" ").join(whens))
        parts = [sql.SQL("EXISTS (SELECT 1 FROM runs vr WHERE vr.run_id = {a}.run_id AND "
                         "vr.promoted_generation > {lo} AND vr.promoted_generation <= {hi})")
                 .format(a=a, lo=sql.Literal(-1 if self.lo is None else self.lo),
                         hi=sql.Literal(self.hi))]
        if self.own is not None:
            parts.append(sql.SQL("({a}.run_id = {r} AND {a}.batch_seq <= {k})").format(
                a=a, r=sql.Literal(self.own[0]), k=sql.Literal(self.own[1])))
        return sql.SQL("({})").format(sql.SQL(" OR ").join(parts))

    def rank(self, a: str) -> sql.Composable:
        """The supersession rank of a VISIBLE revision alias `a` (its run's generation)."""
        a = sql.Identifier(a)
        own = sql.Literal(self.own_rank)
        if self._literal():
            whens = [sql.SQL("WHEN {} THEN {}").format(sql.Literal(r), sql.Literal(g))
                     for r, g in self.runs]
            if self.own is not None:
                whens.append(sql.SQL("WHEN {} THEN {}").format(sql.Literal(self.own[0]), own))
            return sql.SQL("(CASE {}.run_id {} END)").format(a, sql.SQL(" ").join(whens))
        return sql.SQL("COALESCE((SELECT vr.promoted_generation FROM runs vr WHERE vr.run_id = "
                       "{a}.run_id AND vr.promoted_generation > {lo} AND vr.promoted_generation "
                       "<= {hi}), {own})").format(
            a=a, lo=sql.Literal(-1 if self.lo is None else self.lo), hi=sql.Literal(self.hi),
            own=own)

    # Keyset scans (scan()) fence their correlated subqueries with OFFSET 0: they stay per-row
    # index probes, so each branch streams in index order. Unfenced (sources for lookups,
    # membership and aggregates), the planner is free to hash-anti-join the whole table, which is
    # right there but O(table) for every page of a scan.
    @staticmethod
    def _fence(ordered: bool) -> sql.Composable:
        return sql.SQL(" OFFSET 0") if ordered else sql.SQL("")

    def overridden(self, tbl: str, key: sql.Composable, ordered: bool = False) -> sql.Composable:
        """A visible revision exists for projection key `key` of revision table `tbl`."""
        return sql.SQL("EXISTS (SELECT 1 FROM revisions o WHERE o.tbl = {t} AND o.key = {k} "
                       "AND {vis}{f})").format(t=sql.Literal(tbl), k=key, vis=self.vis("o"),
                                                f=self._fence(ordered))

    def effective(self, a: str = "r", ordered: bool = False) -> sql.Composable:
        """No visible revision of the same key supersedes revision alias `a`."""
        return sql.SQL("NOT EXISTS (SELECT 1 FROM revisions n WHERE n.tbl = {a}.tbl AND n.key = "
                       "{a}.key AND {vis} AND ({rn}, n.batch_seq) > ({ra}, {a}.batch_seq){f})"
                       ).format(a=sql.Identifier(a), vis=self.vis("n"), rn=self.rank("n"),
                                ra=self.rank(a), f=self._fence(ordered))

    def _live(self, tbl: str, ordered: bool = False) -> sql.Composable:
        return sql.SQL("r.tbl = {t} AND r.op = 'put' AND {vis} AND {eff}").format(
            t=sql.Literal(tbl), vis=self.vis("r"), eff=self.effective("r", ordered))

    # table/order -> (projection table, revision table, projection key exprs, revision key exprs,
    # revision ORDER BY, projection text, revision text, projection jsonb row, revision jsonb row,
    # projection key as a revision key). Ledger revision keys are "<digest>:<n zero-padded>", so
    # their text order is the projection's (key, n) order.
    _SCAN = {
        ("entries", "key"): ("entries", "entries", ("p.id",), ("r.key",), ("r.key",),
                             "p.row_text", "r.row_text", "p.row", "p.id"),
        ("manifest", "key"): ("manifest", "manifest", ("p.id",), ("r.key",), ("r.key",),
                              "p.row_text", "r.row_text", "p.row", "p.id"),
        ("manifest", "legacy"): ("manifest", "manifest", ("p.shard", "p.topic_key", "p.id"),
                                 ("r.shard", "r.topic_key", "r.key"),
                                 ("r.shard", "r.topic_key", "r.key"), "p.row_text", "r.row_text",
                                 "p.row", "p.id"),
        ("blocklist", "key"): ("blocklist", "blocklist", ("p.key",), ("r.key",), ("r.key",),
                               "p.url", "(r.row_text::jsonb ->> 'url')",
                               "jsonb_build_object('url', p.url)", "p.key"),
        ("ledger", "key"): ("ledger", "ledger", ("p.key", "p.n"),
                            ("split_part(r.key, ':', 1)", "split_part(r.key, ':', 2)::int"),
                            ("r.key",), "p.row_text", "r.row_text", "p.row",
                            f"p.key || ':' || lpad(p.n::text, {LEDGER_N_DIGITS}, '0')"),
    }

    def scan(self, q, table: str, order: str, where, after: tuple | None,
             limit: int) -> list[tuple]:
        """Up to `limit` visible rows after keyset position `after`, as (keys..., text) tuples
        in the table's scan order (PgReadView.scan's contract; the blocklist's text is its URL).
        `q(query, params)` runs a query in the view's snapshot.

        Bounded windows, so a page never costs more than its own key range plus one overlay
        chunk, however dense the overlay (a full re-clean revises every row): fetch the next
        chunk of effective overlay puts (each flagged with the predicate); its last key bounds
        the window; fetch the projection rows in the window that no visible revision overrides
        and that match the predicate, at most as many as the page still needs; merge up to the
        point both are complete, and continue from there. Both are single-table ORDER BY ...
        LIMIT index scans whose visibility probes stay per row (_fence)."""
        (ptbl, rtbl, pkeys, rkeys, rorder, ptext, rtext, prow, pkey_as_rev) = self._SCAN[
            (table, order)]
        p = sql.SQL
        pcond, pparams = store_pg.compile_predicate(where, p(prow))
        rcond, rparams = store_pg.compile_predicate(where, p("(r.row_text::jsonb)"))
        ledger = table == "ledger"
        chunk = SCAN_CHUNK or max(limit, 256)

        def compare(keys, op: str, values) -> sql.Composable:
            """(keys...) <op> (%s, ...) — a row comparison, index-usable"""
            return p("({}) {} ({})").format(p(", ").join(map(p, keys)), p(op),
                                            p(", ").join(sql.Placeholder() for _ in values))

        def fetch_overlay(lo):
            cond, params = p("true"), []
            if lo is not None:
                if ledger:   # the revision key's text order is (key, n) order
                    cond, params = p("r.key > %s"), [f"{lo[0]}:{lo[1]:0{LEDGER_N_DIGITS}d}"]
                else:
                    cond, params = compare(rkeys, ">", lo), list(lo)
            rows = q(p("SELECT {rk}, {rt}, COALESCE({rc}, false) FROM revisions r WHERE r.tbl = "
                       "{t} AND r.op = 'put' AND {vis} AND {cond} AND {eff} ORDER BY {ro} "
                       "LIMIT %s").format(
                rk=p(", ").join(map(p, rkeys)), rt=p(rtext), rc=rcond, t=sql.Literal(rtbl),
                vis=self.vis("r"), cond=cond, eff=self.effective("r", True),
                ro=p(", ").join(map(p, rorder))), rparams + params + [chunk]).fetchall()
            return rows

        def fetch_projection(lo, hi, n):
            conds, params = [pcond], list(pparams)
            for bound, op in ((lo, ">"), (hi, "<=")):
                if bound is not None:
                    conds.append(compare(pkeys, op, bound))
                    params += list(bound)
            return q(p("SELECT {pk}, {pt} FROM {tbl} p WHERE {conds} AND NOT {ov} ORDER BY {pk} "
                       "LIMIT %s").format(
                pk=p(", ").join(map(p, pkeys)), pt=p(ptext), tbl=sql.Identifier(ptbl),
                conds=p(" AND ").join(p("({})").format(c) for c in conds),
                ov=self.overridden(rtbl, p(pkey_as_rev), True)), params + [n]).fetchall()

        n_keys = len(pkeys)
        out: list[tuple] = []
        lo = None if after is None else tuple(after)
        while len(out) < limit:
            overlay = fetch_overlay(lo)
            hi = tuple(overlay[-1][:n_keys]) if len(overlay) == chunk else None
            need = limit - len(out)
            base = fetch_projection(lo, hi, need)
            # the merged rows are exact up to `cut`: every overlay row up to hi is loaded, and
            # every projection row up to hi — or up to the last one fetched when it hit `need`
            cut = hi if len(base) < need else tuple(base[-1][:n_keys])
            merged = [tuple(r[:n_keys + 1]) for r in overlay
                      if r[-1] and (cut is None or tuple(r[:n_keys]) <= cut)]
            merged += [tuple(r) for r in base]
            merged.sort(key=lambda r: r[:n_keys])
            out += merged
            if cut is None:
                break
            lo = cut
        return out[:limit]

    def source(self, table: str) -> sql.Composable:
        """The visible rows of projection table `table`, as a FROM item named like the table and
        with its columns, so PgReadView's lookup, membership and aggregate queries run over it
        unchanged (keyset scans use scan())."""
        p = sql.SQL
        if table in ("entries", "manifest"):
            extra = ["sha256", "shard", "topic_key"] if table == "manifest" else []
            cols = ["row_text", "url_norm", "url_key", "title_norm", "title_key", *extra]
            pcols = p(", ").join(p("p.{}").format(sql.Identifier(c)) for c in cols)
            rcols = p(", ").join(p("r.{}").format(sql.Identifier(c)) for c in cols)
            return p("(SELECT p.id, p.row, {pc} FROM {t} p WHERE NOT {ov} UNION ALL "
                     "SELECT r.key, r.row_text::jsonb, {rc} FROM revisions r WHERE {live}) AS {t}"
                     ).format(pc=pcols, rc=rcols, t=sql.Identifier(table),
                              ov=self.overridden(table, p("p.id")),
                              live=self._live(table))
        if table == "blocklist":
            return p("(SELECT p.key, p.url FROM blocklist p WHERE NOT {ov} UNION ALL "
                     "SELECT r.key, r.row_text::jsonb ->> 'url' FROM revisions r WHERE {live}) "
                     "AS blocklist").format(ov=self.overridden("blocklist", p("p.key")),
                                            live=self._live("blocklist"))
        if table == "ledger":
            key = p("p.key || ':' || lpad(p.n::text, {}, '0')").format(
                sql.Literal(LEDGER_N_DIGITS))
            return p("(SELECT p.key, p.n, p.row_text, p.row FROM ledger p WHERE NOT {ov} UNION ALL "
                     "SELECT split_part(r.key, ':', 1), split_part(r.key, ':', 2)::int, "
                     "r.row_text, r.row_text::jsonb FROM revisions r WHERE {live}) AS ledger"
                     ).format(ov=self.overridden("ledger", key),
                              live=self._live("ledger"))
        if table == "rotation":
            return p("(SELECT p.name, p.value_text FROM rotation p WHERE NOT {ov} UNION ALL "
                     "SELECT r.key, r.row_text FROM revisions r WHERE {live}) AS rotation").format(
                ov=self.overridden("rotation", p("p.name")), live=self._live("rotation"))
        if table == "control_docs":
            return p("(SELECT p.name, p.doc_text FROM control_docs p WHERE NOT {ov} UNION ALL "
                     "SELECT r.key, r.row_text FROM revisions r WHERE {live}) AS control_docs"
                     ).format(ov=self.overridden("control", p("p.name")),
                              live=self._live("control"))
        if table == "backend_state":
            return p("(SELECT p.name, p.enabled, p.reason FROM backend_state p WHERE NOT {ov} "
                     "UNION ALL SELECT r.key, (r.row_text::jsonb ->> 'enabled')::boolean, "
                     "r.row_text::jsonb ->> 'reason' FROM revisions r WHERE {live}) AS "
                     "backend_state").format(ov=self.overridden("backend_state", p("p.name")),
                                             live=self._live("backend_state"))
        if table == "events":
            # the legacy journal: staged runs journal through batch receipts and revisions
            return sql.Identifier("events")
        raise StoreError(f"no overlay source for {table!r}")


def config_from_set(conn, digest: str) -> ConfigSnapshot:
    """A sealed configuration set (exact bytes) as the ConfigSnapshot views serve."""
    documents, digests = {}, {}
    for name, data in conn.execute(
            "SELECT m.name, b.bytes FROM config_set_members m JOIN config_blobs b USING (sha256) "
            "WHERE m.digest = %s ORDER BY m.name", [digest]):
        data = bytes(data)
        documents[name] = json.loads(data.decode())
        digests[name] = hashlib.sha256(data).hexdigest()
    return ConfigSnapshot(documents, digests)


def committed(conn) -> tuple[Visibility | None, ConfigSnapshot | None, int | None]:
    """For an ordinary view in `conn`'s snapshot: the overlay over the projection (None when the
    projection alone is generation G), generation G's pinned configuration (None: before the first
    generation, the pinned `config` table applies) and G."""
    head, = conn.execute("SELECT current_generation FROM dataset").fetchone()
    if head is None:
        return None, None, None
    lo, = conn.execute("SELECT generation FROM projection_state").fetchone()
    vis = Visibility.read(conn, lo, head)
    digest, = conn.execute("SELECT config_digest FROM generations WHERE generation = %s",
                           [head]).fetchone()
    return (None if vis.empty else vis), config_from_set(conn, digest), head


# --- the staged write view ------------------------------------------------------------------------

def _staged(fn):
    """One mutation inside a savepoint: a caught error leaves nothing of it (FileStore validates
    before mutating to the same end); its journal ops are dropped with it."""
    def wrapper(self, *args, **kwargs):
        self._check_open()
        saved = {t: dict(ops) for t, ops in self._counts.items()}
        self._mutations += 1
        try:
            with self._conn.transaction():
                return fn(self, *args, **kwargs)
        except BaseException:
            self._counts = saved   # a failed mutation leaves no count (its revisions are undone)
            raise
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    wrapper.__wrapped__ = fn
    return wrapper


_REVISION_COLS = ("run_id", "batch_seq", "tbl", "key", "op", "row_text", "row_sha256",
                  "before_sha256", "reason", "url_norm", "url_key", "title_norm", "title_key",
                  "sha256", "shard", "topic_key")
_UPSERT_REVISION = sql.SQL(
    "INSERT INTO revisions ({cols}) VALUES ({vals}) ON CONFLICT (run_id, tbl, key, batch_seq) "
    "DO UPDATE SET {sets}").format(
    cols=sql.SQL(", ").join(map(sql.Identifier, _REVISION_COLS)),
    vals=sql.SQL(", ").join(sql.Placeholder() for _ in _REVISION_COLS),
    # before_sha256 stays the row visible at the batch's basis (identity; the guard enforces it)
    sets=sql.SQL(", ").join(sql.SQL("{c} = EXCLUDED.{c}").format(c=sql.Identifier(c))
                            for c in _REVISION_COLS if c not in (
                                "run_id", "batch_seq", "tbl", "key", "before_sha256")))


_UPSERT_REVISIONS_FROM_BUF = sql.SQL(
    "INSERT INTO revisions ({cols}) SELECT {cols} FROM nk_stage_buf ORDER BY tbl, key "
    "ON CONFLICT (run_id, tbl, key, batch_seq) DO UPDATE SET {sets}").format(
    cols=sql.SQL(", ").join(map(sql.Identifier, _REVISION_COLS)),
    sets=sql.SQL(", ").join(sql.SQL("{c} = EXCLUDED.{c}").format(c=sql.Identifier(c))
                            for c in _REVISION_COLS if c not in (
                                "run_id", "batch_seq", "tbl", "key", "before_sha256")))
# a mutation staging at least this many revisions COPYs them (fewer: one pipelined statement each)
COPY_THRESHOLD = 1000


class StagedWriteView(store_pg.PgReadView):
    """Applies one batch of a run: mutations become revisions at the batch's sequence, read
    through the overlay including the batch's own revisions. Semantics, validation and return
    values are PgWriteView's; the projection is never written."""

    def __init__(self, st, conn, config: ConfigSnapshot, visibility: Visibility, run_id: str,
                 seq: int, generation: int | None, artifact_policy: str = "unchecked"):
        super().__init__(st, conn, stage_version(run_id, seq), config, visibility=visibility,
                         generation=generation, stage=(run_id, seq))
        self.run_id, self.seq = run_id, seq
        self.artifact_policy = artifact_policy
        # operation counts per table and op (fixed size, whatever a batch touches); no per-row
        # journal: the revisions are the record
        self._counts: dict[str, dict[str, int]] = {}
        self._mutations = 0

    def _cursor_scope(self) -> str:
        return f"{self._id}:{self._mutations}"

    def _record(self, table: str, op: str, n: int = 1) -> None:
        if n:
            per = self._counts.setdefault(table, {})
            per[op] = per.get(op, 0) + n

    def counts(self) -> dict:
        return {t: dict(ops) for t, ops in self._counts.items()}

    def _rows(self, table: str, ids: list[str]) -> dict[str, tuple[dict, str]]:
        """The visible rows `ids` (including this batch's): id -> (row, its stored canonical
        text). Canonical text is the row's identity (store.same_row), so it is compared and
        hashed as stored, never re-serialized."""
        q = sql.SQL("SELECT id, row_text FROM {} WHERE id = ANY(%s)").format(self._src(table))
        return {i: (json.loads(t), t) for i, t in self._q(q, [ids]).fetchall()}

    def _stage(self, tbl: str, items: list[tuple]) -> None:
        """items: (key, op, row or None, row's canonical text or None, reason or None, canonical
        text of the row visible before this mutation or None). A key the batch already staged
        keeps its original before-image; a key whose net effect over the batch is nothing loses
        its revision."""
        if not items:
            return
        if tbl == "manifest" and self.artifact_policy == "versioned":
            self._claim_artifacts(items)
        params = []
        for key, op, row, text, reason, before in items:
            if text is not None:
                store_pg._check_text(text, f"{tbl} row")
            derived = (None,) * 7
            if row is not None and tbl in ("entries", "manifest"):
                derived = store_pg.revision_keys(tbl, row)
            params.append((self.run_id, self.seq, tbl, store_pg._check_text(key, f"{tbl} key"),
                           op, text, None if text is None else _sha(text),
                           None if before is None else _sha(before), reason, *derived))
        with self._conn.cursor() as cur:
            if len(params) < COPY_THRESHOLD:
                cur.executemany(_UPSERT_REVISION, params)
            else:  # bulk: one set-based statement (same rows, same triggers)
                cur.execute(sql.SQL("CREATE TEMP TABLE IF NOT EXISTS nk_stage_buf ON COMMIT DROP "
                                    "AS SELECT {} FROM revisions WITH NO DATA").format(
                    sql.SQL(", ").join(map(sql.Identifier, _REVISION_COLS))))
                cur.execute("TRUNCATE nk_stage_buf")
                with cur.copy(sql.SQL("COPY nk_stage_buf ({}) FROM STDIN").format(
                        sql.SQL(", ").join(map(sql.Identifier, _REVISION_COLS)))) as cp:
                    for rec in params:
                        cp.write_row(rec)
                cur.execute(_UPSERT_REVISIONS_FROM_BUF)
        self._q("DELETE FROM revisions WHERE run_id = %s AND batch_seq = %s AND tbl = %s AND "
                "key = ANY(%s) AND ((op = 'put' AND row_sha256 IS NOT DISTINCT FROM "
                "before_sha256) OR (op = 'tombstone' AND before_sha256 IS NULL))",
                [self.run_id, self.seq, tbl, [p[3] for p in params]])

    def _claim_artifacts(self, items: list[tuple]) -> None:
        """Versioned runs (schema v6): every payload claim a manifest put makes that the row it
        supersedes did not make identically names an immutable version that must already be
        written (artifact_store.LocalArtifacts.put_*): check it, register its identity and local
        locator if new (after a directory fsync barrier) and record the run's reference, all in
        this batch's transaction. The database re-checks the rule when the batch seals."""
        import artifact_store
        need: dict[tuple[str, str], int | None] = {}
        for key, op, row, _text, _reason, before in items:
            if op != "put" or row is None:
                continue
            prior = None if before is None else json.loads(before)
            for stage, path, sha in artifact_store.changed_claims(row, prior):
                if not isinstance(path, str) or not path:
                    raise StoreError(f"manifest {key}: its {stage} claim needs a non-empty path")
                try:
                    artifact_store.check_identity(stage, sha)
                except artifact_store.ArtifactError as exc:
                    raise StoreError(f"manifest {key}: {exc}") from None
                expect = None
                if stage == "raw" and row.get("bytes") is not None:
                    expect = row["bytes"]
                    if isinstance(expect, bool) or not isinstance(expect, int):
                        raise StoreError(f"manifest {key}: bytes must be an integer")
                if (stage, sha) in need and None not in (need[(stage, sha)], expect) \
                        and need[(stage, sha)] != expect:
                    raise StoreError(f"raw artifact {sha} claimed with two sizes in one batch")
                if need.get((stage, sha)) is None:
                    need[(stage, sha)] = expect
        if need:
            self._register_artifacts(need)

    def _register_artifacts(self, need: dict[tuple[str, str], int | None]) -> None:
        import artifact_store
        keys = sorted(need)
        # keyed lookups per identity (LATERAL), never a scan of the growing artifacts table
        have = {(s, h): (size, local) for s, h, size, local in self._q(
            "SELECT a.stage, a.sha256, a.size, EXISTS (SELECT 1 FROM artifact_locators l WHERE "
            "l.stage = a.stage AND l.sha256 = a.sha256 AND l.kind = 'local') FROM "
            "unnest(%s::text[], %s::text[]) AS u(stage, sha256) CROSS JOIN LATERAL (SELECT * "
            "FROM artifacts x WHERE x.stage = u.stage AND x.sha256 = u.sha256 OFFSET 0) a",
            [[k[0] for k in keys], [k[1] for k in keys]])}
        local = artifact_store.LocalArtifacts(self._store.root)
        new_ids, new_locs = [], []
        for stage, sha in keys:
            on_disk = local.size(stage, sha)
            if on_disk is None:
                raise StoreError(f"{stage} artifact {sha} was not written before a batch "
                                 f"referenced it (expected {local.path(stage, sha)})")
            registered = have.get((stage, sha))
            size = on_disk if registered is None else registered[0]
            if on_disk != size:
                raise StoreError(f"{stage} artifact {sha} has {on_disk} bytes on disk but was "
                                 f"registered with {size}")
            if need[(stage, sha)] is not None and need[(stage, sha)] != size:
                raise StoreError(f"raw artifact {sha} has {size} bytes, not the row's "
                                 f"{need[(stage, sha)]}")
            if registered is None:
                new_ids.append((stage, sha, size))
            if registered is None or not registered[1]:
                new_locs.append((stage, sha))
        if new_locs:   # the writer fsynced them; make sure of their directory entries too
            artifact_store.barrier((local.path(s, h) for s, h in new_locs), local.root)
        if new_ids:
            self._q("INSERT INTO artifacts (stage, sha256, size, first_run) SELECT s, h, z, %s "
                    "FROM unnest(%s::text[], %s::text[], %s::bigint[]) AS u(s, h, z) "
                    "ON CONFLICT DO NOTHING",
                    [self.run_id, [i[0] for i in new_ids], [i[1] for i in new_ids],
                     [i[2] for i in new_ids]])
        if new_locs:
            self._q("INSERT INTO artifact_locators (stage, sha256, locator, kind) SELECT s, h, "
                    "nk_local_locator(s, h), 'local' FROM unnest(%s::text[], %s::text[]) AS "
                    "u(s, h) ON CONFLICT DO NOTHING",
                    [[i[0] for i in new_locs], [i[1] for i in new_locs]])
        self._q("INSERT INTO run_artifacts (run_id, stage, sha256, batch_seq) SELECT %s, s, h, %s "
                "FROM unnest(%s::text[], %s::text[]) AS u(s, h) ON CONFLICT DO NOTHING",
                [self.run_id, self.seq, [k[0] for k in keys], [k[1] for k in keys]])

    def _upsert(self, tbl: str, rows: list[dict], op: str) -> int:
        """Stage every row that differs from the visible one; returns how many did."""
        before = self._rows(tbl, [r["id"] for r in rows])
        items = []
        for r in rows:
            text = canonical_row(r)
            old = before.get(r["id"])
            if old is None or old[1] != text:
                items.append((r["id"], "put", r, text, None, None if old is None else old[1]))
                self._record(tbl, op)
        self._stage(tbl, items)
        return len(items)

    def _tombstone(self, tbl: str, ids: list[str], reason: str,
                   before: Mapping[str, tuple[dict, str]]) -> int:
        present = [i for i in ids if i in before]
        self._stage(tbl, [(i, "tombstone", None, None, reason, before[i][1]) for i in present])
        self._record(tbl, "delete", len(present))
        return len(present)

    def uniquify_ids(self, entries: Sequence[Mapping]) -> list[dict]:
        return store.uniquify(entries, lambda i: bool(self.known(ids=[i]).ids))

    @_staged
    def insert_entries(self, entries: Iterable[Mapping]) -> int:
        rows = [WriteView._entry_row(e) for e in entries]
        WriteView._unique_ids(rows, "insert_entries")
        if clash := sorted(self._rows("entries", [r["id"] for r in rows])):
            raise StoreError(f"insert_entries: id(s) already exist: {', '.join(clash[:5])}")
        return self._upsert("entries", rows, "insert")

    @_staged
    def upsert_entries(self, entries: Iterable[Mapping]) -> int:
        rows = [WriteView._entry_row(e) for e in entries]
        WriteView._unique_ids(rows, "upsert_entries")
        return self._upsert("entries", rows, "upsert")

    @_staged
    def delete_entries(self, ids: Iterable[str], *, reason: str) -> int:
        if not reason:
            raise StoreError("delete_entries requires a reason")
        ids = list(dict.fromkeys(ids))
        return self._tombstone("entries", ids, reason, self._rows("entries", ids))

    @_staged
    def upsert_manifest(self, rows: Iterable[Mapping]) -> int:
        rows = [dict(r) for r in rows]
        WriteView._unique_ids(rows, "upsert_manifest")
        return self._upsert("manifest", [json.loads(canonical_row(r)) for r in rows], "upsert")

    @_staged
    def replace_manifest(self, rows: Iterable[Mapping], *, reason: str) -> int:
        """write_manifest_rows semantics: `rows` becomes the whole manifest; every other visible
        row is tombstoned with `reason`. The tombstones are ONE set-based statement in the
        database (before-image digests computed there), so Python holds only the request's own
        rows however large the manifest; the net-no-op cleanup and the seal digest are bounded
        the same way."""
        if not reason:
            raise StoreError("replace_manifest requires a reason")
        rows = [dict(r) for r in rows]
        WriteView._unique_ids(rows, "replace_manifest")
        rows = [json.loads(canonical_row(r)) for r in rows]
        deleted = self._q(sql.SQL(
            "INSERT INTO revisions (run_id, batch_seq, tbl, key, op, before_sha256, reason) "
            "SELECT %s, %s, 'manifest', manifest.id, 'tombstone', "
            "encode(sha256(convert_to(manifest.row_text, 'UTF8')), 'hex'), %s FROM {src} "
            "WHERE NOT EXISTS (SELECT 1 FROM unnest(%s::text[]) k(id) WHERE k.id = manifest.id) "
            "ON CONFLICT (run_id, tbl, key, batch_seq) DO UPDATE SET op = 'tombstone', "
            "row_text = NULL, row_sha256 = NULL, reason = EXCLUDED.reason, url_norm = NULL, "
            "url_key = NULL, title_norm = NULL, title_key = NULL, sha256 = NULL, shard = NULL, "
            "topic_key = NULL").format(src=self._src("manifest")),
            [self.run_id, self.seq, reason, [r["id"] for r in rows]]).rowcount
        self._q("DELETE FROM revisions WHERE run_id = %s AND batch_seq = %s AND tbl = 'manifest' "
                "AND op = 'tombstone' AND before_sha256 IS NULL", [self.run_id, self.seq])
        self._record("manifest", "delete", deleted)
        return deleted + self._upsert("manifest", rows, "upsert")

    @_staged
    def update_manifest_fields(self, updates: Mapping[str, Mapping[str, Any]], *,
                               unset: tuple[str, ...] = ()) -> int:
        before = self._rows("manifest", list(updates))
        store.validate_patch(updates, unset, lambda ids: [i for i in ids if i in before])
        items = []
        for sid, patch in updates.items():
            row, old = before[sid]
            after = {k: v for k, v in row.items() if k not in unset}
            after.update(json.loads(json.dumps(dict(patch))))
            text = canonical_row(after)
            if text != old:
                items.append((sid, "put", after, text, None, old))
                self._record("manifest", "update")
        self._stage("manifest", items)
        return len(items)

    @_staged
    def delete_manifest(self, ids: Iterable[str], *, reason: str) -> int:
        if not reason:
            raise StoreError("delete_manifest requires a reason")
        ids = list(dict.fromkeys(ids))
        return self._tombstone("manifest", ids, reason, self._rows("manifest", ids))

    @_staged
    def blocklist_add(self, urls: Iterable[str]) -> int:
        cands = {u for u in map(store.norm_url, urls) if u}
        for u in cands:
            store.validate_json(u, "blocklist url")
        have = {r[0] for r in self._q(sql.SQL("SELECT url FROM {} WHERE key = ANY(%s)").format(
            self._src("blocklist")), [[store.key_digest(u) for u in cands]])}
        new = sorted(cands - have)
        self._stage("blocklist", [
            (store.key_digest(store_pg._check_text(u, "blocklist url")), "put", {"url": u},
             canonical_row({"url": u}), None, None) for u in new])
        self._record("blocklist", "insert", len(new))
        return len(new)

    @_staged
    def ledger_append(self, rows: Iterable[Mapping]) -> int:
        rows = [dict(r) for r in rows]
        store.validate_ledger_rows(rows)
        next_n: dict[str, int] = {}
        items = []
        for r in rows:
            text = store_pg._check_text(canonical_row(r), "ledger row")
            digest = store.key_digest(text)
            if digest not in next_n:
                next_n[digest] = self._q(sql.SQL("SELECT COALESCE(max(n) + 1, 0) FROM {} WHERE "
                                                 "key = %s").format(self._src("ledger")),
                                         [digest]).fetchone()[0]
            n = next_n[digest]
            if n >= 10 ** LEDGER_N_DIGITS:
                raise StoreError("ledger row repeated beyond the staging key range")
            items.append((f"{digest}:{n:0{LEDGER_N_DIGITS}d}", "put", None, text, None, None))
            next_n[digest] = n + 1
        self._stage("ledger", items)
        self._record("ledger", "insert", len(rows))
        return len(rows)

    def _small(self, table: str, column: str, name: str) -> str | None:
        """The stored canonical text of a small table's entry `name`, or None."""
        row = self._q(sql.SQL("SELECT {} FROM {} WHERE name = %s").format(
            sql.Identifier(column), self._src(table)), [name]).fetchone()
        return None if row is None else row[0]

    @_staged
    def rotation_set(self, name: str, value: Mapping) -> None:
        if not isinstance(value, Mapping):
            raise StoreError("rotation value must be a mapping")
        store.validate_json(dict(value), f"rotation {name}")
        text = canonical_row(dict(value))
        before = self._small("rotation", "value_text", name)
        if before == text:
            return
        self._stage("rotation", [(name, "put", None, text, None, before)])
        self._record("rotation", "upsert")

    @_staged
    def control_set(self, name: str, doc: Mapping | None) -> None:
        if name not in store.CONTROL_FILES:
            raise StoreError(f"unknown control document {name!r}")
        text = None
        if doc is not None:
            if not isinstance(doc, Mapping):
                raise StoreError("a control document must be a JSON object")
            store.validate_json(dict(doc), f"control {name}")
            text = canonical_row(dict(doc))
        before = self._small("control_docs", "doc_text", name)
        if before == text:
            return
        if text is None:
            self._stage("control", [(name, "tombstone", None, None, "control_set: deleted",
                                     before)])
            self._record("control", "delete")
        else:
            self._stage("control", [(name, "put", None, text, None, before)])
            self._record("control", "upsert")

    @_staged
    def backend_state_set(self, name: str, value: BackendState) -> None:
        if name.startswith("_") or name not in self._config.backends:
            raise StoreError(f"unknown backend {name}")
        row = self._q(sql.SQL("SELECT enabled, reason FROM {} WHERE name = %s").format(
            self._src("backend_state")), [name]).fetchone()
        before = {"enabled": row[0], "reason": row[1]} if row else None
        after = {"enabled": bool(value.enabled), "reason": value.reason}
        store.validate_json(after, f"backend state {name}")
        if (before or {"enabled": True, "reason": None}) == after:
            return
        self._stage("backend_state", [(name, "put", None, canonical_row(after), None,
                                       None if before is None else canonical_row(before))])
        self._record("backend_state", "upsert")


# --- writer transactions -----------------------------------------------------------------------------

@contextmanager
def _writer_txn(st, writer: WriterToken) -> Iterator[psycopg.Connection]:
    """One fenced transaction on the writer's session (like PgStore.transaction)."""
    conn = st._writer_conn(writer)
    if st._active:
        raise StoreError("transactions do not nest")
    st._active = True
    try:
        with conn.transaction():
            st._fence(conn, writer)
            yield conn
    finally:
        st._active = False


_RUN_COLS = ("run_id", "status", "parent_generation", "writer_epoch", "staged_seq", "frozen_seq",
             "frozen_digest", "required_gates", "promoted_generation", "config_digest",
             "batches_open", "artifact_policy", "owner_epoch", "kind", "producer_commit",
             "extractor_version", "cleaning_ruleset")


def _run(conn, run_id: str, *, lock: bool = False) -> dict:
    row = conn.execute(f"SELECT {', '.join(_RUN_COLS)} FROM runs WHERE run_id = %s"
                       + (" FOR UPDATE" if lock else ""), [run_id]).fetchone()
    if row is None:
        raise StoreError(f"unknown run {run_id}")
    return dict(zip(_RUN_COLS, row))


def _head(conn, *, lock: bool = False) -> int | None:
    return conn.execute("SELECT current_generation FROM dataset"
                        + (" FOR UPDATE" if lock else "")).fetchone()[0]


def _owned_run(conn, run_id: str, writer: WriterToken, what: str) -> dict:
    """The run, locked, after the one ownership check (store_pg.require_run_owner)."""
    store_pg.require_run_owner(conn, run_id, writer, what)
    return _run(conn, run_id, lock=True)


def _require_current(conn, run: dict) -> int | None:
    head = _head(conn)
    if run["parent_generation"] != head:
        raise StaleView(f"run {run['run_id']} was staged on generation "
                        f"{run['parent_generation']}; the current generation is {head}")
    return head


def receipt(conn, run_id: str, step: str, batch: str) -> Receipt | None:
    row = conn.execute(
        "SELECT run_id, step, batch, request_digest, request_text, status, basis_seq, seq, sealed, "
        "chain_digest, counts_text FROM batches WHERE run_id = %s AND step = %s AND batch = %s",
        [run_id, step, batch]).fetchone()
    return None if row is None else Receipt(*row)


def normalize_requests(requests: Sequence[Mapping]) -> tuple[list[dict], str, str]:
    """Validate a batch's mutation requests (store_broker's format: store mutation name, JSON args
    and kwargs); returns them with their canonical text and digest — the batch identity's
    content."""
    import store_broker
    out = []
    if not isinstance(requests, (list, tuple)):
        raise StoreError("requests must be a list")
    for r in requests:
        if not isinstance(r, Mapping) or r.get("call") not in store_broker.MUTATIONS:
            raise StoreError(f"not a store mutation: {r.get('call') if isinstance(r, Mapping) else r!r}")
        args, kwargs = r.get("args") or [], r.get("kwargs") or {}
        if not isinstance(args, list) or not isinstance(kwargs, Mapping):
            raise StoreError("request args must be a list and kwargs an object")
        out.append({"call": r["call"], "args": list(args), "kwargs": dict(kwargs)})
    store.validate_json(out, "batch requests")
    text = store_pg._check_text(store._canonical(out), "batch request")
    return out, text, _sha(text)


def _check_names(step: str, batch: str) -> None:
    if not _NAME.fullmatch(step) or not _NAME.fullmatch(batch):
        raise StoreError("step and batch must be plain names")


# --- run lifecycle -----------------------------------------------------------------------------------

def open_run(st, writer: WriterToken, run_id: str, *, kind: str = "round", producer_commit: str,
             extractor_version: str, cleaning_ruleset: str,
             artifacts: str = "versioned",
             config_documents: Mapping[str, bytes] | None = None) -> StagedRun:
    """Open (or, with the same identity, re-open for the same owner) a run staged on the current
    generation, pinning its git-owned configuration as a sealed config set: the exact bytes
    `config_documents` ({name: bytes}, the producer commit's configuration files — what
    run_round and the standalone runs pass) or, without them, the `config` table. Returns a new
    access token for the run's pipeline children. `artifacts` is the run's immutable artifact
    policy (schema v6): "versioned" — payloads are immutable versions whose every new claim the
    database checks — or "unchecked" (metadata-only runs in tests)."""
    store._check_run_id(run_id)
    token = secrets.token_hex(32)
    with _writer_txn(st, writer) as conn:
        docs = {}
        if config_documents is not None:
            docs = {name: bytes(data) for name, data in config_documents.items()}
            unknown = sorted(set(docs) - set(store.CONFIG_FILES))
            if unknown:
                raise StoreError(f"not configuration documents: {unknown}")
            for name, data in docs.items():
                json.loads(data.decode())   # exact bytes, but they must be JSON
        for name, text, digest in ([] if config_documents is not None else conn.execute(
                "SELECT name, doc_text, digest FROM config ORDER BY name").fetchall()):
            data = text.encode()
            if hashlib.sha256(data).hexdigest() != digest:
                raise StoreError(f"pinned configuration {name} does not match its digest")
            docs[name] = data
        c = store_pg.Contracts(conn, writer)
        config_digest = c.put_config_set(docs)
        head = _head(conn)
        status = c.open_run(run_id, kind=kind, parent_generation=head,
                            producer_commit=producer_commit, config_digest=config_digest,
                            extractor_version=extractor_version, cleaning_ruleset=cleaning_ruleset,
                            artifact_policy=artifacts)
        run = _owned_run(conn, run_id, writer, "re-opening")
        if status != "open":
            raise StoreError(f"run {run_id} is {status}")
        _require_current(conn, run)
        conn.execute("INSERT INTO run_access (run_id, token_sha256) VALUES (%s, %s)",
                     [run_id, _sha(token)])
    return StagedRun(run_id, token, head, artifacts)


@contextmanager
def read_staged(st, run_id: str, *, seq: int | None = None, token: str | None = None,
                writer: WriterToken | None = None) -> Iterator["store_pg.PgReadView"]:
    """A snapshot of generation G plus run `run_id`'s revisions up to staging sequence `seq`
    (default: its staged sequence now). Authorized by the run's writer or an access token of the
    run; the run must be open or frozen and still staged on the current generation."""
    if writer is not None:
        with _writer_txn(st, writer) as wconn:   # the owner check, fenced
            store_pg.require_run_owner(wconn, run_id, writer, "reading its staging")
    elif token is None:
        raise store.AuthorityError(f"reading run {run_id}'s staging needs its writer or an "
                                   "access token")
    conn = st._connect()
    try:
        conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        run = _run(conn, run_id)
        if token is not None and writer is None and not conn.execute(
                "SELECT 1 FROM run_access WHERE run_id = %s AND token_sha256 = %s",
                [run_id, _sha(token)]).fetchone():
            raise store.AuthorityError(f"the access token does not belong to run {run_id}")
        if run["status"] not in ("open", "frozen"):
            raise StaleView(f"run {run_id} is {run['status']}: its staging is not readable")
        head = _require_current(conn, run)
        pin = run["staged_seq"] if seq is None else seq
        if isinstance(pin, bool) or not isinstance(pin, int) or not 0 <= pin <= run["staged_seq"]:
            raise StaleView(f"run {run_id} has staged sequence {run['staged_seq']}, not {seq}")
        lo, = conn.execute("SELECT generation FROM projection_state").fetchone()
        vis = Visibility.read(conn, lo, head, own=(run_id, pin))
        view = store_pg.PgReadView(st, conn, stage_version(run_id, pin),
                                   config_from_set(conn, run["config_digest"]), visibility=vis,
                                   generation=head, stage=(run_id, pin))
        try:
            yield view
        finally:
            view._closed = True
    finally:
        conn.rollback()
        conn.close()


@contextmanager
def read_generation(st, generation: int) -> Iterator["store_pg.PgReadView"]:
    """A snapshot of committed generation `generation` (historical reads). Served from the
    projection plus the overlay while the projection has not passed it: pin a generation
    (pin_generation) to keep it reconstructible; reconstructing generations the projection
    already passed needs the baseline generation (decision (1), a separate tool)."""
    conn = st._connect()
    try:
        conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
        head = _head(conn)
        lo, folding = conn.execute("SELECT generation, fold_generation FROM projection_state"
                                   ).fetchone()
        if head is None or not 0 <= generation <= head:
            raise StoreError(f"generation {generation} is not promoted (current: {head})")
        floor = folding if folding is not None else (-1 if lo is None else lo)
        if generation < floor:
            raise StoreError(f"generation {generation} is already folded into the projection "
                             f"(at {lo}, folding {folding}); pin generations that must stay "
                             "reconstructible")
        vis = Visibility.read(conn, lo, generation)
        digest, = conn.execute("SELECT config_digest FROM generations WHERE generation = %s",
                               [generation]).fetchone()
        view = store_pg.PgReadView(st, conn, Version(f"pg:generation:{generation}"),
                                   config_from_set(conn, digest),
                                   visibility=None if vis.empty else vis, generation=generation)
        try:
            yield view
        finally:
            view._closed = True
    finally:
        conn.rollback()
        conn.close()


def stage_batch(st, writer: WriterToken, run_id: str, step: str, batch: str,
                requests: Sequence[Mapping], *, expected_version: Version,
                persist_only: bool = False) -> StageResult:
    """Record batch (run, step, batch) of `requests` in ONE transaction: its immutable request
    and digest, then — unless `persist_only` — its revisions at the next staging sequence, sealed.

    * Exact retry: an applied batch with the same digest answers with its stored results (no
      version check, nothing written); a persisted (requested) one with the same digest is
      applied now, exactly as persisted, provided the run is still at the sequence it was
      computed at.
    * Conflicting retry: the same identity with another digest raises StoreError.
    * A new batch must expect the run's current staging version (stage_version), else
      VersionConflict: it was computed against a sequence that no longer is the run's.
    Any failure leaves nothing: no receipt, no revision, no sequence."""
    _check_names(step, batch)
    requests, text, digest = normalize_requests(requests)
    with _writer_txn(st, writer) as conn:
        run = _owned_run(conn, run_id, writer, "staging")
        got = receipt(conn, run_id, step, batch)
        if got is not None:
            if got.request_digest != digest:
                raise StoreError(f"batch {run_id}.{step}.{batch} was requested with different "
                                 "content (conflicting retry)")
            if got.status == "applied":
                return StageResult(got.results, stage_version(run_id, got.seq), got.seq,
                                   "applied", True)
            if got.status != "requested":
                raise StoreError(f"batch {run_id}.{step}.{batch} is {got.status}")
        if run["status"] != "open":
            raise StaleView(f"run {run_id} is {run['status']}: it stages nothing more")
        head = _require_current(conn, run)
        if got is None:
            if expected_version != stage_version(run_id, run["staged_seq"]):
                raise VersionConflict(f"batch {run_id}.{step}.{batch} was computed at "
                                      f"{expected_version.token}; run {run_id} is at sequence "
                                      f"{run['staged_seq']}")
            conn.execute("INSERT INTO batches (run_id, step, batch, request_digest, request_text, "
                         "basis_seq) VALUES (%s, %s, %s, %s, %s, %s)",
                         [run_id, step, batch, digest, text, run["staged_seq"]])
            basis = run["staged_seq"]
        else:
            basis = got.basis_seq
            if basis != run["staged_seq"]:
                raise VersionConflict(f"persisted batch {run_id}.{step}.{batch} was computed at "
                                      f"sequence {basis}; run {run_id} is at {run['staged_seq']}")
        if persist_only:
            return StageResult([], stage_version(run_id, basis), None, "requested", got is not None)
        seq = basis + 1
        conn.execute("UPDATE batches SET status = 'applied', seq = %s, applied_at = now() "
                     "WHERE run_id = %s AND step = %s AND batch = %s",
                     [seq, run_id, step, batch])
        lo, = conn.execute("SELECT generation FROM projection_state").fetchone()
        view = StagedWriteView(st, conn, config_from_set(conn, run["config_digest"]),
                               Visibility.read(conn, lo, head, own=(run_id, seq)), run_id, seq,
                               head, run["artifact_policy"])
        try:
            import store_broker
            results = [getattr(view, r["call"])(**store_broker._bind(r["call"], r["args"],
                                                                    r["kwargs"]))
                       for r in requests]
        finally:
            view._closed = True
        counts = store._canonical({"counts": view.counts(), "results": results})
        conn.execute("UPDATE batches SET sealed = true, counts_text = %s WHERE run_id = %s AND "
                     "step = %s AND batch = %s", [counts, run_id, step, batch])
        if seq % ANALYZE_EVERY == 0:
            # the trigger functions' cached plans were made against these tables' statistics
            # when the run started; refreshing them (and invalidating those plans) keeps a long
            # writer session from scanning a grown table, independently of autovacuum
            conn.execute("ANALYZE batches, revisions")
    return StageResult(results, stage_version(run_id, seq), seq, "applied", False)


def batch_receipt(st, writer: WriterToken, run_id: str, step: str, batch: str) -> Receipt | None:
    with _writer_txn(st, writer) as conn:
        store_pg.require_run_owner(conn, run_id, writer, "reading a batch receipt")
        return receipt(conn, run_id, step, batch)


def abandon_batch(st, writer: WriterToken, run_id: str, step: str, batch: str) -> None:
    """Give up a persisted (requested) batch without applying it (owner only; a new owner
    aborts the run instead)."""
    with _writer_txn(st, writer) as conn:
        store_pg.require_run_owner(conn, run_id, writer, "abandoning a batch")
        got = receipt(conn, run_id, step, batch)
        if got is None or got.status == "abandoned":
            return
        if got.status != "requested":
            raise StoreError(f"batch {run_id}.{step}.{batch} is {got.status}")
        conn.execute("UPDATE batches SET status = 'abandoned' WHERE run_id = %s AND step = %s "
                     "AND batch = %s", [run_id, step, batch])


def freeze(st, writer: WriterToken, run_id: str, *, required_gates: Iterable[str]) -> Frozen:
    """Stop staging: bind the run to its staged sequence, that sequence's chain digest and the
    gates that must pass before promotion. Refused while a batch is requested but not applied.
    Exact retry returns the same Frozen."""
    gates = sorted(set(required_gates))
    if not gates or not all(isinstance(g, str) and _NAME.fullmatch(g) for g in gates):
        raise StoreError("freeze needs a non-empty list of plain gate names")
    text = store._canonical(gates)
    with _writer_txn(st, writer) as conn:
        run = _owned_run(conn, run_id, writer, "freezing")
        if run["status"] == "frozen":
            if run["required_gates"] != text:
                raise StoreError(f"run {run_id} is frozen with required gates "
                                 f"{run['required_gates']}, not {text}")
            return Frozen(run_id, run["frozen_seq"], run["frozen_digest"])
        if run["status"] != "open":
            raise StaleView(f"run {run_id} is {run['status']}")
        _require_current(conn, run)
        if run["artifact_policy"] == "versioned" and "artifacts" not in gates:
            raise StoreError(f"run {run_id} stages immutable artifact versions: its required "
                             "gates must include \"artifacts\" (artifact_store.verify_run)")
        if run["batches_open"]:
            raise StoreError(f"run {run_id} has {run['batches_open']} requested batch(es) that "
                             "were never applied: apply or abandon them first")
        seq, digest = conn.execute(
            "UPDATE runs SET status = 'frozen', frozen_seq = staged_seq, frozen_digest = CASE WHEN "
            "staged_seq = 0 THEN nk_chain_origin(run_id) ELSE (SELECT b.chain_digest FROM "
            "batches b WHERE b.run_id = runs.run_id AND b.seq = runs.staged_seq) END, "
            "required_gates = %s WHERE run_id = %s RETURNING frozen_seq, frozen_digest",
            [text, run_id]).fetchone()
    return Frozen(run_id, seq, digest)


def record_gate(st, writer: WriterToken, frozen: Frozen, gate: str, *, passed: bool,
                detail: Mapping | None = None) -> None:
    """Record a gate's verdict bound to the frozen sequence and digest it validated. Exact retry
    is a no-op; a different verdict for the same gate raises."""
    if not isinstance(gate, str) or not _NAME.fullmatch(gate):
        raise StoreError(f"invalid gate name {gate!r}")
    verdict = "passed" if passed else "failed"
    store.validate_json(dict(detail or {}), f"gate {gate} detail")
    text = store_pg._check_text(store._canonical(dict(detail or {})), f"gate {gate} detail")
    with _writer_txn(st, writer) as conn:
        run = _owned_run(conn, frozen.run_id, writer, "recording a gate")
        row = conn.execute("SELECT frozen_seq, frozen_digest, verdict, detail_text FROM "
                           "gate_receipts WHERE run_id = %s AND gate = %s",
                           [frozen.run_id, gate]).fetchone()
        if row is not None:
            if tuple(row) != (frozen.seq, frozen.digest, verdict, text):
                raise StoreError(f"gate {gate} of run {frozen.run_id} was already recorded "
                                 "differently")
            return
        if run["status"] != "frozen":
            raise StaleView(f"run {frozen.run_id} is {run['status']}, not frozen")
        if (run["frozen_seq"], run["frozen_digest"]) != (frozen.seq, frozen.digest):
            raise StoreError(f"gate {gate} validated sequence {frozen.seq}/{frozen.digest[:12]}; "
                             f"run {frozen.run_id} froze at {run['frozen_seq']}/"
                             f"{run['frozen_digest'][:12]}")
        conn.execute("INSERT INTO gate_receipts (run_id, gate, frozen_seq, frozen_digest, verdict, "
                     "detail_text) VALUES (%s, %s, %s, %s, %s, %s)",
                     [frozen.run_id, gate, frozen.seq, frozen.digest, verdict, text])


def promote(st, writer: WriterToken, frozen: Frozen) -> int:
    """Promote a frozen run whose required gates passed at exactly `frozen`: ONE transaction
    writing a constant number of rows (generation, run status, current generation, outbox row,
    version counter) whatever the run staged. Returns the new generation; exact retry returns
    it again."""
    run_id = frozen.run_id
    with _writer_txn(st, writer) as conn:
        run = _owned_run(conn, run_id, writer, "promotion")
        if run["status"] == "promoted":
            if (run["frozen_seq"], run["frozen_digest"]) != (frozen.seq, frozen.digest):
                raise StoreError(f"run {run_id} was promoted at another frozen state")
            return run["promoted_generation"]
        if run["status"] != "frozen":
            raise StaleView(f"run {run_id} is {run['status']}, not frozen")
        if (run["frozen_seq"], run["frozen_digest"]) != (frozen.seq, frozen.digest):
            raise StoreError(f"run {run_id} froze at {run['frozen_seq']}/"
                             f"{run['frozen_digest'][:12]}, not at the state the gates validated")
        head = _head(conn, lock=True)
        if run["parent_generation"] != head:
            raise StaleView(f"run {run_id} was staged on generation {run['parent_generation']}; "
                            f"the current generation is {head}")
        if not conn.execute("SELECT nk_gates_passed(%s)", [run_id]).fetchone()[0]:
            raise StoreError(f"run {run_id} has not passed every required gate "
                             f"{run['required_gates']} at its frozen sequence")
        # the run's summary, accumulated as each batch sealed (V5_DDL): constant work here
        counts, batches = conn.execute("SELECT staged_counts, staged_seq FROM runs WHERE "
                                       "run_id = %s", [run_id]).fetchone()
        generation = 0 if head is None else head + 1
        counts_text = store._canonical({"batches": batches, "ops": counts})
        conn.execute("INSERT INTO generations (generation, parent, run_id, producer_commit, "
                     "config_digest, extractor_version, cleaning_ruleset, frozen_seq, "
                     "frozen_digest, counts_text) SELECT %s, %s, run_id, producer_commit, "
                     "config_digest, extractor_version, cleaning_ruleset, frozen_seq, "
                     "frozen_digest, %s FROM runs WHERE run_id = %s",
                     [generation, head, counts_text, run_id])
        conn.execute("UPDATE runs SET status = 'promoted', promoted_generation = %s, "
                     "ended_at = now() WHERE run_id = %s", [generation, run_id])
        conn.execute("UPDATE dataset SET current_generation = %s", [generation])
        seq = conn.execute("SELECT allocated + 1 FROM outbox_state").fetchone()[0]
        conn.execute("INSERT INTO outbox (seq, generation, payload_text) VALUES (%s, %s, %s)",
                     [seq, generation, store._canonical({
                         "run": run_id, "generation": generation, "parent": head,
                         "frozen_seq": frozen.seq, "frozen_digest": frozen.digest,
                         "counts": json.loads(counts_text)})])
        conn.execute("UPDATE state SET generation = generation + 1")
    return generation


def abort_run(st, writer: WriterToken, run_id: str, *, reason: str) -> None:
    """CROSS-OWNER recovery operation: abort an unpromoted run, whichever writer epoch opened it
    (aborting is the safe default for a run whose owner is gone). Its staging stays invisible and
    may be purged. Idempotent."""
    if not reason:
        raise StoreError("aborting a run needs a reason")
    with _writer_txn(st, writer) as conn:
        run = _run(conn, run_id, lock=True)
        if run["status"] == "aborted":
            return
        if run["status"] == "promoted":
            raise StoreError(f"run {run_id} is promoted: it stands")
        conn.execute("UPDATE runs SET status = 'aborted', ended_at = now(), detail_text = %s "
                     "WHERE run_id = %s", [store._canonical({"aborted": reason}), run_id])


def purge_run(st, writer: WriterToken, run_id: str, *, limit: int = FOLD_BATCH) -> int:
    """CROSS-OWNER cleanup: delete up to `limit` staging rows of an aborted run — revisions,
    then artifact references, then gate receipts and batch receipts — in ONE short transaction;
    returns how many rows went. Call until it returns 0: the call that finds nothing left also
    takes the run off the purge queue (schema v7). The run row itself, with its status and
    abort reason, is kept (failure evidence for the generation-range review)."""
    with _writer_txn(st, writer) as conn:
        if _run(conn, run_id)["status"] != "aborted":
            raise StoreError(f"run {run_id} is not aborted")
        n = conn.execute("DELETE FROM revisions WHERE rev_id IN (SELECT rev_id FROM revisions "
                         "WHERE run_id = %s LIMIT %s)", [run_id, limit]).rowcount
        if n == 0:
            # its artifact references go; the registered identities and their immutable local
            # versions stay (reference-checked cleanup is stage 5's)
            n += conn.execute("DELETE FROM run_artifacts WHERE ctid IN (SELECT ctid FROM "
                              "run_artifacts WHERE run_id = %s LIMIT %s)",
                              [run_id, limit]).rowcount
        if n == 0:
            n += conn.execute("DELETE FROM gate_receipts WHERE run_id = %s", [run_id]).rowcount
        if n == 0:
            n += conn.execute("DELETE FROM batches WHERE ctid IN (SELECT ctid FROM batches WHERE "
                              "run_id = %s LIMIT %s)", [run_id, limit]).rowcount
        if n == 0:
            conn.execute("DELETE FROM purge_queue WHERE run_id = %s", [run_id])
        return n


def purge_due(st, writer: WriterToken, *, grace_seconds: float, limit: int = 16) -> list[str]:
    """Aborted runs queued for purging at least `grace_seconds` ago (oldest first): a failed
    round's staging stays inspectable for a while before it is purged."""
    with _writer_txn(st, writer) as conn:
        return [r for (r,) in conn.execute(
            "SELECT run_id FROM purge_queue WHERE queued_at <= now() - make_interval(secs => %s) "
            "ORDER BY queued_at, run_id LIMIT %s", [float(grace_seconds), limit]).fetchall()]


# --- durable run status and adoption (stage 4 step 4, schema v7) ------------------------------------

def run_status(st, writer: WriterToken, run_id: str) -> dict | None:
    """The run's durable row (status, parent, owner, provenance, sequences), read in a fenced
    writer transaction, or None when the query succeeded and no such run exists. Any database
    failure raises: an unknown outcome is never read as "absent"."""
    store._check_run_id(run_id)
    with _writer_txn(st, writer) as conn:
        row = conn.execute(f"SELECT {', '.join(_RUN_COLS)} FROM runs WHERE run_id = %s",
                           [run_id]).fetchone()
        return None if row is None else dict(zip(_RUN_COLS, row))


def unfinished_runs(st, writer: WriterToken) -> list[dict]:
    """Every open or frozen run (the runs_unfinished index), oldest first."""
    with _writer_txn(st, writer) as conn:
        return [dict(zip(_RUN_COLS, row)) for row in conn.execute(
            f"SELECT {', '.join(_RUN_COLS)} FROM runs WHERE status IN ('open', 'frozen') "
            "ORDER BY started_at, run_id").fetchall()]


def gate_receipts(st, writer: WriterToken, run_id: str) -> dict[str, str]:
    """{gate: verdict} of the run's recorded gate receipts."""
    with _writer_txn(st, writer) as conn:
        return dict(conn.execute("SELECT gate, verdict FROM gate_receipts WHERE run_id = %s",
                                 [run_id]).fetchall())


def adopt_run(st, writer: WriterToken, run_id: str, *, reason: str, producer_commit: str,
              config_digest: str, extractor_version: str) -> StagedRun:
    """Explicit resume: `writer` takes over open or frozen run `run_id` in ONE transaction — a
    logged adoption (run_adoptions) and a new access token for the resumed coordinator's
    children. The database refuses unless `writer` is the current writer, the run's parent is
    still the current generation, the producer commit, configuration set and extractor version
    the caller runs with are the run's, and every artifact the run referenced has a verified
    local version (artifact_store.verify_run first). Otherwise abort and start a new run.
    Re-adopting a run this writer already owns only issues a new token."""
    if not reason:
        raise StoreError("adopting a run needs a reason")
    token = secrets.token_hex(32)
    with _writer_txn(st, writer) as conn:
        run = _run(conn, run_id, lock=True)
        if run["status"] not in ("open", "frozen"):
            raise StaleView(f"run {run_id} is {run['status']}: only an open or frozen run can "
                            "be resumed")
        if run["owner_epoch"] != writer.epoch:
            conn.execute(
                "INSERT INTO run_adoptions (run_id, owner_epoch, previous_epoch, status, "
                "parent_generation, producer_commit, config_digest, extractor_version, "
                "staged_seq, reason) SELECT %s, %s, %s, %s, current_generation, %s, %s, %s, %s, "
                "%s FROM dataset",
                [run_id, writer.epoch, run["owner_epoch"], run["status"], producer_commit,
                 config_digest, extractor_version, run["staged_seq"], reason])
        conn.execute("INSERT INTO run_access (run_id, token_sha256) VALUES (%s, %s)",
                     [run_id, _sha(token)])
        attempt = 1 + conn.execute("SELECT count(*) FROM run_adoptions WHERE run_id = %s",
                                   [run_id]).fetchone()[0]
    return StagedRun(run_id, token, run["parent_generation"], run["artifact_policy"], attempt,
                     run["status"])


def pin_generation(st, writer: WriterToken, generation: int, *, holder: str, reason: str,
                   until: str | None = None) -> None:
    """Keep `generation` reconstructible (read_generation): the fold stops before it."""
    with _writer_txn(st, writer) as conn:
        conn.execute("INSERT INTO generation_retention (generation, holder, reason, until) VALUES "
                     "(%s, %s, %s, %s) ON CONFLICT (generation, holder) DO UPDATE SET "
                     "reason = EXCLUDED.reason, until = EXCLUDED.until",
                     [generation, holder, reason, until])


def unpin_generation(st, writer: WriterToken, generation: int, *, holder: str) -> None:
    with _writer_txn(st, writer) as conn:
        conn.execute("DELETE FROM generation_retention WHERE generation = %s AND holder = %s",
                     [generation, holder])


# --- folding (the projection consumer) ------------------------------------------------------------------

def _fold_apply(conn, tbl: str, puts: list[tuple[str, str]], gone: list[str]) -> None:
    with conn.cursor() as cur:
        if tbl in ("entries", "manifest"):
            store_pg.put_rows(cur, tbl, [json.loads(text) for _, text in puts],
                              texts=[text for _, text in puts])
            if gone:
                cur.execute(sql.SQL("DELETE FROM {} WHERE id = ANY(%s)").format(
                    sql.Identifier(tbl)), [gone])
        elif tbl == "blocklist":
            cur.executemany("INSERT INTO blocklist (key, url) VALUES (%s, %s) ON CONFLICT (key) "
                            "DO NOTHING", [(k, json.loads(t)["url"]) for k, t in puts])
            if gone:
                cur.execute("DELETE FROM blocklist WHERE key = ANY(%s)", [gone])
        elif tbl == "ledger":
            cur.executemany("INSERT INTO ledger (key, n, row_text) VALUES (%s, %s, %s) "
                            "ON CONFLICT (key, n) DO NOTHING",
                            [(k.split(":")[0], int(k.split(":")[1]), t) for k, t in puts])
            for k in gone:
                cur.execute("DELETE FROM ledger WHERE key = %s AND n = %s",
                            [k.split(":")[0], int(k.split(":")[1])])
        elif tbl == "rotation":
            cur.executemany("INSERT INTO rotation (name, value_text) VALUES (%s, %s) ON CONFLICT "
                            "(name) DO UPDATE SET value_text = EXCLUDED.value_text", puts)
            if gone:
                cur.execute("DELETE FROM rotation WHERE name = ANY(%s)", [gone])
        elif tbl == "control":
            cur.executemany("INSERT INTO control_docs (name, doc_text) VALUES (%s, %s) ON "
                            "CONFLICT (name) DO UPDATE SET doc_text = EXCLUDED.doc_text", puts)
            if gone:
                cur.execute("DELETE FROM control_docs WHERE name = ANY(%s)", [gone])
        elif tbl == "backend_state":
            cur.executemany("INSERT INTO backend_state (name, enabled, reason) VALUES (%s, %s, %s) "
                            "ON CONFLICT (name) DO UPDATE SET enabled = EXCLUDED.enabled, "
                            "reason = EXCLUDED.reason",
                            [(k, bool(json.loads(t)["enabled"]), json.loads(t)["reason"])
                             for k, t in puts])
            if gone:
                cur.execute("DELETE FROM backend_state WHERE name = ANY(%s)", [gone])
        else:
            raise StoreError(f"cannot fold table {tbl!r}")


def fold(st, writer: WriterToken, *, limit: int = FOLD_BATCH) -> FoldProgress:
    """Fold up to `limit` keys of the next promoted generation into the projection tables (ONE
    transaction; idempotent, resumable from its keyset cursor). When the generation is complete
    the projection becomes it and the "projection" consumer acknowledges its outbox row. Never
    folds past an active retention pin."""
    if not 1 <= limit <= 1_000_000:
        raise StoreError("fold limit must be within 1..1000000")
    with _writer_txn(st, writer) as conn:
        lo, folding, ctbl, ckey = conn.execute(
            "SELECT generation, fold_generation, fold_tbl, fold_key FROM projection_state "
            "FOR UPDATE").fetchone()
        head = _head(conn)
        target = folding if folding is not None else (0 if lo is None else lo + 1)
        if head is None or target > head:
            return FoldProgress(None, 0, False)
        pinned = conn.execute("SELECT min(generation) FROM generation_retention WHERE until IS "
                              "NULL OR until > now()").fetchone()[0]
        if pinned is not None and pinned < target:
            return FoldProgress(target, 0, False, blocked=True)
        run_id, = conn.execute("SELECT run_id FROM generations WHERE generation = %s",
                               [target]).fetchone()
        after = sql.SQL("") if ctbl is None else sql.SQL(" AND (tbl, key) > ({}, {})").format(
            sql.Literal(ctbl), sql.Literal(ckey))
        rows = conn.execute(sql.SQL(
            "SELECT DISTINCT ON (tbl, key) tbl, key, op, row_text FROM revisions WHERE run_id = %s"
            "{} ORDER BY tbl, key, batch_seq DESC LIMIT %s").format(after),
            [run_id, limit]).fetchall()
        by_tbl: dict[str, tuple[list, list]] = {}
        for tbl, key, op, text in rows:
            puts, gone = by_tbl.setdefault(tbl, ([], []))
            (puts.append((key, text)) if op == "put" else gone.append(key))
        for tbl, (puts, gone) in by_tbl.items():
            _fold_apply(conn, tbl, puts, gone)
        if len(rows) == limit:
            conn.execute("UPDATE projection_state SET fold_generation = %s, fold_tbl = %s, "
                         "fold_key = %s", [target, rows[-1][0], rows[-1][1]])
            return FoldProgress(target, len(rows), False)
        conn.execute("UPDATE projection_state SET generation = %s, fold_generation = NULL, "
                     "fold_tbl = NULL, fold_key = NULL", [target])
        seq = conn.execute("SELECT seq FROM outbox WHERE generation = %s", [target]).fetchone()
        if seq is None:
            raise StoreError(f"generation {target} has no outbox row to acknowledge")
        c = store_pg.Contracts(conn, writer)
        c.ack("projection", seq[0], "ok", {"generation": target})
        c.advance("projection")
        return FoldProgress(target, len(rows), True)


def fold_all(st, writer: WriterToken, *, limit: int = FOLD_BATCH) -> int:
    """Fold every promoted generation the pins allow; returns how many were completed."""
    done = 0
    while True:
        progress = fold(st, writer, limit=limit)
        if progress.generation is None or progress.blocked:
            return done
        done += progress.done


def generation_config(view, generation: int) -> str | None:
    """Generation `generation`'s pinned configuration set digest, in `view`'s snapshot."""
    row = view._q("SELECT config_digest FROM generations WHERE generation = %s",
                  [generation]).fetchone()
    return None if row is None else row[0]


def changed_manifest_ids(view, lo: int | None, hi: int, *, after: str = "",
                         limit: int = 10_000) -> list[str]:
    """Up to `limit` ids, after `after` in key order, whose manifest rows a generation in
    (lo, hi] revised (a put or a tombstone), read in `view`'s snapshot. Revisions of promoted
    runs are never rewritten or purged, so this holds whether or not the fold has passed them
    (materialize.refresh's incremental mode)."""
    runs = [r for (r,) in view._q(
        "SELECT run_id FROM runs WHERE promoted_generation > %s AND promoted_generation <= %s",
        [-1 if lo is None else lo, hi]).fetchall()]
    if not runs:
        return []
    return [k for (k,) in view._q(
        "SELECT DISTINCT key FROM revisions WHERE tbl = 'manifest' AND run_id = ANY(%s) AND "
        "key > %s ORDER BY key LIMIT %s", [runs, after, limit]).fetchall()]


def legacy_writes_refused(conn) -> str | None:
    """Why direct (legacy) writes to the projection tables are refused in `conn`'s schema, or
    None: once a run is staging or a generation exists, the projection belongs to the fold.
    Callers hold the writer (state row lock), so no run can open meanwhile."""
    head, unfinished = conn.execute(
        "SELECT current_generation, EXISTS (SELECT 1 FROM runs WHERE status IN "
        "('open', 'frozen')) FROM dataset").fetchone()
    if head is not None:
        return f"generation {head} is promoted: the projection is maintained by folding"
    if unfinished:
        return "a staged run is open or frozen: the projection must not change under it"
    return None
