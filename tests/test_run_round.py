from types import SimpleNamespace
import json
import os
import sys
from pathlib import Path

import pytest

import rotation
import run_round


def test_real_backend_config_covers_rotation_and_finders():
    backends = run_round.load_backends()
    assert run_round.validate_backends(backends, rotation.load()) == []
    assert backends["find_books"]["enabled"] is False
    assert backends["find_books"]["required"] is False


def test_real_openalex_backend_uses_one_rotating_query_per_round():
    backends = run_round.load_backends()
    # The suite runs after discovery has advanced the live pointer.  Use an
    # isolated nonzero pointer so this contract never depends on mutable state.
    state = {"find_openalex": {"flag": "--query-cursor", "next": 7}}

    assert run_round.finder_command(
        "find_openalex", backends["find_openalex"], state, python="python"
    ) == [
        "python", str(run_round.SCRIPTS / "find_sources.py"),
        "--per", "100", "--backends", "openalex", "--query-count", "1",
        "--circuit-threshold", "1",
        "--query-cursor", "7", "--append",
    ]


def test_backend_required_flag_must_be_boolean(monkeypatch):
    monkeypatch.setattr(run_round, "SCRIPTS", Path(__file__).parent / "fixtures")
    errors = run_round.validate_backends({
        "bad": {
            "script": "fake_finder.py",
            "rotation": False,
            "required": "sometimes",
        }
    }, {})
    assert errors == ["bad: required must be true or false"]


def test_backend_rejects_malformed_rotation_skip(monkeypatch):
    monkeypatch.setattr(run_round, "SCRIPTS", Path(__file__).parent / "fixtures")
    errors = run_round.validate_backends({
        "bad": {"script": "fake_finder.py"},
    }, {
        "bad": {
            "flag": "--bucket",
            "next": "2020-W53",
            "skip": [["2017-W49", "2020-W52"]],
        }
    })
    assert errors == ["bad: skip[0] newest bucket must not precede oldest bucket"]


def test_finder_command_combines_fixed_args_pointer_and_append():
    cfg = {"script": "find_osti.py", "args": ["--rows", "50"]}
    state = {"find_osti": {"flag": "--page", "next": 7}}
    assert run_round.finder_command("find_osti", cfg, state, python="python") == [
        "python", str(run_round.SCRIPTS / "find_osti.py"),
        "--rows", "50", "--page", "7", "--append",
    ]


def test_required_pipeline_is_fail_closed_and_complete():
    serial = [step for step, _, _ in run_round.PIPELINE]
    gates = [step for step, _, _ in run_round.VERIFY]
    assert serial == ["fetch", "prune", "clean", "stats"]
    assert sorted(gates) == ["check", "contracts", "index", "lint"]
    assert not set(serial) & set(gates)
    # contracts checks the README counts that stats writes -> stats must be in the serial prefix
    assert "stats" in serial and "contracts" in gates


def test_run_verify_parallel_awaits_every_gate_and_aggregates_failures(monkeypatch, capsys):
    import time as _time

    seen = []

    def fake_run(cmd, **kwargs):
        step = cmd[-1]
        seen.append(step)
        _time.sleep(0.05 if step == "slow-ok" else 0)
        rc = {"fail-a": 2, "fail-b": 3}.get(step, 0)
        return SimpleNamespace(returncode=rc, stdout=f"out {step}\n", stderr="")

    events = []
    monkeypatch.setattr(run_round.subprocess, "run", fake_run)
    monkeypatch.setattr(
        run_round.ops, "run_event",
        lambda run_id, event, **kw: events.append((event, kw.get("step"))),
    )
    gates = [
        ("fail-a", ["x", "fail-a"]), ("slow-ok", ["x", "slow-ok"]),
        ("fail-b", ["x", "fail-b"]), ("ok", ["x", "ok"]),
    ]

    with pytest.raises(RuntimeError, match=r"fail-a \(exit 2\), fail-b \(exit 3\)"):
        run_round.run_verify_parallel(gates, {}, "run-1")

    assert sorted(seen) == sorted(step for step, _ in gates)  # nothing skipped after a failure
    assert ("step_failed", "fail-a") in events and ("step_completed", "slow-ok") in events
    out = capsys.readouterr().out  # replayed in declared order, not completion order
    assert out.index("out fail-a") < out.index("out slow-ok") < out.index("out fail-b") < out.index("out ok")


