"""pg_shadow: import / sync / verify a PostgreSQL shadow from git commits (ADR 0001 stage 2).
Opt-in like the PostgreSQL contract tests: runs only when NEKAISE_PG_TEST_DSN is set."""
import json
import os
import subprocess
import uuid

import pytest

import registry

pytestmark = pytest.mark.skipif(not os.environ.get("NEKAISE_PG_TEST_DSN"),
                                reason="NEKAISE_PG_TEST_DSN not set")


def entry(sid, **extra):
    return {"id": sid, "title": f"Title {sid}", "url": f"https://e.org/{sid}.pdf", "source": "t",
            "license": "public-domain", "topic": "building_energy", "format": "pdf", **extra}


def mrow(sid, **extra):
    return {**entry(sid), "status": "ok", "sha256": f"h-{sid}", "bytes": 1, "text_chars": 10,
            "quality": {"alpha": 0.75025, "big": 1e20, "tiny": 1e-07}, **extra}


class Repo:
    def __init__(self, path):
        self.path = path
        path.mkdir()
        self.git("init", "-q", "-b", "main")
        self.git("config", "user.email", "t@t")
        self.git("config", "user.name", "t")

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.path), *args], check=True, text=True,
                              capture_output=True).stdout.strip()

    def write(self, rel, text):
        p = self.path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text)

    def rm(self, rel):
        (self.path / rel).unlink()

    def commit(self, msg):
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg)
        return self.git("rev-parse", "HEAD")


def yaml_shard(stem, rows):
    return registry.shard_header(stem) + "".join(registry.emit_entry(r) for r in rows)


def manifest(rows):
    return registry.manifest_shard_text(rows)


@pytest.fixture
def env(tmp_path, monkeypatch):
    import pg_shadow
    import store_pg
    repo = Repo(tmp_path / "repo")
    repo.write("registry/backends.json", json.dumps({"find_x": {"script": "x.py", "args": []}}))
    repo.write("registry/eligibility.json", json.dumps({"version": 1, "restrictions": {}}))
    repo.write("registry/rotation.json", json.dumps({"find_x": {"flag": "--page", "next": 1}}))
    repo.write("registry/curated.yaml", "# hand\nsources:\n" + registry.emit_entry(entry("hand-1")))
    repo.write("registry/books.yaml", yaml_shard("books", [entry("oer-a"), entry("oer-b")]))
    repo.write("manifest/curated.jsonl", manifest([mrow("hand-1")]))
    repo.write("manifest/books.jsonl", manifest([mrow("oer-a"), mrow("oer-b")]))
    repo.write("pruned_urls.txt", "https://e.org/old\n")
    repo.write("registry/pruned-3.jsonl", json.dumps({"id": "oer-z", "reason": "junk"}) + "\n")
    c1 = repo.commit("c1")
    st = store_pg.PgStore(repo.path, dsn=os.environ["NEKAISE_PG_TEST_DSN"],
                          schema=f"s_{uuid.uuid4().hex[:12]}")
    yield pg_shadow, st, repo, c1
    st.drop()


