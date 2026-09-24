"""Backend-agnostic conformance suite for scripts/store.py (ADR 0001).

Every Store implementation must pass these tests. Stage 1 runs them against FileStore; stage 2 adds
the PostgreSQL store to STORES. Where FileStore and the legacy registry.py writers overlap, the
tests also pin byte-for-byte parity so moving a caller onto the store changes no tracked file.
"""
import json
import shutil

import pytest

import blocklist
import ops
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
            "quality": {"total": 100}, **extra}


def build_repo(root):
    reg = root / "registry"
    reg.mkdir(parents=True)
    (reg / "curated.yaml").write_text(
        "# hand comment that must survive\nsources:\n"
        + registry.emit_entry(entry("hand-one"))
        + "  # inline hand note\n"
        + registry.emit_entry(entry("hand-two", title="Shared Title")))
    (reg / "books.yaml").write_text(
        registry.shard_header("books") + registry.emit_entry(entry("oer-a"))
        + registry.emit_entry(entry("oer-b", url="https://e.org/dup/")))
    (reg / "rotation.json").write_text(json.dumps(
        {"find_books": {"flag": "--offset", "next": 5, "step": 5}}, indent=2) + "\n")
    (reg / "backends.json").write_text(json.dumps({
        "_readme": "config",
        "find_books": {"script": "find_books.py", "args": [], "enabled": True},
    }, indent=2) + "\n")
    (reg / "eligibility.json").write_text(json.dumps({"version": 1, "restrictions": {}}) + "\n")
    (reg / "pruned-3.jsonl").write_text(json.dumps({"id": "oer-gone", "reason": "junk"}) + "\n")
    man = root / "manifest"
    man.mkdir()
    rows = [mrow("hand-one"), mrow("oer-a", sha256="same"), mrow("oer-b", sha256="same"),
            mrow("hand-two", status="failed", sha256=None, text_chars=None)]
    shards = {}
    for r in rows:
        shards.setdefault(registry.manifest_shard(r["id"]), []).append(r)
    for stem, group in shards.items():
        (man / f"{stem}.jsonl").write_text(registry.manifest_shard_text(group))
    (root / "pruned_urls.txt").write_text("https://e.org/blocked\n")
    return root


def file_store(root):
    return store.FileStore(root)


STORES = [file_store]


@pytest.fixture(params=STORES, ids=lambda f: f.__name__)
def st(request, tmp_path):
    return request.param(build_repo(tmp_path / "repo"))


def snapshot_bytes(root):
    return {str(p.relative_to(root)): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file() and "workspace" not in p.parts}


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
    rows = []
    for sid in ("pat-cn1", "pat-us1", "jst-1", "x", ""):
        for source in ("google_patents", "jstage", "other", None):
            for lic in ("open", "proprietary-internal", None):
                r = {"id": sid}
                if source is not None:
                    r["source"] = source
                if lic is not None:
                    r["license"] = lic
                rows.append(r)
    pred = store.eligibility_where(restrictions)
    for r in rows:
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
        assert ids == sorted(["hand-one", "hand-two", "oer-a", "oer-b"])
        page = v.scan(Table.MANIFEST, where=Eq("status", "ok"), fields=("id", "corpus_path"),
                      limit=10)
        assert page.rows == [{"id": "hand-one"}, {"id": "oer-a"}, {"id": "oer-b"}]
        assert page.next_cursor is None
        first = v.scan(Table.ENTRIES, limit=1)
        with pytest.raises(store.StoreError, match="different view or query"):
            v.scan(Table.MANIFEST, cursor=first.next_cursor)
        with pytest.raises(store.StoreError, match="limit"):
            v.scan(Table.ENTRIES, limit=0)
        assert [r["url"] for r in v.scan(Table.BLOCKLIST).rows] == ["https://e.org/blocked"]


def test_known_normalizes_like_existing_keys(st, monkeypatch):
    with st.read() as v:
        hits = v.known(urls=["https://e.org/dup", " https://e.org/blocked/ ", "https://new", ""],
                       titles=["SHARED   title!", "fresh"], ids=["oer-a", "zzz", ""])
        assert hits.urls == {"https://e.org/dup", "https://e.org/blocked"}
        assert hits.titles == {"shared title"}
        assert hits.ids == {"oer-a"}
        assert v.known(urls=["https://e.org/blocked"], include_blocklist=False).urls == frozenset()
    # Parity with the legacy canonical path over the same files.
    monkeypatch.setenv("NEKAISE_DISABLE_INDEX", "1")
    monkeypatch.setattr(registry, "REG_DIR", st.reg)
    monkeypatch.setattr(registry, "MAN_DIR", st.man)
    monkeypatch.setattr(blocklist, "PATH", st.blocklist_path)
    urls, titles, ids = registry.existing_keys()
    with st.read() as v:
        assert v.known(urls=urls).urls == {u for u in urls if u}
        assert v.known(titles=titles).titles == {t for t in titles if t}
        assert v.known(ids=ids).ids == {i for i in ids if i}


