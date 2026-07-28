from types import SimpleNamespace
import sys

import pytest

import rotation
import run_round


def test_real_backend_config_covers_rotation_and_finders():
    assert run_round.validate_backends(run_round.load_backends(), rotation.load()) == []


def test_finder_command_combines_fixed_args_pointer_and_append():
    cfg = {"script": "find_osti.py", "args": ["--rows", "50"]}
    state = {"find_osti": {"flag": "--page", "next": 7}}
    assert run_round.finder_command("find_osti", cfg, state, python="python") == [
        "python", str(run_round.SCRIPTS / "find_osti.py"),
        "--rows", "50", "--page", "7", "--append",
    ]


def test_required_pipeline_is_fail_closed_and_complete():
    assert [step for step, _, _ in run_round.PIPELINE] == [
        "fetch", "prune", "clean", "check", "stats", "index", "lint",
    ]


def test_run_command_raises_on_nonzero(monkeypatch):
    monkeypatch.setattr(
        run_round.subprocess, "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=9),
    )
    monkeypatch.setattr(run_round.ops, "run_event", lambda *args, **kwargs: None)
    with pytest.raises(RuntimeError, match="failed with exit 9"):
        run_round.run_command("broken", ["false"], {}, "run-1")


def test_main_rolls_back_tracked_state_when_pipeline_fails(tmp_path, monkeypatch):
    (tmp_path / "registry").mkdir()
    (tmp_path / "manifest").mkdir()
    state = tmp_path / "registry" / "state.txt"
    state.write_text("before")
    (tmp_path / "README.md").write_text("before")
    (tmp_path / "pruned_urls.txt").write_text("")
    monkeypatch.setattr(run_round, "ROOT", tmp_path)
    monkeypatch.setattr(run_round.ops, "SNAPSHOTS", tmp_path / "workspace" / "round-snapshots")
    monkeypatch.setattr(run_round.ops, "WORKSPACE", tmp_path / "workspace")
    monkeypatch.setattr(run_round, "git_clean", lambda: True)
    monkeypatch.setattr(run_round, "load_backends", lambda: {})
    monkeypatch.setattr(run_round.rotation, "load", lambda: {})
    monkeypatch.setattr(run_round, "doc_stats", lambda: (1, 10))
    monkeypatch.setattr(run_round.ops, "run_event", lambda *args, **kwargs: None)

    def fail_after_mutation(*_args, **_kwargs):
        state.write_text("partial")
        raise RuntimeError("boom")

    monkeypatch.setattr(run_round, "run_command", fail_after_mutation)
    monkeypatch.setattr(
        sys, "argv",
        ["run_round.py", "--skip-discovery", "--skip-tests", "--allow-dirty"],
    )

    assert run_round.main() == 1
    assert state.read_text() == "before"
    assert not run_round.ops.StateSnapshot.pending()
