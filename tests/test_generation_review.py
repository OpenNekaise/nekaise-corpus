"""Stage 4 step 4 (ADR 0001): the generation-range review — evidence per contiguous range of
promoted generations, verdicts persisted in the database with a contiguous reviewed watermark,
findings withholding endorsement (and so publication), integrity findings blocking growth, and
repairs as compensating generations. Opt-in: NEKAISE_PG_TEST_DSN."""
from __future__ import annotations

import json
import os
import uuid

import pytest

import generation_review
from runids import rid
from test_store_contract import write_config
from test_store_pg_recovery import finish, open_run, stage, v6_code
from test_store_pg_staging import mrow, q

pytestmark = pytest.mark.skipif(not os.environ.get("NEKAISE_PG_TEST_DSN"),
                                reason="NEKAISE_PG_TEST_DSN not set")
DSN = os.environ.get("NEKAISE_PG_TEST_DSN", "")
SHA = "0" * 40


def sqlerr():
    import psycopg
    return psycopg.Error


@pytest.fixture
def pg(tmp_path):
    import store_pg
    root = tmp_path / "pg"
    write_config(root)
    st = store_pg.PgStore(root, dsn=DSN, schema=f"gr_{uuid.uuid4().hex[:12]}")
    st.pin_config_from_files()
    yield st
    st.drop()


def generations(pg, w, n, *, fail_between=False):
    """`n` promoted generations (each one manifest row); optionally an aborted attempt before
    each."""
    for _ in range(n):
        if fail_between:
            dead = rid(f"gr-dead-{uuid.uuid4().hex[:6]}")
            open_run(pg, w, dead)
            pg.abort_run(w, dead, reason="a gate failed")
        run_id = rid(f"gr-{uuid.uuid4().hex[:6]}")
        open_run(pg, w, run_id)
        stage(pg, w, run_id, "b1", lambda tx, r=run_id: tx.upsert_manifest(
            [mrow(f"d-{r}"), mrow(f"f-{r}", status="failed")]))
        finish(pg, w, run_id)


def verdict(pg, w, through, kind="ok", resolves=(), summary="s"):
    ev = generation_review.evidence(pg, w, through=through, root=pg.root, bases=pg.root / "no-backups")
    return generation_review.record(pg, w, through=through, verdict=kind, reviewer="test",
                                    evidence_digest=ev["digest"],
                                    detail={"summary": summary}, resolves=list(resolves),
                                    root=pg.root)


# --- evidence ---------------------------------------------------------------------------------------------

def test_evidence_covers_the_unreviewed_range_and_its_digest_binds_the_verdict(pg):
    with pg.writer() as w:
        generations(pg, w, 2, fail_between=True)
        ev = generation_review.evidence(pg, w, root=pg.root, bases=pg.root / "no-backups")
        assert ev["range"] == [0, 1] and ev["current_generation"] == 1
        g0 = ev["generations"][0]
        assert g0["gates"] == {"tests": {"verdict": "passed"}}
        assert g0["manifest_puts_by_status"] == {"failed": 1, "ok": 1}
        assert g0["counts"]["ops"] == {"manifest": {"upsert": 2}}
        assert ev["failures"]["total"] == 2
        assert {f["detail"]["aborted"] for f in ev["failures"]["shown"]} == {"a gate failed"}
        assert "wal" in ev["backup_health"]
        assert "error" in ev["backup_health"]["base_backup"]      # reported, never hidden
        with pytest.raises(generation_review.ReviewError, match="digest differs"):
            generation_review.record(pg, w, through=1, verdict="ok", reviewer="test",
                                     evidence_digest="0" * 64, root=pg.root)
        # a reviewer who saw only generation 0 cannot endorse 0..1 with that digest
        partial = generation_review.evidence(pg, w, through=0, root=pg.root, bases=pg.root / "no-backups")
        with pytest.raises(generation_review.ReviewError, match="digest differs"):
            generation_review.record(pg, w, through=1, verdict="ok", reviewer="test",
                                     evidence_digest=partial["digest"], root=pg.root)
        after = generation_review.record(pg, w, through=0, verdict="ok", reviewer="test",
                                         evidence_digest=partial["digest"], root=pg.root)
        assert (after["reviewed_through"], after["endorsed_through"]) == (0, 0)
        assert generation_review.evidence(pg, w, root=pg.root, bases=pg.root / "no-backups")["range"] == [1, 1]


