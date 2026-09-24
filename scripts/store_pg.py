#!/usr/bin/env python3
"""store_pg.py — PostgreSQL implementation of the store.py interface (ADR 0001, stage 2).

Selected with NEKAISE_STORE=postgres and NEKAISE_PG_DSN (store.open). It passes the same
conformance suite as FileStore (tests/test_store_contract.py) and produces byte-identical canonical
exports. During stage 2 it is a SHADOW: FileStore stays authoritative and scripts/pg_shadow.py
replicates committed rounds into it.

Representation (decided in the stage-2 review):
* The authoritative form of every row is its canonical JSON text (store.canonical_row) in a
  `row_text` column; a generated `row jsonb` column exists only for predicates. jsonb rewrites
  numbers (1e20 becomes an integer, -0.0 becomes 0), so rows are always read back from row_text.
* Unbounded lookup values (normalized URLs and titles, blocklist URLs, ledger rows) are indexed by
  their sha256 and verified against the full value after lookup: a B-tree entry cannot hold them.
* Normalized keys are computed in Python (store.norm_url / norm_title), so known() agrees with
  FileStore by construction.
* Git-owned configuration is pinned in the `config` table (replicated per commit), and a view pins
  it when it opens.

Concurrency: a writer holds a session advisory lock on a dedicated connection and bumps
state.writer_epoch; every transaction runs on that same session and fences on the epoch, so a
token dies with its session. This is a recorded ADR exception for single-host operation: the
lease + heartbeat design is required before a second host writes. Reads are REPEATABLE READ
snapshots and never mix generations.
"""
from __future__ import annotations

import hashlib
import json
import os
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

import store
from store import (BackendState, ConfigSnapshot, Cursor, KnownHits, Page, Stage, StaleView,
                   StoreError, Table, Version, VersionConflict, WriteView, WriterError,
                   WriterToken, canonical_row, key_digest, norm_title, norm_url)

SCHEMA_VERSION = 3
DEFAULT_DSN = "host=/home/zengp/.local/share/nekaise-pg/run dbname=nekaise"

DDL = """
CREATE SCHEMA IF NOT EXISTS {s};
CREATE TABLE IF NOT EXISTS {s}.state (
    one boolean PRIMARY KEY DEFAULT true CHECK (one),
    schema_version int NOT NULL,
    generation bigint NOT NULL DEFAULT 0,
    writer_epoch bigint NOT NULL DEFAULT 0,
    replication jsonb NOT NULL DEFAULT '{{}}'::jsonb
);
CREATE TABLE IF NOT EXISTS {s}.entries (
    id text COLLATE "C" PRIMARY KEY,
    row_text text NOT NULL,
    row jsonb GENERATED ALWAYS AS (row_text::jsonb) STORED,
    url_norm text, url_key bytea,
    title_norm text, title_key bytea
);
CREATE INDEX IF NOT EXISTS entries_url_key ON {s}.entries (url_key);
CREATE INDEX IF NOT EXISTS entries_title_key ON {s}.entries (title_key);
CREATE TABLE IF NOT EXISTS {s}.manifest (
    id text COLLATE "C" PRIMARY KEY,
    row_text text NOT NULL,
    row jsonb GENERATED ALWAYS AS (row_text::jsonb) STORED,
    url_norm text, url_key bytea,
    title_norm text, title_key bytea,
    sha256 text COLLATE "C",
    shard text COLLATE "C",
    topic_key text COLLATE "C"
);
CREATE INDEX IF NOT EXISTS manifest_url_key ON {s}.manifest (url_key);
CREATE INDEX IF NOT EXISTS manifest_title_key ON {s}.manifest (title_key);
CREATE INDEX IF NOT EXISTS manifest_sha256 ON {s}.manifest (sha256) WHERE sha256 IS NOT NULL;
CREATE TABLE IF NOT EXISTS {s}.blocklist (
    key text COLLATE "C" PRIMARY KEY,
    url text NOT NULL
);
CREATE TABLE IF NOT EXISTS {s}.ledger (
    key text COLLATE "C" NOT NULL,
    n int NOT NULL,
    row_text text NOT NULL,
    row jsonb GENERATED ALWAYS AS (row_text::jsonb) STORED,
    PRIMARY KEY (key, n)
);
CREATE TABLE IF NOT EXISTS {s}.events (
    seq bigint PRIMARY KEY,
    run_id text NOT NULL,
    op text NOT NULL,
    row_text text NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS events_commit_run ON {s}.events (run_id) WHERE op = 'commit';
CREATE TABLE IF NOT EXISTS {s}.rotation (
    name text COLLATE "C" PRIMARY KEY,
    value_text text NOT NULL
);
CREATE TABLE IF NOT EXISTS {s}.backend_state (
    name text COLLATE "C" PRIMARY KEY,
    enabled boolean NOT NULL,
    reason text
);
CREATE TABLE IF NOT EXISTS {s}.control_docs (
    name text COLLATE "C" PRIMARY KEY,
    doc_text text NOT NULL
);
CREATE TABLE IF NOT EXISTS {s}.config (
    name text COLLATE "C" PRIMARY KEY,
    doc_text text NOT NULL,
    digest text NOT NULL
);
CREATE TABLE IF NOT EXISTS {s}.replication_receipts (
    commit text PRIMARY KEY,
    parent text,
    applied_at timestamptz NOT NULL DEFAULT now(),
    stats jsonb NOT NULL
);
INSERT INTO {s}.state (schema_version) VALUES ({v}) ON CONFLICT DO NOTHING;
"""
# Schema migrations: version -> function(conn, schema) bringing the previous version up to it. The
# DDL above is idempotent and already creates every table and column for NEW schemas; migrations
# add what an older schema lacks and backfill it.


