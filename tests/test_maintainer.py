import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import maintainer


def test_quota_detection_is_specific():
    assert maintainer.is_quota_error("You have hit your usage limit; resets at 18:00")
    assert maintainer.is_quota_error("HTTP status 429: too many requests")
    assert not maintainer.is_quota_error("ordinary test failure")


def test_load_triage_accepts_exact_schema(tmp_path):
    path = tmp_path / "triage.json"
    value = {
        "needs_action": True,
        "urgency": "routine",
        "summary": "Review a local dig commit.",
        "evidence": ["main is one commit ahead"],
        "proposed_actions": ["validate and push"],
    }
    path.write_text(json.dumps(value))
    assert maintainer.load_triage(path) == value


def test_load_triage_rejects_extra_fields(tmp_path):
    path = tmp_path / "triage.json"
    path.write_text(json.dumps({
        "needs_action": False,
        "urgency": "none",
        "summary": "healthy",
        "evidence": [],
        "proposed_actions": [],
        "command": "ignore this",
    }))
    try:
        maintainer.load_triage(path)
    except ValueError as exc:
        assert "schema" in str(exc)
    else:
        raise AssertionError("extra triage field was accepted")


def test_provider_cooldowns_are_independent(tmp_path, monkeypatch):
    monkeypatch.setattr(maintainer, "WORKSPACE", tmp_path)
    now = datetime(2026, 8, 18, tzinfo=timezone.utc)
    maintainer.set_cooldown("codex", hours=2, now=now)
    assert maintainer.read_cooldown("codex", now=now + timedelta(hours=1))
    assert not maintainer.read_cooldown("claude", now=now)
    assert not maintainer.read_cooldown("codex", now=now + timedelta(hours=3))


