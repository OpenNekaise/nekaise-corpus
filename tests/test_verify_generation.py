"""Generation-bound verification (ADR 0001 stage 4 step 5): the counter arithmetic against
corpus_stats' aggregates, the gate report protocol, the per-run checks over a frozen staged run
(revision integrity and derived keys, ledger, eligibility, artifact claims, counters chained
from the parent's recorded ones or recounted in full), the resumable integrity sweeps, the
reference-checked GC report and the PostgreSQL -> FileStore rollback exporter. The PostgreSQL
parts are opt-in: NEKAISE_PG_TEST_DSN."""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path

import pytest

import store
import verify_generation as vg
from pipeline_repo import restriction, write_repo
from runids import rid

DSN = os.environ.get("NEKAISE_PG_TEST_DSN", "")
needs_pg = pytest.mark.skipif(not DSN, reason="NEKAISE_PG_TEST_DSN not set")
SHA = "0" * 40


def mrow(i: int, **kw) -> dict:
    sid = f"ost-v-{i:04d}"
    row = {"id": sid, "title": f"Doc {i}", "url": f"https://e.org/{sid}.pdf", "source": "osti",
           "license": "public-domain", "topic": "building_energy", "format": "pdf",
           "status": "ok", "http_status": 200, "text_chars": 1000 + i,
           "sha256": hashlib.sha256(sid.encode()).hexdigest()}
    row.update(kw)
    return {k: v for k, v in row.items() if v is not ...}


def entry(row: dict) -> dict:
    return {k: row[k] for k in ("id", "title", "url", "source", "license", "topic", "format")
            if row.get(k) is not None}


VARIED = [
    mrow(1), mrow(2, corpus_chars=900), mrow(3, corpus_chars=None), mrow(4, text_chars=12.0),
    mrow(5, topic="architecture"), mrow(6, topic="materials"), mrow(7, status="failed", text_chars=...),
    mrow(8, license="proprietary-internal"), mrow(9, source="soep"), mrow(10, text_chars="x"),
    mrow(11, corpus_chars=0), mrow(12, status=None), mrow(13, license="cc-by", topic="urban"),
]
RESTRICTIONS = {"soep": restriction({"source": "soep"})}
# rows lint accepts (VARIED also holds a pointer-only row with a payload, which lint flags)
CLEAN = [r for r in VARIED if r["license"] != "proprietary-internal"]


# --- the counter arithmetic (no database) -------------------------------------------------------

def test_row_arithmetic_equals_corpus_stats_on_varied_rows(tmp_path):
    """The per-row contributions the gates chain from generation to generation must equal
    corpus_stats' aggregate definitions exactly (floats, missing and null fields, pointer-only
    licences, restrictions, failed and status-less rows)."""
    import corpus_stats
    root = tmp_path / "repo"
    write_repo(root, entries=[entry(r) for r in VARIED], manifest=VARIED,
               restrictions=RESTRICTIONS, policy={})
    st = store.open(root=root)
    with st.read() as view:
        restrictions, _ = store.pinned_policy(view)
        stats = corpus_stats.compute(view, restrictions)
    c = vg.new_counters()
    eligible = store.eligibility_where(restrictions)
    for r in VARIED:
        vg.add_manifest_row(c, r, eligible, 1)
    c = vg.finalize(c)
    assert (c["documents"], c["excluded"], c["text_chars"], c["corpus_chars"]) == (
        stats.documents, stats.excluded, stats.text_chars, stats.corpus_chars)
    assert c["topics"] == {vg._label(t): n for t, n in stats.topics}
    assert c["licenses"] == {vg._label(k): n for k, n in stats.licenses.items()}
    assert c["rows"]["manifest"] == len(VARIED)
    # removing every row again leaves nothing (the delta arithmetic is exact)
    for r in VARIED:
        vg.add_manifest_row(c, r, eligible, -1)
    assert vg.finalize(c) == vg.finalize(vg.new_counters())


