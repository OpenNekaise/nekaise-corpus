"""The test harness cannot publish fixture failures as production recovery events."""
import json
from pathlib import Path

import pytest

import ops


LIVE = Path(__file__).resolve().parents[1]


def test_real_events_and_their_lock_are_private(tmp_path):
    ops.run_event("fixture-recovery", "round_processes_stopped", pids=[123])
    ledger = tmp_path / "logs" / "run_history.jsonl"
    assert ops.RUN_LEDGER == ledger
    row, = map(json.loads, ledger.read_text().splitlines())
    assert row["run_id"] == "fixture-recovery"
    assert row["event"] == "round_processes_stopped" and row["pids"] == [123]
    assert (tmp_path / "workspace" / ".run-ledger.lock").is_file()


def test_undoing_test_patches_keeps_event_isolation(tmp_path, monkeypatch):
    monkeypatch.setattr(ops, "RUN_LEDGER", tmp_path / "another-ledger.jsonl")
    monkeypatch.setattr(ops, "run_event", lambda *a, **kw: None)
    monkeypatch.undo()
    ops.run_event("fixture-recovered", "staged_run_aborted")
    assert (tmp_path / "logs" / "run_history.jsonl").is_file()


@pytest.mark.parametrize("via_symlink", [False, True])
def test_live_ledger_fails_before_any_write(tmp_path, monkeypatch, via_symlink):
    destination = LIVE / "logs" / "run_history.jsonl"
    if via_symlink:
        link = tmp_path / "live-ledger-link"
        link.symlink_to(destination)
        destination = link
    monkeypatch.setattr(ops, "RUN_LEDGER", destination)
    with pytest.raises(AssertionError, match="live run ledger"):
        ops.run_event("fixture-refused", "staged_run_aborted")
    assert not (tmp_path / "workspace" / ".run-ledger.lock").exists()


def test_live_lock_directory_fails_before_any_write(tmp_path, monkeypatch):
    monkeypatch.setattr(ops, "WORKSPACE", LIVE / "workspace")
    with pytest.raises(AssertionError, match="live run-ledger lock directory"):
        ops.run_event("fixture-refused", "staged_run_aborted")
    assert not ops.RUN_LEDGER.exists()
