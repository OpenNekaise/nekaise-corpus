"""PostgreSQL stage-4 step-1 contracts (ADR 0001): schema v4 migration from the live shadow's
version, old-client rejection, the database half of the authority record, and the immutable
run/batch/revision/generation/outbox/artifact tables. Opt-in: NEKAISE_PG_TEST_DSN."""
import hashlib
import importlib.util
import json
import os
import subprocess
import uuid
from pathlib import Path

import pytest

import store
import store_authority
from store import AuthorityError, StoreError
import test_pg_shadow
from test_pg_shadow import manifest, mrow
from test_store_contract import seed, write, write_config

pytestmark = pytest.mark.skipif(not os.environ.get("NEKAISE_PG_TEST_DSN"),
                                reason="NEKAISE_PG_TEST_DSN not set")
DSN = os.environ.get("NEKAISE_PG_TEST_DSN", "")
REPO = Path(__file__).resolve().parents[1]
env = test_pg_shadow.env  # the shadow fixture: a git repository plus a fresh PgStore
V3_COMMIT = "b188e930bd"  # last commit whose store_pg.py writes schema version 3 (stage 3 end)
SHA = "0" * 40


def quiet(*_):
    pass


@pytest.fixture
def pg(tmp_path):
    import store_pg
    write_config(tmp_path / "pg")
    st = store_pg.PgStore(tmp_path / "pg", dsn=DSN, schema=f"c_{uuid.uuid4().hex[:12]}")
    st.pin_config_from_files()
    yield st
    st.drop()


def sqlerr():
    import psycopg
    return psycopg.IntegrityError  # triggers raise 23000; CHECK/FK violations are 235xx


def tables(st) -> set[str]:
    with st._connect(autocommit=True) as conn:
        return {r[0] for r in conn.execute(
            "SELECT table_name FROM information_schema.tables WHERE table_schema = %s",
            [st.schema])}


# --- migration from the live shadow's version -----------------------------------------------------

def _downgrade_to_v3(st) -> None:
    """Remove every stage-4 object: the schema the shadow runs today (version 3)."""
    import store_pg
    with st._connect(autocommit=True) as conn:
        for t in reversed(store_pg.V6_TABLES + store_pg.V5_TABLES + store_pg.V4_TABLES):
            conn.execute(f"DROP TABLE IF EXISTS {st.schema}.{t} CASCADE")
        for (fn,) in conn.execute("SELECT p.oid::regprocedure::text FROM pg_proc p JOIN "
                                  "pg_namespace n ON n.oid = p.pronamespace WHERE n.nspname = %s",
                                  [st.schema]).fetchall():
            conn.execute(f"DROP FUNCTION {fn}")
        conn.execute("UPDATE state SET schema_version = 3")


def _snapshot(st):
    """Every pre-v4 table's rows as stored (canonical text, derived columns, events)."""
    import pg_shadow
    watermark, digests = pg_shadow.pg_digests(st)
    with st._connect(autocommit=True) as conn:
        events = conn.execute("SELECT seq, run_id, op, row_text FROM events ORDER BY seq").fetchall()
        state = conn.execute("SELECT generation, writer_epoch, replication FROM state").fetchone()
        receipts = conn.execute("SELECT commit, parent, stats FROM replication_receipts "
                                "ORDER BY commit").fetchall()
    return watermark, digests, events, state, receipts


def test_v3_shadow_migrates_in_place_without_rewriting_anything(env):
    pg_shadow, st, repo, c1 = env
    import store_pg
    pg_shadow.do_import(st, c1, repo.path, log=quiet)
    repo.write("registry/journal/2026-09-24.jsonl", json.dumps(
        {"seq": 1, "run_id": "r", "op": "commit", "digest": "d", "v": 2}) + "\n")
    repo.write("manifest/books.jsonl", manifest([mrow("oer-a", text_chars=1e20), mrow("oer-b")]))
    c2 = repo.commit("c2")
    pg_shadow.do_sync(st, c2, repo.path, log=quiet)
    _downgrade_to_v3(st)
    assert not tables(st) & set(store_pg.V4_TABLES)
    before = _snapshot(st)

    migrated = store_pg.PgStore(repo.path, dsn=st.dsn, schema=st.schema)
    assert _snapshot(migrated) == before                # rows, events, watermark, receipts
    assert set(store_pg.V4_TABLES) <= tables(migrated)
    with migrated._connect(autocommit=True) as conn:
        assert conn.execute("SELECT schema_version FROM state").fetchone()[0] == store_pg.SCHEMA_VERSION
        assert conn.execute("SELECT consumer, watermark FROM outbox_consumers ORDER BY 1"
                            ).fetchall() == [("index", 0), ("projection", 0), ("publication", 0),
                                             ("review", 0)]
    auth = migrated.authority()
    assert auth["mode"] == "file" and auth["epoch"] == 1 and auth["current_generation"] is None
    uuid.UUID(auth["dataset_uuid"])
    assert pg_shadow.do_verify(migrated, repo.path, log=quiet)
    # re-opening does not re-run migration DDL or change the dataset identity
    again = store_pg.PgStore(repo.path, dsn=st.dsn, schema=st.schema)
    assert again.authority() == auth
    # the shadow keeps replicating after the migration
    repo.write("pruned_urls.txt", "https://e.org/old\nhttps://e.org/after-v4\n")
    c3 = repo.commit("c3")
    assert pg_shadow.do_sync(again, c3, repo.path, log=quiet) == 1
    assert pg_shadow.do_verify(again, repo.path, log=quiet)