def test_combine_and_negative():
    a = vg.finalize({**vg.new_counters(), "documents": 3, "topics": {"urban": 3}})
    d = {**vg.new_counters(), "documents": -4, "topics": {"urban": -4}}
    got = vg.combine(a, d)
    assert got["documents"] == -1 and vg.negative(got) == ["documents", "topics.urban"]


def test_a_reporting_gate_that_passes_without_its_report_is_failed(tmp_path):
    path = tmp_path / "contracts.json"
    assert vg.read_gate_report("contracts", path, True) == (
        False, None, "gate contracts passed without handing back its report")
    assert vg.read_gate_report("lint", path, True) == (True, None, None)
    assert vg.read_gate_report("contracts", path, False)[0] is False
    path.write_text("{not json")
    assert vg.read_gate_report("contracts", path, True)[0] is False
    path.write_text(json.dumps({"counters": {"v": 1}}))
    assert vg.read_gate_report("contracts", path, True) == (True, {"counters": {"v": 1}}, None)
    path.write_text(json.dumps({"x": "y" * (vg.MAX_REPORT_BYTES + 1)}))
    assert vg.read_gate_report("contracts", path, True)[0] is False


def test_write_report_only_when_the_coordinator_asked(tmp_path):
    vg.write_report({"a": 1}, env={})
    assert not list(tmp_path.iterdir())
    vg.write_report({"a": 1}, env={vg.REPORT_ENV: str(tmp_path / "r.json")})
    assert json.loads((tmp_path / "r.json").read_text()) == {"a": 1}


# --- PostgreSQL: per-run checks --------------------------------------------------------------------

class PgWorld:
    def __init__(self, tmp_path: Path, restrictions=None):
        import store_pg
        self.root = tmp_path / "repo"
        write_repo(self.root, entries=[], manifest=[], restrictions=restrictions or {}, policy={})
        (self.root / "registry" / "backends.json").write_text(json.dumps(
            {"_readme": "test", "find_x": {"script": "find_x.py", "args": [], "enabled": True}},
            indent=2) + "\n")
        self.st = store_pg.PgStore(self.root, dsn=DSN, schema=f"vg_{uuid.uuid4().hex[:12]}")
        self.st.pin_config_from_files()
        self.n = 0

    def q(self, statement, params=()):
        with self.st._connect(autocommit=True) as conn:
            return conn.execute(statement, params).fetchall()

    def run(self, batches, *, gates=("contracts",), promote=True, config=None, check=True,
            artifacts="unchecked"):
        """Open a run, stage `batches` ([(step, fn(recorder))]), freeze; with `check`, run the
        per-run checks at the frozen state and record them as the contracts receipt; promote.
        Returns (run id, report)."""
        import store_broker
        self.n += 1
        run_id = rid(f"vg-{self.n}")
        st = self.st
        with st.writer() as w:
            st.open_run(w, run_id, producer_commit=SHA, extractor_version="x1",
                        cleaning_ruleset="none", artifacts=artifacts, config_documents=config)
            for i, (step, fn) in enumerate(batches):
                rec = store_broker.Recorder()
                fn(rec)
                with st.read_staged(run_id, writer=w) as v:
                    version = v.version()
                st.stage_batch(w, run_id, step, f"b{i}", rec.requests, expected_version=version)
            if artifacts == "versioned":
                gates = (*gates, "artifacts")
            frozen = st.freeze(w, run_id, required_gates=list(gates))
            if artifacts == "versioned":
                import artifact_store
                verified = artifact_store.verify_run(st, w, run_id)
                st.record_gate(w, frozen, "artifacts", passed=not verified["failed"])
            rep = None
            if check:
                with st.read_staged(run_id, seq=frozen.seq, writer=w) as view:
                    restrictions, _ = store.pinned_policy(view)
                    rep = vg.run_checks(view, restrictions, root=self.root)
                if not rep.errors:
                    st.record_gate(w, frozen, "contracts", passed=True,
                                   detail={"report": rep.as_report()})
            if promote and rep is not None and not rep.errors:
                st.promote(w, frozen)
            elif not promote or (rep is not None and rep.errors):
                st.abort_run(w, run_id, reason="test")
        return run_id, rep

    def full(self):
        with self.st.read() as view:
            restrictions, _ = store.pinned_policy(view)
            return vg.full_counters(view, restrictions)

    def close(self):
        self.st.drop()


