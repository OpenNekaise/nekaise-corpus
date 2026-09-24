"""FileStore-specific behavior: byte parity with the legacy registry.py writers, crash injection
and recovery on the real file layout, lock ownership, and interlocks with round snapshots."""
import json
import os
import shutil
import subprocess
import sys

import pytest

import blocklist
import ops
import registry
import store
from store import Table
from test_store_contract import entry, mrow, write, write_config


@pytest.fixture
def st(tmp_path):
    """A FileStore over hand-written files, exactly as the legacy writers leave them."""
    root = tmp_path / "repo"
    write_config(root)
    reg = root / "registry"
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
    man = root / "manifest"
    man.mkdir()
    shards = {}
    for r in (mrow("hand-one"), mrow("oer-a", sha256="same"), mrow("oer-b", sha256="same"),
              mrow("hand-two", status="failed", sha256=None, text_chars=None)):
        shards.setdefault(registry.manifest_shard(r["id"]), []).append(r)
    for stem, group in shards.items():
        (man / f"{stem}.jsonl").write_text(registry.manifest_shard_text(group))
    (root / "pruned_urls.txt").write_text("https://e.org/blocked\n")
    return store.FileStore(root)


@pytest.fixture
def legacy(st, tmp_path, monkeypatch):
    """A byte-identical copy of the store's files, driven through the legacy registry.py API."""
    root = tmp_path / "legacy"
    shutil.copytree(st.root, root)
    monkeypatch.setattr(registry, "REG_DIR", root / "registry")
    monkeypatch.setattr(registry, "MAN_DIR", root / "manifest")
    monkeypatch.setattr(blocklist, "PATH", root / "pruned_urls.txt")
    monkeypatch.setenv("NEKAISE_DISABLE_INDEX", "1")
    return root


def files(root):
    return {str(p.relative_to(root)): p.read_bytes()
            for p in sorted(root.rglob("*")) if p.is_file() and "workspace" not in p.parts}


def data_files(root):
    return {k: v for k, v in files(root).items() if not k.startswith("registry/journal/")}


# --- legacy parity ------------------------------------------------------------------------------

def test_insert_entries_matches_legacy_append_bytes(st, legacy):
    new = [entry("oer-c"), entry("arc-old"), entry("hand-three")]
    assert write(st, "r1", lambda tx: tx.insert_entries(new)) == 3
    registry.append_entries(sorted(new, key=lambda e: e["id"]))
    assert data_files(st.root) == files(legacy)
    assert "# inline hand note" in (st.reg / "curated.yaml").read_text()


def test_delete_entries_matches_legacy_remove_ids(st, legacy):
    assert write(st, "r1", lambda tx: tx.delete_entries(["hand-one", "oer-b", "none"],
                                                        reason="prune")) == 2
    registry.remove_ids({"hand-one", "oer-b"})
    assert data_files(st.root) == files(legacy)


def test_replace_manifest_matches_legacy_write_manifest_rows(st, legacy):
    rows = [mrow("hand-one"), mrow("hand-two", status="failed", sha256=None, text_chars=None),
            mrow("zen-new")]
    write(st, "r1", lambda tx: tx.replace_manifest(rows, reason="prune"))
    registry.write_manifest_rows(rows)
    assert data_files(st.root) == files(legacy)
    assert not (st.man / "books.jsonl").exists()


def test_blocklist_and_ledger_match_legacy_appends(st, legacy):
    write(st, "r1", lambda tx: (tx.blocklist_add(["https://b.org/2/", "https://b.org/1"]),
                                tx.ledger_append([{"id": "oer-a", "reason": "junk"}])))
    legacy_blocklist_add(["https://b.org/2/", "https://b.org/1"])
    ops.append_jsonl(registry.prune_ledger_path("oer-a"), {"id": "oer-a", "reason": "junk"})
    assert data_files(st.root) == files(legacy)


def legacy_blocklist_add(urls) -> int:
    """blocklist.add() before it wrote through the store (ADR 0001 stage 3, step 5), verbatim."""
    cur = blocklist.load()
    new = {blocklist.normalize(u) for u in urls if u and blocklist.normalize(u)} - cur
    if new:
        old = blocklist.PATH.read_text() if blocklist.PATH.exists() else ""
        if old and not old.endswith("\n"):
            old += "\n"
        ops.atomic_write_text(blocklist.PATH, old + "".join(f"{u}\n" for u in sorted(new)))
    return len(new)


