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

Stage 4, step 1 (schema v4): the dataset UUID and the database half of the authority record, and
the contract tables later steps stage and promote through (runs, batch receipts, immutable
revisions, generations, outbox with per-consumer acknowledgements, artifact identities); see
V4_DDL. A PgStore opened by store.open() is bound to the host record and re-checks the authority
inside every write transaction; one addressed directly (pg_shadow, tests) is not.

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
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Iterator, Mapping, Sequence

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

import store
from store import (BackendState, ConfigSnapshot, Cursor, KnownHits, Page, Stage, StaleView,
                   StoreError, Table, Version, VersionConflict, WriteView, WriterError,
                   WriterToken, canonical_row, key_digest, norm_title, norm_url)

SCHEMA_VERSION = 7
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


# ADR 0001 stage 4, step 1: authority and generation contracts. Created with a fresh schema or by
# migration 4, never re-run on every open (CREATE OR REPLACE TRIGGER locks its table). Adds tables
# only: no existing row, event or column is rewritten. The contracts live in the database
# (triggers), so no client — old, new or ad hoc SQL — can break them:
#   dataset            one row: dataset UUID, authority mode/epoch (the database half of the
#                      authority record), current promoted generation
#   authority_log      every authority epoch, append-only
#   config_blobs/_sets exact configuration bytes (content addressed) and the named sets pinned by
#                      runs and generations
#   runs               open -> frozen -> promoted | aborted; staged_seq = the run's staging sequence
#   batches            immutable computed request + digest per (run, step, batch); requested ->
#                      applied (with the staging seq it produced) | abandoned
#   revisions          immutable puts/tombstones keyed by stable identity (tbl, key)
#   generations        linear promoted chain with producer commit, config set, extractor version,
#                      cleaning ruleset, frozen sequence/digest; generation_retention pins
#   outbox (+consumers, acks)  gap-free per-generation references; independent watermarks
#   artifacts / artifact_locators  (stage, sha256) identity apart from where the bytes live
V4_DDL = r"""
CREATE OR REPLACE FUNCTION {s}.nk_refuse() RETURNS trigger LANGUAGE plpgsql AS $f$
BEGIN
    RAISE EXCEPTION 'nekaise: % on %.% is refused (immutable contract)', TG_OP, TG_TABLE_SCHEMA,
        TG_TABLE_NAME USING ERRCODE = 'integrity_constraint_violation';
END $f$;

CREATE TABLE IF NOT EXISTS {s}.dataset (
    one boolean PRIMARY KEY DEFAULT true CHECK (one),
    dataset_uuid uuid NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    authority_mode text NOT NULL DEFAULT 'file' CHECK (authority_mode IN ('file', 'postgres')),
    authority_epoch bigint NOT NULL DEFAULT 1 CHECK (authority_epoch >= 1),
    authority_root text,
    authority_changed_at timestamptz NOT NULL DEFAULT now(),
    current_generation bigint
);
INSERT INTO {s}.dataset (dataset_uuid) VALUES (gen_random_uuid()) ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS {s}.authority_log (
    epoch bigint PRIMARY KEY,
    mode text NOT NULL CHECK (mode IN ('file', 'postgres')),
    root text,
    changed_at timestamptz NOT NULL DEFAULT now(),
    reason text NOT NULL CHECK (reason <> '')
);
INSERT INTO {s}.authority_log (epoch, mode, reason)
    SELECT authority_epoch, authority_mode, 'schema v4: FileStore authoritative; this schema is '
           'not a production writer' FROM {s}.dataset ON CONFLICT DO NOTHING;
CREATE OR REPLACE TRIGGER authority_log_immutable BEFORE UPDATE OR DELETE ON {s}.authority_log
    FOR EACH ROW EXECUTE FUNCTION {s}.nk_refuse();

CREATE TABLE IF NOT EXISTS {s}.config_blobs (
    sha256 text COLLATE "C" PRIMARY KEY,
    bytes bytea NOT NULL,
    CHECK (sha256 = encode(sha256(bytes), 'hex'))
);
-- A config set is sealed when created: members_text is the canonical JSON {{name: sha256}}
-- (store._canonical: sorted keys, no spaces) and digest = sha256(members_text), both checked
-- here; its member rows must be exactly those names (each insert checked against members_text,
-- completeness checked at commit by a deferred constraint trigger), so no member can be added
-- to a set later, and nothing can be updated or deleted.
CREATE TABLE IF NOT EXISTS {s}.config_sets (
    digest text COLLATE "C" PRIMARY KEY CHECK (digest ~ '^[0-9a-f]{{64}}$'),
    members_text text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CHECK (digest = encode(sha256(convert_to(members_text, 'UTF8')), 'hex'))
);
CREATE TABLE IF NOT EXISTS {s}.config_set_members (
    digest text COLLATE "C" NOT NULL REFERENCES {s}.config_sets,
    name text COLLATE "C" NOT NULL CHECK (name ~ '^[A-Za-z0-9][A-Za-z0-9._-]{{0,127}}$'),
    sha256 text COLLATE "C" NOT NULL REFERENCES {s}.config_blobs,
    PRIMARY KEY (digest, name)
);
CREATE OR REPLACE TRIGGER config_blobs_immutable BEFORE UPDATE OR DELETE ON {s}.config_blobs
    FOR EACH ROW EXECUTE FUNCTION {s}.nk_refuse();
CREATE OR REPLACE FUNCTION {s}.nk_config_sets_guard() RETURNS trigger LANGUAGE plpgsql AS $f$
DECLARE doc jsonb; canon text;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'nekaise: config set % is sealed', OLD.digest
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    BEGIN
        doc := NEW.members_text::jsonb;
    EXCEPTION WHEN others THEN
        RAISE EXCEPTION 'nekaise: config set members are not JSON'
            USING ERRCODE = 'integrity_constraint_violation';
    END;
    IF jsonb_typeof(doc) <> 'object' OR EXISTS (
            SELECT 1 FROM jsonb_each(doc) e WHERE jsonb_typeof(e.value) <> 'string'
            OR NOT (e.value #>> '{{}}') ~ '^[0-9a-f]{{64}}$'
            OR NOT e.key ~ '^[A-Za-z0-9][A-Za-z0-9._-]{{0,127}}$') THEN
        RAISE EXCEPTION 'nekaise: config set members must map names to sha256 digests'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    SELECT '{{' || COALESCE(string_agg(to_json(e.key)::text || ':'
                                      || to_json(e.value #>> '{{}}')::text,
                                      ',' ORDER BY e.key COLLATE "C"), '') || '}}'
        INTO canon FROM jsonb_each(doc) e;
    IF canon <> NEW.members_text THEN
        RAISE EXCEPTION 'nekaise: config set members_text is not canonical JSON'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END $f$;
CREATE OR REPLACE TRIGGER config_sets_guard BEFORE INSERT OR UPDATE OR DELETE ON {s}.config_sets
    FOR EACH ROW EXECUTE FUNCTION {s}.nk_config_sets_guard();
CREATE OR REPLACE FUNCTION {s}.nk_config_members_guard() RETURNS trigger LANGUAGE plpgsql AS $f$
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'nekaise: config set % is sealed', OLD.digest
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM {s}.config_sets c WHERE c.digest = NEW.digest
                   AND (c.members_text::jsonb ->> NEW.name) = NEW.sha256) THEN
        RAISE EXCEPTION 'nekaise: % is not a member of sealed config set %', NEW.name, NEW.digest
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END $f$;
CREATE OR REPLACE TRIGGER config_set_members_guard BEFORE INSERT OR UPDATE OR DELETE
    ON {s}.config_set_members FOR EACH ROW EXECUTE FUNCTION {s}.nk_config_members_guard();
CREATE OR REPLACE FUNCTION {s}.nk_config_set_complete() RETURNS trigger LANGUAGE plpgsql AS $f$
BEGIN
    IF (SELECT count(*) FROM jsonb_object_keys(NEW.members_text::jsonb)) <>
       (SELECT count(*) FROM {s}.config_set_members m WHERE m.digest = NEW.digest) THEN
        RAISE EXCEPTION 'nekaise: config set % was not created with all its members', NEW.digest
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NULL;
END $f$;
DO $d$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'config_sets_complete'
                   AND tgrelid = '{s}.config_sets'::regclass) THEN
        CREATE CONSTRAINT TRIGGER config_sets_complete AFTER INSERT ON {s}.config_sets
            DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
            EXECUTE FUNCTION {s}.nk_config_set_complete();
    END IF;
END $d$;

CREATE TABLE IF NOT EXISTS {s}.runs (
    run_id text COLLATE "C" PRIMARY KEY CHECK (run_id ~ '^[A-Za-z0-9][A-Za-z0-9._-]{{0,127}}$'),
    kind text NOT NULL CHECK (kind IN ('round', 'maintenance', 'standalone', 'baseline')),
    parent_generation bigint,
    status text NOT NULL DEFAULT 'open'
        CHECK (status IN ('open', 'frozen', 'promoted', 'aborted')),
    authority_epoch bigint NOT NULL,
    writer_epoch bigint NOT NULL,
    producer_commit text COLLATE "C" NOT NULL CHECK (producer_commit ~ '^[0-9a-f]{{40,64}}$'),
    config_digest text COLLATE "C" NOT NULL REFERENCES {s}.config_sets,
    extractor_version text NOT NULL,
    cleaning_ruleset text NOT NULL,
    started_at timestamptz NOT NULL DEFAULT now(),
    staged_seq int NOT NULL DEFAULT 0 CHECK (staged_seq >= 0),
    frozen_seq int,
    frozen_digest text COLLATE "C",
    ended_at timestamptz,
    promoted_generation bigint UNIQUE,
    detail_text text NOT NULL DEFAULT '{{}}'
);
CREATE INDEX IF NOT EXISTS runs_unfinished ON {s}.runs (status) WHERE status IN ('open', 'frozen');
CREATE OR REPLACE FUNCTION {s}.nk_runs_guard() RETURNS trigger LANGUAGE plpgsql AS $f$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'nekaise: runs are never deleted (run %)', OLD.run_id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'open' OR NEW.staged_seq <> 0 OR NEW.frozen_seq IS NOT NULL
                OR NEW.frozen_digest IS NOT NULL OR NEW.promoted_generation IS NOT NULL
                OR NEW.ended_at IS NOT NULL THEN
            RAISE EXCEPTION 'nekaise: a run starts open and empty (run %)', NEW.run_id
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.status IN ('promoted', 'aborted') THEN
        RAISE EXCEPTION 'nekaise: run % is %, final', OLD.run_id, OLD.status
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF (NEW.run_id, NEW.kind, NEW.parent_generation, NEW.authority_epoch, NEW.writer_epoch,
        NEW.producer_commit, NEW.config_digest, NEW.extractor_version, NEW.cleaning_ruleset,
        NEW.started_at) IS DISTINCT FROM
       (OLD.run_id, OLD.kind, OLD.parent_generation, OLD.authority_epoch, OLD.writer_epoch,
        OLD.producer_commit, OLD.config_digest, OLD.extractor_version, OLD.cleaning_ruleset,
        OLD.started_at) THEN
        RAISE EXCEPTION 'nekaise: run % identity is immutable', OLD.run_id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.staged_seq < OLD.staged_seq
            OR (OLD.status <> 'open' AND NEW.staged_seq <> OLD.staged_seq) THEN
        RAISE EXCEPTION 'nekaise: run % staging sequence only grows while open', OLD.run_id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.status <> OLD.status AND (OLD.status, NEW.status) NOT IN
            (('open', 'frozen'), ('open', 'aborted'), ('frozen', 'promoted'), ('frozen', 'aborted'))
    THEN
        RAISE EXCEPTION 'nekaise: run % cannot go from % to %', OLD.run_id, OLD.status, NEW.status
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.status = 'open' AND (NEW.frozen_seq IS NOT NULL OR NEW.frozen_digest IS NOT NULL) THEN
        RAISE EXCEPTION 'nekaise: open run % has no frozen sequence', OLD.run_id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.status = 'open' AND NEW.status = 'frozen' AND (NEW.frozen_seq IS DISTINCT FROM
            NEW.staged_seq OR NEW.frozen_digest IS NULL) THEN
        RAISE EXCEPTION 'nekaise: run % must freeze at its staged sequence with a digest',
            OLD.run_id USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.status = 'frozen' AND (NEW.frozen_seq, NEW.frozen_digest) IS DISTINCT FROM
            (OLD.frozen_seq, OLD.frozen_digest) THEN
        RAISE EXCEPTION 'nekaise: run % frozen sequence is immutable', OLD.run_id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF (NEW.status = 'promoted') <> (NEW.promoted_generation IS NOT NULL)
            OR (NEW.status = 'promoted' AND NOT EXISTS (
                SELECT 1 FROM {s}.generations g WHERE g.generation = NEW.promoted_generation
                AND g.run_id = NEW.run_id)) THEN
        RAISE EXCEPTION 'nekaise: run % is promoted exactly when its generation exists',
            OLD.run_id USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END $f$;
CREATE OR REPLACE TRIGGER runs_guard BEFORE INSERT OR UPDATE OR DELETE ON {s}.runs
    FOR EACH ROW EXECUTE FUNCTION {s}.nk_runs_guard();

CREATE TABLE IF NOT EXISTS {s}.batches (
    run_id text COLLATE "C" NOT NULL REFERENCES {s}.runs,
    step text COLLATE "C" NOT NULL CHECK (step ~ '^[A-Za-z0-9][A-Za-z0-9._-]{{0,127}}$'),
    batch text COLLATE "C" NOT NULL CHECK (batch ~ '^[A-Za-z0-9][A-Za-z0-9._-]{{0,127}}$'),
    request_digest text COLLATE "C" NOT NULL CHECK (request_digest ~ '^[0-9a-f]{{64}}$'),
    request_text text NOT NULL,
    status text NOT NULL DEFAULT 'requested'
        CHECK (status IN ('requested', 'applied', 'abandoned')),
    requested_at timestamptz NOT NULL DEFAULT now(),
    applied_at timestamptz,
    seq int CHECK (seq >= 1),
    counts_text text,
    PRIMARY KEY (run_id, step, batch),
    UNIQUE (run_id, seq),
    CHECK ((status = 'applied') = (seq IS NOT NULL AND applied_at IS NOT NULL))
);
CREATE OR REPLACE FUNCTION {s}.nk_batches_guard() RETURNS trigger LANGUAGE plpgsql AS $f$
DECLARE run_status text;
BEGIN
    IF TG_OP = 'DELETE' THEN
        SELECT status INTO run_status FROM {s}.runs WHERE run_id = OLD.run_id;
        IF run_status = 'aborted' THEN RETURN OLD; END IF;
        RAISE EXCEPTION 'nekaise: batch receipts of run % (%) are retained', OLD.run_id,
            run_status USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    SELECT status INTO run_status FROM {s}.runs WHERE run_id = NEW.run_id;
    IF TG_OP = 'INSERT' THEN
        IF run_status IS DISTINCT FROM 'open' OR NEW.status <> 'requested' THEN
            RAISE EXCEPTION 'nekaise: batch %.%.% must be requested in an open run', NEW.run_id,
                NEW.step, NEW.batch USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF (NEW.run_id, NEW.step, NEW.batch, NEW.request_digest, NEW.request_text, NEW.requested_at)
            IS DISTINCT FROM
       (OLD.run_id, OLD.step, OLD.batch, OLD.request_digest, OLD.request_text, OLD.requested_at)
    THEN
        RAISE EXCEPTION 'nekaise: batch request %.%.% is immutable', OLD.run_id, OLD.step,
            OLD.batch USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.status <> 'requested' THEN
        RAISE EXCEPTION 'nekaise: batch %.%.% is %, final', OLD.run_id, OLD.step, OLD.batch,
            OLD.status USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.status = 'applied' AND run_status IS DISTINCT FROM 'open' THEN
        RAISE EXCEPTION 'nekaise: batch %.%.% can only be applied in an open run', OLD.run_id,
            OLD.step, OLD.batch USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END $f$;
CREATE OR REPLACE TRIGGER batches_guard BEFORE INSERT OR UPDATE OR DELETE ON {s}.batches
    FOR EACH ROW EXECUTE FUNCTION {s}.nk_batches_guard();

CREATE TABLE IF NOT EXISTS {s}.revisions (
    rev_id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    run_id text COLLATE "C" NOT NULL,
    batch_seq int NOT NULL,
    tbl text COLLATE "C" NOT NULL CHECK (tbl IN ('entries', 'manifest', 'blocklist', 'ledger',
                                                 'rotation', 'control', 'backend_state')),
    key text COLLATE "C" NOT NULL,
    op text NOT NULL CHECK (op IN ('put', 'tombstone')),
    row_text text,
    row_sha256 text COLLATE "C",
    before_sha256 text COLLATE "C",
    reason text,
    FOREIGN KEY (run_id, batch_seq) REFERENCES {s}.batches (run_id, seq),
    UNIQUE (run_id, tbl, key, batch_seq),
    CHECK ((op = 'put') = (row_text IS NOT NULL AND row_sha256 IS NOT NULL)),
    CHECK (op = 'put' OR (reason IS NOT NULL AND reason <> ''))
);
CREATE INDEX IF NOT EXISTS revisions_history ON {s}.revisions (tbl, key, rev_id);
CREATE OR REPLACE FUNCTION {s}.nk_revisions_guard() RETURNS trigger LANGUAGE plpgsql AS $f$
DECLARE run_status text;
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'nekaise: revision % is immutable', OLD.rev_id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF TG_OP = 'DELETE' THEN
        SELECT status INTO run_status FROM {s}.runs WHERE run_id = OLD.run_id;
        IF run_status = 'aborted' THEN RETURN OLD; END IF;
        RAISE EXCEPTION 'nekaise: revisions of run % (%) are retained', OLD.run_id, run_status
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    SELECT status INTO run_status FROM {s}.runs WHERE run_id = NEW.run_id;
    IF run_status IS DISTINCT FROM 'open' THEN
        RAISE EXCEPTION 'nekaise: run % is %, it stages no revisions', NEW.run_id, run_status
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.op = 'put' AND NEW.row_sha256 <> encode(sha256(convert_to(NEW.row_text, 'UTF8')), 'hex')
    THEN
        RAISE EXCEPTION 'nekaise: revision row_sha256 does not match its row text'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END $f$;
CREATE OR REPLACE TRIGGER revisions_guard BEFORE INSERT OR UPDATE OR DELETE ON {s}.revisions
    FOR EACH ROW EXECUTE FUNCTION {s}.nk_revisions_guard();

CREATE TABLE IF NOT EXISTS {s}.generations (
    generation bigint PRIMARY KEY CHECK (generation >= 0),
    parent bigint UNIQUE REFERENCES {s}.generations,
    run_id text COLLATE "C" NOT NULL UNIQUE REFERENCES {s}.runs,
    promoted_at timestamptz NOT NULL DEFAULT now(),
    producer_commit text COLLATE "C" NOT NULL,
    config_digest text COLLATE "C" NOT NULL REFERENCES {s}.config_sets,
    extractor_version text NOT NULL,
    cleaning_ruleset text NOT NULL,
    frozen_seq int NOT NULL,
    frozen_digest text COLLATE "C" NOT NULL,
    counts_text text NOT NULL,
    CHECK ((parent IS NULL) = (generation = 0)),
    CHECK (parent IS NULL OR parent = generation - 1)
);
CREATE OR REPLACE FUNCTION {s}.nk_generations_guard() RETURNS trigger LANGUAGE plpgsql AS $f$
DECLARE r record; head bigint;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'nekaise: generation % is immutable', OLD.generation
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    SELECT * INTO r FROM {s}.runs WHERE run_id = NEW.run_id;
    SELECT current_generation INTO head FROM {s}.dataset;
    IF r.status IS DISTINCT FROM 'frozen'
            OR NEW.parent IS DISTINCT FROM head
            OR r.parent_generation IS DISTINCT FROM head
            OR (NEW.producer_commit, NEW.config_digest, NEW.extractor_version,
                NEW.cleaning_ruleset, NEW.frozen_seq, NEW.frozen_digest) IS DISTINCT FROM
               (r.producer_commit, r.config_digest, r.extractor_version, r.cleaning_ruleset,
                r.frozen_seq, r.frozen_digest) THEN
        RAISE EXCEPTION 'nekaise: generation % must promote a frozen run staged on the current '
            'generation % with that run''s provenance', NEW.generation, head
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END $f$;
CREATE OR REPLACE TRIGGER generations_guard BEFORE INSERT OR UPDATE OR DELETE ON {s}.generations
    FOR EACH ROW EXECUTE FUNCTION {s}.nk_generations_guard();
CREATE TABLE IF NOT EXISTS {s}.generation_retention (
    generation bigint NOT NULL REFERENCES {s}.generations,
    holder text COLLATE "C" NOT NULL,
    reason text NOT NULL CHECK (reason <> ''),
    pinned_at timestamptz NOT NULL DEFAULT now(),
    until timestamptz,
    PRIMARY KEY (generation, holder)
);

CREATE OR REPLACE FUNCTION {s}.nk_dataset_guard() RETURNS trigger LANGUAGE plpgsql AS $f$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'nekaise: the dataset row is never deleted'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.dataset_uuid <> OLD.dataset_uuid OR NEW.created_at <> OLD.created_at THEN
        RAISE EXCEPTION 'nekaise: the dataset identity is immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.authority_epoch < OLD.authority_epoch
            OR ((NEW.authority_mode, NEW.authority_root) IS DISTINCT FROM
                (OLD.authority_mode, OLD.authority_root)
                AND NEW.authority_epoch = OLD.authority_epoch)
            OR (NEW.authority_epoch <> OLD.authority_epoch AND NOT EXISTS (
                SELECT 1 FROM {s}.authority_log l WHERE l.epoch = NEW.authority_epoch
                AND l.mode = NEW.authority_mode AND l.root IS NOT DISTINCT FROM NEW.authority_root))
    THEN
        RAISE EXCEPTION 'nekaise: an authority change needs a new, logged epoch'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.current_generation IS DISTINCT FROM OLD.current_generation AND NOT EXISTS (
            SELECT 1 FROM {s}.generations g WHERE g.generation = NEW.current_generation
            AND g.parent IS NOT DISTINCT FROM OLD.current_generation) THEN
        RAISE EXCEPTION 'nekaise: the current generation only advances to its promoted child'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END $f$;
CREATE OR REPLACE TRIGGER dataset_guard BEFORE UPDATE OR DELETE ON {s}.dataset
    FOR EACH ROW EXECUTE FUNCTION {s}.nk_dataset_guard();

-- Outbox sequence allocation is durable and independent of retained rows: `allocated` is the
-- highest sequence ever issued (a new row takes allocated + 1), `compacted` the prefix removed
-- (rows are compacted lowest first, only once every consumer is past them). So a sequence is
-- never reused, even after every row was compacted.
CREATE TABLE IF NOT EXISTS {s}.outbox_state (
    one boolean PRIMARY KEY DEFAULT true CHECK (one),
    allocated bigint NOT NULL DEFAULT 0 CHECK (allocated >= 0),
    compacted bigint NOT NULL DEFAULT 0 CHECK (compacted >= 0 AND compacted <= allocated),
    -- bumped by every consumer registration: registration and compaction both WRITE this row,
    -- so under any isolation level one of two concurrent ones waits and then either re-reads
    -- the other's effect (READ COMMITTED) or fails with a serialization error (REPEATABLE READ,
    -- SERIALIZABLE) — a stale snapshot can never act on the row
    consumers_registered bigint NOT NULL DEFAULT 0 CHECK (consumers_registered >= 0)
);
INSERT INTO {s}.outbox_state DEFAULT VALUES ON CONFLICT DO NOTHING;
CREATE OR REPLACE FUNCTION {s}.nk_outbox_state_guard() RETURNS trigger LANGUAGE plpgsql AS $f$
BEGIN
    IF TG_OP = 'DELETE' OR NEW.allocated < OLD.allocated OR NEW.compacted < OLD.compacted
            OR NEW.consumers_registered < OLD.consumers_registered THEN
        RAISE EXCEPTION 'nekaise: the outbox allocation and compaction marks only grow'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF pg_trigger_depth() < 2 THEN  -- only the outbox's own trigger moves them
        RAISE EXCEPTION 'nekaise: outbox_state is maintained by the outbox triggers'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END $f$;
CREATE OR REPLACE TRIGGER outbox_state_guard BEFORE UPDATE OR DELETE ON {s}.outbox_state
    FOR EACH ROW EXECUTE FUNCTION {s}.nk_outbox_state_guard();
CREATE TABLE IF NOT EXISTS {s}.outbox (
    seq bigint PRIMARY KEY CHECK (seq >= 1),
    generation bigint NOT NULL UNIQUE REFERENCES {s}.generations,
    created_at timestamptz NOT NULL DEFAULT now(),
    payload_text text NOT NULL
);
CREATE TABLE IF NOT EXISTS {s}.outbox_consumers (
    consumer text COLLATE "C" PRIMARY KEY CHECK (consumer ~ '^[a-z][a-z0-9_-]{{0,63}}$'),
    watermark bigint NOT NULL DEFAULT 0 CHECK (watermark >= 0),
    updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS {s}.outbox_acks (
    consumer text COLLATE "C" NOT NULL REFERENCES {s}.outbox_consumers,
    seq bigint NOT NULL REFERENCES {s}.outbox ON DELETE CASCADE,
    acked_at timestamptz NOT NULL DEFAULT now(),
    verdict text NOT NULL CHECK (verdict IN ('ok', 'finding', 'integrity')),
    detail_text text NOT NULL DEFAULT '{{}}',
    PRIMARY KEY (consumer, seq)
);
CREATE OR REPLACE FUNCTION {s}.nk_acks_guard() RETURNS trigger LANGUAGE plpgsql AS $f$
BEGIN
    IF TG_OP = 'DELETE' AND NOT EXISTS (SELECT 1 FROM {s}.outbox_consumers
                                        WHERE watermark < OLD.seq) THEN
        RETURN OLD;  -- compacted together with its outbox row
    END IF;
    RAISE EXCEPTION 'nekaise: acknowledgement %/% is immutable', OLD.consumer, OLD.seq
        USING ERRCODE = 'integrity_constraint_violation';
END $f$;
CREATE OR REPLACE TRIGGER outbox_acks_guard BEFORE UPDATE OR DELETE ON {s}.outbox_acks
    FOR EACH ROW EXECUTE FUNCTION {s}.nk_acks_guard();
CREATE OR REPLACE FUNCTION {s}.nk_outbox_guard() RETURNS trigger LANGUAGE plpgsql AS $f$
DECLARE st record;
BEGIN
    -- the state row lock serializes allocation, compaction and consumer registration
    SELECT * INTO st FROM {s}.outbox_state FOR UPDATE;
    IF TG_OP = 'INSERT' THEN
        IF NEW.seq <> st.allocated + 1 THEN
            RAISE EXCEPTION 'nekaise: the next outbox sequence is % (got %)', st.allocated + 1,
                NEW.seq USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        -- `allocated` advances in the AFTER trigger: only for a row actually inserted (a
        -- BEFORE trigger also runs for an INSERT ... ON CONFLICT DO NOTHING that is skipped)
        RETURN NEW;
    END IF;
    IF TG_OP = 'DELETE' AND OLD.seq = st.compacted + 1
            AND NOT EXISTS (SELECT 1 FROM {s}.outbox_consumers WHERE watermark < OLD.seq) THEN
        UPDATE {s}.outbox_state SET compacted = OLD.seq;
        RETURN OLD;  -- the lowest row, acknowledged by every consumer: compaction removes it
    END IF;
    RAISE EXCEPTION 'nekaise: outbox row % is retained (% refused)', OLD.seq, TG_OP
        USING ERRCODE = 'integrity_constraint_violation';
END $f$;
CREATE OR REPLACE TRIGGER outbox_guard BEFORE INSERT OR UPDATE OR DELETE ON {s}.outbox
    FOR EACH ROW EXECUTE FUNCTION {s}.nk_outbox_guard();
CREATE OR REPLACE FUNCTION {s}.nk_outbox_allocated() RETURNS trigger LANGUAGE plpgsql AS $f$
BEGIN
    UPDATE {s}.outbox_state SET allocated = NEW.seq WHERE allocated = NEW.seq - 1;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'nekaise: outbox sequence % is not the next allocation', NEW.seq
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NULL;
END $f$;
CREATE OR REPLACE TRIGGER outbox_allocated AFTER INSERT ON {s}.outbox
    FOR EACH ROW EXECUTE FUNCTION {s}.nk_outbox_allocated();
CREATE OR REPLACE FUNCTION {s}.nk_consumers_guard() RETURNS trigger LANGUAGE plpgsql AS $f$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'nekaise: outbox consumer % is retained', OLD.consumer
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF TG_OP = 'INSERT' THEN
        -- UPDATE the state row compaction also updates (see outbox_state): the two serialize
        -- in the database whatever the isolation level; a stale snapshot fails instead
        UPDATE {s}.outbox_state SET consumers_registered = consumers_registered + 1;
        IF NEW.watermark <> 0 OR (SELECT compacted FROM {s}.outbox_state) > 0 THEN
            RAISE EXCEPTION 'nekaise: a new consumer starts at watermark 0, before any '
                'compaction (it would miss compacted history)'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.consumer <> OLD.consumer OR NEW.watermark < OLD.watermark
            OR NEW.watermark > (SELECT allocated FROM {s}.outbox_state)
            OR EXISTS (SELECT 1 FROM {s}.outbox o WHERE o.seq > OLD.watermark
                       AND o.seq <= NEW.watermark AND NOT EXISTS (
                           SELECT 1 FROM {s}.outbox_acks a WHERE a.consumer = OLD.consumer
                           AND a.seq = o.seq)) THEN
        RAISE EXCEPTION 'nekaise: the watermark of % only advances over acknowledged rows',
            OLD.consumer USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END $f$;
CREATE OR REPLACE TRIGGER outbox_consumers_guard BEFORE INSERT OR UPDATE OR DELETE
    ON {s}.outbox_consumers FOR EACH ROW EXECUTE FUNCTION {s}.nk_consumers_guard();
INSERT INTO {s}.outbox_consumers (consumer) VALUES ('review'), ('publication'), ('index')
    ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS {s}.artifacts (
    stage text COLLATE "C" NOT NULL CHECK (stage IN ('raw', 'text', 'corpus')),
    sha256 text COLLATE "C" NOT NULL CHECK (sha256 ~ '^[0-9a-f]{{64}}$'),
    size bigint NOT NULL CHECK (size >= 0),
    first_run text COLLATE "C",
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (stage, sha256)
);
CREATE OR REPLACE TRIGGER artifacts_immutable BEFORE UPDATE OR DELETE ON {s}.artifacts
    FOR EACH ROW EXECUTE FUNCTION {s}.nk_refuse();
CREATE TABLE IF NOT EXISTS {s}.artifact_locators (
    stage text COLLATE "C" NOT NULL,
    sha256 text COLLATE "C" NOT NULL,
    locator text COLLATE "C" NOT NULL,
    kind text NOT NULL CHECK (kind IN ('local', 'pack', 'object')),
    pack_offset bigint,
    pack_length bigint,
    codec text,
    created_at timestamptz NOT NULL DEFAULT now(),
    verified_at timestamptz,
    PRIMARY KEY (stage, sha256, locator),
    FOREIGN KEY (stage, sha256) REFERENCES {s}.artifacts,
    CHECK ((kind = 'pack') = (pack_offset IS NOT NULL AND pack_length IS NOT NULL))
);
"""
# Every table V4_DDL creates (tests and pg_shadow's emptiness check use it).
V4_TABLES = ("dataset", "authority_log", "config_blobs", "config_sets", "config_set_members",
             "runs", "batches", "revisions", "generations", "generation_retention",
             "outbox_state", "outbox", "outbox_consumers", "outbox_acks", "artifacts",
             "artifact_locators")


