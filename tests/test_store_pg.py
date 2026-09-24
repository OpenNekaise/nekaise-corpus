"""PostgreSQL-only store behavior: cross-backend byte-identical exports, and session loss."""
import os
import uuid

import pytest

import store
from test_store_contract import entry, file_store, mrow, seed, write

pytestmark = pytest.mark.skipif(not os.environ.get("NEKAISE_PG_TEST_DSN"),
                                reason="NEKAISE_PG_TEST_DSN not set")


@pytest.fixture
def pg(tmp_path):
    import store_pg
    from test_store_contract import write_config
    write_config(tmp_path / "pg")
    st = store_pg.PgStore(tmp_path / "pg", dsn=os.environ["NEKAISE_PG_TEST_DSN"],
                          schema=f"t_{uuid.uuid4().hex[:12]}")
    st.pin_config_from_files()
    yield st
    st.drop()


def script(tx):
    seed(tx)
    tx.update_manifest_fields({"oer-a": {"corpus_path": "corpus/oer-a.md", "text_chars": 1e20}})
    tx.delete_manifest(["oer-b"], reason="dup-bytes")
    tx.ledger_append([{"id": "oer-b", "reason": "dup-bytes"}, {"id": "oer-b", "reason": "dup-bytes"}])
    tx.backend_state_set("find_books", store.BackendState(False, "exhausted: x"))
    tx.upsert_entries([entry("oer-a", title="Renamed")])


def test_file_and_pg_exports_are_byte_identical(pg, tmp_path, monkeypatch):
    import store_pg
    fixed = lambda fmt, t=None: "2026-09-24T00:00:00Z"  # noqa: E731  events carry a timestamp
    monkeypatch.setattr(store.time, "strftime", fixed)
    monkeypatch.setattr(store_pg.time, "strftime", fixed)
    fs = file_store(tmp_path / "fs")
    for st in (fs, pg):
        write(st, "r1", script)
    with fs.read() as a, pg.read() as b:
        ea = fs.export(tmp_path / "ea", view=a)
        eb = pg.export(tmp_path / "eb", view=b)
    assert ea.files == eb.files
    for name in list(ea.files) + ["EXPORT.json"]:
        assert (tmp_path / "ea" / name).read_bytes() == (tmp_path / "eb" / name).read_bytes(), name


def test_a_killed_session_rolls_back_and_the_store_recovers(pg):
    write(pg, "seed", seed)
    with pytest.raises(Exception):
        with pg.writer() as w:
            with pg.transaction("r1", expected_version=pg.version(), writer=w) as tx:
                tx.insert_entries([entry("oer-ghost")])
                pid = tx._conn.info.backend_pid
                with pg._connect(autocommit=True) as admin:
                    admin.execute("SELECT pg_terminate_backend(%s)", [pid])
                tx.blocklist_add(["https://after.kill"])
    with pg.read() as v:
        assert v.known(ids=["oer-ghost"]).ids == frozenset()
    write(pg, "r1", lambda tx: tx.insert_entries([entry("oer-ghost")]))  # same run id, now commits
    with pg.read() as v:
        assert v.known(ids=["oer-ghost"]).ids == {"oer-ghost"}


def test_a_released_writer_token_is_rejected(pg):
    with pg.writer() as w:
        pass
    with pytest.raises(store.WriterError, match="stale"):
        with pg.transaction("r1", expected_version=pg.version(), writer=w):
            pass
