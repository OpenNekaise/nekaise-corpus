"""Stage 4 step 2 (ADR 0001): run-scoped staging with constant-size promotion over PostgreSQL
(scripts/store_staging.py, schema v5). Opt-in: NEKAISE_PG_TEST_DSN.

The reference for every overlay read is a second PostgreSQL store to which the same batches were
applied directly as ordinary transactions (itself equal to FileStore by the conformance suite):
lookups, keyset pagination (key and legacy order, predicates, projections), membership,
aggregates, duplicate detection and the small control tables must agree at every staging
sequence, after promotion, and before, during and after folding."""
import hashlib
import importlib.util
import json
import os
import random
import subprocess
import sys
import threading
import uuid
from pathlib import Path

import pytest

import store
import store_broker
import store_staging
from store import And, Eq, Exists, In, Not, Prefix, StaleView, StoreError, Table, VersionConflict
from test_store_contract import write, write_config

pytestmark = pytest.mark.skipif(not os.environ.get("NEKAISE_PG_TEST_DSN"),
                                reason="NEKAISE_PG_TEST_DSN not set")
DSN = os.environ.get("NEKAISE_PG_TEST_DSN", "")
REPO = Path(__file__).resolve().parents[1]
V4_COMMIT = "e8ba0d581b"  # last commit whose store_pg.py writes schema version 4 (stage 4 step 1)
SHA = "0" * 40


def sqlerr():
    import psycopg
    return psycopg.IntegrityError


def new_store(root, prefix):
    import store_pg
    write_config(root)
    st = store_pg.PgStore(root, dsn=DSN, schema=f"{prefix}_{uuid.uuid4().hex[:12]}")
    st.pin_config_from_files()
    return st


@pytest.fixture
def pg(tmp_path):
    st = new_store(tmp_path / "pg", "st")
    yield st
    st.drop()


@pytest.fixture
def pair(tmp_path):
    """(staged store, reference store) with the same seed and configuration."""
    a, b = new_store(tmp_path / "a", "sa"), new_store(tmp_path / "b", "sb")
    for st in (a, b):
        write(st, "seed", seed_rows)
    yield a, b
    a.drop()
    b.drop()


def entry(sid, **extra):
    return {"id": sid, "title": extra.pop("title", f"Title of {sid}"),
            "url": extra.pop("url", f"https://e.org/{sid}.pdf"), "source": "test",
            "license": extra.pop("license", "public-domain"),
            "topic": extra.pop("topic", "building_energy"), "format": "pdf", **extra}


def mrow(sid, **extra):
    base = {"status": "ok", "http_status": 200, "sha256": f"sha-{sid}", "bytes": 10,
            "raw_path": f"raw/test/{sid}.pdf", "text_path": f"text/{sid}.md", "text_chars": 100,
            "error": None, "fetched_at": "2026-09-25T00:00:00Z", "quality": {"total": 1.5}}
    for k in list(base):
        if k in extra:
            base[k] = extra.pop(k)
    return {**entry(sid, **extra), **base}


def seed_rows(tx):
    tx.insert_entries([entry(f"oer-{i}") for i in range(8)] + [entry("hand-1", title="Shared")])
    tx.upsert_manifest([mrow(f"oer-{i}", sha256="dup" if i % 3 == 0 else f"s{i}",
                             text_chars=[100, 1e20, 2.5, -0.0][i % 4]) for i in range(8)]
                       + [mrow("hand-1", status="failed", sha256=None, text_chars=None)])
    tx.blocklist_add(["https://e.org/blocked", "https://e.org/blocked/b"])
    tx.ledger_append([{"id": "oer-x", "reason": "junk"}, {"id": "oer-x", "reason": "junk"}])
    tx.rotation_set("find_books", {"flag": "--offset", "next": 5, "step": 5})
    tx.control_set("github_passes.json", {"gh_a": {"md": "2026-09-01"}})


def recorded(fn):
    rec = store_broker.Recorder()
    fn(rec)
    return rec.requests


def open_run(st, w, run_id="rnd1"):
    return st.open_run(w, run_id, producer_commit=SHA, extractor_version="x1",
                       cleaning_ruleset="rules-1")


def version_of(st, w, run_id="rnd1"):
    with st.read_staged(run_id, writer=w) as v:
        return v.version()


def stage(st, w, batch, fn, run_id="rnd1", step="fetch"):
    return st.stage_batch(w, run_id, step, batch, recorded(fn),
                          expected_version=version_of(st, w, run_id))


def finish(st, w, run_id="rnd1", gates=("tests",)):
    frozen = st.freeze(w, run_id, required_gates=gates)
    for g in gates:
        st.record_gate(w, frozen, g, passed=True)
    return st.promote(w, frozen)


def q(st, statement, params=()):
    with st._connect(autocommit=True) as conn:
        return conn.execute(statement, params).fetchall()


def expect_refused(st, statement, params=()):
    with st._connect(autocommit=True) as conn, pytest.raises(sqlerr()):
        conn.execute(statement, params)


# --- snapshots: everything a view answers ----------------------------------------------------------

M_PREDICATES = [None, Eq("topic", "building_energy"), Prefix("id", "oer-"), Not(Exists("error")),
                In("license", ["cc-by", "public-domain"]),
                And(Eq("status", "ok"), Not(Prefix("id", "hand"))), Eq("text_chars", 1e20),
                Eq("text_chars", 100), Exists("topic")]
E_PREDICATES = [None, Prefix("id", "new-"), Eq("title", "Shared"), Not(Eq("license", "cc-by"))]
IDS = [f"oer-{i}" for i in range(8)] + [f"new-{i}" for i in range(40)] + ["hand-1", "nope"]
URLS = [f"https://e.org/{i}.pdf" for i in IDS] + ["https://e.org/shared", "https://e.org/blocked/",
                                                   "https://e.org/blocked", "https://x.org/b/1"]
TITLES = [f"Title of {i}" for i in IDS] + ["Shared", "shared", "Other"]


def paged(view, table, limit=7, **kw):
    rows, cursor = [], None
    while True:
        page = view.scan(table, cursor=cursor, limit=limit, **kw)
        rows += page.rows
        if (cursor := page.next_cursor) is None:
            return rows


def snapshot(view) -> dict:
    out = {t.value: paged(view, t) for t in (Table.ENTRIES, Table.MANIFEST, Table.BLOCKLIST,
                                             Table.LEDGER)}
    for p in M_PREDICATES:
        out[f"m:{p!r}"] = paged(view, Table.MANIFEST, where=p, limit=3)
    for p in E_PREDICATES:
        out[f"e:{p!r}"] = paged(view, Table.ENTRIES, where=p, limit=4)
    out["legacy"] = paged(view, Table.MANIFEST, order="legacy", limit=5)
    out["legacy_where"] = paged(view, Table.MANIFEST, order="legacy", where=Prefix("id", "new"))
    out["fields"] = paged(view, Table.MANIFEST, fields=("id", "sha256", "status"), limit=100)
    out["agg"] = list(view.aggregate_manifest(group_by=("topic", "status"),
                                              sums=("text_chars", "bytes")))
    out["agg_all"] = list(view.aggregate_manifest(group_by=(), sums=("bytes",),
                                                  where=Prefix("id", "oer-")))
    out["dups"] = list(view.iter_duplicate_sha256(batch_size=3))
    out["dups_where"] = list(view.iter_duplicate_sha256(where=Eq("status", "ok")))
    out["get_m"] = view.get_manifest(IDS)
    out["get_e"] = view.get_entries(IDS)
    out["known"] = view.known(urls=URLS, titles=TITLES, ids=IDS)
    out["known_nobl"] = view.known(urls=URLS, include_blocklist=False)
    out["rotation"] = view.rotation_get()
    out["control"] = view.control_get("github_passes.json")
    out["backend_state"] = view.backend_state_get()
    out["enabled"] = {n: view.backend_enabled(n) for n in ("find_books", "find_paused")}
    out["config"] = view.config_get()
    out["artifact"] = [view.resolve_artifact(i, store.Stage.RAW) for i in IDS[:10]]
    return out


def assert_same(got: dict, want: dict) -> None:
    assert got.keys() == want.keys()
    for k in want:
        assert got[k] == want[k], k


# --- a random workload applied both ways ------------------------------------------------------------

class Workload:
    """Random valid batches over the reference store's current state (seeded)."""

    def __init__(self, seed: int):
        self.rng = random.Random(seed)
        self.fresh = 0

    def row(self, sid):
        r = self.rng
        return mrow(sid, topic=r.choice(["building_energy", "structures_civil", "urban"]),
                    status=r.choice(["ok", "ok", "failed"]),
                    sha256=r.choice([f"s-{sid}", "dup", "dup2", None]),
                    text_chars=r.choice([100, 1e20, 2.5, -0.0, 1.0, 1, None, 7e-07]),
                    license=r.choice(["public-domain", "cc-by", "open"]),
                    url=r.choice([f"https://e.org/{sid}.pdf", "https://e.org/shared/"]),
                    title=r.choice([f"Title of {sid}", "Shared", "  shared "]),
                    error=r.choice([None, None, "timeout"]), bytes=r.randint(0, 5))

    def batch(self, ref) -> list:
        r = self.rng
        with ref.read() as v:
            manifest = {x["id"]: x for x in paged(v, Table.MANIFEST, limit=1000)}
            entries = {x["id"] for x in paged(v, Table.ENTRIES, limit=1000)}
        ops = []
        for _ in range(r.randint(1, 5)):
            kind = r.choice(["upsert_m", "upsert_m", "patch", "del_m", "ins_e", "up_e", "del_e",
                             "block", "ledger", "rotation", "control", "backend", "replace"])
            ids = sorted(manifest)
            if kind == "upsert_m":
                sids = r.sample(ids, min(len(ids), 2)) + [self.new_id()]
                rows = [self.row(s) for s in sids]
                ops.append(lambda tx, rows=rows: tx.upsert_manifest(rows))
            elif kind == "patch" and ids:
                sid = r.choice(ids)
                patch = {"status": r.choice(["ok", "gone"]), "corpus_path": f"corpus/{sid}.md",
                         "text_chars": r.choice([1e20, 3, -0.0])}
                unset = tuple(r.sample(["error", "quality", "fetched_at"], r.randint(0, 2)))
                ops.append(lambda tx, u={sid: patch}, n=unset:
                           tx.update_manifest_fields(u, unset=n))
            elif kind == "del_m" and ids:
                gone = r.sample(ids, min(len(ids), r.randint(1, 3))) + ["missing-id"]
                ops.append(lambda tx, g=gone, why=r.choice(["prune: junk", "dup-bytes"]):
                           tx.delete_manifest(g, reason=why))
                for g in gone:
                    manifest.pop(g, None)
            elif kind == "ins_e":
                new = [entry(self.new_id(), title=r.choice(["Shared", "Fresh"]))]
                ops.append(lambda tx, n=new: tx.insert_entries(n))
            elif kind == "up_e" and entries:
                sid = r.choice(sorted(entries))
                ops.append(lambda tx, e=entry(sid, title=f"Renamed {r.random():.3f}"):
                           tx.upsert_entries([e]))
            elif kind == "del_e" and entries:
                gone = r.sample(sorted(entries), 1)
                ops.append(lambda tx, g=gone: tx.delete_entries(g, reason="prune: thin"))
                entries.difference_update(gone)
            elif kind == "block":
                urls = [r.choice(["https://e.org/shared/", "https://x.org/b/1", "https://e.org/blocked",
                                  f"https://x.org/b/{r.randint(2, 9)}"])]
                ops.append(lambda tx, u=urls: tx.blocklist_add(u))
            elif kind == "ledger":
                rows = [{"id": r.choice(["oer-x", "oer-y"]), "reason": "junk"}
                        for _ in range(r.randint(1, 3))]
                ops.append(lambda tx, rows=rows: tx.ledger_append(rows))
            elif kind == "rotation":
                ops.append(lambda tx, v={"flag": "--offset", "next": r.randint(0, 50), "step": 5,
                                         "f": r.choice([1.0, 1, 2.5])},
                           name=r.choice(["find_books", "find_x"]): tx.rotation_set(name, v))
            elif kind == "control":
                doc = r.choice([None, {"gh_a": {"md": f"2026-09-{r.randint(10, 28)}"}}])
                ops.append(lambda tx, d=doc: tx.control_set("github_passes.json", d))
            elif kind == "backend":
                state = r.choice([store.BackendState(False, "exhausted: dry"),
                                  store.BackendState(), store.BackendState(False, "outage")])
                ops.append(lambda tx, s=state, name=r.choice(["find_books", "find_paused"]):
                           tx.backend_state_set(name, s))
            elif kind == "replace" and r.random() < 0.4:
                keep = r.sample(ids, max(0, len(ids) - 3))
                rows = [manifest[s] if r.random() < 0.7 else self.row(s) for s in keep]
                rows.append(self.row(self.new_id()))
                ops.append(lambda tx, rows=rows: tx.replace_manifest(rows, reason="replace: test"))
                manifest = {x["id"]: x for x in rows}
        return ops

    def new_id(self):
        self.fresh += 1
        return f"new-{self.fresh}"