def _migrate_2(conn, schema):  # control_docs (created by the DDL)
    pass


def _migrate_3(conn, schema):  # manifest legacy-order columns, backfilled from the rows
    s = sql.Identifier(schema)
    conn.execute(sql.SQL("ALTER TABLE {}.manifest ADD COLUMN IF NOT EXISTS shard text COLLATE \"C\", "
                         "ADD COLUMN IF NOT EXISTS topic_key text COLLATE \"C\"").format(s))
    with conn.cursor(name="migrate3") as cur, conn.cursor() as up:
        cur.itersize = 20000
        cur.execute(sql.SQL("SELECT id, row_text FROM {}.manifest").format(s))
        batch = []
        for sid, text in cur:
            shard, topic, _ = store.legacy_manifest_key(json.loads(text))
            batch.append((shard, topic, sid))
            if len(batch) >= 20000:
                up.executemany(sql.SQL("UPDATE {}.manifest SET shard = %s, topic_key = %s "
                                       "WHERE id = %s").format(s), batch)
                batch.clear()
        if batch:
            up.executemany(sql.SQL("UPDATE {}.manifest SET shard = %s, topic_key = %s "
                                   "WHERE id = %s").format(s), batch)
    conn.execute(sql.SQL("CREATE INDEX IF NOT EXISTS manifest_legacy_order ON {}.manifest "
                         "(shard, topic_key, id)").format(s))


MIGRATIONS = {2: _migrate_2, 3: _migrate_3}
# Indexes on columns that migrations may have just added: created after migrating.
POST_DDL = "CREATE INDEX IF NOT EXISTS manifest_legacy_order ON {s}.manifest (shard, topic_key, id);"


def put_rows(cur, table: str, rows: list[dict]) -> None:
    """Upsert entries/manifest rows with every derived column (the one writer both the store and
    the shadow replicator use)."""
    if not rows:
        return
    manifest = table == "manifest"
    cols = ["id", "row_text", "url_norm", "url_key", "title_norm", "title_key"]
    if manifest:
        cols += ["sha256", "shard", "topic_key"]
    q = sql.SQL("INSERT INTO {t} ({c}) VALUES ({v}) ON CONFLICT (id) DO UPDATE SET {u}").format(
        t=sql.Identifier(table), c=sql.SQL(", ").join(map(sql.Identifier, cols)),
        v=sql.SQL(", ").join(sql.Placeholder() for _ in cols),
        u=sql.SQL(", ").join(sql.SQL("{c} = EXCLUDED.{c}").format(c=sql.Identifier(c))
                             for c in cols[1:]))
    params = []
    for r in rows:
        store.validate_json(r, f"{table} row {r.get('id')!r}")
        rec = [r["id"], canonical_row(r), *_keys_for(r)]
        if manifest:
            sha = r.get("sha256")
            shard, topic, _ = store.legacy_manifest_key(r)
            rec += [sha if isinstance(sha, str) and sha else None, shard, topic]
        params.append(rec)
    cur.executemany(q, params)


ROW_TABLES = {Table.ENTRIES: "entries", Table.MANIFEST: "manifest", Table.LEDGER: "ledger"}


def _url_key(u: str | None) -> bytes | None:
    return hashlib.sha256(u.encode()).digest() if u else None


def _check_text(value: str, what: str) -> str:
    if "\x00" in value:
        raise StoreError(f"{what} contains a NUL character, which PostgreSQL text cannot store")
    return value


def _keys_for(row: Mapping) -> tuple:
    u, t = norm_url(row.get("url")), norm_title(row.get("title"))
    return (u or None, _url_key(u), t or None, _url_key(t))


def _json_number_is_float(text: str) -> bool:
    return any(c in text for c in ".eE")


# --- predicate compiler ----------------------------------------------------------------------------