def _migrate_4(conn, schema):  # stage 4 step 1 contracts: new tables only, nothing rewritten
    # V4_DDL runs once per schema (here or at creation), not on every open. Once any schema has
    # reached version 4 — i.e. after this step is deployed — a revised trigger, function or
    # column must ship as a NEW migration (version 5, ...) that applies the change; editing
    # V4_DDL alone would not reach existing v4 schemas.
    conn.execute(V4_DDL.format(s=schema))


# ADR 0001 stage 4, step 2: run-scoped staging with constant-size promotion. Created with a fresh
# schema or by migration 5 (never re-run on open; a later revision ships as migration 6, ...).
# Additive: new columns (nullable or defaulted), new tables, new indexes, and replaced trigger
# FUNCTIONS for the step-1 tables — no projection row, event or receipt is rewritten.
#
# Every cross-row rule below is protected by an UPDATE of one shared row, never by a read or a
# lock alone: two concurrent operations then conflict on that row under ANY isolation level
# (READ COMMITTED re-reads the committed effect in the trigger's fresh statement snapshot;
# REPEATABLE READ / SERIALIZABLE fail with a serialization error). The shared rows are the run
# (`runs`: batch requests, applies, freezing, gate receipts, promotion) and `projection_state`
# (folding and retention pins). Side effects live in AFTER row triggers, which fire only for rows
# actually written (a conflict-skipped INSERT ... ON CONFLICT DO NOTHING has none).
#
#   runs            + batches_open (requested, not yet applied/abandoned), gate_receipts,
#                     required_gates (canonical JSON array, fixed when frozen). staged_seq and the
#                     counters move only through the batch/receipt triggers.
#   batches         + basis_seq (the staging sequence the request was computed at), sealed,
#                     revision_count, revisions_digest and chain_digest (computed by the database
#                     when the batch is sealed; a committed applied batch is always sealed).
#                     requested -> applied (seq = basis_seq + 1) -> sealed | requested -> abandoned
#   revisions       + the derived lookup/order columns of entries/manifest rows (overlay reads).
#                     Only the transaction that applies a batch can write its revisions (the batch
#                     is applied and unsealed only inside it); after sealing they are immutable.
#   gate_receipts   one verdict per (run, gate), bound to the run's frozen sequence and digest
#   run_access      sha256 of the tokens that authorize pipeline children to read a run's overlay
#   projection_state  the generation the projection tables materialize (NULL: the pre-generation
#                     base) and the fold in progress; retention pins hold the fold back
V5_DDL = r"""
-- the run counters, backfilled for unfinished step-1 runs under the step-1 guard (still installed
-- at this point, and it allows that); only when the columns are new, so re-running is a no-op
DO $d$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = '{s}'
                   AND table_name = 'runs' AND column_name = 'batches_open') THEN
        ALTER TABLE {s}.runs
            ADD COLUMN batches_open int NOT NULL DEFAULT 0 CHECK (batches_open >= 0),
            ADD COLUMN gate_receipts int NOT NULL DEFAULT 0 CHECK (gate_receipts >= 0),
            ADD COLUMN required_gates text,
            ADD COLUMN staged_counts jsonb NOT NULL DEFAULT '{{}}'::jsonb;
        UPDATE {s}.runs r SET batches_open = (SELECT count(*) FROM {s}.batches b
                                              WHERE b.run_id = r.run_id
                                              AND b.status = 'requested')
            WHERE r.status IN ('open', 'frozen') AND EXISTS (
                SELECT 1 FROM {s}.batches b WHERE b.run_id = r.run_id
                AND b.status = 'requested');
    END IF;
END $d$;
-- a batch receipt's operation counts ({{"counts": {{table: {{op: n}}}}, ...}}), or NULL unless
-- every n is a JSON number written as a non-negative integer (no null, string, sign or fraction)
CREATE OR REPLACE FUNCTION {s}.nk_batch_counts(receipt text) RETURNS jsonb
    LANGUAGE plpgsql IMMUTABLE AS $f$
DECLARE counts jsonb;
BEGIN
    BEGIN
        counts := receipt::jsonb -> 'counts';
    EXCEPTION WHEN others THEN
        RETURN NULL;
    END;
    IF counts IS NULL OR jsonb_typeof(counts) <> 'object' OR EXISTS (
            SELECT 1 FROM jsonb_each(counts) e WHERE jsonb_typeof(e.value) <> 'object'
            OR EXISTS (SELECT 1 FROM jsonb_each(e.value) o
                       WHERE jsonb_typeof(o.value) <> 'number'
                       OR NOT (o.value::text) ~ '^[0-9]{{1,15}}$')) THEN
        RETURN NULL;
    END IF;
    RETURN counts;
END $f$;
-- per-table/op operation counts: {{table: {{op: n}}}}; a + b, key by key (fixed size: tables x ops)
CREATE OR REPLACE FUNCTION {s}.nk_counts_add(a jsonb, b jsonb) RETURNS jsonb
    LANGUAGE sql IMMUTABLE AS $f$
    SELECT COALESCE(jsonb_object_agg(t, ops), '{{}}'::jsonb) FROM (
        SELECT t, jsonb_object_agg(op, n) AS ops FROM (
            SELECT e.key AS t, o.key AS op, sum((o.value #>> '{{}}')::bigint) AS n
            FROM (SELECT * FROM jsonb_each(a) UNION ALL SELECT * FROM jsonb_each(b)) e,
                 jsonb_each(e.value) o GROUP BY 1, 2) x GROUP BY t) y
$f$;
ALTER TABLE {s}.batches
    ADD COLUMN IF NOT EXISTS basis_seq int CHECK (basis_seq >= 0),
    ADD COLUMN IF NOT EXISTS sealed boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS revision_count int,
    ADD COLUMN IF NOT EXISTS revisions_digest text COLLATE "C",
    ADD COLUMN IF NOT EXISTS chain_digest text COLLATE "C";
ALTER TABLE {s}.revisions
    ADD COLUMN IF NOT EXISTS url_norm text,
    ADD COLUMN IF NOT EXISTS url_key bytea,
    ADD COLUMN IF NOT EXISTS title_norm text,
    ADD COLUMN IF NOT EXISTS title_key bytea,
    ADD COLUMN IF NOT EXISTS sha256 text COLLATE "C",
    ADD COLUMN IF NOT EXISTS shard text COLLATE "C",
    ADD COLUMN IF NOT EXISTS topic_key text COLLATE "C";
CREATE INDEX IF NOT EXISTS revisions_batch ON {s}.revisions (run_id, batch_seq);
CREATE INDEX IF NOT EXISTS revisions_url_key ON {s}.revisions (url_key) WHERE url_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS revisions_title_key ON {s}.revisions (title_key)
    WHERE title_key IS NOT NULL;
CREATE INDEX IF NOT EXISTS revisions_legacy ON {s}.revisions (shard, topic_key, key)
    WHERE tbl = 'manifest';

CREATE TABLE IF NOT EXISTS {s}.run_access (
    run_id text COLLATE "C" NOT NULL REFERENCES {s}.runs,
    token_sha256 text COLLATE "C" NOT NULL CHECK (token_sha256 ~ '^[0-9a-f]{{64}}$'),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, token_sha256)
);
CREATE OR REPLACE TRIGGER run_access_immutable BEFORE UPDATE OR DELETE ON {s}.run_access
    FOR EACH ROW EXECUTE FUNCTION {s}.nk_refuse();

CREATE TABLE IF NOT EXISTS {s}.gate_receipts (
    run_id text COLLATE "C" NOT NULL REFERENCES {s}.runs,
    gate text COLLATE "C" NOT NULL CHECK (gate ~ '^[A-Za-z0-9][A-Za-z0-9._-]{{0,127}}$'),
    frozen_seq int NOT NULL,
    frozen_digest text COLLATE "C" NOT NULL,
    verdict text NOT NULL CHECK (verdict IN ('passed', 'failed')),
    detail_text text NOT NULL DEFAULT '{{}}',
    recorded_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, gate)
);

CREATE TABLE IF NOT EXISTS {s}.projection_state (
    one boolean PRIMARY KEY DEFAULT true CHECK (one),
    generation bigint REFERENCES {s}.generations,
    fold_generation bigint REFERENCES {s}.generations,
    fold_tbl text COLLATE "C",
    fold_key text COLLATE "C",
    pins bigint NOT NULL DEFAULT 0 CHECK (pins >= 0),
    CHECK ((fold_tbl IS NULL) = (fold_key IS NULL)),
    CHECK (fold_generation IS NOT NULL OR fold_tbl IS NULL)
);
INSERT INTO {s}.projection_state DEFAULT VALUES ON CONFLICT DO NOTHING;

-- the chain digest of a run before its first batch
CREATE OR REPLACE FUNCTION {s}.nk_chain_origin(rid text) RETURNS text
    LANGUAGE sql IMMUTABLE AS $f$
    SELECT encode(sha256(convert_to('nekaise-stage-chain:' || rid, 'UTF8')), 'hex')
$f$;

-- Every required gate passed, bound to the run's frozen sequence and digest, and no receipt of
-- the run failed or is bound to anything else. Evaluated inside the promotion's own statements
-- (fresh snapshots under READ COMMITTED, after the run row lock).
CREATE OR REPLACE FUNCTION {s}.nk_gates_passed(rid text) RETURNS boolean LANGUAGE plpgsql AS $f$
DECLARE r record;
BEGIN
    SELECT required_gates, frozen_seq, frozen_digest INTO r FROM {s}.runs WHERE run_id = rid;
    IF r.required_gates IS NULL OR r.frozen_seq IS NULL OR r.frozen_digest IS NULL THEN
        RETURN false;
    END IF;
    IF EXISTS (SELECT 1 FROM {s}.gate_receipts g WHERE g.run_id = rid
               AND (g.verdict <> 'passed' OR g.frozen_seq <> r.frozen_seq
                    OR g.frozen_digest <> r.frozen_digest)) THEN
        RETURN false;
    END IF;
    RETURN NOT EXISTS (
        SELECT 1 FROM jsonb_array_elements_text(r.required_gates::jsonb) q(gate)
        WHERE NOT EXISTS (SELECT 1 FROM {s}.gate_receipts g WHERE g.run_id = rid
                          AND g.gate = q.gate AND g.verdict = 'passed'));
END $f$;

CREATE OR REPLACE FUNCTION {s}.nk_runs_guard() RETURNS trigger LANGUAGE plpgsql AS $f$
DECLARE chain text; doc jsonb; canon text;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'nekaise: runs are never deleted (run %)', OLD.run_id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'open' OR NEW.staged_seq <> 0 OR NEW.frozen_seq IS NOT NULL
                OR NEW.frozen_digest IS NOT NULL OR NEW.promoted_generation IS NOT NULL
                OR NEW.ended_at IS NOT NULL OR NEW.batches_open <> 0 OR NEW.gate_receipts <> 0
                OR NEW.required_gates IS NOT NULL OR NEW.staged_counts <> '{{}}'::jsonb THEN
            RAISE EXCEPTION 'nekaise: a run starts open and empty (run %)', NEW.run_id
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.status IN ('promoted', 'aborted') THEN
        RAISE EXCEPTION 'nekaise: run % is %, final', OLD.run_id, OLD.status
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF (NEW.run_id, NEW.kind, NEW.parent_generation, NEW.authority_epoch, NEW.writer_epoch,
        NEW.producer_commit, NEW.config_digest, NEW.extractor_version, NEW.cleaning_ruleset,
        NEW.started_at) IS DISTINCT FROM
       (OLD.run_id, OLD.kind, OLD.parent_generation, OLD.authority_epoch, OLD.writer_epoch,
        OLD.producer_commit, OLD.config_digest, OLD.extractor_version, OLD.cleaning_ruleset,
        OLD.started_at) THEN
        RAISE EXCEPTION 'nekaise: run % identity is immutable', OLD.run_id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    -- the staging sequence and the counters are maintained by the batch and gate receipt
    -- triggers only (depth 2: client statement -> their trigger -> this guard)
    IF (NEW.staged_seq, NEW.batches_open, NEW.gate_receipts, NEW.staged_counts)
            IS DISTINCT FROM (OLD.staged_seq, OLD.batches_open, OLD.gate_receipts,
                              OLD.staged_counts) AND pg_trigger_depth() < 2 THEN
        RAISE EXCEPTION 'nekaise: run % staging counters are maintained by the batch and gate '
            'triggers', OLD.run_id USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.staged_seq NOT IN (OLD.staged_seq, OLD.staged_seq + 1)
            OR (OLD.status <> 'open' AND NEW.staged_seq <> OLD.staged_seq) THEN
        RAISE EXCEPTION 'nekaise: run % staging sequence only grows by one while open', OLD.run_id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.staged_counts IS DISTINCT FROM OLD.staged_counts
            AND (OLD.status <> 'open' OR NEW.status <> 'open') THEN
        RAISE EXCEPTION 'nekaise: run % operation counts grow only while it stages', OLD.run_id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.gate_receipts <> OLD.gate_receipts
            AND (OLD.status <> 'frozen' OR NEW.status <> 'frozen') THEN
        RAISE EXCEPTION 'nekaise: gate receipts are recorded only for a frozen run (run %)',
            OLD.run_id USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.status <> OLD.status AND (OLD.status, NEW.status) NOT IN
            (('open', 'frozen'), ('open', 'aborted'), ('frozen', 'promoted'), ('frozen', 'aborted'))
    THEN
        RAISE EXCEPTION 'nekaise: run % cannot go from % to %', OLD.run_id, OLD.status, NEW.status
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.status = 'open' AND (NEW.frozen_seq IS NOT NULL OR NEW.frozen_digest IS NOT NULL
                                OR NEW.required_gates IS NOT NULL) THEN
        RAISE EXCEPTION 'nekaise: open run % has no frozen sequence or gate set', OLD.run_id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.status = 'open' AND NEW.status = 'frozen' THEN
        IF NEW.batches_open <> 0 THEN
            RAISE EXCEPTION 'nekaise: run % has requested batches that were never applied',
                OLD.run_id USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF NEW.staged_seq = 0 THEN
            chain := {s}.nk_chain_origin(NEW.run_id);
        ELSE
            SELECT b.chain_digest INTO chain FROM {s}.batches b
                WHERE b.run_id = NEW.run_id AND b.seq = NEW.staged_seq AND b.sealed;
        END IF;
        IF NEW.frozen_seq IS DISTINCT FROM NEW.staged_seq OR chain IS NULL
                OR NEW.frozen_digest IS DISTINCT FROM chain THEN
            RAISE EXCEPTION 'nekaise: run % must freeze at its staged sequence with that '
                'sequence''s chain digest', OLD.run_id
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        BEGIN
            doc := NEW.required_gates::jsonb;
        EXCEPTION WHEN others THEN
            doc := NULL;
        END;
        IF doc IS NULL OR jsonb_typeof(doc) <> 'array' OR jsonb_array_length(doc) = 0
                OR EXISTS (SELECT 1 FROM jsonb_array_elements(doc) e
                           WHERE jsonb_typeof(e) <> 'string'
                           OR NOT (e #>> '{{}}') ~ '^[A-Za-z0-9][A-Za-z0-9._-]{{0,127}}$') THEN
            RAISE EXCEPTION 'nekaise: run % needs a non-empty list of required gate names',
                OLD.run_id USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        SELECT '[' || string_agg(to_json(g)::text, ',' ORDER BY g COLLATE "C") || ']' INTO canon
            FROM (SELECT DISTINCT e #>> '{{}}' AS g FROM jsonb_array_elements(doc) e) d;
        IF canon <> NEW.required_gates THEN
            RAISE EXCEPTION 'nekaise: run % required gates are not a sorted, distinct, canonical '
                'JSON list', OLD.run_id USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    END IF;
    IF OLD.status = 'frozen' AND (NEW.frozen_seq, NEW.frozen_digest, NEW.required_gates)
            IS DISTINCT FROM (OLD.frozen_seq, OLD.frozen_digest, OLD.required_gates) THEN
        RAISE EXCEPTION 'nekaise: run % frozen sequence and gate set are immutable', OLD.run_id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF (NEW.status = 'promoted') <> (NEW.promoted_generation IS NOT NULL)
            OR (NEW.status = 'promoted' AND NOT EXISTS (
                SELECT 1 FROM {s}.generations g WHERE g.generation = NEW.promoted_generation
                AND g.run_id = NEW.run_id)) THEN
        RAISE EXCEPTION 'nekaise: run % is promoted exactly when its generation exists',
            OLD.run_id USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.status = 'promoted' AND NOT {s}.nk_gates_passed(NEW.run_id) THEN
        RAISE EXCEPTION 'nekaise: run % did not pass its required gates at its frozen sequence',
            OLD.run_id USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END $f$;

CREATE OR REPLACE FUNCTION {s}.nk_batches_guard() RETURNS trigger LANGUAGE plpgsql AS $f$
DECLARE run_status text; run_staged int; prev text; cnt int; rdig text;
BEGIN
    IF TG_OP = 'DELETE' THEN
        SELECT status INTO run_status FROM {s}.runs WHERE run_id = OLD.run_id;
        IF run_status = 'aborted' THEN RETURN OLD; END IF;
        RAISE EXCEPTION 'nekaise: batch receipts of run % (%) are retained', OLD.run_id,
            run_status USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    SELECT status, staged_seq INTO run_status, run_staged FROM {s}.runs WHERE run_id = NEW.run_id;
    IF TG_OP = 'INSERT' THEN
        IF run_status IS DISTINCT FROM 'open' OR NEW.status <> 'requested' OR NEW.sealed
                OR NEW.seq IS NOT NULL OR NEW.applied_at IS NOT NULL
                OR NEW.basis_seq IS DISTINCT FROM run_staged OR NEW.counts_text IS NOT NULL
                OR NEW.revision_count IS NOT NULL OR NEW.revisions_digest IS NOT NULL
                OR NEW.chain_digest IS NOT NULL THEN
            RAISE EXCEPTION 'nekaise: batch %.%.% must be requested in an open run at its current '
                'staging sequence', NEW.run_id, NEW.step, NEW.batch
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF NEW.request_digest <> encode(sha256(convert_to(NEW.request_text, 'UTF8')), 'hex') THEN
            RAISE EXCEPTION 'nekaise: batch %.%.% request digest does not match its text',
                NEW.run_id, NEW.step, NEW.batch USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF (NEW.run_id, NEW.step, NEW.batch, NEW.request_digest, NEW.request_text, NEW.requested_at,
        NEW.basis_seq) IS DISTINCT FROM
       (OLD.run_id, OLD.step, OLD.batch, OLD.request_digest, OLD.request_text, OLD.requested_at,
        OLD.basis_seq) THEN
        RAISE EXCEPTION 'nekaise: batch request %.%.% is immutable', OLD.run_id, OLD.step,
            OLD.batch USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF OLD.status = 'requested' AND NEW.status = 'abandoned' THEN
        IF NEW.seq IS NOT NULL OR NEW.sealed OR NEW.counts_text IS NOT NULL THEN
            RAISE EXCEPTION 'nekaise: an abandoned batch stages nothing'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.status = 'requested' AND NEW.status = 'applied' THEN
        IF run_status IS DISTINCT FROM 'open' OR OLD.basis_seq IS NULL
                OR NEW.seq IS DISTINCT FROM OLD.basis_seq + 1 OR NEW.sealed
                OR NEW.counts_text IS NOT NULL OR NEW.revision_count IS NOT NULL
                OR NEW.revisions_digest IS NOT NULL OR NEW.chain_digest IS NOT NULL THEN
            RAISE EXCEPTION 'nekaise: batch %.%.% applies in an open run exactly after the '
                'sequence it was computed at', OLD.run_id, OLD.step, OLD.batch
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.status = 'applied' AND NOT OLD.sealed AND NEW.sealed THEN
        -- sealing: the client may set counts_text; the database computes the digests
        IF NEW.status <> 'applied' OR NEW.seq IS DISTINCT FROM OLD.seq
                OR NEW.applied_at IS DISTINCT FROM OLD.applied_at THEN
            RAISE EXCEPTION 'nekaise: sealing batch %.%.% changes only its seal', OLD.run_id,
                OLD.step, OLD.batch USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        -- two levels, so no value grows with the batch: each chunk of 4096 revisions (in
        -- (tbl, key) order) is hashed, then the list of chunk digests
        SELECT COALESCE(sum(c.n), 0), encode(sha256(convert_to(COALESCE(string_agg(
                   c.digest, E'\n' ORDER BY c.chunk), ''), 'UTF8')), 'hex')
            INTO cnt, rdig FROM (
                SELECT w.chunk, count(*) AS n, encode(sha256(convert_to(string_agg(w.line,
                       E'\n' ORDER BY w.tbl, w.key), 'UTF8')), 'hex') AS digest
                FROM (SELECT v.tbl, v.key, (row_number() OVER (ORDER BY v.tbl, v.key) - 1)
                             / 4096 AS chunk,
                             json_build_array(v.tbl, v.key, v.op, v.row_sha256, v.before_sha256,
                                              v.reason)::text AS line
                      FROM {s}.revisions v
                      WHERE v.run_id = OLD.run_id AND v.batch_seq = OLD.seq) w
                GROUP BY w.chunk) c;
        IF OLD.seq = 1 THEN
            prev := {s}.nk_chain_origin(OLD.run_id);
        ELSE
            SELECT b.chain_digest INTO prev FROM {s}.batches b
                WHERE b.run_id = OLD.run_id AND b.seq = OLD.seq - 1 AND b.sealed;
        END IF;
        IF prev IS NULL THEN
            RAISE EXCEPTION 'nekaise: batch %.%.% follows an unsealed batch', OLD.run_id,
                OLD.step, OLD.batch USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        NEW.revision_count := cnt;
        NEW.revisions_digest := rdig;
        NEW.chain_digest := encode(sha256(convert_to(prev || ':' || OLD.seq || ':' || OLD.step
            || ':' || OLD.batch || ':' || OLD.request_digest || ':' || rdig, 'UTF8')), 'hex');
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'nekaise: batch %.%.% is %, final', OLD.run_id, OLD.step, OLD.batch,
        CASE WHEN OLD.sealed THEN 'sealed' ELSE OLD.status END
        USING ERRCODE = 'integrity_constraint_violation';
END $f$;

-- The run row is the shared row every batch transition writes (see the header).
CREATE OR REPLACE FUNCTION {s}.nk_batches_after() RETURNS trigger LANGUAGE plpgsql AS $f$
DECLARE counts jsonb;
BEGIN
    IF TG_OP = 'INSERT' THEN
        UPDATE {s}.runs SET batches_open = batches_open + 1
            WHERE run_id = NEW.run_id AND status = 'open' AND staged_seq = NEW.basis_seq;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'nekaise: run % moved on while batch %.% was requested', NEW.run_id,
                NEW.step, NEW.batch USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    ELSIF OLD.status = 'requested' AND NEW.status = 'applied' THEN
        UPDATE {s}.runs SET staged_seq = NEW.seq, batches_open = batches_open - 1
            WHERE run_id = NEW.run_id AND status = 'open' AND staged_seq = NEW.seq - 1;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'nekaise: run % is not open at sequence % (batch %.% is stale)',
                NEW.run_id, NEW.seq - 1, NEW.step, NEW.batch
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    ELSIF OLD.status = 'requested' AND NEW.status = 'abandoned' THEN
        UPDATE {s}.runs SET batches_open = batches_open - 1
            WHERE run_id = NEW.run_id AND status IN ('open', 'frozen');
    ELSIF NOT OLD.sealed AND NEW.sealed AND NEW.counts_text IS NOT NULL THEN
        -- the run's fixed-size summary grows with each sealed batch: promotion reads only it
        counts := {s}.nk_batch_counts(NEW.counts_text);
        IF counts IS NULL THEN
            RAISE EXCEPTION 'nekaise: batch %.%.% counts must map tables to operation counts',
                NEW.run_id, NEW.step, NEW.batch USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        UPDATE {s}.runs SET staged_counts = {s}.nk_counts_add(staged_counts, counts)
            WHERE run_id = NEW.run_id AND status = 'open';
        IF NOT FOUND THEN
            RAISE EXCEPTION 'nekaise: run % is not open: batch %.% cannot be counted', NEW.run_id,
                NEW.step, NEW.batch USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    END IF;
    RETURN NULL;
END $f$;
CREATE OR REPLACE TRIGGER batches_after AFTER INSERT OR UPDATE ON {s}.batches
    FOR EACH ROW EXECUTE FUNCTION {s}.nk_batches_after();
-- at commit, every applied batch is sealed: no other transaction ever sees an unsealed one
CREATE OR REPLACE FUNCTION {s}.nk_batches_sealed() RETURNS trigger LANGUAGE plpgsql AS $f$
BEGIN
    IF EXISTS (SELECT 1 FROM {s}.batches b WHERE b.run_id = NEW.run_id AND b.step = NEW.step
               AND b.batch = NEW.batch AND b.status = 'applied' AND NOT b.sealed) THEN
        RAISE EXCEPTION 'nekaise: batch %.%.% was applied but not sealed', NEW.run_id, NEW.step,
            NEW.batch USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NULL;
END $f$;
DO $d$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'batches_sealed'
                   AND tgrelid = '{s}.batches'::regclass) THEN
        CREATE CONSTRAINT TRIGGER batches_sealed AFTER UPDATE ON {s}.batches
            DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
            EXECUTE FUNCTION {s}.nk_batches_sealed();
    END IF;
END $d$;

CREATE OR REPLACE FUNCTION {s}.nk_revisions_guard() RETURNS trigger LANGUAGE plpgsql AS $f$
DECLARE run_status text; b_status text; b_sealed boolean;
BEGIN
    IF TG_OP = 'DELETE' THEN
        SELECT status INTO run_status FROM {s}.runs WHERE run_id = OLD.run_id;
        IF run_status = 'aborted' THEN RETURN OLD; END IF;
        SELECT status, sealed INTO b_status, b_sealed FROM {s}.batches
            WHERE run_id = OLD.run_id AND seq = OLD.batch_seq;
        IF run_status = 'open' AND b_status = 'applied' AND NOT b_sealed THEN
            RETURN OLD;  -- the applying transaction drops a net no-op before sealing
        END IF;
        RAISE EXCEPTION 'nekaise: revisions of run % (%) are retained', OLD.run_id, run_status
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    SELECT status INTO run_status FROM {s}.runs WHERE run_id = NEW.run_id;
    SELECT status, sealed INTO b_status, b_sealed FROM {s}.batches
        WHERE run_id = NEW.run_id AND seq = NEW.batch_seq;
    IF run_status IS DISTINCT FROM 'open' OR b_status IS DISTINCT FROM 'applied' OR b_sealed THEN
        RAISE EXCEPTION 'nekaise: revision %/%/% belongs to no batch being applied (run %, batch '
            '%)', NEW.tbl, NEW.key, NEW.batch_seq, run_status, COALESCE(b_status, 'missing')
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF TG_OP = 'UPDATE' AND (NEW.rev_id, NEW.run_id, NEW.batch_seq, NEW.tbl, NEW.key,
                             NEW.before_sha256) IS DISTINCT FROM
                            (OLD.rev_id, OLD.run_id, OLD.batch_seq, OLD.tbl, OLD.key,
                             OLD.before_sha256) THEN
        RAISE EXCEPTION 'nekaise: revision % identity is immutable', OLD.rev_id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.op = 'put' AND NEW.row_sha256 <> encode(sha256(convert_to(NEW.row_text, 'UTF8')), 'hex')
    THEN
        RAISE EXCEPTION 'nekaise: revision row_sha256 does not match its row text'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END $f$;

CREATE OR REPLACE FUNCTION {s}.nk_generations_guard() RETURNS trigger LANGUAGE plpgsql AS $f$
DECLARE r record; head bigint;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'nekaise: generation % is immutable', OLD.generation
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    SELECT * INTO r FROM {s}.runs WHERE run_id = NEW.run_id;
    SELECT current_generation INTO head FROM {s}.dataset;
    IF r.status IS DISTINCT FROM 'frozen'
            OR NEW.parent IS DISTINCT FROM head
            OR r.parent_generation IS DISTINCT FROM head
            OR (NEW.producer_commit, NEW.config_digest, NEW.extractor_version,
                NEW.cleaning_ruleset, NEW.frozen_seq, NEW.frozen_digest) IS DISTINCT FROM
               (r.producer_commit, r.config_digest, r.extractor_version, r.cleaning_ruleset,
                r.frozen_seq, r.frozen_digest) THEN
        RAISE EXCEPTION 'nekaise: generation % must promote a frozen run staged on the current '
            'generation % with that run''s provenance', NEW.generation, head
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NOT {s}.nk_gates_passed(NEW.run_id) THEN
        RAISE EXCEPTION 'nekaise: generation % needs every required gate of run % passed at its '
            'frozen sequence', NEW.generation, NEW.run_id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END $f$;

-- A generation is one atomic promotion: by the end of the transaction that inserts it, its run
-- is promoted to it, the dataset's current generation reached it, and its outbox row exists.
CREATE OR REPLACE FUNCTION {s}.nk_generations_complete() RETURNS trigger LANGUAGE plpgsql AS $f$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM {s}.runs r WHERE r.run_id = NEW.run_id
                   AND r.status = 'promoted' AND r.promoted_generation = NEW.generation)
            OR (SELECT current_generation FROM {s}.dataset) IS DISTINCT FROM NEW.generation
            OR NOT EXISTS (SELECT 1 FROM {s}.outbox o WHERE o.generation = NEW.generation) THEN
        RAISE EXCEPTION 'nekaise: generation % must commit with its run promoted, the current '
            'generation advanced to it and its outbox row', NEW.generation
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NULL;
END $f$;
DO $d$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'generations_complete'
                   AND tgrelid = '{s}.generations'::regclass) THEN
        CREATE CONSTRAINT TRIGGER generations_complete AFTER INSERT ON {s}.generations
            DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
            EXECUTE FUNCTION {s}.nk_generations_complete();
    END IF;
END $d$;

CREATE OR REPLACE FUNCTION {s}.nk_gate_receipts_guard() RETURNS trigger LANGUAGE plpgsql AS $f$
DECLARE r record;
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF (SELECT status FROM {s}.runs WHERE run_id = OLD.run_id) = 'aborted' THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION 'nekaise: gate receipt %/% is retained', OLD.run_id, OLD.gate
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'nekaise: gate receipt %/% is immutable', OLD.run_id, OLD.gate
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    SELECT status, frozen_seq, frozen_digest INTO r FROM {s}.runs WHERE run_id = NEW.run_id;
    IF r.status IS DISTINCT FROM 'frozen' OR (NEW.frozen_seq, NEW.frozen_digest)
            IS DISTINCT FROM (r.frozen_seq, r.frozen_digest) THEN
        RAISE EXCEPTION 'nekaise: gate receipt %/% must be bound to the frozen sequence and '
            'digest of a frozen run', NEW.run_id, NEW.gate
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END $f$;
CREATE OR REPLACE TRIGGER gate_receipts_guard BEFORE INSERT OR UPDATE OR DELETE
    ON {s}.gate_receipts FOR EACH ROW EXECUTE FUNCTION {s}.nk_gate_receipts_guard();
CREATE OR REPLACE FUNCTION {s}.nk_gate_receipts_after() RETURNS trigger LANGUAGE plpgsql AS $f$
BEGIN
    UPDATE {s}.runs SET gate_receipts = gate_receipts + 1
        WHERE run_id = NEW.run_id AND status = 'frozen';
    IF NOT FOUND THEN
        RAISE EXCEPTION 'nekaise: run % is no longer frozen: gate % cannot be recorded',
            NEW.run_id, NEW.gate USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NULL;
END $f$;
CREATE OR REPLACE TRIGGER gate_receipts_after AFTER INSERT ON {s}.gate_receipts
    FOR EACH ROW EXECUTE FUNCTION {s}.nk_gate_receipts_after();

-- The projection advances one generation at a time and never past an active retention pin;
-- a pin can only be taken on a generation the projection has not passed. Pins and folding both
-- write the projection_state row.
CREATE OR REPLACE FUNCTION {s}.nk_projection_state_guard() RETURNS trigger LANGUAGE plpgsql AS $f$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'nekaise: projection_state is never deleted'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.pins <> OLD.pins AND (pg_trigger_depth() < 2 OR NEW.pins < OLD.pins) THEN
        RAISE EXCEPTION 'nekaise: projection_state.pins is maintained by the retention trigger'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.generation IS DISTINCT FROM OLD.generation THEN
        IF NEW.generation IS DISTINCT FROM COALESCE(OLD.generation, -1) + 1 THEN
            RAISE EXCEPTION 'nekaise: the projection advances one generation at a time'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        IF EXISTS (SELECT 1 FROM {s}.generation_retention p WHERE p.generation < NEW.generation
                   AND (p.until IS NULL OR p.until > now())) THEN
            RAISE EXCEPTION 'nekaise: a retention pin holds the projection before generation %',
                NEW.generation USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    END IF;
    IF NEW.fold_generation IS NOT NULL
            AND NEW.fold_generation <> COALESCE(NEW.generation, -1) + 1 THEN
        RAISE EXCEPTION 'nekaise: only the next generation can be folding'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    -- starting a fold already passes the generations below it (a partially folded projection
    -- serves none of them), so it needs the same clearance as completing it
    IF NEW.fold_generation IS NOT NULL
            AND NEW.fold_generation IS DISTINCT FROM OLD.fold_generation
            AND EXISTS (SELECT 1 FROM {s}.generation_retention p
                        WHERE p.generation < NEW.fold_generation
                        AND (p.until IS NULL OR p.until > now())) THEN
        RAISE EXCEPTION 'nekaise: a retention pin holds the projection before generation %',
            NEW.fold_generation USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END $f$;
CREATE OR REPLACE TRIGGER projection_state_guard BEFORE UPDATE OR DELETE ON {s}.projection_state
    FOR EACH ROW EXECUTE FUNCTION {s}.nk_projection_state_guard();
CREATE OR REPLACE FUNCTION {s}.nk_retention_after() RETURNS trigger LANGUAGE plpgsql AS $f$
DECLARE p bigint; f bigint;
BEGIN
    -- the floor is the generation being folded when a fold is in progress (it has passed the
    -- projection generation below it), else the projection generation
    UPDATE {s}.projection_state SET pins = pins + 1 RETURNING generation, fold_generation
        INTO p, f;
    IF NEW.generation < COALESCE(f, p, -1) THEN
        RAISE EXCEPTION 'nekaise: generation % is already folded into the projection (at %, '
            'folding %); it can no longer be pinned', NEW.generation, p, f
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NULL;
END $f$;
CREATE OR REPLACE TRIGGER generation_retention_after AFTER INSERT OR UPDATE
    ON {s}.generation_retention FOR EACH ROW EXECUTE FUNCTION {s}.nk_retention_after();

-- Legacy state, inside the migration's transaction and before the new rules apply to it:
-- (1) batches applied by step-1 code were never sealed: seal them in sequence order (the digests
-- are computed from their immutable revisions; nothing else changes) — with the summary trigger
-- off, since it enforces NEW seals (open runs only) and these runs may be frozen, promoted or
-- aborted; (2) every run's summary is computed from its sealed receipts in one statement, with
-- the run guard off (it lets only the batch trigger change the counters, and final runs not at
-- all). A receipt whose counts are malformed refuses the migration. Idempotent: the summary is
-- recomputed in full from the same receipts.
DO $d$ DECLARE b record; BEGIN
    -- checked as each legacy seal runs (queued deferred events would block ALTER TABLE)
    SET CONSTRAINTS {s}.batches_sealed IMMEDIATE;
    ALTER TABLE {s}.batches DISABLE TRIGGER batches_after;
    FOR b IN SELECT run_id, step, batch FROM {s}.batches WHERE status = 'applied' AND NOT sealed
             ORDER BY run_id, seq LOOP
        UPDATE {s}.batches SET sealed = true
            WHERE run_id = b.run_id AND step = b.step AND batch = b.batch;
    END LOOP;
    ALTER TABLE {s}.batches ENABLE TRIGGER batches_after;
    SET CONSTRAINTS {s}.batches_sealed DEFERRED;
    SELECT run_id, step, batch INTO b FROM {s}.batches
        WHERE sealed AND counts_text IS NOT NULL AND {s}.nk_batch_counts(counts_text) IS NULL
        LIMIT 1;
    IF FOUND THEN
        RAISE EXCEPTION 'nekaise: legacy batch %.%.% has malformed counts: migrate by hand',
            b.run_id, b.step, b.batch USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    ALTER TABLE {s}.runs DISABLE TRIGGER runs_guard;
    UPDATE {s}.runs r SET staged_counts = c.summary FROM (
        SELECT run_id, jsonb_object_agg(t, ops) AS summary FROM (
            SELECT run_id, t, jsonb_object_agg(op, n) AS ops FROM (
                SELECT lb.run_id, e.key AS t, o.key AS op, sum((o.value #>> '{{}}')::bigint) AS n
                FROM {s}.batches lb, jsonb_each({s}.nk_batch_counts(lb.counts_text)) e,
                     jsonb_each(e.value) o
                WHERE lb.sealed AND lb.counts_text IS NOT NULL GROUP BY 1, 2, 3) x
            GROUP BY run_id, t) y GROUP BY run_id) c
        WHERE c.run_id = r.run_id AND r.staged_counts IS DISTINCT FROM c.summary;
    ALTER TABLE {s}.runs ENABLE TRIGGER runs_guard;
END $d$;

-- the projection consumer folds promoted generations into the projection tables; registered
-- before any compaction (the consumer trigger refuses it afterwards)
INSERT INTO {s}.outbox_consumers (consumer)
    SELECT 'projection' WHERE NOT EXISTS (
        SELECT 1 FROM {s}.outbox_consumers WHERE consumer = 'projection');
"""
V5_TABLES = ("run_access", "gate_receipts", "projection_state")