def apply_both(a, w, b, name, ops, run_id="rnd1"):
    body = lambda tx: [op(tx) for op in ops]  # noqa: E731
    want = write(b, name, body)
    got = stage(a, w, name, body, run_id=run_id)
    assert got.results == want, name     # same return values, batch by batch
    return got


@pytest.mark.parametrize("literal_runs,chunk", [(store_staging.LITERAL_RUNS, None), (0, None),
                                               (store_staging.LITERAL_RUNS, 2)],
                         ids=["literal-visibility", "subquery-visibility", "two-row-windows"])
def test_overlay_matches_direct_application_everywhere(pair, monkeypatch, literal_runs, chunk):
    monkeypatch.setattr(store_staging, "LITERAL_RUNS", literal_runs)
    monkeypatch.setattr(store_staging, "SCAN_CHUNK", chunk)   # several scan windows per page
    if chunk:
        monkeypatch.setattr(store_staging, "COPY_THRESHOLD", 1)  # every mutation COPYs
    a, b = pair
    work = Workload(20260925)
    with b.read() as v:
        history = [snapshot(v)]
    with a.writer(round_id="rnd1") as w:
        open_run(a, w)
        with a.read_staged("rnd1", writer=w) as v:
            assert_same(snapshot(v), history[0])
        for i in range(10):
            apply_both(a, w, b, f"b{i:02d}", work.batch(b))
            with b.read() as vb:
                history.append(snapshot(vb))
            with a.read_staged("rnd1", writer=w) as va:
                assert_same(snapshot(va), history[-1])
        # every earlier staging sequence still reads exactly as it was (pinned views)
        for k, want in enumerate(history):
            with a.read_staged("rnd1", seq=k, writer=w) as va:
                assert va.version() == store_staging.stage_version("rnd1", k)
                assert_same(snapshot(va), want)
        with a.read() as va:                      # ordinary views: still the seed
            assert_same(snapshot(va), history[0])
        assert finish(a, w) == 0
        final = history[-1]
        with a.read() as va:
            assert va.generation == 0
            assert_same(snapshot(va), final)
        # folding in small steps: every intermediate projection reads the same generation
        while not (progress := a.fold(w, limit=4)).done:
            assert progress.generation == 0 and progress.rows == 4
            with a.read() as va:
                assert_same(snapshot(va), final)
        with a.read() as va:
            assert va._visibility is None         # the projection alone is generation 0 now
            assert_same(snapshot(va), final)
        # a second run on top of generation 0 (visibility: the projection + that run)
        open_run(a, w, "rnd2")
        for i in range(6):
            apply_both(a, w, b, f"c{i:02d}", work.batch(b), run_id="rnd2")
            with b.read() as vb:
                want = snapshot(vb)
            with a.read_staged("rnd2", writer=w) as va:
                assert_same(snapshot(va), want)
        assert finish(a, w, "rnd2") == 1
        with a.read() as va:
            assert_same(snapshot(va), want)
        with a.read_generation(0) as va:          # not folded past: still reconstructible
            assert_same(snapshot(va), final)
        a.fold(w, limit=3)                        # generation 1 partially folded
        with a.read() as va:
            assert_same(snapshot(va), want)
        with pytest.raises(StoreError, match="already folded"):
            with a.read_generation(0):
                pass


def test_many_unfolded_generations_use_the_subquery_visibility(pair, monkeypatch):
    """More promoted-but-unfolded runs than LITERAL_RUNS: visibility switches spelling."""
    monkeypatch.setattr(store_staging, "LITERAL_RUNS", 2)
    a, b = pair
    work = Workload(7)
    with a.writer() as w:
        for g in range(4):
            open_run(a, w, f"r{g}")
            apply_both(a, w, b, f"g{g}", work.batch(b), run_id=f"r{g}")
            assert finish(a, w, f"r{g}") == g
        with a.read() as va, b.read() as vb:
            assert len(va._visibility.runs) == 4 and not va._visibility._literal()
            assert_same(snapshot(va), snapshot(vb))
        assert store_staging.fold_all(a, w, limit=5) == 4
        with a.read() as va, b.read() as vb:
            assert va._visibility is None
            assert_same(snapshot(va), snapshot(vb))


# --- replacement semantics, net changes, numbers ------------------------------------------------------

def revisions(st, run_id="rnd1"):
    return q(st, "SELECT batch_seq, tbl, key, op, row_text, before_sha256, reason FROM revisions "
                 "WHERE run_id = %s ORDER BY batch_seq, tbl, key", [run_id])


def test_replace_manifest_tombstones_every_omitted_row_with_its_reason(pair):
    a, b = pair
    with a.read() as v:
        before = {r["id"]: r for r in paged(v, Table.MANIFEST, limit=1000)}
    keep = [before["oer-1"], {**before["oer-2"], "status": "changed"}, mrow("new-1")]
    with a.writer() as w:
        open_run(a, w)
        apply_both(a, w, b, "replace", [lambda tx: tx.replace_manifest(keep, reason="rebuild")])
        tombs = [r for r in revisions(a) if r[3] == "tombstone"]
        assert sorted(r[2] for r in tombs) == sorted(set(before) - {"oer-1", "oer-2"})
        for _, tbl, key, _, text, before_sha, reason in tombs:
            assert (tbl, text, reason) == ("manifest", None, "rebuild")
            assert before_sha == hashlib.sha256(store.canonical_row(before[key]).encode()).hexdigest()
        puts = {r[2]: r for r in revisions(a) if r[3] == "put"}
        assert set(puts) == {"oer-2", "new-1"}   # oer-1 was kept unchanged: no revision
        with a.read_staged("rnd1", writer=w) as va, b.read() as vb:
            assert_same(snapshot(va), snapshot(vb))


def test_a_batch_records_only_its_net_changes(pair):
    a, b = pair
    ops = [lambda tx: tx.insert_entries([entry("tmp-1")]),
           lambda tx: tx.delete_entries(["tmp-1"], reason="oops"),     # created and gone
           lambda tx: tx.update_manifest_fields({"oer-1": {"status": "x"}}),
           lambda tx: tx.update_manifest_fields({"oer-1": {"status": "ok"}}),  # reverted
           lambda tx: tx.delete_manifest(["oer-2"], reason="dup"),
           lambda tx: tx.upsert_manifest([mrow("oer-2", sha256="s2", text_chars=2.5)]),  # same row
           lambda tx: tx.delete_manifest(["oer-3"], reason="first"),
           lambda tx: tx.upsert_manifest([mrow("oer-3", status="back")]),     # a new row
           lambda tx: tx.rotation_set("find_books", {"next": 9}),
           lambda tx: tx.rotation_set("find_books", {"flag": "--offset", "next": 5, "step": 5})]
    with a.writer() as w:
        open_run(a, w)
        got = apply_both(a, w, b, "net", ops)
        assert got.results == [1, 1, 1, 1, 1, 1, 1, 1, None, None]   # every call still counted
        assert [(r[1], r[2], r[3]) for r in revisions(a)] == [("manifest", "oer-3", "put")]
        with a.read_staged("rnd1", writer=w) as va, b.read() as vb:
            assert_same(snapshot(va), snapshot(vb))
        counts = json.loads(q(a, "SELECT counts_text FROM batches")[0][0])["counts"]
        assert counts["manifest"] == {"delete": 2, "update": 2, "upsert": 2}


def test_json_numbers_survive_staging_promotion_and_folding_byte_for_byte(pg):
    rows = [mrow(f"n-{i}", text_chars=v, quality={"v": v, "l": [v, {"x": v}]})
            for i, v in enumerate([1e20, -0.0, 1.0, 1, 7e-07, 0.1, 12345678901234567890, True])]
    texts = {r["id"]: store.canonical_row(r) for r in rows}
    with pg.writer() as w:
        open_run(pg, w)
        stage(pg, w, "nums", lambda tx: tx.upsert_manifest(rows))
        for when in ("staged", "promoted", "folded"):
            if when == "promoted":
                finish(pg, w)
            if when == "folded":
                store_staging.fold_all(pg, w)
            view = pg.read_staged("rnd1", writer=w) if when == "staged" else pg.read()
            with view as v:
                got = {r["id"]: store.canonical_row(r)
                       for r in paged(v, Table.MANIFEST, where=Prefix("id", "n-"))}
                assert got == texts, when
                assert [r["id"] for r in paged(v, Table.MANIFEST, where=Eq("text_chars", 1e20))] \
                    == ["n-0"]
                total = next(iter(v.aggregate_manifest(group_by=(), sums=("text_chars",),
                                                       where=Prefix("id", "n-"))))
                assert total["sum_text_chars"] == store.exact_sum(r["text_chars"] for r in rows)


