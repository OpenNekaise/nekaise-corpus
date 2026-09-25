"""Backend-agnostic conformance suite for scripts/store.py (ADR 0001).

Every Store implementation must pass these tests through the PUBLIC API only: a factory creates an
empty store over a directory holding the git-owned configuration, and the fixture seeds it through
transactions. Stage 2 adds the PostgreSQL store to STORES. FileStore-specific behavior (legacy byte
parity, crash injection, lock semantics) lives in tests/test_filestore.py.
"""
import json
import os

import pytest

import registry
import store
from store import And, Eq, Exists, In, Not, Or, Prefix, Table


def entry(sid, url=None, title=None, **extra):
    return {"id": sid, "title": title or f"Title of {sid}", "url": url or f"https://e.org/{sid}.pdf",
            "source": "test", "license": "public-domain", "topic": "building_energy",
            "format": "pdf", **extra}


def mrow(sid, **extra):
    return {**entry(sid), "status": "ok", "http_status": 200, "sha256": f"sha-{sid}",
            "bytes": 10, "raw_path": f"raw/test/{sid}.pdf", "text_path": f"text/{sid}.md",
            "text_chars": 100, "error": None, "fetched_at": "2026-09-24T00:00:00Z",
            "quality": {"total": 100, "w20": {"domain": 3}}, **extra}


CONFIG = {
    "backends.json": {"_readme": "config", "find_books": {"script": "find_books.py", "args": [],
                                                          "enabled": True},
                      "find_paused": {"script": "x.py", "args": [], "enabled": False}},
    "eligibility.json": {"version": 1, "restrictions": {}},
    "vendors.json": {"acme": {"sitemap": "https://acme.example/sitemap.xml"}},
}


def write_config(root):
    reg = root / "registry"
    reg.mkdir(parents=True, exist_ok=True)
    for name, doc in CONFIG.items():
        (reg / name).write_text(json.dumps(doc, indent=2) + "\n")


def file_store(root):
    write_config(root)
    return store.FileStore(root)


def pg_store(root):
    import uuid

    import store_pg
    write_config(root)
    st = store_pg.PgStore(root, dsn=os.environ["NEKAISE_PG_TEST_DSN"],
                          schema=f"t_{uuid.uuid4().hex[:12]}")
    st.pin_config_from_files()
    return st


# The PostgreSQL store joins the suite when a test database is configured (opt-in, so the dig
# round's pytest gate never depends on a running server).
STORES = [file_store] + ([pg_store] if os.environ.get("NEKAISE_PG_TEST_DSN") else [])


def write(st, run_id, fn):
    with st.writer() as w:
        with st.transaction(run_id, expected_version=st.version(), writer=w) as tx:
            return fn(tx)


def seed(tx):
    tx.insert_entries([entry("hand-one"), entry("hand-two", title="Shared Title"),
                       entry("oer-a"), entry("oer-b", url="https://e.org/dup/")])
    tx.upsert_manifest([mrow("hand-one"), mrow("oer-a", sha256="same"),
                        mrow("oer-b", sha256="same", url="https://e.org/dup/"),
                        mrow("hand-two", status="failed", sha256=None, text_chars=None)])
    tx.blocklist_add(["https://e.org/blocked"])
    tx.ledger_append([{"id": "oer-gone", "reason": "junk"}])
    tx.rotation_set("find_books", {"flag": "--offset", "next": 5, "step": 5})


@pytest.fixture(params=STORES, ids=lambda f: f.__name__)
def st(request, tmp_path):
    s = request.param(tmp_path / "repo")
    if hasattr(s, "drop"):
        request.addfinalizer(s.drop)
    write(s, "seed", seed)
    return s


# --- predicates ---------------------------------------------------------------------------------

def test_predicate_semantics_missing_vs_null():
    row = {"id": "x", "error": None, "license": "cc-by"}
    assert store.evaluate(Exists("error"), row)
    assert not store.evaluate(Exists("topic"), row)
    assert store.evaluate(Eq("error", None), row)
    assert not store.evaluate(Eq("topic", None), row)  # missing is not null
    assert store.evaluate(Not(In("topic", ["a"])), row)
    assert store.evaluate(And(Prefix("id", "x"), Or(Eq("license", "cc-by"), Eq("id", "y"))), row)
    assert not store.evaluate(Prefix("error", ""), row)  # prefix needs a string