def test_blocklist_add_writes_through_the_store_with_legacy_bytes(st, legacy, monkeypatch):
    """Standalone blocklist.add is one store transaction (journaled) whose file bytes equal the
    legacy writer's, including a missing final newline; nothing new means no transaction."""
    (st.root / "pruned_urls.txt").write_text("https://e.org/blocked")  # no final newline
    (legacy / "pruned_urls.txt").write_text("https://e.org/blocked")
    urls = ["https://b.org/2/", " https://b.org/1 ", "https://e.org/blocked/", ""]
    assert legacy_blocklist_add(urls) == 2
    monkeypatch.setattr(blocklist, "PATH", st.blocklist_path)
    assert blocklist.add(urls) == 2
    assert data_files(st.root) == data_files(legacy)
    with st.read() as v:
        runs = {e["run_id"] for e in v.scan(Table.EVENTS).rows}
    assert len(runs) == 1 and runs.pop().startswith("blocklist-")
    before = files(st.root)
    assert blocklist.add(["https://b.org/1"]) == 0
    assert files(st.root) == before  # no journal row either


def test_blocklist_add_inside_a_round_goes_through_the_broker(st, monkeypatch):
    import store_broker
    monkeypatch.setattr(blocklist, "PATH", st.blocklist_path)
    with st.writer(round_id="rnd-bl") as w:
        broker = store_broker.Broker(st, w, "rnd-bl")
        with broker.serving():
            for k, v in broker.env().items():
                monkeypatch.setenv(k, v)
            monkeypatch.setenv(store.INHERITED_LOCK_ENV, f"{os.getpid()}:rnd-bl")
            assert blocklist.add(["https://b.org/9"]) == 1
    assert st.blocklist_path.read_text().endswith("https://b.org/9\n")
    monkeypatch.delenv(store.INHERITED_LOCK_ENV)
    with st.read() as v:
        runs = {e["run_id"] for e in v.scan(Table.EVENTS).rows}
    assert len(runs) == 1 and runs.pop().startswith("rnd-bl.blocklist.add-")


# --- normalization: index and canonical paths agree ----------------------------------------------

def test_indexed_and_canonical_known_agree_including_whitespace(st, monkeypatch):
    (st.reg / "curated.yaml").write_text((st.reg / "curated.yaml").read_text()
                                         + registry.emit_entry(entry("ws", url=" https://w.org/x/ ")))
    probe = dict(urls=["https://e.org/dup", "https://e.org/blocked", "https://w.org/x",
                       " https://w.org/x/", "https://absent"],
                 titles=["shared title", "title of oer a", "absent"], ids=["oer-a", "absent"])
    with st.read() as v:
        indexed = v.known(**probe)
    assert (st.workspace / "corpus-index.sqlite3").exists()  # the indexed path really ran
    monkeypatch.setenv("NEKAISE_DISABLE_INDEX", "1")
    with st.read() as v:
        assert v.known(**probe) == indexed
    assert indexed.urls == {"https://e.org/dup", "https://e.org/blocked", "https://w.org/x"}
    # and the legacy canonical existing_keys agrees
    monkeypatch.setattr(registry, "REG_DIR", st.reg)
    monkeypatch.setattr(registry, "MAN_DIR", st.man)
    monkeypatch.setattr(blocklist, "PATH", st.blocklist_path)
    urls, _, _ = registry.existing_keys()
    assert "https://w.org/x" in urls


def test_uncommitted_writes_bypass_the_index(st):
    def body(tx):
        tx.blocklist_add(["https://absent"])
        assert "https://absent" in tx.known(urls=["https://absent"]).urls
    write(st, "r1", body)


# --- crash injection and recovery ---------------------------------------------------------------

def crashing_writes(monkeypatch, fail_at):
    real_write, real_unlink = store._write_durable, store._unlink_durable
    calls = {"n": 0}

    def tick(path):
        if "store-transactions" not in str(path):
            calls["n"] += 1
            if calls["n"] == fail_at:
                raise KeyboardInterrupt("simulated crash")  # BaseException, like a kill

    monkeypatch.setattr(store, "_write_durable", lambda p, d: (tick(p), real_write(p, d)))
    monkeypatch.setattr(store, "_unlink_durable", lambda p: (tick(p), real_unlink(p)))
    monkeypatch.setattr(store.FileStore, "_rollback", lambda self, txn, meta: None)  # "power loss"
    return calls