def _migrate_5(conn, schema):  # stage 4 step 2 staging: additive, see V5_DDL
    conn.execute(V5_DDL.format(s=schema))
    _backfill_revision_keys(conn, schema)


def _backfill_revision_keys(conn, schema) -> int:
    """Fill the derived lookup/order columns V5_DDL added to revisions (url/title keys, sha256,
    legacy shard/topic) for entries/manifest puts staged before them — without them the overlay
    misses those rows in known(), duplicate detection and legacy order. Computed from the stored
    row text exactly as stage_batch computes them; row text, digests, identity and every other
    column stay as they are. The revision guard refuses updates, so it is disabled for the
    backfill's statements (several batched UPDATEs) and re-enabled right after, all inside the
    migration's transaction (ALTER TABLE holds an exclusive lock until it commits: nothing else
    sees the table meanwhile, and a failure rolls the disable back). Returns the rows filled."""
    s = sql.Identifier(schema)
    conn.execute(sql.SQL("ALTER TABLE {}.revisions DISABLE TRIGGER revisions_guard").format(s))
    filled = 0
    with conn.cursor(name="backfill5") as cur, conn.cursor() as up:
        cur.itersize = 20000
        cur.execute(sql.SQL("SELECT rev_id, tbl, row_text FROM {}.revisions WHERE op = 'put' AND "
                            "tbl IN ('entries', 'manifest')").format(s))
        q = sql.SQL("UPDATE {}.revisions SET url_norm = %s, url_key = %s, title_norm = %s, "
                    "title_key = %s, sha256 = %s, shard = %s, topic_key = %s WHERE rev_id = %s"
                    ).format(s)
        batch = []
        for rev_id, tbl, text in cur:
            batch.append((*revision_keys(tbl, json.loads(text)), rev_id))
            if len(batch) >= 20000:
                up.executemany(q, batch)
                filled += len(batch)
                batch.clear()
        if batch:
            up.executemany(q, batch)
            filled += len(batch)
    conn.execute(sql.SQL("ALTER TABLE {}.revisions ENABLE TRIGGER revisions_guard").format(s))
    return filled


