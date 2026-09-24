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

SCHEMA_VERSION = 4
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
    compacted bigint NOT NULL DEFAULT 0 CHECK (compacted >= 0 AND compacted <= allocated)
);
INSERT INTO {s}.outbox_state DEFAULT VALUES ON CONFLICT DO NOTHING;
CREATE OR REPLACE FUNCTION {s}.nk_outbox_state_guard() RETURNS trigger LANGUAGE plpgsql AS $f$
BEGIN
    IF TG_OP = 'DELETE' OR NEW.allocated < OLD.allocated OR NEW.compacted < OLD.compacted THEN
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
    SELECT * INTO st FROM {s}.outbox_state FOR UPDATE;
    IF TG_OP = 'INSERT' THEN
        IF NEW.seq <> st.allocated + 1 THEN
            RAISE EXCEPTION 'nekaise: the next outbox sequence is % (got %)', st.allocated + 1,
                NEW.seq USING ERRCODE = 'integrity_constraint_violation';
        END IF;
        UPDATE {s}.outbox_state SET allocated = NEW.seq;
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
CREATE OR REPLACE FUNCTION {s}.nk_consumers_guard() RETURNS trigger LANGUAGE plpgsql AS $f$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'nekaise: outbox consumer % is retained', OLD.consumer
            USING ERRCODE = 'integrity_constraint_violation';
    END IF;
    IF TG_OP = 'INSERT' THEN
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
    conn.execute(V4_DDL.format(s=schema))


MIGRATIONS = {2: _migrate_2, 3: _migrate_3, 4: _migrate_4}
# Indexes on columns that migrations may have just added: created after migrating.
POST_DDL = "CREATE INDEX IF NOT EXISTS manifest_legacy_order ON {s}.manifest (shard, topic_key, id);"
# store.open()'s marker for a root without an authority record: the schema must not be
# PostgreSQL-authoritative (it may be a shadow or a scratch schema).
UNBOUND = "unbound"


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
                 cleaning_ruleset: str) -> str:
        """Open a run staged on the current generation. Returns its status; an existing run
        with the same identity is returned as is (exact retry), a different one raises."""
        store._check_run_id(run_id)
        want = dict(zip(RUN_IDENTITY, (kind, parent_generation, producer_commit, config_digest,
                                       extractor_version, cleaning_ruleset)))
        cols = ", ".join(RUN_IDENTITY)
        row = self._q(f"SELECT status, {cols} FROM runs WHERE run_id = %s FOR UPDATE",
                      [run_id]).fetchone()
        if row is not None:
            if dict(zip(RUN_IDENTITY, row[1:])) != want:
                raise StoreError(f"run {run_id} already exists with a different identity")
            return row[0]
        ds = self.dataset()
        if parent_generation != ds["current_generation"]:
            raise VersionConflict(f"run {run_id} would stage on generation {parent_generation}; "
                                  f"the current generation is {ds['current_generation']}")
        self._q(f"INSERT INTO runs (run_id, authority_epoch, writer_epoch, {cols}) VALUES "
                "(%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                [run_id, ds["epoch"], self._writer.epoch, *want.values()])
        return "open"

    def request_batch(self, run_id: str, step: str, batch: str,
                      requests: Sequence[Mapping]) -> BatchReceipt:
        """Persist a batch's computed request (canonical JSON text) before it is applied. Same
        identity + same digest: the existing receipt (exact retry, whatever its status); a
        different digest: StoreError (conflicting retry)."""
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
        self._q("INSERT INTO batches (run_id, step, batch, request_digest, request_text) "
                "VALUES (%s, %s, %s, %s, %s)", [run_id, step, batch, digest, text])
        return BatchReceipt(run_id, step, batch, digest, "requested", None, False)

    def register_artifact(self, stage: Stage | str, sha256: str, size: int, *,
                          locator: str | None = None, kind: str = "local",
                          first_run: str | None = None) -> bool:
        """Record artifact identity (stage, sha256) and optionally a locator; True if new. The
        same identity with a different size raises (identity is immutable)."""
        stage = Stage(stage).value
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

    def get_entries(self, ids: Iterable[str]) -> dict[str, dict]:
        ids = list(dict.fromkeys(ids))
        found = dict(self._q("SELECT id, row_text FROM entries WHERE id = ANY(%s)", [ids]).fetchall())
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