def big_change(tx):
    tx.insert_entries([entry("oer-new")])
    tx.replace_manifest([mrow("hand-one")], reason="x")  # deletes books.jsonl, rewrites curated
    tx.blocklist_add(["https://x.org/y"])


@pytest.mark.parametrize("fail_at", [1, 2, 3, 4, 5])
def test_crash_at_any_write_recovers_to_the_exact_prior_state(st, monkeypatch, fail_at):
    before = files(st.root)
    crashing_writes(monkeypatch, fail_at)
    with pytest.raises(KeyboardInterrupt):
        write(st, "r1", big_change)
    monkeypatch.undo()
    assert [t.state for t in st.pending_transactions()] == ["prepared"]
    with pytest.raises(store.PendingTransaction):
        with st.read():
            pass
    with st.writer() as w:
        with pytest.raises(store.PendingTransaction):
            with st.transaction("r2", expected_version=st.version(), writer=w):
                pass
        assert st.recover("r1", writer=w).action == "rolled_back"
    assert files(st.root) == before


def test_ordinary_commit_failure_rolls_back_immediately(st, monkeypatch):
    before = files(st.root)
    real = store._write_durable
    calls = {"n": 0}

    def flaky(path, data):
        if "store-transactions" not in str(path):
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("disk full")
        real(path, data)

    monkeypatch.setattr(store, "_write_durable", flaky)
    with pytest.raises(OSError):
        write(st, "r1", big_change)
    assert st.pending_transactions() == [] and files(st.root) == before


def test_crash_after_commit_marker_finalizes(st, monkeypatch):
    real = store._rmtree_durable
    monkeypatch.setattr(store, "_rmtree_durable", lambda p: (_ for _ in ()).throw(
        KeyboardInterrupt()) if "store-transactions" in str(p) else real(p))
    with pytest.raises(KeyboardInterrupt):
        write(st, "r1", big_change)
    monkeypatch.undo()
    after = files(st.root)
    assert [t.state for t in st.pending_transactions()] == ["committed"]
    with st.writer() as w:
        assert st.recover("r1", writer=w).action == "finalized"
    assert files(st.root) == after


def test_recovery_refuses_to_overwrite_newer_work(st, monkeypatch):
    crashing_writes(monkeypatch, 2)
    with pytest.raises(KeyboardInterrupt):
        write(st, "r1", big_change)
    monkeypatch.undo()
    (st.root / "pruned_urls.txt").write_text("someone else's newer write\n")
    with st.writer() as w:
        with pytest.raises(store.StoreError, match="refusing to restore"):
            st.recover("r1", writer=w)


# --- lock semantics and round interlocks ----------------------------------------------------------

def test_read_view_holds_the_round_lock(st):
    with st.read():
        with pytest.raises(RuntimeError, match="held by pid"):
            with st.read(timeout=0):
                pass


def test_legacy_round_snapshot_blocks_views_and_transactions(st):
    snap = st.round_snapshots / "20260924T000000Z-abc"
    snap.mkdir(parents=True)
    (snap / "snapshot.json").write_text("{}")
    with pytest.raises(store.PendingTransaction, match="round snapshot"):
        with st.read():
            pass
    with st.writer() as w:
        with pytest.raises(store.PendingTransaction, match="round snapshot"):
            with st.transaction("r1", expected_version=st.version(), writer=w):
                pass
        with pytest.raises(store.PendingTransaction, match="round snapshot"):
            with st.read(writer=w):  # a fresh lock holder does not own an abandoned round
                pass
    with pytest.raises(store.PendingTransaction, match="must be recovered"):
        with st.writer(round_id="20260924T000000Z-abc"):  # cannot claim an abandoned round
            pass
    shutil.rmtree(snap)
    with st.writer(round_id="20260924T010000Z-new") as w:
        live = st.round_snapshots / "20260924T010000Z-new"  # the round starts under this lock
        live.mkdir(parents=True)
        (live / "snapshot.json").write_text("{}")
        with st.read(writer=w) as v:  # its own writer may read its in-flight state
            v.scan(Table.ENTRIES)