def _v3_module():
    """store_pg.py exactly as the live shadow runs it (git object at V3_COMMIT)."""
    got = subprocess.run(["git", "-C", str(REPO), "show", f"{V3_COMMIT}:scripts/store_pg.py"],
                         capture_output=True)
    if got.returncode:
        pytest.skip(f"{V3_COMMIT} not in this clone")
    spec = importlib.util.spec_from_loader("store_pg_v3", loader=None)
    mod = importlib.util.module_from_spec(spec)
    exec(compile(got.stdout, "store_pg_v3.py", "exec"), mod.__dict__)  # noqa: S102
    assert mod.SCHEMA_VERSION == 3
    return mod


def test_real_v3_code_data_migrates_and_old_clients_are_rejected(tmp_path, monkeypatch):
    import store_pg
    fixed = lambda fmt, t=None: "2026-09-24T00:00:00Z"  # noqa: E731
    v3 = _v3_module()
    monkeypatch.setattr(v3.time, "strftime", fixed)
    monkeypatch.setattr(store_pg.time, "strftime", fixed)
    root = tmp_path / "pg"
    write_config(root)
    schema = f"v3_{uuid.uuid4().hex[:12]}"
    old = v3.PgStore(root, dsn=DSN, schema=schema)
    try:
        old.pin_config_from_files()
        write(old, "seed", seed)
        write(old, "r2", lambda tx: tx.delete_manifest(["oer-b"], reason="dup-bytes"))
        with old.read() as v:
            old_export = old.export(tmp_path / "old", view=v).files
        old_writer = v3.PgStore(root, dsn=DSN, schema=schema)  # constructed before migrating

        new = store_pg.PgStore(root, dsn=DSN, schema=schema)   # migrates 3 -> 4
        with new.read() as v:
            assert new.export(tmp_path / "new", view=v).files == old_export
        for name in list(old_export) + ["EXPORT.json"]:
            assert (tmp_path / "old" / name).read_bytes() == (tmp_path / "new" / name).read_bytes()
        write(new, "r3", lambda tx: tx.blocklist_add(["https://e.org/v4"]))

        # old clients: construction refuses the newer schema; an instance built before the
        # migration cannot take the writer
        with pytest.raises(StoreError, match=f"version {store_pg.SCHEMA_VERSION}, code expects 3"):
            v3.PgStore(root, dsn=DSN, schema=schema)
        with pytest.raises(StoreError, match="restart with matching code"):
            with old_writer.writer():
                pass
    finally:
        old.drop()


def test_new_code_rejects_a_newer_schema_inside_transactions(pg, monkeypatch):
    import store_pg
    with pg.writer() as w:
        with pg._connect(autocommit=True) as admin:
            admin.execute("UPDATE state SET schema_version = %s",   # a newer client migrated
                          [store_pg.SCHEMA_VERSION + 1])
        with pytest.raises(store.WriterError, match="restart with matching code"):
            with pg.transaction("r1", expected_version=pg.version(), writer=w) as tx:
                tx.blocklist_add(["https://e.org/x"])
    with pytest.raises(StoreError, match=f"version {store_pg.SCHEMA_VERSION + 1}, code expects "
                                         f"{store_pg.SCHEMA_VERSION}"):
        store_pg.PgStore(pg.root, dsn=pg.dsn, schema=pg.schema)
    with pg._connect(autocommit=True) as admin:
        admin.execute("UPDATE state SET schema_version = %s", [store_pg.SCHEMA_VERSION])


def test_a_fresh_schema_has_every_contract_table(pg):
    import store_pg
    assert set(store_pg.V4_TABLES) <= tables(pg)
    assert pg.authority()["mode"] == "file"


# --- the database half of the authority record ----------------------------------------------------

def _cutover(pg, root):
    """What stage 4 step 6 will do: database first, then the host record with the same epoch."""
    epoch = pg.set_authority("postgres", root=root, reason="test cutover")
    auth = pg.authority()
    store_authority.write_record(root, "postgres", reason="test cutover", epoch=epoch,
                                 dataset_uuid=auth["dataset_uuid"], dsn=pg.dsn, schema=pg.schema)
    return epoch


def _pg_env(monkeypatch, pg):
    monkeypatch.setenv("NEKAISE_STORE", "postgres")
    monkeypatch.setenv("NEKAISE_PG_DSN", pg.dsn)
    monkeypatch.setenv("NEKAISE_PG_SCHEMA", pg.schema)


def test_postgres_authority_opens_the_bound_schema_only(pg, monkeypatch):
    import store_pg
    root = pg.root
    assert _cutover(pg, root) == 2
    _pg_env(monkeypatch, pg)
    st = store.open(root=root)
    assert isinstance(st, store_pg.PgStore)
    write(st, "seed", seed)
    with pytest.raises(AuthorityError):
        store.FileStore(root)
    # the same schema opened for ANOTHER, unbound root is refused (it is authoritative)
    other = pg.root.parent / "other"
    write_config(other)
    with pytest.raises(AuthorityError, match="no authority record"):
        store.open(root=other)
    # a record naming another dataset or epoch is refused
    store_authority.write_record(root, "postgres", reason="wrong dataset", epoch=3,
                                 dataset_uuid=str(uuid.uuid4()), dsn=pg.dsn, schema=pg.schema)
    with pytest.raises(AuthorityError, match="refusing"):
        store.open(root=root)


