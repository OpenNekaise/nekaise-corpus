"""FileStore-specific behavior: byte parity with the legacy registry.py writers, crash injection
and recovery on the real file layout, lock ownership, and interlocks with round snapshots."""
import json
import os
import shutil
import subprocess
import sys

import pytest

import blocklist
import legacy_registry
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
    """A byte-identical copy of the store's files, driven through the legacy registry.py API
    (tests/legacy_registry.py: registry.py's file writers before they became store adapters)."""
    root = tmp_path / "legacy"
    shutil.copytree(st.root, root)
    monkeypatch.setattr(registry, "ROOT", root)  # tests/legacy_registry.py follows it
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
    legacy_registry.append_entries(sorted(new, key=lambda e: e["id"]))
    assert data_files(st.root) == files(legacy)
    assert "# inline hand note" in (st.reg / "curated.yaml").read_text()


def test_delete_entries_matches_legacy_remove_ids(st, legacy):
    assert write(st, "r1", lambda tx: tx.delete_entries(["hand-one", "oer-b", "none"],
                                                        reason="prune")) == 2
    legacy_registry.remove_ids({"hand-one", "oer-b"})
    assert data_files(st.root) == files(legacy)


def test_replace_manifest_matches_legacy_write_manifest_rows(st, legacy):
    rows = [mrow("hand-one"), mrow("hand-two", status="failed", sha256=None, text_chars=None),
            mrow("zen-new")]
    write(st, "r1", lambda tx: tx.replace_manifest(rows, reason="prune"))
    legacy_registry.write_manifest_rows(rows)
    assert data_files(st.root) == files(legacy)
    assert not (st.man / "books.jsonl").exists()


def test_blocklist_and_ledger_match_legacy_appends(st, legacy):
    write(st, "r1", lambda tx: (tx.blocklist_add(["https://b.org/2/", "https://b.org/1"]),
                                tx.ledger_append([{"id": "oer-a", "reason": "junk"}])))
    legacy_blocklist_add(["https://b.org/2/", "https://b.org/1"])
    ops.append_jsonl(legacy_registry.prune_ledger_path("oer-a"), {"id": "oer-a", "reason": "junk"})
    assert data_files(st.root) == files(legacy)


def legacy_blocklist_add(urls) -> int:
    """blocklist.add() before it wrote through the store (ADR 0001 stage 3, step 5), verbatim."""
    cur = legacy_registry.blocklist_load()
    new = {blocklist.normalize(u) for u in urls if u and blocklist.normalize(u)} - cur
    if new:
        path = legacy_registry.blocklist_path()
        old = path.read_text() if path.exists() else ""
        if old and not old.endswith("\n"):
            old += "\n"
        ops.atomic_write_text(path, old + "".join(f"{u}\n" for u in sorted(new)))
    return len(new)


def test_blocklist_add_writes_through_the_store_with_legacy_bytes(st, legacy, monkeypatch):
    """Standalone blocklist.add is one store transaction (journaled) whose file bytes equal the
    legacy writer's, including a missing final newline; nothing new means no transaction."""
    (st.root / "pruned_urls.txt").write_text("https://e.org/blocked")  # no final newline
    (legacy / "pruned_urls.txt").write_text("https://e.org/blocked")
    urls = ["https://b.org/2/", " https://b.org/1 ", "https://e.org/blocked/", ""]
    assert legacy_blocklist_add(urls) == 2
    monkeypatch.setattr(blocklist, "ROOT", st.root)
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
    code = ("import sys; sys.path.insert(0, 'scripts'); import blocklist\n"
            f"from pathlib import Path; blocklist.ROOT = Path({str(st.root)!r})\n"
            "print(blocklist.add(['https://b.org/9']))\n")
    with st.writer(round_id="rnd-bl") as w:
        broker = store_broker.Broker(st, w, "rnd-bl")
        with broker.serving():  # the prune step is a child of the round: it writes via the broker
            env = ops.with_holder(dict(os.environ, **broker.env()), os.getpid(),
                                  st.workspace / ".corpus-round.lock", "rnd-bl")
            out = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                                 text=True, cwd=store.ROOT)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "1"
    assert st.blocklist_path.read_text().endswith("https://b.org/9\n")
    with st.read() as v:
        runs = {e["run_id"] for e in v.scan(Table.EVENTS).rows}
    assert len(runs) == 1 and runs.pop().startswith("rnd-bl.blocklist.blocklist-")


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
    # and the legacy canonical existing_keys agrees, and so does its deprecated store adapter
    monkeypatch.setattr(registry, "ROOT", st.root)
    urls, _, _ = legacy_registry.existing_keys()
    assert "https://w.org/x" in urls
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
    legacy_registry.append_entries(new)
    assert data_files(st.root) == files(legacy)