def test_inherited_lock_must_be_verified(st, monkeypatch):
    monkeypatch.setenv(store.INHERITED_LOCK_ENV, json.dumps(
        [{"pid": os.getppid(), "run": "run", "lock": str((st.workspace / ".corpus-round.lock").resolve())}]))
    with pytest.raises(store.WriterError, match="does not name"):
        with st.read():  # a real ancestor, but it does not hold the lock
            pass
    code = (
        "import sys; sys.path.insert(0, 'scripts'); import store\n"
        f"st = store.FileStore({str(st.root)!r})\n"
        "with st.read() as v:\n    print(len(v.scan(store.Table.ENTRIES).rows))\n"
    )
    with st.writer():
        env = ops.with_holder(dict(os.environ), os.getpid(), st.workspace / ".corpus-round.lock",
                              "run")
        out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                             text=True, cwd=store.ROOT)
        assert out.returncode == 0, out.stderr
        assert out.stdout.strip() == "4"


def test_crash_during_rollback_cleanup_is_still_recoverable(st, monkeypatch):
    before = files(st.root)
    crashing_writes(monkeypatch, 2)
    with pytest.raises(KeyboardInterrupt):
        write(st, "r1", big_change)
    monkeypatch.undo()
    real = store._rmtree_durable
    monkeypatch.setattr(store, "_rmtree_durable", lambda p: (_ for _ in ()).throw(
        KeyboardInterrupt()) if "store-transactions" in str(p) else real(p))
    with st.writer() as w:
        with pytest.raises(KeyboardInterrupt):
            st.recover("r1", writer=w)  # restored, then died deleting the backups
    monkeypatch.undo()
    assert [t.state for t in st.pending_transactions()] == ["rolled_back"]
    assert files(st.root) == before
    with st.writer() as w:
        assert st.recover("r1", writer=w).action == "rolled_back"
    assert st.pending_transactions() == []


def test_interrupted_preparation_does_not_strand_the_run(st, monkeypatch):
    before = files(st.root)
    real = store._write_durable
    monkeypatch.setattr(store, "_write_durable", lambda p, d: (_ for _ in ()).throw(
        KeyboardInterrupt()) if p.name.endswith(".bin") else real(p, d))
    monkeypatch.setattr(store, "_rmtree_durable", lambda p: None)  # the crash skips cleanup too
    with pytest.raises(KeyboardInterrupt):
        write(st, "r1", big_change)
    monkeypatch.undo()
    assert (st.txn_dir / "r1").exists() and st.pending_transactions() == []
    assert files(st.root) == before
    write(st, "r1", big_change)  # the same run commits normally afterwards
    assert st.pending_transactions() == [] and not (st.txn_dir / "r1").exists()


def test_recover_discards_an_unmarked_preparation(st):
    (st.txn_dir / "r9" / "state").mkdir(parents=True)
    with st.writer() as w:
        assert st.recover("r9", writer=w).action == "discarded"
    assert not (st.txn_dir / "r9").exists()


@pytest.mark.parametrize("bad", ["", ".", "..", "../x", "/tmp", "a/b", "a..b", " r1", "x" * 200])
def test_invalid_run_ids_never_touch_transaction_storage(st, monkeypatch, bad):
    crashing_writes(monkeypatch, 2)
    with pytest.raises(KeyboardInterrupt):
        write(st, "r1", big_change)
    monkeypatch.undo()
    with st.writer() as w:
        with pytest.raises(store.StoreError, match="invalid run id"):
            st.recover(bad, writer=w)
        with pytest.raises(store.StoreError, match="invalid run id"):
            with st.transaction(bad, expected_version=st.version(), writer=w):
                pass
    assert [t.run_id for t in st.pending_transactions()] == ["r1"]  # evidence preserved
    with pytest.raises(store.StoreError, match="invalid run id"):
        with st.writer(round_id=bad):
            pass


def test_view_expires_when_its_generation_moves(st):
    with st.writer() as w:
        with st.read(writer=w) as v:
            v.scan(Table.ENTRIES)
            with st.transaction("r1", expected_version=st.version(), writer=w) as tx:
                tx.blocklist_add(["https://x.org/new"])
            with pytest.raises(store.StaleView):
                v.scan(Table.BLOCKLIST)  # would lazily load the newer generation


