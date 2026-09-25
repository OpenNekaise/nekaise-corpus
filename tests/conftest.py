import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))


@pytest.fixture(autouse=True)
def _no_live_store_dedup(monkeypatch):
    """A finder under test must never ask the live repository's store (it would take the round
    lock or build the full index): tests patch dedup.open_keys or pass an explicit root."""
    import dedup

    def refuse():
        raise AssertionError("test reached the live store through dedup: patch dedup.open_keys "
                             "/ dedup.read_view or pass a root")
    monkeypatch.setattr(dedup, "_default_root", refuse)


@pytest.fixture(autouse=True)
def _no_live_store(monkeypatch, request):
    """No test may open a store over the live repository (it would read 1.6M rows or take the
    live round lock); tests build their own store in tmp_path. Opt out with @pytest.mark.live."""
    if request.node.get_closest_marker("live"):
        return
    import store

    live = store.ROOT.resolve()
    real_init = store.FileStore.__init__

    def guarded(self, root=store.ROOT):
        if Path(root).resolve() == live:
            raise AssertionError("test opened a FileStore over the live repository; build one in "
                                 "tmp_path and point the code under test at it")
        real_init(self, root)
    monkeypatch.setattr(store.FileStore, "__init__", guarded)

    import ops

    real_snapshot = ops.StateSnapshot.__init__

    def guarded_snapshot(self, run_id, root=ops.ROOT):
        if Path(root).resolve() == live:  # restore() would overwrite the live tracked files
            raise AssertionError("test built a round snapshot over the live repository")
        real_snapshot(self, run_id, root)
    monkeypatch.setattr(ops.StateSnapshot, "__init__", guarded_snapshot)


@pytest.fixture(autouse=True)
def _no_inherited_round_access(monkeypatch):
    """Tests build their own stores; a round lock inherited from whatever launched pytest (a
    round's gate, the maintainer, a backup) is never theirs."""
    for name in ("NEKAISE_STORE_LOCK_INHERITED", "NEKAISE_STORE_BROKER", "NEKAISE_STORE_CAP",
                 "NEKAISE_STORE_ROUND", "NEKAISE_STORE_STAGE"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _private_authority_record(monkeypatch, tmp_path):
    """The host authority record (scripts/store_authority.py) of the machine running the tests
    never applies in-process: each test gets an empty private one, and the store selection
    environment of whatever launched pytest is cleared (a test that wants PostgreSQL sets it)."""
    import store_authority

    monkeypatch.setattr(store_authority, "HOST_RECORD",
                        tmp_path / ".host-config" / "store-authority.json")
    for name in ("NEKAISE_STORE", "NEKAISE_PG_DSN", "NEKAISE_PG_SCHEMA"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture(autouse=True)
def _private_machine_state(monkeypatch, tmp_path):
    """The machine-level politeness state (scripts/openalex_state.py: shared OpenAlex spacing
    and per-host cooldowns under ~/.cache/nekaise) is shared with live rounds of every checkout:
    each test gets a private directory, and reaching the real default location fails."""
    import openalex_state

    monkeypatch.setenv(openalex_state.STATE_ENV, str(tmp_path / ".machine-state"))

    def refuse():
        raise AssertionError("test reached the machine-level state directory; it is isolated "
                             f"through {openalex_state.STATE_ENV}")
    monkeypatch.setattr(openalex_state, "default_state_dir", refuse)