def revision_keys(tbl: str, row: Mapping) -> tuple:
    """The derived columns of an entries/manifest revision row: url_norm, url_key, title_norm,
    title_key, sha256, shard, topic_key (the last three only for manifest rows)."""
    keys = _keys_for(row)
    if tbl != "manifest":
        return (*keys, None, None, None)
    sha = row.get("sha256")
    shard, topic, _ = store.legacy_manifest_key(row)
    return (*keys, sha if isinstance(sha, str) and sha else None, shard, topic)


# ADR 0001 stage 4, step 3: local artifacts compatible with atomic metadata. Created with a fresh
# schema or by migration 6 (never re-run on open; a later revision ships as migration 7, ...).
# Additive: one new column on runs (existing runs are backfilled 'unchecked': they were staged by
# code that knew nothing of artifacts), one new table, new functions and triggers. No projection
# row, revision, receipt, event or watermark is rewritten.
#
#   runs.artifact_policy   'versioned' (the default for new runs): every manifest row a batch
#                          stages must, for each payload it claims (raw/text/corpus path +
#                          sha256), either claim exactly what the row it supersedes claimed or
#                          name an identity registered for this run in run_artifacts — checked by
#                          the database when the batch seals, so a batch whose rows point at
#                          bytes that were never durably written cannot commit. 'unchecked': the
#                          step-2 rules only (runs from before this migration, metadata tests).
#                          Immutable. A versioned run freezes only with the "artifacts" gate
#                          required (artifact_store.verify_run re-hashes what it introduced).
#   run_artifacts          (run, stage, sha256): the identities a run's batches introduced; FK to
#                          artifacts; written only by the transaction applying the batch (like
#                          revisions), immutable, deletable only for an aborted run (purge). The
#                          verification gate and later reference-checked cleanup read it.
#   artifact_locators      created unverified; immutable except verified_at (set, or moved
#                          later); never deleted
#                          (stage 5 relaxes that with reference checks); a 'local' locator must be
#                          the canonical content address artifacts/<stage>/<aa>/<bb>/<sha256>.
V6_DDL = r"""
DO $d$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = '{s}'
                   AND table_name = 'runs' AND column_name = 'artifact_policy') THEN
        -- existing runs get 'unchecked' (the backfill); new runs default to 'versioned'
        ALTER TABLE {s}.runs ADD COLUMN artifact_policy text NOT NULL DEFAULT 'unchecked'
            CHECK (artifact_policy IN ('unchecked', 'versioned'));
        ALTER TABLE {s}.runs ALTER COLUMN artifact_policy SET DEFAULT 'versioned';
    END IF;
END $d$;
CREATE OR REPLACE FUNCTION {s}.nk_runs_artifact_policy() RETURNS trigger LANGUAGE plpgsql AS $f$
DECLARE gates jsonb;
BEGIN
    IF NEW.artifact_policy IS DISTINCT FROM OLD.artifact_policy THEN
        RAISE EXCEPTION 'nekaise: run % artifact policy is immutable', OLD.run_id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    -- a versioned run freezes only with the artifact gate among its required gates, so it is
    -- never promoted before the versions it introduced were re-hashed at the frozen state
    IF NEW.artifact_policy = 'versioned' AND OLD.status = 'open' AND NEW.status = 'frozen' THEN
        BEGIN
            gates := NEW.required_gates::jsonb;
        EXCEPTION WHEN others THEN
            gates := NULL;
        END;
        IF gates IS NULL OR jsonb_typeof(gates) <> 'array' OR NOT gates ? 'artifacts' THEN
            RAISE EXCEPTION 'nekaise: versioned run % must require the "artifacts" gate',
                OLD.run_id USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    END IF;
    RETURN NEW;
END $f$;
CREATE OR REPLACE TRIGGER runs_artifact_policy BEFORE UPDATE ON {s}.runs
    FOR EACH ROW EXECUTE FUNCTION {s}.nk_runs_artifact_policy();

CREATE OR REPLACE FUNCTION {s}.nk_local_locator(stage text, sha text) RETURNS text
    LANGUAGE sql IMMUTABLE AS $f$
    SELECT 'artifacts/' || stage || '/' || substr(sha, 1, 2) || '/' || substr(sha, 3, 2) || '/'
           || sha
$f$;
CREATE OR REPLACE FUNCTION {s}.nk_locators_guard() RETURNS trigger LANGUAGE plpgsql AS $f$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'nekaise: artifact locator %/% is retained', OLD.stage, OLD.locator
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF TG_OP = 'UPDATE' THEN
        IF (NEW.stage, NEW.sha256, NEW.locator, NEW.kind, NEW.pack_offset, NEW.pack_length,
            NEW.codec, NEW.created_at) IS DISTINCT FROM
           (OLD.stage, OLD.sha256, OLD.locator, OLD.kind, OLD.pack_offset, OLD.pack_length,
            OLD.codec, OLD.created_at)
                OR NEW.verified_at IS NULL
                OR (OLD.verified_at IS NOT NULL AND NEW.verified_at < OLD.verified_at) THEN
            RAISE EXCEPTION 'nekaise: an artifact locator changes only its verification time'
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.verified_at IS NOT NULL THEN   -- verification is recorded afterwards, never seeded
        RAISE EXCEPTION 'nekaise: a new artifact locator starts unverified'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.kind = 'local' AND NEW.locator IS DISTINCT FROM
            {s}.nk_local_locator(NEW.stage, NEW.sha256) THEN
        RAISE EXCEPTION 'nekaise: a local locator is the content address %, not %',
            {s}.nk_local_locator(NEW.stage, NEW.sha256), NEW.locator
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END $f$;
CREATE OR REPLACE TRIGGER artifact_locators_guard BEFORE INSERT OR UPDATE OR DELETE
    ON {s}.artifact_locators FOR EACH ROW EXECUTE FUNCTION {s}.nk_locators_guard();

CREATE TABLE IF NOT EXISTS {s}.run_artifacts (
    run_id text COLLATE "C" NOT NULL REFERENCES {s}.runs,
    stage text COLLATE "C" NOT NULL,
    sha256 text COLLATE "C" NOT NULL,
    batch_seq int NOT NULL CHECK (batch_seq >= 1),
    PRIMARY KEY (run_id, stage, sha256),
    FOREIGN KEY (stage, sha256) REFERENCES {s}.artifacts
);
CREATE INDEX IF NOT EXISTS run_artifacts_identity ON {s}.run_artifacts (stage, sha256);
CREATE OR REPLACE FUNCTION {s}.nk_run_artifacts_guard() RETURNS trigger LANGUAGE plpgsql AS $f$
BEGIN
    IF TG_OP = 'DELETE' THEN
        IF (SELECT status FROM {s}.runs WHERE run_id = OLD.run_id) = 'aborted' THEN
            RETURN OLD;
        END IF;
        RAISE EXCEPTION 'nekaise: artifact references of run % are retained', OLD.run_id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RAISE EXCEPTION 'nekaise: artifact reference %/%/% is immutable', OLD.run_id, OLD.stage,
        OLD.sha256 USING ERRCODE = 'integrity_constraint_violation';
END $f$;
CREATE OR REPLACE TRIGGER run_artifacts_guard BEFORE UPDATE OR DELETE
    ON {s}.run_artifacts FOR EACH ROW EXECUTE FUNCTION {s}.nk_run_artifacts_guard();
-- Inserted references are checked per statement (the stager inserts a batch's references in
-- one statement): each names a batch being applied (applied, unsealed, in an open run — i.e.
-- only the applying transaction can add references, like revisions) and an artifact with its
-- canonical local locator (the only readable kind before stage 5).
CREATE OR REPLACE FUNCTION {s}.nk_run_artifacts_insert() RETURNS trigger LANGUAGE plpgsql AS $f$
DECLARE bad record;
BEGIN
    -- keyed lookups only (LATERAL), never a scan of runs, batches or locators
    SELECT d.run_id, d.batch_seq, x.run_status, x.batch_status INTO bad
        FROM (SELECT DISTINCT run_id, batch_seq FROM ins) d
        CROSS JOIN LATERAL (SELECT (SELECT r.status FROM {s}.runs r WHERE r.run_id = d.run_id)
                                   AS run_status,
                                   b.status AS batch_status, b.sealed
                            FROM (SELECT 1) one LEFT JOIN {s}.batches b
                                ON b.run_id = d.run_id AND b.seq = d.batch_seq OFFSET 0) x
        WHERE x.run_status IS DISTINCT FROM 'open' OR x.batch_status IS DISTINCT FROM 'applied'
            OR x.sealed
        LIMIT 1;
    IF FOUND THEN
        RAISE EXCEPTION 'nekaise: artifact references of run % belong to no batch being applied '
            '(run %, batch % %)', bad.run_id, bad.run_status, bad.batch_seq,
            COALESCE(bad.batch_status, 'missing')
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    SELECT i.stage, i.sha256 INTO bad FROM ins i CROSS JOIN LATERAL (
        SELECT count(*) AS n FROM (SELECT 1 FROM {s}.artifact_locators l WHERE l.stage = i.stage
                                   AND l.sha256 = i.sha256 AND l.kind = 'local'
                                   AND l.locator = {s}.nk_local_locator(i.stage, i.sha256)
                                   LIMIT 1) z) found
        WHERE found.n = 0
        LIMIT 1;
    IF FOUND THEN
        RAISE EXCEPTION 'nekaise: artifact %/% has no local locator: it cannot be referenced',
            bad.stage, bad.sha256 USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NULL;
END $f$;
CREATE OR REPLACE TRIGGER run_artifacts_insert AFTER INSERT ON {s}.run_artifacts
    REFERENCING NEW TABLE AS ins FOR EACH STATEMENT
    EXECUTE FUNCTION {s}.nk_run_artifacts_insert();

-- A manifest row's claim on one stage's payload: [path, sha256 or null], or NULL when the path
-- field is absent or JSON null (artifact_store.claim is the same rule).
CREATE OR REPLACE FUNCTION {s}.nk_claim(j jsonb, stage text) RETURNS jsonb
    LANGUAGE sql IMMUTABLE AS $f$
    SELECT CASE WHEN NULLIF(j -> f.p, 'null'::jsonb) IS NULL THEN NULL
                ELSE jsonb_build_array(j -> f.p, COALESCE(j -> f.h, 'null'::jsonb)) END
    FROM (SELECT CASE stage WHEN 'raw' THEN 'raw_path' WHEN 'text' THEN 'text_path'
                            WHEN 'corpus' THEN 'corpus_path' END AS p,
                 CASE stage WHEN 'raw' THEN 'sha256' WHEN 'text' THEN 'text_sha256'
                            WHEN 'corpus' THEN 'corpus_sha256' END AS h) f
$f$;
-- The manifest row `k` visible to batch `seq` of run `rid` at its basis (NULL: none), found
-- from the actual preceding state, never from a digest a revision supplies: the run's own
-- latest revision of the key in an EARLIER batch; else the latest revision of a run promoted
-- in (projection generation P, the run's parent generation] (the committed overlay the run was
-- staged on — no other run, aborted or pending, can supply it); else the projection row.
-- (The run's parent is the current generation while it stages, and the fold never passes the
-- current generation, so P <= parent.)
CREATE OR REPLACE FUNCTION {s}.nk_basis_text(rid text, seq int, k text) RETURNS text
    LANGUAGE plpgsql STABLE AS $f$
DECLARE o record; parent bigint; lo bigint;
BEGIN
    SELECT v.op, v.row_text INTO o FROM {s}.revisions v
        WHERE v.run_id = rid AND v.tbl = 'manifest' AND v.key = k AND v.batch_seq < seq
        ORDER BY v.batch_seq DESC LIMIT 1;
    IF FOUND THEN
        RETURN CASE WHEN o.op = 'put' THEN o.row_text END;
    END IF;
    SELECT r.parent_generation INTO parent FROM {s}.runs r WHERE r.run_id = rid;
    SELECT p.generation INTO lo FROM {s}.projection_state p;
    IF parent IS NOT NULL THEN
        SELECT v.op, v.row_text INTO o FROM {s}.revisions v JOIN {s}.runs u
                ON u.run_id = v.run_id
            WHERE v.tbl = 'manifest' AND v.key = k
              AND u.promoted_generation > COALESCE(lo, -1) AND u.promoted_generation <= parent
            ORDER BY u.promoted_generation DESC, v.batch_seq DESC LIMIT 1;
        IF FOUND THEN
            RETURN CASE WHEN o.op = 'put' THEN o.row_text END;
        END IF;
    END IF;
    RETURN (SELECT m.row_text FROM {s}.manifest m WHERE m.id = k);
END $f$;
-- Sealing a batch of a versioned run: every claim of its manifest puts is either unchanged from
-- the row visible at the batch's basis (nk_basis_text: the actual preceding state, not the
-- revision's own before_sha256, which a writer supplies) or a valid identity (non-empty string
-- path, string sha256) this run registered in run_artifacts. Runs in the sealing transaction, so a violation rolls the whole
-- batch back (its revisions, references and registrations).
-- One pass over the batch's manifest puts (index range (run, seq)); per claim one primary-key
-- probe of run_artifacts, and the before-image is looked up (once per row) only for a claim not
-- registered for the run. Every statement is a keyed lookup, so the work is linear in the batch
-- whatever the planner's statistics say about the freshly written tables.
CREATE OR REPLACE FUNCTION {s}.nk_batch_artifacts() RETURNS trigger LANGUAGE plpgsql AS $f$
DECLARE r record; st text; now jsonb; before jsonb; fetched boolean;
BEGIN
    IF (SELECT artifact_policy FROM {s}.runs WHERE run_id = NEW.run_id)
            IS DISTINCT FROM 'versioned' THEN
        RETURN NULL;
    END IF;
    FOR r IN SELECT v.key, v.row_text::jsonb AS j, v.before_sha256 FROM {s}.revisions v
             WHERE v.run_id = NEW.run_id AND v.batch_seq = NEW.seq AND v.tbl = 'manifest'
             AND v.op = 'put' LOOP
        fetched := false;
        FOREACH st IN ARRAY ARRAY['raw', 'text', 'corpus'] LOOP
            now := {s}.nk_claim(r.j, st);
            CONTINUE WHEN now IS NULL;
            CONTINUE WHEN jsonb_typeof(now -> 0) = 'string' AND (now ->> 0) <> ''
                AND jsonb_typeof(now -> 1) = 'string'
                AND EXISTS (SELECT 1 FROM {s}.run_artifacts a WHERE a.run_id = NEW.run_id
                            AND a.stage = st AND a.sha256 = (now ->> 1));
            IF NOT fetched THEN
                before := {s}.nk_basis_text(NEW.run_id, NEW.seq, r.key)::jsonb;
                fetched := true;
            END IF;
            IF now IS DISTINCT FROM {s}.nk_claim(before, st) THEN
                RAISE EXCEPTION 'nekaise: batch %.%.% stages % whose % payload claim is neither '
                    'unchanged nor an artifact registered for the run (write the version first)',
                    NEW.run_id, NEW.step, NEW.batch, r.key, st
                    USING ERRCODE = 'integrity_constraint_violation';
            END IF;
        END LOOP;
    END LOOP;
    RETURN NULL;
END $f$;
CREATE OR REPLACE TRIGGER batches_artifacts AFTER UPDATE ON {s}.batches FOR EACH ROW
    WHEN (NEW.sealed AND NOT OLD.sealed) EXECUTE FUNCTION {s}.nk_batch_artifacts();
"""
V6_TABLES = ("run_artifacts",)