def test_run_command_raises_on_nonzero(monkeypatch):
    monkeypatch.setattr(
        run_round.subprocess, "run",
        lambda *args, **kwargs: SimpleNamespace(returncode=9),
    )
    monkeypatch.setattr(run_round.ops, "run_event", lambda *args, **kwargs: None)
    with pytest.raises(RuntimeError, match="failed with exit 9"):
        run_round.run_command("broken", ["false"], {}, "run-1")


def test_doc_stats_counts_only_training_eligible_rows(monkeypatch):
    rows = [
        {"id": "pat-us1", "status": "ok", "license": "public-domain", "text_chars": 100},
        {"id": "pat-cn1", "status": "ok", "license": "open", "text_chars": 800},
        {"id": "jst-1", "status": "ok", "source": "jstage_aij", "license": "open",
         "text_chars": 400},
        {"id": "pat-us2", "status": "failed", "license": "public-domain", "text_chars": 40},
    ]
    restrictions = {
        "cn": {"match": {"id_prefix": "pat-cn"}},
        "jstage": {"match": {"source": "jstage_aij"}},
    }
    monkeypatch.setattr(run_round.registry, "load_manifest_rows", lambda: rows)
    monkeypatch.setattr(run_round.registry, "load_eligibility", lambda: restrictions)

    assert run_round.doc_stats() == (1, 25, 2)


def test_merge_proposals_is_deterministic_and_deduplicates(tmp_path, monkeypatch):
    first = tmp_path / "first.json"
    second = tmp_path / "second.json"
    first.write_text(json.dumps([
        {
            "id": "ost-same",
            "title": "First",
            "url": "https://e.org/first.pdf",
            "source": "osti",
            "license": "public-domain",
            "topic": "construction",
            "format": "pdf",
        }
    ]))
    second.write_text(json.dumps([
        {
            "id": "ost-same",
            "title": "Second",
            "url": "https://e.org/second.pdf",
            "source": "osti",
            "license": "public-domain",
            "topic": "construction",
            "format": "pdf",
        },
        {
            "id": "ost-duplicate",
            "title": "Duplicate URL",
            "url": "https://e.org/first.pdf",
            "source": "osti",
            "license": "public-domain",
            "topic": "construction",
            "format": "pdf",
        },
    ]))
    appended = []
    monkeypatch.setattr(run_round.registry, "existing_keys", lambda: (set(), set(), set()))
    monkeypatch.setattr(
        run_round.registry,
        "append_entries",
        lambda entries: appended.extend(entries),
    )

    total, accepted = run_round.merge_proposals([
        {"index": 1, "name": "second", "proposal": second},
        {"index": 0, "name": "first", "proposal": first},
    ])

    assert total == 2
    assert accepted == {"first": 1, "second": 1}
    assert [entry["title"] for entry in appended] == ["First", "Second"]
    assert len({entry["id"] for entry in appended}) == 2


def test_parallel_finders_stage_in_subprocesses_then_merge_once(tmp_path, monkeypatch):
    fixtures = Path(__file__).parent / "fixtures"
    monkeypatch.setattr(run_round, "SCRIPTS", fixtures)
    monkeypatch.setattr(run_round.ops, "WORKSPACE", tmp_path / "workspace")
    monkeypatch.setattr(run_round.registry, "existing_keys", lambda: (set(), set(), set()))
    appended = []
    monkeypatch.setattr(
        run_round.registry,
        "append_entries",
        lambda entries: appended.extend(entries),
    )
    events = []
    monkeypatch.setattr(
        run_round.ops,
        "run_event",
        lambda run_id, event, **fields: events.append((run_id, event, fields)),
    )
    backends = {
        "one": {
            "script": "fake_finder.py",
            "args": [
                "--id", "ost-one", "--title", "One", "--url", "https://e.org/one",
            ],
            "rotation": False,
        },
        "two": {
            "script": "fake_finder.py",
            "args": [
                "--id", "ost-two", "--title", "Two", "--url", "https://e.org/two",
            ],
            "rotation": False,
        },
    }

    run_round.run_finders_parallel(
        ["one", "two"],
        backends,
        {},
        os.environ.copy(),
        "fixture-run",
        workers=2,
    )

    assert [entry["id"] for entry in appended] == ["ost-one", "ost-two"]
    assert ("fixture-run", "discovery_merged", {
        "candidates": 2,
        "accepted": {"one": 1, "two": 1},
    }) in events