def compile_predicate(pred, rowexpr: sql.Composable) -> tuple[sql.Composable, list]:
    """SQL for a store predicate over a jsonb row expression. Every leaf is COALESCEd to false so
    the logic stays two-valued and Not() is exact, mirroring store.evaluate."""
    if pred is None:
        return sql.SQL("true"), []
    if isinstance(pred, store.Eq):
        return (sql.SQL("COALESCE(({r} -> %s) = %s::jsonb, false)").format(r=rowexpr),
                [pred.field, Jsonb(pred.value)])
    if isinstance(pred, store.In):
        return (sql.SQL("COALESCE(({r} -> %s) = ANY(%s::jsonb[]), false)").format(r=rowexpr),
                [pred.field, [Jsonb(v) for v in pred.values]])
    if isinstance(pred, store.Prefix):
        return (sql.SQL("COALESCE(jsonb_typeof({r} -> %s) = 'string' "
                        "AND starts_with({r} ->> %s, %s), false)").format(r=rowexpr),
                [pred.field, pred.field, pred.prefix])
    if isinstance(pred, store.Exists):
        return sql.SQL("({r} ? %s)").format(r=rowexpr), [pred.field]
    if isinstance(pred, (store.And, store.Or)):
        if not pred.parts:
            return sql.SQL("true" if isinstance(pred, store.And) else "false"), []
        parts, params = [], []
        for p in pred.parts:
            q, a = compile_predicate(p, rowexpr)
            parts.append(sql.SQL("({})").format(q))
            params += a
        joiner = sql.SQL(" AND " if isinstance(pred, store.And) else " OR ")
        return joiner.join(parts), params
    if isinstance(pred, store.Not):
        q, a = compile_predicate(pred.part, rowexpr)
        return sql.SQL("NOT ({})").format(q), a
    raise StoreError(f"unsupported predicate {type(pred).__name__}")


# --- store ---------------------------------------------------------------------------------------

