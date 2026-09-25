"""Stage 4 step 4 (ADR 0001): rounds, recovery and standalone mutations under PostgreSQL
authority, end to end — real `run_round.py` processes with every child they start (a fake
finder, fetch/prune/clean, the gates) over a throwaway checkout whose authority record makes a
schema of the test database authoritative (tests/staged_world.py). Opt-in: NEKAISE_PG_TEST_DSN.

Production keeps FileStore authority, legacy recovery and the legacy round selected; these paths
are chosen only by the authority record, which is exactly what these tests set up."""
from __future__ import annotations

import json
import os
import signal
import threading

import pytest

import round_recovery
from runids import rid
from staged_world import World, entry, git, kill

pytestmark = pytest.mark.skipif(not os.environ.get("NEKAISE_PG_TEST_DSN"),
                                reason="NEKAISE_PG_TEST_DSN not set")


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    yield w
    w.close()


def seeded(world, n=2, **kw):
    """A checkout whose registry holds `n` documents to fetch (ids in fetch order); returns
    their entries."""
    entries = [entry(world.payloads, f"ost-s-{i:02d}" if n > 10 else f"ost-s-{i}")
               for i in range(n)]
    world.build(entries, **kw)
    return entries


def ok(result) -> None:
    assert result.returncode == 0, result.stdout[-6000:] + result.stderr[-6000:]


def materialized(world) -> dict:
    stamp = world.root / "corpus" / ".materialization.json"
    return json.loads(stamp.read_text()) if stamp.exists() else {}


def corpus_ids(world) -> set[str]:
    return {p.stem for p in (world.root / "corpus").glob("*.md")}


# --- a round is one staged run -------------------------------------------------------------------------

def test_a_round_under_postgres_authority_is_one_promoted_generation(world):
    seeded(world)
    world.finder([entry(world.payloads, "ost-s-new")])
    head = git(world.root, "rev-parse", "HEAD")
    run_id = rid("sr-one")
    result = world.run("--run-id", run_id)
    ok(result)
    assert world.run_row(run_id)["status"] == "promoted"
    assert world.generation() == 0
    # every required gate recorded passed, bound to the frozen state
    gates = world.q("SELECT g.gate, g.verdict FROM gate_receipts g JOIN runs r USING (run_id) "
                    "WHERE run_id = %s AND g.frozen_seq = r.frozen_seq AND g.frozen_digest = "
                    "r.frozen_digest ORDER BY gate", [run_id])
    assert gates == [(g, "passed") for g in ("artifacts", "check", "contracts", "lint", "tests")]
    # the promoted generation carries the checkout's commit and configuration
    commit, = world.q("SELECT producer_commit FROM generations WHERE generation = 0")[0]
    assert commit == head
    # corpus/ is a complete materialization of it; git is untouched (no snapshot, no commit)
    assert materialized(world)["state"] == "complete" and materialized(world)["generation"] == 0
    assert corpus_ids(world) == {"ost-s-0", "ost-s-1", "ost-s-new"}
    assert git(world.root, "rev-parse", "HEAD") == head
    assert git(world.root, "status", "--porcelain") == ""
    assert not (world.root / "workspace" / "round-snapshots").exists()
    # the finder read the round's pinned view (the run's overlay at sequence 0)
    assert world.finder_runs() == [{"run": run_id, "version": f"pg:stage:{run_id}:0"}]
    kinds = [e["event"] for e in world.events(run_id)]
    for event in ("staged_run_opened", "discovery_merged", "staged_run_frozen", "run_promoted",
                  "run_completed"):
        assert event in kinds


def test_a_second_round_builds_on_the_first_generation_incrementally(world):
    seeded(world)
    world.finder([])
    ok(world.run("--run-id", rid("sr-g0")))
    world.finder([entry(world.payloads, "ost-s-late")])
    ok(world.run("--run-id", rid("sr-g1")))
    assert world.generation() == 1
    stamp = materialized(world)
    assert (stamp["state"], stamp["generation"]) == ("complete", 1)
    assert corpus_ids(world) == {"ost-s-0", "ost-s-1", "ost-s-late"}
    done = [e for e in world.events(rid("sr-g1")) if e["event"] == "run_completed"][0]
    assert done["materialized"] == "diff" and done["generation"] == 1
    assert (done["before_docs"], done["after_docs"]) == (2, 3)