@pytest.fixture
def pgw(tmp_path):
    if not DSN:
        pytest.skip("NEKAISE_PG_TEST_DSN not set")
    w = PgWorld(tmp_path, restrictions=RESTRICTIONS)
    yield w
    w.close()


def add(rows):
    def fn(tx):
        tx.insert_entries([entry(r) for r in rows])
        tx.upsert_manifest(rows)
    return fn


@needs_pg
def test_counters_chain_from_generation_to_generation(pgw):
    _, rep = pgw.run([("discover", add(VARIED))])
    assert rep.mode == "full" and not rep.errors and rep.full_checks   # the first generation
    assert rep.counters == pgw.full()
    # a second run: updates, a new row, a prune with its ledger and blocklist
    gone = VARIED[0]

    def prune(tx):
        tx.update_manifest_fields({VARIED[1]["id"]: {"corpus_chars": 5}})
        tx.delete_entries([gone["id"]], reason="prune: junk")
        tx.delete_manifest([gone["id"]], reason="prune: junk")
        tx.blocklist_add([gone["url"]])
        tx.ledger_append([{"id": gone["id"], "url": gone["url"], "reason": "junk",
                           "pruned_at": "2026-09-25T00:00:00Z", "blocklisted": True}])
    new = [mrow(50, text_chars=77), mrow(49, text_chars=12.5, corpus_chars=3.25)]
    _, rep = pgw.run([("fetch", add(new)), ("prune", prune)])
    assert rep.mode == "delta" and not rep.errors and not rep.full_checks
    assert rep.counters == pgw.full()
    # a non-integral character count is counted apart, never summed inexactly
    assert rep.counters["fractional"] == {"text_chars": 1, "corpus_chars": 1}
    assert rep.changed == {"entries": 3, "manifest": 4, "blocklist": 1, "ledger": 1}
    # the recorded counters of each generation are its contracts receipt at its frozen state
    with pgw.st.read() as view:
        assert vg.recorded_counters(view, view.generation) == rep.counters
        assert vg.recorded_counters(view, 7) is None
    # additions alone (every row counted once, through its contribution)
    _, rep = pgw.run([("fetch", add([mrow(51 + i) for i in range(5)]))])
    assert rep.mode == "delta" and rep.counters == pgw.full()
    assert rep.counters["rows"]["manifest"] == len(VARIED) + 2 - 1 + 5   # +2 fetched, -1 pruned


@needs_pg
def test_without_recorded_parent_counters_the_run_recounts(pgw):
    pgw.run([("discover", add(VARIED[:3]))], check=False, gates=("tests",), promote=False)
    # a generation promoted without a contracts receipt (e.g. before step 5)
    import store_broker
    st = pgw.st
    with st.writer() as w:
        st.open_run(w, rid("vg-legacy"), producer_commit=SHA, extractor_version="x1",
                    cleaning_ruleset="none", artifacts="unchecked")
        rec = store_broker.Recorder()
        add(VARIED[:3])(rec)
        with st.read_staged(rid("vg-legacy"), writer=w) as v:
            st.stage_batch(w, rid("vg-legacy"), "discover", "b", rec.requests,
                           expected_version=v.version())
        fr = st.freeze(w, rid("vg-legacy"), required_gates=["tests"])
        st.record_gate(w, fr, "tests", passed=True)
        st.promote(w, fr)
    _, rep = pgw.run([("fetch", add([mrow(60)]))])
    assert rep.mode == "full" and not rep.full_checks and rep.counters == pgw.full()


