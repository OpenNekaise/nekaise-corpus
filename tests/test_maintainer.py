import json
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