def test_insert_order_within_one_shard_matches_legacy_append(st, legacy):
    new = [entry("oer-zz"), entry("oer-aa"), entry("oer-mm")]  # deliberately unsorted
    write(st, "r1", lambda tx: tx.insert_entries(new))
    registry.append_entries(new)
    assert data_files(st.root) == files(legacy)


def test_lint_sees_a_duplicated_corrupt_manifest_row(st, monkeypatch, capsys):
    import lint_registry
    shard = st.man / f"{registry.manifest_shard('oer-a')}.jsonl"
    bad = json.dumps({**mrow("oer-a", sha256="INVALID")})
    shard.write_text(bad + "\n" + shard.read_text())  # same id twice: corrupt first, valid last
    monkeypatch.setattr(registry, "REG_DIR", st.reg)
    monkeypatch.setattr(registry, "MAN_DIR", st.man)
    assert lint_registry.main(st.root) == 1
    assert "duplicate manifest id (2x): oer-a" in capsys.readouterr().out


def test_membership_fallback_loads_the_corpus_once_per_generation(st, monkeypatch):
    monkeypatch.setenv("NEKAISE_DISABLE_INDEX", "1")
    loads = []
    real = store.FileStore._load
    monkeypatch.setattr(store.FileStore, "_load",
                        lambda self, state, table: (loads.append(table), real(self, state, table)))
    for batch in (["https://e.org/dup"], ["https://nope"], ["https://e.org/blocked"]):
        with st.read() as v:  # a fresh view per lookup, as dedup opens them
            v.known(urls=batch, titles=["x"], ids=["oer-a"])
    assert loads.count("manifest") == 1 and loads.count("entries") == 1
    assert loads.count("blocklist") == 1


def test_a_lock_holder_s_children_can_read_the_store(st):
    # backup_corpus / the maintainer hold the round lock and run clean_corpus --check as a child
    code = (
        "import sys; sys.path.insert(0, 'scripts'); import store\n"
        f"st = store.FileStore({str(st.root)!r})\n"
        "with st.read(timeout=0) as v:\n    print(len(v.scan(store.Table.ENTRIES).rows))\n"
    )
    with ops.named_lock("corpus-round", workspace=st.workspace):
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                             cwd=store.ROOT)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "4"
    assert store.INHERITED_LOCK_ENV not in os.environ  # restored on release


def _second_store(tmp_path):
    root = tmp_path / "second"
    write_config(root)
    other = store.FileStore(root)
    write(other, "seed", lambda tx: tx.insert_entries([entry("hand-z")]))
    return other


READER = ("import sys; sys.path.insert(0, 'scripts'); import store\n"
          "with store.FileStore(sys.argv[1]).read(timeout=0) as v:\n"
          "    print(len(v.scan(store.Table.ENTRIES).rows))\n")


def test_nested_holders_of_different_locks_keep_their_own_inheritance(st, tmp_path):
    other = _second_store(tmp_path)
    middle = ("import subprocess, sys; sys.path.insert(0, 'scripts'); import ops\n"
              f"with ops.named_lock('corpus-round', workspace={str(other.workspace)!r}):\n"
              f"    out = subprocess.run([sys.executable, '-c', {READER!r}, {str(other.root)!r}],\n"
              "                         capture_output=True, text=True)\n"
              "    print(out.stdout.strip() or out.stderr[-400:])\n")
    with ops.named_lock("corpus-round", workspace=st.workspace):   # holder A (this process)
        out = subprocess.run([sys.executable, "-c", middle], capture_output=True, text=True,
                             cwd=store.ROOT)                        # B holds the second lock
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "1", out.stdout                  # C read the second store


