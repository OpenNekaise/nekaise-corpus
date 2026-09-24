from contextlib import contextmanager
from types import SimpleNamespace
import json
import os
import sys
from pathlib import Path

import pytest

import rotation
import run_round
import store
import store_broker


def test_real_backend_config_covers_rotation_and_finders():
    backends = run_round.load_backends()
    committed = json.loads((run_round.ROOT / "registry" / "rotation.json").read_text())
    assert run_round.validate_backends(backends, committed) == []
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


def test_doc_stats_counts_only_training_eligible_rows(monkeypatch, tmp_path):
    rows = [
        {"id": "pat-us1", "status": "ok", "license": "public-domain", "text_chars": 100},
        {"id": "pat-cn1", "status": "ok", "license": "open", "text_chars": 800},
        {"id": "jst-1", "status": "ok", "source": "jstage_aij", "license": "open",
         "text_chars": 400},
        {"id": "pat-us2", "status": "failed", "license": "public-domain", "text_chars": 40},
    ]
    import pipeline_repo
    import store
    restrictions = {
        "cn": pipeline_repo.restriction({"id_prefix": "pat-cn"}),
        "jstage": pipeline_repo.restriction({"source": "jstage_aij"}),
    }
    pipeline_repo.pin_policy(tmp_path, restrictions=restrictions)  # doc_stats pins it
    st = store.FileStore(tmp_path)
    with st.writer() as w:
        with st.transaction("seed", expected_version=st.version(), writer=w) as tx:
            tx.upsert_manifest(rows)
    with st.read() as view:
        assert run_round.doc_stats(view) == (1, 25, 2)


def fixture_store(tmp_path, backends, rotation_state=None):
    """A throwaway repository whose store the discovery transaction writes."""
    reg = tmp_path / "repo" / "registry"
    reg.mkdir(parents=True)
    (reg / "backends.json").write_text(json.dumps({"_readme": "control plane", **backends},
                                                  indent=2) + "\n")
    (reg / "rotation.json").write_text(json.dumps(rotation_state or {}, indent=2) + "\n")
    return store.FileStore(tmp_path / "repo")


@contextmanager
def discovery_transaction(st, run_id="fixture-run"):
    """run_round's discovery transaction factory: the round broker's local batch."""
    with st.writer(round_id=run_id) as w:
        broker = store_broker.Broker(st, w, run_id)
        with broker.serving():
            yield lambda: broker.local_batch("discover", "merge")


def registry_ids(st):
    with st.read() as view:
        return [e["id"] for e in view.scan(store.Table.ENTRIES).rows]


def finder_env(tmp_path, monkeypatch):
    monkeypatch.setattr(run_round, "SCRIPTS", Path(__file__).parent / "fixtures")
    monkeypatch.setattr(run_round.ops, "WORKSPACE", tmp_path / "workspace")
    events = []
    monkeypatch.setattr(
        run_round.ops,
        "run_event",
        lambda run_id, event, **fields: events.append((run_id, event, fields)),
    )
    return events


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
    st = fixture_store(tmp_path, {})

    with st.read() as view:
        merged, accepted, passes = run_round.merge_proposals(view, [
            {"index": 1, "name": "second", "proposal": second},
            {"index": 0, "name": "first", "proposal": first},
        ])

    assert accepted == {"first": 1, "second": 1}
    assert [entry["title"] for entry in merged] == ["First", "Second"]
    assert [entry["id"] for entry in merged] == ["ost-same", "ost-same-2"]
    assert passes == {}


def test_parallel_finders_stage_in_subprocesses_then_merge_once(tmp_path, monkeypatch):
    events = finder_env(tmp_path, monkeypatch)
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
    st = fixture_store(tmp_path, backends)

    with discovery_transaction(st) as transaction:
        run_round.run_finders_parallel(
            ["one", "two"],
            backends,
            {},
            os.environ.copy(),
            "fixture-run",
            2,
            transaction,
        )

    assert registry_ids(st) == ["ost-one", "ost-two"]
    assert ("fixture-run", "discovery_merged", {
        "candidates": 2,
        "accepted": {"one": 1, "two": 1},
    }) in events
    with st.read() as view:  # one discovery transaction
        assert {e["run_id"] for e in view.scan(store.Table.EVENTS).rows} == {
            "fixture-run.discover.merge"}