def test_lint_sees_a_duplicated_corrupt_manifest_row(st, monkeypatch, capsys):
    import lint_registry
    shard = st.man / f"{registry.manifest_shard('oer-a')}.jsonl"
    bad = json.dumps({**mrow("oer-a", sha256="INVALID")})
    shard.write_text(bad + "\n" + shard.read_text())  # same id twice: corrupt first, valid last
    monkeypatch.setattr(registry, "ROOT", st.root)  # its eligibility policy
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
    real_load = store.FileStore._load
    monkeypatch.setattr(store.FileStore, "_load",
                        lambda self, state, table: (loads.append(table),
                                                    real_load(self, state, table)))
    real_shard = store._RegistryShard.__init__
    monkeypatch.setattr(store._RegistryShard, "__init__",
                        lambda self, path, name: (files_read.append(path.name),
                                                  real_shard(self, path, name))[1])
    new = [entry("oer-c"), entry("vnd-x1"), entry("kit-first"), entry("oer-d")]
    assert write(st, "r1", lambda tx: tx.insert_entries(new)) == 4
    assert "entries" not in loads
    assert sorted(set(files_read)) == sorted({"books.yaml", "kitopen.yaml",
                                              registry.shard_filename("vnd-x1")})
    legacy_registry.append_entries(new)
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
    assert [e["seq"] for e in mine] == [42]  # a v2 receipt only
    day = mine[0]["at"][:10]
    assert (st.journal_dir / f"{day}.jsonl").exists()
    assert st._commit_digest("r-daily") == mine[-1]["digest"]
    assert st._commit_digest("old") == "d" and st._commit_digest("r-none") is None
    # a retry of the committed run is still recognized as one
    write(st, "r-daily", lambda tx: tx.blocklist_add(["https://j.org/1"]))
    with st.read() as v:
        assert len([e for e in v.scan(Table.EVENTS).rows if e["run_id"] == "r-daily"]) == 1


# --- routed manifest mutations (stage 3, step 6: a loader checkpoint must not parse 2 GB) --------

def _manifest_reads(monkeypatch):
    """Record every manifest shard file the store reads, and every whole-table load."""
    read, loads = [], []
    real_shard, real_load = store.FileStore._manifest_shard, store.FileStore._load
    monkeypatch.setattr(store.FileStore, "_manifest_shard",
                        lambda self, stem: (read.append(stem), real_shard(self, stem))[1])
    monkeypatch.setattr(store.FileStore, "_load",
                        lambda self, state, table: (loads.append(table),
                                                    real_load(self, state, table)))
    return read, loads