def test_configuration_decisions_are_named_in_the_evidence(pg, tmp_path):
    with pg.writer() as w:
        generations(pg, w, 1)
        docs = {"backends.json": b'{"find_x": {"script": "x.py", "enabled": false}}\n'}
        run_id = rid("gr-cfg")
        pg.open_run(w, run_id, producer_commit=SHA, extractor_version="x1",
                    cleaning_ruleset="rules-1", artifacts="unchecked", config_documents=docs)
        finish(pg, w, run_id)
        ev = generation_review.evidence(pg, w, root=pg.root, bases=pg.root / "no-backups")
    assert ev["decisions"] == [{"generation": 1, "cleaning_ruleset": None,
                                "configuration_changed": ["backends.json", "eligibility.json",
                                                          "vendors.json"]}]


# --- verdicts, watermarks, endorsement ---------------------------------------------------------------------------

def test_findings_withhold_endorsement_until_a_later_verdict_resolves_them(pg):
    with pg.writer() as w:
        generations(pg, w, 1)
        assert verdict(pg, w, 0)["endorsed_through"] == 0
        generations(pg, w, 1)
        s = verdict(pg, w, 1, "finding", summary="yield dropped")
        assert (s["reviewed_through"], s["endorsed_through"], s["open_findings"]) == (1, 0, 1)
        generations(pg, w, 1)
        s = verdict(pg, w, 2)                         # ok, but the finding is still open
        assert (s["reviewed_through"], s["endorsed_through"]) == (2, 0)
        generations(pg, w, 1)                         # the repair: a compensating generation
        s = verdict(pg, w, 3, resolves=[2])
        assert (s["reviewed_through"], s["endorsed_through"], s["open_findings"]) == (3, 3, 0)
        # an empty range only records a finding about generations already reviewed; it neither
        # endorses nor resolves (the resolution must cover a later, compensating generation)
        s = verdict(pg, w, 3, "finding", summary="noticed late")   # empty range, a new finding
        assert (s["endorsed_through"], s["open_findings"]) == (3, 1)
        with pytest.raises(sqlerr(), match="at least one generation"):
            verdict(pg, w, 3)
        generations(pg, w, 1)
        assert verdict(pg, w, 4, resolves=[5])["open_findings"] == 0
    assert q(pg, "SELECT seq, lo_generation, hi_generation, verdict, resolved_by FROM "
                 "review_verdicts ORDER BY seq") == [
        (1, 0, 0, "ok", None), (2, 1, 1, "finding", 4), (3, 2, 2, "ok", None),
        (4, 3, 3, "ok", None), (5, 4, 3, "finding", 6), (6, 4, 4, "ok", None)]
    # the review consumer is the verdicts' outbox image: acknowledged and advanced by them
    assert q(pg, "SELECT seq, verdict FROM outbox_acks WHERE consumer = 'review' ORDER BY seq") \
        == [(1, "ok"), (2, "finding"), (3, "ok"), (4, "ok"), (5, "ok")]
    assert q(pg, "SELECT watermark FROM outbox_consumers WHERE consumer = 'review'") == [(5,)]


def test_a_finding_is_resolved_only_by_a_verdict_covering_a_later_generation(pg):
    """The repair is a compensating generation: a verdict written in the same pass as the
    finding's repair attempt — before the repair was promoted — cannot resolve it, nor can an
    empty range."""
    with pg.writer() as w:
        generations(pg, w, 2)
        verdict(pg, w, 0, "integrity", summary="broken rows")
        with pytest.raises(sqlerr(), match="compensating repair"):
            verdict(pg, w, 0, "finding", resolves=[1], summary="empty range")
        s = verdict(pg, w, 1, resolves=[1])           # generation 1 was promoted after it
        assert (s["open_integrity"], s["endorsed_through"]) == (0, 1)