def test_eligibility_predicate_matches_python_policy():
    restrictions = {
        "cn": {"status": "restricted", "match": {"id_prefix": "pat-cn", "source": "google_patents"}},
        "js": {"status": "restricted", "match": {"source": "jstage"}},
    }
    pred = store.eligibility_where(restrictions)
    for sid in ("pat-cn1", "pat-us1", "jst-1", "x", ""):
        for source in ("google_patents", "jstage", "other", None):
            for lic in ("open", "proprietary-internal", None):
                r = {"id": sid}
                if source is not None:
                    r["source"] = source
                if lic is not None:
                    r["license"] = lic
                assert store.evaluate(pred, r) == registry.is_training_eligible(r, restrictions), r


def test_unknown_fields_are_rejected(st):
    with st.read() as v:
        with pytest.raises(store.StoreError, match="unknown manifest field"):
            v.scan(Table.MANIFEST, where=Eq("nope", 1))
        with pytest.raises(store.StoreError, match="unknown entries field"):
            v.scan(Table.ENTRIES, fields=("status",))


# --- reads --------------------------------------------------------------------------------------

def test_scan_orders_and_paginates(st):
    with st.read() as v:
        ids, cursor = [], None
        while True:
            page = v.scan(Table.MANIFEST, fields=("id",), cursor=cursor, limit=1)
            ids += [r["id"] for r in page.rows]
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
        assert ids == ["hand-one", "hand-two", "oer-a", "oer-b"]
        page = v.scan(Table.MANIFEST, where=Eq("status", "ok"), fields=("id", "corpus_path"))
        assert page.rows == [{"id": "hand-one"}, {"id": "oer-a"}, {"id": "oer-b"}]
        first = v.scan(Table.ENTRIES, limit=1)
        with pytest.raises(store.StoreError, match="different view"):
            v.scan(Table.MANIFEST, cursor=first.next_cursor)
        with pytest.raises(store.StoreError, match="limit"):
            v.scan(Table.ENTRIES, limit=0)
        assert [r["url"] for r in v.scan(Table.BLOCKLIST).rows] == ["https://e.org/blocked"]
    with st.read() as v2:  # cursors do not survive their view
        with pytest.raises(store.StoreError, match="different view"):
            v2.scan(Table.ENTRIES, cursor=first.next_cursor)


def test_returned_values_are_isolated(st):
    with st.writer() as w:
        with st.transaction("r1", expected_version=st.version(), writer=w) as tx:
            got = tx.scan(Table.MANIFEST, fields=("quality",), where=Eq("id", "oer-a")).rows[0]
            got["quality"]["total"] = -1
            tx.get_manifest(["oer-a"])["oer-a"]["quality"]["w20"]["domain"] = -1
            with pytest.raises(store.StoreError, match="cannot group by 'quality'"):
                list(tx.aggregate_manifest(group_by=("quality",)))
            assert tx.get_manifest(["oer-a"])["oer-a"]["quality"] == {"total": 100,
                                                                       "w20": {"domain": 3}}
            tx.update_manifest_fields({"oer-a": {"topic": "x"}})
    with st.read() as v:
        events = [e for e in v.scan(Table.EVENTS, limit=1000).rows if e["run_id"] == "r1"]
        assert events[-1]["counts"] == {"manifest": {"update": 1}}  # receipt, no row images
        assert v.get_manifest(["oer-a"])["oer-a"]["quality"]["total"] == 100


def test_known_normalization(st):
    with st.read() as v:
        hits = v.known(urls=["https://e.org/dup", " https://e.org/blocked/ ", "https://new", ""],
                       titles=["SHARED   title!", "fresh"], ids=["oer-a", "zzz", ""])
        assert hits.urls == {"https://e.org/dup", "https://e.org/blocked"}
        assert hits.titles == {"shared title"}
        assert hits.ids == {"oer-a"}
        assert v.known(urls=["https://e.org/blocked"], include_blocklist=False).urls == frozenset()
        with pytest.raises(store.StoreError, match="at most"):
            v.known(ids=[str(i) for i in range(store.MAX_KNOWN + 1)])


