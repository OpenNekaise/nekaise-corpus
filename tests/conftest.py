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