@pytest.mark.parametrize("note, detail", [
    ("candidate cap reached", "candidate cap reached"),
    ("2026-09 is the open UTC month; re-probe until it closes",
     "2026-09 is the open UTC month; re-probe until it closes"),
    ("", ""),
    ("  capped\t\x1b‮ 月  \nignored second line", "capped 月"),
])
def test_successful_finder_can_hold_rotation_with_optional_detail(
    tmp_path, monkeypatch, capsys, note, detail
):
    events = finder_env(tmp_path, monkeypatch)
    backends = {
        "capped": {
            "script": "fake_finder.py",
            "args": [
                "--id", "pat-cn100a", "--title", "Concrete foundation",
                "--url", "https://e.org/cn100a", "--hold-rotation",
                "--hold-note", note,
            ],
        },
    }
    state = {"capped": {"flag": "--bucket", "next": "2022-W48"}}
    st = fixture_store(tmp_path, backends, state)

    with discovery_transaction(st) as transaction:
        run_round.run_finders_parallel(
            ["capped"],
            backends,
            state,
            os.environ.copy(),
            "fixture-run",
            1,
            transaction,
        )

    with st.read() as view:
        assert view.rotation_get("capped")["next"] == "2022-W48"  # held: pointer kept
    assert registry_ids(st) == ["pat-cn100a"]
    assert ("fixture-run", "rotation_held", {
        "backend": "capped",
        "reason": "finder_requested",
        **({"detail": detail} if detail else {}),
    }) in events
    assert not [e for e in events if e[1] == "rotation_advanced"]
    assert f"rotation held for capped: {detail or 'finder requested hold'}" in capsys.readouterr().out


@pytest.mark.parametrize("payload, expected", [
    (b"x" * 10000, "x" * 512),
    (b"\nsecond line", ""),
    (b"\xff\x00\x1f\x7f cap\r\nignored", "\ufffd cap"),
])
def test_rotation_hold_note_is_bounded_and_tolerates_malformed_text(tmp_path, payload, expected):
    note = tmp_path / "hold"
    note.write_bytes(payload)
    assert run_round._rotation_hold_detail(note) == expected


def test_unreadable_rotation_hold_note_is_only_missing_detail(tmp_path, monkeypatch):
    def denied(*args, **kwargs):
        raise PermissionError("unreadable hold note")

    monkeypatch.setattr(Path, "open", denied)
    assert run_round._rotation_hold_detail(tmp_path / "hold") == ""


def test_dynamic_finder_replaces_cursor_and_disables_itself_at_exhaustion(
    tmp_path, monkeypatch, capsys
):
    events = finder_env(tmp_path, monkeypatch)
    backends = {
        "dynamic": {
            "script": "fake_finder.py",
            "args": [
                "--id", "kit-one", "--title", "Architecture",
                "--url", "https://e.org/kit-one", "--next-pointer", "END",
                "--exhausted", "set fully harvested",
            ],
        },
    }
    state = {"dynamic": {"flag": "--token", "next": "START", "dynamic": True}}
    st = fixture_store(tmp_path, backends, state)
    config_before = (st.reg / "backends.json").read_bytes()

    with discovery_transaction(st) as transaction:
        run_round.run_finders_parallel(
            ["dynamic"], backends, state, os.environ.copy(), "fixture-run", 1, transaction
        )

    with st.read() as view:
        assert view.rotation_get("dynamic")["next"] == "END"
        assert view.backend_state_get("dynamic") == store.BackendState(
            False, "exhausted: set fully harvested")
        assert not view.backend_enabled("dynamic")
    # runtime state, never the git-owned configuration
    assert (st.reg / "backends.json").read_bytes() == config_before
    assert ("fixture-run", "rotation_advanced", {
        "backend": "dynamic", "next": "--token END",
    }) in events
    assert ("fixture-run", "backend_disabled", {
        "backend": "dynamic", "reason": "set fully harvested",
    }) in events
    assert "backend disabled for dynamic: exhausted: set fully harvested" in capsys.readouterr().out


def test_dynamic_finder_missing_next_cursor_fails_before_merge(tmp_path, monkeypatch):
    finder_env(tmp_path, monkeypatch)
    backends = {
        "dynamic": {
            "script": "fake_finder.py",
            "args": [
                "--id", "kit-one", "--title", "Architecture",
                "--url", "https://e.org/kit-one",
            ],
        },
    }
    state = {"dynamic": {"flag": "--token", "next": "START", "dynamic": True}}
    st = fixture_store(tmp_path, backends, state)
    version = st.version()

    with discovery_transaction(st) as transaction:
        with pytest.raises(RuntimeError, match="did not report its next cursor"):
            run_round.run_finders_parallel(
                ["dynamic"], backends, state, os.environ.copy(), "fixture-run", 1, transaction
            )

    assert st.version() == version  # protocol failure precedes the discovery transaction