@needs_pg
def test_a_configuration_change_runs_the_full_checks(pgw):
    pgw.run([("discover", add(VARIED[:4]))])
    docs = {name: store.config_path(name, pgw.root).read_bytes()
            for name in store.CONFIG_FILES if store.config_path(name, pgw.root).exists()}
    docs["eligibility.json"] = (json.dumps({"version": 1, "restrictions": {
        **RESTRICTIONS, "osti": restriction({"source": "osti"})}}, indent=2) + "\n").encode()
    _, rep = pgw.run([("fetch", add([mrow(70, source="nist")]))], config=docs)
    assert rep.full_checks and rep.mode == "full"
    # the new restriction makes rows that claim corpus data a violation found by the full check
    assert rep.counters["documents"] == 1 and rep.counters["excluded"] >= 3


@needs_pg
def test_a_restricted_row_that_claims_corpus_data_fails(pgw):
    pgw.run([("discover", add(VARIED[:2]))])
    _, rep = pgw.run([("fetch", add([mrow(80, source="soep", corpus_path="corpus/x.md")]))])
    assert any("restricted row claims corpus data" in e for e in rep.errors)


@needs_pg
def test_ledger_rules(pgw):
    pgw.run([("discover", add(VARIED[:3]))])
    kept = VARIED[1]

    def bad_ledger(tx):   # a "pruned" document that is still there, a blocklisted url that is not
        tx.ledger_append([{"id": kept["id"], "url": kept["url"], "reason": "junk",
                           "pruned_at": "2026-09-25T00:00:00Z", "blocklisted": True},
                          {"id": "ost-v-9999", "url": "", "reason": "junk",
                           "pruned_at": "2026-09-25T00:00:00Z"}])
    _, rep = pgw.run([("prune", bad_ledger)])
    text = "\n".join(rep.errors)
    assert "still in the registry or manifest" in text
    assert "recorded as blocklisted but its URL is not" in text
    assert "missing url" in text


@needs_pg
def test_revision_integrity_and_derived_keys_are_checked(pgw):
    pgw.run([("discover", add(VARIED[:3]))])
    import store_broker
    st = pgw.st
    with st.writer() as w:
        st.open_run(w, rid("vg-bad"), producer_commit=SHA, extractor_version="x1",
                    cleaning_ruleset="none", artifacts="unchecked")
        rec = store_broker.Recorder()
        rec.upsert_manifest([mrow(90)])
        rec.upsert_entries([entry(mrow(90))])
        with st.read_staged(rid("vg-bad"), writer=w) as v:
            st.stage_batch(w, rid("vg-bad"), "fetch", "b", rec.requests, expected_version=v.version())
        fr = st.freeze(w, rid("vg-bad"), required_gates=["contracts"])
        # corrupt a derived column and a digest behind the store's back (triggers off)
        with st._connect(autocommit=True) as conn:
            conn.execute("ALTER TABLE revisions DISABLE TRIGGER USER")
            conn.execute("UPDATE revisions SET url_key = %s WHERE run_id = %s AND "
                         "tbl = 'manifest'", [b"x" * 32, rid("vg-bad")])
            conn.execute("UPDATE revisions SET row_sha256 = %s WHERE run_id = %s AND "
                         "tbl = 'entries'", ["f" * 64, rid("vg-bad")])
            conn.execute("ALTER TABLE revisions ENABLE TRIGGER USER")
        with st.read_staged(rid("vg-bad"), seq=fr.seq, writer=w) as view:
            rep = vg.run_checks(view, RESTRICTIONS, root=pgw.root)
        st.abort_run(w, rid("vg-bad"), reason="test")
    text = "\n".join(rep.errors)
    assert "manifest ost-v-0090: derived columns differ" in text
    assert "entries ost-v-0090: stored digest does not match" in text