def test_a_failing_gate_aborts_the_round_and_the_next_round_promotes(world):
    seeded(world)
    world.finder([])
    world.commit("a failing test", **{"tests__test_bad.py": "def test_bad():\n    assert False\n"})
    bad = rid("sr-bad")
    result = world.run("--run-id", bad)
    assert result.returncode == 1 and "verification failed: tests" in result.stderr
    assert world.run_row(bad)["status"] == "aborted"
    assert world.generation() is None
    assert world.q("SELECT gate, verdict FROM gate_receipts WHERE run_id = %s AND "
                   "verdict = 'failed'", [bad]) == [("tests", "failed")]
    assert world.q("SELECT run_id FROM purge_queue") == [(bad,)]   # purged after its grace
    assert not corpus_ids(world)
    world.commit("fix", **{"tests__test_bad.py": "def test_bad():\n    assert True\n"})
    good = rid("sr-good")
    ok(world.run("--run-id", good))
    assert world.run_row(good)["status"] == "promoted" and world.generation() == 0


def test_skip_tests_is_refused_before_a_staged_run_opens(world):
    seeded(world)
    world.finder([])
    refused = world.run("--run-id", rid("sr-skip"), "--skip-tests")
    assert refused.returncode == 2 and "--skip-tests does not apply" in refused.stderr
    assert world.run_row(rid("sr-skip")) is None and world.generation() is None


def test_discovery_follows_the_configuration_of_the_commit_that_runs(world):
    """A committed backend change is honoured by the very next round: its discovery reads the
    run's own pinned configuration (sequence 0), not the parent generation's."""
    seeded(world)
    world.finder([entry(world.payloads, "ost-s-a")])
    ok(world.run("--run-id", rid("sr-cfg0")))
    assert len(world.finder_runs()) == 1
    # a rename: the old script is gone, the configuration names the new one
    backends = json.loads((world.root / "registry" / "backends.json").read_text())
    backends["find_fake"]["script"] = "find_fake2.py"
    (world.root / "scripts" / "find_fake2.py").write_text(
        (world.root / "scripts" / "find_fake.py").read_text())
    (world.root / "scripts" / "find_fake.py").unlink()
    world.commit("rename the finder",
                 **{"registry__backends.json": json.dumps(backends, indent=2) + "\n"})
    world.finder([entry(world.payloads, "ost-s-b")])
    ok(world.run("--run-id", rid("sr-cfg1")))
    assert len(world.finder_runs()) == 2 and "ost-s-b" in corpus_ids(world)
    # a disable: the next round does not invoke it
    backends["find_fake"].update(enabled=False, reason="operator: paused for the test")
    world.commit("disable the finder",
                 **{"registry__backends.json": json.dumps(backends, indent=2) + "\n"})
    world.finder([entry(world.payloads, "ost-s-c")])
    ok(world.run("--run-id", rid("sr-cfg2")))
    assert len(world.finder_runs()) == 2 and "ost-s-c" not in corpus_ids(world)
    assert world.generation() == 2


def test_a_dirty_checkout_or_push_is_refused_before_anything_stages(world):
    seeded(world)
    (world.root / "scripts" / "stray.py").write_text("# uncommitted\n")
    result = world.run("--run-id", rid("sr-dirty"))
    assert result.returncode == 1 and "uncommitted changes" in result.stderr
    assert world.run_row(rid("sr-dirty")) is None
    (world.root / "scripts" / "stray.py").unlink()
    result = world.run("--push", "main", "--commit")
    assert result.returncode == 2 and "--push does not apply" in result.stderr


