"""Schema v8 (ADR 0001 stage 4 step 5): indexed persistent-identifier membership. The `pids`
derived column is written by every derived-column writer, migration 8 backfills it for rows and
revisions the real v7 code wrote, known_pids() answers through it identically to the v7
expression scan (projection, staged overlay, tombstones), the index is used, and the derived-key
checks (pg_shadow verify, verify_generation, the integrity sweep) cover it. Opt-in:
NEKAISE_PG_TEST_DSN."""
from __future__ import annotations

import json
import os
import uuid

import pytest

import old_code
import store
import store_broker
from runids import rid
from pipeline_repo import write_repo


def write_config(root):
    write_repo(root, entries=[], manifest=[], policy={})

pytestmark = pytest.mark.skipif(not os.environ.get("NEKAISE_PG_TEST_DSN"),
                                reason="NEKAISE_PG_TEST_DSN not set")
DSN = os.environ.get("NEKAISE_PG_TEST_DSN", "")
V7_COMMIT = "d7e35dd958"   # last commit whose store_pg.py writes schema version 7 (OpenAlex phase 1)
SHA = "0" * 40
CANDIDATES = ["doi:10.1234/abc.1", "doi:10.5555/pub.2", "doi:10.2139/ssrn.99", "openalex:W77",
              "doi:10.9999/manifest.only", "doi:10.1234/abc", "doi:10.4444/listed",
              "doi:10.5555/spaced", "doi:10.6666/gone", "doi:10.7777/staged", "openalex:W7",
              "doi:10.1234/abc.10"]


def entry(sid, **extra):
    return {"id": sid, "title": f"Title of {sid}", "url": f"https://e.org/{sid}.pdf",
            "source": "test", "license": "public-domain", "topic": "building_energy",
            "format": "pdf", **extra}


def mrow(sid, **extra):
    return {**entry(sid), "status": "ok", "http_status": 200, "sha256": f"sha-{sid}",
            "bytes": 10, "raw_path": f"raw/test/{sid}.pdf", "text_path": f"text/{sid}.md",
            "text_chars": 100, "error": None, **extra}


def q(st, statement, params=()):
    with st._connect(autocommit=True) as conn:
        return conn.execute(statement, params).fetchall()


def seed(tx):
    tx.insert_entries([
        entry("ojs-paper", persistent_id="https://doi.org/10.1234/ABC.1"),
        entry("ope-legacy", persistent_id="https://doi.org/10.5555/pub.2",
              origin_ids="doi:10.5555/pub.2 doi:10.2139/ssrn.99 openalex:W77"),
        entry("nlr-report", persistent_id="NREL/TP-5500-1"),
        entry("oa-list", origin_ids=["doi:10.1234/abc", " openalex:W77 ",
                                     "https://doi.org/10.4444/Listed"]),
        entry("oa-space", persistent_id="  https://doi.org/10.5555/Spaced  \n"),
        entry("oa-gone", persistent_id="doi:10.6666/gone"),
        entry("plain"),
    ])
    tx.upsert_manifest([mrow("hand-one", persistent_id="doi:10.9999/Manifest.Only"),
                        mrow("plain")])


def stage_run(st, run_id, fn, *, promote=True):
    with st.writer() as w:
        run = st.open_run(w, run_id, producer_commit=SHA, extractor_version="x1",
                    cleaning_ruleset="none", artifacts="unchecked")
        rec = store_broker.Recorder()
        fn(rec)
        with st.read_staged(run_id, writer=w) as v:
            st.stage_batch(w, run_id, "b", "b0", rec.requests, expected_version=v.version())
        if promote:
            fr = st.freeze(w, run_id, required_gates=["tests"])
            st.record_gate(w, fr, "tests", passed=True)
            st.promote(w, fr)
    return run


def staged_changes(tx):
    tx.insert_entries([entry("oa-staged", origin_ids="doi:10.7777/staged")])
    tx.delete_entries(["oa-gone"], reason="test")