def test_publication_never_passes_the_endorsed_generation(pg):
    with pg.writer() as w:
        generations(pg, w, 2)
        verdict(pg, w, 0)
        with pg.contracts(w) as c:
            c.ack("publication", 1, "ok")
            c.ack("publication", 2, "ok")
            with pytest.raises(sqlerr(), match="endorsed"), c._conn.transaction():
                c.advance("publication")          # would pass generation 1 (not reviewed)
        verdict(pg, w, 1, "finding")
        with pg.contracts(w) as c:
            with pytest.raises(sqlerr(), match="withheld"), c._conn.transaction():
                c.advance("publication")          # a finding is open
    assert q(pg, "SELECT watermark FROM outbox_consumers WHERE consumer = 'publication'") == [(0,)]


def test_a_finding_raised_after_endorsement_stops_publication(pg):
    """Endorse through 1 while publication lags at 0; then an empty-range integrity finding
    about the reviewed data: endorsed_through stays 1, yet publication may not advance — until a
    verdict covering the compensating repair resolves it."""
    with pg.writer() as w:
        generations(pg, w, 2)
        assert verdict(pg, w, 1)["endorsed_through"] == 1
        with pg.contracts(w) as c:
            c.ack("publication", 1, "ok")
            assert c.advance("publication") == 1     # publication lags behind endorsement
            c.ack("publication", 2, "ok")
        s = verdict(pg, w, 1, "integrity", summary="found after endorsement")
        assert (s["endorsed_through"], s["open_integrity"], s["publishable_through"]) == (
            1, 1, None)
        with pg.contracts(w) as c:
            with pytest.raises(sqlerr(), match="withheld"), c._conn.transaction():
                c.advance("publication")
        generations(pg, w, 1)                        # the repair
        assert verdict(pg, w, 2, resolves=[2])["publishable_through"] == 2
        with pg.contracts(w) as c:
            c.ack("publication", 3, "ok")
            assert c.advance("publication") == 3


def test_a_publication_advance_and_a_new_finding_never_both_commit(pg):
    """Two connections: the publication advance holds the review state row (it writes it);
    a finding recorded meanwhile waits and then either follows the advance or — under
    REPEATABLE READ — fails; never does publication pass a finding it did not see."""
    import psycopg
    with pg.writer() as w:
        generations(pg, w, 1)
        verdict(pg, w, 0)
        with pg.contracts(w) as c:
            c.ack("publication", 1, "ok")
    publisher, reviewer = pg._connect(), pg._connect()
    try:
        publisher.execute("UPDATE outbox_consumers SET watermark = 1 WHERE consumer = "
                          "'publication'")               # uncommitted: holds review_state
        import threading
        done = {}

        def review():
            try:
                reviewer.execute("INSERT INTO review_verdicts (seq, lo_generation, hi_generation, "
                                 "verdict, reviewer, evidence_digest) VALUES (2, 1, 0, 'integrity', "
                                 "'t', repeat('0', 64))")
                reviewer.commit()
            except psycopg.Error as exc:
                done["exc"] = exc
        t = threading.Thread(target=review)
        t.start()
        t.join(1.0)
        assert t.is_alive(), "the finding did not wait for the publication advance"
        publisher.commit()
        t.join(30)
    finally:
        publisher.close()
        reviewer.close()
    # the finding committed after the advance (READ COMMITTED re-reads): it now stops any further
    # publication
    assert q(pg, "SELECT open_integrity FROM review_state") == [(1,)]