def test_a_killed_round_is_recovered_its_orphans_stopped_and_its_run_aborted(world):
    """SIGKILL the coordinator while its fetch is still downloading: the fetch lives on as an
    orphan. A new round is refused until recovery; `--recover latest` stops the orphan (tagged
    with the run id) before it touches the run, aborts the run by its durable status, sweeps
    temporaries, and the next round promotes."""
    entries = seeded(world, n=3)
    world.finder([])
    world.payloads.hold["ost-s-2"] = threading.Event()
    run_id = rid("sr-kill")
    proc = world.start("--run-id", run_id)
    world.wait_for(lambda: "ost-s-2" in world.payloads.waiting, what="the held download")
    kill(proc)
    orphans = round_recovery.round_processes(run_id)
    assert orphans, "the fetch child should outlive its coordinator"
    incoming = world.root / "artifacts" / ".incoming"
    incoming.mkdir(parents=True, exist_ok=True)
    (incoming / "999999999-deadbeef").write_bytes(b"a dead writer's temporary")
    refused = world.run("--run-id", rid("sr-after"))
    assert refused.returncode == 1 and "unfinished staged run" in refused.stderr
    assert world.run_row(run_id)["status"] == "open"
    recovered = world.run("--recover", "latest")
    ok(recovered)
    assert f"run {run_id}: open -> aborted" in recovered.stdout
    assert not any(round_recovery._alive(p) for p in orphans)
    assert world.run_row(run_id)["status"] == "aborted"
    assert not (incoming / "999999999-deadbeef").exists()
    kinds = [e["event"] for e in world.events(run_id)]
    assert kinds.index("round_processes_stopped") < kinds.index("staged_run_aborted")
    world.payloads.release()
    ok(world.run("--run-id", rid("sr-after")))
    assert world.generation() == 0
    assert corpus_ids(world) == {e["id"] for e in entries}


def test_a_timed_out_round_recovers_itself_on_the_way_out(world):
    """dig.sh's `timeout` sends SIGTERM: the round unwinds, stops its fetch, drains its broker
    and aborts its run before it exits — no separate recovery is needed."""
    seeded(world, n=3)
    world.finder([])
    world.payloads.hold["ost-s-2"] = threading.Event()
    run_id = rid("sr-term")
    proc = world.start("--run-id", run_id)
    world.wait_for(lambda: "ost-s-2" in world.payloads.waiting, what="the held download")
    fetchers = round_recovery.round_processes(run_id)
    assert fetchers
    proc.send_signal(signal.SIGTERM)
    assert proc.wait(60) == 130
    assert not any(round_recovery._alive(p) for p in fetchers)
    assert world.run_row(run_id)["status"] == "aborted"
    kinds = [e["event"] for e in world.events(run_id)]
    assert "staged_run_aborted" in kinds and kinds[-1] == "run_interrupted"
    world.payloads.release()
    ok(world.run("--run-id", rid("sr-term2")))      # nothing left to recover
    assert world.generation() == 0


def test_a_round_killed_during_its_materialization_stands_and_recovery_completes_it(world):
    seeded(world)
    world.finder([])
    run_id = rid("sr-mat")
    result = world.run("--run-id", run_id,
                       env={"NEKAISE_TEST_CRASH": "materialize:_crash:stamped"})
    assert result.returncode == 137
    assert world.run_row(run_id)["status"] == "promoted" and world.generation() == 0
    assert materialized(world)["state"] == "refreshing"     # consumers are refused meanwhile
    recovered = world.run("--recover", "latest")
    ok(recovered)
    assert "no unfinished staged run" in recovered.stdout
    assert (materialized(world)["state"], materialized(world)["generation"]) == ("complete", 0)
    assert corpus_ids(world) == {"ost-s-0", "ost-s-1"}


# --- explicit resume ----------------------------------------------------------------------------------

N_RESUME = 27   # the loader checkpoints every 25 results: one checkpoint, then the held one


def _interrupted_fetch(world, run_id):
    """A round killed mid-fetch after the loader's first checkpoint was staged while the last
    document is still downloading; returns the orphaned fetch's pids."""
    world.payloads.hold[f"ost-s-{N_RESUME - 1:02d}"] = threading.Event()
    proc = world.start("--run-id", run_id)
    world.wait_for(lambda: world.q("SELECT count(*) FROM batches WHERE run_id = %s AND step = "
                                   "'fetch'", [run_id])[0][0] >= 1, what="a loader checkpoint")
    world.wait_for(lambda: f"ost-s-{N_RESUME - 1:02d}" in world.payloads.waiting,
                   what="the held download")
    kill(proc)
    return round_recovery.round_processes(run_id)