def test_aggregates_and_duplicates(st):
    with st.read() as v:
        groups = list(v.aggregate_manifest(group_by=("status",), sums=("text_chars",)))
        assert groups == [
            {"status": "failed", "count": 1, "sum_text_chars": 0},
            {"status": "ok", "count": 3, "sum_text_chars": 300},
        ]
        dups = [r["id"] for r in v.iter_duplicate_sha256()]
        assert dups == ["oer-a", "oer-b"]
        assert list(v.iter_duplicate_sha256(where=Not(Eq("id", "oer-b")))) == []


def test_read_view_holds_the_round_lock(st):
    with st.read():
        with pytest.raises(RuntimeError, match="held by pid"):
            with st.read(timeout=0):
                pass


def test_resolve_artifact(st):
    with st.read() as v:
        ref = v.resolve_artifact("oer-a", store.Stage.RAW)
        assert ref.locator == "file:raw/test/oer-a.pdf" and ref.sha256 == "same" and ref.size == 10
        assert v.resolve_artifact("oer-a", store.Stage.CORPUS) is None
        assert v.resolve_artifact("nope", store.Stage.RAW) is None


# --- writes -------------------------------------------------------------------------------------

def write(st, run_id, fn):
    with st.writer() as w:
        with st.transaction(run_id, expected_version=st.version(), writer=w) as tx:
            return fn(tx)


def test_insert_entries_matches_legacy_append_bytes(st, tmp_path, monkeypatch):
    legacy_root = tmp_path / "legacy"
    shutil.copytree(st.root, legacy_root)
    new = [entry("oer-c"), entry("arc-old"), entry("hand-three")]
    assert write(st, "r1", lambda tx: tx.insert_entries(new)) == 3
    monkeypatch.setattr(registry, "REG_DIR", legacy_root / "registry")
    monkeypatch.setattr(registry, "MAN_DIR", legacy_root / "manifest")
    monkeypatch.setattr(blocklist, "PATH", legacy_root / "pruned_urls.txt")
    monkeypatch.setenv("NEKAISE_DISABLE_INDEX", "1")
    registry.append_entries(sorted(new, key=lambda e: e["id"]))
    for name in ("books.yaml", "archive.yaml", "curated.yaml"):
        assert (st.reg / name).read_bytes() == (legacy_root / "registry" / name).read_bytes()
    assert "# inline hand note" in (st.reg / "curated.yaml").read_text()


def test_delete_entries_matches_legacy_remove_ids(st, tmp_path, monkeypatch):
    legacy_root = tmp_path / "legacy"
    shutil.copytree(st.root, legacy_root)
    assert write(st, "r1", lambda tx: tx.delete_entries(["hand-one", "oer-b", "none"],
                                                        reason="prune")) == 2
    monkeypatch.setattr(registry, "REG_DIR", legacy_root / "registry")
    registry.remove_ids({"hand-one", "oer-b"})
    for name in ("books.yaml", "curated.yaml"):
        assert (st.reg / name).read_bytes() == (legacy_root / "registry" / name).read_bytes()


def test_replace_manifest_matches_legacy_write_manifest_rows(st, tmp_path, monkeypatch):
    legacy_root = tmp_path / "legacy"
    shutil.copytree(st.root, legacy_root)
    with st.read() as v:
        rows = [r for r in v.scan(Table.MANIFEST).rows if not r["id"].startswith("oer-")]
    rows.append(mrow("zen-new"))
    write(st, "r1", lambda tx: tx.replace_manifest(rows, reason="prune"))
    monkeypatch.setattr(registry, "MAN_DIR", legacy_root / "manifest")
    registry.write_manifest_rows(rows)
    ours = {p.name: p.read_bytes() for p in st.man.glob("*.jsonl")}
    theirs = {p.name: p.read_bytes() for p in (legacy_root / "manifest").glob("*.jsonl")}
    assert ours == theirs and "books.jsonl" not in ours


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
            assert tx.update_manifest_fields({"oer-a": {"text_chars": 100}}) == 0  # no-op patch


def test_read_your_writes_and_journal_tombstones(st):
    def body(tx):
        tx.insert_entries([entry("oer-new", url="https://e.org/new")])
        assert tx.known(urls=["https://e.org/new"]).urls == {"https://e.org/new"}
        assert tx.uniquify_ids([entry("oer-new")])[0]["id"] == "oer-new-2"
        tx.update_manifest_fields({"oer-a": {"corpus_path": "corpus/oer-a.md"}})
        assert tx.get_manifest(["oer-a"])["oer-a"]["corpus_path"] == "corpus/oer-a.md"
        tx.delete_manifest(["oer-b"], reason="dup-bytes")
        tx.blocklist_add(["https://e.org/dup/"])
        tx.ledger_append([{"id": "oer-b", "reason": "dup-bytes"}])
        tx.rotation_set("find_books", {"flag": "--offset", "next": 10, "step": 5})
        tx.backend_state_set("find_books", store.BackendState(False, "exhausted: test"))
    write(st, "r1", body)
    with st.read() as v:
        events = v.scan(Table.EVENTS, limit=100).rows
        tomb = [e for e in events if e["op"] == "delete"]
        assert tomb[0]["id"] == "oer-b" and tomb[0]["reason"] == "dup-bytes"
        assert tomb[0]["before"]["sha256"] == "same"
        assert events[-1]["op"] == "commit" and [e["seq"] for e in events] == list(
            range(1, len(events) + 1))
        assert v.rotation_get("find_books")["next"] == 10
        assert v.backend_state_get("find_books") == store.BackendState(False, "exhausted: test")
        assert v.known(urls=["https://e.org/dup"]).urls == {"https://e.org/dup"}
        assert "oer-b" not in v.get_manifest(["oer-b"])
    assert "https://e.org/dup\n" in st.blocklist_path.read_text()
    backends = json.loads((st.reg / "backends.json").read_text())
    assert backends["_readme"] == "config" and backends["find_books"]["enabled"] is False
    ledger = st.reg / registry.prune_ledger_name("oer-b")
    assert json.loads(ledger.read_text().splitlines()[-1]) == {"id": "oer-b", "reason": "dup-bytes"}


