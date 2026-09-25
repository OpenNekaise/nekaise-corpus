"""Stage 4 step 4 (ADR 0001): the maintainer under PostgreSQL authority — its snapshot window
recovers unfinished runs with the shared routine and pins the triage generation; its action
window's store mutations stage into ONE maintenance run that is gated and promoted, or aborted;
cancellation drains before anything is judged. The maintainer runs in-process over a throwaway
PostgreSQL-authoritative checkout (tests/staged_world.py); its agents are real child processes.
Opt-in: NEKAISE_PG_TEST_DSN."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

import maintainer
import store_broker
from runids import rid
from staged_world import DSN, SITE, World, entry, kill

pytestmark = pytest.mark.skipif(not os.environ.get("NEKAISE_PG_TEST_DSN"),
                                reason="NEKAISE_PG_TEST_DSN not set")


@pytest.fixture
def world(tmp_path, monkeypatch):
    w = World(tmp_path)
    w.build([entry(w.payloads, f"ost-m-{i}") for i in range(2)])
    w.finder([])
    root = w.root
    for name, value in (("ROOT", root), ("WORKSPACE", root / "workspace"), ("LOGS", root / "logs"),
                        ("HISTORY", root / "logs" / "maintainer_history.jsonl"),
                        ("BLOCKED", root / "workspace" / ".maintenance-blocked")):
        monkeypatch.setattr(maintainer, name, value)
    (root / "logs").mkdir(exist_ok=True)
    monkeypatch.setattr(maintainer.ops, "WORKSPACE", root / "workspace")
    monkeypatch.setattr(maintainer.ops, "RUN_LEDGER", root / "logs" / "run_history.jsonl")
    monkeypatch.setattr(maintainer.ops, "SNAPSHOTS", root / "workspace" / "round-snapshots")
    monkeypatch.setenv("MAINTAINER_LOCK_WAIT_SECONDS", "5")
    # the operator's environment under PostgreSQL authority (+ the test hook for agents)
    for key in ("NEKAISE_STORE", "NEKAISE_PG_DSN", "NEKAISE_PG_SCHEMA", "PYTHONPATH",
                "NEKAISE_TEST_AUTHORITY_RECORD", "NEKAISE_DISABLE_INDEX"):
        monkeypatch.setenv(key, w.env[key])
    yield w
    w.close()


CHILD = "import blocklist\nprint(blocklist.add({urls!r}))\n"


def agent(world, env, code, **kw):
    prelude = f"import sys\nsys.path.insert(0, {str(world.root / 'scripts')!r})\n"
    return subprocess.run([sys.executable, "-c", prelude + code], cwd=world.root, env=env,
                          capture_output=True, text=True, timeout=300, **kw)


def committed_known(world, urls) -> set:
    """URLs the committed generation knows, read as any process outside the window would (the
    window exports its run's read pin to its children through this process's environment)."""
    import store_staging
    saved = os.environ.pop(store_staging.STAGE_ENV, None)
    try:
        with world.store().read() as view:
            return set(view.known(urls=urls).urls)
    finally:
        if saved is not None:
            os.environ[store_staging.STAGE_ENV] = saved


def promoted_round(world, name):
    got = world.run("--run-id", rid(name))
    assert got.returncode == 0, got.stdout[-4000:] + got.stderr[-4000:]


def killed_round(world, name):
    world.payloads.hold["ost-m-late"] = threading.Event()
    world.finder([entry(world.payloads, "ost-m-late")])
    proc = world.start("--run-id", rid(name))
    world.wait_for(lambda: "ost-m-late" in world.payloads.waiting, what="the held download")
    kill(proc)
    world.payloads.release()
    world.finder([])


def test_the_snapshot_window_recovers_unfinished_runs_and_pins_the_triage_generation(world):
    promoted_round(world, "mt-g0")
    killed_round(world, "mt-dead")
    with maintainer.maintenance_window("snapshot") as window:
        assert window.staged and window.broker is None
        assert store_broker.BROKER_ENV not in os.environ     # nothing may mutate here
        assert any("unfinished staged run" in r for r in maintainer.block_reasons())
        assert maintainer.recover_pending_round() == rid("mt-dead")
        maintainer.verify_recovered_corpus()
        assert not any("unfinished" in r for r in maintainer.block_reasons())
        assert maintainer.pin_triage_generation("maintainer-t") == 0
        snapshot = maintainer.repo_snapshot("exit=0")
    assert snapshot["store"]["generation"] == 0 and snapshot["store"]["unfinished_runs"] == []
    assert world.run_row(rid("mt-dead"))["status"] == "aborted"
    assert world.q("SELECT generation, holder FROM generation_retention") == [(0, "maintainer-t")]
    # a round promoted during triage cannot fold past the pinned generation
    promoted_round(world, "mt-g1")
    assert world.generation() == 1
    assert world.q("SELECT generation FROM projection_state")[0][0] == 0   # held at the pin
    with maintainer.maintenance_window("snapshot"):
        maintainer.release_triage_pin("maintainer-t", 0)
    assert world.q("SELECT count(*) FROM generation_retention")[0][0] == 0


def test_action_window_mutations_are_one_gated_promoted_maintenance_run(world):
    promoted_round(world, "mt-g0")
    with maintainer.maintenance_window("action") as window:
        run_id = window.run.run.run_id
        env = maintainer.agent_env(Path(sys.executable))
        assert env[store_broker.BROKER_ENV]
        one = agent(world, env, CHILD.format(urls=["https://x.example/m1"]))
        two = agent(world, env, CHILD.format(urls=["https://x.example/m2"]))
        bare = {k: v for k, v in env.items() if not k.startswith("NEKAISE_STORE_")}
        bare["NEKAISE_STORE"] = env["NEKAISE_STORE"]
        refused = agent(world, bare, "import blocklist, store_broker\n"
                                     "blocklist.add(['https://x.example/m3'])\n")
        assert not committed_known(world, ["https://x.example/m1"])   # not before promotion
        outcome = window.conclude(ok=True)
    assert one.returncode == 0 and two.returncode == 0, one.stderr + two.stderr
    assert refused.returncode != 0 and "writer" in refused.stderr.lower()
    assert outcome == {"run": run_id, "status": "promoted", "generation": 1}
    row = world.run_row(run_id)
    assert (row["status"], row["kind"]) == ("promoted", "maintenance")
    assert world.q("SELECT gate FROM gate_receipts WHERE run_id = %s AND verdict = 'passed' "
                   "ORDER BY 1", [run_id]) == [("artifacts",), ("check",), ("contracts",),
                                               ("lint",)]
    assert committed_known(world, ["https://x.example/m1", "https://x.example/m2"]) == {
        "https://x.example/m1", "https://x.example/m2"}
    assert store_broker.BROKER_ENV not in os.environ


@pytest.mark.parametrize("how", ["failed", "unconcluded", "nothing"])
def test_an_unsuccessful_or_empty_action_window_aborts_its_run(world, how):
    promoted_round(world, "mt-g0")
    with pytest.raises(RuntimeError) if how == "unconcluded" else _nullcontext():
        with maintainer.maintenance_window("action") as window:
            run_id = window.run.run.run_id
            if how != "nothing":
                env = maintainer.agent_env(Path(sys.executable))
                assert agent(world, env, CHILD.format(urls=["https://x.example/z"])).returncode \
                    == 0
            if how == "unconcluded":
                raise RuntimeError("the maintainer was interrupted")
            outcome = window.conclude(ok=how != "failed")
            assert outcome["status"] == "aborted"
    assert world.run_row(run_id)["status"] == "aborted"
    assert world.generation() == 0
    with world.store().read() as view:
        assert not view.known(urls=["https://x.example/z"]).urls


class _nullcontext:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_a_timed_out_agent_s_batch_is_drained_before_the_window_is_judged(world, monkeypatch):
    """The agent is killed while the window's broker still stages its batch: conclude() drains
    first (the batch finishes), then the failed action's run is aborted — nothing is visible."""
    import store_staging
    promoted_round(world, "mt-g0")
    started, finished = threading.Event(), threading.Event()
    real = store_staging.stage_batch

    def slow(*args, **kw):
        started.set()
        time.sleep(1.0)
        out = real(*args, **kw)
        finished.set()
        return out
    monkeypatch.setattr(store_staging, "stage_batch", slow)
    with maintainer.maintenance_window("action") as window:
        run_id = window.run.run.run_id
        env = maintainer.agent_env(Path(sys.executable))
        prelude = f"import sys\nsys.path.insert(0, {str(world.root / 'scripts')!r})\n"
        proc = subprocess.Popen([sys.executable, "-c", prelude + CHILD.format(
            urls=["https://x.example/late"])], cwd=world.root, env=env)
        assert started.wait(30)
        proc.kill()
        proc.wait()
        assert not finished.is_set()
        outcome = window.conclude(ok=False)
        assert finished.is_set()
    assert outcome["status"] == "aborted"
    assert world.q("SELECT count(*) FROM batches WHERE run_id = %s", [run_id])[0][0] == 1
    with world.store().read() as view:
        assert not view.known(urls=["https://x.example/late"]).urls