@needs_pg
def test_versioned_claims_must_be_held_and_registered(pgw):
    import artifact_store
    pgw.run([("discover", add(VARIED[:2]))])
    local = artifact_store.LocalArtifacts(pgw.root)
    raw = local.put_bytes("raw", b"raw bytes of a new document")
    row = mrow(95, sha256=raw.sha256, bytes=raw.size, raw_path="raw/osti/ost-v-0095.pdf")
    _, rep = pgw.run([("fetch", add([row]))], artifacts="versioned")
    assert not rep.errors, rep.errors
    # the version disappears after the run registered it: the run's check names it
    row2 = mrow(96, sha256=local.put_bytes("raw", b"another").sha256, bytes=7,
                raw_path="raw/osti/ost-v-0096.pdf")
    import store_broker
    st = pgw.st
    with st.writer() as w:
        st.open_run(w, rid("vg-art"), producer_commit=SHA, extractor_version="x1",
                    cleaning_ruleset="none")
        rec = store_broker.Recorder()
        add([row2])(rec)
        with st.read_staged(rid("vg-art"), writer=w) as v:
            st.stage_batch(w, rid("vg-art"), "fetch", "b", rec.requests, expected_version=v.version())
        fr = st.freeze(w, rid("vg-art"), required_gates=["artifacts", "contracts"])
        path = local.path("raw", row2["sha256"])
        os.chmod(path, 0o644)
        path.unlink()
        with st.read_staged(rid("vg-art"), seq=fr.seq, writer=w) as view:
            rep = vg.run_checks(view, RESTRICTIONS, root=pgw.root)
        st.abort_run(w, rid("vg-art"), reason="test")
    assert any("is not held locally" in e for e in rep.errors)


# --- PostgreSQL: sweeps, GC report, rollback export ------------------------------------------------

@needs_pg
def test_the_metadata_sweep_resumes_and_finds_what_rounds_do_not(pgw, tmp_path):
    import integrity_sweep
    pgw.run([("discover", add(CLEAN))])
    pgw.run([("fetch", add([mrow(100 + i) for i in range(30)]))])
    state = tmp_path / "sweep.json"
    logs = []
    # tiny pages and no time: the pass advances one page per invocation and resumes
    out = integrity_sweep.metadata(pgw.st, seconds=0, restart=False, page=7,
                                   state_path=state, log=logs.append)
    assert out["metadata"]["current"]["phase"] == "entries"
    for _ in range(200):
        out = integrity_sweep.metadata(pgw.st, seconds=0.001, restart=False, page=7,
                                       state_path=state, log=logs.append)
        if out["metadata"].get("current") is None:
            break
    done = out["metadata"]
    assert done["failure_count"] == 0 and done["recorded"] == "equal", done.get("failures")
    assert done["checked"]["manifest"] == len(CLEAN) + 30
    assert done["counters_full"] == pgw.full()
    assert pgw.q("SELECT count(*) FROM generation_retention") == [(0,)]   # nothing pinned
    # damage a projection row's derived column after folding: the next pass finds it
    import store_staging
    with pgw.st.writer() as w:
        store_staging.fold_all(pgw.st, w)
    pgw.q("UPDATE manifest SET title_key = %s WHERE id = 'ost-v-0003' RETURNING id", [b"z" * 32])
    out = integrity_sweep.metadata(pgw.st, seconds=60, restart=True,
                                   state_path=state, log=logs.append)
    assert any("ost-v-0003: projection derived columns differ" in f
               for f in out["metadata"]["failures"])