def _migrate_6(conn, schema):  # stage 4 step 3 artifacts: additive, see V6_DDL
    conn.execute(V6_DDL.format(s=schema))


# ADR 0001 stage 4, step 4: recovery and operational review. Created with a fresh schema or by
# migration 7 (never re-run on open; a later revision ships as migration 8, ...). Additive: one
# new column on runs (backfilled), new tables, functions and triggers; no projection row,
# revision, receipt, event or watermark is rewritten.
#
#   runs.owner_epoch   the writer epoch that owns the run now: the opener's (backfilled from
#                      writer_epoch for every existing run, set on insert) until an adoption
#                      moves it. Every owner-only operation checks it (require_run_owner).
#   run_adoptions      the immutable log of explicit resumes: a new writer epoch takes over an
#                      open or frozen run ONLY when it is the current writer, the run's parent is
#                      still the current generation, its producer commit, configuration set and
#                      extractor version are the run's (checked against the row, never against
#                      what the adopter claims alone), and every artifact the run referenced
#                      has a verified canonical local locator. (A batch left requested is the
#                      persisted discovery merge, which the resumed coordinator replays exactly.)
#                      Otherwise: abort and start a new run.
#   purge_queue        aborted runs whose staging still has to be purged (bounded batches,
#                      store_staging.purge_run); an abort queues its run (trigger), completion
#                      dequeues it. Backfilled with every aborted run that still has staging.
#   review_state       the generation-range review (scripts/generation_review.py): the contiguous
#   review_verdicts    reviewed watermark, the endorsed watermark publication may reach, and the
#                      open findings. A verdict covers exactly (reviewed_through, hi]; findings
#                      withhold endorsement until a later verdict resolves them — one whose range
#                      covers a generation promoted after the finding (the compensating repair);
#                      an integrity finding also blocks growth; any open finding (also one raised
#                      after endorsement) stops publication. Every verdict acknowledges the range's outbox
#                      rows for consumer "review" and advances its watermark; the "publication"
#                      consumer may never pass the endorsed generation. Backfilled from the review
#                      consumer's existing acknowledgements (a legacy non-ok one refuses the
#                      migration: it would need a finding to resolve).
V7_DDL = r"""
DO $d$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = '{s}'
                   AND table_name = 'runs' AND column_name = 'owner_epoch') THEN
        ALTER TABLE {s}.runs ADD COLUMN owner_epoch bigint;
        -- the backfill touches final runs too, which the run guards refuse: off for this one
        -- statement, inside the migration's transaction (ALTER TABLE holds the table exclusively
        -- until it commits; a failure rolls the disable back)
        ALTER TABLE {s}.runs DISABLE TRIGGER runs_guard;
        ALTER TABLE {s}.runs DISABLE TRIGGER runs_artifact_policy;
        UPDATE {s}.runs SET owner_epoch = writer_epoch;
        ALTER TABLE {s}.runs ENABLE TRIGGER runs_artifact_policy;
        ALTER TABLE {s}.runs ENABLE TRIGGER runs_guard;
        ALTER TABLE {s}.runs ALTER COLUMN owner_epoch SET NOT NULL;
    END IF;
END $d$;

CREATE TABLE IF NOT EXISTS {s}.run_adoptions (
    run_id text COLLATE "C" NOT NULL REFERENCES {s}.runs,
    owner_epoch bigint NOT NULL,
    previous_epoch bigint NOT NULL,
    status text NOT NULL CHECK (status IN ('open', 'frozen')),
    parent_generation bigint,
    producer_commit text COLLATE "C" NOT NULL,
    config_digest text COLLATE "C" NOT NULL,
    extractor_version text NOT NULL,
    staged_seq int NOT NULL,
    reason text NOT NULL CHECK (reason <> ''),
    adopted_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, owner_epoch)
);
CREATE OR REPLACE FUNCTION {s}.nk_run_adoptions_guard() RETURNS trigger LANGUAGE plpgsql AS $f$
DECLARE r record; w bigint; head bigint; bad record;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'nekaise: adoption %/% is immutable', OLD.run_id, OLD.owner_epoch
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    -- the run row lock serializes an adoption with every owner-only operation on the run
    SELECT * INTO r FROM {s}.runs WHERE run_id = NEW.run_id FOR UPDATE;
    SELECT writer_epoch INTO w FROM {s}.state;
    SELECT current_generation INTO head FROM {s}.dataset;
    IF r.status IS NULL OR r.status NOT IN ('open', 'frozen') OR NEW.status <> r.status THEN
        RAISE EXCEPTION 'nekaise: run % is %: only an open or frozen run can be adopted',
            NEW.run_id, COALESCE(r.status, 'unknown')
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.owner_epoch IS DISTINCT FROM w OR NEW.owner_epoch <= r.owner_epoch
            OR NEW.previous_epoch IS DISTINCT FROM r.owner_epoch THEN
        RAISE EXCEPTION 'nekaise: run % can only be adopted by the current writer (epoch %) '
            'from its owner (epoch %)', NEW.run_id, w, r.owner_epoch
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF r.parent_generation IS DISTINCT FROM head
            OR NEW.parent_generation IS DISTINCT FROM head THEN
        RAISE EXCEPTION 'nekaise: run % was staged on generation %, the current generation is %: '
            'it cannot be resumed (abort it and start a new run)', NEW.run_id,
            r.parent_generation, head USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF (NEW.producer_commit, NEW.config_digest, NEW.extractor_version, NEW.staged_seq)
            IS DISTINCT FROM (r.producer_commit, r.config_digest, r.extractor_version,
                              r.staged_seq) THEN
        RAISE EXCEPTION 'nekaise: run % was staged by other code, configuration or extractor '
            '(or at another sequence): it cannot be resumed (abort it and start a new run)',
            NEW.run_id USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    -- keyed probes over the run's own references (never a scan of the global tables)
    SELECT ra.stage, ra.sha256 INTO bad FROM {s}.run_artifacts ra
        WHERE ra.run_id = NEW.run_id AND NOT EXISTS (
            SELECT 1 FROM {s}.artifact_locators l WHERE l.stage = ra.stage
            AND l.sha256 = ra.sha256 AND l.kind = 'local'
            AND l.locator = {s}.nk_local_locator(ra.stage, ra.sha256)
            AND l.verified_at IS NOT NULL)
        LIMIT 1;
    IF FOUND THEN
        RAISE EXCEPTION 'nekaise: run % references %/% without a verified local version: verify '
            'its artifacts before resuming it', NEW.run_id, bad.stage, bad.sha256
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END $f$;
CREATE OR REPLACE TRIGGER run_adoptions_guard BEFORE INSERT OR UPDATE OR DELETE
    ON {s}.run_adoptions FOR EACH ROW EXECUTE FUNCTION {s}.nk_run_adoptions_guard();
CREATE OR REPLACE FUNCTION {s}.nk_run_adoptions_after() RETURNS trigger LANGUAGE plpgsql AS $f$
BEGIN
    UPDATE {s}.runs SET owner_epoch = NEW.owner_epoch
        WHERE run_id = NEW.run_id AND owner_epoch = NEW.previous_epoch
        AND status IN ('open', 'frozen');
    IF NOT FOUND THEN
        RAISE EXCEPTION 'nekaise: run % changed owner or status during its adoption', NEW.run_id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NULL;
END $f$;
CREATE OR REPLACE TRIGGER run_adoptions_after AFTER INSERT ON {s}.run_adoptions
    FOR EACH ROW EXECUTE FUNCTION {s}.nk_run_adoptions_after();
-- the owner is the opener until an adoption (its trigger, depth 2) moves it, one epoch forward
CREATE OR REPLACE FUNCTION {s}.nk_runs_owner() RETURNS trigger LANGUAGE plpgsql AS $f$
BEGIN
    IF TG_OP = 'INSERT' THEN
        IF NEW.owner_epoch IS NULL THEN
            NEW.owner_epoch := NEW.writer_epoch;
        ELSIF NEW.owner_epoch <> NEW.writer_epoch THEN
            RAISE EXCEPTION 'nekaise: run % starts owned by its opener', NEW.run_id
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.owner_epoch IS DISTINCT FROM OLD.owner_epoch AND (
            pg_trigger_depth() < 2 OR NEW.owner_epoch IS NULL
            OR NEW.owner_epoch <= OLD.owner_epoch OR NOT EXISTS (
                SELECT 1 FROM {s}.run_adoptions a WHERE a.run_id = OLD.run_id
                AND a.owner_epoch = NEW.owner_epoch AND a.previous_epoch = OLD.owner_epoch)) THEN
        RAISE EXCEPTION 'nekaise: run % changes owner only through a logged adoption', OLD.run_id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END $f$;
CREATE OR REPLACE TRIGGER runs_owner BEFORE INSERT OR UPDATE ON {s}.runs
    FOR EACH ROW EXECUTE FUNCTION {s}.nk_runs_owner();

CREATE TABLE IF NOT EXISTS {s}.purge_queue (
    run_id text COLLATE "C" PRIMARY KEY REFERENCES {s}.runs,
    queued_at timestamptz NOT NULL DEFAULT now()
);
CREATE OR REPLACE FUNCTION {s}.nk_purge_queue_guard() RETURNS trigger LANGUAGE plpgsql AS $f$
BEGIN
    IF TG_OP = 'UPDATE' THEN
        RAISE EXCEPTION 'nekaise: purge queue entries are immutable'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF (SELECT status FROM {s}.runs WHERE run_id = NEW.run_id) IS DISTINCT FROM 'aborted' THEN
            RAISE EXCEPTION 'nekaise: only an aborted run is purged (run %)', NEW.run_id
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;
    IF EXISTS (SELECT 1 FROM {s}.revisions WHERE run_id = OLD.run_id)
            OR EXISTS (SELECT 1 FROM {s}.run_artifacts WHERE run_id = OLD.run_id)
            OR EXISTS (SELECT 1 FROM {s}.gate_receipts WHERE run_id = OLD.run_id)
            OR EXISTS (SELECT 1 FROM {s}.batches WHERE run_id = OLD.run_id) THEN
        RAISE EXCEPTION 'nekaise: run % still has staging to purge', OLD.run_id
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN OLD;
END $f$;
CREATE OR REPLACE TRIGGER purge_queue_guard BEFORE INSERT OR UPDATE OR DELETE ON {s}.purge_queue
    FOR EACH ROW EXECUTE FUNCTION {s}.nk_purge_queue_guard();
CREATE OR REPLACE FUNCTION {s}.nk_runs_aborted() RETURNS trigger LANGUAGE plpgsql AS $f$
BEGIN
    INSERT INTO {s}.purge_queue (run_id) VALUES (NEW.run_id) ON CONFLICT DO NOTHING;
    RETURN NULL;
END $f$;
CREATE OR REPLACE TRIGGER runs_aborted AFTER UPDATE ON {s}.runs FOR EACH ROW
    WHEN (OLD.status <> 'aborted' AND NEW.status = 'aborted')
    EXECUTE FUNCTION {s}.nk_runs_aborted();
INSERT INTO {s}.purge_queue (run_id, queued_at)
    SELECT r.run_id, COALESCE(r.ended_at, now()) FROM {s}.runs r WHERE r.status = 'aborted'
    AND (EXISTS (SELECT 1 FROM {s}.batches b WHERE b.run_id = r.run_id)
         OR EXISTS (SELECT 1 FROM {s}.revisions v WHERE v.run_id = r.run_id)
         OR EXISTS (SELECT 1 FROM {s}.run_artifacts a WHERE a.run_id = r.run_id)
         OR EXISTS (SELECT 1 FROM {s}.gate_receipts g WHERE g.run_id = r.run_id))
    ON CONFLICT DO NOTHING;

CREATE TABLE IF NOT EXISTS {s}.review_state (
    one boolean PRIMARY KEY DEFAULT true CHECK (one),
    reviewed_through bigint,
    endorsed_through bigint,
    verdicts bigint NOT NULL DEFAULT 0 CHECK (verdicts >= 0),
    open_findings int NOT NULL DEFAULT 0 CHECK (open_findings >= 0),
    open_integrity int NOT NULL DEFAULT 0 CHECK (open_integrity >= 0),
    updated_at timestamptz,
    CHECK (endorsed_through IS NULL OR endorsed_through <= reviewed_through)
);
CREATE TABLE IF NOT EXISTS {s}.review_verdicts (
    seq bigint PRIMARY KEY CHECK (seq >= 1),
    lo_generation bigint NOT NULL CHECK (lo_generation >= 0),
    hi_generation bigint NOT NULL,
    verdict text NOT NULL CHECK (verdict IN ('ok', 'finding', 'integrity')),
    reviewer text COLLATE "C" NOT NULL CHECK (reviewer ~ '^[A-Za-z0-9][A-Za-z0-9._:@-]{{0,127}}$'),
    evidence_digest text COLLATE "C" NOT NULL CHECK (evidence_digest ~ '^[0-9a-f]{{64}}$'),
    resolves_text text NOT NULL DEFAULT '[]',
    detail_text text NOT NULL DEFAULT '{{}}',
    recorded_at timestamptz NOT NULL DEFAULT now(),
    resolved_by bigint REFERENCES {s}.review_verdicts,
    CHECK (hi_generation >= lo_generation - 1),
    CHECK (resolved_by IS NULL OR (verdict <> 'ok' AND resolved_by > seq))
);
CREATE OR REPLACE FUNCTION {s}.nk_review_verdicts_guard() RETURNS trigger LANGUAGE plpgsql AS $f$
DECLARE st record; head bigint; doc jsonb; canon text; n int;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'nekaise: review verdict % is retained', OLD.seq
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF TG_OP = 'UPDATE' THEN
        -- only a later verdict's trigger resolves a finding, once
        IF pg_trigger_depth() < 2 OR OLD.resolved_by IS NOT NULL OR NEW.resolved_by IS NULL
                OR (NEW.seq, NEW.lo_generation, NEW.hi_generation, NEW.verdict, NEW.reviewer,
                    NEW.evidence_digest, NEW.resolves_text, NEW.detail_text, NEW.recorded_at)
                   IS DISTINCT FROM
                   (OLD.seq, OLD.lo_generation, OLD.hi_generation, OLD.verdict, OLD.reviewer,
                    OLD.evidence_digest, OLD.resolves_text, OLD.detail_text, OLD.recorded_at) THEN
            RAISE EXCEPTION 'nekaise: review verdict % is immutable', OLD.seq
                USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        RETURN NEW;
    END IF;
    -- the state row lock serializes verdicts (and the AFTER trigger writes it)
    SELECT * INTO st FROM {s}.review_state FOR UPDATE;
    SELECT current_generation INTO head FROM {s}.dataset;
    IF NEW.seq <> st.verdicts + 1 OR NEW.resolved_by IS NOT NULL THEN
        RAISE EXCEPTION 'nekaise: the next review verdict is % (got %)', st.verdicts + 1, NEW.seq
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.lo_generation <> COALESCE(st.reviewed_through, -1) + 1 THEN
        RAISE EXCEPTION 'nekaise: review verdicts are contiguous: the next one starts at '
            'generation % (got %)', COALESCE(st.reviewed_through, -1) + 1, NEW.lo_generation
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.hi_generation >= NEW.lo_generation AND (head IS NULL OR NEW.hi_generation > head) THEN
        RAISE EXCEPTION 'nekaise: generation % is not promoted (current: %)', NEW.hi_generation,
            head USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    BEGIN
        doc := NEW.resolves_text::jsonb;
    EXCEPTION WHEN others THEN
        doc := NULL;
    END;
    IF doc IS NULL OR jsonb_typeof(doc) <> 'array' OR EXISTS (
            SELECT 1 FROM jsonb_array_elements(doc) e WHERE jsonb_typeof(e) <> 'number'
            OR NOT (e::text) ~ '^[1-9][0-9]{{0,17}}$') THEN
        RAISE EXCEPTION 'nekaise: resolves must be a JSON list of verdict numbers'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    SELECT COALESCE('[' || string_agg(v::text, ',' ORDER BY v) || ']', '[]') INTO canon
        FROM (SELECT DISTINCT (e::text)::bigint AS v FROM jsonb_array_elements(doc) e) d;
    IF canon <> NEW.resolves_text THEN
        RAISE EXCEPTION 'nekaise: resolves must be sorted and distinct'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    SELECT count(*) INTO n FROM jsonb_array_elements(doc) e JOIN {s}.review_verdicts v
        ON v.seq = (e::text)::bigint
        WHERE v.verdict <> 'ok' AND v.resolved_by IS NULL;
    IF n <> jsonb_array_length(doc) THEN
        RAISE EXCEPTION 'nekaise: a verdict resolves only open findings'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    -- a repair is a compensating generation: a finding is resolved only by a verdict whose range
    -- covers a generation promoted after the finding's own range (never by a verdict written
    -- before the repair exists, nor over nothing)
    IF EXISTS (SELECT 1 FROM jsonb_array_elements(doc) e JOIN {s}.review_verdicts v
               ON v.seq = (e::text)::bigint
               WHERE NEW.hi_generation < NEW.lo_generation
               OR NEW.hi_generation <= GREATEST(v.hi_generation, v.lo_generation - 1)) THEN
        RAISE EXCEPTION 'nekaise: a finding is resolved only by a verdict covering a generation '
            'promoted after it (the compensating repair)'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    -- an empty range (hi = lo - 1) only records a finding about generations already reviewed
    IF NEW.hi_generation < NEW.lo_generation AND NEW.verdict = 'ok' THEN
        RAISE EXCEPTION 'nekaise: an ok verdict must cover at least one generation'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END $f$;
CREATE OR REPLACE TRIGGER review_verdicts_guard BEFORE INSERT OR UPDATE OR DELETE
    ON {s}.review_verdicts FOR EACH ROW EXECUTE FUNCTION {s}.nk_review_verdicts_guard();
CREATE OR REPLACE FUNCTION {s}.nk_review_verdicts_after() RETURNS trigger LANGUAGE plpgsql AS $f$
DECLARE fixed_f int; fixed_i int; open_f int; open_i int; reviewed bigint; top bigint;
BEGIN
    WITH done AS (
        UPDATE {s}.review_verdicts v SET resolved_by = NEW.seq
            FROM jsonb_array_elements(NEW.resolves_text::jsonb) e
            WHERE v.seq = (e::text)::bigint AND v.resolved_by IS NULL
            RETURNING v.verdict)
    SELECT count(*) FILTER (WHERE verdict = 'finding'), count(*) FILTER (WHERE verdict = 'integrity')
        INTO fixed_f, fixed_i FROM done;
    IF fixed_f + fixed_i <> jsonb_array_length(NEW.resolves_text::jsonb) THEN
        RAISE EXCEPTION 'nekaise: verdict % could not resolve every finding it names', NEW.seq
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    SELECT open_findings - fixed_f + (NEW.verdict = 'finding')::int,
           open_integrity - fixed_i + (NEW.verdict = 'integrity')::int,
           GREATEST(COALESCE(reviewed_through, -1), NEW.hi_generation)
        INTO open_f, open_i, reviewed FROM {s}.review_state;
    UPDATE {s}.review_state SET verdicts = NEW.seq, open_findings = open_f,
        open_integrity = open_i,
        reviewed_through = CASE WHEN reviewed < 0 THEN NULL ELSE reviewed END,
        endorsed_through = CASE WHEN open_f + open_i = 0 AND reviewed >= 0 THEN reviewed
                                ELSE endorsed_through END,
        updated_at = now();
    -- the outbox: the range's rows are acknowledged by the review consumer with this verdict,
    -- and its watermark moves over them (rows compacted already were reviewed before)
    INSERT INTO {s}.outbox_acks (consumer, seq, verdict, detail_text)
        SELECT 'review', o.seq, NEW.verdict, '{{"review":' || NEW.seq || '}}' FROM {s}.outbox o
        WHERE o.generation >= NEW.lo_generation AND o.generation <= NEW.hi_generation
        ON CONFLICT DO NOTHING;
    SELECT max(o.seq) INTO top FROM {s}.outbox o WHERE o.generation <= NEW.hi_generation
        AND o.generation >= NEW.lo_generation;
    IF top IS NOT NULL THEN
        UPDATE {s}.outbox_consumers SET watermark = top, updated_at = now()
            WHERE consumer = 'review' AND watermark < top;
    END IF;
    RETURN NULL;
END $f$;
CREATE OR REPLACE TRIGGER review_verdicts_after AFTER INSERT ON {s}.review_verdicts
    FOR EACH ROW EXECUTE FUNCTION {s}.nk_review_verdicts_after();
CREATE OR REPLACE FUNCTION {s}.nk_review_state_guard() RETURNS trigger LANGUAGE plpgsql AS $f$
BEGIN
    IF TG_OP = 'DELETE' OR pg_trigger_depth() < 2 THEN
        RAISE EXCEPTION 'nekaise: review_state is maintained by the review verdict trigger'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.verdicts < OLD.verdicts
            OR COALESCE(NEW.reviewed_through, -1) < COALESCE(OLD.reviewed_through, -1)
            OR COALESCE(NEW.endorsed_through, -1) < COALESCE(OLD.endorsed_through, -1) THEN
        RAISE EXCEPTION 'nekaise: the review watermarks only grow'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END $f$;
-- the review consumer is driven by verdicts only (their trigger, depth 2): its acknowledgements
-- and watermark are the verdicts' outbox image, never a second, self-supplied review record;
-- publication never passes the endorsed generation
CREATE OR REPLACE FUNCTION {s}.nk_review_acks_guard() RETURNS trigger LANGUAGE plpgsql AS $f$
BEGIN
    IF NEW.consumer = 'review' AND pg_trigger_depth() < 2 THEN
        RAISE EXCEPTION 'nekaise: the review consumer acknowledges only through review verdicts'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    RETURN NEW;
END $f$;
CREATE OR REPLACE TRIGGER outbox_acks_review BEFORE INSERT ON {s}.outbox_acks
    FOR EACH ROW EXECUTE FUNCTION {s}.nk_review_acks_guard();
CREATE OR REPLACE FUNCTION {s}.nk_publication_guard() RETURNS trigger LANGUAGE plpgsql AS $f$
DECLARE endorsed bigint; top bigint; open_n int;
BEGIN
    IF NEW.consumer = 'review' AND NEW.watermark > OLD.watermark AND pg_trigger_depth() < 2 THEN
        RAISE EXCEPTION 'nekaise: the review watermark moves only with review verdicts'
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF NEW.consumer = 'publication' AND NEW.watermark > OLD.watermark THEN
        -- WRITE the review state row (a no-op update; its guard allows depth 2): a verdict writes
        -- it too, so a publication advance and a new finding never both commit on stale reads,
        -- under any isolation level
        UPDATE {s}.review_state SET verdicts = verdicts
            RETURNING endorsed_through, open_findings + open_integrity INTO endorsed, open_n;
        -- any outstanding finding stops publication, including one raised AFTER its generations
        -- were endorsed (an empty-range finding about reviewed data)
        IF open_n > 0 THEN
            RAISE EXCEPTION 'nekaise: publication is withheld while % review finding(s) are open',
                open_n USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        SELECT max(o.seq) INTO top FROM {s}.outbox o WHERE o.generation <= endorsed;
        IF endorsed IS NULL OR NEW.watermark > COALESCE(top, OLD.watermark) THEN
            RAISE EXCEPTION 'nekaise: publication may not pass the endorsed generation %',
                endorsed USING ERRCODE = 'integrity_constraint_violation';
        END IF;
    END IF;
    RETURN NEW;
END $f$;
CREATE OR REPLACE TRIGGER outbox_consumers_publication BEFORE UPDATE ON {s}.outbox_consumers
    FOR EACH ROW EXECUTE FUNCTION {s}.nk_publication_guard();
-- backfill from the review consumer's acknowledgements (all 'ok' up to its watermark)
DO $d$ DECLARE w bigint; bad bigint; top bigint; BEGIN
    IF NOT EXISTS (SELECT 1 FROM {s}.review_state) THEN
        SELECT watermark INTO w FROM {s}.outbox_consumers WHERE consumer = 'review';
        SELECT min(a.seq) INTO bad FROM {s}.outbox_acks a WHERE a.consumer = 'review'
            AND a.seq <= COALESCE(w, 0) AND a.verdict <> 'ok';
        IF bad IS NOT NULL THEN
            RAISE EXCEPTION 'nekaise: legacy review acknowledgement % is not ok: migrate by hand',
                bad USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        SELECT max(o.generation) INTO top FROM {s}.outbox o WHERE o.seq <= COALESCE(w, 0);
        IF top IS NULL AND COALESCE(w, 0) > 0 THEN
            -- every reviewed row was compacted: one outbox row per generation, from 1
            top := w - 1;
        END IF;
        INSERT INTO {s}.review_state (reviewed_through, endorsed_through, updated_at)
            VALUES (top, top, now());
    END IF;
END $d$;
CREATE OR REPLACE TRIGGER review_state_guard BEFORE UPDATE OR DELETE ON {s}.review_state
    FOR EACH ROW EXECUTE FUNCTION {s}.nk_review_state_guard();
"""
V7_TABLES = ("run_adoptions", "purge_queue", "review_state", "review_verdicts")