def test_a_round_inside_the_postgres_window_is_refused_at_once(world):
    with maintainer.maintenance_window("action"):
        env = maintainer.agent_env(Path(sys.executable))
        started = time.monotonic()
        nested = subprocess.run([sys.executable, str(world.root / "scripts" / "run_round.py")],
                                cwd=world.root, env=env, capture_output=True, text=True,
                                timeout=60)
        elapsed = time.monotonic() - started
    assert nested.returncode == 2 and elapsed < 20 and "cannot run nested" in nested.stderr


def test_a_maintenance_pass_refreshes_generation_evidence_and_promotes_its_repair(world,
                                                                               monkeypatch):
    """The whole pass: triage pins generation G without holding the locks, a round promotes
    G+1 meanwhile, the action reacquires the locks with fresh code AND generation evidence,
    its repair is gated and promoted as a maintenance run, and the pin is released."""
    promoted_round(world, "mt-g0")
    monkeypatch.setattr(maintainer, "resolve_agent",
                        lambda name, override=None: Path("/bin") / name)
    def git(*args, **_kw):
        if args[0] == "fetch":
            return 0, ""
        got = subprocess.run(["git", *args], cwd=world.root, capture_output=True, text=True)
        return got.returncode, (got.stdout + got.stderr).strip()
    monkeypatch.setattr(maintainer, "git", git)
    seen = {}

    def run(command, **kwargs):
        kwargs["stdout_path"].write_text("")
        kwargs["stderr_path"].write_text("")
        if "--output-schema" in command:
            seen["triage"] = kwargs["prompt"]
            out = Path(command[command.index("--output-last-message") + 1])
            out.write_text(json.dumps({
                "needs_action": True, "action_kind": "repair", "urgency": "routine",
                "summary": "s", "evidence": ["e"], "proposed_actions": ["a"]}))
            world.finder([entry(world.payloads, "ost-m-new")])
            promoted_round(world, "mt-during")            # growth continues meanwhile
            return 0
        if "--print" in command:
            return 1
        seen["action"] = kwargs["prompt"]
        got = agent(world, kwargs["env"], CHILD.format(urls=["https://x.example/repair"]))
        assert got.returncode == 0, got.stderr
        return 0
    monkeypatch.setattr(maintainer, "run_command", run)
    assert maintainer.main() == 0
    assert '"triage_generation": 0' in seen["triage"]
    assert '"triage_generation": 0' in seen["action"]
    assert '"changed_since_triage": true' in seen["action"]
    history = [json.loads(l) for l in maintainer.HISTORY.read_text().splitlines()]
    run = history[-1]["maintenance_run"]
    assert run["status"] == "promoted" and run["generation"] == 2
    assert world.q("SELECT count(*) FROM generation_retention")[0][0] == 0
    with world.store().read() as view:
        assert view.generation == 2 and view.known(urls=["https://x.example/repair"]).urls