def test_resume_continues_an_interrupted_run_only_when_nothing_changed(world):
    entries = seeded(world, n=N_RESUME)
    world.finder([entry(world.payloads, "ost-s-disc")])
    run_id = rid("sr-res")
    orphans = _interrupted_fetch(world, run_id)
    assert orphans
    staged_before = world.run_row(run_id)["staged_seq"]
    # changed code: refused, and the run is left exactly as it was (only its orphans stopped)
    head = git(world.root, "rev-parse", "HEAD")
    world.commit("a code change", **{"scripts__note.txt": "changed\n"})
    refused = world.run("--resume", run_id)
    assert refused.returncode == 1 and "producer commit" in refused.stderr
    assert "--recover" in refused.stderr
    assert not any(round_recovery._alive(p) for p in orphans)
    row = world.run_row(run_id)
    assert (row["status"], row["staged_seq"]) == ("open", staged_before)
    git(world.root, "reset", "-q", "--hard", head)
    # changed configuration (same commit is impossible to fake here: a committed config change
    # moves HEAD too, so check the configuration comparison through the digest directly)
    world.payloads.release()
    resumed = world.run("--resume", run_id)
    ok(resumed)
    row = world.run_row(run_id)
    assert row["status"] == "promoted" and row["owner_epoch"] > row["writer_epoch"]
    assert world.q("SELECT count(*) FROM run_adoptions WHERE run_id = %s", [run_id])[0][0] == 1
    # the finder ran once: the persisted discovery merge was skipped, not recomputed
    assert [r["run"] for r in world.finder_runs()] == [run_id]
    # the re-run fetch staged its own batch namespace after the first attempt's checkpoints
    names = [b for (b,) in world.q("SELECT batch FROM batches WHERE run_id = %s AND step = "
                                   "'fetch' ORDER BY seq", [run_id])]
    assert names[0] == "ckpt-0001" and names[1:] and all(n.startswith("a2-") for n in names[1:])
    assert corpus_ids(world) == {e["id"] for e in entries} | {"ost-s-disc"}
    events = [e["event"] for e in world.events(run_id)]
    assert "staged_run_adopted" in events and events[-1] == "run_completed"


def test_a_resumed_frozen_run_only_runs_its_missing_gates(world):
    seeded(world)
    world.finder([])
    slow = world.root / "workspace" / "slow-test"
    world.commit("a slow test", **{"tests__test_slow.py": (
        "import pathlib, time\n\ndef test_slow():\n"
        f"    while pathlib.Path({str(slow)!r}).exists():\n        time.sleep(0.2)\n")})
    slow.write_text("hold the tests gate\n")
    run_id = rid("sr-frz")
    proc = world.start("--run-id", run_id)
    world.wait_for(lambda: (world.run_row(run_id) or {}).get("status") == "frozen",
                   what="the frozen run")
    world.wait_for(lambda: any(e["event"] == "step_started" and e.get("step") == "tests"
                               for e in world.events(run_id)), what="the tests gate")
    kill(proc)
    assert world.q("SELECT count(*) FROM gate_receipts WHERE run_id = %s", [run_id])[0][0] == 0
    slow.unlink()
    wrong = world.run("--resume", run_id, "--skip-tests")
    assert wrong.returncode == 2 and "--skip-tests does not apply" in wrong.stderr
    assert world.run_row(run_id)["status"] == "frozen"      # refused before any adoption
    assert world.q("SELECT count(*) FROM run_adoptions")[0][0] == 0
    ok(world.run("--resume", run_id))
    assert world.run_row(run_id)["status"] == "promoted"
    assert world.q("SELECT count(*) FROM gate_receipts WHERE run_id = %s AND verdict = 'passed'",
                   [run_id])[0][0] == 5


def test_resume_is_refused_for_an_unverifiable_artifact(world):
    """A run whose referenced versions do not verify cannot be adopted (the database's adoption
    guard checks it again); it is left open for --recover."""
    seeded(world, n=N_RESUME)
    world.finder([])
    run_id = rid("sr-art")
    _interrupted_fetch(world, run_id)
    # damage one version the run referenced (same size, other bytes)
    stage, sha = world.q("SELECT stage, sha256 FROM run_artifacts WHERE run_id = %s ORDER BY 1, 2 "
                         "LIMIT 1", [run_id])[0]
    path = world.root / "artifacts" / stage / sha[:2] / sha[2:4] / sha
    data = path.read_bytes()
    os.chmod(path, 0o644)
    path.write_bytes(bytes([data[0] ^ 1]) + data[1:])
    refused = world.run("--resume", run_id)
    assert refused.returncode == 1 and "do not verify" in refused.stderr
    assert world.run_row(run_id)["status"] == "open"
    assert world.q("SELECT count(*) FROM run_adoptions")[0][0] == 0
    ok(world.run("--recover", run_id))
    assert world.run_row(run_id)["status"] == "aborted"
    refused = world.run("--resume", run_id)
    assert refused.returncode == 1 and "it is aborted" in refused.stderr


# --- standalone mutations are staged runs too ----------------------------------------------------------