def test_aggregates_and_duplicates(st):
    with st.read() as v:
        groups = list(v.aggregate_manifest(group_by=("status",), sums=("text_chars",)))
        assert groups == [
            {"status": "failed", "count": 1, "sum_text_chars": 0},
            {"status": "ok", "count": 3, "sum_text_chars": 300},
        ]
        assert [r["id"] for r in v.iter_duplicate_sha256()] == ["oer-a", "oer-b"]
        assert list(v.iter_duplicate_sha256(where=Not(Eq("id", "oer-b")))) == []


def test_resolve_artifact(st):
    with st.read() as v:
        ref = v.resolve_artifact("oer-a", store.Stage.RAW)
        assert ref.locator == "file:raw/test/oer-a.pdf" and ref.sha256 == "same" and ref.size == 10
        assert v.resolve_artifact("oer-a", store.Stage.CORPUS) is None
        assert v.resolve_artifact("nope", store.Stage.RAW) is None


def test_views_close(st):
    with st.read() as v:
        pass
    with pytest.raises(store.StaleView):
        v.scan(Table.ENTRIES)


# --- writes -------------------------------------------------------------------------------------

def test_mutation_validation(st):
    with st.writer() as w:
        with st.transaction("r1", expected_version=st.version(), writer=w) as tx:
            with pytest.raises(store.StoreError, match="already exist"):
                tx.insert_entries([entry("oer-a")])
            with pytest.raises(store.StoreError, match="duplicate ids"):
                tx.upsert_manifest([mrow("a"), mrow("a")])
            with pytest.raises(store.StoreError, match="unknown id"):
                tx.update_manifest_fields({"nope": {"topic": "x"}})
            with pytest.raises(store.StoreError, match="cannot change id"):
                tx.update_manifest_fields({"oer-a": {"id": "x"}})
            with pytest.raises(store.StoreError, match="both set and unset"):
                tx.update_manifest_fields({"oer-a": {"topic": "x"}}, unset=("topic",))
            with pytest.raises(store.StoreError, match="lacks"):
                tx.insert_entries([{"id": "x"}])
            with pytest.raises(store.StoreError, match="unknown backend"):
                tx.backend_state_set("_readme", store.BackendState(False))
            assert tx.update_manifest_fields({"oer-a": {"text_chars": 100}}) == 0  # no-op patch


def test_read_your_writes_journal_and_tombstones(st):
    def body(tx):
        tx.insert_entries([entry("oer-new", url="https://e.org/new")])
        assert tx.known(urls=["https://e.org/new"]).urls == {"https://e.org/new"}
        assert tx.uniquify_ids([entry("oer-new")])[0]["id"] == "oer-new-2"
        tx.update_manifest_fields({"oer-a": {"corpus_path": "corpus/oer-a.md"}})
        assert tx.get_manifest(["oer-a"])["oer-a"]["corpus_path"] == "corpus/oer-a.md"
        tx.delete_manifest(["oer-b"], reason="dup-bytes")
        tx.delete_entries(["oer-b"], reason="dup-bytes")
        tx.blocklist_add(["https://e.org/dup/"])
        tx.ledger_append([{"id": "oer-b", "reason": "dup-bytes"}])
        tx.rotation_set("find_books", {"flag": "--offset", "next": 10, "step": 5})
    write(st, "r1", body)
    with st.read() as v:
        events = v.scan(Table.EVENTS, limit=1000).rows
        assert [e["seq"] for e in events] == list(range(1, len(events) + 1))
        mine = [e for e in events if e["run_id"] == "r1"]
        tombs = {e["table"]: e for e in mine if e["op"] == "delete"}
        assert tombs["manifest"]["id"] == tombs["entries"]["id"] == "oer-b"
        assert len(tombs["manifest"]["before_sha256"]) == 64
        assert all(e["v"] == store.EVENT_VERSION and "before" not in e and "after" not in e
                   for e in mine)
        assert mine[-1]["counts"]["manifest"]["delete"] == 1
        assert mine[-1]["counts"]["blocklist"] == {"insert": 1}
        assert tombs["manifest"]["reason"] == tombs["entries"]["reason"] == "dup-bytes"
        assert mine[-1]["op"] == "commit" and mine[-1]["digest"]
        assert v.rotation_get("find_books")["next"] == 10
        assert v.known(urls=["https://e.org/dup"]).urls == {"https://e.org/dup"}
        assert v.get_manifest(["oer-b"]) == {}
        assert [r["id"] for r in v.scan(Table.LEDGER).rows] == ["oer-b", "oer-gone"]