class PgStore:
    def __init__(self, root: Path = store.ROOT, *, dsn: str = DEFAULT_DSN, schema: str = "nekaise",
                 create: bool = True):
        if not schema.isidentifier():
            raise StoreError(f"invalid schema name {schema!r}")
        self.root = Path(root)
        self.dsn = dsn
        self.schema = schema
        self._s = sql.Identifier(schema)
        self._writers: dict[str, psycopg.Connection] = {}
        self._active = False
        if create:
            with self._connect(autocommit=True) as conn:
                conn.execute(DDL.format(s=schema, v=SCHEMA_VERSION))
                got = conn.execute(sql.SQL("SELECT schema_version FROM {}.state").format(
                    self._s)).fetchone()[0]
                if got < SCHEMA_VERSION:
                    # Migrate as the writer: an older-version replicator holds this lock while it
                    # writes, so it cannot interleave rows lacking the new derived columns; once
                    # migrated, older code refuses the newer schema at construction.
                    conn.execute("SELECT pg_advisory_lock(hashtext(%s))", [self._lock_name()])
                    got = conn.execute(sql.SQL("SELECT schema_version FROM {}.state").format(
                        self._s)).fetchone()[0]
                for version in range(got + 1, SCHEMA_VERSION + 1):
                    with conn.transaction():
                        MIGRATIONS[version](conn, schema)
                        conn.execute(sql.SQL("UPDATE {}.state SET schema_version = %s").format(
                            self._s), [version])
                    got = version
                conn.execute(POST_DDL.format(s=schema))
                conn.execute("SELECT pg_advisory_unlock_all()")
                if got != SCHEMA_VERSION:
                    raise StoreError(f"schema {schema} is version {got}, code expects "
                                     f"{SCHEMA_VERSION}")

    def _connect(self, *, autocommit: bool = False) -> psycopg.Connection:
        conn = psycopg.connect(self.dsn, autocommit=autocommit)
        conn.execute(sql.SQL("SET search_path TO {}").format(self._s))
        return conn

    def drop(self) -> None:
        """Delete the schema (tests only)."""
        for conn in list(self._writers.values()):
            conn.close()
        with self._connect(autocommit=True) as conn:
            conn.execute(sql.SQL("DROP SCHEMA IF EXISTS {} CASCADE").format(self._s))

    # -- configuration (git-owned, pinned per commit) --------------------------------------------

    def pin_config(self, documents: Mapping[str, bytes], conn: psycopg.Connection | None = None) -> None:
        """Replace the pinned configuration with these raw documents (name -> file bytes)."""
        def run(c):
            c.execute("DELETE FROM config")
            for name, data in sorted(documents.items()):
                text = _check_text(data.decode(), f"config {name}")
                json.loads(text)  # must be valid JSON
                c.execute("INSERT INTO config (name, doc_text, digest) VALUES (%s, %s, %s)",
                          [name, text, hashlib.sha256(data).hexdigest()])
        if conn is not None:
            run(conn)
        else:
            with self._connect() as c:
                run(c)

    def pin_config_from_files(self) -> None:
        docs = {}
        for name in store.CONFIG_FILES:
            path = self.root / "registry" / name
            if path.exists():
                docs[name] = path.read_bytes()
        self.pin_config(docs)

    @staticmethod
    def _config(conn: psycopg.Connection) -> ConfigSnapshot:
        documents, digests = {}, {}
        for name, text, digest in conn.execute("SELECT name, doc_text, digest FROM config"):
            documents[name] = json.loads(text)
            digests[name] = digest
        return ConfigSnapshot(documents, digests)

    # -- versions and writers ------------------------------------------------------------------

    def version(self) -> Version:
        with self._connect(autocommit=True) as conn:
            return Version(f"pg:{conn.execute('SELECT generation FROM state').fetchone()[0]}")

    def _lock_name(self) -> str:
        return f"nekaise-writer:{self.schema}"

    @contextmanager
    def writer(self, timeout: float = 0, *, round_id: str | None = None) -> Iterator[WriterToken]:
        if round_id is not None:
            store._check_run_id(round_id)
        conn = self._connect(autocommit=True)
        nonce = uuid.uuid4().hex
        try:
            deadline = None if timeout < 0 else time.monotonic() + timeout
            while not conn.execute("SELECT pg_try_advisory_lock(hashtext(%s))",
                                   [self._lock_name()]).fetchone()[0]:
                if deadline is not None and time.monotonic() >= deadline:
                    raise WriterError(f"writer lock for schema {self.schema} is held by another "
                                      "session")
                time.sleep(0.1)
            # A process constructed before a migration may only now get the lock: it must not
            # write with an outdated idea of the schema (derived columns it does not fill).
            version = conn.execute("SELECT schema_version FROM state").fetchone()[0]
            if version != SCHEMA_VERSION:
                raise WriterError(f"schema {self.schema} is version {version}; this code writes "
                                  f"version {SCHEMA_VERSION} — restart with matching code")
            epoch = conn.execute("UPDATE state SET writer_epoch = writer_epoch + 1 "
                                 "RETURNING writer_epoch").fetchone()[0]
            token = WriterToken("pg-lock", str(os.getpid()), epoch, f"pg:{self.schema}", nonce,
                                round_id)
            self._writers[nonce] = conn
            yield token
        finally:
            self._writers.pop(nonce, None)
            conn.close()  # ends the session: releases the advisory lock

    def _writer_conn(self, writer: WriterToken) -> psycopg.Connection:
        if not isinstance(writer, WriterToken) or writer.kind != "pg-lock" \
                or writer.lock_path != f"pg:{self.schema}":
            raise WriterError("PgStore requires a pg-lock writer token from PgStore.writer()")
        conn = self._writers.get(writer.nonce)
        if conn is None or conn.closed:
            raise WriterError("writer token is stale: its session has ended")
        try:
            epoch = conn.execute("SELECT writer_epoch FROM state").fetchone()[0]
        except psycopg.Error as exc:
            raise WriterError(f"writer session is broken: {exc}") from exc
        if epoch != writer.epoch:
            raise WriterError("writer token is stale: a newer writer took over")
        return conn

    # -- views ---------------------------------------------------------------------------------

    @contextmanager
    def read(self, *, timeout: float = 0, writer: WriterToken | None = None) -> Iterator["PgReadView"]:
        """A snapshot-consistent view (REPEATABLE READ, read only) on its own connection."""
        if writer is not None:
            self._writer_conn(writer)
        conn = self._connect()
        try:
            conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            gen = conn.execute("SELECT generation FROM state").fetchone()[0]
            view = PgReadView(self, conn, Version(f"pg:{gen}"), self._config(conn))
            try:
                yield view
            finally:
                view._closed = True
        finally:
            conn.rollback()
            conn.close()

    def pending_transactions(self) -> list:
        return []  # a committed-store transaction is atomic; interrupted rounds are tracked apart

    def recover(self, run_id: str, *, writer: WriterToken):
        store._check_run_id(run_id)
        self._writer_conn(writer)
        raise StoreError(f"no pending store transaction {run_id}")

    @contextmanager
    def transaction(self, run_id: str, *, expected_version: Version,
                    writer: WriterToken) -> Iterator["PgWriteView"]:
        store._check_run_id(run_id)
        if self._active:
            raise StoreError("transactions do not nest")
        conn = self._writer_conn(writer)
        self._active = True
        view = None
        try:
            with conn.transaction():
                gen, epoch = conn.execute(
                    "SELECT generation, writer_epoch FROM state FOR UPDATE").fetchone()
                if epoch != writer.epoch:
                    raise WriterError("writer token is stale: a newer writer took over")
                current = Version(f"pg:{gen}")
                row = conn.execute("SELECT row_text FROM events WHERE run_id = %s AND op = 'commit'",
                                   [run_id]).fetchone()
                committed = json.loads(row[0])["digest"] if row else None
                if committed is None and current != expected_version:
                    raise VersionConflict("store changed since the expected version was read")
                view = PgWriteView(self, conn, current, self._config(conn), run_id,
                                   replay=committed is not None)
                yield view
                view._closed = True
                digest = store._digest(view._requests)
                if committed is not None:
                    if committed != digest:
                        raise StoreError(f"run {run_id} already committed different requests")
                    return
                if view._requests:
                    self._journal(conn, view, digest)
                    conn.execute("UPDATE state SET generation = generation + 1")
        finally:
            if view is not None:
                view._closed = True
            self._active = False

    def _journal(self, conn, view: "PgWriteView", digest: str) -> None:
        seq = conn.execute("SELECT COALESCE(max(seq), 0) FROM events").fetchone()[0]
        at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        rows = []
        for n, op in enumerate(view._ops, 1):
            seq += 1
            rows.append({"seq": seq, "event_id": f"{view.run_id}:{n}", "run_id": view.run_id,
                         "at": at, **op})
        rows.append({"seq": seq + 1, "event_id": f"{view.run_id}:commit", "run_id": view.run_id,
                     "at": at, "table": None, "op": "commit", "id": None, "digest": digest})
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO events (seq, run_id, op, row_text) VALUES (%s,%s,%s,%s)",
                            [(r["seq"], r["run_id"], r["op"], _check_text(canonical_row(r), "event"))
                             for r in rows])

    def export(self, directory: Path, *, view: "PgReadView"):
        return store.export(directory, view=view)


