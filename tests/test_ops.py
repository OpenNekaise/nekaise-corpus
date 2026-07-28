import json

import pytest

import ops


def test_atomic_write_replaces_complete_file(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("old")
    ops.atomic_write_text(path, '{"ok": true}\n')
    assert json.loads(path.read_text()) == {"ok": True}
    assert not list(tmp_path.glob(".*.tmp"))


def test_named_lock_rejects_second_owner(tmp_path, monkeypatch):
    monkeypatch.setattr(ops, "WORKSPACE", tmp_path)
    with ops.named_lock("round"):
        with pytest.raises(RuntimeError, match="held by pid"):
            with ops.named_lock("round"):
                pass


def test_state_snapshot_restores_changed_created_and_deleted_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(ops, "SNAPSHOTS", tmp_path / "workspace" / "round-snapshots")
    (tmp_path / "registry").mkdir()
    (tmp_path / "registry" / "state.json").write_text("before")
    (tmp_path / "README.md").write_text("before readme")
    snap = ops.StateSnapshot.capture("r1", ("registry", "README.md", "new.txt"), tmp_path)

    (tmp_path / "registry" / "state.json").write_text("after")
    (tmp_path / "registry" / "extra.json").write_text("extra")
    (tmp_path / "README.md").unlink()
    (tmp_path / "new.txt").write_text("new")
    snap.restore()

    assert (tmp_path / "registry" / "state.json").read_text() == "before"
    assert not (tmp_path / "registry" / "extra.json").exists()
    assert (tmp_path / "README.md").read_text() == "before readme"
    assert not (tmp_path / "new.txt").exists()
    snap.discard()