def test_threads_holding_different_locks_do_not_remove_each_other(st, tmp_path):
    import threading
    other = _second_store(tmp_path)
    first_done, second_held, finished = threading.Event(), threading.Event(), threading.Event()
    result = {}

    def hold_first():
        with ops.named_lock("corpus-round", workspace=st.workspace):
            second_held.wait(5)
        first_done.set()

    def hold_second():
        with ops.named_lock("corpus-round", workspace=other.workspace):
            second_held.set()
            first_done.wait(5)  # the first holder has released: our entry must survive it
            out = subprocess.run([sys.executable, "-c", READER, str(other.root)],
                                 capture_output=True, text=True, cwd=store.ROOT, timeout=30)
            result["out"] = out
        finished.set()

    threads = [threading.Thread(target=hold_first), threading.Thread(target=hold_second)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(30)
    assert result["out"].returncode == 0, result["out"].stderr
    assert result["out"].stdout.strip() == "1"
    assert store.INHERITED_LOCK_ENV not in os.environ


# --- routed inserts (ADR 0001 stage 3, step 5: the discovery merge must not parse every shard) ---

def test_insert_reads_only_the_routed_shards(st, legacy, monkeypatch):
    loads, files_read = [], []
    real_load, real_files = store.FileStore._load, store.FileStore._load_files
    monkeypatch.setattr(store.FileStore, "_load",
                        lambda self, state, table: (loads.append(table),
                                                    real_load(self, state, table)))
    monkeypatch.setattr(store.FileStore, "_load_files",
                        lambda self, table, paths: (files_read.extend(p.name for p in paths),
                                                    real_files(self, table, paths))[1])
    new = [entry("oer-c"), entry("vnd-x1"), entry("kit-first"), entry("oer-d")]
    assert write(st, "r1", lambda tx: tx.insert_entries(new)) == 4
    assert "entries" not in loads
    assert sorted(set(files_read)) == sorted({"books.yaml", "kitopen.yaml",
                                              registry.shard_filename("vnd-x1")})
    registry.append_entries(new)
    assert data_files(st.root) == files(legacy)


def test_routed_inserts_join_a_later_full_load_in_the_same_transaction(st):
    def body(tx):
        tx.insert_entries([entry("oer-c")])
        tx.upsert_entries([entry("oer-c", title="Changed"), entry("hand-one", title="Also")])
        tx.delete_entries(["oer-a"], reason="test")
        return [e["id"] for e in tx.scan(Table.ENTRIES).rows]
    assert write(st, "r1", body) == ["hand-one", "hand-two", "oer-b", "oer-c"]
    with st.read() as v:
        got = v.get_entries(["oer-c", "hand-one", "oer-a"])
    assert got["oer-c"]["title"] == "Changed" and got["hand-one"]["title"] == "Also"
    assert "oer-a" not in got
    assert "# inline hand note" in (st.reg / "curated.yaml").read_text()


def test_routed_insert_clashes_are_refused(st):
    before = files(st.root)
    with pytest.raises(store.StoreError, match="already exist: oer-a"):
        write(st, "r1", lambda tx: tx.insert_entries([entry("oer-a")]))
    with pytest.raises(store.StoreError, match="already exist: oer-z"):
        write(st, "r2", lambda tx: (tx.insert_entries([entry("oer-z")]),
                                    tx.insert_entries([entry("oer-z")])))
    # an existing id outside its routed shard is caught once the table is loaded
    (st.reg / "curated.yaml").write_text((st.reg / "curated.yaml").read_text()
                                         + registry.emit_entry(entry("oer-misplaced")))
    before = files(st.root)
    with pytest.raises(store.StoreError, match="already exist: oer-misplaced"):
        write(st, "r3", lambda tx: (tx.insert_entries([entry("oer-misplaced")]),
                                    tx.get_entries(["oer-misplaced"])))
    assert files(st.root) == before


# --- journal files -------------------------------------------------------------------------------

def test_journal_is_daily_and_sequences_continue_across_files(st):
    st.journal_dir.mkdir(parents=True)
    (st.journal_dir / "2026-08.jsonl").write_text(json.dumps(  # a legacy monthly file
        {"seq": 41, "event_id": "old:commit", "run_id": "old", "op": "commit", "digest": "d",
         "table": None, "id": None, "at": "2026-08-01T00:00:00Z"}) + "\n")
    write(st, "r-daily", lambda tx: tx.blocklist_add(["https://j.org/1"]))
    with st.read() as v:
        mine = [e for e in v.scan(Table.EVENTS).rows if e["run_id"] == "r-daily"]
    assert [e["seq"] for e in mine] == [42, 43]
    day = mine[0]["at"][:10]
    assert (st.journal_dir / f"{day}.jsonl").exists()
    assert st._commit_digest("r-daily") == mine[-1]["digest"]
    assert st._commit_digest("old") == "d" and st._commit_digest("r-none") is None
    # a retry of the committed run is still recognized as one
    write(st, "r-daily", lambda tx: tx.blocklist_add(["https://j.org/1"]))
    with st.read() as v:
        assert len([e for e in v.scan(Table.EVENTS).rows if e["run_id"] == "r-daily"]) == 2
