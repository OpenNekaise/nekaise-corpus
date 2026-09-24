"""Backend-agnostic conformance suite for scripts/store.py (ADR 0001).

Every Store implementation must pass these tests through the PUBLIC API only: a factory creates an
empty store over a directory holding the git-owned configuration, and the fixture seeds it through
transactions. Stage 2 adds the PostgreSQL store to STORES. FileStore-specific behavior (legacy byte
parity, crash injection, lock semantics) lives in tests/test_filestore.py.
"""
import json

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


STORES = [file_store]


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
            next(iter(tx.aggregate_manifest(group_by=("quality",))))["quality"]["total"] = -1
            assert tx.get_manifest(["oer-a"])["oer-a"]["quality"] == {"total": 100,
                                                                       "w20": {"domain": 3}}
            tx.update_manifest_fields({"oer-a": {"topic": "x"}})
    with st.read() as v:
        events = [e for e in v.scan(Table.EVENTS, limit=1000).rows if e["run_id"] == "r1"]
        assert events[0]["before"]["quality"] == {"total": 100, "w20": {"domain": 3}}
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
        assert tombs["manifest"]["before"]["sha256"] == "same"
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


def test_view_expires_when_its_generation_moves(st):
    with st.writer() as w:
        with st.read(writer=w) as v:
            v.scan(Table.ENTRIES)
            with st.transaction("r1", expected_version=st.version(), writer=w) as tx:
                tx.blocklist_add(["https://x.org/new"])
            with pytest.raises(store.StaleView):
                v.scan(Table.BLOCKLIST)  # would lazily load the newer generation


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
        store.open(root=tmp_path, backend="postgres")