# --- retries, stale sequences, failures -------------------------------------------------------------

def test_exact_retry_answers_from_the_receipt_and_writes_nothing(pg):
    body = lambda tx: (tx.blocklist_add(["https://e.org/x"]),  # noqa: E731
                       tx.upsert_manifest([mrow("a")]))
    with pg.writer() as w:
        open_run(pg, w)
        first = stage(pg, w, "b1", body)
        assert (first.results, first.seq, first.retried) == ([1, 1], 1, False)
        stage(pg, w, "b2", lambda tx: tx.upsert_manifest([mrow("b")]))
        before = revisions(pg)
        again = pg.stage_batch(w, "rnd1", "fetch", "b1", recorded(body),
                               expected_version=store.Version("stale"))   # no version check
        assert (again.results, again.seq, again.retried) == ([1, 1], 1, True)
        assert again.version == store_staging.stage_version("rnd1", 1)
        assert revisions(pg) == before
        assert q(pg, "SELECT staged_seq FROM runs")[0][0] == 2


def test_conflicting_retry_is_refused_and_changes_nothing(pg):
    with pg.writer() as w:
        open_run(pg, w)
        stage(pg, w, "b1", lambda tx: tx.upsert_manifest([mrow("a")]))
        before = revisions(pg)
        with pytest.raises(StoreError, match="conflicting retry"):
            pg.stage_batch(w, "rnd1", "fetch", "b1",
                           recorded(lambda tx: tx.upsert_manifest([mrow("a", status="other")])),
                           expected_version=version_of(pg, w))
        assert revisions(pg) == before
        assert q(pg, "SELECT count(*), max(staged_seq) FROM batches, runs")[0] == (1, 1)


def test_stale_sequences_are_refused(pg):
    with pg.writer() as w:
        open_run(pg, w)
        at0 = version_of(pg, w)
        pg.stage_batch(w, "rnd1", "fetch", "b1", recorded(lambda tx: tx.upsert_manifest(
            [mrow("a")])), expected_version=at0)
        with pytest.raises(VersionConflict, match="sequence 1"):   # computed before b1 applied
            pg.stage_batch(w, "rnd1", "prune", "b1", recorded(lambda tx: tx.delete_manifest(
                ["a"], reason="x")), expected_version=at0)
        # a persisted request: computed at 1, applied later exactly at 1
        at1 = version_of(pg, w)
        req = recorded(lambda tx: tx.upsert_manifest([mrow("p")]))
        pg.stage_batch(w, "rnd1", "discover", "merge", req, expected_version=at1,
                       persist_only=True)
        with pytest.raises(VersionConflict):
            pg.stage_batch(w, "rnd1", "fetch", "b2", req, expected_version=at0)
        # the run cannot freeze over it
        with pytest.raises(StoreError, match="requested batch"):
            pg.freeze(w, "rnd1", required_gates=["t"])
        got = pg.stage_batch(w, "rnd1", "discover", "merge", req, expected_version=at1)
        assert got.seq == 2
        # views pinned beyond the staged sequence are refused
        with pytest.raises(StaleView, match="staged sequence 2"):
            with pg.read_staged("rnd1", seq=3, writer=w):
                pass


def test_a_persisted_request_goes_stale_when_another_batch_applies_first(pg):
    with pg.writer() as w:
        open_run(pg, w)
        at0 = version_of(pg, w)
        req = recorded(lambda tx: tx.upsert_manifest([mrow("p")]))
        pg.stage_batch(w, "rnd1", "discover", "merge", req, expected_version=at0,
                       persist_only=True)
        # another batch computed at 0 applies first: the persisted one is now stale
        pg.stage_batch(w, "rnd1", "fetch", "b1", recorded(lambda tx: tx.upsert_manifest(
            [mrow("f")])), expected_version=at0)
        with pytest.raises(VersionConflict, match="computed at sequence 0"):
            pg.stage_batch(w, "rnd1", "discover", "merge", req, expected_version=at0)
        expect_refused(pg, "UPDATE batches SET status = 'applied', seq = 2, applied_at = now() "
                           "WHERE step = 'discover'")          # seq must be basis + 1
        expect_refused(pg, "UPDATE batches SET status = 'applied', seq = 1, applied_at = now() "
                           "WHERE step = 'discover'")          # the run is no longer at 0
        store_staging.abandon_batch(pg, w, "rnd1", "discover", "merge")
        assert pg.freeze(w, "rnd1", required_gates=["t"]).seq == 1


def test_a_failed_batch_leaves_no_receipt_revision_or_sequence(pg):
    with pg.writer() as w:
        open_run(pg, w)
        v0 = version_of(pg, w)
        bad = recorded(lambda tx: (tx.upsert_manifest([mrow("a")]),
                                   tx.update_manifest_fields({"missing": {"status": "x"}})))
        with pytest.raises(StoreError, match="unknown id"):
            pg.stage_batch(w, "rnd1", "fetch", "b1", bad, expected_version=v0)
        assert q(pg, "SELECT count(*) FROM batches")[0][0] == 0
        assert revisions(pg) == []
        assert q(pg, "SELECT staged_seq, batches_open FROM runs")[0] == (0, 0)
        good = pg.stage_batch(w, "rnd1", "fetch", "b1",
                              recorded(lambda tx: tx.upsert_manifest([mrow("a")])),
                              expected_version=v0)
        assert good.seq == 1


def test_requests_are_validated_before_anything_is_written(pg):
    with pg.writer() as w:
        open_run(pg, w)
        v0 = version_of(pg, w)
        for bad, msg in (([{"call": "drop_everything"}], "not a store mutation"),
                         ([{"call": "blocklist_add", "args": [[float("nan")]]}], "NaN"),
                         ([{"call": "blocklist_add", "args": ["x\x00"]}], "NUL")):
            with pytest.raises(StoreError, match=msg):
                pg.stage_batch(w, "rnd1", "fetch", "b1", bad, expected_version=v0)
        with pytest.raises(StoreError, match="plain names"):
            pg.stage_batch(w, "rnd1", "fe:tch", "b1", [], expected_version=v0)
        assert q(pg, "SELECT count(*) FROM batches")[0][0] == 0


# --- visibility and atomic promotion ---------------------------------------------------------------

def test_staging_is_invisible_until_promotion_which_is_atomic(pair):
    a, b = pair
    work = Workload(99)
    with a.read() as v:
        seed_snap = snapshot(v)
    stop = threading.Event()
    seen, errors = set(), []

    def poll():
        try:
            while not stop.is_set():
                with a.read() as v:
                    seen.add((len(paged(v, Table.MANIFEST, limit=1000)),
                              len(paged(v, Table.ENTRIES, limit=1000)), v.generation))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    with a.writer() as w:
        open_run(a, w)
        for i in range(6):
            apply_both(a, w, b, f"b{i}", work.batch(b))
        with b.read() as vb:
            final = snapshot(vb)
        with a.read() as early:
            t = threading.Thread(target=poll)
            t.start()
            try:
                assert finish(a, w) == 0
            finally:
                stop.set()
                t.join()
            assert_same(snapshot(early), seed_snap)        # an open view keeps its snapshot
        with a.read() as late:
            assert_same(snapshot(late), final)
    assert not errors and seen
    before = (len(seed_snap["manifest"]), len(seed_snap["entries"]), None)
    after = (len(final["manifest"]), len(final["entries"]), 0)
    assert before != after and seen <= {before, after}   # never a partial promotion


def test_promotion_writes_a_constant_number_of_rows(pg):
    rows = [mrow(f"bulk-{i:05d}") for i in range(3000)]
    with pg.writer() as w:
        open_run(pg, w)
        for i in range(3):
            stage(pg, w, f"b{i}", lambda tx, i=i: tx.upsert_manifest(rows[i * 1000:(i + 1) * 1000]))
        frozen = pg.freeze(w, "rnd1", required_gates=["tests"])
        pg.record_gate(w, frozen, "tests", passed=True)
        tables = ("revisions", "batches", "manifest", "entries", "generations", "outbox", "runs")
        count = lambda: {t: q(pg, f"SELECT count(*) FROM {t}")[0][0] for t in tables}  # noqa: E731
        stamp = lambda: q(pg, "SELECT xmin::text, rev_id FROM revisions ORDER BY rev_id")  # noqa: E731
        before, before_stamp = count(), stamp()
        assert pg.promote(w, frozen) == 0
        after = count()
        assert stamp() == before_stamp                      # no revision rewritten
        assert {t: after[t] - before[t] for t in tables} == {
            "revisions": 0, "batches": 0, "manifest": 0, "entries": 0, "generations": 1,
            "outbox": 1, "runs": 0}
        payload = json.loads(q(pg, "SELECT payload_text FROM outbox")[0][0])
        assert payload["counts"] == {"batches": 3, "ops": {"manifest": {"upsert": 3000}}}
        assert (payload["frozen_seq"], payload["frozen_digest"]) == (frozen.seq, frozen.digest)


# --- freezing, gates, ownership ------------------------------------------------------------------------

def test_freeze_gates_and_promotion_rules(pg):
    with pg.writer() as w:
        open_run(pg, w)
        stage(pg, w, "b1", lambda tx: tx.upsert_manifest([mrow("a")]))
        v1 = version_of(pg, w)
        with pytest.raises(StaleView):
            pg.record_gate(w, store_staging.Frozen("rnd1", 1, "x" * 64), "tests", passed=True)
        frozen = pg.freeze(w, "rnd1", required_gates=["tests", "lint", "tests"])
        assert frozen == pg.freeze(w, "rnd1", required_gates=["lint", "tests"])  # exact retry
        with pytest.raises(StoreError, match="required gates"):
            pg.freeze(w, "rnd1", required_gates=["tests"])
        assert frozen.seq == 1
        chain = q(pg, "SELECT chain_digest FROM batches WHERE seq = 1")[0][0]
        assert frozen.digest == chain
        with pytest.raises(StaleView, match="frozen"):             # staging is over
            pg.stage_batch(w, "rnd1", "fetch", "b2", [], expected_version=v1)
        with pytest.raises(StoreError, match="not passed"):        # no receipts yet
            pg.promote(w, frozen)
        pg.record_gate(w, frozen, "tests", passed=True)
        pg.record_gate(w, frozen, "tests", passed=True)            # exact retry
        with pytest.raises(StoreError, match="differently"):
            pg.record_gate(w, frozen, "tests", passed=False)
        with pytest.raises(StoreError, match="not passed"):        # lint missing
            pg.promote(w, frozen)
        with pytest.raises(StoreError, match="validated sequence"):
            pg.record_gate(w, store_staging.Frozen("rnd1", 1, "e" * 64), "lint", passed=True)
        with pytest.raises(StoreError, match="not at the state"):
            pg.promote(w, store_staging.Frozen("rnd1", 1, "e" * 64))
        pg.record_gate(w, frozen, "lint", passed=True, detail={"elapsed": 1.5})
        assert pg.promote(w, frozen) == 0
        assert pg.promote(w, frozen) == 0                          # exact retry
        with pytest.raises(StoreError, match="another frozen state"):
            pg.promote(w, store_staging.Frozen("rnd1", 1, "e" * 64))