def test_runtime_backend_state_is_separate_from_config(st):
    write(st, "r1", lambda tx: tx.backend_state_set(
        "find_books", store.BackendState(False, "exhausted: test")))
    with st.read() as v:
        assert v.backend_state_get("find_books") == store.BackendState(False, "exhausted: test")
        assert v.config_get().backends["find_books"]["enabled"] is True  # config untouched
        assert not v.backend_enabled("find_books")
    write(st, "r2", lambda tx: tx.backend_state_set("find_paused", store.BackendState(True)))
    with st.read() as v:
        assert not v.backend_enabled("find_paused")  # runtime can never override operator pause


def test_runtime_state_accepts_only_configured_backends_and_reads_back_exactly(st):
    with pytest.raises(store.StoreError, match="unknown backend"):
        write(st, "r1", lambda tx: tx.backend_state_set("find_nobody", store.BackendState(False,
                                                                                          "x")))
    write(st, "r2", lambda tx: tx.backend_state_set(
        "find_books", store.BackendState(False, "exhausted: all offsets")))
    write(st, "r3", lambda tx: tx.backend_state_set(  # identical value: a no-op change
        "find_books", store.BackendState(False, "exhausted: all offsets")))
    with st.read() as v:
        assert v.backend_state_get() == {
            "find_books": store.BackendState(False, "exhausted: all offsets"),
            "find_paused": store.BackendState()}
        receipts = [e for e in v.scan(Table.EVENTS, limit=1000).rows if e["op"] == "commit"]
    assert [(e["run_id"], e["counts"]) for e in receipts if e["run_id"] in ("r2", "r3")] == [
        ("r2", {"backend_state": {"upsert": 1}}), ("r3", {})]


def test_inserts_are_visible_to_later_reads_and_writes_of_the_transaction(st):
    def body(tx):
        assert tx.insert_entries([entry("oer-c"), entry("ost-new"), entry("vnd-x1")]) == 3
        assert set(tx.get_entries(["oer-c", "vnd-x1"])) == {"oer-c", "vnd-x1"}
        assert tx.upsert_entries([entry("oer-c", title="Changed")]) == 1
        return [r["id"] for r in tx.scan(Table.ENTRIES, limit=100).rows]
    assert write(st, "r1", body) == ["hand-one", "hand-two", "oer-a", "oer-b", "oer-c",
                                     "ost-new", "vnd-x1"]
    with st.read() as v:
        assert v.get_entries(["oer-c"])["oer-c"]["title"] == "Changed"
    for run, fn in (("r2", lambda tx: tx.insert_entries([entry("oer-a")])),
                    ("r3", lambda tx: (tx.insert_entries([entry("oer-z")]),
                                       tx.insert_entries([entry("oer-z")])))):
        with pytest.raises(store.StoreError, match="already exist"):
            write(st, run, fn)
    with st.read() as v:
        assert v.get_entries(["oer-z"]) == {}


def test_exception_rolls_back_everything(st, tmp_path):
    with st.read() as v:
        before = st.export(tmp_path / "before", view=v).files
    with pytest.raises(ValueError):
        def body(tx):
            tx.insert_entries([entry("oer-new")])
            raise ValueError("boom")
        write(st, "r1", body)
    with st.read() as v:
        assert st.export(tmp_path / "after", view=v).files == before
    assert st.pending_transactions() == []