def _migrate_7(conn, schema):  # stage 4 step 4 recovery and review: additive, see V7_DDL
    conn.execute(V7_DDL.format(s=schema))


MIGRATIONS = {2: _migrate_2, 3: _migrate_3, 4: _migrate_4, 5: _migrate_5, 6: _migrate_6,
              7: _migrate_7}
# Indexes on columns that migrations may have just added: created after migrating.
POST_DDL = "CREATE INDEX IF NOT EXISTS manifest_legacy_order ON {s}.manifest (shard, topic_key, id);"
# store.open()'s marker for a root without an authority record: the schema must not be
# PostgreSQL-authoritative (it may be a shadow or a scratch schema).
UNBOUND = "unbound"


def put_rows(cur, table: str, rows: list[dict], texts: list[str] | None = None) -> None:
    """Upsert entries/manifest rows with every derived column (the one writer the store, the
    shadow replicator and the staging fold use). `texts`: the rows' stored canonical text, written
    verbatim (the fold copies revisions; nothing is re-serialized)."""
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
    for i, r in enumerate(rows):
        store.validate_json(r, f"{table} row {r.get('id')!r}")
        rec = [r["id"], canonical_row(r) if texts is None else texts[i], *_keys_for(r)]
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
                 create: bool = True, authority=None):
        """`authority`: None for tools and tests that address a schema directly (pg_shadow,
        conformance tests; the architectural test limits who may); UNBOUND when store.open()
        serves a root without an authority record (the schema must not be PostgreSQL-
        authoritative); or the root's store_authority.Record (the schema must be authoritative
        for exactly that dataset and epoch — checked now and inside every write transaction)."""
        if not schema.isidentifier():
            raise StoreError(f"invalid schema name {schema!r}")
        self.root = Path(root)
        self.dsn = dsn
        self.schema = schema
        self._s = sql.Identifier(schema)
        self._writers: dict[str, psycopg.Connection] = {}
        self._active = False
        self._authority = authority
        if create:
            with self._connect(autocommit=True) as conn:
                fresh = conn.execute("SELECT to_regclass(%s) IS NULL",
                                     [f"{schema}.state"]).fetchone()[0]
                if fresh:  # nobody can hold a writer on a schema that does not exist yet
                    conn.execute("SELECT pg_advisory_lock(hashtext(%s))", [self._lock_name()])
                    fresh = conn.execute("SELECT to_regclass(%s) IS NULL",
                                         [f"{schema}.state"]).fetchone()[0]
                if fresh:
                    with conn.transaction():
                        conn.execute(DDL.format(s=schema, v=SCHEMA_VERSION))
                        conn.execute(V4_DDL.format(s=schema))
                        conn.execute(V5_DDL.format(s=schema))
                        conn.execute(V6_DDL.format(s=schema))
                        conn.execute(V7_DDL.format(s=schema))
                else:
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
        if authority is not None:
            with self._connect(autocommit=True) as conn:
                self._check_authority(conn)

    # -- authority (ADR 0001 stage 4 step 1; host half: scripts/store_authority.py) ----------------

    def _check_authority(self, conn: psycopg.Connection) -> None:
        """Raise AuthorityError unless this schema may serve this opener (see __init__)."""
        if self._authority is None:
            return
        row = conn.execute("SELECT dataset_uuid::text, authority_mode, authority_epoch "
                           "FROM dataset").fetchone()
        if row is None:
            raise store.AuthorityError(f"schema {self.schema} has no dataset row")
        uuid_, mode, epoch = row
        if self._authority == UNBOUND:
            if mode != "file":
                raise store.AuthorityError(
                    f"schema {self.schema} is PostgreSQL-authoritative (epoch {epoch}) but "
                    f"{self.root} has no authority record binding it: refusing")
            return
        rec = self._authority
        if (mode, uuid_, epoch) != ("postgres", rec.dataset_uuid, rec.epoch):
            raise store.AuthorityError(
                f"schema {self.schema} says mode {mode!r}, dataset {uuid_}, epoch {epoch}; the "
                f"authority record for {rec.root} says postgres, dataset {rec.dataset_uuid}, "
                f"epoch {rec.epoch}: refusing")

    def authority(self) -> dict:
        """The dataset row: UUID, authority mode/epoch/root, current generation."""
        with self._connect(autocommit=True) as conn:
            row = conn.execute("SELECT dataset_uuid::text, authority_mode, authority_epoch, "
                               "authority_root, current_generation FROM dataset").fetchone()
        return dict(zip(("dataset_uuid", "mode", "epoch", "root", "current_generation"), row))

    def set_authority(self, mode: str, *, root: Path | str | None, reason: str,
                      timeout: float = 30) -> int:
        """Move the database half of the authority record to `mode` under a new epoch (logged),
        holding the writer lock so no write transaction straddles the change. Returns the new
        epoch; the host record (store_authority.write_record) must then carry the same epoch.
        Cutover tooling (stage 4 step 6) and tests only."""
        if mode not in ("file", "postgres"):
            raise StoreError(f"unknown authority mode {mode!r}")
        if not reason:
            raise StoreError("an authority change needs a reason")
        root_s = str(Path(root).resolve()) if root is not None else None
        with self.writer(timeout=timeout) as w:
            conn = self._writer_conn(w)
            with conn.transaction():
                epoch = conn.execute("SELECT authority_epoch FROM dataset FOR UPDATE"
                                     ).fetchone()[0] + 1
                conn.execute("INSERT INTO authority_log (epoch, mode, root, reason) "
                             "VALUES (%s, %s, %s, %s)", [epoch, mode, root_s, reason])
                conn.execute("UPDATE dataset SET authority_mode = %s, authority_epoch = %s, "
                             "authority_root = %s, authority_changed_at = now()",
                             [mode, epoch, root_s])
        return epoch

    @contextmanager
    def contracts(self, writer: WriterToken) -> Iterator["Contracts"]:
        """One fenced transaction over the stage-4 contract tables (runs, batch receipts, config
        sets, artifacts, outbox acknowledgements). Stage 4 step 2 builds staging on it."""
        conn = self._writer_conn(writer)
        if self._active:
            raise StoreError("transactions do not nest")
        self._active = True
        try:
            with conn.transaction():
                self._fence(conn, writer)
                yield Contracts(conn, writer)
        finally:
            self._active = False

    def _fence(self, conn: psycopg.Connection, writer: WriterToken) -> int:
        """Inside a write transaction: lock the state row, refuse a stale writer, a schema this
        code does not write, and a changed authority. Returns the store generation counter."""
        gen, epoch, version = conn.execute(
            "SELECT generation, writer_epoch, schema_version FROM state FOR UPDATE").fetchone()
        if epoch != writer.epoch:
            raise WriterError("writer token is stale: a newer writer took over")
        if version != SCHEMA_VERSION:
            raise WriterError(f"schema {self.schema} is version {version}; this code writes "
                              f"version {SCHEMA_VERSION} — restart with matching code")
        self._check_authority(conn)
        return gen

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
            path = store.config_path(name, self.root)
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
        """A snapshot-consistent view (REPEATABLE READ, read only) on its own connection, pinned
        at the committed generation G (stage 4 step 2: the projection plus the revisions of runs
        promoted since it was folded). A pipeline child of a staged round
        (store_staging.STAGE_ENV) reads its run's overlay instead; a writer's own view never
        does (the coordinator reads its run with read_staged)."""
        import store_staging
        if writer is None and (pin := store_staging.pin_from_env()) is not None:
            with store_staging.read_staged(self, pin.run_id, seq=pin.seq,
                                           token=pin.token) as view:
                yield view
            return
        if writer is not None:
            self._writer_conn(writer)
        conn = self._connect()
        try:
            conn.execute("BEGIN ISOLATION LEVEL REPEATABLE READ READ ONLY")
            gen = conn.execute("SELECT generation FROM state").fetchone()[0]
            visibility, config, head = store_staging.committed(conn)
            view = PgReadView(self, conn, Version(f"pg:{gen}"), config or self._config(conn),
                              visibility=visibility, generation=head)
            try:
                yield view
            finally:
                view._closed = True
        finally:
            conn.rollback()
            conn.close()

    # -- staging and promotion (stage 4 step 2; scripts/store_staging.py) -----------------------

    def open_run(self, writer: WriterToken, run_id: str, **identity):
        import store_staging
        return store_staging.open_run(self, writer, run_id, **identity)

    def read_staged(self, run_id: str, **kw):
        import store_staging
        return store_staging.read_staged(self, run_id, **kw)

    def read_generation(self, generation: int):
        import store_staging
        return store_staging.read_generation(self, generation)

    def stage_batch(self, writer: WriterToken, run_id: str, step: str, batch: str,
                    requests: Sequence[Mapping], **kw):
        import store_staging
        return store_staging.stage_batch(self, writer, run_id, step, batch, requests, **kw)

    def batch_receipt(self, writer: WriterToken, run_id: str, step: str, batch: str):
        import store_staging
        return store_staging.batch_receipt(self, writer, run_id, step, batch)

    def freeze(self, writer: WriterToken, run_id: str, **kw):
        import store_staging
        return store_staging.freeze(self, writer, run_id, **kw)

    def record_gate(self, writer: WriterToken, frozen, gate: str, **kw) -> None:
        import store_staging
        store_staging.record_gate(self, writer, frozen, gate, **kw)

    def promote(self, writer: WriterToken, frozen) -> int:
        import store_staging
        return store_staging.promote(self, writer, frozen)

    def abort_run(self, writer: WriterToken, run_id: str, **kw) -> None:
        import store_staging
        store_staging.abort_run(self, writer, run_id, **kw)

    def fold(self, writer: WriterToken, **kw):
        import store_staging
        return store_staging.fold(self, writer, **kw)

    # -- artifact versions (stage 4 step 3; scripts/artifact_store.py) ---------------------------

    def unverified_run_artifacts(self, writer: WriterToken, run_id: str, *,
                                 after: tuple[str, str] = ("", ""),
                                 limit: int = 1000) -> list[tuple]:
        """The artifacts run `run_id` referenced that still need verifying, after keyset
        position (stage, sha256): [(stage, sha256, size, locator)] — every reference whose
        canonical local locator was never verified, AND every reference with no canonical local
        locator at all (locator None, size possibly None): verification must fail on those, never
        skip them."""
        import store_staging
        with store_staging._writer_txn(self, writer) as conn:
            return [tuple(r) for r in conn.execute(
                # the run's references in key order, each probed by key (never a join scan of
                # the global artifacts/locators tables)
                "SELECT ra.stage, ra.sha256, (SELECT a.size FROM artifacts a WHERE a.stage = "
                "ra.stage AND a.sha256 = ra.sha256), l.locator FROM run_artifacts ra "
                "LEFT JOIN LATERAL (SELECT x.locator, x.verified_at FROM artifact_locators x "
                "WHERE x.stage = ra.stage AND x.sha256 = ra.sha256 AND x.kind = 'local' AND "
                "x.locator = nk_local_locator(ra.stage, ra.sha256) OFFSET 0) l ON true "
                "WHERE ra.run_id = %s AND (ra.stage, ra.sha256) > (%s, %s) AND "
                "(l.locator IS NULL OR l.verified_at IS NULL) "
                "ORDER BY ra.stage, ra.sha256 LIMIT %s",
                [run_id, after[0], after[1], limit]).fetchall()]

    def mark_verified(self, writer: WriterToken, items: Sequence[tuple[str, str, str]]) -> int:
        """Record that locators (stage, sha256, locator) were re-hashed just now."""
        if not items:
            return 0
        import store_staging
        with store_staging._writer_txn(self, writer) as conn:
            return conn.execute(
                "UPDATE artifact_locators l SET verified_at = now() FROM unnest(%s::text[], "
                "%s::text[], %s::text[]) AS u(stage, sha256, locator) WHERE l.stage = u.stage "
                "AND l.sha256 = u.sha256 AND l.locator = u.locator",
                [[i[0] for i in items], [i[1] for i in items], [i[2] for i in items]]).rowcount

    def peek(self, table: str):
        """store.FileStore.peek: a committed snapshot needs no lock here."""
        if table not in store.PEEK_TABLES:
            raise store.StoreError(f"peek: {table!r} is not a small table")
        with self.read() as view:
            if table == "rotation":
                return view.rotation_get()
            if table == "control":
                return {n: d for n in store.CONTROL_FILES if (d := view.control_get(n)) is not None}
            if table == "backend_state":
                return {n: {"enabled": s.enabled, "reason": s.reason}
                        for n, s in view.backend_state_get().items()
                        if (s.enabled, s.reason) != (True, None)}
            out, cursor = set(), None
            while True:
                page = view.scan(store.Table.BLOCKLIST, cursor=cursor, limit=store.MAX_PAGE)
                out.update(r["url"] for r in page.rows)
                if (cursor := page.next_cursor) is None:
                    return out

    def config_documents(self) -> dict:
        with self._connect(autocommit=True) as conn:
            return self._config(conn).documents

    def validate_layout(self) -> tuple[list[str], dict]:
        """Physical checks: the schema matches this code. (Row-level consistency of derived
        columns is pg_shadow verify's job while the shadow exists.)"""
        with self._connect(autocommit=True) as conn:
            version = conn.execute("SELECT schema_version FROM state").fetchone()[0]
            entries = conn.execute("SELECT count(*) FROM entries").fetchone()[0]
        errors = [] if version == SCHEMA_VERSION else [
            f"schema {self.schema} is version {version}, code expects {SCHEMA_VERSION}"]
        return errors, {"shards": None, "entries": entries}

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
                gen = self._fence(conn, writer)
                import store_staging
                if why := store_staging.legacy_writes_refused(conn):
                    raise StoreError(f"direct store transactions are refused: {why} (stage its "
                                     "batches in a run)")
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
        rows = store.journal_events(view._ops, view.run_id, at, seq, digest)
        with conn.cursor() as cur:
            cur.executemany("INSERT INTO events (seq, run_id, op, row_text) VALUES (%s,%s,%s,%s)",
                            [(r["seq"], r["run_id"], r["op"], _check_text(canonical_row(r), "event"))
                             for r in rows])

    def export(self, directory: Path, *, view: "PgReadView"):
        return store.export(directory, view=view)