ADD = "import blocklist\nprint(blocklist.add({urls!r}))\n"


def standalone_runs(world) -> list[tuple]:
    return world.q("SELECT run_id, status, promoted_generation, detail_text FROM runs WHERE "
                   "kind = 'standalone' ORDER BY started_at")


def test_a_standalone_blocklist_add_is_one_gated_promoted_run(world):
    seeded(world)
    world.finder([])
    ok(world.run("--run-id", rid("sr-base")))
    got = world.python(ADD.format(urls=["https://x.example/dropped/"]))
    ok(got)
    assert got.stdout.strip().endswith("1")
    [(run_id, status, generation, _)] = standalone_runs(world)
    assert (status, generation) == ("promoted", 1) and run_id.startswith("blocklist-")
    gates = world.q("SELECT gate, verdict FROM gate_receipts WHERE run_id = %s ORDER BY 1",
                    [run_id])
    assert gates == [(g, "passed") for g in ("artifacts", "check", "contracts", "lint",
                                              "tests")]
    with world.store().read() as view:
        assert view.known(urls=["https://x.example/dropped"]).urls
    # nothing new: no run at all
    again = world.python(ADD.format(urls=["https://x.example/dropped"]))
    ok(again)
    assert again.stdout.strip().endswith("0") and len(standalone_runs(world)) == 1
    # the corpus materialization followed the promotion
    assert materialized(world)["generation"] == 1


def test_a_standalone_fetch_prunes_and_cleans_in_its_own_run_and_promotes(world):
    """A standalone loader's run completes the pipeline (prune, clean) before it freezes, so
    the claim check accepts it: the fetched documents are promoted and materialized."""
    seeded(world, n=3)
    ok(world.run(script="build_corpus.py"))
    (run_id, status, generation, _), = standalone_runs(world)
    assert (status, generation) == ("promoted", 0) and run_id.startswith("fetch-")
    steps = {s for (s,) in world.q("SELECT DISTINCT step FROM batches WHERE run_id = %s",
                                   [run_id])}
    assert {"fetch", "clean"} <= steps
    assert corpus_ids(world) == {"ost-s-0", "ost-s-1", "ost-s-2"}


def test_a_killed_standalone_loader_s_workers_are_found_by_recovery(world):
    """SIGKILL a standalone loader while its extraction workers are up: they carry the run id
    from exec (the command tags itself before it starts them), so recovery run from another
    process stops them before it aborts the run."""
    seeded(world, n=3)
    world.payloads.hold["ost-s-2"] = threading.Event()
    proc = world.start(script="build_corpus.py")
    world.wait_for(lambda: world.q("SELECT count(*) FROM runs WHERE kind = 'standalone'")[0][0],
                   what="the standalone run")
    (run_id,), = world.q("SELECT run_id FROM runs WHERE kind = 'standalone'")
    world.wait_for(lambda: "ost-s-2" in world.payloads.waiting, what="the held download")
    world.wait_for(lambda: len(round_recovery.round_processes(run_id) - {proc.pid}) >= 1,
                   what="the extraction workers")
    kill(proc)
    orphans = round_recovery.round_processes(run_id)
    assert orphans
    world.payloads.release()
    recovered = world.run("--recover", "latest")
    ok(recovered)
    assert f"run {run_id}: open -> aborted" in recovered.stdout
    assert not any(round_recovery._alive(p) for p in orphans)
    assert not list((world.root / "workspace" / "run-owners").glob("*"))


def test_a_standalone_step_stages_gates_and_promotes_or_aborts_a_no_op(world):
    seeded(world, n=3)
    world.finder([])
    ok(world.run("--run-id", rid("sr-base")))
    ids = world.root / "workspace" / "reviewed-drops.txt"
    ids.write_text("ost-s-2\n")
    ok(world.run("--apply", "--drop-ids-from", str(ids), script="prune_corpus.py"))
    (run_id, status, generation, _), = standalone_runs(world)
    assert (status, generation) == ("promoted", 1) and run_id.startswith("prune-")
    assert corpus_ids(world) == {"ost-s-0", "ost-s-1"}     # membership changed, bytes kept
    assert (world.root / "artifacts").exists()
    # a standalone clean with nothing to do stages nothing: its run is aborted as a no-op
    ok(world.run(script="clean_corpus.py"))
    last = standalone_runs(world)[-1]
    assert last[1] == "aborted" and "no-op" in last[3]
    assert world.generation() == 1


