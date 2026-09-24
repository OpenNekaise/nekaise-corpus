"""Host authority record and backend selection (ADR 0001 stage 4, step 1).

The record (scripts/store_authority.py) decides which store is authoritative for a data root;
store.open() and FileStore consult it, entrypoints that are still the legacy file path refuse any
other authority, and nothing ever falls back. conftest gives every test a private, empty record.
"""
import json
import sys

import pytest

import store
import store_authority
from store import AuthorityError, StoreError
from test_store_contract import write_config

UUID = "11111111-2222-3333-4444-555555555555"
DSN = "host=/nonexistent/socket dbname=nekaise_x"


def postgres(root, epoch=None):
    return store_authority.write_record(root, "postgres", reason="test cutover",
                                        dataset_uuid=UUID, dsn=DSN, schema="nk", epoch=epoch)


def env(backend=None, dsn=None, schema=None):
    out = {}
    if backend:
        out["NEKAISE_STORE"] = backend
    if dsn:
        out["NEKAISE_PG_DSN"] = dsn
    if schema:
        out["NEKAISE_PG_SCHEMA"] = schema
    return out


# --- the selection matrix: record x environment ------------------------------------------------

NONE, FILE, PG = "none", "file", "postgres"
MATRIX = [
    # (record, environment, expected: "file" | ("postgres", dsn, schema) | error class)
    (NONE, env(), "file"),
    (NONE, env("file"), "file"),
    (NONE, env("postgres"), StoreError),                                  # needs a DSN
    (NONE, env("postgres", DSN), ("postgres", DSN, "nekaise")),           # today's default schema
    (NONE, env("postgres", DSN, "s1"), ("postgres", DSN, "s1")),
    (NONE, env("nope"), StoreError),
    (FILE, env(), "file"),
    (FILE, env("file"), "file"),
    (FILE, env("postgres", DSN, "nk"), AuthorityError),                  # PgStore is no writer
    (FILE, env("nope"), AuthorityError),
    (PG, env(), AuthorityError),                                          # missing settings
    (PG, env("file"), AuthorityError),
    (PG, env("postgres"), AuthorityError),                                # no DSN
    (PG, env("postgres", DSN), AuthorityError),                           # no schema
    (PG, env("postgres", DSN, "other"), AuthorityError),                  # wrong schema
    (PG, env("postgres", DSN + " x", "nk"), AuthorityError),              # wrong DSN
    (PG, env("postgres", DSN, "nk"), ("postgres", DSN, "nk")),
]


@pytest.mark.parametrize("record,environment,expected", MATRIX,
                         ids=[f"{r}-{sorted(e.items())}" for r, e, _ in MATRIX])
def test_backend_selection_matrix(tmp_path, record, environment, expected):
    root = tmp_path / "repo"
    if record == FILE:
        store_authority.write_record(root, "file", reason="explicit")
    elif record == PG:
        postgres(root)
    if isinstance(expected, type):
        with pytest.raises(expected):
            store_authority.select(root, env=environment)
        return
    sel = store_authority.select(root, env=environment)
    if expected == "file":
        assert sel.backend == "file"
    else:
        assert (sel.backend, sel.dsn, sel.schema) == expected
    assert (sel.record is None) == (record == NONE)


def test_explicit_backend_argument_is_subject_to_the_record(tmp_path):
    root = tmp_path / "repo"
    store_authority.write_record(root, "file", reason="explicit")
    with pytest.raises(AuthorityError):
        store.open(root=root, backend="postgres")
    assert isinstance(store.open(root=root, backend="file"), store.FileStore)