# --- stage-4 contracts ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BatchReceipt:
    run_id: str
    step: str
    batch: str
    request_digest: str
    status: str          # requested | applied | abandoned
    seq: int | None      # the run staging sequence it produced, once applied
    retried: bool        # True: this identity was already requested with the same digest


RUN_IDENTITY = ("kind", "parent_generation", "producer_commit", "config_digest",
                "extractor_version", "cleaning_ruleset")
ARTIFACT_POLICIES = ("versioned", "unchecked")


def require_run_owner(conn: psycopg.Connection, run_id: str, writer: WriterToken,
                      what: str) -> None:
    """The ONE ownership check of every owner-only run operation (staging, requesting, applying
    or abandoning batches, reading a run's staging as its writer, freezing, gates, promotion):
    the run exists and `writer` is its owner — the writer epoch that opened it, or the one that
    adopted it last (schema v7, run_adoptions). Locks the run row. Cross-owner
    operations are separate and named as such (store_staging.abort_run / purge_run). This is a
    consistency guard for the trusted single-host model (ADR 0001: one writer at a time,
    fenced by the advisory lock and epoch), not a security boundary: any database client can
    bypass it."""
    row = conn.execute("SELECT owner_epoch FROM runs WHERE run_id = %s FOR UPDATE",
                       [run_id]).fetchone()
    if row is None:
        raise StoreError(f"unknown run {run_id}")
    if row[0] != writer.epoch:
        raise WriterError(f"run {run_id} belongs to writer epoch {row[0]}; this writer is epoch "
                          f"{writer.epoch} ({what} refused; a new owner either aborts the run — "
                          "abort_run, the explicit cross-owner operation — or resumes it through "
                          "a logged adoption, store_staging.adopt_run)")