def test_manifest_mutations_read_only_the_routed_shards(st, legacy, monkeypatch):
    read, loads = _manifest_reads(monkeypatch)
    changed = mrow("oer-a", sha256="new", error="drift")

    def body(tx):
        tx.upsert_manifest([changed, mrow("vnd-x1"), mrow("kit-first")])
        tx.update_manifest_fields({"hand-one": {"corpus_path": "corpus/hand-one.md",
                                                "corpus_chars": 7}})
        tx.delete_manifest(["oer-b", "zen-missing"], reason="prune: dup-bytes")
        return tx.get_manifest(["oer-a", "oer-b", "vnd-x1"])  # read-your-writes, still routed
    got = write(st, "r1", body)
    assert "manifest" not in loads
    assert sorted(set(read)) == sorted({"books", "curated", "kitopen", "zenodo",
                                        registry.manifest_shard("vnd-x1")})
    assert got["oer-a"]["error"] == "drift" and "oer-b" not in got and "vnd-x1" in got
    # the same change through the legacy whole-manifest writer: identical bytes
    rows = {r["id"]: r for r in legacy_registry.load_manifest_rows()}
    rows["oer-a"] = changed
    rows["vnd-x1"], rows["kit-first"] = mrow("vnd-x1"), mrow("kit-first")
    rows["hand-one"] = {**rows["hand-one"], "corpus_path": "corpus/hand-one.md", "corpus_chars": 7}
    del rows["oer-b"]
    legacy_registry.write_manifest_rows(rows.values())
    assert data_files(st.root) == files(legacy)
    with st.read() as v:  # the tombstone carries the reason and the deleted row's digest
        events = [e for e in v.scan(Table.EVENTS).rows if e["run_id"] == "r1"]
    [delete] = [e for e in events if e["op"] == "delete"]
    assert delete["reason"] == "prune: dup-bytes" and delete["id"] == "oer-b"
    assert delete["before_sha256"] == store.hashlib.sha256(
        store.canonical_row(mrow("oer-b", sha256="same")).encode()).hexdigest()
    assert events[-1]["counts"] == {"manifest": {"upsert": 3, "update": 1, "delete": 1}}


def test_routed_manifest_changes_join_a_later_full_load(st):
    def body(tx):
        tx.upsert_manifest([mrow("oer-c")])
        tx.delete_manifest(["oer-a"], reason="x")
        tx.update_manifest_fields({"oer-b": {"error": "e"}})
        return {r["id"]: r.get("error") for r in tx.scan(Table.MANIFEST).rows}
    got = write(st, "r1", body)
    assert got == {"hand-one": None, "hand-two": None, "oer-b": "e", "oer-c": None}
    with st.read() as v:
        assert sorted(r["id"] for r in v.scan(Table.MANIFEST).rows) == sorted(got)


def test_a_shard_emptied_by_routed_deletes_is_removed(st, legacy):
    write(st, "r1", lambda tx: tx.delete_manifest(["oer-a", "oer-b"], reason="prune"))
    assert not (st.man / "books.jsonl").exists()
    rows = [r for r in legacy_registry.load_manifest_rows() if r["id"] not in ("oer-a", "oer-b")]
    legacy_registry.write_manifest_rows(rows)
    assert data_files(st.root) == files(legacy)