def test_an_authority_change_fences_open_writers(pg, monkeypatch):
    root = pg.root
    _cutover(pg, root)
    _pg_env(monkeypatch, pg)
    st = store.open(root=root)
    pg.set_authority("file", root=root, reason="rollback")   # database moves on (epoch 3)
    with pytest.raises(AuthorityError):
        write(st, "late", seed)                              # checked inside the transaction
    with pytest.raises(AuthorityError):
        store.open(root=root)                                 # the host record is now stale


def test_file_authority_never_serves_postgres(pg, monkeypatch):
    root = pg.root
    store_authority.write_record(root, "file", reason="explicit")
    _pg_env(monkeypatch, pg)
    with pytest.raises(AuthorityError, match="file-authoritative"):
        store.open(root=root)


def test_a_database_failure_is_an_error_not_a_fallback(pg, monkeypatch):
    root = pg.root
    _cutover(pg, root)
    bad = "host=/nonexistent/socket dbname=nope connect_timeout=1"
    store_authority.write_record(root, "postgres", reason="broken dsn", dataset_uuid=pg.authority()
                                 ["dataset_uuid"], dsn=bad, schema=pg.schema)
    monkeypatch.setenv("NEKAISE_STORE", "postgres")
    monkeypatch.setenv("NEKAISE_PG_DSN", bad)
    monkeypatch.setenv("NEKAISE_PG_SCHEMA", pg.schema)
    import psycopg
    with pytest.raises(psycopg.OperationalError):
        store.open(root=root)
    with pytest.raises(AuthorityError):
        store.FileStore(root)


def test_shadow_replay_fails_closed_after_cutover(env):
    pg_shadow, st, repo, c1 = env
    pg_shadow.do_import(st, c1, repo.path, log=quiet)
    st.set_authority("postgres", root=repo.path, reason="test cutover")
    repo.write("pruned_urls.txt", "https://e.org/old\nhttps://e.org/late\n")
    c2 = repo.commit("c2")
    with pytest.raises(SystemExit, match="shadow replay is refused"):
        pg_shadow.do_sync(st, c2, repo.path, log=quiet)
    with st._connect(autocommit=True) as conn:
        assert conn.execute("SELECT count(*) FROM replication_receipts").fetchone()[0] == 1
    import store_pg
    fresh = store_pg.PgStore(repo.path, dsn=st.dsn, schema=f"f_{uuid.uuid4().hex[:12]}")
    try:
        fresh.set_authority("postgres", root=repo.path, reason="test")
        with pytest.raises(SystemExit, match="shadow replay is refused"):
            pg_shadow.do_import(fresh, c1, repo.path, log=quiet)
    finally:
        fresh.drop()


def test_the_host_record_binds_the_shadow_dataset(env):
    pg_shadow, st, repo, c1 = env
    store_authority.write_record(repo.path, "file", reason="bound shadow",
                                 dataset_uuid=str(uuid.uuid4()), dsn=st.dsn, schema=st.schema)
    with pytest.raises(SystemExit, match="another dataset"):
        pg_shadow.check_host_authority(st, repo.path)
    store_authority.write_record(repo.path, "file", reason="bound shadow",
                                 dataset_uuid=st.authority()["dataset_uuid"], dsn=st.dsn,
                                 schema="elsewhere")
    with pytest.raises(SystemExit, match="binds the shadow"):
        pg_shadow.check_host_authority(st, repo.path)
    store_authority.write_record(repo.path, "file", reason="bound shadow",
                                 dataset_uuid=st.authority()["dataset_uuid"], dsn=st.dsn,
                                 schema=st.schema)
    pg_shadow.check_host_authority(st, repo.path)


def test_authority_epochs_are_logged_and_only_grow(pg):
    pg.set_authority("postgres", root=pg.root, reason="a")
    pg.set_authority("file", root=pg.root, reason="b")
    with pg._connect(autocommit=True) as conn:
        assert [r[:2] for r in conn.execute("SELECT epoch, mode FROM authority_log ORDER BY 1")] \
            == [(1, "file"), (2, "postgres"), (3, "file")]
        with pytest.raises(sqlerr()):
            conn.execute("UPDATE dataset SET authority_mode = 'postgres'")   # same epoch
        with pytest.raises(sqlerr()):
            conn.execute("UPDATE dataset SET authority_epoch = 9")           # not logged
        with pytest.raises(sqlerr()):
            conn.execute("UPDATE authority_log SET reason = 'x'")
        with pytest.raises(sqlerr()):
            conn.execute("UPDATE dataset SET dataset_uuid = gen_random_uuid()")
        with pytest.raises(sqlerr()):
            conn.execute("DELETE FROM dataset")


# --- runs, batches, revisions, generations, outbox ------------------------------------------------

CONFIG = {"backends.json": b'{"find_x": {"script": "x.py"}}\n', "eligibility.json": b"{}\n"}
# every registered outbox consumer (schema v5 adds the projection consumer)
CONSUMERS = ("review", "publication", "index", "projection")


def _open(c, run_id="r1", parent=None, **kw):
    kw.setdefault("artifact_policy", "unchecked")   # step-1/2 contracts (v6 adds the artifact gate)
    digest = c.put_config_set(CONFIG)
    return c.open_run(run_id, kind="round", parent_generation=parent, producer_commit=SHA,
                      config_digest=digest, extractor_version="x1", cleaning_ruleset="rules-1",
                      **kw), digest