def test_a_failed_gate_blocks_promotion(pg):
    with pg.writer() as w:
        open_run(pg, w)
        frozen = pg.freeze(w, "rnd1", required_gates=["tests"])
        pg.record_gate(w, frozen, "extra", passed=False)           # even a non-required gate
        pg.record_gate(w, frozen, "tests", passed=True)
        with pytest.raises(StoreError, match="not passed"):
            pg.promote(w, frozen)
        pg.abort_run(w, "rnd1", reason="gate failed")
        with pg.read() as v:
            assert v.generation is None


def test_every_owner_only_operation_refuses_a_later_writer_epoch(pg):
    """Codex review (step 2, P2 4): once the owner's session ended, a later writer epoch can do
    nothing owner-only with the run — stage, request (contract level), read its staging as its
    writer, read receipts, abandon, freeze, gate, promote, re-open — even exact retries; only
    the explicit cross-owner recovery operations (abort_run, purge_run) work. The run's token
    still authorizes its readers."""
    with pg.writer() as w:
        run = open_run(pg, w)
        stage(pg, w, "b1", lambda tx: tx.upsert_manifest([mrow("a")]))
        v1 = version_of(pg, w)
        pg.stage_batch(w, "rnd1", "discover", "merge", recorded(lambda tx: tx.upsert_manifest(
            [mrow("p")])), expected_version=v1, persist_only=True)
    owner_only = "belongs to writer epoch"
    with pg.writer() as w2:                                        # a new writer epoch
        calls = [
            lambda: pg.stage_batch(w2, "rnd1", "fetch", "b2", [], expected_version=v1),
            lambda: pg.stage_batch(w2, "rnd1", "fetch", "b1", recorded(   # an exact retry
                lambda tx: tx.upsert_manifest([mrow("a")])), expected_version=v1),
            lambda: pg.batch_receipt(w2, "rnd1", "fetch", "b1"),
            lambda: store_staging.abandon_batch(pg, w2, "rnd1", "discover", "merge"),
            lambda: pg.freeze(w2, "rnd1", required_gates=["t"]),
            lambda: pg.record_gate(w2, store_staging.Frozen("rnd1", 1, "0" * 64), "t",
                                   passed=True),
            lambda: pg.promote(w2, store_staging.Frozen("rnd1", 1, "0" * 64)),
            lambda: open_run(pg, w2),
            lambda: pg.read_staged("rnd1", writer=w2).__enter__(),
        ]
        for call in calls:
            with pytest.raises(store.WriterError, match=owner_only):
                call()
        with pg.contracts(w2) as c, pytest.raises(store.WriterError, match=owner_only):
            c.request_batch("rnd1", "fetch", "b9", [{"call": "x"}])
        with pg.read_staged("rnd1", token=run.token) as v:        # its readers still read
            assert v.known(ids=["a"]).ids == {"a"}
        assert q(pg, "SELECT count(*), max(status) FROM batches WHERE step = 'discover'")[0] \
            == (1, "requested")                                   # nothing was abandoned
        pg.abort_run(w2, "rnd1", reason="owner gone")              # the explicit cross-owner op
        pg.abort_run(w2, "rnd1", reason="again")                   # idempotent
        with pytest.raises(StaleView, match="aborted"):
            with pg.read_staged("rnd1", token=run.token):
                pass
        while store_staging.purge_run(pg, w2, "rnd1", limit=1):
            pass
        assert q(pg, "SELECT (SELECT count(*) FROM revisions), (SELECT count(*) FROM batches)"
                 )[0] == (0, 0)


def test_a_run_goes_stale_when_another_generation_is_promoted(pg):
    with pg.writer() as w:
        open_run(pg, w, "late")
        stage(pg, w, "b1", lambda tx: tx.upsert_manifest([mrow("late")]), run_id="late")
        open_run(pg, w, "first")
        stage(pg, w, "b1", lambda tx: tx.upsert_manifest([mrow("first")]), run_id="first")
        assert finish(pg, w, "first") == 0
        for call in (lambda: version_of(pg, w, "late"),
                     lambda: stage(pg, w, "b2", lambda tx: None, run_id="late"),
                     lambda: pg.freeze(w, "late", required_gates=["t"])):
            with pytest.raises(StaleView, match="current generation is 0"):
                call()
        with pg.read() as v:
            assert v.known(ids=["late", "first"]).ids == {"first"}


def test_legacy_writes_are_refused_once_a_run_stages(pg, tmp_path):
    import pg_shadow
    write(pg, "before", lambda tx: tx.blocklist_add(["https://e.org/ok"]))  # no run yet: fine
    with pg.writer() as w:
        open_run(pg, w)
    with pytest.raises(StoreError, match="direct store transactions are refused"):
        write(pg, "after", lambda tx: tx.blocklist_add(["https://e.org/no"]))
    with pg.writer() as w, pytest.raises(SystemExit, match="shadow replay is refused"):
        with pg._writer_conn(w).transaction():
            pg_shadow._require_shadow(pg._writer_conn(w))


# --- the database contracts, against direct SQL ------------------------------------------------------

def test_sealed_batches_and_their_revisions_are_immutable(pg):
    with pg.writer() as w:
        open_run(pg, w)
        stage(pg, w, "b1", lambda tx: tx.upsert_manifest([mrow("a")]))
    for stmt in ("UPDATE revisions SET row_text = '{}'", "DELETE FROM revisions",
                 "UPDATE batches SET counts_text = '{}'", "UPDATE batches SET sealed = false",
                 "UPDATE batches SET chain_digest = repeat('0', 64)",
                 "UPDATE runs SET staged_seq = 5", "UPDATE runs SET batches_open = 3",
                 "UPDATE runs SET gate_receipts = 1", "DELETE FROM batches",
                 "INSERT INTO revisions (run_id, batch_seq, tbl, key, op, row_text, row_sha256) "
                 "VALUES ('rnd1', 1, 'manifest', 'x', 'put', '{}', encode(sha256('{}'), 'hex'))",
                 "UPDATE run_access SET token_sha256 = repeat('0', 64)"):
        expect_refused(pg, stmt)


def test_an_applied_batch_must_be_sealed_before_commit(pg):
    import psycopg
    with pg.writer() as w:
        open_run(pg, w)
        c = pg._writer_conn(w)
        text = "[]"
        digest = hashlib.sha256(text.encode()).hexdigest()
        with pytest.raises(psycopg.IntegrityError, match="not sealed"):
            with c.transaction():
                c.execute("INSERT INTO batches (run_id, step, batch, request_digest, request_text, "
                          "basis_seq) VALUES ('rnd1', 's', 'b', %s, %s, 0)", [digest, text])
                c.execute("UPDATE batches SET status = 'applied', seq = 1, applied_at = now()")
        with pytest.raises(psycopg.IntegrityError, match="digest does not match"):
            c.execute("INSERT INTO batches (run_id, step, batch, request_digest, request_text, "
                      "basis_seq) VALUES ('rnd1', 's', 'b', repeat('a', 64), '[]', 0)")
        with pytest.raises(psycopg.IntegrityError, match="current staging sequence"):
            c.execute("INSERT INTO batches (run_id, step, batch, request_digest, request_text, "
                      "basis_seq) VALUES ('rnd1', 's', 'b', %s, %s, 3)", [digest, text])
        assert q(pg, "SELECT staged_seq, batches_open FROM runs")[0] == (0, 0)


def test_freeze_and_gate_contracts_hold_against_direct_sql(pg):
    with pg.writer() as w:
        open_run(pg, w)
        stage(pg, w, "b1", lambda tx: tx.upsert_manifest([mrow("a")]))
    chain = q(pg, "SELECT chain_digest FROM batches")[0][0]
    for gates, digest in (('["t"]', "0" * 64), ('["b","a"]', chain), ('["a","a"]', chain),
                          ('[]', chain), ('["a b"]', chain), ('{"a": 1}', chain), (None, chain)):
        expect_refused(pg, "UPDATE runs SET status = 'frozen', frozen_seq = 1, frozen_digest = %s, "
                           "required_gates = %s", [digest, gates])
    q(pg, "UPDATE runs SET status = 'frozen', frozen_seq = 1, frozen_digest = %s, "
          "required_gates = '[\"t\"]' RETURNING 1", [chain])
    expect_refused(pg, "INSERT INTO gate_receipts (run_id, gate, frozen_seq, frozen_digest, "
                       "verdict) VALUES ('rnd1', 't', 1, repeat('0', 64), 'passed')")
    expect_refused(pg, "UPDATE runs SET required_gates = '[\"u\"]'")
    expect_refused(pg, "INSERT INTO generations (generation, run_id, producer_commit, "
                       "config_digest, extractor_version, cleaning_ruleset, frozen_seq, "
                       "frozen_digest, counts_text) SELECT 0, run_id, producer_commit, "
                       "config_digest, extractor_version, cleaning_ruleset, frozen_seq, "
                       "frozen_digest, '{}' FROM runs")                     # no gate receipt
    q(pg, "INSERT INTO gate_receipts (run_id, gate, frozen_seq, frozen_digest, verdict) VALUES "
          "('rnd1', 't', 1, %s, 'passed') RETURNING 1", [chain])
    for stmt in ("UPDATE gate_receipts SET verdict = 'failed'", "DELETE FROM gate_receipts"):
        expect_refused(pg, stmt)


def test_projection_and_pins_contracts(pg):
    with pg.writer() as w:
        for g in range(3):
            open_run(pg, w, f"r{g}")
            stage(pg, w, "b", lambda tx, g=g: tx.upsert_manifest([mrow(f"g{g}")]), run_id=f"r{g}")
            finish(pg, w, f"r{g}")
        store_staging.pin_generation(pg, w, 1, holder="test", reason="keep")
        assert pg.fold(w).done                                      # 0
        assert pg.fold(w).done                                      # 1: the projection may BE it
        assert pg.fold(w).blocked                                   # 2 would pass the pin on 1
    assert q(pg, "SELECT generation FROM projection_state")[0][0] == 1
    expect_refused(pg, "UPDATE projection_state SET generation = 2")   # past the pin
    expect_refused(pg, "UPDATE projection_state SET generation = 3")   # skipping
    expect_refused(pg, "UPDATE projection_state SET generation = NULL")
    expect_refused(pg, "UPDATE projection_state SET fold_generation = 0")
    expect_refused(pg, "UPDATE projection_state SET pins = 0")
    expect_refused(pg, "DELETE FROM projection_state")
    expect_refused(pg, "INSERT INTO generation_retention (generation, holder, reason) VALUES "
                       "(0, 'late', 'too late')")                     # already folded past 0
    with pg.writer() as w:
        with pg.read_generation(1) as v:
            assert v.known(ids=["g0", "g1", "g2"]).ids == {"g0", "g1"}
        store_staging.unpin_generation(pg, w, 1, holder="test")
        assert store_staging.fold_all(pg, w) == 1
    assert q(pg, "SELECT generation FROM projection_state")[0][0] == 2
    assert dict(q(pg, "SELECT consumer, watermark FROM outbox_consumers"))["projection"] == 3