def test_version_conflict_stale_writer_and_nesting(st):
    stale = st.version()
    write(st, "r1", lambda tx: tx.blocklist_add(["https://x.org/a"]))
    with st.writer() as w:
        with pytest.raises(store.VersionConflict):
            with st.transaction("r2", expected_version=stale, writer=w):
                pass
        with st.transaction("r3", expected_version=st.version(), writer=w):
            with pytest.raises(store.StoreError, match="do not nest"):
                with st.transaction("r4", expected_version=st.version(), writer=w):
                    pass
    with pytest.raises(store.WriterError, match="stale"):
        with st.transaction("r5", expected_version=st.version(), writer=w):
            pass


def test_views_never_mix_generations(st):
    """A view serves one snapshot: after a later commit it either keeps returning its original
    snapshot (PostgreSQL) or raises StaleView (FileStore) — it never mixes generations."""
    with st.writer() as w:
        with st.read(writer=w) as v:
            before = [r["url"] for r in v.scan(Table.BLOCKLIST).rows]
            with st.transaction("r1", expected_version=st.version(), writer=w) as tx:
                tx.blocklist_add(["https://x.org/new"])
            try:
                again = [r["url"] for r in v.scan(Table.BLOCKLIST).rows]
                hits = v.known(urls=["https://x.org/new"]).urls
            except store.StaleView:
                return
            assert again == before and "https://x.org/new" not in hits


def test_retry_of_a_committed_run_is_a_noop_and_different_requests_fail(st, tmp_path):
    def body(tx):
        tx.update_manifest_fields({"oer-a": {"topic": "t1"}})
        tx.ledger_append([{"id": "oer-a", "reason": "audit"}])
        tx.insert_entries([entry("oer-z")])
    write(st, "r1", body)
    with st.read() as v:
        before = st.export(tmp_path / "before", view=v).files
    write(st, "r1", body)  # an ordinary retry of the same run: identical requests
    with st.read() as v:
        assert st.export(tmp_path / "after", view=v).files == before
    with pytest.raises(store.StoreError, match="already committed different"):
        write(st, "r1", lambda tx: tx.update_manifest_fields({"oer-a": {"topic": "t2"}}))


def test_export_is_deterministic_and_complete(st, tmp_path):
    with st.read() as v:
        a = st.export(tmp_path / "a", view=v)
        b = st.export(tmp_path / "b", view=v)
    assert a.files == b.files
    assert a.files["manifest.jsonl"]["rows"] == 4
    assert {"config/vendors.json", "config/backends.json", "backend_state.json"} <= set(a.files)
    summary = json.loads((tmp_path / "a" / "EXPORT.json").read_text())
    assert set(summary["config_digests"]) == set(CONFIG)
    for name in a.files:
        assert (tmp_path / "a" / name).read_bytes() == (tmp_path / "b" / name).read_bytes()


def test_unknown_backend_fails_explicitly(tmp_path):
    with pytest.raises(store.StoreError, match="not available"):
        store.open(root=tmp_path, backend="nope")


def test_noop_run_still_records_its_identity(st):
    write(st, "a", lambda tx: tx.update_manifest_fields({"oer-a": {"topic": "building_energy"}}))
    write(st, "b", lambda tx: tx.update_manifest_fields({"oer-a": {"topic": "changed"}}))
    write(st, "a", lambda tx: tx.update_manifest_fields({"oer-a": {"topic": "building_energy"}}))
    with st.read() as v:  # retrying the no-op run must not revert run b
        assert v.get_manifest(["oer-a"])["oer-a"]["topic"] == "changed"


def test_one_shot_iterables_are_accepted_positionally_and_by_keyword(st):
    def body(tx):
        return (tx.blocklist_add(urls=(u for u in ["https://g.org/1"])),
                tx.blocklist_add(u for u in ["https://g.org/2"]),
                tx.delete_manifest(ids={"oer-b": 1}.keys(), reason="x"))
    assert write(st, "r1", body) == (1, 1, 1)
    assert write(st, "r1", body) == (0, 0, 0)  # the identical requests replay as a no-op
    with st.read() as v:
        assert v.known(urls=["https://g.org/1", "https://g.org/2"]).urls == {
            "https://g.org/1", "https://g.org/2"}


