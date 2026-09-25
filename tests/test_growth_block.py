"""The recoverability growth block during a round (ADR 0001 stage 4 step 5, Codex review fix 10):
a growth round re-checks recoverability before every mutating step, right before its promotion
and periodically while it works; losing it aborts the round (nothing promoted), while standalone
repairs still promote. Real run_round.py processes over a throwaway PostgreSQL-authoritative
checkout (tests/staged_world.py); "not recoverable" is a file whose existence the test hook
(tests/authority_site) turns into a block. Opt-in: NEKAISE_PG_TEST_DSN."""
from __future__ import annotations

import os
import threading

import pytest

from runids import rid
from staged_world import World, entry

pytestmark = pytest.mark.skipif(not os.environ.get("NEKAISE_PG_TEST_DSN"),
                                reason="NEKAISE_PG_TEST_DSN not set")


@pytest.fixture
def world(tmp_path):
    w = World(tmp_path)
    yield w
    w.close()


def blocked_env(world):
    return {"NEKAISE_TEST_RECOVERABILITY_BLOCK_FILE": str(world.tmp / "archive-gone")}


def ok(result):
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-4000:]


def events(world, run_id):
    return [e for e in world.events(run_id) if e["event"] == "growth_blocked"]


def test_a_block_that_appears_during_the_gates_stops_the_promotion(world):
    world.build([entry(world.payloads, "ost-b-0")])
    world.finder([])
    # the test gate itself makes the archive "go away" while the round is being validated
    world.commit("a test that removes recoverability", **{
        "tests__test_ok.py": "import os, pathlib\n\ndef test_ok():\n    pathlib.Path(os.environ["
                             "'NEKAISE_TEST_RECOVERABILITY_BLOCK_FILE']).touch()\n"})
    run_id = rid("gb-promote")
    got = world.run("--run-id", run_id, env=blocked_env(world))
    assert got.returncode == 1 and "growth blocked before promotion" in got.stderr
    assert world.run_row(run_id)["status"] == "aborted" and world.generation() is None
    assert [e["when"] for e in events(world, run_id)] == ["before promotion"]


def test_a_block_between_steps_stops_the_round_before_its_next_step(world):
    world.build([entry(world.payloads, "ost-b-1")])
    world.finder([])
    world.payloads.hold["ost-b-1"] = threading.Event()
    run_id = rid("gb-step")
    proc = world.start("--run-id", run_id, env=blocked_env(world))
    world.wait_for(lambda: "ost-b-1" in world.payloads.waiting, what="the held download")
    (world.tmp / "archive-gone").touch()          # recoverability lost while fetching
    world.payloads.release()
    out, _ = proc.communicate(timeout=300)
    assert proc.returncode == 1 and "growth blocked before prune" in out
    assert world.run_row(run_id)["status"] == "aborted" and world.generation() is None


def test_the_watcher_stops_a_long_round_while_it_works(world):
    world.build([entry(world.payloads, "ost-b-2")])
    world.finder([])
    world.payloads.hold["ost-b-2"] = threading.Event()
    run_id = rid("gb-watch")
    proc = world.start("--run-id", run_id, env={**blocked_env(world),
                                                "NEKAISE_RECOVERABILITY_RECHECK_SECONDS": "1"})
    world.wait_for(lambda: "ost-b-2" in world.payloads.waiting, what="the held download")
    (world.tmp / "archive-gone").touch()
    try:
        out, _ = proc.communicate(timeout=120)     # the download is still held: the watcher acts
    finally:
        world.payloads.release()
    assert proc.returncode == 130, out[-3000:]
    assert world.run_row(run_id)["status"] == "aborted" and world.generation() is None
    assert [e["when"] for e in events(world, run_id)] == ["while the round worked"]


def test_repairs_still_promote_while_growth_is_blocked(world):
    world.build([entry(world.payloads, "ost-b-3")])
    world.finder([])
    ok(world.run("--run-id", rid("gb-base")))
    (world.tmp / "archive-gone").touch()
    refused = world.run("--run-id", rid("gb-refused"), env=blocked_env(world))
    assert refused.returncode == 1 and "growth blocked" in refused.stderr
    got = world.python("import blocklist\nprint(blocklist.add(['https://x.example/r/']))\n",
                       env=blocked_env(world))
    ok(got)
    assert world.generation() == 1