# --- concurrency: every cross-row rule conflicts on one row under any isolation level -------------

def _blocked_then(fn, release):
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


def _two(pg, level):
    import psycopg
    first, second = pg._connect(), pg._connect()
    for c in (first, second):
        c.commit()
    second.isolation_level = getattr(psycopg.IsolationLevel, level)
    return first, second


LEVELS = ["READ_COMMITTED", "REPEATABLE_READ", "SERIALIZABLE"]


def _staged_run(pg):
    with pg.writer() as w:
        open_run(pg, w)
        stage(pg, w, "b1", lambda tx: tx.upsert_manifest([mrow("a")]))
    return q(pg, "SELECT chain_digest FROM batches")[0][0]


def _req(conn, batch):
    text = "[]"
    conn.execute("INSERT INTO batches (run_id, step, batch, request_digest, request_text, "
                 "basis_seq) VALUES ('rnd1', 's', %s, %s, %s, 1)",
                 [batch, hashlib.sha256(text.encode()).hexdigest(), text])


FREEZE = ("UPDATE runs SET status = 'frozen', frozen_seq = staged_seq, frozen_digest = %s, "
          "required_gates = '[\"t\"]'")


@pytest.mark.parametrize("level", LEVELS)
def test_a_batch_request_and_a_freeze_never_both_commit(pg, level):
    import psycopg
    chain = _staged_run(pg)
    requester, freezer = _two(pg, level)
    try:
        freezer.execute("SELECT 1 FROM runs")                     # the freezer's snapshot
        _req(requester, "late")                                  # uncommitted: holds the run row
        exc = _blocked_then(lambda: (freezer.execute(FREEZE, [chain]), freezer.commit()),
                            requester.commit)
        assert isinstance(exc, (psycopg.IntegrityError, psycopg.errors.SerializationFailure)), exc
        freezer.rollback()
    finally:
        requester.close()
        freezer.close()
    assert q(pg, "SELECT status, batches_open FROM runs")[0] == ("open", 1)


@pytest.mark.parametrize("level", LEVELS)
def test_a_freeze_and_a_batch_request_never_both_commit(pg, level):
    import psycopg
    chain = _staged_run(pg)
    freezer, requester = _two(pg, level)
    try:
        requester.execute("SELECT 1 FROM runs")
        freezer.execute(FREEZE, [chain])
        exc = _blocked_then(lambda: (_req(requester, "late"), requester.commit()), freezer.commit)
        assert isinstance(exc, (psycopg.IntegrityError, psycopg.errors.SerializationFailure)), exc
        requester.rollback()
    finally:
        requester.close()
        freezer.close()
    assert q(pg, "SELECT status, batches_open FROM runs")[0] == ("frozen", 0)
    assert q(pg, "SELECT count(*) FROM batches")[0][0] == 1


def _frozen_with_pass(pg):
    chain = _staged_run(pg)
    q(pg, FREEZE + " RETURNING 1", [chain])
    q(pg, "INSERT INTO gate_receipts (run_id, gate, frozen_seq, frozen_digest, verdict) VALUES "
          "('rnd1', 't', 1, %s, 'passed') RETURNING 1", [chain])
    return chain


PROMOTE = ["INSERT INTO generations (generation, run_id, producer_commit, config_digest, "
           "extractor_version, cleaning_ruleset, frozen_seq, frozen_digest, counts_text) SELECT "
           "0, run_id, producer_commit, config_digest, extractor_version, cleaning_ruleset, "
           "frozen_seq, frozen_digest, '{}' FROM runs",
           "UPDATE runs SET status = 'promoted', promoted_generation = 0, ended_at = now()",
           "UPDATE dataset SET current_generation = 0",
           "INSERT INTO outbox (seq, generation, payload_text) VALUES (1, 0, '{}')"]


@pytest.mark.parametrize("level", LEVELS)
def test_a_failed_gate_recorded_during_promotion_blocks_it(pg, level):
    import psycopg
    chain = _frozen_with_pass(pg)
    recorder, promoter = _two(pg, level)
    try:
        promoter.execute("SELECT 1 FROM runs")
        recorder.execute("INSERT INTO gate_receipts (run_id, gate, frozen_seq, frozen_digest, "
                         "verdict) VALUES ('rnd1', 'x', 1, %s, 'failed')", [chain])

        def promote():
            for stmt in PROMOTE:
                promoter.execute(stmt)
            promoter.commit()
        exc = _blocked_then(promote, recorder.commit)
        assert isinstance(exc, (psycopg.IntegrityError, psycopg.errors.SerializationFailure)), exc
        promoter.rollback()
    finally:
        recorder.close()
        promoter.close()
    assert q(pg, "SELECT status FROM runs")[0][0] == "frozen"
    assert q(pg, "SELECT count(*) FROM generations")[0][0] == 0


@pytest.mark.parametrize("level", LEVELS)
def test_a_gate_receipt_after_promotion_is_refused(pg, level):
    import psycopg
    chain = _frozen_with_pass(pg)
    promoter, recorder = _two(pg, level)
    try:
        recorder.execute("SELECT 1 FROM runs")
        for stmt in PROMOTE:
            promoter.execute(stmt)
        exc = _blocked_then(lambda: (recorder.execute(
            "INSERT INTO gate_receipts (run_id, gate, frozen_seq, frozen_digest, verdict) "
            "VALUES ('rnd1', 'x', 1, %s, 'failed')", [chain]), recorder.commit()), promoter.commit)
        assert isinstance(exc, (psycopg.IntegrityError, psycopg.errors.SerializationFailure)), exc
        recorder.rollback()
    finally:
        recorder.close()
        promoter.close()
    assert q(pg, "SELECT count(*) FROM gate_receipts")[0][0] == 1


def _two_generations_folded_to_0(pg):
    with pg.writer() as w:
        for g in range(2):
            open_run(pg, w, f"r{g}")
            finish(pg, w, f"r{g}")
        assert pg.fold(w).done
    assert q(pg, "SELECT generation FROM projection_state")[0][0] == 0


@pytest.mark.parametrize("level", LEVELS)
def test_a_pin_taken_during_a_fold_blocks_it(pg, level):
    import psycopg
    _two_generations_folded_to_0(pg)
    pinner, folder = _two(pg, level)
    try:
        folder.execute("SELECT 1 FROM projection_state")
        pinner.execute("INSERT INTO generation_retention (generation, holder, reason) VALUES "
                       "(0, 'h', 'keep 0')")
        exc = _blocked_then(lambda: (folder.execute("UPDATE projection_state SET generation = 1"),
                                     folder.commit()), pinner.commit)
        assert isinstance(exc, (psycopg.IntegrityError, psycopg.errors.SerializationFailure)), exc
        folder.rollback()
    finally:
        pinner.close()
        folder.close()
    assert q(pg, "SELECT generation FROM projection_state")[0][0] == 0


@pytest.mark.parametrize("level", LEVELS)
def test_a_pin_on_a_generation_folded_meanwhile_is_refused(pg, level):
    import psycopg
    _two_generations_folded_to_0(pg)
    folder, pinner = _two(pg, level)
    try:
        pinner.execute("SELECT 1 FROM projection_state")
        folder.execute("UPDATE projection_state SET generation = 1")
        exc = _blocked_then(lambda: (pinner.execute(
            "INSERT INTO generation_retention (generation, holder, reason) VALUES "
            "(0, 'h', 'keep 0')"), pinner.commit()), folder.commit)
        assert isinstance(exc, (psycopg.IntegrityError, psycopg.errors.SerializationFailure)), exc
        pinner.rollback()
    finally:
        pinner.close()
        folder.close()
    assert q(pg, "SELECT count(*) FROM generation_retention")[0][0] == 0


# --- historical reconstruction ------------------------------------------------------------------------

def export_bytes(st, view, directory):
    report = st.export(directory, view=view)
    return {name: (directory / name).read_bytes() for name in report.files}


def test_promoted_generations_stay_reconstructible_and_immutable(pair, tmp_path):
    a, b = pair
    work = Workload(5)
    exports = {}
    with a.writer() as w:
        for g in range(3):
            open_run(a, w, f"r{g}")
            for i in range(3):
                apply_both(a, w, b, f"g{g}b{i}", work.batch(b), run_id=f"r{g}")
            assert finish(a, w, f"r{g}") == g
            store_staging.pin_generation(a, w, g, holder="history", reason="test")
            with a.read() as v:
                exports[g] = export_bytes(a, v, tmp_path / f"at{g}")
        # later activity: an aborted (and purged) run, a frozen-but-unpromoted run, folds
        open_run(a, w, "aborted")
        stage(a, w, "x", lambda tx: tx.delete_manifest(["oer-1", "oer-2"], reason="x"),
              run_id="aborted")
        a.abort_run(w, "aborted", reason="test")
        while store_staging.purge_run(a, w, "aborted"):
            pass
        open_run(a, w, "pending")
        stage(a, w, "x", lambda tx: tx.upsert_manifest([mrow("pending")]), run_id="pending")
        a.freeze(w, "pending", required_gates=["t"])
        assert a.fold(w).done                              # 0: the pin on 0 allows P = 0
        assert a.fold(w).blocked                            # 1 would pass the pin on 0
        for g in range(3):
            with a.read_generation(g) as v:
                assert export_bytes(a, v, tmp_path / f"re{g}") == exports[g], g
        with a.read() as v:
            assert export_bytes(a, v, tmp_path / "now") == exports[2]
    for stmt in ("UPDATE revisions SET row_text = '{}'", "DELETE FROM revisions",
                 "UPDATE generations SET frozen_digest = 'x'", "DELETE FROM generations"):
        expect_refused(a, stmt)


def test_a_generation_commits_only_as_a_complete_promotion(pg):
    """Promotion is atomic by contract, not only by the client: a generation whose run is not
    promoted to it, whose dataset head did not reach it, or without its outbox row cannot
    commit."""
    import psycopg
    with pg.writer() as w:
        open_run(pg, w)
        frozen = pg.freeze(w, "rnd1", required_gates=["t"])
        pg.record_gate(w, frozen, "t", passed=True)
    for n in range(1, len(PROMOTE)):          # every incomplete prefix of the promotion
        with pg._connect() as conn, pytest.raises(psycopg.IntegrityError, match="must commit"):
            for stmt in PROMOTE[:n]:
                conn.execute(stmt)
            conn.commit()
    assert q(pg, "SELECT count(*) FROM generations")[0][0] == 0
    with pg._connect() as conn:
        for stmt in PROMOTE:
            conn.execute(stmt)
        conn.commit()
    assert q(pg, "SELECT current_generation FROM dataset")[0][0] == 0