def test_aggregate_sums_are_exact_and_typed(st):
    def body(tx):
        tx.upsert_manifest([mrow("x-1", text_chars=0.1, status="s"),
                            mrow("x-2", text_chars=0.2, status="s"),
                            mrow("x-3", text_chars=True, status="s"),  # bools never sum
                            mrow("x-4", text_chars=7, status="t")])
    write(st, "r1", body)
    with st.read() as v:
        got = {g["status"]: g["sum_text_chars"]
               for g in v.aggregate_manifest(group_by=("status",), sums=("text_chars",),
                                             where=Prefix("id", "x-"))}
    assert got == {"s": 0.3, "t": 7}  # exact: 0.1 + 0.2 == 0.3, not 0.30000000000000004
    assert isinstance(got["t"], int)


def test_lost_commit_response_retry_is_a_noop(st, tmp_path):
    stale = st.version()  # the client never learns the version its commit produced

    def body(tx):
        tx.insert_entries([entry("oer-lost")])
        tx.blocklist_add(["https://lost.example"])
    write(st, "r1", body)
    with st.read() as v:
        before = st.export(tmp_path / "a", view=v).files
    with st.writer() as w:
        with st.transaction("r1", expected_version=stale, writer=w) as tx:
            body(tx)
    with st.read() as v:
        assert st.export(tmp_path / "b", view=v).files == before


def test_invalid_json_values_are_rejected_everywhere(st):
    with st.writer() as w:
        with st.transaction("r1", expected_version=st.version(), writer=w) as tx:
            with pytest.raises(store.StoreError, match="NaN"):
                tx.upsert_manifest([mrow("n", text_chars=float("nan"))])
            with pytest.raises(store.StoreError, match="NUL"):
                tx.insert_entries([entry("z", title="bad\x00title")])
            with pytest.raises(store.StoreError, match="NUL"):
                tx.update_manifest_fields({"oer-a": {"error": "x\x00y"}})
            with pytest.raises(store.StoreError, match="NUL"):
                tx.ledger_append([{"id": "q", "reason": "\x00"}])
            assert tx.update_manifest_fields({"oer-a": {"text_chars": 100.0}}) == 1  # 100 -> 100.0
    with st.read() as v:
        events = [e for e in v.scan(Table.EVENTS, limit=1000).rows if e["run_id"] == "r1"]
        assert [e["op"] for e in events] == ["commit"]  # failed batches left no trace
        assert events[0]["counts"] == {"manifest": {"update": 1}}


def test_control_documents_roundtrip_journal_and_export(st, tmp_path):
    doc = {"NatLabRockies/ResStock": {"docs": ["md", "tex"], "at": "2026-09-24"}}
    write(st, "r1", lambda tx: tx.control_set("github_passes.json", doc))
    with st.read() as v:
        assert v.control_get("github_passes.json") == doc
        with pytest.raises(store.StoreError, match="unknown control"):
            v.control_get("nope.json")
        assert "control/github_passes.json" in st.export(tmp_path / "e", view=v).files
    write(st, "r2", lambda tx: tx.control_set("github_passes.json", None))
    with st.read() as v:
        assert v.control_get("github_passes.json") is None
        events = [e for e in v.scan(Table.EVENTS, limit=1000).rows if e["run_id"] in ("r1", "r2")]
    assert [(e["run_id"], e["op"], e.get("counts")) for e in events] == [
        ("r1", "commit", {"control": {"upsert": 1}}),
        ("r2", "delete", None), ("r2", "commit", {"control": {"delete": 1}})]
    assert events[1]["id"] == "github_passes.json"


def test_legacy_manifest_order_matches_load_manifest_rows(st):
    rows = [mrow("oer-z", topic="a"), mrow("oer-y", topic="b"), mrow("zen-1", topic="a"),
            mrow("pat-us123", topic="c"), mrow("hand-x", topic="z")]
    write(st, "r1", lambda tx: tx.upsert_manifest(rows))
    with st.read() as v:
        got, cursor = [], None
        while True:
            page = v.scan(Table.MANIFEST, fields=("id",), cursor=cursor, limit=2, order="legacy")
            got += [r["id"] for r in page.rows]
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
        everything = v.scan(Table.MANIFEST, limit=100).rows
        with pytest.raises(store.StoreError, match="unsupported scan order"):
            v.scan(Table.ENTRIES, order="legacy")
    assert got == [r["id"] for r in sorted(everything, key=store.legacy_manifest_key)]
    # the legacy order really is registry's: shard file name, then (topic, id) inside a shard
    assert got.index("oer-z") < got.index("oer-y") < got.index("hand-x")  # books < curated
    assert got.index("oer-z") < got.index("oer-y") < got.index("oer-a")  # topics a < b < building_energy