def _apply(conn, run_id, step, batch, seq, revisions):
    """The v5 shape of applying one batch (store_staging.stage_batch does this): receipt applied
    right after the sequence it was computed at (the database advances the run's staging
    sequence), its revisions, then the seal — all in the caller's transaction."""
    conn.execute("UPDATE batches SET status = 'applied', seq = %s, applied_at = now() WHERE "
                 "run_id = %s AND step = %s AND batch = %s", [seq, run_id, step, batch])
    for tbl, key, op, row, reason in revisions:
        text = store.canonical_row(row) if row is not None else None
        conn.execute("INSERT INTO revisions (run_id, batch_seq, tbl, key, op, row_text, row_sha256, "
                     "reason) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)",
                     [run_id, seq, tbl, key, op, text,
                      hashlib.sha256(text.encode()).hexdigest() if text else None, reason])
    conn.execute("UPDATE batches SET sealed = true WHERE run_id = %s AND step = %s AND batch = %s",
                 [run_id, step, batch])


def _freeze(conn, run_id):
    """Freeze at the staged sequence's chain digest with one required gate, and pass it."""
    conn.execute("UPDATE runs SET status = 'frozen', frozen_seq = staged_seq, frozen_digest = "
                 "COALESCE((SELECT chain_digest FROM batches b WHERE b.run_id = runs.run_id AND "
                 "b.seq = runs.staged_seq), nk_chain_origin(run_id)), required_gates = '[\"test\"]' "
                 "WHERE run_id = %s", [run_id])
    conn.execute("INSERT INTO gate_receipts (run_id, gate, frozen_seq, frozen_digest, verdict) "
                 "SELECT run_id, 'test', frozen_seq, frozen_digest, 'passed' FROM runs "
                 "WHERE run_id = %s", [run_id])


def _promote(conn, run_id, generation, parent):
    """Stage-2 shape of promotion: a constant number of row writes whatever the run staged."""
    staged = conn.execute("SELECT staged_seq FROM runs WHERE run_id = %s", [run_id]).fetchone()[0]
    _freeze(conn, run_id)
    conn.execute("INSERT INTO generations (generation, parent, run_id, producer_commit, "
                 "config_digest, extractor_version, cleaning_ruleset, frozen_seq, frozen_digest, "
                 "counts_text) SELECT %s, %s, run_id, producer_commit, config_digest, "
                 "extractor_version, cleaning_ruleset, frozen_seq, frozen_digest, '{}' FROM runs "
                 "WHERE run_id = %s", [generation, parent, run_id])
    conn.execute("UPDATE runs SET status = 'promoted', promoted_generation = %s, ended_at = now() "
                 "WHERE run_id = %s", [generation, run_id])
    conn.execute("UPDATE dataset SET current_generation = %s", [generation])
    seq = conn.execute("SELECT allocated + 1 FROM outbox_state").fetchone()[0]
    conn.execute("INSERT INTO outbox (seq, generation, payload_text) VALUES (%s, %s, %s)",
                 [seq, generation, json.dumps({"run": run_id, "frozen_seq": staged})])
    return seq


def test_a_run_stages_batches_and_promotes_without_copying(pg):
    with pg.writer() as w:
        with pg.contracts(w) as c:
            assert _open(c)[0] == "open"
            r = c.request_batch("r1", "fetch", "ckpt-0001", [{"call": "upsert_manifest",
                                                              "args": [[{"id": "a"}]]}])
            assert (r.status, r.retried) == ("requested", False)
            _apply(c._conn, "r1", "fetch", "ckpt-0001", 1,
                   [("manifest", "a", "put", {"id": "a", "status": "ok"}, None),
                    ("entries", "b", "tombstone", None, "prune: junk")])
            assert _promote(c._conn, "r1", 0, None) == 1
        with pg._connect(autocommit=True) as conn:
            assert conn.execute("SELECT current_generation FROM dataset").fetchone()[0] == 0
            # visibility of a revision = its run's promoted generation (no revision was rewritten)
            got = conn.execute("SELECT v.tbl, v.key, v.op, r.promoted_generation FROM revisions v "
                               "JOIN runs r USING (run_id) ORDER BY rev_id").fetchall()
            assert got == [("manifest", "a", "put", 0), ("entries", "b", "tombstone", 0)]
            gen = conn.execute("SELECT producer_commit, extractor_version, cleaning_ruleset, "
                               "parent FROM generations").fetchone()
            assert gen == (SHA, "x1", "rules-1", None)
        with pg.contracts(w) as c:  # exact retry of the run and batch are no-ops
            assert _open(c)[0] == "promoted"
            assert c.request_batch("r1", "fetch", "ckpt-0001", [
                {"call": "upsert_manifest", "args": [[{"id": "a"}]]}]).retried
            with pytest.raises(StoreError, match="conflicting retry"):
                c.request_batch("r1", "fetch", "ckpt-0001", [{"call": "other"}])
            with pytest.raises(StoreError, match="different identity"):
                c.open_run("r1", kind="round", parent_generation=None, producer_commit="1" * 40,
                           config_digest=c.put_config_set(CONFIG), extractor_version="x1",
                           cleaning_ruleset="rules-1")
            with pytest.raises(store.VersionConflict):
                _open(c, "r2", parent=None)   # the current generation is 0 now
            assert _open(c, "r2", parent=0)[0] == "open"
            assert c.config_set(c.put_config_set(CONFIG)) == CONFIG


def _expect_refused(pg, statement, params=()):
    with pg._connect(autocommit=True) as conn, pytest.raises(sqlerr()):
        conn.execute(statement, params)