def test_verdicts_are_contiguous_immutable_and_only_resolve_open_findings(pg):
    with pg.writer() as w:
        generations(pg, w, 3)
        verdict(pg, w, 0, "finding")
    with pg._connect(autocommit=True) as conn:
        insert = ("INSERT INTO review_verdicts (seq, lo_generation, hi_generation, verdict, "
                  "reviewer, evidence_digest, resolves_text) VALUES (%s, %s, %s, %s, 't', %s, %s)")
        for params in ((2, 2, 2, "ok", "0" * 64, "[]"),       # a gap: generation 1 skipped
                       (3, 1, 1, "ok", "0" * 64, "[]"),       # not the next verdict number
                       (2, 1, 9, "ok", "0" * 64, "[]"),       # not promoted
                       (2, 1, 1, "ok", "0" * 64, "[2]"),      # resolves no open finding
                       (2, 1, 1, "ok", "0" * 64, "[1, 1]"),   # not canonical
                       (2, 1, 1, "bogus", "0" * 64, "[]")):
            with pytest.raises(sqlerr()):
                conn.execute(insert, params)
        for stmt in ("UPDATE review_verdicts SET verdict = 'ok'",
                     "UPDATE review_verdicts SET resolved_by = 1",
                     "DELETE FROM review_verdicts",
                     "UPDATE review_state SET endorsed_through = 2",
                     "DELETE FROM review_state",
                     "INSERT INTO outbox_acks (consumer, seq, verdict) VALUES ('review', 2, 'ok')",
                     "UPDATE outbox_consumers SET watermark = 2 WHERE consumer = 'review'"):
            with pytest.raises(sqlerr()):
                conn.execute(stmt)
        conn.execute(insert, (2, 1, 2, "ok", "0" * 64, "[1]"))   # the next range, resolving 1
    assert q(pg, "SELECT reviewed_through, endorsed_through FROM review_state") == [(2, 2)]


def test_an_integrity_finding_blocks_growth_until_resolved(pg):
    with pg.writer() as w:
        generations(pg, w, 1)
        verdict(pg, w, 0, "integrity", summary="rows claim bytes that are not held")
        assert "integrity finding(s) [1]" in generation_review.growth_block(pg, w)
        generations(pg, w, 1)                         # a compensating repair (not a round)
        verdict(pg, w, 1, resolves=[1])
        assert generation_review.growth_block(pg, w) is None


def test_a_verdict_file_is_validated(tmp_path):
    good = {"through": 3, "verdict": "finding", "evidence_digest": "a" * 64, "summary": "s",
            "findings": ["yield fell by half"], "resolves": [1]}
    path = tmp_path / "v.json"
    path.write_text(json.dumps(good))
    assert generation_review.verdict_from_file(path) == good
    for bad in ({**good, "extra": 1}, {**good, "verdict": "fine"}, {**good, "through": True},
                {**good, "findings": []}, {**good, "resolves": [0]},
                {k: v for k, v in good.items() if k != "summary"}):
        path.write_text(json.dumps(bad))
        with pytest.raises(generation_review.ReviewError):
            generation_review.verdict_from_file(path)


# --- the migration: the review state is backfilled from existing acknowledgements -----------------------

def _v6_generations(v6, root, schema, review_verdicts):
    old = v6.PgStore(root, dsn=DSN, schema=schema)
    old.pin_config_from_files()
    with old.writer() as w:
        for i, _ in enumerate(review_verdicts):
            run_id = rid(f"gr-v6-{i}")
            old.open_run(w, run_id, producer_commit=SHA, extractor_version="x",
                         cleaning_ruleset="none", artifacts="unchecked")
            frozen = old.freeze(w, run_id, required_gates=["tests"])
            old.record_gate(w, frozen, "tests", passed=True)
            old.promote(w, frozen)
        with old.contracts(w) as c:
            for seq, kind in enumerate(review_verdicts, 1):
                c.ack("review", seq, kind)
            c.advance("review")


def test_the_review_state_is_backfilled_from_legacy_acknowledgements(tmp_path, monkeypatch):
    import store_pg
    root = tmp_path / "r"
    write_config(root)
    for kinds, want in ((["ok", "ok"], (1, 1)), (["ok", "finding"], None)):
        schema = f"m7r_{uuid.uuid4().hex[:12]}"
        try:
            with v6_code(monkeypatch) as v6:
                _v6_generations(v6, root, schema, kinds)
            if want is None:
                with pytest.raises(sqlerr(), match="migrate by hand"):
                    store_pg.PgStore(root, dsn=DSN, schema=schema)
                continue
            new = store_pg.PgStore(root, dsn=DSN, schema=schema)
            assert q(new, "SELECT reviewed_through, endorsed_through FROM review_state") == [want]
            with new.writer() as w:
                generations(new, w, 1)
                assert verdict(new, w, 2)["reviewed_through"] == 2
        finally:
            store_pg.PgStore(root, dsn=DSN, schema=schema, create=False).drop()