# --- read view -----------------------------------------------------------------------------------

class PgReadView:
    def __init__(self, st: PgStore, conn: psycopg.Connection, version: Version,
                 config: ConfigSnapshot):
        self._store = st
        self._conn = conn
        self._version = version
        self._config = config
        self._id = uuid.uuid4().hex
        self._closed = False

    def _check_open(self) -> None:
        if self._closed:
            raise StaleView("view is closed")

    def _q(self, query, params=()):
        self._check_open()
        return self._conn.execute(query, params)

    def version(self) -> Version:
        self._check_open()
        return self._version

    def _cursor_scope(self) -> str:
        return self._id

    def _validate(self, table, where, fields) -> None:
        store.ReadView._validate(self, table, where, fields)

    # table -> (key columns, jsonb row expression, row-text expression)
    _SCAN = {
        Table.ENTRIES: (("id",), "row", "row_text", "entries"),
        Table.MANIFEST: (("id",), "row", "row_text", "manifest"),
        Table.BLOCKLIST: (("key",), "jsonb_build_object('url', url)", "NULL", "blocklist"),
        Table.LEDGER: (("key", "n"), "row", "row_text", "ledger"),
        Table.EVENTS: (("seq",), "(row_text::jsonb)", "row_text", "events"),
    }

    def scan(self, table: Table, *, where=None, fields: tuple[str, ...] | None = None,
             cursor: Cursor | None = None, limit: int = store.DEFAULT_PAGE,
             order: str = "key") -> Page:
        table = Table(table)
        self._validate(table, where, fields)
        store.check_order(table, order)
        if not 1 <= limit <= store.MAX_PAGE:
            raise StoreError(f"limit must be within 1..{store.MAX_PAGE}")
        query_id = store._digest([table.value, repr(where), list(fields) if fields else None,
                                  order])
        if cursor is not None and (cursor.view != self._cursor_scope() or cursor.query != query_id):
            raise StoreError("cursor belongs to a different view, generation or query")
        keys, rowexpr, textexpr, name = self._SCAN[table]
        if order == "legacy":
            keys = ("shard", "topic_key", "id")
        cond, params = compile_predicate(where, sql.SQL(rowexpr))
        keycols = sql.SQL(", ").join(sql.Identifier(k) for k in keys)
        after = sql.SQL("true")
        if cursor is not None:
            after = sql.SQL("({}) > ({})").format(
                keycols, sql.SQL(", ").join(sql.Placeholder() for _ in keys))
            params = params + list(cursor.last_key)
        extra = sql.SQL(", url") if table is Table.BLOCKLIST else sql.SQL("")
        q = sql.SQL("SELECT {keys}, {text}{extra} FROM {t} WHERE ({cond}) AND {after} "
                    "ORDER BY {keys} LIMIT %s").format(
            keys=keycols, text=sql.SQL(textexpr), extra=extra, t=sql.Identifier(name),
            cond=cond, after=after)
        got = self._q(q, params + [limit + 1]).fetchall()
        rows, last = [], None
        for rec in got[:limit]:
            key = tuple(rec[:len(keys)])
            row = {"url": rec[-1]} if table is Table.BLOCKLIST else json.loads(rec[len(keys)])
            rows.append({f: row[f] for f in fields if f in row} if fields else row)
            last = key
        nxt = Cursor(self._cursor_scope(), query_id, last) if len(got) > limit else None
        return Page(rows, nxt)

    def get_manifest(self, ids: Iterable[str]) -> dict[str, dict]:
        ids = list(dict.fromkeys(ids))
        found = dict(self._q("SELECT id, row_text FROM manifest WHERE id = ANY(%s)", [ids]).fetchall())
        return {i: json.loads(found[i]) for i in ids if i in found}

    def known(self, *, urls: Iterable[str] = (), titles: Iterable[str] = (),
              ids: Iterable[str] = (), include_blocklist: bool = True) -> KnownHits:
        self._check_open()
        cand_u = {u for u in map(norm_url, urls) if u}
        cand_t = {t for t in map(norm_title, titles) if t}
        cand_i = {i for i in ids if i}
        for label, values in (("urls", cand_u), ("titles", cand_t), ("ids", cand_i)):
            if len(values) > store.MAX_KNOWN:
                raise StoreError(f"known(): at most {store.MAX_KNOWN} {label} per call")
        ukeys = [_url_key(u) for u in cand_u]
        tkeys = [_url_key(t) for t in cand_t]
        hit_u = {r[0] for r in self._q(
            "SELECT url_norm FROM entries WHERE url_key = ANY(%s) "
            "UNION SELECT url_norm FROM manifest WHERE url_key = ANY(%s)", [ukeys, ukeys])}
        if include_blocklist:
            hit_u |= {r[0] for r in self._q("SELECT url FROM blocklist WHERE key = ANY(%s)",
                                            [[key_digest(u) for u in cand_u]])}
        hit_t = {r[0] for r in self._q(
            "SELECT title_norm FROM entries WHERE title_key = ANY(%s) "
            "UNION SELECT title_norm FROM manifest WHERE title_key = ANY(%s)", [tkeys, tkeys])}
        hit_i = {r[0] for r in self._q(
            "SELECT id FROM entries WHERE id = ANY(%s) UNION SELECT id FROM manifest "
            "WHERE id = ANY(%s)", [list(cand_i), list(cand_i)])}
        # digest hits are verified against the full value
        return KnownHits(frozenset(hit_u & cand_u), frozenset(hit_t & cand_t),
                         frozenset(hit_i & cand_i))

    def aggregate_manifest(self, *, group_by: tuple[str, ...], where=None,
                           sums: tuple[str, ...] = (), count: bool = True) -> Iterator[dict]:
        self._validate(Table.MANIFEST, where, tuple(group_by) + tuple(sums))
        cond, params = compile_predicate(where, sql.SQL("row"))
        gcols = [sql.SQL("COALESCE(row -> {}, 'null'::jsonb)").format(sql.Literal(f))
                 for f in group_by]
        scols = []
        for f in sums:
            j = sql.SQL("(row_text::json -> {})").format(sql.Literal(f))
            jt = sql.SQL("(row_text::json ->> {})").format(sql.Literal(f))
            scols += [sql.SQL("SUM(({jt})::numeric) FILTER (WHERE json_typeof({j}) = 'number')")
                      .format(j=j, jt=jt),
                      sql.SQL("COALESCE(bool_or(json_typeof({j}) = 'number' AND {jt} ~ '[.eE]'), "
                              "false)").format(j=j, jt=jt)]
        cols = gcols + [sql.SQL("count(*)")] + scols
        q = sql.SQL("SELECT {cols} FROM manifest WHERE {cond}{grp}").format(
            cols=sql.SQL(", ").join(cols), cond=cond,
            grp=sql.SQL(" GROUP BY {}").format(sql.SQL(", ").join(
                sql.SQL(str(i + 1)) for i in range(len(gcols)))) if gcols else sql.SQL(""))
        out = {}
        for rec in self._q(q, params).fetchall():
            if not gcols and rec[0] == 0:
                continue  # no matching rows: no group, like FileStore
            key = tuple(store.check_group_value(f, v) for f, v in zip(group_by, rec[:len(gcols)]))
            n = rec[len(gcols)]
            agg = {}
            for i, f in enumerate(sums):
                total, is_float = rec[len(gcols) + 1 + 2 * i], rec[len(gcols) + 2 + 2 * i]
                total = total if total is not None else 0
                agg[f"sum_{f}"] = float(total) if is_float else int(total)
            out[store._canonical(key)] = {**dict(zip(group_by, key)),
                                          **({"count": n} if count else {}), **agg}
        for k in sorted(out):
            yield out[k]

    def iter_duplicate_sha256(self, *, where=None,
                              batch_size: int = store.DEFAULT_PAGE) -> Iterator[dict]:
        self._validate(Table.MANIFEST, where, ("sha256",))
        if not 1 <= batch_size <= store.MAX_PAGE:
            raise StoreError(f"batch_size must be within 1..{store.MAX_PAGE}")
        cond, params = compile_predicate(where, sql.SQL("row"))
        q = sql.SQL("SELECT row_text FROM (SELECT id, sha256, row_text, count(*) OVER "
                    "(PARTITION BY sha256) AS c FROM manifest WHERE sha256 IS NOT NULL AND ({})) t "
                    "WHERE c > 1 ORDER BY sha256, id").format(cond)
        self._check_open()
        with self._conn.cursor(name=f"dup_{uuid.uuid4().hex}") as cur:
            cur.itersize = batch_size
            cur.execute(q, params)
            for (text,) in cur:
                yield json.loads(text)

    def rotation_get(self, name: str | None = None) -> dict:
        rows = {n: json.loads(t) for n, t in self._q("SELECT name, value_text FROM rotation")}
        return rows if name is None else rows[name]

    def control_get(self, name: str) -> dict | None:
        if name not in store.CONTROL_FILES:
            raise StoreError(f"unknown control document {name!r}")
        row = self._q("SELECT doc_text FROM control_docs WHERE name = %s", [name]).fetchone()
        return json.loads(row[0]) if row else None

    def config_get(self) -> ConfigSnapshot:
        self._check_open()
        return ConfigSnapshot(json.loads(json.dumps(self._config.documents)),
                              dict(self._config.digests))

    def backend_state_get(self, name: str | None = None):
        runtime = {n: BackendState(e, r) for n, e, r in
                   self._q("SELECT name, enabled, reason FROM backend_state")}
        names = [k for k in self._config.backends if not k.startswith("_")]
        states = {k: runtime.get(k, BackendState()) for k in sorted(set(names) | set(runtime))}
        return states if name is None else states.get(name, BackendState())

    def backend_enabled(self, name: str) -> bool:
        cfg = self._config.backends.get(name)
        if cfg is None or name.startswith("_"):
            raise StoreError(f"unknown backend {name}")
        return bool(cfg.get("enabled", True)) and self.backend_state_get(name).enabled

    def resolve_artifact(self, id: str, stage: Stage):  # noqa: A002
        return store.artifact_ref(self.get_manifest([id]).get(id), id, stage)