def test_contract_tables_refuse_rewrites(pg):
    with pg.writer() as w:
        with pg.contracts(w) as c:
            _, digest = _open(c)
            c.request_batch("r1", "prune", "apply", [{"call": "x"}])
            _apply(c._conn, "r1", "prune", "apply", 1,
                   [("manifest", "a", "put", {"id": "a"}, None)])
            _promote(c._conn, "r1", 0, None)
            c.register_artifact("raw", "a" * 64, 10, locator="artifacts/raw/aa/aa/" + "a" * 64)
    for stmt in ("UPDATE revisions SET key = 'z'", "DELETE FROM revisions",
                 "UPDATE generations SET cleaning_ruleset = 'x'", "DELETE FROM generations",
                 "UPDATE runs SET status = 'aborted'", "DELETE FROM runs",
                 "UPDATE batches SET request_text = '[]'", "DELETE FROM batches",
                 "UPDATE config_blobs SET bytes = 'x'", "DELETE FROM config_set_members",
                 "UPDATE artifacts SET size = 11", "DELETE FROM artifacts",
                 "UPDATE outbox SET payload_text = '{}'", "DELETE FROM outbox",
                 "DELETE FROM outbox_consumers",
                 "INSERT INTO config_blobs (sha256, bytes) VALUES (repeat('0', 64), 'x')"):
        _expect_refused(pg, stmt)
    with pg.writer() as w, pg.contracts(w) as c:
        with pytest.raises(StoreError, match="size"):
            c.register_artifact("raw", "a" * 64, 11)
        assert not c.register_artifact("raw", "a" * 64, 10)


def test_run_lifecycle_rules(pg):
    with pg.writer() as w:
        with pg.contracts(w) as c:
            _open(c)
            c.request_batch("r1", "fetch", "b1", [{"call": "x"}])
            _apply(c._conn, "r1", "fetch", "b1", 1, [("manifest", "a", "put", {"id": "a"}, None)])
    # a generation needs a frozen run; freezing needs the staged sequence and a digest
    _expect_refused(pg, "INSERT INTO generations (generation, run_id, producer_commit, "
                    "config_digest, extractor_version, cleaning_ruleset, frozen_seq, "
                    "frozen_digest, counts_text) SELECT 0, run_id, producer_commit, config_digest, "
                    "extractor_version, cleaning_ruleset, 1, 'f', '{}' FROM runs")
    _expect_refused(pg, "UPDATE runs SET status = 'frozen', frozen_seq = 0, frozen_digest = 'f'")
    _expect_refused(pg, "UPDATE runs SET status = 'promoted'")
    _expect_refused(pg, "UPDATE runs SET staged_seq = 0")
    _expect_refused(pg, "UPDATE runs SET producer_commit = %s", ["1" * 40])
    _expect_refused(pg, "UPDATE dataset SET current_generation = 0")  # no such generation
    # an aborted run's staging may be discarded; nothing more may be staged in it
    with pg._connect(autocommit=True) as conn:
        conn.execute("UPDATE runs SET status = 'aborted', ended_at = now()")
        conn.execute("DELETE FROM revisions")
        conn.execute("DELETE FROM batches")
    _expect_refused(pg, "INSERT INTO batches (run_id, step, batch, request_digest, request_text) "
                    "VALUES ('r1', 'fetch', 'b2', repeat('a', 64), '[]')")
    _expect_refused(pg, "UPDATE runs SET status = 'open'")


def test_outbox_watermarks_are_independent_and_contiguous(pg):
    with pg.writer() as w:
        parent = None
        for g in range(3):  # three promoted generations -> outbox rows 1..3
            with pg.contracts(w) as c:
                _open(c, f"r{g}", parent=parent)
                c.request_batch(f"r{g}", "fetch", "b", [{"call": "x", "g": g}])
                _apply(c._conn, f"r{g}", "fetch", "b", 1,
                       [("manifest", f"d{g}", "put", {"id": f"d{g}"}, None)])
                _promote(c._conn, f"r{g}", g, parent)
            parent = g
        with pg.contracts(w) as c:
            c.ack("review", 1, "ok")
            c.ack("review", 3, "finding", {"note": "yield drop"})
            assert c.advance("review") == 1            # 2 is not acknowledged: stops before it
            c.ack("review", 2, "ok")
            assert c.advance("review") == 3
            c.ack("publication", 1, "ok")
            assert c.advance("publication") == 1
            c.ack("review", 1, "ok")                   # exact retry
            with pytest.raises(StoreError, match="differently"):
                c.ack("review", 1, "integrity")
            assert c.watermarks() == {"index": 0, "projection": 0, "publication": 1, "review": 3}
    _expect_refused(pg, "UPDATE outbox_consumers SET watermark = 3 WHERE consumer = 'index'")
    _expect_refused(pg, "UPDATE outbox_consumers SET watermark = 0 WHERE consumer = 'review'")
    _expect_refused(pg, "UPDATE outbox_acks SET verdict = 'ok'")
    _expect_refused(pg, "DELETE FROM outbox WHERE seq = 1")   # the index has not seen it
    _expect_refused(pg, "INSERT INTO outbox (seq, generation, payload_text) VALUES (9, 2, '{}')")
    with pg.writer() as w, pg.contracts(w) as c:
        for consumer in ("index", "projection"):
            c.ack(consumer, 1, "ok")
            assert c.advance(consumer) == 1
    _expect_refused(pg, "DELETE FROM outbox_acks WHERE seq = 2")
    with pg._connect(autocommit=True) as conn:
        conn.execute("DELETE FROM outbox WHERE seq = 1")   # every consumer is past it: compacts
        assert conn.execute("SELECT count(*) FROM outbox_acks WHERE seq = 1").fetchone()[0] == 0
        with pytest.raises(sqlerr()):
            conn.execute("DELETE FROM outbox WHERE seq = 2")  # index and publication are not


