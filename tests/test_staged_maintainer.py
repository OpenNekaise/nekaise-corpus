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
from staged_world import World, entry, kill

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
                                               ("lint",), ("tests",)]
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


KILLED_MAINTAINER = """
import os, subprocess, sys, time
from pathlib import Path
sys.path.insert(0, {scripts!r})
import maintainer
with maintainer.maintenance_window("action") as window:
    env = maintainer.agent_env(Path(sys.executable))
    agent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], env=env,
                             start_new_session=True)
    forked = os.fork()
    if forked == 0:
        time.sleep(120)
        os._exit(0)
    print(window.run.run.run_id, agent.pid, forked, flush=True)
    time.sleep(120)
"""


def test_a_killed_maintainer_s_agent_and_forks_are_found_by_recovery(world):
    """SIGKILL the maintainer while its action agent runs: the agent (exec'd with the window
    run's NEKAISE_RUN_ID) and a fork of the maintainer (holding the run's ownership mark) are
    found and stopped by a recovery run from another process, which then aborts the run."""
    promoted_round(world, "mt-g0")
    proc = subprocess.Popen([sys.executable, "-c", KILLED_MAINTAINER.format(
        scripts=str(world.root / "scripts"))], cwd=world.root, text=True,
        env={**world.env, "MAINTAINER_LOCK_WAIT_SECONDS": "5"}, stdout=subprocess.PIPE)
    world.procs.append(proc)
    line = []
    while len(line) != 3:
        text = proc.stdout.readline()
        assert text, "the maintainer child did not open its window"
        line = text.split() if not text.startswith("Maintenance") else []
    run_id, agent_pid, forked = line[0], int(line[1]), int(line[2])
    kill(proc)
    assert world.run_row(run_id)["status"] == "open"
    assert {agent_pid, forked} <= maintainer.round_recovery.round_processes(run_id)
    # the fork inherited the dead maintainer's database session: the writer lock is still held,
    # so recovery must stop the dead coordinator's orphans BEFORE it can take ownership
    with pytest.raises(maintainer.store.WriterError, match="held by another session"):
        with world.store().writer():
            pass
    recovered = world.run("--recover", "latest")
    assert recovered.returncode == 0, recovered.stderr
    assert f"run {run_id}: open -> aborted" in recovered.stdout
    assert not maintainer.round_recovery._alive(agent_pid)
    assert not maintainer.round_recovery._alive(forked)


def test_a_round_inside_the_postgres_window_is_refused_at_once(world):
    with maintainer.maintenance_window("action"):
        env = maintainer.agent_env(Path(sys.executable))
        started = time.monotonic()
        nested = subprocess.run([sys.executable, str(world.root / "scripts" / "run_round.py")],
                                cwd=world.root, env=env, capture_output=True, text=True,
                                timeout=60)
        elapsed = time.monotonic() - started
    assert nested.returncode == 2 and elapsed < 20 and "cannot run nested" in nested.stderr


