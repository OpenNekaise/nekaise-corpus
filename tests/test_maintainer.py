import json
import subprocess
from datetime import datetime, timedelta, timezone

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