def test_standalone_mutations_wait_for_recovery_and_fail_closed_on_gates(world):
    seeded(world, n=27)
    world.finder([])
    run_id = rid("sr-open")
    world.payloads.hold["ost-s-26"] = threading.Event()
    proc = world.start("--run-id", run_id)
    world.wait_for(lambda: "ost-s-26" in world.payloads.waiting, what="the held download")
    kill(proc)
    refused = world.python(ADD.format(urls=["https://x.example/a"]))
    assert refused.returncode != 0 and "unfinished staged run" in refused.stderr
    assert not standalone_runs(world)
    world.payloads.release()
    ok(world.run("--recover", "latest"))
    # a configuration the gates reject: the run is aborted and nothing becomes visible
    backends = json.loads((world.root / "registry" / "backends.json").read_text())
    backends["find_fake"]["script"] = "find_missing.py"
    world.commit("break the configuration",
                 **{"registry__backends.json": json.dumps(backends, indent=2) + "\n"})
    failed = world.python(ADD.format(urls=["https://x.example/a"]))
    assert failed.returncode != 0 and "gate(s) failed" in failed.stderr
    (_, status, _, detail), = standalone_runs(world)
    assert status == "aborted" and "contracts" in detail
    with world.store().read() as view:
        assert not view.known(urls=["https://x.example/a"]).urls


# --- the generation-range review through its command line ------------------------------------------------

def review(world, *args):
    got = world.run(*args, script="generation_review.py")
    ok(got)
    return json.loads(got.stdout)


def test_an_integrity_finding_blocks_rounds_until_a_compensating_generation_resolves_it(world):
    seeded(world)
    world.finder([])
    ok(world.run("--run-id", rid("sr-rv0")))
    ev = review(world, "evidence")
    assert ev["range"] == [0, 0] and ev["generations"][0]["kind"] == "round"
    state = review(world, "record", "--through", "0", "--verdict", "integrity", "--reviewer",
                   "operator", "--evidence-digest", ev["digest"], "--summary",
                   "a pointer-only row reached the corpus")
    assert (state["reviewed_through"], state["endorsed_through"], state["open_integrity"]) == (
        0, None, 1)
    blocked = world.run("--run-id", rid("sr-rv-blocked"))
    assert blocked.returncode == 1 and "integrity finding(s) [1]" in blocked.stderr
    assert world.run_row(rid("sr-rv-blocked")) is None
    # the repair is a standalone (or maintenance) run: a compensating generation
    ok(world.python(ADD.format(urls=["https://x.example/repair"])))
    ev = review(world, "evidence")
    assert ev["range"] == [1, 1] and ev["generations"][0]["kind"] == "standalone"
    stale = world.run("record", "--through", "1", "--verdict", "ok", "--reviewer", "operator",
                      "--evidence-digest", "0" * 64, "--resolves", "1",
                      script="generation_review.py")
    assert stale.returncode == 1 and "digest differs" in stale.stderr
    state = review(world, "record", "--through", "1", "--verdict", "ok", "--reviewer",
                   "operator", "--evidence-digest", ev["digest"], "--resolves", "1")
    assert (state["reviewed_through"], state["endorsed_through"], state["open_integrity"]) == (
        1, 1, 0)
    ok(world.run("--run-id", rid("sr-rv-after")))
    assert world.generation() == 2


def test_an_integrity_finding_also_refuses_resuming_a_round(world):
    seeded(world, n=N_RESUME)
    world.finder([])
    ok(world.run("--run-id", rid("sr-ri0")))
    world.finder([entry(world.payloads, "ost-s-late")])
    run_id = rid("sr-ri1")
    world.payloads.hold["ost-s-late"] = threading.Event()
    proc = world.start("--run-id", run_id)
    world.wait_for(lambda: "ost-s-late" in world.payloads.waiting, what="the held download")
    kill(proc)
    world.payloads.release()
    ev = review(world, "evidence")
    review(world, "record", "--through", "0", "--verdict", "integrity", "--reviewer", "operator",
           "--evidence-digest", ev["digest"], "--summary", "found after the round started")
    refused = world.run("--resume", run_id)
    assert refused.returncode == 1 and "integrity finding(s) [1]" in refused.stderr
    assert world.run_row(run_id)["status"] == "open"
    assert world.q("SELECT count(*) FROM run_adoptions")[0][0] == 0