@needs_pg
def test_a_sweep_pass_spans_promotions_and_folds_without_holding_them(pgw, tmp_path):
    """Nothing is pinned: generations promoted and folded while a pass is under way are swept
    by its later slices, and the counters step compares the generation current at the end."""
    import integrity_sweep
    import store_staging
    pgw.run([("discover", add(CLEAN[:3]))])
    state = tmp_path / "sweep.json"
    integrity_sweep.metadata(pgw.st, seconds=0, restart=False, page=1, state_path=state,
                             log=lambda *_: None)
    pgw.run([("fetch", add([mrow(200)]))])
    with pgw.st.writer() as w:
        assert store_staging.fold_all(pgw.st, w) == 2      # the fold is never held back
    for _ in range(100):
        out = integrity_sweep.metadata(pgw.st, seconds=60, restart=False, state_path=state,
                                       log=lambda *_: None)
        if out["metadata"].get("current") is None:
            break
    done = out["metadata"]
    assert done["failure_count"] == 0, done["failures"]
    assert done["generations"] == [0, 1] and done["counters_generation"] == 1
    assert done["recorded"] == "equal" and done["counters_full"] == pgw.full()
@needs_pg
def test_artifact_reverification_finds_damage(pgw, tmp_path):
    import artifact_store
    import integrity_sweep
    local = artifact_store.LocalArtifacts(pgw.root)
    versions = [local.put_bytes("raw", f"bytes {i}".encode()) for i in range(5)]
    rows = [mrow(300 + i, sha256=v.sha256, bytes=v.size, raw_path=f"raw/osti/{i}.pdf")
            for i, v in enumerate(versions)]
    pgw.run([("fetch", add(rows))], artifacts="versioned")
    state = tmp_path / "sweep.json"
    out = integrity_sweep.artifacts(pgw.st, seconds=60, restart=False, page=2, state_path=state,
                                    log=lambda *_: None)
    assert out["artifacts"]["checked"] == 5 and out["artifacts"]["failure_count"] == 0
    damaged = local.path("raw", versions[2].sha256)
    os.chmod(damaged, 0o644)
    damaged.write_bytes(b"bytes X")   # same size, other bytes
    out = integrity_sweep.artifacts(pgw.st, seconds=60, restart=False, page=2, state_path=state,
                                    log=lambda *_: None)
    assert out["artifacts"]["failure_count"] == 1
    assert versions[2].sha256 in out["artifacts"]["failures"][0]


@needs_pg
def test_the_gc_report_is_reference_checked_and_deletes_nothing(pgw):
    import time as _time

    import artifact_gc
    import artifact_store
    local = artifact_store.LocalArtifacts(pgw.root)
    kept = local.put_bytes("raw", b"claimed by a promoted row")
    orphan_old = local.put_bytes("text", b"nobody claims this, long ago")
    local.put_bytes("text", b"nobody claims this yet")   # unreferenced but young
    linked = local.put_bytes("corpus", b"materialized somewhere")
    aborted = local.put_bytes("raw", b"claimed only by an aborted run")
    os.link(local.path("corpus", linked.sha256), pgw.root / "linked.md")
    pgw.run([("fetch", add([mrow(400, sha256=kept.sha256, bytes=kept.size,
                                 raw_path="raw/osti/400.pdf")]))], artifacts="versioned")
    pgw.run([("fetch", add([mrow(401, sha256=aborted.sha256, bytes=aborted.size,
                                 raw_path="raw/osti/401.pdf")]))], artifacts="versioned",
            promote=False)
    old = _time.time() - 40 * 86400
    for v, stage in ((orphan_old, "text"), (aborted, "raw")):
        os.utime(local.path(stage, v.sha256), (old, old))
    before = sorted(p for p in (pgw.root / "artifacts").rglob("*") if p.is_file())
    out = artifact_gc.report(pgw.st, pgw.root, grace_days=30)
    after = sorted(p for p in (pgw.root / "artifacts").rglob("*") if p.is_file())
    assert before == after and out["dry_run"]
    assert out["scanned"] == 5 and out["referenced"] == 1 and out["linked_elsewhere"] == 1
    assert out["young"] == 1
    assert sorted(out["candidates"]["sample"]) == sorted(
        [f"text/{orphan_old.sha256}", f"raw/{aborted.sha256}"])