def test_contracts_are_fenced_like_transactions(pg, monkeypatch):
    with pg.writer() as w:
        pass
    with pytest.raises(store.WriterError, match="stale"):
        with pg.contracts(w):
            pass


def _generations(pg, w, n, first=0):
    parent = first - 1 if first else None
    for g in range(first, first + n):
        with pg.contracts(w) as c:
            _open(c, f"r{g}", parent=parent)
            c.request_batch(f"r{g}", "fetch", "b", [{"call": "x", "g": g}])
            _apply(c._conn, f"r{g}", "fetch", "b", 1,
                   [("manifest", f"d{g}", "put", {"id": f"d{g}"}, None)])
            _promote(c._conn, f"r{g}", g, parent)
        parent = g


def test_full_compaction_never_reuses_outbox_sequences(pg):
    """Codex review P1: after every row is compacted the next promotion must take a NEW
    sequence, so consumers whose watermark is past the old rows still receive it."""
    with pg.writer() as w:
        _generations(pg, w, 2)
        with pg.contracts(w) as c:
            for consumer in CONSUMERS:
                for seq in (1, 2):
                    c.ack(consumer, seq, "ok")
                assert c.advance(consumer) == 2
    _expect_refused(pg, "DELETE FROM outbox WHERE seq = 2")   # compaction goes lowest first
    with pg._connect(autocommit=True) as conn:
        conn.execute("DELETE FROM outbox WHERE seq = 1")
        conn.execute("DELETE FROM outbox WHERE seq = 2")
        assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 0
    _expect_refused(pg, "UPDATE outbox_state SET allocated = 0")
    _expect_refused(pg, "UPDATE outbox_state SET allocated = 7, compacted = 7")
    _expect_refused(pg, "INSERT INTO outbox (seq, generation, payload_text) "
                        "VALUES (1, 1, '{}')")                # a reused sequence
    _expect_refused(pg, "INSERT INTO outbox_consumers (consumer) VALUES ('late')")
    with pg.writer() as w:
        _generations(pg, w, 1, first=2)                       # the next promotion
        with pg.contracts(w) as c:
            assert c.outbox_marks() == (3, 2)
            assert c._q("SELECT seq, generation FROM outbox").fetchall() == [(3, 2)]
            assert c.advance("review") == 2                   # not acknowledged yet: still due
            c.ack("review", 3, "ok")
            assert c.advance("review") == 3


def test_config_sets_are_sealed_and_digest_checked(pg):
    """Codex review P2: a config set is created and sealed in one transaction; its digest is
    the canonical digest of its members; nothing can be added, changed or removed later."""
    import psycopg
    extra = hashlib.sha256(b"{}").hexdigest()
    with pg.writer() as w, pg.contracts(w) as c:
        digest = c.put_config_set(CONFIG)
        assert digest == store._digest({n: hashlib.sha256(b).hexdigest()
                                        for n, b in CONFIG.items()})
        assert c.put_config_set(CONFIG) == digest             # exact retry
        c._q("INSERT INTO config_blobs (sha256, bytes) VALUES (%s, %s)", [extra, b"{}"])
    _expect_refused(pg, "INSERT INTO config_set_members (digest, name, sha256) VALUES "
                        "(%s, 'vendors.json', %s)", [digest, extra])   # added after sealing
    _expect_refused(pg, "UPDATE config_set_members SET sha256 = %s", [extra])
    _expect_refused(pg, "DELETE FROM config_set_members")
    _expect_refused(pg, "UPDATE config_sets SET created_at = now()")
    _expect_refused(pg, "DELETE FROM config_sets")
    good = store._canonical({"a.json": extra})
    for text, dig in ((good, "0" * 64),                                # wrong digest
                      ('{"a.json": "%s"}' % extra, None),              # not canonical
                      ('{"a.json":"nothex"}', None),                   # not a sha256
                      ("[]", None)):
        dig = dig or hashlib.sha256(text.encode()).hexdigest()
        _expect_refused(pg, "INSERT INTO config_sets (digest, members_text) VALUES (%s, %s)",
                        [dig, text])
    # a set committed without all its members fails at commit (deferred check)
    with pg._connect() as conn, pytest.raises(psycopg.IntegrityError):
        conn.execute("INSERT INTO config_sets (digest, members_text) VALUES (%s, %s)",
                     [hashlib.sha256(good.encode()).hexdigest(), good])
        conn.commit()
    with pg._connect(autocommit=True) as conn:
        assert conn.execute("SELECT count(*) FROM config_sets").fetchone()[0] == 1