def test_compiled_predicates_match_evaluate_exactly():
    import itertools
    values = [None, "a", "ab", 1, 1.0, True, {"k": 1}, [1]]
    rows = [{}] + [{"f": v} for v in values]
    preds = [Eq("f", v) for v in values] + [In("f", ["a", "b"]), In("f", [1, True, None]),
             Prefix("f", "a"), Exists("f")]
    preds += [Not(p) for p in preds[:4]] + [And(preds[1], Exists("f")), Or(preds[0], preds[1]),
                                            And(), Or()]
    for pred, row in itertools.product(preds, rows):
        assert store.compile_python(pred)(row) == store.evaluate(pred, row), (pred, row)


def test_streaming_exact_sum_matches_exact_sum():
    cases = [[], [1, 2], [0.1, 0.2], [10**30, 1, -10**30], [1, 0.5, True, None, "x"], [1e20, 1]]
    for values in cases:
        acc = store.ExactSum()
        for v in values:
            acc.add(v)
        assert acc.value() == store.exact_sum(values) and \
            type(acc.value()) is type(store.exact_sum(values)), values


@pytest.mark.parametrize("table,kwargs", [
    (Table.ENTRIES, {}),
    (Table.ENTRIES, {"where": Prefix("id", "oer-")}),
    (Table.MANIFEST, {"order": "legacy"}),
    (Table.BLOCKLIST, {}),
])
def test_a_warmed_view_still_refuses_after_close(st, table, kwargs):
    # found by the maintainer's publication review (2026-09-24): cached scans bypassed the check
    with st.read() as view:
        first = view.scan(table, limit=1, **kwargs)
        assert first.rows
    with pytest.raises(store.StaleView):
        view.scan(table, **kwargs)
    if first.next_cursor is not None:
        with pytest.raises(store.StaleView):
            view.scan(table, cursor=first.next_cursor, limit=1, **kwargs)


def test_known_pids_reads_persistent_ids_and_aliases_of_any_row(st):
    """Persistent-identifier membership (DOI / OpenAlex id) over the rows' own JSON: the
    persistent_id of ANY source and every origin_ids alias, registry and manifest-only rows,
    committed and read-your-writes."""
    from runids import rid

    def body(tx):
        tx.insert_entries([
            entry("ojs-paper", persistent_id="https://doi.org/10.1234/ABC.1"),
            entry("ope-legacy", persistent_id="https://doi.org/10.5555/pub.2",
                  origin_ids="doi:10.5555/pub.2 doi:10.2139/ssrn.99 openalex:W77"),
            entry("nlr-report", persistent_id="NREL/TP-5500-1"),
        ])
        tx.upsert_manifest([mrow("hand-one", persistent_id="doi:10.9999/Manifest.Only")])
        assert tx.known_pids(["doi:10.2139/ssrn.99"]) == {"doi:10.2139/ssrn.99"}
    write(st, rid("pids"), body)
    with st.read() as v:
        assert v.known_pids(["doi:10.1234/abc.1", "doi:10.5555/pub.2", "doi:10.2139/ssrn.99",
                             "openalex:W77", "doi:10.9999/manifest.only", "doi:10.1234/abc",
                             "doi:10.1234/abc.10", "openalex:W7"]) == {
            "doi:10.1234/abc.1", "doi:10.5555/pub.2", "doi:10.2139/ssrn.99", "openalex:W77",
            "doi:10.9999/manifest.only"}
        # only normalized values can be known
        assert v.known_pids(["https://doi.org/10.1234/ABC.1", "", "NREL/TP-5500-1"]) == set()
        with pytest.raises(store.StoreError, match="at most"):
            v.known_pids([f"doi:10.1000/{i}" for i in range(store.MAX_KNOWN + 1)])