# --- the broker: discovery persisted before it applies, children through the overlay -----------------

def discovery(ids):
    def compute(view, batch):
        taken = view.known(ids=ids).ids
        fresh = [i for i in ids if i not in taken]
        if fresh:
            batch.insert_entries([entry(i) for i in fresh])
            batch.rotation_set("find_books", {"next": len(fresh)})
        return fresh
    return compute


def test_discovery_is_persisted_before_it_applies_and_replayed_exactly(pg, monkeypatch):
    with pg.writer(round_id="rnd1") as w:
        run = open_run(pg, w)
        broker = store_broker.Broker(pg, w, "rnd1", stage=run)
        with broker.serving():
            real = pg.stage_batch
            calls = []

            def crash_on_apply(*args, **kw):
                calls.append(kw.get("persist_only", False))
                if not kw.get("persist_only"):
                    raise RuntimeError("killed between persisting and applying")
                return real(*args, **kw)
            monkeypatch.setattr(pg, "stage_batch", crash_on_apply)
            with pytest.raises(RuntimeError, match="killed"):
                broker.computed_batch("discover", "merge", discovery(["d-1", "d-2"]))
            monkeypatch.setattr(pg, "stage_batch", real)
            got = pg.batch_receipt(w, "rnd1", "discover", "merge")
            assert (got.status, got.basis_seq) == ("requested", 0)
            persisted = got.request_text

            def must_not_recompute(view, batch):
                raise AssertionError("recomputed against its own effects")
            # re-entry: the persisted request is applied exactly, compute is never called
            assert broker.computed_batch("discover", "merge", must_not_recompute) is None
            got = pg.batch_receipt(w, "rnd1", "discover", "merge")
            assert (got.status, got.seq, got.request_text) == ("applied", 1, persisted)
            # and once applied it is skipped
            assert broker.computed_batch("discover", "merge", must_not_recompute) is None
            with pg.read_staged("rnd1", writer=w) as v:
                assert v.known(ids=["d-1", "d-2"]).ids == {"d-1", "d-2"}
                assert v.rotation_get("find_books") == {"next": 2}
                assert v.version() == store_staging.stage_version("rnd1", 1)
            # a fresh discovery step computes, persists and applies in two transactions
            assert broker.computed_batch("discover", "again", discovery(["d-2", "d-3"])) == ["d-3"]
            assert pg.batch_receipt(w, "rnd1", "discover", "again").seq == 2
    assert calls == [True, False]


def test_an_empty_discovery_gets_a_receipt_and_is_skipped_on_reentry(pg):
    with pg.writer(round_id="rnd1") as w:
        run = open_run(pg, w)
        broker = store_broker.Broker(pg, w, "rnd1", stage=run)
        with broker.serving():
            assert broker.computed_batch("discover", "merge", lambda view, batch: "nothing") \
                == "nothing"
            got = pg.batch_receipt(w, "rnd1", "discover", "merge")
            assert (got.status, got.seq, got.requests) == ("applied", 1, [])
            assert q(pg, "SELECT revision_count FROM batches")[0][0] == 0
            assert broker.computed_batch(
                "discover", "merge", lambda view, batch: pytest.fail("recomputed")) is None
            with pytest.raises(store_broker.BrokerError, match="computed_batch"):
                with broker.local_batch("discover", "merge"):
                    pass


def test_run_round_discovery_replays_the_persisted_merge_not_new_proposals(pg, tmp_path,
                                                                        monkeypatch):
    """The real plan_discovery through a staged broker: after the merge was persisted, the
    finders' proposal files change (a recomputation would differ); re-entry applies exactly the
    persisted ids, suffixes and cursor values."""
    import run_round
    from test_discovery_store import Finders
    monkeypatch.setattr(run_round.ops, "run_event", lambda *a, **k: None)
    backends = {"find_books": {"script": "find_books.py", "args": [], "enabled": True}}
    finders = Finders(tmp_path).add("find_books", [
        entry("oer-1", title="Clash", url="https://e.org/elsewhere.pdf"),  # id taken: suffixed
        entry("fresh-1", title="Fresh one")])
    with pg.writer(round_id="rnd1") as w:
        run = open_run(pg, w)
        stage(pg, w, "seed", lambda tx: (tx.insert_entries([entry("oer-1", title="Existing")]),
                                         tx.rotation_set("find_books", {"flag": "--offset",
                                                                        "next": 5, "step": 5})),
              step="setup")
        broker = store_broker.Broker(pg, w, "rnd1", stage=run)
        with broker.serving():
            real = pg.stage_batch

            def crash_on_apply(*args, **kw):
                if not kw.get("persist_only"):
                    raise RuntimeError("killed")
                return real(*args, **kw)
            monkeypatch.setattr(pg, "stage_batch", crash_on_apply)
            merge = lambda compute: broker.computed_batch("discover", "merge", compute)  # noqa
            successful = run_round.check_finder_results(finders.results, backends,
                                                        {"find_books": {"next": 5}}, "rnd1")
            with pytest.raises(RuntimeError):
                merge(lambda v, b: run_round.plan_discovery(v, b, successful, ["find_books"],
                                                            backends))
            monkeypatch.setattr(pg, "stage_batch", real)
            finders.results[0]["proposal"].write_text(json.dumps([entry("other-9")]))
            assert merge(lambda v, b: pytest.fail("recomputed")) is None
        with pg.read_staged("rnd1", writer=w) as v:
            ids = {e["id"] for e in paged(v, Table.ENTRIES)}
            assert {"oer-1-2", "fresh-1"} <= ids and "other-9" not in ids
            assert v.rotation_get("find_books")["next"] == 10


CHILD = r"""
import json, os, sys
sys.path.insert(0, "scripts")
import store, store_broker, store_pg
st = store_pg.PgStore(sys.argv[1], dsn=sys.argv[2], schema=sys.argv[3], create=False)
out = {}
with st.read() as v:
    out["version"] = v.version().token
    out["ids"] = sorted(r["id"] for r in v.scan(store.Table.MANIFEST, limit=1000).rows)
if sys.argv[4] == "write":
    with store_broker.step_session(st, "fetch") as session:
        with session.batch(sys.argv[5]) as tx:
            tx.upsert_manifest([{"id": sys.argv[5], "title": "t", "url": "https://c.org/" +
                                 sys.argv[5], "source": "s", "license": "open", "topic": "t",
                                 "format": "pdf"}])
        out["after"] = session.version.token
print(json.dumps(out))
"""


def run_child(pg, env, mode, batch=""):
    got = subprocess.run([sys.executable, "-c", CHILD, str(pg.root), pg.dsn, pg.schema, mode,
                          batch], env={**env, "NEKAISE_RUN_ID": "rnd1"}, capture_output=True,
                         text=True, cwd=REPO)
    return got


def test_a_staged_round_through_real_child_processes(pg):
    base = {k: v for k, v in os.environ.items() if not k.startswith("NEKAISE_STORE")}
    with pg.writer(round_id="rnd1") as w:
        with store_broker.staged_round(pg, w, "rnd1", producer_commit=SHA, extractor_version="x",
                                       cleaning_ruleset="none") as rnd:
            rnd.broker.computed_batch("discover", "merge", lambda v, b: b.upsert_manifest(
                [mrow("disc-1")]))
            finders = rnd.pinned_now()                     # discovery workers' shared view
            one = run_child(pg, {**base, **rnd.broker.env()}, "write", "child-1")
            assert one.returncode == 0, one.stderr
            got = json.loads(one.stdout)
            assert got["ids"] == ["disc-1"] and got["version"] == "pg:stage:rnd1:1"
            assert got["after"] == "pg:stage:rnd1:2"
            two = json.loads(run_child(pg, {**base, **rnd.broker.env()}, "write", "child-2").stdout)
            assert two["ids"] == ["child-1", "disc-1"]      # sees the completed batch before it
            pinned = json.loads(run_child(pg, {**base, **finders}, "read").stdout)
            assert pinned["ids"] == ["disc-1"]              # the shared initial view
            plain = json.loads(run_child(pg, base, "read").stdout)
            assert plain["ids"] == [] and plain["version"].startswith("pg:")  # committed only
            forged = run_child(pg, {**base, store_staging.STAGE_ENV: f"rnd1:live:{'0' * 64}"},
                               "read")
            assert forged.returncode != 0 and "AuthorityError" in forged.stderr
            frozen = rnd.freeze(["tests"])
            gate = json.loads(run_child(pg, {**base, **rnd.gate_env()}, "read").stdout)
            assert gate["version"] == f"pg:stage:rnd1:{frozen.seq}" == "pg:stage:rnd1:3"
            late = run_child(pg, {**base, **rnd.broker.env()}, "write", "child-late")
            assert late.returncode != 0                     # drained: no more mutations
            rnd.record_gate("tests", passed=True)
            assert rnd.promote() == 0
        stale = run_child(pg, {**base, **rnd.gate_env()}, "read")
        assert stale.returncode != 0 and "promoted" in stale.stderr
    with pg.read() as v:
        assert sorted(r["id"] for r in paged(v, Table.MANIFEST)) == [
            "child-1", "child-2", "disc-1"]


def test_a_failing_staged_round_is_aborted(pg):
    with pg.writer(round_id="rnd1") as w:
        with pytest.raises(RuntimeError, match="gate crashed"):
            with store_broker.staged_round(pg, w, "rnd1", producer_commit=SHA,
                                           extractor_version="x", cleaning_ruleset="none") as rnd:
                rnd.broker.computed_batch("discover", "merge",
                                          lambda v, b: b.upsert_manifest([mrow("x")]))
                raise RuntimeError("gate crashed")
    assert q(pg, "SELECT status FROM runs")[0][0] == "aborted"
    with pg.read() as v:
        assert v.generation is None and not v.known(ids=["x"]).ids


# --- the v4 -> v5 migration ------------------------------------------------------------------------------

def _v4_module():
    """store_pg.py exactly as the live shadow runs it now (git object at V4_COMMIT)."""
    got = subprocess.run(["git", "-C", str(REPO), "show", f"{V4_COMMIT}:scripts/store_pg.py"],
                         capture_output=True)
    if got.returncode:
        pytest.skip(f"{V4_COMMIT} not in this clone")
    spec = importlib.util.spec_from_loader("store_pg_v4", loader=None)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod  # its dataclasses resolve their module there
    try:
        exec(compile(got.stdout, "store_pg_v4.py", "exec"), mod.__dict__)  # noqa: S102
    finally:
        del sys.modules[spec.name]
    assert mod.SCHEMA_VERSION == 4
    return mod


def quiet(*_):
    pass