class Contracts:
    """Stage-4 contract operations inside one fenced PgStore transaction (PgStore.contracts).
    Every rule the tables state is also enforced by triggers; these helpers add exact-retry
    semantics: the same identity with the same content is a no-op, different content raises."""

    def __init__(self, conn: psycopg.Connection, writer: WriterToken):
        self._conn = conn
        self._writer = writer

    def _q(self, query, params=()):
        return self._conn.execute(query, params)

    def dataset(self) -> dict:
        row = self._q("SELECT dataset_uuid::text, authority_mode, authority_epoch, "
                      "current_generation FROM dataset").fetchone()
        return dict(zip(("dataset_uuid", "mode", "epoch", "current_generation"), row))

    def put_config_set(self, documents: Mapping[str, bytes]) -> str:
        """Store configuration documents as exact bytes; returns the set digest
        (store._digest of {name: sha256})."""
        members = {}
        for name, data in sorted(documents.items()):
            if not isinstance(data, (bytes, bytearray)):
                raise StoreError(f"config {name}: exact bytes required")
            sha = hashlib.sha256(data).hexdigest()
            members[name] = sha
            self._q("INSERT INTO config_blobs (sha256, bytes) VALUES (%s, %s) "
                    "ON CONFLICT DO NOTHING", [sha, bytes(data)])
        text = store._canonical(members)
        digest = hashlib.sha256(text.encode()).hexdigest()  # == store._digest(members)
        # created and sealed here, in this one transaction (the database checks the digest, the
        # canonical form and, at commit, that every member row exists)
        if self._q("INSERT INTO config_sets (digest, members_text) VALUES (%s, %s) "
                   "ON CONFLICT DO NOTHING", [digest, text]).rowcount:
            with self._conn.cursor() as cur:
                cur.executemany("INSERT INTO config_set_members (digest, name, sha256) "
                                "VALUES (%s, %s, %s)", [(digest, n, h) for n, h in members.items()])
        return digest

    def config_set(self, digest: str) -> dict[str, bytes]:
        return {n: bytes(b) for n, b in self._q(
            "SELECT m.name, b.bytes FROM config_set_members m JOIN config_blobs b USING (sha256) "
            "WHERE m.digest = %s ORDER BY m.name", [digest])}

    def open_run(self, run_id: str, *, kind: str, parent_generation: int | None,
                 producer_commit: str, config_digest: str, extractor_version: str,
                 cleaning_ruleset: str, artifact_policy: str = "versioned") -> str:
        """Open a run staged on the current generation. Returns its status; an existing run
        with the same identity is returned as is (exact retry), a different one raises.
        `artifact_policy` (schema v6): "versioned" — the database checks every payload claim
        the run's manifest rows make (V6_DDL) — or "unchecked" (metadata-only tests)."""
        store._check_run_id(run_id)
        if artifact_policy not in ARTIFACT_POLICIES:
            raise StoreError(f"artifact policy must be one of {ARTIFACT_POLICIES}")
        want = dict(zip(RUN_IDENTITY + ("artifact_policy",),
                        (kind, parent_generation, producer_commit, config_digest,
                         extractor_version, cleaning_ruleset, artifact_policy)))
        cols = ", ".join(want)
        row = self._q(f"SELECT status, {cols} FROM runs WHERE run_id = %s FOR UPDATE",
                      [run_id]).fetchone()
        if row is not None:
            if dict(zip(want, row[1:])) != want:
                raise StoreError(f"run {run_id} already exists with a different identity")
            return row[0]
        ds = self.dataset()
        if parent_generation != ds["current_generation"]:
            raise VersionConflict(f"run {run_id} would stage on generation {parent_generation}; "
                                  f"the current generation is {ds['current_generation']}")
        self._q(f"INSERT INTO runs (run_id, authority_epoch, writer_epoch, {cols}) VALUES "
                "(%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [run_id, ds["epoch"], self._writer.epoch, *want.values()])
        return "open"

    def request_batch(self, run_id: str, step: str, batch: str,
                      requests: Sequence[Mapping]) -> BatchReceipt:
        """Persist a batch's computed request (canonical JSON text) before it is applied. Same
        identity + same digest: the existing receipt (exact retry, whatever its status); a
        different digest: StoreError (conflicting retry)."""
        require_run_owner(self._conn, run_id, self._writer, "requesting a batch")
        store.validate_json(list(requests), f"batch {run_id}.{step}.{batch}")
        text = _check_text(store._canonical(list(requests)), "batch request")
        digest = hashlib.sha256(text.encode()).hexdigest()
        row = self._q("SELECT request_digest, status, seq FROM batches WHERE run_id = %s AND "
                      "step = %s AND batch = %s", [run_id, step, batch]).fetchone()
        if row is not None:
            if row[0] != digest:
                raise StoreError(f"batch {run_id}.{step}.{batch} was requested with different "
                                 "content (conflicting retry)")
            return BatchReceipt(run_id, step, batch, digest, row[1], row[2], True)
        # computed at the run's current staging sequence (schema v5 applies it right after it)
        if self._q("INSERT INTO batches (run_id, step, batch, request_digest, request_text, "
                   "basis_seq) SELECT %s, %s, %s, %s, %s, staged_seq FROM runs WHERE run_id = %s",
                   [run_id, step, batch, digest, text, run_id]).rowcount != 1:
            raise StoreError(f"unknown run {run_id}")
        return BatchReceipt(run_id, step, batch, digest, "requested", None, False)

    def register_artifact(self, stage: Stage | str, sha256: str, size: int, *,
                          locator: str | None = None, kind: str = "local",
                          first_run: str | None = None) -> bool:
        """Record artifact identity (stage, sha256) and optionally a locator; True if new. The
        same identity with a different size raises (identity is immutable)."""
        stage = Stage(stage).value
        if locator is not None and kind == "local":
            import artifact_store
            if locator != artifact_store.local_locator(stage, sha256):
                raise StoreError("a local locator is the content address "
                                 f"{artifact_store.local_locator(stage, sha256)}, not {locator}")
        new = self._q("INSERT INTO artifacts (stage, sha256, size, first_run) VALUES "
                      "(%s, %s, %s, %s) ON CONFLICT DO NOTHING",
                      [stage, sha256, size, first_run]).rowcount == 1
        if not new:
            have = self._q("SELECT size FROM artifacts WHERE stage = %s AND sha256 = %s",
                           [stage, sha256]).fetchone()[0]
            if have != size:
                raise StoreError(f"artifact {stage}:{sha256} has size {have}, not {size}")
        if locator is not None:
            self._q("INSERT INTO artifact_locators (stage, sha256, locator, kind) VALUES "
                    "(%s, %s, %s, %s) ON CONFLICT DO NOTHING", [stage, sha256, locator, kind])
        return new

    def ack(self, consumer: str, seq: int, verdict: str, detail: Mapping | None = None) -> None:
        """Acknowledge outbox row `seq` for `consumer` (exact retry is a no-op)."""
        text = store._canonical(dict(detail or {}))
        row = self._q("SELECT verdict, detail_text FROM outbox_acks WHERE consumer = %s AND "
                      "seq = %s", [consumer, seq]).fetchone()
        if row is not None:
            if tuple(row) != (verdict, text):
                raise StoreError(f"{consumer} already acknowledged outbox row {seq} differently")
            return
        self._q("INSERT INTO outbox_acks (consumer, seq, verdict, detail_text) VALUES "
                "(%s, %s, %s, %s)", [consumer, seq, verdict, text])

    def advance(self, consumer: str) -> int:
        """Move `consumer`'s watermark over its contiguous acknowledged prefix; returns it."""
        w = self._q("SELECT watermark FROM outbox_consumers WHERE consumer = %s FOR UPDATE",
                    [consumer]).fetchone()
        if w is None:
            raise StoreError(f"unknown outbox consumer {consumer!r}")
        gap = self._q("SELECT min(o.seq) FROM outbox o WHERE o.seq > %s AND NOT EXISTS (SELECT 1 "
                      "FROM outbox_acks a WHERE a.consumer = %s AND a.seq = o.seq)",
                      [w[0], consumer]).fetchone()[0]
        top = gap - 1 if gap is not None else \
            self._q("SELECT allocated FROM outbox_state").fetchone()[0]
        if top > w[0]:
            self._q("UPDATE outbox_consumers SET watermark = %s, updated_at = now() "
                    "WHERE consumer = %s", [top, consumer])
        return max(top, w[0])

    def outbox_marks(self) -> tuple[int, int]:
        """(allocated, compacted): the highest outbox sequence ever issued (the next row takes
        allocated + 1) and the compacted prefix."""
        return tuple(self._q("SELECT allocated, compacted FROM outbox_state").fetchone())

    def watermarks(self) -> dict[str, int]:
        return dict(self._q("SELECT consumer, watermark FROM outbox_consumers ORDER BY consumer"))


# --- read view -----------------------------------------------------------------------------------

class PgReadView:
    """A snapshot view. `visibility` (store_staging.Visibility) overlays staged or promoted-but-
    unfolded revisions on the projection tables; every query reads the visible rows through
    _src(table), so the same SQL serves the projection alone (visibility None: the table itself)
    and any overlay. `generation` is the committed generation the view pins (None before the
    first one), `stage` the (run, sequence) of a staging view."""

    def __init__(self, st: PgStore, conn: psycopg.Connection, version: Version,
                 config: ConfigSnapshot, *, visibility=None, generation: int | None = None,
                 stage: tuple[str, int] | None = None):
        self._store = st
        self._conn = conn
        self._version = version
        self._config = config
        self._id = uuid.uuid4().hex
        self._closed = False
        self._visibility = visibility
        self._sources: dict[str, sql.Composable] = {}
        self.generation = generation
        self.stage = stage

    def _src(self, table: str) -> sql.Composable:
        """The visible rows of a projection table, as a FROM item named like the table (lookups,
        membership, aggregates; keyset scans build their own ordered query, Visibility.scan)."""
        if self._visibility is None:
            return sql.Identifier(table)
        if table not in self._sources:
            self._sources[table] = self._visibility.source(table)
        return self._sources[table]

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
        if self._visibility is not None and table is not Table.EVENTS:
            # rows are (keys..., text) — for the blocklist (keys..., url), like the query below
            got = self._visibility.scan(self._q, table.value, order, where,
                                        cursor.last_key if cursor else None, limit + 1)
            return self._page(table, keys, fields, got, limit, query_id)
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
        return self._page(table, keys, fields, got, limit, query_id)

    def _page(self, table: Table, keys: tuple, fields, got: list, limit: int,
              query_id: str) -> Page:
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
        found = dict(self._q(sql.SQL("SELECT id, row_text FROM {} WHERE id = ANY(%s)").format(
            self._src("manifest")), [ids]).fetchall())
        return {i: json.loads(found[i]) for i in ids if i in found}

    def get_entries(self, ids: Iterable[str]) -> dict[str, dict]:
        ids = list(dict.fromkeys(ids))
        found = dict(self._q(sql.SQL("SELECT id, row_text FROM {} WHERE id = ANY(%s)").format(
            self._src("entries")), [ids]).fetchall())
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
        e, m = self._src("entries"), self._src("manifest")
        hit_u = {r[0] for r in self._q(sql.SQL(
            "SELECT url_norm FROM {} WHERE url_key = ANY(%s) "
            "UNION SELECT url_norm FROM {} WHERE url_key = ANY(%s)").format(e, m), [ukeys, ukeys])}
        if include_blocklist:
            hit_u |= {r[0] for r in self._q(sql.SQL("SELECT url FROM {} WHERE key = ANY(%s)")
                                            .format(self._src("blocklist")),
                                            [[key_digest(u) for u in cand_u]])}
        hit_t = {r[0] for r in self._q(sql.SQL(
            "SELECT title_norm FROM {} WHERE title_key = ANY(%s) "
            "UNION SELECT title_norm FROM {} WHERE title_key = ANY(%s)").format(e, m),
            [tkeys, tkeys])}
        hit_i = {r[0] for r in self._q(sql.SQL(
            "SELECT id FROM {} WHERE id = ANY(%s) UNION SELECT id FROM {} "
            "WHERE id = ANY(%s)").format(e, m), [list(cand_i), list(cand_i)])}
        # digest hits are verified against the full value
        return KnownHits(frozenset(hit_u & cand_u), frozenset(hit_t & cand_t),
                         frozenset(hit_i & cand_i))

    def known_pids(self, pids: Iterable[str]) -> frozenset:
        """store.ReadView.known_pids over the rows' JSON. A pure lookup, no schema change: the
        candidate rows are found with an UNINDEXED expression match (a sequential scan of both
        tables per call; callers batch their pids) and verified exactly in Python with
        codec.row_pids on the NATIVE JSON values (`->`, so an origin_ids list stays a list).
        The SQL filter only ever WIDENS: case-folded substring matches on the JSON text of
        persistent_id and origin_ids, so whitespace, doi.org/doi: prefixes, arrays and
        strings all reach the exact check. Indexed PID membership is required before PostgreSQL
        becomes authoritative (docs/decisions/0001-storage-architecture.md)."""
        self._check_open()
        cand = {p for p in pids if p and store.codec.normalize_pid(p) == p}
        if len(cand) > store.MAX_KNOWN:
            raise StoreError(f"known_pids(): at most {store.MAX_KNOWN} per call")
        if not cand:
            return frozenset()
        # "%value%" over the lower-cased JSON text. LIKE wildcards in a DOI ("_", "%") only
        # widen; JSON escapes a backslash (and a quote) as two characters, so each of them —
        # and LIKE's own escape character — becomes "%".
        likes = sorted({"%" + "".join("%" if ch in '\\"' else ch
                                      for ch in p.split(":", 1)[1].lower()) + "%"
                        for p in cand})
        hits: set = set()
        for table in ("entries", "manifest"):
            rows = self._q(sql.SQL(
                "SELECT row->'persistent_id', row->'origin_ids' FROM {} "
                "WHERE lower((row->'persistent_id')::text) LIKE ANY(%s) "
                "OR lower((row->'origin_ids')::text) LIKE ANY(%s)").format(self._src(table)),
                [likes, likes]).fetchall()
            for persistent_id, origin_ids in rows:
                hits.update(cand.intersection(store.codec.row_pids(
                    {"persistent_id": persistent_id, "origin_ids": origin_ids})))
        return frozenset(hits)

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
        q = sql.SQL("SELECT {cols} FROM {src} WHERE {cond}{grp}").format(
            cols=sql.SQL(", ").join(cols), cond=cond, src=self._src("manifest"),
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
                    "(PARTITION BY sha256) AS c FROM {} WHERE sha256 IS NOT NULL AND ({})) t "
                    "WHERE c > 1 ORDER BY sha256, id").format(self._src("manifest"), cond)
        self._check_open()
        with self._conn.cursor(name=f"dup_{uuid.uuid4().hex}") as cur:
            cur.itersize = batch_size
            cur.execute(q, params)
            for (text,) in cur:
                yield json.loads(text)

    def rotation_get(self, name: str | None = None) -> dict:
        rows = {n: json.loads(t) for n, t in self._q(sql.SQL("SELECT name, value_text FROM {}")
                                                     .format(self._src("rotation")))}
        return rows if name is None else rows[name]

    def control_get(self, name: str) -> dict | None:
        if name not in store.CONTROL_FILES:
            raise StoreError(f"unknown control document {name!r}")
        row = self._q(sql.SQL("SELECT doc_text FROM {} WHERE name = %s").format(
            self._src("control_docs")), [name]).fetchone()
        return json.loads(row[0]) if row else None

    def config_get(self) -> ConfigSnapshot:
        self._check_open()
        return ConfigSnapshot(json.loads(json.dumps(self._config.documents)),
                              dict(self._config.digests))

    def backend_state_get(self, name: str | None = None):
        runtime = {n: BackendState(e, r) for n, e, r in
                   self._q(sql.SQL("SELECT name, enabled, reason FROM {}").format(
                       self._src("backend_state")))}
        names = [k for k in self._config.backends if not k.startswith("_")]
        states = {k: runtime.get(k, BackendState()) for k in sorted(set(names) | set(runtime))}
        return states if name is None else states.get(name, BackendState())

    def backend_enabled(self, name: str) -> bool:
        cfg = self._config.backends.get(name)
        if cfg is None or name.startswith("_"):
            raise StoreError(f"unknown backend {name}")
        return bool(cfg.get("enabled", True)) and self.backend_state_get(name).enabled

    def resolve_artifact(self, id: str, stage: Stage):  # noqa: A002
        """Resolved through the view's membership: the visible row's claim (path, sha256). The
        identity decides, never the logical path: the immutable version at its canonical
        content address when this machine holds it (registered or not — an adopted legacy file
        is held there before its path is reused), else the registered local locator (schema
        v6), else the claim's legacy path (store.artifact_ref). artifact_store.VersionedAccess
        resolves in the same order."""
        return self.resolve_artifacts([id], stage).get(id)

    def resolve_artifacts(self, ids: Sequence[str], stage: Stage) -> dict:
        """resolve_artifact for up to MAX_KNOWN ids in two queries: {id: ArtifactRef}."""
        import artifact_store
        stage = Stage(stage)
        ids = list(dict.fromkeys(ids))
        if len(ids) > store.MAX_KNOWN:
            raise StoreError(f"resolve at most {store.MAX_KNOWN} ids per call")
        refs = {i: ref for i, row in self.get_manifest(ids).items()
                if (ref := store.artifact_ref(row, i, stage)) is not None}
        local = artifact_store.LocalArtifacts(self._store.root)
        held, rest = {}, set()
        for ref in refs.values():
            sha = ref.sha256
            if isinstance(sha, str) and artifact_store.is_identity(sha):
                size = local.size(stage.value, sha)
                if size is not None:
                    held[sha] = (artifact_store.local_locator(stage.value, sha), size)
                else:
                    rest.add(sha)
        found = dict(held)
        if rest:
            found.update({sha: (loc, size) for sha, loc, size in self._q(
                "SELECT DISTINCT ON (a.sha256) a.sha256, l.locator, a.size FROM artifacts a "
                "JOIN artifact_locators l USING (stage, sha256) WHERE a.stage = %s AND "
                "a.sha256 = ANY(%s) AND l.kind = 'local' ORDER BY a.sha256, l.locator",
                [stage.value, sorted(rest)])})
        for i, ref in refs.items():
            if (hit := found.get(ref.sha256)) is not None:
                refs[i] = store.ArtifactRef(i, stage, f"file:{hit[0]}", ref.sha256, hit[1])
        return refs

    def provenance(self) -> dict | None:
        """What this view pins (stage 4): the dataset, the committed generation and — for a
        staging view — the run's recorded provenance (cleaning ruleset, extractor version,
        artifact policy); for a committed view the provenance of generation G's run. None
        before the first generation outside a run."""
        if self.stage is not None:
            row = self._q("SELECT r.run_id, r.cleaning_ruleset, r.extractor_version, "
                          "r.artifact_policy, r.config_digest, d.dataset_uuid::text FROM runs r, "
                          "dataset d WHERE r.run_id = %s", [self.stage[0]]).fetchone()
        elif self.generation is not None:
            row = self._q("SELECT r.run_id, g.cleaning_ruleset, g.extractor_version, "
                          "r.artifact_policy, g.config_digest, d.dataset_uuid::text FROM "
                          "generations g JOIN runs r USING (run_id), dataset d WHERE "
                          "g.generation = %s", [self.generation]).fetchone()
        else:
            return None
        return {"run": row[0], "cleaning_ruleset": row[1], "extractor_version": row[2],
                "artifact_policy": row[3], "config_digest": row[4], "dataset": row[5],
                "generation": self.generation, "staging": self.stage is not None}


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
        self._ops.append(store.journal_op(table, op, sid, before, reason))

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
