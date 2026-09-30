"""Publication blockers the maintainer reproduced on 2026-09-30, kept as regression tests.

Record: workspace/maintainer-publication-20260930T171346Z/record.md. Each test is one blocker:
numbers deleted by rules, a readable table dropped by the model, a changed measurement accepted,
a stale gate approval reused, and an id outside the training view written to corpus_v1/.
"""
import json
import time
from contextlib import nullcontext

import corpus_v1 as builder
import corpus_v1_night as night
import sonnet_clean as repair
import v1_rules as rules


def test_numeric_measurement_column_survives_cleaning():
    source = ["Measured air temperatures (C):", "20", "21", "22", "23", "24", "25",
              "End of measured temperatures."]
    output = rules.clean_body(source, "")
    assert all(value in output for value in source[1:7]), output


def test_numeric_table_cannot_be_dropped_by_model():
    source = "Concrete grade | strength | density\nC25/30 | 25 | 2400\nC30/37 | 30 | 2400"
    assert repair.check(source, repair.DROP) is not None


def test_model_cannot_remove_digit_from_pressure():
    source = "The specified design pressure is 1200 kPa."
    output = "The specified design pressure is 200 kPa."
    assert repair.check(source, output) is not None


def test_failed_gate_cannot_reuse_old_approval(tmp_path, monkeypatch):
    (tmp_path / "codex-gate.json").write_text(json.dumps({
        "verdict": "MERGE AS IS", "summary": "Old review of a different commit", "findings": []}))
    calls = []

    def fake_run(cmd, **kwargs):
        # No process is launched: tests pass but today's Codex review fails.
        return 1 if cmd[0] == "codex" else 0

    def fake_git(*args, **kwargs):
        # No repository is changed: record whether the gate tries to merge.
        calls.append(args)
        return 0, ""

    monkeypatch.setattr(night, "run", fake_run)
    monkeypatch.setattr(night, "git", fake_git)
    monkeypatch.setattr(night.ops, "named_lock", lambda *a, **kw: nullcontext())
    result = night.gate(tmp_path, "v1-night/review-probe", time.time() + 60)
    assert result.get("merged") is False and not any(c[0] == "merge" for c in calls), result


def test_targeted_rebuild_cannot_add_non_default_document(tmp_path, monkeypatch):
    # A held original exists in text/ but not in the default corpus/. All paths are fake.
    for folder in ("text", "corpus", "corpus_v1"):
        (tmp_path / folder).mkdir()
    out = tmp_path / "corpus_v1"
    monkeypatch.setattr(builder, "TEXT", tmp_path / "text")
    monkeypatch.setattr(builder, "CORPUS", tmp_path / "corpus")
    monkeypatch.setattr(builder, "OUT", out)
    monkeypatch.setattr(builder, "REVISIONS", out / ".revisions")
    monkeypatch.setattr(builder, "DB", out / ".state.sqlite")
    (tmp_path / "text" / "held.md").write_text("# Held document\n\n---\nBuilding energy content.")
    builder.build(["held.md"], workers=1)
    assert not (out / "held.md").exists()