def test_exception_rolls_back_everything(st):
    before = snapshot_bytes(st.root)
    with pytest.raises(ValueError):
        def body(tx):
            tx.insert_entries([entry("oer-new")])
            raise ValueError("boom")
        write(st, "r1", body)
    assert snapshot_bytes(st.root) == before
    assert st.pending_transactions() == []


def test_version_conflict_and_stale_writer(st):
    stale = st.version()
    (st.reg / "curated.yaml").write_text((st.reg / "curated.yaml").read_text() + "\n")
    with st.writer() as w:
        with pytest.raises(store.VersionConflict):
            with st.transaction("r1", expected_version=stale, writer=w):
                pass
    with pytest.raises(store.WriterError, match="stale"):
        with st.transaction("r1", expected_version=st.version(), writer=w):
            pass


def test_identical_retry_is_idempotent_and_conflicting_retry_fails(st):
    shard = st.man / f"{registry.manifest_shard('oer-a')}.jsonl"
    pre = shard.read_bytes()
    write(st, "r1", lambda tx: tx.update_manifest_fields({"oer-a": {"topic": "t1"}}))
    # Replay the same run against the same pre-state (what shadow replay does): the journal
    # already holds r1 with an identical digest, so the retry must change nothing.
    shard.write_bytes(pre)
    events = st._events()
    write(st, "r1", lambda tx: tx.update_manifest_fields({"oer-a": {"topic": "t1"}}))
    assert shard.read_bytes() == pre and st._events() == events
    with pytest.raises(store.StoreError, match="already committed different"):
        write(st, "r1", lambda tx: tx.update_manifest_fields({"oer-a": {"topic": "t2"}}))
    assert st._events() == events


def test_crash_mid_commit_is_recoverable(st, monkeypatch):
    before = snapshot_bytes(st.root)
    real = ops.atomic_write_bytes
    calls = {"n": 0}

    def flaky(path, data):
        if "store-transactions" not in str(path):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("disk yanked")
        real(path, data)

    monkeypatch.setattr(ops, "atomic_write_bytes", flaky)
    with pytest.raises(OSError):
        write(st, "r1", lambda tx: (tx.insert_entries([entry("oer-new")]),
                                    tx.delete_manifest(["oer-a"], reason="x")))
    monkeypatch.setattr(ops, "atomic_write_bytes", real)
    assert [t.state for t in st.pending_transactions()] == ["prepared"]
    with st.writer() as w:
        with pytest.raises(store.PendingTransaction):
            with st.transaction("r2", expected_version=st.version(), writer=w):
                pass
        assert st.recover("r1", writer=w).action == "rolled_back"
    assert snapshot_bytes(st.root) == before


def test_export_is_deterministic(st, tmp_path):
    with st.read() as v:
        a = st.export(tmp_path / "a", view=v)
        b = st.export(tmp_path / "b", view=v)
    assert a.files == b.files
    assert snapshot_bytes(tmp_path / "a") == snapshot_bytes(tmp_path / "b")
    assert a.files["manifest.jsonl"]["rows"] == 4


def test_unknown_backend_fails_explicitly(tmp_path):
    with pytest.raises(store.StoreError, match="not available"):
        store.open(root=tmp_path, backend="postgres")


def test_indexed_and_canonical_known_agree(st, monkeypatch):
    probe = dict(urls=["https://e.org/dup", "https://e.org/blocked", "https://e.org/oer-a.pdf",
                       "https://absent"],
                 titles=["shared title", "title of oer a", "absent"], ids=["oer-a", "absent"])
    with st.read() as v:
        indexed = v.known(**probe)
    monkeypatch.setenv("NEKAISE_DISABLE_INDEX", "1")
    with st.read() as v:
        assert v.known(**probe) == indexed
    monkeypatch.delenv("NEKAISE_DISABLE_INDEX")

    def body(tx):  # uncommitted local writes must bypass the (stale) index
        tx.blocklist_add(["https://absent"])
        assert "https://absent" in tx.known(urls=["https://absent"]).urls
    write(st, "r1", body)