def test_successful_finder_can_hold_rotation_at_a_capped_pointer(tmp_path, monkeypatch):
    fixtures = Path(__file__).parent / "fixtures"
    monkeypatch.setattr(run_round, "SCRIPTS", fixtures)
    monkeypatch.setattr(run_round.ops, "WORKSPACE", tmp_path / "workspace")
    monkeypatch.setattr(run_round.registry, "existing_keys", lambda: (set(), set(), set()))
    monkeypatch.setattr(run_round.registry, "append_entries", lambda _entries: None)
    events = []
    monkeypatch.setattr(
        run_round.ops,
        "run_event",
        lambda run_id, event, **fields: events.append((run_id, event, fields)),
    )
    advanced = []
    monkeypatch.setattr(
        run_round.rotation,
        "advance",
        lambda name: advanced.append(name),
    )
    backends = {
        "capped": {
            "script": "fake_finder.py",
            "args": [
                "--id", "pat-cn100a", "--title", "Concrete foundation",
                "--url", "https://e.org/cn100a", "--hold-rotation",
            ],
        },
    }
    state = {"capped": {"flag": "--bucket", "next": "2022-W48"}}

    run_round.run_finders_parallel(
        ["capped"],
        backends,
        state,
        os.environ.copy(),
        "fixture-run",
        workers=1,
    )

    assert advanced == []
    assert ("fixture-run", "rotation_held", {
        "backend": "capped",
        "reason": "finder_requested",
    }) in events


def test_optional_finder_failure_is_reported_without_blocking_merge(tmp_path, monkeypatch):
    fixtures = Path(__file__).parent / "fixtures"
    monkeypatch.setattr(run_round, "SCRIPTS", fixtures)
    monkeypatch.setattr(run_round.ops, "WORKSPACE", tmp_path / "workspace")
    monkeypatch.setattr(run_round.registry, "existing_keys", lambda: (set(), set(), set()))
    appended = []
    monkeypatch.setattr(
        run_round.registry,
        "append_entries",
        lambda entries: appended.extend(entries),
    )
    events = []
    monkeypatch.setattr(
        run_round.ops,
        "run_event",
        lambda run_id, event, **fields: events.append((run_id, event, fields)),
    )
    advanced = []
    monkeypatch.setattr(
        run_round.rotation,
        "advance",
        lambda name: advanced.append(name),
    )
    backends = {
        "good": {
            "script": "fake_finder.py",
            "args": [
                "--id", "ost-good", "--title", "Good", "--url", "https://e.org/good",
            ],
            "rotation": False,
        },
        "volatile": {
            "script": "fake_finder.py",
            "args": [
                "--id", "ost-bad", "--title", "Bad", "--url", "https://e.org/bad",
                "--exit-code", "7",
            ],
            "required": False,
        },
    }

    run_round.run_finders_parallel(
        ["good", "volatile"],
        backends,
        {"volatile": {"flag": "--bucket", "next": "2022-W48"}},
        os.environ.copy(),
        "fixture-run",
        workers=2,
    )

    assert [entry["id"] for entry in appended] == ["ost-good"]
    assert ("fixture-run", "discovery_degraded", {"failures": {"volatile": 7}}) in events
    assert ("fixture-run", "discovery_merged", {
        "candidates": 1,
        "accepted": {"good": 1, "volatile": 0},
    }) in events
    assert advanced == []


def test_required_finder_failure_still_blocks_merge(tmp_path, monkeypatch):
    fixtures = Path(__file__).parent / "fixtures"
    monkeypatch.setattr(run_round, "SCRIPTS", fixtures)
    monkeypatch.setattr(run_round.ops, "WORKSPACE", tmp_path / "workspace")
    monkeypatch.setattr(run_round.registry, "existing_keys", lambda: (set(), set(), set()))
    appended = []
    monkeypatch.setattr(
        run_round.registry,
        "append_entries",
        lambda entries: appended.extend(entries),
    )
    monkeypatch.setattr(run_round.ops, "run_event", lambda *_args, **_kwargs: None)
    backends = {
        "good": {
            "script": "fake_finder.py",
            "args": [
                "--id", "ost-good", "--title", "Good", "--url", "https://e.org/good",
            ],
            "rotation": False,
        },
        "required": {
            "script": "fake_finder.py",
            "args": [
                "--id", "ost-bad", "--title", "Bad", "--url", "https://e.org/bad",
                "--exit-code", "7",
            ],
            "rotation": False,
        },
    }

    with pytest.raises(RuntimeError, match=r"discovery failed: required \(7\)"):
        run_round.run_finders_parallel(
            ["good", "required"],
            backends,
            {},
            os.environ.copy(),
            "fixture-run",
            workers=2,
        )

    assert appended == []


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
    monkeypatch.setattr(run_round, "doc_stats", lambda: (1, 10, 0))
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