def test_file_mode_is_todays_file_store(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    write_config(root)
    assert type(store.open(root=root)) is store.FileStore      # unbound root
    store_authority.write_record(root, "file", reason="explicit")
    st = store.open(root=root)                                 # explicit file authority
    assert type(st) is store.FileStore and st.root == root
    with st.read() as view:
        assert view.config_get().backends["find_books"]["script"] == "find_books.py"


# --- fail closed --------------------------------------------------------------------------------

def test_postgres_authority_refuses_the_file_store_everywhere(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    write_config(root)
    postgres(root)
    with pytest.raises(AuthorityError, match="PostgreSQL-authoritative"):
        store.FileStore(root)
    with pytest.raises(AuthorityError):
        store.open(root=root)                                  # environment says nothing
    monkeypatch.setenv("NEKAISE_STORE", "file")
    with pytest.raises(AuthorityError):
        store.open(root=root)


def test_the_fence_survives_a_lost_host_record(tmp_path):
    root = tmp_path / "repo"
    write_config(root)
    postgres(root)
    assert store_authority.fence_path(root).exists()
    store_authority.HOST_RECORD.unlink()                       # record lost or deleted
    with pytest.raises(AuthorityError, match="fences"):
        store.FileStore(root)
    with pytest.raises(AuthorityError, match="fences"):
        store.open(root=root)
    # a file record cannot be written at or below the fence's epoch (it would not lift it)
    with pytest.raises(AuthorityError, match="must grow"):
        store_authority.write_record(root, "file", reason="stale", epoch=1)
    with pytest.raises(AuthorityError, match="fences"):
        store.FileStore(root)
    # a newer file epoch (rollback to the files) lifts it and removes the fence
    store_authority.write_record(root, "file", reason="rollback")
    assert not store_authority.fence_path(root).exists()
    assert type(store.open(root=root)) is store.FileStore


def test_rollback_to_file_authority_restores_the_file_store(tmp_path):
    root = tmp_path / "repo"
    write_config(root)
    rec = postgres(root)
    assert rec.epoch == 1
    back = store_authority.write_record(root, "file", reason="rollback")
    assert back.epoch == 2
    assert type(store.open(root=root)) is store.FileStore


def test_epochs_only_grow(tmp_path):
    root = tmp_path / "repo"
    store_authority.write_record(root, "file", reason="a")
    postgres(root)
    with pytest.raises(AuthorityError, match="must grow"):
        store_authority.write_record(root, "file", reason="b", epoch=2)
    with pytest.raises(AuthorityError):
        store_authority.write_record(root, "file", reason="")


@pytest.mark.parametrize("content", [b"not json", b"[]", b'{"format": 2, "roots": {}}',
                                     b'{"format": 1, "roots": {"/x": {"mode": "sqlite", "epoch": 1}}}'])
def test_a_corrupt_or_unknown_record_fails_closed(tmp_path, content):
    root = tmp_path / "repo"
    write_config(root)
    store_authority.HOST_RECORD.parent.mkdir(parents=True, exist_ok=True)
    store_authority.HOST_RECORD.write_bytes(content)
    with pytest.raises(AuthorityError):
        store.open(root=root)
    with pytest.raises(AuthorityError):
        store.FileStore(root)


def test_an_incomplete_postgres_record_is_refused(tmp_path):
    root = tmp_path / "repo"
    doc = {"format": 1, "roots": {str(root.resolve()): {"mode": "postgres", "epoch": 3}}}
    store_authority.HOST_RECORD.parent.mkdir(parents=True, exist_ok=True)
    store_authority.HOST_RECORD.write_text(json.dumps(doc))
    with pytest.raises(AuthorityError, match="needs dataset_uuid"):
        store_authority.select(root, env=env("postgres", DSN, "nk"))


def test_records_are_per_root(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    write_config(a)
    write_config(b)
    postgres(a)
    assert type(store.open(root=b)) is store.FileStore
    with pytest.raises(AuthorityError):
        store.open(root=a)


def test_the_host_record_location_ignores_the_environment(monkeypatch):
    monkeypatch.setenv("HOME", "/elsewhere")
    assert store_authority._host_record_path() == store_authority._host_record_path()
    assert not str(store_authority._host_record_path()).startswith("/elsewhere")


# --- entrypoints that still run the legacy file path refuse other authority -----------------------

def test_run_round_refuses_postgres_authority_before_anything(tmp_path, monkeypatch, capsys):
    import run_round
    root = tmp_path / "repo"
    write_config(root)
    postgres(root)
    monkeypatch.setattr(run_round, "ROOT", root)
    for argv in (["run_round.py", "--skip-discovery"], ["run_round.py", "--recover", "latest"]):
        monkeypatch.setattr(sys, "argv", argv)
        assert run_round.main() == 2
        assert "PostgreSQL-authoritative" in capsys.readouterr().err
    # even with the environment set for PostgreSQL (the store would open): the round path is
    # still the file path, so it refuses explicitly instead of half-working
    monkeypatch.setenv("NEKAISE_STORE", "postgres")
    monkeypatch.setenv("NEKAISE_PG_DSN", DSN)
    monkeypatch.setenv("NEKAISE_PG_SCHEMA", "nk")
    monkeypatch.setattr(store, "open", lambda **kw: object())
    monkeypatch.setattr(sys, "argv", ["run_round.py"])
    assert run_round.main() == 2
    assert "legacy file-store round path" in capsys.readouterr().err
    assert not (root / "workspace" / "round-snapshots").exists()


def test_the_maintainer_refuses_postgres_authority(tmp_path, monkeypatch):
    import maintainer
    root = tmp_path / "repo"
    write_config(root)
    postgres(root)
    monkeypatch.setattr(maintainer, "ROOT", root)
    with pytest.raises(AuthorityError):
        maintainer.open_file_store("test")
    with pytest.raises(AuthorityError):
        maintainer.backend_control_state()  # never read around the record


def test_the_tracked_state_backup_refuses_postgres_authority(tmp_path):
    root = tmp_path / "repo"
    postgres(root)
    with pytest.raises(AuthorityError, match="backup_corpus.py needs file authority"):
        store_authority.require_file_mode(root, "backup_corpus.py")


def test_shadow_replay_refuses_postgres_authority_on_the_host(tmp_path):
    import pg_shadow
    root = tmp_path / "repo"
    postgres(root)
    with pytest.raises(SystemExit, match="shadow replay is refused"):
        pg_shadow.check_host_authority(None, root)


def test_require_file_authority_names_the_store():
    class Other:
        pass
    with pytest.raises(AuthorityError, match="Other"):
        store_authority.require_file_authority(Other(), "x")


def test_show_command_prints_the_selection(tmp_path, capsys):
    root = tmp_path / "repo"
    assert store_authority.main(["show", "--root", str(root)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["record"] is None and out["selection"] == "file"
    assert store_authority.main(["init-file", "--root", str(root)]) == 0
    capsys.readouterr()
    assert store_authority.main(["show", "--root", str(root)]) == 0
    assert json.loads(capsys.readouterr().out)["record"]["mode"] == "file"
    assert store_authority.main(["init-file", "--root", str(root)]) == 0  # idempotent