def test_import_sync_verify_roundtrip(env):
    pg_shadow, st, repo, c1 = env
    pg_shadow.do_import(st, c1, repo.path, log=lambda *_: None)
    assert pg_shadow.do_verify(st, repo.path, log=lambda *_: None)

    # c2: add, delete, move a row between shards, ledger + blocklist appends, rotation, config
    repo.write("registry/books.yaml", yaml_shard("books", [entry("oer-a"), entry("oer-c")]))
    repo.write("registry/curated.yaml", "# hand\nsources:\n" + registry.emit_entry(entry("hand-1"))
               + registry.emit_entry(entry("oer-b")))  # oer-b moved shards
    repo.write("manifest/books.jsonl", manifest([mrow("oer-a", topic="t2"), mrow("oer-c")]))
    repo.write("manifest/curated.jsonl", manifest([mrow("hand-1"), mrow("oer-b")]))
    repo.write("pruned_urls.txt", "https://e.org/old\nhttps://e.org/new\n")
    repo.write("registry/pruned-3.jsonl", json.dumps({"id": "oer-z", "reason": "junk"}) + "\n"
               + json.dumps({"id": "oer-z", "reason": "junk"}) + "\n")  # duplicate ledger row
    repo.write("registry/rotation.json", json.dumps({"find_x": {"flag": "--page", "next": 2}}))
    repo.write("registry/vendors.json", json.dumps({"acme": {}}))
    repo.write("registry/journal/2026-09.jsonl", json.dumps(
        {"seq": 1, "run_id": "r", "op": "commit", "digest": "d", "table": None, "id": None}) + "\n")
    repo.commit("c2")
    # c3: a shard disappears entirely, and a +0 commit touching nothing tracked
    repo.rm("registry/books.yaml")
    repo.write("manifest/books.jsonl", "")
    repo.rm("manifest/books.jsonl")
    repo.commit("c3")
    repo.write("README.md", "x")
    head = repo.commit("c4")

    assert pg_shadow.do_sync(st, head, repo.path, log=lambda *_: None) == 3
    assert pg_shadow.do_verify(st, repo.path, log=lambda *_: None)
    assert pg_shadow.pg_digests(st)[0] == head
    assert pg_shadow.do_sync(st, head, repo.path, log=lambda *_: None) == 0  # idempotent
    with st.read() as v:  # exact values survive (jsonb would turn 1e20 into an integer)
        q = v.get_manifest(["oer-b"])["oer-b"]["quality"]
        assert q == {"alpha": 0.75025, "big": 1e20, "tiny": 1e-07} and isinstance(q["big"], float)
        assert "vendors.json" in v.config_get().documents


def test_verify_detects_divergence(env):
    pg_shadow, st, repo, c1 = env
    pg_shadow.do_import(st, c1, repo.path, log=lambda *_: None)
    with st._connect(autocommit=True) as conn:
        conn.execute("UPDATE manifest SET row_text = replace(row_text, 'h-oer-a', 'tampered')")
    assert not pg_shadow.do_verify(st, repo.path, log=lambda *_: None)


def test_import_refuses_a_non_empty_schema_and_sync_refuses_rewritten_history(env):
    pg_shadow, st, repo, c1 = env
    pg_shadow.do_import(st, c1, repo.path, log=lambda *_: None)
    with pytest.raises(SystemExit, match="not empty"):
        pg_shadow.do_import(st, c1, repo.path, log=lambda *_: None)
    repo.write("pruned_urls.txt", "https://e.org/rewritten\n")
    repo.git("add", "-A")
    repo.git("commit", "-q", "--amend", "-m", "rewritten c1")
    with pytest.raises(SystemExit, match="re-import required"):
        pg_shadow.do_sync(st, "HEAD", repo.path, log=lambda *_: None)


def test_representation_only_changes_replicate(env):
    pg_shadow, st, repo, c1 = env
    pg_shadow.do_import(st, c1, repo.path, log=lambda *_: None)
    repo.write("manifest/books.jsonl", manifest([mrow("oer-a", bytes=1.0), mrow("oer-b", bytes=True)]))
    head = repo.commit("1 -> 1.0 and 1 -> true")
    pg_shadow.do_sync(st, head, repo.path, log=lambda *_: None)
    assert pg_shadow.do_verify(st, repo.path, log=lambda *_: None)
    with st.read() as v:
        got = v.get_manifest(["oer-a", "oer-b"])
    assert repr(got["oer-a"]["bytes"]) == "1.0" and got["oer-b"]["bytes"] is True


def test_journal_rename_replays_and_rewrite_is_rejected(env):
    pg_shadow, st, repo, c1 = env
    ev = lambda seq, d="d": json.dumps({"seq": seq, "run_id": f"r{seq}", "op": "commit",  # noqa: E731
                                        "digest": d, "table": None, "id": None}) + "\n"
    repo.write("registry/journal/2026-09.jsonl", ev(1))
    c2 = repo.commit("journal")
    pg_shadow.do_import(st, c2, repo.path, log=lambda *_: None)
    repo.rm("registry/journal/2026-09.jsonl")
    repo.write("registry/journal/2026-10.jsonl", ev(1) + ev(2))  # renamed + appended
    c3 = repo.commit("rename")
    assert pg_shadow.do_sync(st, c3, repo.path, log=lambda *_: None) == 1
    assert pg_shadow.do_verify(st, repo.path, log=lambda *_: None)
    repo.write("registry/journal/2026-10.jsonl", ev(1, "changed") + ev(2))
    c4 = repo.commit("rewrite")
    with pytest.raises(SystemExit, match="append-only"):
        pg_shadow.do_sync(st, c4, repo.path, log=lambda *_: None)
    assert pg_shadow.pg_digests(st)[0] == c3  # watermark did not move