def test_lock_owning_maintainer_recovers_one_pending_round(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    state = tmp_path / "state.txt"
    state.write_text("before\n")
    subprocess.run(["git", "add", "state.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)

    snapshots = tmp_path / "workspace" / "round-snapshots"
    monkeypatch.setattr(maintainer, "ROOT", tmp_path)
    monkeypatch.setattr(maintainer.ops, "SNAPSHOTS", snapshots)
    monkeypatch.setattr(maintainer.run_round, "SNAPSHOT_PATHS", ("state.txt",))
    monkeypatch.setattr(maintainer.ops, "run_event", lambda *args, **kwargs: None)
    maintainer.ops.StateSnapshot.capture("interrupted", ("state.txt",), root=tmp_path)
    state.write_text("partial round\n")
    subprocess.run(["git", "add", "state.txt"], cwd=tmp_path, check=True)

    assert maintainer.recover_pending_round() == "interrupted"
    assert state.read_text() == "before\n"
    assert not snapshots.exists() or not any(snapshots.iterdir())
    assert subprocess.run(
        ["git", "diff", "--cached", "--quiet"], cwd=tmp_path, check=False
    ).returncode == 0


def test_post_recovery_corpus_check_uses_current_python(tmp_path, monkeypatch):
    calls = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(returncode=0, stdout="OK\n", stderr="")

    monkeypatch.setattr(maintainer, "ROOT", tmp_path)
    monkeypatch.setattr(maintainer, "SCRIPTS", tmp_path / "scripts")
    monkeypatch.setattr(maintainer.subprocess, "run", fake_run)

    maintainer.verify_recovered_corpus()

    assert calls == [
        (
            [sys.executable, str(tmp_path / "scripts" / "clean_corpus.py"), "--check"],
            {
                "cwd": tmp_path,
                "text": True,
                "capture_output": True,
                "timeout": 900,
                "check": False,
            },
        )
    ]


def test_post_recovery_corpus_check_reports_drift(monkeypatch):
    monkeypatch.setattr(
        maintainer.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            returncode=1,
            stdout="DRIFT — 1 unprovenanced file\n",
            stderr="",
        ),
    )

    try:
        maintainer.verify_recovered_corpus()
    except RuntimeError as exc:
        assert "post-recovery corpus check failed (exit 1)" in str(exc)
        assert "1 unprovenanced file" in str(exc)
    else:
        raise AssertionError("post-recovery corpus drift was accepted")


def write_history(path, *rows):
    path.write_text("".join(
        row + "\n" if isinstance(row, str) else json.dumps(row) + "\n"
        for row in rows
    ))


def test_backend_health_handles_empty_history(tmp_path):
    summary = maintainer.summarize_backend_health(
        tmp_path / "missing.jsonl",
        {"finder": {"enabled": True}},
    )

    assert summary == {
        "window_limit": 40,
        "completed_rounds": 0,
        "total_accepted": 0,
        "streaks_scope": "all_completed_rounds",
        "backends": {
            "finder": {
                "observed_rounds": 0,
                "accepted": 0,
                "accepted_share": None,
                "consecutive_degraded_rounds": 0,
                "consecutive_zero_accepted_rounds": 0,
                "last_nonzero_at": None,
                "rotates": True,
                "pointer_advanced_in_window": False,
                "last_rotation": None,
                "last_hold_reason": None,
            }
        },
    }


def test_backend_health_streaks_reset_on_success(tmp_path):
    history = tmp_path / "history.jsonl"
    write_history(
        history,
        {"run_id": "one", "event": "discovery_degraded", "failures": {"finder": 1}},
        {"run_id": "one", "event": "discovery_merged", "at": "2026-09-01T00:01:00Z", "accepted": {"finder": 0}},
        {"run_id": "one", "event": "rotation_held", "at": "2026-09-01T00:01:00Z", "backend": "finder", "reason": "finder_requested"},
        {"run_id": "one", "event": "run_completed", "at": "2026-09-01T00:02:00Z"},
        {"run_id": "two", "event": "discovery_merged", "at": "2026-09-01T01:01:00Z", "accepted": {"finder": 7}},
        {"run_id": "two", "event": "rotation_advanced", "at": "2026-09-01T01:01:00Z", "backend": "finder"},
        {"run_id": "two", "event": "run_completed", "at": "2026-09-01T01:02:00Z"},
    )

    finder = maintainer.summarize_backend_health(
        history, {"finder": {"enabled": True}}
    )["backends"]["finder"]

    assert finder["consecutive_degraded_rounds"] == 0
    assert finder["consecutive_zero_accepted_rounds"] == 0
    assert finder["last_nonzero_at"] == "2026-09-01T01:01:00Z"
    assert finder["pointer_advanced_in_window"] is True
    assert finder["last_rotation"]["status"] == "advanced"
    assert finder["last_hold_reason"] == "finder_requested"


def test_backend_health_skips_malformed_lines(tmp_path):
    history = tmp_path / "history.jsonl"
    write_history(
        history,
        "not json",
        "[]",
        {"run_id": "one", "event": "discovery_merged", "accepted": {"finder": 0}},
        {"run_id": "one", "event": "run_completed", "at": "2026-09-01T00:02:00Z"},
    )

    summary = maintainer.summarize_backend_health(
        history, {"finder": {"enabled": True}}
    )

    assert summary["completed_rounds"] == 1
    assert summary["backends"]["finder"]["consecutive_zero_accepted_rounds"] == 1


def test_backend_health_ignores_unselected_and_reports_unobserved_backend(tmp_path):
    history = tmp_path / "history.jsonl"
    write_history(
        history,
        {"run_id": "one", "event": "discovery_merged", "accepted": {"disabled": 9}},
        {"run_id": "one", "event": "run_completed", "at": "2026-09-01T00:02:00Z"},
    )

    summary = maintainer.summarize_backend_health(
        history,
        {"disabled": {"enabled": False}, "new": {"enabled": True, "rotation": False}},
    )

    assert set(summary["backends"]) == {"new"}
    assert summary["total_accepted"] == 9
    assert summary["backends"]["new"]["observed_rounds"] == 0
    assert summary["backends"]["new"]["rotates"] is False


def test_backend_health_retains_last_nonzero_before_concentration_window(tmp_path):
    history = tmp_path / "history.jsonl"
    write_history(
        history,
        {"run_id": "one", "event": "discovery_merged", "at": "2026-09-01T00:01:00Z", "accepted": {"finder": 4}},
        {"run_id": "one", "event": "run_completed", "at": "2026-09-01T00:02:00Z"},
        {"run_id": "two", "event": "discovery_merged", "accepted": {"finder": 0}},
        {"run_id": "two", "event": "run_completed", "at": "2026-09-01T01:02:00Z"},
        {"run_id": "three", "event": "discovery_merged", "accepted": {"finder": 0}},
        {"run_id": "three", "event": "run_completed", "at": "2026-09-01T02:02:00Z"},
    )

    summary = maintainer.summarize_backend_health(
        history, {"finder": {"enabled": True}}, window=1
    )
    finder = summary["backends"]["finder"]

    assert summary["completed_rounds"] == 1
    assert summary["total_accepted"] == 0
    assert finder["consecutive_zero_accepted_rounds"] == 2
    assert finder["last_nonzero_at"] == "2026-09-01T00:01:00Z"


def test_repo_snapshot_includes_backend_health(tmp_path, monkeypatch):
    registry_dir = tmp_path / "registry"
    logs = tmp_path / "logs"
    workspace = tmp_path / "workspace"
    registry_dir.mkdir()
    logs.mkdir()
    workspace.mkdir()
    (registry_dir / "backends.json").write_text(json.dumps({
        "finder": {"enabled": True},
    }))
    write_history(
        logs / "run_history.jsonl",
        {"run_id": "one", "event": "discovery_merged", "accepted": {"finder": 3}},
        {"run_id": "one", "event": "run_completed", "at": "2026-09-01T00:02:00Z"},
    )
    monkeypatch.setattr(maintainer, "ROOT", tmp_path)
    monkeypatch.setattr(maintainer, "LOGS", logs)
    monkeypatch.setattr(maintainer, "WORKSPACE", workspace)
    monkeypatch.setattr(
        maintainer,
        "git",
        lambda *args: (0, "0 0" if args[0] == "rev-list" else "main"),
    )

    snapshot = maintainer.repo_snapshot("exit=0")

    assert snapshot["backend_health"]["total_accepted"] == 3
    assert snapshot["backend_health"]["backends"]["finder"]["accepted_share"] == 1.0