@pytest.mark.parametrize("n_rows,n_changes", [(200, 1), (200, 5), (2000, 40), (200, 40), (3, 3)])
def test_spliced_shards_equal_a_full_rerender(tmp_path, n_rows, n_changes):
    """The splice (few changes) and the full re-render (many) give the legacy bytes: rows keep
    (topic, id) order whatever their topic moves to, deletions and insertions included."""
    import random
    rng = random.Random(n_rows * 1000 + n_changes)
    topics = ["a_topic", "building_energy", "hvac", "zz"]
    rows = {f"oer-{n:04d}": mrow(f"oer-{n:04d}", topic=rng.choice(topics),
                                 title=f'T {n} é漢 "q"')
            for n in range(n_rows)}
    root = tmp_path / "r"
    write_config(root)
    (root / "manifest").mkdir()
    (root / "manifest" / "books.jsonl").write_text(registry.manifest_shard_text(rows.values()))
    st2 = store.FileStore(root)
    changes = {}
    for sid in rng.sample(sorted(rows), n_changes):
        kind = rng.choice(["move", "delete", "edit"])
        if kind == "delete":
            changes[sid] = None
        elif kind == "move":
            changes[sid] = {**rows[sid], "topic": rng.choice(topics)}
        else:
            changes[sid] = {**rows[sid], "error": "edited"}
    for n in range(max(1, n_changes // 2)):
        changes[f"oer-new-{n}"] = mrow(f"oer-new-{n}", topic=rng.choice(topics))

    def body(tx):
        tx.delete_manifest([s for s, r in changes.items() if r is None], reason="x")
        tx.upsert_manifest([r for r in changes.values() if r is not None])
    write(st2, "r1", body)
    expect = dict(rows)
    for sid, r in changes.items():
        if r is None:
            expect.pop(sid, None)
        else:
            expect[sid] = r
    assert (root / "manifest" / "books.jsonl").read_text() == \
        registry.manifest_shard_text(expect.values())


def test_a_non_canonical_shard_fails_closed_and_is_linted(st):
    path = st.man / "books.jsonl"
    path.write_text(registry.manifest_shard_text(
        [mrow("oer-a"), mrow("oer-b")] + [mrow(f"oer-z{n:03d}") for n in range(100)]))
    lines = path.read_text().splitlines(keepends=True)
    path.write_text("".join(reversed(lines)))  # out of (topic, id) order
    errors, _ = st.validate_layout()
    assert any("out of (topic, id) order" in e for e in errors)
    path.write_text("".join(lines).replace('"id": "oer-a"', '"id":"oer-a"'))
    errors, _ = st.validate_layout()
    assert any("oer-a: not canonical JSON" in e for e in errors)
    # the splice cannot find the non-canonical row, so re-inserting its id is refused
    with pytest.raises(store.StoreError, match="non-canonical"):
        write(st, "r1", lambda tx: tx.upsert_manifest([mrow("oer-a", error="x")]))


def test_a_stray_manifest_row_is_linted(st):
    (st.man / "books.jsonl").write_text((st.man / "books.jsonl").read_text()
                                        + json.dumps(mrow("zen-stray")) + "\n")
    errors, _ = st.validate_layout()
    assert any("zen-stray belongs in zenodo.jsonl" in e for e in errors)


def test_read_views_answer_keyed_manifest_reads_from_routed_shards(st, monkeypatch):
    read, loads = _manifest_reads(monkeypatch)
    with st.read() as v:
        assert set(v.get_manifest(["oer-a", "hand-one", "zzz-none"])) == {"oer-a", "hand-one"}
        assert v.resolve_artifact("oer-a", store.Stage.TEXT).locator == "file:text/oer-a.md"
    assert "manifest" not in loads and set(read) <= {"books", "curated"}


# --- journal size rolling and the commit index ---------------------------------------------------

def test_the_journal_rolls_by_size_and_keeps_sequence_and_replay(st, monkeypatch):
    monkeypatch.setattr(store, "JOURNAL_ROLL_BYTES", 400)
    for n in range(4):
        write(st, f"r{n}", lambda tx, n=n: tx.upsert_manifest(
            [mrow(f"oer-j{n}-{k}", title="x" * 300) for k in range(3)]))
    paths = sorted(st.journal_dir.glob("*.jsonl"))
    assert len(paths) > 2
    for p in paths:  # a file only exceeds the limit when a single event does
        assert p.stat().st_size <= 400 or len(p.read_text().splitlines()) == 1
    day = paths[0].name[:10]
    assert {f"{day}.jsonl", f"{day}.001.jsonl", f"{day}.002.jsonl"} <= {p.name for p in paths}
    with st.read() as v:
        seqs = [e["seq"] for e in v.scan(Table.EVENTS).rows]
    assert seqs == list(range(1, len(seqs) + 1))
    for n in range(4):  # every commit is still found, so identical retries stay no-ops
        assert st._commit_digest(f"r{n}") is not None
    before = files(st.root)
    write(st, "r2", lambda tx: tx.upsert_manifest(
        [mrow(f"oer-j2-{k}", title="x" * 300) for k in range(3)]))
    assert files(st.root) == before


def test_the_commit_index_rereads_only_changed_journal_files(st, monkeypatch):
    write(st, "a", lambda tx: tx.blocklist_add(["https://j.org/a"]))
    assert st._commit_digest("a")
    opened = []
    real_open = store.Path.open
    monkeypatch.setattr(store.Path, "open", lambda self, *a, **k: (
        opened.append(self.name), real_open(self, *a, **k))[1])
    assert st._commit_digest("a") and st._commit_digest("zzz") is None
    assert not [n for n in opened if n.endswith(".jsonl")]  # cached: nothing re-parsed


def test_entry_deletes_read_only_the_routed_shards(st, legacy, monkeypatch):
    loads, files_read = [], []
    real_load = store.FileStore._load
    monkeypatch.setattr(store.FileStore, "_load",
                        lambda self, state, table: (loads.append(table),
                                                    real_load(self, state, table)))
    real_shard = store._RegistryShard.__init__
    monkeypatch.setattr(store._RegistryShard, "__init__",
                        lambda self, path, name: (files_read.append(path.name),
                                                  real_shard(self, path, name))[1])

    def body(tx):
        n = tx.delete_entries(["oer-a", "hand-two", "kit-none"], reason="prune: failed")
        tx.insert_entries([entry("oer-c"), entry("oer-a", title="Back again")])
        return n
    assert write(st, "r1", body) == 2
    assert "entries" not in loads
    assert sorted(set(files_read)) == ["books.yaml", "curated.yaml", "kitopen.yaml"]
    legacy_registry.remove_ids({"oer-a", "hand-two"})
    legacy_registry.append_entries([entry("oer-c"), entry("oer-a", title="Back again")])
    assert data_files(st.root) == files(legacy)
    assert "# inline hand note" in (st.reg / "curated.yaml").read_text()


@pytest.mark.parametrize("drop", [{"oer-multi"}, {"oer-after"}, {"oer-multi", "oer-after"}])
def test_routed_entry_deletes_cut_multiline_entries_like_remove_ids(st, legacy, drop):
    """A quoted title with blank lines inside spans several lines (the OSTI case remove_ids
    handles); the routed cut must find exactly the same entry blocks."""
    multi = entry("oer-multi", title="First line\nafter a blank line: " + "long words " * 12)
    for root in (st.root, legacy):
        path = root / "registry" / "books.yaml"
        path.write_text(path.read_text() + registry.emit_entry(multi)
                        + registry.emit_entry(entry("oer-after")))
    assert "\n\n" in (st.reg / "books.yaml").read_text().split("oer-multi", 1)[1]
    got = write(st, "r1", lambda tx: (tx.get_manifest([]),
                                      tx.delete_entries(sorted(drop), reason="prune"))[1])
    assert got == len(drop)
    legacy_registry.remove_ids(drop)
    assert data_files(st.root) == files(legacy)
    with st.read() as v:
        tombs = [e for e in v.scan(Table.EVENTS).rows if e["op"] == "delete"]
    assert sorted(e["id"] for e in tombs) == sorted(drop)
    for e in tombs:  # the digest of exactly the entry that was cut (its parsed block)
        row = multi if e["id"] == "oer-multi" else entry("oer-after")
        assert e["before_sha256"] == store.hashlib.sha256(
            store.canonical_row(row).encode()).hexdigest()


def test_version_1_events_stay_valid_next_to_version_2(st):
    """Journals already committed carry v1 events (whole before/after rows, no "v"); they are
    read back unchanged, their commits still identify replays, and new transactions append v2
    receipts and tombstones with continuing sequence numbers."""
    st.journal_dir.mkdir(parents=True)
    v1 = [{"seq": 1, "event_id": "old:1", "run_id": "old", "at": "2026-09-01T00:00:00Z",
           "table": "manifest", "op": "upsert", "id": "oer-a", "before": None,
           "after": mrow("oer-a"), "reason": None},
          {"seq": 2, "event_id": "old:commit", "run_id": "old", "at": "2026-09-01T00:00:00Z",
           "table": None, "op": "commit", "id": None, "digest": "d1"}]
    (st.journal_dir / "2026-09-01.jsonl").write_text(
        "".join(json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n" for e in v1))
    write(st, "new", lambda tx: tx.delete_manifest(["oer-a"], reason="prune: thin"))
    with st.read() as v:
        events = v.scan(Table.EVENTS).rows
    assert events[:2] == v1
    assert [(e["seq"], e["op"], e.get("v")) for e in events[2:]] == [
        (3, "delete", 2), (4, "commit", 2)]
    assert st._commit_digest("old") == "d1"