def test_a_v4_shadow_migrates_to_v5_and_keeps_replicating(tmp_path, monkeypatch):
    import pg_shadow
    import store_pg
    import test_pg_shadow as shadow
    v4 = _v4_module()
    repo = shadow.Repo(tmp_path / "repo")
    repo.write("registry/backends.json", json.dumps({"find_x": {"script": "x.py", "args": []}}))
    repo.write("registry/eligibility.json", json.dumps({"version": 1, "restrictions": {}}))
    repo.write("registry/rotation.json", json.dumps({"find_x": {"flag": "--page", "next": 1}}))
    repo.write("registry/books.yaml", shadow.yaml_shard("books", [shadow.entry("oer-a")]))
    repo.write("manifest/books.jsonl", shadow.manifest([shadow.mrow("oer-a"),
                                                        shadow.mrow("oer-b", text_chars=1e20)]))
    repo.write("pruned_urls.txt", "https://e.org/old\n")
    repo.write("registry/pruned-3.jsonl", json.dumps({"id": "oer-z", "reason": "junk"}) + "\n")
    c1 = repo.commit("c1")
    schema = f"m_{uuid.uuid4().hex[:12]}"
    old = v4.PgStore(repo.path, dsn=DSN, schema=schema)       # the live shadow's code
    try:
        pg_shadow.do_import(old, c1, repo.path, log=quiet)
        repo.write("pruned_urls.txt", "https://e.org/old\nhttps://e.org/v4\n")
        repo.write("registry/journal/2026-09-25.jsonl", json.dumps(
            {"seq": 1, "run_id": "r", "op": "commit", "digest": "d", "v": 2}) + "\n")
        c2 = repo.commit("c2")
        assert pg_shadow.do_sync(old, c2, repo.path, log=quiet) == 1
        assert pg_shadow.do_verify(old, repo.path, log=quiet)
        with old.read() as v:
            before = export_bytes(old, v, tmp_path / "v4")
        before_digests = pg_shadow.pg_digests(old)
        auth = old.authority()

        new = store_pg.PgStore(repo.path, dsn=DSN, schema=schema)   # migrates 4 -> 5
        assert q(new, "SELECT schema_version FROM state")[0][0] == 5
        assert pg_shadow.pg_digests(new) == before_digests          # nothing rewritten
        with new.read() as v:
            assert export_bytes(new, v, tmp_path / "v5") == before
        assert new.authority() == auth
        assert pg_shadow.do_verify(new, repo.path, log=quiet)
        assert set(q(new, "SELECT consumer FROM outbox_consumers")) >= {("projection",)}
        repo.write("pruned_urls.txt", "https://e.org/old\nhttps://e.org/v4\nhttps://e.org/v5\n")
        c3 = repo.commit("c3")
        assert pg_shadow.do_sync(new, c3, repo.path, log=quiet) == 1
        assert pg_shadow.do_verify(new, repo.path, log=quiet)
        # old clients are refused now
        with pytest.raises(StoreError, match="version 5, code expects 4"):
            v4.PgStore(repo.path, dsn=DSN, schema=schema)
        with pytest.raises(StoreError, match="restart with matching code"):
            with old.writer():
                pass
    finally:
        store_pg.PgStore(repo.path, dsn=DSN, schema=schema, create=False).drop()


def test_v4_runs_in_every_state_migrate_consistently(tmp_path):
    """A v4 schema with runs staged the step-1 way (open with a requested batch, frozen,
    promoted, aborted): the migration seals every applied batch in order, backfills the open
    batch counter, and the promoted generation reads through the overlay."""
    import store_pg
    v4 = _v4_module()
    root = tmp_path / "pg"
    write_config(root)
    schema = f"m_{uuid.uuid4().hex[:12]}"
    old = v4.PgStore(root, dsn=DSN, schema=schema)
    config = {"backends.json": b'{"find_books": {"script": "x.py"}}\n'}
    try:
        old.pin_config_from_files()
        write(old, "seed", lambda tx: tx.upsert_manifest([mrow("base")]))
        with old.writer() as w, old.contracts(w) as c:
            digest = c.put_config_set(config)
            for rid, parent in (("promoted", None), ("open", 0), ("aborted", 0)):
                if rid == "open":
                    conn = c._conn
                    conn.execute("INSERT INTO generations (generation, parent, run_id, "
                                 "producer_commit, config_digest, extractor_version, "
                                 "cleaning_ruleset, frozen_seq, frozen_digest, counts_text) "
                                 "SELECT 0, NULL, run_id, producer_commit, config_digest, "
                                 "extractor_version, cleaning_ruleset, frozen_seq, frozen_digest, "
                                 "'{}' FROM runs WHERE run_id = 'promoted'")
                    conn.execute("UPDATE runs SET status = 'promoted', promoted_generation = 0 "
                                 "WHERE run_id = 'promoted'")
                    conn.execute("UPDATE dataset SET current_generation = 0")
                    conn.execute("INSERT INTO outbox (seq, generation, payload_text) VALUES "
                                 "(1, 0, '{}')")
                c.open_run(rid, kind="round", parent_generation=parent, producer_commit=SHA,
                           config_digest=digest, extractor_version="x", cleaning_ruleset="r")
                for n in (1, 2):
                    c.request_batch(rid, "fetch", f"b{n}", [{"call": "x", "n": n}])
                    c._conn.execute("UPDATE batches SET status = 'applied', seq = %s, applied_at "
                                    "= now() WHERE run_id = %s AND batch = %s",
                                    [n, rid, f"b{n}"])
                    c._conn.execute("UPDATE runs SET staged_seq = %s WHERE run_id = %s", [n, rid])
                    text = store.canonical_row(mrow(f"{rid}-{n}"))
                    c._conn.execute("INSERT INTO revisions (run_id, batch_seq, tbl, key, op, "
                                    "row_text, row_sha256) VALUES (%s, %s, 'manifest', %s, 'put', "
                                    "%s, %s)", [rid, n, f"{rid}-{n}", text,
                                                hashlib.sha256(text.encode()).hexdigest()])
                if rid == "promoted":
                    c._conn.execute("UPDATE runs SET status = 'frozen', frozen_seq = 2, "
                                    "frozen_digest = %s WHERE run_id = %s", ["f" * 64, rid])
                elif rid == "open":
                    c.request_batch(rid, "fetch", "pending", [{"call": "y"}])
                else:
                    c._conn.execute("UPDATE runs SET status = 'aborted' WHERE run_id = %s", [rid])
        new = store_pg.PgStore(root, dsn=DSN, schema=schema)       # migrates 4 -> 5
        rows = q(new, "SELECT run_id, seq, sealed, revision_count, chain_digest FROM batches "
                      "WHERE status = 'applied' ORDER BY run_id, seq")
        assert len(rows) == 6 and all(r[2] and r[3] == 1 and len(r[4]) == 64 for r in rows)
        for rid in ("open", "promoted", "aborted"):
            chain = [r[4] for r in rows if r[0] == rid]
            with new._connect(autocommit=True) as conn:
                origin = conn.execute("SELECT nk_chain_origin(%s)", [rid]).fetchone()[0]
            assert chain[0] != origin and len(set(chain)) == 2
        assert dict(q(new, "SELECT run_id, batches_open FROM runs")) == {
            "open": 1, "promoted": 0, "aborted": 0}
        with new.read() as v:                                        # generation 0 overlays
            assert v.generation == 0
            assert v.known(ids=["base", "promoted-1", "promoted-2", "open-1"]).ids == {
                "base", "promoted-1", "promoted-2"}
        with new.writer() as w:
            with pytest.raises(store.WriterError):                   # the v4 writer owned it
                new.freeze(w, "open", required_gates=["t"])
            new.abort_run(w, "open", reason="migrated")
            assert new.fold(w).done
        with new.read() as v:
            assert v._visibility is None
            assert v.known(ids=["promoted-1", "promoted-2"]).ids == {"promoted-1", "promoted-2"}
    finally:
        store_pg.PgStore(root, dsn=DSN, schema=schema, create=False).drop()


# --- Codex review of 8e40df11cc (P2 1): a partial fold is the pin floor --------------------------------

def _three_generations(pg, rows_per=4):
    with pg.writer() as w:
        for g in range(3):
            open_run(pg, w, f"r{g}")
            stage(pg, w, "b", lambda tx, g=g: tx.upsert_manifest(
                [mrow(f"g{g}-{i}") for i in range(rows_per)]), run_id=f"r{g}")
            finish(pg, w, f"r{g}")
        assert pg.fold(w).done                                     # the projection is 0


def test_a_partial_fold_is_the_pin_floor(pg):
    import psycopg
    _three_generations(pg)
    with pg.writer() as w:
        progress = pg.fold(w, limit=1)                             # generation 1: one row in
        assert (progress.generation, progress.done) == (1, False)
        assert q(pg, "SELECT generation, fold_generation FROM projection_state")[0] == (0, 1)
        with pytest.raises(StoreError, match="already folded"):
            with pg.read_generation(0):
                pass
        with pytest.raises(psycopg.IntegrityError, match="can no longer be pinned"):
            store_staging.pin_generation(pg, w, 0, holder="late", reason="too late")
        store_staging.pin_generation(pg, w, 1, holder="h", reason="keep 1")   # at the floor: ok
        with pg.read_generation(1) as v:
            assert v.known(ids=["g1-3", "g2-0"]).ids == {"g1-3"}
        while not (p := pg.fold(w, limit=1)).done:                 # the fold completes
            assert not p.blocked
        assert pg.fold(w, limit=1).blocked                         # 2 would pass the pin on 1
        with pg.read_generation(1) as v:
            assert v.known(ids=["g1-3", "g2-0"]).ids == {"g1-3"}
        store_staging.unpin_generation(pg, w, 1, holder="h")
        assert store_staging.fold_all(pg, w, limit=1) == 1
    assert q(pg, "SELECT generation, fold_generation FROM projection_state")[0] == (2, None)


def test_a_fold_does_not_start_below_an_active_pin(pg):
    _three_generations(pg)
    with pg.writer() as w:
        store_staging.pin_generation(pg, w, 0, holder="h", reason="keep 0")
        assert pg.fold(w, limit=1).blocked                         # the client refuses to start
    expect_refused(pg, "UPDATE projection_state SET fold_generation = 1, fold_tbl = 'manifest', "
                       "fold_key = 'g1-0'")                         # and so does the database
    assert q(pg, "SELECT generation, fold_generation FROM projection_state")[0] == (0, None)


START_FOLD = ("UPDATE projection_state SET fold_generation = 1, fold_tbl = 'manifest', "
              "fold_key = 'g1-0'")
PIN_0 = "INSERT INTO generation_retention (generation, holder, reason) VALUES (0, 'h', 'keep 0')"