def test_init_file_refuses_an_authoritative_database(pg, tmp_path, capsys):
    """Codex review P1: init-file binding a shadow must check the database's authority."""
    root = tmp_path / "live"
    write_config(root)
    pg.set_authority("postgres", root=root, reason="test cutover")
    assert store_authority.main(["init-file", "--root", str(root), "--dsn", pg.dsn,
                                 "--schema", pg.schema]) == 1
    assert "PostgreSQL-authoritative" in capsys.readouterr().err
    assert store_authority.record_for(root) is None
    other = tmp_path / "other"
    fresh_schema = f"x_{uuid.uuid4().hex[:12]}"
    with pytest.raises(Exception):   # never creates a schema that does not exist
        store_authority.main(["init-file", "--root", str(other), "--dsn", pg.dsn,
                              "--schema", fresh_schema])
    with pg._connect(autocommit=True) as conn:
        assert conn.execute("SELECT to_regnamespace(%s)", [fresh_schema]).fetchone()[0] is None


def test_init_file_binds_a_file_mode_shadow(pg, tmp_path):
    root = tmp_path / "live"
    write_config(root)
    assert store_authority.main(["init-file", "--root", str(root), "--dsn", pg.dsn,
                                 "--schema", pg.schema]) == 0
    rec = store_authority.record_for(root)
    assert (rec.mode, rec.dataset_uuid, rec.schema) == ("file", pg.authority()["dataset_uuid"],
                                                        pg.schema)


def _ack_all(pg, w, seqs):
    with pg.contracts(w) as c:
        for consumer in CONSUMERS:
            for seq in seqs:
                c.ack(consumer, seq, "ok")
            c.advance(consumer)


def test_a_conflict_skipped_outbox_insert_allocates_nothing(pg):
    """Codex re-review P2: an INSERT ... ON CONFLICT DO NOTHING that is skipped must not
    advance the allocation (a gap would block compaction forever)."""
    with pg.writer() as w:
        _generations(pg, w, 1)                                   # seq 1 -> generation 0
        with pg._connect(autocommit=True) as conn:
            cur = conn.execute("INSERT INTO outbox (seq, generation, payload_text) VALUES "
                               "(2, 0, '{}') ON CONFLICT (generation) DO NOTHING")
            assert cur.rowcount == 0
            assert conn.execute("SELECT allocated, compacted FROM outbox_state").fetchone() \
                == (1, 0)
        _generations(pg, w, 1, first=1)                          # seq 2 -> generation 1
        _ack_all(pg, w, (1, 2))
        with pg._connect(autocommit=True) as conn:              # full compaction
            conn.execute("DELETE FROM outbox WHERE seq = 1")
            conn.execute("DELETE FROM outbox WHERE seq = 2")
        _generations(pg, w, 1, first=2)                          # then a promotion
        with pg.contracts(w) as c:
            assert c.outbox_marks() == (3, 2)
            assert c._q("SELECT seq, generation FROM outbox").fetchall() == [(3, 2)]
        _ack_all(pg, w, (3,))
    with pg._connect(autocommit=True) as conn:
        conn.execute("DELETE FROM outbox WHERE seq = 3")         # compaction still proceeds
        assert conn.execute("SELECT allocated, compacted FROM outbox_state").fetchone() == (3, 3)


def _blocked_then(fn, release):
    """Run fn in a thread; assert it blocks until release() runs; return its exception."""
    import threading
    out = {}

    def run():
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            out["exc"] = exc
    t = threading.Thread(target=run)
    t.start()
    t.join(1.0)
    assert t.is_alive(), "the second transaction did not wait for the first"
    release()
    t.join(30)
    assert not t.is_alive()
    return out.get("exc")


def _compactable(pg):
    with pg.writer() as w:
        _generations(pg, w, 1)
        _ack_all(pg, w, (1,))


def test_registration_waiting_for_compaction_is_refused(pg):
    """Codex re-review P2 (two connections): compaction first, registration waits for its row
    lock and then sees the compaction."""
    import psycopg
    _compactable(pg)
    compactor = pg._connect()
    registrar = pg._connect()
    try:
        compactor.execute("DELETE FROM outbox WHERE seq = 1")    # uncommitted, holds the lock
        exc = _blocked_then(
            lambda: (registrar.execute("INSERT INTO outbox_consumers (consumer) VALUES "
                                       "('late')"), registrar.commit()),
            compactor.commit)
        assert isinstance(exc, psycopg.IntegrityError)
        registrar.rollback()
    finally:
        compactor.close()
        registrar.close()
    with pg._connect(autocommit=True) as conn:
        assert conn.execute("SELECT count(*) FROM outbox_consumers WHERE consumer = 'late'"
                            ).fetchone()[0] == 0
        assert conn.execute("SELECT compacted FROM outbox_state").fetchone()[0] == 1


def test_compaction_waiting_for_registration_is_refused(pg):
    """Codex re-review P2 (two connections): registration first, compaction waits for its row
    lock and then sees the new consumer at watermark 0, so the row is kept for it."""
    import psycopg
    _compactable(pg)
    registrar = pg._connect()
    compactor = pg._connect()
    try:
        registrar.execute("INSERT INTO outbox_consumers (consumer) VALUES ('late')")
        exc = _blocked_then(
            lambda: (compactor.execute("DELETE FROM outbox WHERE seq = 1"), compactor.commit()),
            registrar.commit)
        assert isinstance(exc, psycopg.IntegrityError)
        compactor.rollback()
    finally:
        registrar.close()
        compactor.close()
    with pg._connect(autocommit=True) as conn:
        assert conn.execute("SELECT count(*) FROM outbox").fetchone()[0] == 1
        assert conn.execute("SELECT compacted FROM outbox_state").fetchone()[0] == 0
    with pg.writer() as w, pg.contracts(w) as c:
        assert c.advance("late") == 0                            # row 1 is still due to it
        c.ack("late", 1, "ok")
        assert c.advance("late") == 1