@needs_pg
def test_the_rollback_export_is_a_verified_file_store_of_the_generation(pgw, tmp_path):
    import rollback_export
    import store_staging
    pgw.run([("discover", add(VARIED))])

    def more(tx):
        add([mrow(500), mrow(501, topic="urban")])(tx)
        tx.delete_entries([VARIED[0]["id"]], reason="prune: junk")
        tx.delete_manifest([VARIED[0]["id"]], reason="prune: junk")
        tx.blocklist_add([VARIED[0]["url"]])
        tx.ledger_append([{"id": VARIED[0]["id"], "url": VARIED[0]["url"], "reason": "junk",
                           "pruned_at": "2026-09-25T00:00:00Z"}])
        tx.rotation_set("find_x", {"next": 3})
        tx.backend_state_set("find_x", store.BackendState(False, "exhausted: test"))
    pgw.run([("prune", more)])
    with pgw.st.writer() as w:   # half the history folded, half still an overlay
        store_staging.fold(pgw.st, w, limit=5)
    target = tmp_path / "rollback"
    meta = rollback_export.export(pgw.st, target, log=lambda *_: None)
    assert meta["identical"] and meta["generation"] == 1
    assert meta["counts"]["manifest"] == len(VARIED) + 1
    fs = store.open(root=target, backend="file")
    assert fs.validate_layout()[0] == []
    with fs.read() as view:
        assert view.rotation_get() == {"find_x": {"next": 3}}
        assert view.backend_state_get()["find_x"] == store.BackendState(False, "exhausted: test")
    # an existing target is refused; a tampered tree does not verify
    with pytest.raises(rollback_export.ExportError, match="exists"):
        rollback_export.export(pgw.st, target, log=lambda *_: None)
    shard = next((target / "manifest").glob("*.jsonl"))
    shard.write_text(shard.read_text().replace('"text_chars": 1002', '"text_chars": 1003'))
    assert not rollback_export.verify(pgw.st, target, None, log=lambda *_: None)["identical"]


def test_labels_are_injective_and_refuse_ungroupable_values():
    labels = [vg._label(v) for v in ("urban", "json:null", "str:x", None, True, False, "true")]
    assert len(set(labels)) == len(labels)
    with pytest.raises(vg.VerifyError):
        vg._label(3)


def test_a_report_about_another_frozen_state_fails_the_gate(tmp_path):
    path = tmp_path / "contracts.json"
    path.write_text(json.dumps({"run": "r1", "seq": 4, "counters": {"v": 1}}))
    assert vg.read_gate_report("contracts", path, True, expect={"run": "r1", "seq": 4})[0]
    passed, _, why = vg.read_gate_report("contracts", path, True, expect={"run": "r1", "seq": 5})
    assert not passed and "not the frozen r1 at 5" in why
    assert not vg.read_gate_report("contracts", path, True, expect={"run": "r2", "seq": 4})[0]
    assert vg.read_gate_report("tests", None, True) == (True, None, None)


@needs_pg
def test_incremental_lint_catches_a_restriction_the_run_emptied(pgw):
    import lint_registry
    import store_broker
    soep = mrow(700, source="soep", status="failed", text_chars=...)
    pgw.run([("discover", add(CLEAN[:2] + [soep]))])
    st = pgw.st
    with st.writer() as w:
        run_id = rid("vg-lint")
        st.open_run(w, run_id, producer_commit=SHA, extractor_version="x1",
                    cleaning_ruleset="none", artifacts="unchecked")
        rec = store_broker.Recorder()
        rec.delete_entries([soep["id"]], reason="prune: junk")
        rec.delete_manifest([soep["id"]], reason="prune: junk")
        with st.read_staged(run_id, writer=w) as v:
            st.stage_batch(w, run_id, "prune", "b", rec.requests, expected_version=v.version())
        fr = st.freeze(w, run_id, required_gates=["lint"])
        with st.read_staged(run_id, seq=fr.seq, writer=w) as view:
            errors, _, _ = lint_registry.changed_lint(view)
        st.abort_run(w, run_id, reason="test")
    assert errors == ["eligibility restriction 'soep' matches no registry entries"]