@pytest.mark.parametrize("level", LEVELS)
def test_a_pin_taken_while_a_fold_starts_blocks_it(pg, level):
    import psycopg
    _three_generations(pg)
    pinner, folder = _two(pg, level)
    try:
        folder.execute("SELECT 1 FROM projection_state")
        pinner.execute(PIN_0)
        exc = _blocked_then(lambda: (folder.execute(START_FOLD), folder.commit()), pinner.commit)
        assert isinstance(exc, (psycopg.IntegrityError, psycopg.errors.SerializationFailure)), exc
        folder.rollback()
    finally:
        pinner.close()
        folder.close()
    assert q(pg, "SELECT generation, fold_generation FROM projection_state")[0] == (0, None)


@pytest.mark.parametrize("level", LEVELS)
def test_a_pin_below_a_fold_started_meanwhile_is_refused(pg, level):
    import psycopg
    _three_generations(pg)
    folder, pinner = _two(pg, level)
    try:
        pinner.execute("SELECT 1 FROM projection_state")
        folder.execute(START_FOLD)
        exc = _blocked_then(lambda: (pinner.execute(PIN_0), pinner.commit()), folder.commit)
        assert isinstance(exc, (psycopg.IntegrityError, psycopg.errors.SerializationFailure)), exc
        pinner.rollback()
    finally:
        pinner.close()
        folder.close()
    assert q(pg, "SELECT count(*) FROM generation_retention")[0][0] == 0
    assert q(pg, "SELECT fold_generation FROM projection_state")[0][0] == 1


# --- (P2 3): promotion reads a fixed-size run summary --------------------------------------------------

def test_promotion_reads_the_run_summary_accumulated_at_each_seal(pg, monkeypatch):
    with pg.writer() as w:
        open_run(pg, w)
        stage(pg, w, "b1", lambda tx: (tx.upsert_manifest([mrow("a"), mrow("b")]),
                                       tx.blocklist_add(["https://e.org/x"])))
        stage(pg, w, "b2", lambda tx: tx.delete_manifest(["a"], reason="junk"))
        stage(pg, w, "b3", lambda tx: None)                          # an empty batch counts too
        assert q(pg, "SELECT staged_counts::text, staged_seq FROM runs")[0] == (
            '{"manifest": {"delete": 1, "upsert": 2}, "blocklist": {"insert": 1}}', 3)
        frozen = pg.freeze(w, "rnd1", required_gates=["t"])
        pg.record_gate(w, frozen, "t", passed=True)
        seen = []
        real = pg._writer_conn(w).execute

        def spy(query, params=None, **kw):
            seen.append(str(query))
            return real(query, params, **kw)
        monkeypatch.setattr(pg._writer_conn(w), "execute", spy)
        assert pg.promote(w, frozen) == 0
        monkeypatch.undo()
        assert not [x for x in seen if "FROM batches" in x], seen   # no per-batch work
    counts = json.loads(q(pg, "SELECT counts_text FROM generations")[0][0])
    assert counts == {"batches": 3, "ops": {"blocklist": {"insert": 1},
                                            "manifest": {"delete": 1, "upsert": 2}}}
    expect_refused(pg, "UPDATE runs SET staged_counts = '{}'")


def test_the_run_summary_is_maintained_only_by_sealing(pg):
    import psycopg
    with pg.writer() as w:
        open_run(pg, w)
        c = pg._writer_conn(w)
        text = "[]"
        digest = hashlib.sha256(text.encode()).hexdigest()
        for bad in ('{"counts": {"manifest": {"upsert": -1}}}', '{"counts": []}', "not json",
                    '{"counts": {"manifest": 3}}'):
            with pytest.raises(psycopg.IntegrityError), c.transaction():
                c.execute("INSERT INTO batches (run_id, step, batch, request_digest, "
                          "request_text, basis_seq) VALUES ('rnd1', 's', 'b', %s, %s, 0)",
                          [digest, text])
                c.execute("UPDATE batches SET status = 'applied', seq = 1, applied_at = now()")
                c.execute("UPDATE batches SET sealed = true, counts_text = %s", [bad])
    expect_refused(pg, "UPDATE runs SET staged_counts = '{\"x\": {\"y\": 1}}'")
    assert q(pg, "SELECT staged_counts::text FROM runs")[0][0] == "{}"


# --- (P2 5): replacement expansion and the seal digest are bounded --------------------------------------

def test_replacing_a_large_manifest_is_bounded_in_python(pg, monkeypatch):
    """replace_manifest([]) over 60 000 rows: the tombstones are one statement in the database,
    so Python's peak allocation stays small and independent of the manifest; the seal digest is
    built in 4096-row chunks (checked against an independent computation)."""
    import tracemalloc

    import test_store_pg_staging_bench as bench
    n = 60_000
    bench.populate(pg, n)
    with pg.writer() as w:
        open_run(pg, w)
        v0 = version_of(pg, w)
        requests = recorded(lambda tx: tx.replace_manifest([mrow("kept")], reason="rebuild"))
        tracemalloc.start()
        got = pg.stage_batch(w, "rnd1", "clean", "replace", requests, expected_version=v0)
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        assert got.results == [n + 1]                              # n deleted + 1 upserted
        assert peak < 8 * 1024 * 1024, peak                        # ~60 MB if expanded in Python
        count, digest = q(pg, "SELECT revision_count, revisions_digest FROM batches")[0]
        assert count == n + 1
        lines = [json.dumps([t, k, o, rs, bs, rn], separators=(", ", ": "))
                 for t, k, o, rs, bs, rn in q(pg, "SELECT tbl, key, op, row_sha256, "
                                                  "before_sha256, reason FROM revisions "
                                                  "ORDER BY tbl COLLATE \"C\", key")]
        chunks = [hashlib.sha256("\n".join(lines[i:i + 4096]).encode()).hexdigest()
                  for i in range(0, len(lines), 4096)]
        assert digest == hashlib.sha256("\n".join(chunks).encode()).hexdigest()
        with pg.read_staged("rnd1", writer=w) as v:
            assert [r["id"] for r in v.scan(Table.MANIFEST, limit=10).rows] == ["kept"]
            assert v.get_manifest(["doc-00000000", "kept"]).keys() == {"kept"}
        assert json.loads(q(pg, "SELECT counts_text FROM batches")[0][0])["counts"] == {
            "manifest": {"delete": n, "upsert": 1}}


# --- (P2 2): revisions staged by v4 code are fully readable after the migration -----------------

def test_v4_revisions_get_their_derived_columns(tmp_path, monkeypatch):
    """A generation promoted by step-1 (v4) code: after the migration its revisions carry their
    url/title keys, sha256 and legacy order columns, so membership, duplicate detection and
    small-page legacy scans read the same before folding as after (the projection then holds the
    rows themselves); row text, digests and identities are unchanged."""
    import store_pg
    v4 = _v4_module()
    root = tmp_path / "pg"
    write_config(root)
    schema = f"m_{uuid.uuid4().hex[:12]}"
    old = v4.PgStore(root, dsn=DSN, schema=schema)
    staged = [mrow("v4-a", sha256="dup", title="Shared title"),
              mrow("v4-b", sha256="dup2", url="https://e.org/elsewhere/", topic="urban"),
              mrow("v4-c", sha256="s-c", topic="structures_civil")]
    try:
        old.pin_config_from_files()
        write(old, "seed", lambda tx: tx.upsert_manifest([mrow("base", sha256="dup"),
                                                          mrow("base2", sha256="dup2")]))
        with old.writer() as w, old.contracts(w) as c:
            digest = c.put_config_set({"backends.json": b'{"find_books": {}}'})
            c.open_run("v4run", kind="round", parent_generation=None, producer_commit=SHA,
                       config_digest=digest, extractor_version="x", cleaning_ruleset="r")
            c.request_batch("v4run", "fetch", "b1", [{"call": "x"}])
            conn = c._conn
            conn.execute("UPDATE batches SET status = 'applied', seq = 1, applied_at = now()")
            conn.execute("UPDATE runs SET staged_seq = 1")
            for r in staged:
                text = store.canonical_row(r)
                conn.execute("INSERT INTO revisions (run_id, batch_seq, tbl, key, op, row_text, "
                             "row_sha256) VALUES ('v4run', 1, 'manifest', %s, 'put', %s, %s)",
                             [r["id"], text, hashlib.sha256(text.encode()).hexdigest()])
            conn.execute("UPDATE runs SET status = 'frozen', frozen_seq = 1, frozen_digest = %s",
                         ["f" * 64])
            conn.execute("INSERT INTO generations (generation, parent, run_id, producer_commit, "
                         "config_digest, extractor_version, cleaning_ruleset, frozen_seq, "
                         "frozen_digest, counts_text) SELECT 0, NULL, run_id, producer_commit, "
                         "config_digest, extractor_version, cleaning_ruleset, 1, frozen_digest, "
                         "'{}' FROM runs")
            conn.execute("UPDATE runs SET status = 'promoted', promoted_generation = 0")
            conn.execute("UPDATE dataset SET current_generation = 0")
            conn.execute("INSERT INTO outbox (seq, generation, payload_text) VALUES (1, 0, '{}')")
        before = q(old, "SELECT rev_id, run_id, batch_seq, tbl, key, op, row_text, row_sha256, "
                        "before_sha256, reason FROM revisions ORDER BY rev_id")
        new = store_pg.PgStore(root, dsn=DSN, schema=schema)       # migrates 4 -> 5
        assert q(new, "SELECT rev_id, run_id, batch_seq, tbl, key, op, row_text, row_sha256, "
                      "before_sha256, reason FROM revisions ORDER BY rev_id") == before
        assert q(new, "SELECT count(*) FROM revisions WHERE url_key IS NULL OR title_key IS NULL "
                      "OR sha256 IS NULL OR shard IS NULL OR topic_key IS NULL")[0][0] == 0
        monkeypatch.setattr(store_staging, "SCAN_CHUNK", 1)

        def reads():
            with new.read() as v:
                assert v._visibility is None or v.generation == 0
                return {"known": v.known(urls=[r["url"] for r in staged],
                                         titles=["shared title", "Title of v4-c"],
                                         ids=["v4-a", "v4-b", "v4-c"]),
                        "dups": [r["id"] for r in v.iter_duplicate_sha256(batch_size=1)],
                        "legacy": [r["id"] for r in paged(v, Table.MANIFEST, order="legacy",
                                                          limit=1)],
                        "urban": paged(v, Table.MANIFEST, where=Eq("topic", "urban"), limit=1)}
        unfolded = reads()
        assert unfolded["known"].ids == {"v4-a", "v4-b", "v4-c"}
        assert unfolded["known"].urls == {r["url"].rstrip("/") for r in staged}
        assert unfolded["dups"] == ["base", "v4-a", "base2", "v4-b"]
        assert len(unfolded["legacy"]) == 5
        with new.writer() as w:
            assert new.fold(w).done
        assert reads() == unfolded
    finally:
        store_pg.PgStore(root, dsn=DSN, schema=schema, create=False).drop()
