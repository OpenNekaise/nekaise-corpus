import errno
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
    assert ops.StateSnapshot.pending() == ["r1"]
    snap = ops.StateSnapshot.open("r1", tmp_path)

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
    assert ops.StateSnapshot.pending() == []


@pytest.mark.parametrize("failure_stage", ["copy", "metadata", "after_metadata"])
def test_failed_capture_removes_only_its_artifacts(tmp_path, monkeypatch, failure_stage):
    snapshots = tmp_path / "workspace" / "round-snapshots"
    monkeypatch.setattr(ops, "SNAPSHOTS", snapshots)
    source = tmp_path / "state.txt"
    source.write_bytes(b"tracked state\n")
    other = ops.StateSnapshot.capture("other", ("state.txt",), tmp_path)
    other_before = {p.relative_to(other.path): p.read_bytes()
                    for p in other.path.rglob("*") if p.is_file()}
    error = OSError(errno.ENOSPC, "No space left on device")

    def fail_copy(src, dst):
        dst.write_bytes(b"partial copy")
        raise error

    write_metadata = ops.atomic_write_text

    def fail_metadata(path, text):
        assert (path.parent / "state" / "state.txt").read_bytes() == source.read_bytes()
        assert ops.StateSnapshot.pending() == ["other"]
        if failure_stage == "after_metadata":
            write_metadata(path, text)
        raise error

    if failure_stage == "copy":
        monkeypatch.setattr(ops.shutil, "copy2", fail_copy)
    else:
        monkeypatch.setattr(ops, "atomic_write_text", fail_metadata)

    with pytest.raises(OSError) as caught:
        ops.StateSnapshot.capture("failed", ("state.txt",), tmp_path)

    assert caught.value is error
    assert source.read_bytes() == b"tracked state\n"
    assert not (snapshots / "failed").exists()
    assert ops.StateSnapshot.pending() == ["other"]
    assert {p.relative_to(other.path): p.read_bytes()
            for p in other.path.rglob("*") if p.is_file()} == other_before


def test_capture_refuses_existing_fragment_without_removing_it(tmp_path, monkeypatch):
    snapshots = tmp_path / "snapshots"
    monkeypatch.setattr(ops, "SNAPSHOTS", snapshots)
    fragment = snapshots / "existing" / "state"
    fragment.mkdir(parents=True)
    saved = fragment / "saved.txt"
    saved.write_text("preserve")

    with pytest.raises(RuntimeError, match="snapshot already exists"):
        ops.StateSnapshot.capture("existing", (), tmp_path)

    assert saved.read_text() == "preserve"


@pytest.mark.parametrize("metadata_published", [False, True])
def test_capture_cleanup_failure_preserves_original_error_and_reports_fragment(
    tmp_path, monkeypatch, metadata_published,
):
    monkeypatch.setattr(ops, "SNAPSHOTS", tmp_path / "snapshots")
    error = OSError(errno.ENOSPC, "No space left on device")
    events = []
    write_metadata = ops.atomic_write_text

    def fail_metadata(path, text):
        if metadata_published:
            write_metadata(path, text)
        raise error

    def fail_cleanup(*args):
        raise OSError(errno.EACCES, "cleanup denied")

    monkeypatch.setattr(ops, "atomic_write_text", fail_metadata)
    monkeypatch.setattr(ops.shutil, "rmtree", fail_cleanup)
    monkeypatch.setattr(ops, "run_event",
                        lambda run_id, event, **fields: events.append((run_id, event, fields)))

    with pytest.raises(OSError) as caught:
        ops.StateSnapshot.capture("failed", (), tmp_path)

    assert caught.value is error
    assert ops.StateSnapshot.pending() == []
    assert ops.StateSnapshot.incomplete_captures() == ["failed"]
    assert events == [("failed", "snapshot_cleanup_failed", {"error": "[Errno 13] cleanup denied"})]


def test_incomplete_capture_is_visible_but_not_recoverable(tmp_path, monkeypatch):
    snapshots = tmp_path / "snapshots"
    monkeypatch.setattr(ops, "SNAPSHOTS", snapshots)
    assert ops.StateSnapshot.incomplete_captures() == []
    fragment = snapshots / "killed" / "state"
    fragment.mkdir(parents=True)
    (fragment / "partial.txt").write_text("partial")
    (snapshots / "unrelated-file").write_text("ignore")

    assert ops.StateSnapshot.pending() == []
    assert ops.StateSnapshot.incomplete_captures() == ["killed"]
    with pytest.raises(RuntimeError, match="no pending snapshot"):
        ops.StateSnapshot.open("killed", tmp_path)
    assert (fragment / "partial.txt").read_text() == "partial"