@needs_pg
def test_the_gc_report_skips_what_is_not_the_content_addressed_layout(pgw):
    import artifact_gc
    import artifact_store
    local = artifact_store.LocalArtifacts(pgw.root)
    v = local.put_bytes("text", b"a version filed in the right place")
    wrong = pgw.root / "artifacts" / "text" / "00" / "00" / v.sha256
    wrong.parent.mkdir(parents=True, exist_ok=True)
    os.link(local.path("text", v.sha256), wrong)                 # a misfiled copy
    (pgw.root / "artifacts" / "text" / "stray.txt").write_text("x")
    pgw.run([("fetch", add([mrow(710, text_path="text/x.md", text_sha256=v.sha256)]))],
            artifacts="versioned")
    out = artifact_gc.report(pgw.st, pgw.root, grace_days=0)
    assert out["misplaced"] == 2 and out["referenced"] == 1 and out["candidates"]["count"] == 0



@needs_pg
def test_lifecycle_alerts_over_a_staged_schema(pgw, monkeypatch):
    import time as _time

    import ops_health
    import store_staging
    pgw.run([("discover", add(CLEAN[:2]))])
    pgw.run([("fetch", add([mrow(900)]))])
    monkeypatch.setattr(ops_health, "FOLD_LAG_WARN", 1)
    with pgw.st._connect(autocommit=True) as conn:
        checks = {c["check"]: c for c in ops_health.lifecycle_checks(conn, _time.time(),
                                                                     root=pgw.root)}
    assert checks["stalled_runs"]["severity"] == "ok"
    assert checks["fold_lag"]["facts"]["unfolded"] == 2
    assert checks["fold_lag"]["severity"] == "warning"
    assert checks["review_backlog"]["facts"]["backlog"] == 2
    assert checks["materialization_lag"]["facts"]["state"] is None   # never materialized here
    # an open run left behind is reported with its age; the fold clears the lag
    with pgw.st.writer() as w:
        pgw.st.open_run(w, rid("vg-stall"), producer_commit=SHA, extractor_version="x1",
                        cleaning_ruleset="none", artifacts="unchecked")
        store_staging.fold_all(pgw.st, w)
    with pgw.st._connect(autocommit=True) as conn:
        checks = {c["check"]: c for c in ops_health.lifecycle_checks(conn, _time.time(),
                                                                     root=pgw.root)}
    assert checks["stalled_runs"]["facts"]["runs"][0]["run"] == rid("vg-stall")
    assert checks["fold_lag"]["facts"]["unfolded"] == 0


@needs_pg
def test_the_one_pass_recount_equals_corpus_stats_and_the_row_arithmetic(pgw):
    """full_counters (one grouped pass in the server) against corpus_stats.compute (its
    aggregates) and against the per-row arithmetic the gates chain, on varied rows."""
    import corpus_stats
    rows = VARIED + [mrow(60, text_chars=12.5, corpus_chars=7.75), mrow(61, status="skipped")]
    pgw.run([("discover", add(rows))])
    with pgw.st.read() as view:
        restrictions, _ = store.pinned_policy(view)
        full = vg.full_counters(view, restrictions)
        stats = corpus_stats.compute(view, restrictions)
    assert (full["documents"], full["excluded"]) == (stats.documents, stats.excluded)
    assert full["topics"] == {vg._label(t): n for t, n in stats.topics}
    assert full["licenses"] == {vg._label(k): n for k, n in stats.licenses.items()}
    mine = vg.new_counters()
    eligible = store.eligibility_where(restrictions)
    for r in rows:
        vg.add_manifest_row(mine, r, eligible, 1)
    mine["rows"]["entries"] = len(rows)
    assert vg.finalize(mine) == full
    assert full["fractional"] == {"text_chars": 1, "corpus_chars": 1}
