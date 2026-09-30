"""Publication blockers the maintainer reproduced on 2026-09-30, kept as regression tests.

Record: workspace/maintainer-publication-20260930T171346Z/record.md. Each test is one blocker:
numbers deleted by rules, a readable table dropped by the model, a changed measurement accepted,
a stale gate approval reused, and an id outside the training view written to corpus_v1/.
"""
import json
import time
from pathlib import Path
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
    monkeypatch.setattr(builder, "LOCK_WORKSPACE", tmp_path / "workspace")
    (tmp_path / "text" / "held.md").write_text("# Held document\n\n---\nBuilding energy content.")
    builder.build(["held.md"], workers=1)
    assert not (out / "held.md").exists()


def _gate_world(tmp_path, monkeypatch, *, diff_code=0, move_during_tests=False, verdict="MERGE AS IS",
                base_code_changed=False):
    """A mocked repository for gate(): no process, repository or lock is touched."""
    calls, state = [], {"head": "a" * 40, "tests": 0}

    def fake_run(cmd, **kwargs):
        if cmd[0] == "codex":
            out = cmd[cmd.index("--output-last-message") + 1]
            Path(out).write_text(json.dumps({"verdict": verdict, "summary": "", "findings": []}))
            return 0
        state["tests"] += 1
        if move_during_tests and state["tests"] == 2:
            state["head"] = "b" * 40
        return 0

    def fake_git(*args, **kwargs):
        calls.append(args)
        if args[:2] == ("diff", "--quiet"):
            return (1 if base_code_changed else 0), ""
        if args[0] == "diff":
            return diff_code, "diff --git a/x b/x\n+change\n"
        if args[0] == "rev-parse":
            return 0, state["head"]
        return 0, ""

    monkeypatch.setattr(night, "run", fake_run)
    monkeypatch.setattr(night, "git", fake_git)
    monkeypatch.setattr(night.ops, "named_lock", lambda *a, **kw: nullcontext())
    return night.gate(tmp_path, "v1-night/probe", time.time() + 600), calls


def test_gate_merges_exactly_the_reviewed_commit(tmp_path, monkeypatch):
    result, calls = _gate_world(tmp_path, monkeypatch)
    assert result["merged"] is True, result
    assert ("merge", "--ff-only", "a" * 40) in calls  # the commit, not the branch name


def test_gate_fails_closed_when_git_diff_fails(tmp_path, monkeypatch):
    result, calls = _gate_world(tmp_path, monkeypatch, diff_code=128)
    assert result["merged"] is False and not any(c[0] == "merge" for c in calls)


def test_gate_fails_closed_when_branch_moves_during_final_tests(tmp_path, monkeypatch):
    result, calls = _gate_world(tmp_path, monkeypatch, move_during_tests=True)
    assert result["merged"] is False and not any(c[0] == "merge" for c in calls)


def test_gate_changes_required_never_merges(tmp_path, monkeypatch):
    result, calls = _gate_world(tmp_path, monkeypatch, verdict="CHANGES REQUIRED")
    assert result["merged"] is False and not any(c[0] == "merge" for c in calls)


def _builder_world(tmp_path, monkeypatch):
    for folder in ("text", "corpus", "corpus_v1"):
        (tmp_path / folder).mkdir()
    out = tmp_path / "corpus_v1"
    monkeypatch.setattr(builder, "TEXT", tmp_path / "text")
    monkeypatch.setattr(builder, "CORPUS", tmp_path / "corpus")
    monkeypatch.setattr(builder, "OUT", out)
    monkeypatch.setattr(builder, "REVISIONS", out / ".revisions")
    monkeypatch.setattr(builder, "DB", out / ".state.sqlite")
    monkeypatch.setattr(builder, "LOCK_WORKSPACE", tmp_path / "workspace")  # never the live lock
    return out


def test_path_ids_never_reach_outside_corpus_v1(tmp_path, monkeypatch):
    out = _builder_world(tmp_path, monkeypatch)
    victim = tmp_path / "corpus" / "held.md"
    victim.write_text("# Held\n\n---\nkeep me")
    result = builder.build(["../corpus/held.md", "/etc/passwd", "a/../b.md"], workers=1)
    assert victim.exists() and result["invalid_ids"] == 3
    assert not builder.valid_id("../x.md") and builder.valid_id("pat-cn111964330b.md")


def test_document_leaving_the_view_mid_build_is_not_published(tmp_path, monkeypatch):
    out = _builder_world(tmp_path, monkeypatch)
    (tmp_path / "text" / "d.md").write_text("# D\n\n---\nBuilding energy content.")
    (tmp_path / "corpus" / "d.md").write_text("x")
    real = builder.rule_clean

    def clean_then_unlist(doc_id):
        res = real(doc_id)
        (tmp_path / "corpus" / "d.md").unlink()  # a prune lands while the doc is being cleaned
        return res

    monkeypatch.setattr(builder, "rule_clean", clean_then_unlist)
    assert builder.build_one(("d.md", None)) == ("REMOVED", "d.md")
    assert not (out / "d.md").exists()


def test_repairs_validated_by_the_old_checks_are_not_trusted(tmp_path, monkeypatch):
    out = _builder_world(tmp_path, monkeypatch)
    (tmp_path / "text" / "d.md").write_text("# D\n\n---\nsource")
    con = builder.connect()
    (out / ".revisions" / "d.md").write_text("old repair")
    size, mtime = builder.source_key("d.md")
    for prompt, trusted in (("p2", False), ("p3", True)):
        con.execute("INSERT OR REPLACE INTO revision VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    ("d.md", size, mtime, "0" * 64, "m", prompt, "ok", 1, 0, 0, 1, 1, "t"))
        assert ("d.md" in builder.valid_revisions(con)) is trusted


def test_gate_requires_fresh_review_when_mains_code_changed(tmp_path, monkeypatch):
    result, calls = _gate_world(tmp_path, monkeypatch, base_code_changed=True)
    assert result["merged"] is False and not any(c[0] == "merge" for c in calls)


def test_nothing_is_published_while_a_dig_round_holds_the_lock(tmp_path, monkeypatch):
    out = _builder_world(tmp_path, monkeypatch)
    (tmp_path / "text" / "d.md").write_text("# D\n\n---\nBuilding energy content.")
    (tmp_path / "corpus" / "d.md").write_text("x")
    with builder.ops.named_lock(builder.ops.ROUND_LOCK, workspace=tmp_path / "workspace"):
        try:
            builder.build(["d.md"], workers=1, lock_timeout=0)
            raise AssertionError("build published while the round lock was held")
        except RuntimeError:
            pass
        assert builder.rebuild_one(builder.connect(), "d.md") is False
    assert not (out / "d.md").exists()
    builder.build(["d.md"], workers=1, lock_timeout=0)  # lock free: published
    assert (out / "d.md").exists()