def test_a_v7_schema_migrates_to_indexed_pids_with_identical_answers(tmp_path):
    import store_pg
    v7 = old_code.load(V7_COMMIT, "store_pg")
    assert v7.SCHEMA_VERSION == 7
    staging7 = old_code.load(V7_COMMIT, "store_staging", store_pg=v7)
    root = tmp_path / "pg"
    write_config(root)
    schema = f"p8_{uuid.uuid4().hex[:12]}"
    old = v7.PgStore(root, dsn=DSN, schema=schema)
    import sys
    saved = sys.modules.get("store_staging")
    sys.modules["store_staging"] = staging7   # the v7 store stages through v7 staging
    try:
        old.pin_config_from_files()
        with old.writer() as w:
            with old.transaction(rid("p8-seed"), expected_version=old.version(), writer=w) as tx:
                seed(tx)
        stage_run(old, rid("p8-g0"), staged_changes)                      # promoted, unfolded
        open_run = stage_run(old, rid("p8-open"), lambda tx: tx.insert_entries(
            [entry("oa-open", persistent_id="doi:10.8888/open")]), promote=False)
        with old.read() as v:
            before = v.known_pids(CANDIDATES)
        with old.read() as v:   # a committed view never sees the open run
            assert v.known_pids(["doi:10.8888/open"]) == set()
    finally:
        if saved is not None:
            sys.modules["store_staging"] = saved
        else:
            sys.modules.pop("store_staging", None)
    try:
        new = store_pg.PgStore(root, dsn=DSN, schema=schema)              # migrates 7 -> 8
        assert q(new, "SELECT schema_version FROM state")[0][0] == store_pg.SCHEMA_VERSION == 8
        # the backfill: every row and put revision that declares identifiers, nothing else
        assert dict(q(new, "SELECT id, pids FROM entries WHERE pids IS NOT NULL")) == {
            "ojs-paper": ["doi:10.1234/abc.1"],
            "ope-legacy": ["doi:10.5555/pub.2", "doi:10.2139/ssrn.99", "openalex:W77"],
            "oa-list": ["doi:10.1234/abc", "openalex:W77", "doi:10.4444/listed"],
            "oa-space": ["doi:10.5555/spaced"], "oa-gone": ["doi:10.6666/gone"]}
        assert q(new, "SELECT id, pids FROM manifest WHERE pids IS NOT NULL") == [
            ("hand-one", ["doi:10.9999/manifest.only"])]
        assert sorted(q(new, "SELECT key, pids FROM revisions WHERE pids IS NOT NULL")) == [
            ("oa-open", ["doi:10.8888/open"]), ("oa-staged", ["doi:10.7777/staged"])]
        with new.read() as v:
            assert v.known_pids(CANDIDATES) == before == {
                "doi:10.1234/abc.1", "doi:10.5555/pub.2", "doi:10.2139/ssrn.99", "openalex:W77",
                "doi:10.9999/manifest.only", "doi:10.1234/abc", "doi:10.4444/listed",
                "doi:10.5555/spaced", "doi:10.7777/staged"}    # oa-gone was tombstoned
        # the open run's overlay (its own staged revision) through the new code
        with new.read_staged(rid("p8-open"), token=open_run.token) as v:
            assert v.known_pids(["doi:10.8888/open"]) == {"doi:10.8888/open"}
        # old clients are refused
        with pytest.raises(store.StoreError, match="version 8, code expects 7"):
            v7.PgStore(root, dsn=DSN, schema=schema)
    finally:
        store_pg.PgStore(root, dsn=DSN, schema=schema, create=False).drop()


@pytest.fixture
def pg(tmp_path):
    import store_pg
    root = tmp_path / "pg"
    write_config(root)
    st = store_pg.PgStore(root, dsn=DSN, schema=f"p_{uuid.uuid4().hex[:12]}")
    st.pin_config_from_files()
    yield st
    st.drop()


def test_every_writer_derives_pids_and_the_fold_carries_them(pg):
    import store_pg
    import store_staging
    with pg.writer() as w:
        with pg.transaction(rid("pw-seed"), expected_version=pg.version(), writer=w) as tx:
            seed(tx)
    stage_run(pg, rid("pw-g0"), lambda tx: (
        tx.upsert_manifest([mrow("oa-list", origin_ids=["doi:10.1234/Changed"])]),
        tx.update_manifest_fields({"hand-one": {"persistent_id": None}})))
    assert dict(q(pg, "SELECT key, pids FROM revisions WHERE tbl = 'manifest'")) == {
        "oa-list": ["doi:10.1234/changed"], "hand-one": None}
    with pg.read() as v:
        assert v.known_pids(["doi:10.1234/changed", "doi:10.9999/manifest.only"]) == {
            "doi:10.1234/changed"}
    with pg.writer() as w:
        store_staging.fold_all(pg, w)
    for table in ("entries", "manifest"):
        for rid_, text, pids in q(pg, f"SELECT id, row_text, pids FROM {table}"):
            assert pids == store_pg.pids_for(json.loads(text)), (table, rid_)
    with pg.read() as v:
        assert v.known_pids(["doi:10.1234/changed", "doi:10.9999/manifest.only"]) == {
            "doi:10.1234/changed"}