def test_optional_finder_failure_is_reported_without_blocking_merge(tmp_path, monkeypatch):
    events = finder_env(tmp_path, monkeypatch)
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
    state = {"volatile": {"flag": "--bucket", "next": "2022-W48"}}
    st = fixture_store(tmp_path, backends, state)

    with discovery_transaction(st) as transaction:
        run_round.run_finders_parallel(
            ["good", "volatile"],
            backends,
            state,
            os.environ.copy(),
            "fixture-run",
            2,
            transaction,
        )

    assert registry_ids(st) == ["ost-good"]
    assert ("fixture-run", "discovery_degraded", {"failures": {"volatile": 7}}) in events
    assert ("fixture-run", "discovery_merged", {
        "candidates": 1,
        "accepted": {"good": 1, "volatile": 0},
    }) in events
    with st.read() as view:
        assert view.rotation_get("volatile")["next"] == "2022-W48"  # failed: pointer kept


def test_required_finder_failure_still_blocks_merge(tmp_path, monkeypatch):
    finder_env(tmp_path, monkeypatch)
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
    st = fixture_store(tmp_path, backends)
    version = st.version()

    with discovery_transaction(st) as transaction:
        with pytest.raises(RuntimeError, match=r"discovery failed: required \(7\)"):
            run_round.run_finders_parallel(
                ["good", "required"],
                backends,
                {},
                os.environ.copy(),
                "fixture-run",
                2,
                transaction,
            )

    assert st.version() == version
    assert registry_ids(st) == []


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
    monkeypatch.setattr(run_round, "doc_stats", lambda view: (1, 10, 0))
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


def test_capture_failure_stops_round_before_mutation(tmp_path, monkeypatch, capsys):
    import errno

    state = tmp_path / "README.md"
    state.write_bytes(b"before\n")
    snapshots = tmp_path / "workspace" / "round-snapshots"
    monkeypatch.setattr(run_round, "ROOT", tmp_path)
    monkeypatch.setattr(run_round.ops, "SNAPSHOTS", snapshots)
    monkeypatch.setattr(run_round.ops, "WORKSPACE", tmp_path / "workspace")
    monkeypatch.setattr(run_round, "git_clean", lambda: True)
    monkeypatch.setattr(run_round, "load_backends", lambda: {})
    monkeypatch.setattr(run_round.rotation, "load", lambda: {})
    monkeypatch.setattr(run_round, "doc_stats", lambda view: (1, 10, 0))
    events = []
    monkeypatch.setattr(run_round.ops, "run_event",
                        lambda run_id, event, **fields: events.append(event))

    def fail_copy(src, dst):
        dst.write_bytes(b"partial")
        raise OSError(errno.ENOSPC, "No space left on device")

    def unexpected_mutation(*args, **kwargs):
        pytest.fail("capture failed, so no discovery, pipeline or commit may run")

    monkeypatch.setattr(run_round.ops.shutil, "copy2", fail_copy)
    for name in ("run_finders_parallel", "run_command", "commit_snapshot"):
        monkeypatch.setattr(run_round, name, unexpected_mutation)
    monkeypatch.setattr(sys, "argv", ["run_round.py", "--commit", "--run-id", "capture-failed"])

    assert run_round.main() == 1
    assert "No space left on device" in capsys.readouterr().err
    assert events == ["run_started", "run_failed"]
    assert state.read_bytes() == b"before\n"
    assert not run_round.ops.StateSnapshot.pending()
    assert not (snapshots / "capture-failed").exists()


def test_round_refuses_to_start_over_an_interrupted_store_transaction(tmp_path, monkeypatch,
                                                                       capsys):
    txn = tmp_path / "workspace" / "store-transactions" / "r1"
    txn.mkdir(parents=True)
    (txn / "meta.json").write_text('{"run_id": "r1", "state": "prepared", "files": []}')
    monkeypatch.setattr(run_round, "ROOT", tmp_path)
    monkeypatch.setattr(run_round.ops, "SNAPSHOTS", tmp_path / "workspace" / "round-snapshots")
    monkeypatch.setattr(run_round.ops, "WORKSPACE", tmp_path / "workspace")
    monkeypatch.setattr(run_round.ops, "run_event", lambda *args, **kwargs: None)
    for name in ("run_finders_parallel", "run_command", "commit_snapshot", "doc_stats"):
        monkeypatch.setattr(run_round, name,
                            lambda *a, **k: pytest.fail("no round step may run"))
    monkeypatch.setattr(sys, "argv", ["run_round.py", "--skip-discovery", "--allow-dirty"])

    assert run_round.main() == 1
    assert "interrupted store transaction(s) pending: r1" in capsys.readouterr().err