@pytest.mark.parametrize("review", ["ok", "stale", "other-range", "none", "survivor"])
def test_a_maintenance_pass_refreshes_generation_evidence_and_promotes_its_repair(world,
                                                                               monkeypatch,
                                                                               review):
    """The whole pass: triage pins generation G without holding the locks and gets the
    generation-range review evidence up to G, a round promotes G+1 meanwhile, the action
    reacquires the locks with fresh code AND generation evidence, its repair is gated and
    promoted as a maintenance run (a compensating generation), its verdict file is recorded only
    when its digest is the evidence's, and the pin is released."""
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
        if review in ("ok", "stale", "other-range"):
            evidence, = maintainer.LOGS.glob("maintainer-*/review-evidence.json")
            ev = json.loads(evidence.read_text())
            if review == "other-range":   # evidence the agent computed itself, for more
                import generation_review
                import store
                with world.store()._connect() as conn:
                    db = generation_review._database_evidence(conn, 0, 1)
                ev = {"range": db["range"], "digest": store._digest(db)}
            Path(kwargs["env"]["NEKAISE_REVIEW_VERDICT_FILE"]).write_text(json.dumps({
                "through": ev["range"][1], "verdict": "ok", "summary": "reviewed",
                "evidence_digest": ev["digest"] if review != "stale" else "0" * 64}))
        if review == "survivor":   # a tool the agent left running (stopped by the supervisor)
            kwargs["report"]["survivors"] = [os.getpid()]
        return 0
    monkeypatch.setattr(maintainer, "run_command", run)
    assert maintainer.main() == 0
    assert '"triage_generation": 0' in seen["triage"]
    assert "GENERATION-RANGE review" in seen["triage"] and "GENERATION-RANGE" in seen["action"]
    assert '"range": [\n      0,\n      0\n    ]' in seen["triage"]
    assert '"triage_generation": 0' in seen["action"]
    assert '"changed_since_triage": true' in seen["action"]
    history = [json.loads(l) for l in maintainer.HISTORY.read_text().splitlines()]
    run = history[-1]["maintenance_run"]
    assert world.q("SELECT count(*) FROM generation_retention")[0][0] == 0
    if review == "survivor":   # the action did not finish: its repair is not promoted
        assert run["status"] == "aborted" and "did not succeed" in run["reason"]
        with world.store().read() as view:
            assert view.generation == 1
            assert not view.known(urls=["https://x.example/repair"]).urls
        return
    assert run["status"] == "promoted" and run["generation"] == 2
    with world.store().read() as view:
        assert view.generation == 2 and view.known(urls=["https://x.example/repair"]).urls
    reviewed = world.q("SELECT reviewed_through, endorsed_through FROM review_state")[0]
    if review == "ok":
        assert history[-1]["review"]["recorded"] == "ok" and reviewed == (0, 0)
    elif review == "stale":
        assert "this pass showed" in history[-1]["review"]["refused"]
        assert reviewed == (None, None)
    elif review == "other-range":   # a digest the agent computed for more than it was shown
        assert "through 1" in history[-1]["review"]["refused"] and reviewed == (None, None)
    else:
        assert "review" not in history[-1] and reviewed == (None, None)


def test_run_command_reports_the_tools_an_agent_left_running(world, tmp_path):
    report = {}
    code = maintainer.run_command(
        ["bash", "-c", "sleep 30 & exit 0"], prompt=None, timeout=30,
        stdout_path=tmp_path / "out", stderr_path=tmp_path / "err", report=report)
    assert code == 0 and len(report["survivors"]) == 1
    assert not maintainer.round_recovery._alive(report["survivors"][0])   # and it was stopped


def test_a_window_whose_code_changed_does_not_promote_its_mutations(world):
    promoted_round(world, "mt-g0")
    with maintainer.maintenance_window("action") as window:
        run_id = window.run.run.run_id
        env = maintainer.agent_env(Path(sys.executable))
        assert agent(world, env, CHILD.format(urls=["https://x.example/c"])).returncode == 0
        world.commit("the agent changes code", **{"scripts__note.txt": "new\n"})
        outcome = window.conclude(ok=True)
    assert outcome["status"] == "aborted" and "producer commit" in outcome["reason"]
    assert world.run_row(run_id)["status"] == "aborted" and world.generation() == 0


def test_a_failing_conclusion_is_recovered_before_growth_is_judged(world, monkeypatch):
    import staged_runs
    promoted_round(world, "mt-g0")

    def crash(*_a, **_k):
        raise RuntimeError("a gate process could not start")
    monkeypatch.setattr(staged_runs, "run_gates", crash)
    with maintainer.maintenance_window("action") as window:
        run_id = window.run.run.run_id
        env = maintainer.agent_env(Path(sys.executable))
        assert agent(world, env, CHILD.format(urls=["https://x.example/f"])).returncode == 0
        with pytest.raises(RuntimeError, match="could not start"):
            window.conclude(ok=True)
        assert world.run_row(run_id)["status"] == "aborted"
        assert not any("unfinished" in r for r in maintainer.block_reasons())