def test_the_lookup_uses_the_gin_index(pg):
    with pg.writer() as w:
        with pg.transaction(rid("pi-seed"), expected_version=pg.version(), writer=w) as tx:
            tx.upsert_manifest([mrow(f"m-{i:05d}", persistent_id=f"doi:10.1000/{i}")
                                for i in range(3000)] +
                               [mrow(f"n-{i:05d}") for i in range(3000)])
    with pg._connect(autocommit=True) as conn:
        conn.execute("ANALYZE manifest")
    with pg._connect(autocommit=True) as conn:
        # the per-identifier probe known_pids() issues: an index probe per value, never a scan
        # of the table (a many-value && filter is estimated to match most rows at scale)
        plan = "\n".join(r[0] for r in conn.execute(
            "EXPLAIN SELECT e.id FROM unnest(%s::text[]) u(p) CROSS JOIN LATERAL (SELECT id "
            "FROM manifest WHERE pids @> ARRAY[u.p] OFFSET 0) e",
            [[f"doi:10.1000/{i}" for i in range(400)]]).fetchall())
    assert "manifest_pids" in plan and "Seq Scan on manifest" not in plan, plan
    with pg.read() as v:
        assert v.known_pids(["doi:10.1000/7", "doi:10.1000/99999"]) == {"doi:10.1000/7"}


def test_derived_key_checks_cover_pids(pg, tmp_path):
    """A stale `pids` is found by pg_shadow's derived check, verify_generation's revision check
    and the integrity sweep's projection check."""
    import pg_shadow
    import verify_generation as vg
    with pg.writer() as w:
        with pg.transaction(rid("pd-seed"), expected_version=pg.version(), writer=w) as tx:
            seed(tx)
    assert pg_shadow.pg_digests(pg)[1]["derived"] == "mismatches=0"
    q(pg, "UPDATE entries SET pids = ARRAY['doi:10.0/forged'] WHERE id = 'plain' RETURNING id")
    assert pg_shadow.pg_digests(pg)[1]["derived"] == "mismatches=1"
    q(pg, "UPDATE entries SET pids = NULL WHERE id = 'plain' RETURNING id")
    run_id = rid("pd-run")
    with pg.writer() as w:
        pg.open_run(w, run_id, producer_commit=SHA, extractor_version="x1",
                    cleaning_ruleset="none", artifacts="unchecked")
        rec = store_broker.Recorder()
        rec.insert_entries([entry("oa-new", persistent_id="doi:10.3333/new")])
        with pg.read_staged(run_id, writer=w) as v:
            pg.stage_batch(w, run_id, "b", "b0", rec.requests, expected_version=v.version())
        fr = pg.freeze(w, run_id, required_gates=["contracts"])
        with pg._connect(autocommit=True) as conn:
            conn.execute("ALTER TABLE revisions DISABLE TRIGGER USER")
            conn.execute("UPDATE revisions SET pids = NULL WHERE run_id = %s", [run_id])
            conn.execute("ALTER TABLE revisions ENABLE TRIGGER USER")
        with pg.read_staged(run_id, seq=fr.seq, writer=w) as view:
            restrictions, _ = store.pinned_policy(view)
            rep = vg.run_checks(view, restrictions, root=pg.root)
        pg.abort_run(w, run_id, reason="test")
    assert any("entries oa-new: derived columns differ" in e for e in rep.errors)
    # the sweep's projection check, on a folded generation
    import integrity_sweep
    import store_staging
    stage_run(pg, rid("pd-g0"), lambda tx: tx.insert_entries([entry("oa-g0")]))
    with pg.writer() as w:
        store_staging.fold_all(pg, w)
    q(pg, "UPDATE entries SET pids = ARRAY['doi:10.0/forged'] WHERE id = 'oa-g0' RETURNING id")
    out = integrity_sweep.metadata(pg, seconds=60, restart=True,
                                   state_path=tmp_path / "sweep.json", log=lambda *_: None)
    assert any("entries oa-g0: projection derived columns differ" in f
               for f in out["metadata"]["failures"])