# --- write view -----------------------------------------------------------------------------------

def _savepoint(fn):
    """Run one mutation inside a savepoint: a caught error leaves that whole batch unapplied while
    the enclosing transaction continues (FileStore validates before mutating to the same end)."""
    def wrapper(self, *args, **kwargs):
        with self._conn.transaction():
            return fn(self, *args, **kwargs)
    wrapper.__name__ = fn.__name__
    wrapper.__doc__ = fn.__doc__
    return wrapper


def _mutation(fn):
    return store._mutation(_savepoint(fn))


class PgWriteView(PgReadView):
    def __init__(self, st, conn, version, config, run_id: str, *, replay: bool = False):
        super().__init__(st, conn, version, config)
        self.run_id = run_id
        self._replay = replay
        self._ops: list[dict] = []
        self._requests: list[dict] = []

    def _cursor_scope(self) -> str:
        return f"{self._id}:{len(self._ops)}"

    def _record(self, table, op, sid, before=None, after=None, reason=None) -> None:
        self._ops.append({"table": table, "op": op, "id": sid, "before": before, "after": after,
                          "reason": reason})

    def _rows(self, table: str, ids: list[str], lock: bool = True) -> dict[str, dict]:
        q = sql.SQL("SELECT id, row_text FROM {} WHERE id = ANY(%s){}").format(
            sql.Identifier(table), sql.SQL(" FOR UPDATE") if lock else sql.SQL(""))
        return {i: json.loads(t) for i, t in self._q(q, [ids]).fetchall()}

    def _put(self, table: str, rows: list[dict]) -> None:
        with self._conn.cursor() as cur:
            put_rows(cur, table, rows)

    def _delete(self, table: str, ids: Iterable[str], reason: str) -> int:
        ids = list(dict.fromkeys(ids))
        before = self._rows(table, ids)
        present = [i for i in ids if i in before]
        if present:
            self._q(sql.SQL("DELETE FROM {} WHERE id = ANY(%s)").format(sql.Identifier(table)),
                    [present])
        for i in present:
            self._record(table, "delete", i, before=before[i], reason=reason)
        return len(present)

    def uniquify_ids(self, entries: Sequence[Mapping]) -> list[dict]:
        return store.uniquify(entries, lambda i: bool(self.known(ids=[i]).ids))

    @_mutation
    def insert_entries(self, entries: Iterable[Mapping]) -> int:
        rows = [WriteView._entry_row(e) for e in entries]
        WriteView._unique_ids(rows, "insert_entries")
        clash = sorted(self._rows("entries", [r["id"] for r in rows], lock=False))
        if clash:
            raise StoreError(f"insert_entries: id(s) already exist: {', '.join(clash[:5])}")
        self._put("entries", rows)
        for r in rows:
            self._record("entries", "insert", r["id"], after=r)
        return len(rows)

    @_mutation
    def upsert_entries(self, entries: Iterable[Mapping]) -> int:
        rows = [WriteView._entry_row(e) for e in entries]
        WriteView._unique_ids(rows, "upsert_entries")
        before = self._rows("entries", [r["id"] for r in rows])
        changed = [r for r in rows if not store.same_row(before.get(r["id"]), r)]
        self._put("entries", changed)
        for r in changed:
            self._record("entries", "upsert", r["id"], before=before.get(r["id"]), after=r)
        return len(changed)

    @_mutation
    def delete_entries(self, ids: Iterable[str], *, reason: str) -> int:
        if not reason:
            raise StoreError("delete_entries requires a reason")
        return self._delete("entries", ids, reason)

    def _upsert_manifest(self, rows: list[dict]) -> int:
        WriteView._unique_ids(rows, "upsert_manifest")
        before = self._rows("manifest", [r["id"] for r in rows])
        changed = [r for r in rows if not store.same_row(before.get(r["id"]), r)]
        self._put("manifest", changed)
        for r in changed:
            self._record("manifest", "upsert", r["id"], before=before.get(r["id"]), after=r)
        return len(changed)

    @_mutation
    def upsert_manifest(self, rows: Iterable[Mapping]) -> int:
        rows = [dict(r) for r in rows]
        WriteView._unique_ids(rows, "upsert_manifest")
        return self._upsert_manifest([json.loads(canonical_row(r)) for r in rows])

    @_mutation
    def replace_manifest(self, rows: Iterable[Mapping], *, reason: str) -> int:
        if not reason:
            raise StoreError("replace_manifest requires a reason")
        rows = [dict(r) for r in rows]
        WriteView._unique_ids(rows, "replace_manifest")
        rows = [json.loads(canonical_row(r)) for r in rows]
        keep = [r["id"] for r in rows]
        gone = [i for (i,) in self._q("SELECT id FROM manifest WHERE NOT (id = ANY(%s)) ORDER BY id",
                                      [keep]).fetchall()]
        return self._delete("manifest", gone, reason) + self._upsert_manifest(rows)

    @_mutation
    def update_manifest_fields(self, updates: Mapping[str, Mapping[str, Any]], *,
                               unset: tuple[str, ...] = ()) -> int:
        before = self._rows("manifest", list(updates))
        store.validate_patch(updates, unset, lambda ids: [i for i in ids if i in before])
        changed = []
        for sid, patch in updates.items():
            after = {k: v for k, v in before[sid].items() if k not in unset}
            after.update(json.loads(json.dumps(dict(patch))))
            if not store.same_row(after, before[sid]):
                changed.append(after)
                self._record("manifest", "update", sid, before=before[sid], after=after)
        self._put("manifest", changed)
        return len(changed)

    @_mutation
    def delete_manifest(self, ids: Iterable[str], *, reason: str) -> int:
        if not reason:
            raise StoreError("delete_manifest requires a reason")
        return self._delete("manifest", ids, reason)

    @_mutation
    def blocklist_add(self, urls: Iterable[str]) -> int:
        cands = {u for u in map(norm_url, urls) if u}
        for u in cands:
            store.validate_json(u, "blocklist url")
        have = {r[0] for r in self._q("SELECT url FROM blocklist WHERE key = ANY(%s)",
                                      [[key_digest(u) for u in cands]])}
        new = sorted(cands - have)
        with self._conn.cursor() as cur:
            cur.executemany("INSERT INTO blocklist (key, url) VALUES (%s, %s)",
                            [(key_digest(u), _check_text(u, "blocklist url")) for u in new])
        for u in new:
            self._record("blocklist", "insert", u, after={"url": u})
        return len(new)

    @_mutation
    def ledger_append(self, rows: Iterable[Mapping]) -> int:
        rows = [dict(r) for r in rows]
        store.validate_ledger_rows(rows)
        rows = [json.loads(canonical_row(r)) for r in rows]
        next_n: dict[str, int] = {}
        params = []
        for r in rows:
            text = _check_text(canonical_row(r), "ledger row")
            key = key_digest(text)
            if key not in next_n:
                next_n[key] = self._q("SELECT COALESCE(max(n) + 1, 0) FROM ledger WHERE key = %s",
                                      [key]).fetchone()[0]
            params.append((key, next_n[key], text))
            next_n[key] += 1
        with self._conn.cursor() as cur:
            cur.executemany("INSERT INTO ledger (key, n, row_text) VALUES (%s, %s, %s)", params)
        for r in rows:
            self._record("ledger", "insert", r["id"], after=r)
        return len(rows)

    @_mutation
    def rotation_set(self, name: str, value: Mapping) -> None:
        if not isinstance(value, Mapping):
            raise StoreError("rotation value must be a mapping")
        store.validate_json(dict(value), f"rotation {name}")
        after = json.loads(canonical_row(dict(value)))
        row = self._q("SELECT value_text FROM rotation WHERE name = %s FOR UPDATE", [name]).fetchone()
        before = json.loads(row[0]) if row else None
        if store.same_row(before, after):
            return
        self._q("INSERT INTO rotation (name, value_text) VALUES (%s, %s) ON CONFLICT (name) "
                "DO UPDATE SET value_text = EXCLUDED.value_text", [name, canonical_row(after)])
        self._record("rotation", "upsert", name, before=before, after=after)

    @_mutation
    def control_set(self, name: str, doc: Mapping | None) -> None:
        if name not in store.CONTROL_FILES:
            raise StoreError(f"unknown control document {name!r}")
        if doc is not None:
            if not isinstance(doc, Mapping):
                raise StoreError("a control document must be a JSON object")
            store.validate_json(dict(doc), f"control {name}")
            doc = json.loads(canonical_row(dict(doc)))
        row = self._q("SELECT doc_text FROM control_docs WHERE name = %s FOR UPDATE",
                      [name]).fetchone()
        before = json.loads(row[0]) if row else None
        if store.same_row(before, doc):
            return
        if doc is None:
            self._q("DELETE FROM control_docs WHERE name = %s", [name])
        else:
            self._q("INSERT INTO control_docs (name, doc_text) VALUES (%s, %s) ON CONFLICT (name) "
                    "DO UPDATE SET doc_text = EXCLUDED.doc_text", [name, canonical_row(doc)])
        self._record("control", "upsert" if doc is not None else "delete", name, before=before,
                     after=doc)

    @_mutation
    def backend_state_set(self, name: str, value: BackendState) -> None:
        if name.startswith("_") or name not in self._config.backends:
            raise StoreError(f"unknown backend {name}")
        row = self._q("SELECT enabled, reason FROM backend_state WHERE name = %s FOR UPDATE",
                      [name]).fetchone()
        before = {"enabled": row[0], "reason": row[1]} if row else None
        after = {"enabled": bool(value.enabled), "reason": value.reason}
        if (before or {"enabled": True, "reason": None}) == after:
            return
        self._q("INSERT INTO backend_state (name, enabled, reason) VALUES (%s, %s, %s) "
                "ON CONFLICT (name) DO UPDATE SET enabled = EXCLUDED.enabled, "
                "reason = EXCLUDED.reason", [name, after["enabled"], after["reason"]])
        self._record("backend_state", "upsert", name, before=before, after=after)