@pytest.mark.parametrize("level", ["REPEATABLE_READ", "SERIALIZABLE"])
def test_a_stale_snapshot_compactor_cannot_skip_a_new_consumer(pg, level):
    """Codex third review P2, the exact schedule: row 1 acknowledged by every consumer; the
    registrar inserts `late` and holds it uncommitted; a REPEATABLE READ (or SERIALIZABLE)
    compactor takes its snapshot with DELETE seq 1 and waits; the registrar commits. The
    compactor must fail (serialization) and history must be kept for `late`."""
    import psycopg
    _compactable(pg)
    registrar = pg._connect()
    compactor = pg._connect()
    compactor.commit()  # end the search_path transaction _connect() opened
    compactor.isolation_level = getattr(psycopg.IsolationLevel, level)
    try:
        registrar.execute("INSERT INTO outbox_consumers (consumer) VALUES ('late')")
        exc = _blocked_then(
            lambda: (compactor.execute("DELETE FROM outbox WHERE seq = 1"), compactor.commit()),
            registrar.commit)
        assert isinstance(exc, (psycopg.errors.SerializationFailure, psycopg.IntegrityError)), exc
        compactor.rollback()
    finally:
        registrar.close()
        compactor.close()
    with pg._connect(autocommit=True) as conn:
        assert conn.execute("SELECT seq FROM outbox").fetchall() == [(1,)]
        assert conn.execute("SELECT compacted FROM outbox_state").fetchone()[0] == 0
        assert conn.execute("SELECT watermark FROM outbox_consumers WHERE consumer = 'late'"
                            ).fetchone()[0] == 0
    with pg.writer() as w, pg.contracts(w) as c:
        assert c.advance("late") == 0                            # still due to it


@pytest.mark.parametrize("level", ["REPEATABLE_READ", "SERIALIZABLE"])
def test_a_stale_snapshot_registrar_cannot_miss_a_compaction(pg, level):
    """The reverse order under REPEATABLE READ / SERIALIZABLE: the compaction commits while the
    registrar waits; the registrar fails and no consumer is registered after compaction."""
    import psycopg
    _compactable(pg)
    compactor = pg._connect()
    registrar = pg._connect()
    registrar.commit()
    registrar.isolation_level = getattr(psycopg.IsolationLevel, level)
    try:
        registrar.execute("SELECT 1 FROM outbox_consumers LIMIT 1")  # snapshot taken now
        compactor.execute("DELETE FROM outbox WHERE seq = 1")
        exc = _blocked_then(
            lambda: (registrar.execute("INSERT INTO outbox_consumers (consumer) VALUES "
                                       "('late')"), registrar.commit()),
            compactor.commit)
        assert isinstance(exc, (psycopg.errors.SerializationFailure, psycopg.IntegrityError)), exc
        registrar.rollback()
    finally:
        compactor.close()
        registrar.close()
    with pg._connect(autocommit=True) as conn:
        assert conn.execute("SELECT count(*) FROM outbox_consumers WHERE consumer = 'late'"
                            ).fetchone()[0] == 0
        assert conn.execute("SELECT compacted FROM outbox_state").fetchone()[0] == 1


def test_a_multi_row_outbox_insert_fails_closed(pg):
    """Codex third review (minor): rows are allocated one statement at a time. A multi-row
    INSERT is refused (the second row's BEFORE trigger runs before the first row's AFTER
    trigger advances the allocation) and changes nothing. (Schema v5: a generation commits only
    together with its outbox row, so the attempts run inside its promotion transaction.)"""
    import psycopg
    with pg.writer() as w:
        _generations(pg, w, 1)                                   # seq 1 -> generation 0
        with pg.contracts(w) as c:                               # generation 1, in progress
            _open(c, "m1", parent=0)
            c.request_batch("m1", "fetch", "b", [{"call": "x"}])
            _apply(c._conn, "m1", "fetch", "b", 1, [("manifest", "m", "put", {"id": "m"}, None)])
            _freeze(c._conn, "m1")
            c._q("INSERT INTO generations (generation, parent, run_id, producer_commit, "
                 "config_digest, extractor_version, cleaning_ruleset, frozen_seq, frozen_digest, "
                 "counts_text) SELECT 1, 0, run_id, producer_commit, config_digest, "
                 "extractor_version, cleaning_ruleset, frozen_seq, frozen_digest, '{}' FROM runs "
                 "WHERE run_id = 'm1'")
            c._q("UPDATE runs SET status = 'promoted', promoted_generation = 1, ended_at = now() "
                 "WHERE run_id = 'm1'")
            c._q("UPDATE dataset SET current_generation = 1")
            for stmt in ("INSERT INTO outbox (seq, generation, payload_text) VALUES "
                         "(2, 1, '{}'), (3, 0, '{}')",
                         "INSERT INTO outbox (seq, generation, payload_text) "
                         "SELECT 2, 1, '{}' UNION ALL SELECT 3, 1, '{}'"):
                with pytest.raises(psycopg.IntegrityError), c._conn.transaction():
                    c._q(stmt)
            assert c._q("SELECT allocated, compacted FROM outbox_state").fetchone() == (1, 0)
            assert c._q("SELECT seq FROM outbox ORDER BY seq").fetchall() == [(1,)]
            c._q("INSERT INTO outbox (seq, generation, payload_text) VALUES (2, 1, '{}')")
            assert c._q("SELECT allocated FROM outbox_state").fetchone()[0] == 2