def test_legacy_ledger_monolith_takes_precedence(env):
    pg_shadow, st, repo, c1 = env
    repo.write("registry/pruned.jsonl", json.dumps({"id": "legacy", "reason": "old"}) + "\n")
    c2 = repo.commit("legacy ledger present alongside shards")
    pg_shadow.do_import(st, c2, repo.path, log=lambda *_: None)
    assert pg_shadow.do_verify(st, repo.path, log=lambda *_: None)
    with st.read() as v:
        assert [r["id"] for r in v.scan("ledger").rows] == ["legacy"]
    repo.rm("registry/pruned.jsonl")
    c3 = repo.commit("migrated to shards")
    pg_shadow.do_sync(st, c3, repo.path, log=lambda *_: None)
    assert pg_shadow.do_verify(st, repo.path, log=lambda *_: None)
    with st.read() as v:
        assert [r["id"] for r in v.scan("ledger").rows] == ["oer-z"]


def test_verify_hashes_the_pinned_config_documents_themselves(env):
    pg_shadow, st, repo, c1 = env
    pg_shadow.do_import(st, c1, repo.path, log=lambda *_: None)
    with st._connect(autocommit=True) as conn:  # corrupt the document, keep its stored digest
        conn.execute("UPDATE config SET doc_text = '{\"version\": 1, \"restrictions\": {\"x\": 1}}' "
                     "WHERE name = 'eligibility.json'")
    assert not pg_shadow.do_verify(st, repo.path, log=lambda *_: None)


def test_import_refuses_any_prior_operational_state(env):
    pg_shadow, st, repo, c1 = env
    with st.writer() as w:  # an earlier store transaction that only touched the blocklist
        with st.transaction("r0", expected_version=st.version(), writer=w) as tx:
            tx.blocklist_add(["https://stale.example"])
    with pytest.raises(SystemExit, match="not empty"):
        pg_shadow.do_import(st, c1, repo.path, log=lambda *_: None)


def test_enable_requires_committed_state_under_the_round_lock(env):
    pg_shadow, st, repo, c1 = env
    repo.write("pruned_urls.txt", "https://uncommitted\n")
    with pytest.raises(SystemExit, match="uncommitted tracked change"):
        pg_shadow.enable("dsn", "s", repo.path)
    assert not (repo.path / "workspace" / ".pg-shadow").exists()
    repo.commit("commit it")
    pg_shadow.enable("dsn", "s", repo.path)
    assert (repo.path / "workspace" / ".pg-shadow").read_text() == "dsn\ns\n"


def test_import_refuses_any_prior_operational_state(env):
    pg_shadow, st, repo, c1 = env
    with st.writer() as w:  # an earlier store transaction that only touched the blocklist
        with st.transaction("r0", expected_version=st.version(), writer=w) as tx:
            tx.blocklist_add(["https://stale.example"])
    with pytest.raises(SystemExit, match="not empty"):
        pg_shadow.do_import(st, c1, repo.path, log=lambda *_: None)


def test_enable_requires_committed_state_under_the_round_lock(env):
    pg_shadow, st, repo, c1 = env
    repo.write("pruned_urls.txt", "https://uncommitted\n")
    with pytest.raises(SystemExit, match="uncommitted tracked change"):
        pg_shadow.enable("dsn", "s", repo.path)
    assert not (repo.path / "workspace" / ".pg-shadow").exists()
    repo.commit("commit it")
    pg_shadow.enable("dsn", "s", repo.path)
    assert (repo.path / "workspace" / ".pg-shadow").read_text() == "dsn\ns\n"