def test_round_refuses_to_run_uncommitted_while_a_shadow_is_enabled(tmp_path, monkeypatch, capsys):
    (tmp_path / "workspace").mkdir()
    (tmp_path / "workspace" / ".pg-shadow").write_text("dsn\nschema\n")
    monkeypatch.setattr(run_round, "ROOT", tmp_path)
    monkeypatch.setattr(run_round.ops, "SNAPSHOTS", tmp_path / "workspace" / "round-snapshots")
    monkeypatch.setattr(run_round.ops, "WORKSPACE", tmp_path / "workspace")
    monkeypatch.setattr(run_round.ops, "run_event", lambda *args, **kwargs: None)
    for name in ("run_finders_parallel", "run_command", "commit_snapshot", "doc_stats"):
        monkeypatch.setattr(run_round, name,
                            lambda *a, **k: pytest.fail("no round step may run"))
    monkeypatch.setattr(sys, "argv", ["run_round.py", "--skip-discovery", "--allow-dirty"])

    assert run_round.main() == 1
    assert "rounds must --commit" in capsys.readouterr().err


def test_round_refuses_to_run_uncommitted_while_a_shadow_is_enabled(tmp_path, monkeypatch, capsys):
    (tmp_path / "workspace").mkdir()
    (tmp_path / "workspace" / ".pg-shadow").write_text("dsn\nschema\n")
    monkeypatch.setattr(run_round, "ROOT", tmp_path)
    monkeypatch.setattr(run_round.ops, "SNAPSHOTS", tmp_path / "workspace" / "round-snapshots")
    monkeypatch.setattr(run_round.ops, "WORKSPACE", tmp_path / "workspace")
    monkeypatch.setattr(run_round.ops, "run_event", lambda *args, **kwargs: None)
    for name in ("run_finders_parallel", "run_command", "commit_snapshot", "doc_stats"):
        monkeypatch.setattr(run_round, name,
                            lambda *a, **k: pytest.fail("no round step may run"))
    monkeypatch.setattr(sys, "argv", ["run_round.py", "--skip-discovery", "--allow-dirty"])

    assert run_round.main() == 1
    assert "rounds must --commit" in capsys.readouterr().err


def test_only_mutating_steps_receive_the_store_broker(tmp_path, monkeypatch):
    import store_broker
    for d in ("registry", "manifest"):
        (tmp_path / d).mkdir()
    (tmp_path / "README.md").write_text("x")
    (tmp_path / "pruned_urls.txt").write_text("")
    monkeypatch.setattr(run_round, "ROOT", tmp_path)
    monkeypatch.setattr(run_round.ops, "SNAPSHOTS", tmp_path / "workspace" / "round-snapshots")
    monkeypatch.setattr(run_round.ops, "WORKSPACE", tmp_path / "workspace")
    monkeypatch.setattr(run_round, "git_clean", lambda: True)
    monkeypatch.setattr(run_round, "load_backends", lambda: {})
    monkeypatch.setattr(run_round.rotation, "load", lambda: {})
    monkeypatch.setattr(run_round, "doc_stats", lambda view: (1, 10, 0))
    monkeypatch.setattr(run_round.ops, "run_event", lambda *args, **kwargs: None)
    seen = {}

    def record(step, cmd, env, run_id):
        seen[step] = env

    monkeypatch.setattr(run_round, "run_command", record)
    def gates(gates, env, run_id, envs=None):
        seen["gates"] = env
        seen["tests_gate"] = (envs or {}).get("tests")

    monkeypatch.setattr(run_round, "run_verify_parallel", gates)
    monkeypatch.setattr(sys, "argv", ["run_round.py", "--skip-discovery", "--skip-tests",
                                      "--allow-dirty", "--run-id", "r-env"])
    assert run_round.main() == 0
    for step in ("fetch", "prune", "clean"):
        assert seen[step][store_broker.BROKER_ENV] and seen[step][store_broker.ROUND_ENV] == "r-env"
    for step in ("stats", "gates"):
        assert store_broker.BROKER_ENV not in seen[step]
    tests_env = seen.pop("tests_gate")
    for e in seen.values():
        (holder,) = run_round.ops.inherited_holders(e[run_round.store.INHERITED_LOCK_ENV])
        assert holder["pid"] == os.getpid() and holder["run"] == "r-env"
    # pytest builds its own throwaway stores: it must not inherit the round's lock or broker
    assert run_round.store.INHERITED_LOCK_ENV not in tests_env
    assert store_broker.BROKER_ENV not in tests_env
