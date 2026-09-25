"""Stage 4 step 4 (ADR 0001): the database half of recovery — schema v7's run ownership and
logged adoption (explicit resume), the purge queue, the shared staged recovery routine
(round_recovery.recover_staged: processes stopped, broker drained, durable status decides, an
unknown outcome mutates nothing), a promotion whose reply is lost, bounded housekeeping, and
the v6 -> v7 migration with its backfills. Opt-in: NEKAISE_PG_TEST_DSN."""
from __future__ import annotations

import importlib.util
import json
import os
import signal
import subprocess
import sys
import uuid
from contextlib import contextmanager
from pathlib import Path

import pytest

import artifact_store
import round_recovery
import run_ownership
import staged_runs
import store
import store_broker
import store_staging
from runids import rid
from test_store_contract import write_config
from test_store_pg_staging import mrow, q, recorded, version_of

pytestmark = pytest.mark.skipif(not os.environ.get("NEKAISE_PG_TEST_DSN"),
                                reason="NEKAISE_PG_TEST_DSN not set")
DSN = os.environ.get("NEKAISE_PG_TEST_DSN", "")
REPO = Path(__file__).resolve().parents[1]
V6_COMMIT = "5dfb9ed77b"  # last commit whose store_pg.py writes schema version 6 (step 3)
SHA = "0" * 40
OTHER = "1" * 40
OWNER_ENV = "NEKAISE_RUN_OWNER"


def sqlerr():
    import psycopg
    return psycopg.Error


@pytest.fixture
def pg(tmp_path, monkeypatch):
    import store_pg
    root = tmp_path / "pg"
    write_config(root)
    st = store_pg.PgStore(root, dsn=DSN, schema=f"rc_{uuid.uuid4().hex[:12]}")
    st.pin_config_from_files()
    monkeypatch.setattr(round_recovery.ops, "RUN_LEDGER", tmp_path / "run_history.jsonl")
    yield st
    st.drop()


def events(pg) -> list[tuple]:
    path = Path(round_recovery.ops.RUN_LEDGER)
    return [(e["run_id"], e["event"]) for e in map(json.loads, path.read_text().splitlines())] \
        if path.exists() else []


def open_run(st, w, run_id, **kw):
    kw.setdefault("artifacts", "unchecked")
    return st.open_run(w, run_id, producer_commit=kw.pop("producer_commit", SHA),
                       extractor_version=kw.pop("extractor_version", "x1"),
                       cleaning_ruleset="rules-1", **kw)


def stage(st, w, run_id, batch, fn, step="fetch"):
    return st.stage_batch(w, run_id, step, batch, recorded(fn),
                          expected_version=version_of(st, w, run_id))


def finish(st, w, run_id, gates=("tests",)):
    frozen = st.freeze(w, run_id, required_gates=gates)
    for g in gates:
        st.record_gate(w, frozen, g, passed=True)
    return st.promote(w, frozen)


def adopt(st, w, run_id, **kw):
    run = store_staging.run_status(st, w, run_id)
    return store_staging.adopt_run(
        st, w, run_id, reason="test resume",
        producer_commit=kw.get("producer_commit", run["producer_commit"]),
        config_digest=kw.get("config_digest", run["config_digest"]),
        extractor_version=kw.get("extractor_version", run["extractor_version"]))


# --- ownership and adoption (schema v7) ----------------------------------------------------------------

def test_a_new_writer_resumes_a_run_only_through_a_logged_adoption(pg):
    run_id = rid("rc-own")
    with pg.writer() as w1:
        open_run(pg, w1, run_id)
        stage(pg, w1, run_id, "b1", lambda tx: tx.upsert_manifest([mrow("a")]))
    with pg.writer() as w2:
        with pytest.raises(store.WriterError, match="adoption"):
            stage(pg, w2, run_id, "b2", lambda tx: tx.upsert_manifest([mrow("b")]))
        resumed = adopt(pg, w2, run_id)
        assert (resumed.attempt, resumed.status) == (2, "open")
        stage(pg, w2, run_id, "b2", lambda tx: tx.upsert_manifest([mrow("b")]))
        again = adopt(pg, w2, run_id)          # its owner re-adopting: only a new token
        assert again.attempt == 2 and again.token != resumed.token
        finish(pg, w2, run_id)
    (owner, writer), = q(pg, "SELECT owner_epoch, writer_epoch FROM runs")
    assert owner == w2.epoch and writer == w1.epoch
    assert q(pg, "SELECT owner_epoch, previous_epoch, status FROM run_adoptions") == [
        (w2.epoch, w1.epoch, "open")]
    with pg.read() as v:
        assert v.known(ids=["a", "b"]).ids == {"a", "b"}


def _stale_open(pg, run_id, **kw):
    """An open run whose owner (writer epoch) is gone; returns that epoch."""
    with pg.writer() as w:
        open_run(pg, w, run_id, **kw)
        stage(pg, w, run_id, "b1", lambda tx: tx.upsert_manifest([mrow(f"doc-{run_id}")]))
        return w.epoch


@pytest.mark.parametrize("change", ["producer_commit", "config_digest", "extractor_version"])
def test_an_adoption_with_other_code_configuration_or_extractor_is_refused(pg, change):
    run_id = rid("rc-diff")
    _stale_open(pg, run_id)
    other = {"producer_commit": OTHER, "config_digest": "f" * 64, "extractor_version": "x2"}
    with pg.writer() as w:
        with pytest.raises(sqlerr(), match="other code, configuration or extractor"):
            adopt(pg, w, run_id, **{change: other[change]})
    assert q(pg, "SELECT count(*) FROM run_adoptions")[0][0] == 0


def test_an_adoption_after_another_generation_is_refused(pg):
    stale, fresh = rid("rc-stale"), rid("rc-fresh")
    _stale_open(pg, stale)
    with pg.writer() as w:
        open_run(pg, w, fresh)
        stage(pg, w, fresh, "b1", lambda tx: tx.upsert_manifest([mrow("fresh")]))
        assert finish(pg, w, fresh) == 0
        with pytest.raises(sqlerr(), match="cannot be resumed"):
            adopt(pg, w, stale)


def test_an_adoption_needs_every_referenced_version_verified(pg, tmp_path):
    run_id = rid("rc-art")
    local = artifact_store.LocalArtifacts(pg.root)
    art = local.put_bytes("raw", b"payload bytes")
    with pg.writer() as w:
        open_run(pg, w, run_id, artifacts="versioned")
        stage(pg, w, run_id, "b1", lambda tx: tx.upsert_manifest([mrow(
            "v", sha256=art.sha256, bytes=art.size, raw_path="raw/test/v.pdf", text_path=None,
            text_chars=None)]))
    with pg.writer() as w:
        with pytest.raises(sqlerr(), match="verified local version"):
            adopt(pg, w, run_id)
        assert artifact_store.verify_run(pg, w, run_id) == {"verified": 1, "failed": []}
        assert adopt(pg, w, run_id).attempt == 2


def test_ownership_rows_are_guarded_against_direct_sql(pg):
    run_id = rid("rc-sql")
    epoch = _stale_open(pg, run_id)
    with pg._connect(autocommit=True) as conn:
        for stmt, params in (
                ("UPDATE runs SET owner_epoch = owner_epoch + 5", ()),
                # an adopter that is not the current writer
                ("INSERT INTO run_adoptions (run_id, owner_epoch, previous_epoch, status, "
                 "parent_generation, producer_commit, config_digest, extractor_version, "
                 "staged_seq, reason) SELECT run_id, owner_epoch + 7, owner_epoch, status, "
                 "parent_generation, producer_commit, config_digest, extractor_version, "
                 "staged_seq, 'x' FROM runs", ()),
                ("INSERT INTO runs (run_id, kind, authority_epoch, writer_epoch, owner_epoch, "
                 "producer_commit, config_digest, extractor_version, cleaning_ruleset) SELECT "
                 "'forged', kind, authority_epoch, writer_epoch, writer_epoch + 1, "
                 "producer_commit, config_digest, extractor_version, cleaning_ruleset FROM runs",
                 ())):
            with pytest.raises(sqlerr()):
                conn.execute(stmt, params)
    with pg.writer() as w:
        adopt(pg, w, run_id)
    for stmt in ("UPDATE run_adoptions SET reason = 'y'", "DELETE FROM run_adoptions"):
        with pg._connect(autocommit=True) as conn, pytest.raises(sqlerr()):
            conn.execute(stmt)
    assert q(pg, "SELECT previous_epoch FROM run_adoptions") == [(epoch,)]


def test_a_final_run_cannot_be_adopted(pg):
    run_id = rid("rc-final")
    _stale_open(pg, run_id)
    with pg.writer() as w:
        pg.abort_run(w, run_id, reason="test")
        with pytest.raises(store.StaleView, match="aborted"):
            adopt(pg, w, run_id)


# --- the purge queue ------------------------------------------------------------------------------------------

def test_aborting_queues_the_run_and_purging_dequeues_it_only_when_empty(pg):
    run_id = rid("rc-purge")
    _stale_open(pg, run_id)
    with pg._connect(autocommit=True) as conn, pytest.raises(sqlerr()):
        conn.execute("INSERT INTO purge_queue (run_id) VALUES (%s)", [run_id])   # not aborted
    with pg.writer() as w:
        pg.abort_run(w, run_id, reason="test")
        assert store_staging.purge_due(pg, w, grace_seconds=3600) == []
        assert store_staging.purge_due(pg, w, grace_seconds=0) == [run_id]
        with pg._connect(autocommit=True) as conn, pytest.raises(sqlerr()):
            conn.execute("DELETE FROM purge_queue")                  # staging is left
        n = 0
        while store_staging.purge_run(pg, w, run_id, limit=1):
            n += 1
        assert n == 2 and store_staging.purge_due(pg, w, grace_seconds=0) == []   # rev, batch
    assert q(pg, "SELECT status FROM runs") == [("aborted",)]       # the evidence row stays


# --- the shared staged recovery ------------------------------------------------------------------------------------

DEAD_COORDINATOR = """
import os, sys
sys.path.insert(0, {scripts!r})
import run_ownership
m = run_ownership.Mark({root!r}, {run!r}).__enter__()
print(m.owner.tag, flush=True)
os._exit(0)      # dies holding its attempt: the mark stays
"""


def dead_attempt(root, run_id) -> str:
    """A coordinator attempt of `run_id` whose coordinator is dead; returns its tag."""
    got = subprocess.run([sys.executable, "-c", DEAD_COORDINATOR.format(
        scripts=str(REPO / "scripts"), root=str(root), run=run_id)], capture_output=True,
        text=True, check=True)
    return got.stdout.strip()


def _sleeper(run_id=None, tag=None):
    env = {k: v for k, v in os.environ.items() if k not in ("NEKAISE_RUN_ID", OWNER_ENV)}
    if run_id is not None:
        env["NEKAISE_RUN_ID"] = run_id
    if tag is not None:
        env[OWNER_ENV] = tag
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], env=env,
                            start_new_session=True)


def _owned(root, run_id) -> set[int]:
    rec = run_ownership.read_mark(root, run_id)
    return run_ownership.owner_processes(rec.owner) if rec is not None else set()


def _visible(pid, run_id, root=None):
    import time
    for _ in range(100):
        found = round_recovery.round_processes(run_id) if root is None else _owned(root, run_id)
        if pid in found:
            return
        time.sleep(0.05)
    raise AssertionError("never tagged")


class Drain:
    def __init__(self, log):
        self.log = log

    def drain(self):
        self.log.append("drain")


def test_recovery_stops_processes_then_drains_then_decides_by_durable_status(pg, monkeypatch):
    run_id = rid("rc-order")
    _stale_open(pg, run_id)
    orphan = _sleeper(tag=dead_attempt(pg.root, run_id))
    try:
        _visible(orphan.pid, run_id, pg.root)
        log = []
        real_status = store_staging.run_status

        def status(st, w, rid_):
            log.append("status")
            return real_status(st, w, rid_)
        monkeypatch.setattr(store_staging, "run_status", status)

        class Watching(Drain):
            def drain(self):
                assert not round_recovery._alive(orphan.pid), "drained before the stop"
                super().drain()
        with pg.writer() as w:
            out, = round_recovery.recover_staged(pg, w, None, root=pg.root,
                                                 broker=Watching(log), finish=False)
        assert (out.run_id, out.status, out.action) == (run_id, "open", "aborted")
        assert out.stopped == [orphan.pid]
        assert log[:2] == ["drain", "status"]
    finally:
        orphan.kill()
        orphan.wait()
    assert [e for _, e in events(pg)] == ["round_processes_stopped", "staged_run_aborted"]
    assert run_ownership.read_mark(pg.root, run_id) is None     # the dead attempt's mark swept


def test_an_unknown_database_outcome_mutates_nothing(pg, monkeypatch):
    import psycopg
    run_id = rid("rc-unknown")
    _stale_open(pg, run_id)

    def broken(*_a, **_k):
        raise psycopg.OperationalError("server closed the connection unexpectedly")
    monkeypatch.setattr(store_staging, "run_status", broken)
    with pg.writer() as w:
        with pytest.raises(psycopg.OperationalError):
            round_recovery.recover_staged(pg, w, run_id, root=pg.root, finish=False)
    monkeypatch.undo()
    assert q(pg, "SELECT status FROM runs") == [("open",)]            # not aborted
    monkeypatch.setattr(store_staging, "unfinished_runs", broken)
    with pg.writer() as w:
        with pytest.raises(psycopg.OperationalError):
            round_recovery.recover_staged(pg, w, None, root=pg.root, finish=False)
    assert q(pg, "SELECT status FROM runs") == [("open",)]


def test_recovery_reports_promoted_aborted_and_never_opened_runs(pg):
    done, gone, never = rid("rc-done"), rid("rc-gone"), rid("rc-never")
    with pg.writer() as w:
        open_run(pg, w, done)
        stage(pg, w, done, "b1", lambda tx: tx.upsert_manifest([mrow("d")]))
        finish(pg, w, done)
        open_run(pg, w, gone)
        pg.abort_run(w, gone, reason="test")
        got = {r: round_recovery.recover_staged(pg, w, r, root=pg.root, finish=False)[0].action
               for r in (done, gone, never)}
    assert got == {done: "kept_promoted", gone: "already_aborted", never: "not_opened"}


def test_a_lost_promotion_reply_stands(pg, monkeypatch):
    """The promotion commits but its reply is lost (the client sees a connection error): the
    failed round's recovery reads the durable status — promoted — and keeps it."""
    import psycopg
    run_id = rid("rc-lost")
    real = pg.promote

    def lost(w, frozen):
        real(w, frozen)
        raise psycopg.OperationalError("server closed the connection unexpectedly")
    monkeypatch.setattr(pg, "promote", lost)
    with pg.writer(round_id=run_id) as w:
        with pytest.raises(psycopg.OperationalError):
            with store_broker.staged_round(pg, w, run_id, producer_commit=SHA,
                                           extractor_version="x", cleaning_ruleset="none",
                                           artifacts="unchecked") as rnd:
                rnd.broker.computed_batch("discover", "merge",
                                          lambda v, b: b.upsert_manifest([mrow("kept")]))
                rnd.freeze(["tests"])
                rnd.record_gate("tests", passed=True)
                rnd.promote()
    assert q(pg, "SELECT status, promoted_generation FROM runs") == [("promoted", 0)]
    assert (run_id, "staged_run_kept") in events(pg)
    with pg.read() as v:
        assert v.generation == 0 and v.known(ids=["kept"]).ids == {"kept"}


def _failing_round(pg, run_id, expect):
    with pg.writer(round_id=run_id) as w:
        with pytest.raises(expect) as raised:
            with store_broker.staged_round(pg, w, run_id, producer_commit=SHA,
                                           extractor_version="x", cleaning_ruleset="none",
                                           artifacts="unchecked") as rnd:
                rnd.broker.computed_batch("discover", "merge",
                                          lambda v, b: b.upsert_manifest([mrow("x")]))
                raise RuntimeError("a step failed")
    return rnd, raised.value


def test_an_interrupt_while_stopping_aborts_only_after_a_confirmed_stop(pg, monkeypatch):
    """SIGTERM arrives while a failing round stops its processes (the maintainer's handler
    raises KeyboardInterrupt): the broker is still drained, recovery stops the processes again,
    and the run is aborted only after that stop was CONFIRMED."""
    run_id = rid("rc-sig")
    calls = []

    def stop(root, run, existing=None, current=None, grace=2.0):
        calls.append(("stop", q(pg, "SELECT status FROM runs")[0][0]))
        if len(calls) == 1:
            os.kill(os.getpid(), signal.SIGTERM)   # the interrupt, part-way through the stop
        return []                                  # the second stop: confirmed, nothing alive
    monkeypatch.setattr(run_ownership, "stop_attempt", stop)

    def handler(signum, frame):
        raise KeyboardInterrupt("maintenance terminated")
    previous = signal.signal(signal.SIGTERM, handler)
    try:
        rnd, _ = _failing_round(pg, run_id, KeyboardInterrupt)
    finally:
        signal.signal(signal.SIGTERM, previous)
    assert rnd.broker._drained
    assert calls == [("stop", "open"), ("stop", "open")]   # still open when the stop confirmed
    assert q(pg, "SELECT status FROM runs") == [("aborted",)]


def test_an_unconfirmed_stop_leaves_the_run_unfinished_and_blocking(pg, monkeypatch):
    """A worker survives SIGKILL (or stopping fails otherwise): the broker is drained but the
    run is NOT aborted — it stays open, so every later round and standalone mutation is refused
    until a recovery confirms the stop."""
    run_id = rid("rc-survivor")

    def survivor(*_a, **_k):
        raise round_recovery.RecoveryError("owned process(es) still alive after SIGKILL: [4242]")
    monkeypatch.setattr(run_ownership, "stop_attempt", survivor)
    rnd, exc = _failing_round(pg, run_id, RuntimeError)
    assert rnd.broker._drained and "a step failed" in str(exc)
    assert any("left unfinished" in n for n in exc.__notes__)
    assert q(pg, "SELECT status FROM runs") == [("open",)]
    assert run_ownership.read_mark(pg.root, run_id) is not None  # kept: survivors findable
    with pg.writer() as w:
        with pytest.raises(staged_runs.StagedRunError, match="unfinished staged run"):
            staged_runs.refuse_unfinished(pg, w)
        with pytest.raises(round_recovery.RecoveryError, match="still alive"):
            round_recovery.recover_staged(pg, w, run_id, root=pg.root, finish=False)
    assert q(pg, "SELECT status FROM runs") == [("open",)]      # recovery refused to abort too
    assert run_ownership.read_mark(pg.root, run_id) is not None
    monkeypatch.undo()
    with pg.writer() as w:
        out, = round_recovery.recover_staged(pg, w, run_id, root=pg.root, finish=False)
    assert out.action == "aborted"


def test_a_second_interrupt_during_the_stop_leaves_the_run_open(pg, monkeypatch):
    run_id = rid("rc-sig2")

    def interrupted(*_a, **_k):
        os.kill(os.getpid(), signal.SIGTERM)
    monkeypatch.setattr(run_ownership, "stop_attempt", interrupted)

    def handler(signum, frame):
        raise KeyboardInterrupt("terminated")
    previous = signal.signal(signal.SIGTERM, handler)
    try:
        rnd, _ = _failing_round(pg, run_id, KeyboardInterrupt)
    finally:
        signal.signal(signal.SIGTERM, previous)
    assert rnd.broker._drained
    assert q(pg, "SELECT status FROM runs") == [("open",)]      # never aborted unconfirmed


def test_only_a_dead_coordinator_s_orphans_are_stopped_before_the_writer(tmp_path):
    live_run, dead_run = rid("rc-live"), rid("rc-dead")
    with run_ownership.Mark(tmp_path, live_run) as live:          # this process: alive
        mine = _sleeper(tag=live.owner.tag)
        orphan = _sleeper(tag=dead_attempt(tmp_path, dead_run))
        try:
            _visible(orphan.pid, dead_run, tmp_path)
            _visible(mine.pid, live_run, tmp_path)
            assert run_ownership.sweep_dead(tmp_path) == {dead_run: [orphan.pid]}
            assert round_recovery._alive(mine.pid)               # the live attempt: untouched
            assert run_ownership.read_mark(tmp_path, dead_run) is None
            broken = tmp_path / run_ownership.MARKS / f"{run_ownership.MARK_PREFIX}broken"
            broken.write_text("{not json")
            with pytest.raises(run_ownership.OwnershipError, match="unreadable|malformed"):
                run_ownership.sweep_dead(tmp_path)
        finally:
            for p in (mine, orphan):
                p.kill()
                p.wait()


def test_ownership_marks_identify_forked_workers_and_exec_tags(tmp_path):
    """A fork-only worker's /proc/<pid>/environ still shows its parent's ORIGINAL environment,
    so a tag set later is invisible there; the inherited mark descriptor identifies it. A child
    exec'd after tag() carries the attempt's tag from exec."""
    run_id = rid("rc-mark")
    code = (f"import os, sys, time\nsys.path.insert(0, {str(REPO / 'scripts')!r})\n"
            "import run_ownership, subprocess\n"
            f"m = run_ownership.Mark({str(tmp_path)!r}, {run_id!r}).__enter__()\n"
            "m.tag()\n"
            "pid = os.fork()\n"
            "if pid == 0:\n    time.sleep(60)\n    os._exit(0)\n"
            "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "print(pid, child.pid, m.owner.tag, flush=True)\ntime.sleep(60)\n")
    coord = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True,
                             env={k: v for k, v in os.environ.items() if k != OWNER_ENV})
    try:
        forked, execd, tag = coord.stdout.readline().split()
        forked, execd = int(forked), int(execd)
        assert f"{OWNER_ENV}={tag}".encode() not in Path(
            f"/proc/{forked}/environ").read_bytes().split(b"\0")
        assert f"{OWNER_ENV}={tag}".encode() in Path(
            f"/proc/{execd}/environ").read_bytes().split(b"\0")
        assert round_recovery.round_processes(run_id) == set()   # legacy matching: untouched
        coord.kill()                                   # the coordinator dies
        coord.wait()
        found = _owned(tmp_path, run_id)
        assert {forked, execd} <= found
        assert run_ownership.sweep_dead(tmp_path)[run_id]
        assert not round_recovery._alive(forked) and not round_recovery._alive(execd)
    finally:
        coord.kill()
        for pid in _owned(tmp_path, run_id):
            os.kill(pid, signal.SIGKILL)


# --- Codex second review of 1749fb5056 --------------------------------------------------------------

def test_legacy_recovery_matching_is_exactly_main_s(tmp_path):
    """The file store's recovery (round_recovery.round_processes / stop_owned) is byte for byte
    what it was before step 4: NEKAISE_RUN_ID only — never the staged tag, a mark descriptor
    (same file name, another root) or an error reading an unrelated process."""
    main = subprocess.run(["git", "-C", str(REPO), "show", "5dfb9ed77b:scripts/round_recovery.py"],
                          capture_output=True, text=True)
    if main.returncode:
        pytest.skip("5dfb9ed77b not in this clone")
    now = (REPO / "scripts" / "round_recovery.py").read_text()
    staged = now.index("# --- staged runs (PostgreSQL authority")
    legacy_now = now[:staged].rstrip("\n")
    for name in ("round_processes", "stop_owned", "stop_processes", "descendants"):
        start = f"def {name}("
        assert legacy_now[legacy_now.index(start):].split("\n\n\ndef ")[0] == \
            main.stdout[main.stdout.index(start):].split("\n\n\ndef ")[0], name
    run_id = rid("rc-legacy")
    staged_only = _sleeper(tag=f"x:{run_id}:0")                    # the staged tag only
    with run_ownership.Mark(tmp_path, run_id):
        forked_like = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                                       close_fds=False, env={k: v for k, v in os.environ.items()
                                                              if k != "NEKAISE_RUN_ID"})
    legacy = _sleeper(run_id)
    try:
        _visible(legacy.pid, run_id)
        found = round_recovery.round_processes(run_id)
        assert legacy.pid in found and staged_only.pid not in found
        assert forked_like.pid not in found
    finally:
        for p in (staged_only, forked_like, legacy):
            p.kill()
            p.wait()


def test_staged_ownership_is_scoped_to_its_root_and_attempt(tmp_path):
    """Another root's attempt of the same run id, and an earlier attempt of the same run on the
    same root (same file name, another inode and nonce), never match."""
    run_id = rid("rc-scope")
    other_root = tmp_path / "other"
    with run_ownership.Mark(other_root, run_id) as other:
        theirs = _sleeper(tag=other.owner.tag)
    first_tag = dead_attempt(tmp_path, run_id)
    first = run_ownership.read_mark(tmp_path, run_id).owner
    old = _sleeper(tag=first_tag)
    with run_ownership.Mark(tmp_path, run_id) as second:           # the next attempt
        new = _sleeper(tag=second.owner.tag)
        try:
            _visible(new.pid, run_id, tmp_path)
            assert second.owner.ino != first.ino and second.owner.nonce != first.nonce
            assert run_ownership.owner_processes(second.owner) == {new.pid}
            assert run_ownership.owner_processes(first) == {old.pid}
            assert theirs.pid not in run_ownership.owner_processes(second.owner)
        finally:
            for p in (theirs, old, new):
                p.kill()
                p.wait()


def test_a_zombie_coordinator_is_dead_and_a_reused_pid_is_not_the_coordinator(tmp_path,
                                                                             monkeypatch):
    run_id = rid("rc-zombie")
    # A: the coordinator is a zombie (dead, unreaped): its start time still matches
    code = (f"import os, sys, time\nsys.path.insert(0, {str(REPO / 'scripts')!r})\n"
            "import run_ownership\n"
            f"m = run_ownership.Mark({str(tmp_path)!r}, {run_id!r}).__enter__()\n"
            "print(m.owner.tag, flush=True)\nos._exit(0)\n")
    zombie = subprocess.Popen([sys.executable, "-c", code], stdout=subprocess.PIPE, text=True)
    tag = zombie.stdout.readline().strip()
    worker = _sleeper(tag=tag)
    try:
        for _ in range(100):
            if run_ownership.proc_state(zombie.pid)[0] == "Z":
                break
            __import__("time").sleep(0.05)
        rec = run_ownership.read_mark(tmp_path, run_id)
        assert run_ownership.proc_state(rec.pid)[1] == rec.start      # same start time...
        assert not run_ownership.coordinator_alive(rec)              # ...but a zombie: dead
        _visible(worker.pid, run_id, tmp_path)
        assert run_ownership.sweep_dead(tmp_path) == {run_id: [worker.pid]}
    finally:
        worker.kill()
        worker.wait()
        zombie.wait()
    # B: the recovering process has the dead coordinator's pid (reused): only the start time
    # decides — no self-pid shortcut
    run2 = rid("rc-reuse")
    dead_attempt(tmp_path, run2)
    rec = run_ownership.read_mark(tmp_path, run2)
    doc = json.loads(rec.path.read_text())
    doc["pid"] = os.getpid()                        # "our" pid, but another start time
    rec.path.write_text(json.dumps(doc))
    assert not run_ownership.coordinator_alive(run_ownership.read_mark(tmp_path, run2))
    # another boot: dead; another PID namespace: refused
    doc["boot"] = "00000000-0000-0000-0000-000000000000"
    rec.path.write_text(json.dumps(doc))
    assert not run_ownership.coordinator_alive(run_ownership.read_mark(tmp_path, run2))
    doc["boot"] = run_ownership.boot_id()
    doc["pidns"] = "pid:[1]"
    rec.path.write_text(json.dumps(doc))
    with pytest.raises(run_ownership.OwnershipError, match="namespace"):
        run_ownership.sweep_dead(tmp_path)


def test_a_paused_sweep_cannot_act_while_a_new_attempt_installs_itself(tmp_path, monkeypatch):
    """Sweep A judges attempt N dead and pauses between the check and the signal; a resuming
    coordinator B must take the lifecycle lock before its writer and its new mark, so it waits
    until A is done — and A, bound to attempt N (nonce, inode, pidfd), never signals B's
    attempt N+1."""
    import threading
    run_id = rid("rc-race")
    old = _sleeper(tag=dead_attempt(tmp_path, run_id))
    _visible(old.pid, run_id, tmp_path)
    paused, go = threading.Event(), threading.Event()

    def pause(point):
        if point == "verified":
            paused.set()
            assert go.wait(30)
    monkeypatch.setattr(run_ownership, "_pause", pause)
    result = {}

    def sweep_a():
        with run_ownership.lifecycle(tmp_path):
            result["a"] = run_ownership.sweep_dead(tmp_path)
    a = threading.Thread(target=sweep_a)
    a.start()
    assert paused.wait(30)
    with pytest.raises(run_ownership.OwnershipError, match="lifecycle lock"):
        with run_ownership.lifecycle(tmp_path, timeout=0.5):   # B cannot adopt or re-mark now
            pass
    monkeypatch.setattr(run_ownership, "_pause", lambda point: None)
    go.set()
    a.join(30)
    assert result["a"] == {run_id: [old.pid]}
    with run_ownership.lifecycle(tmp_path, timeout=5) as held:   # B, after A
        with run_ownership.Mark(tmp_path, run_id) as b:
            held.release()
            b_child = _sleeper(tag=b.owner.tag)
            try:
                _visible(b_child.pid, run_id, tmp_path)
                assert run_ownership.sweep_dead(tmp_path) == {}   # B's coordinator is alive
                assert round_recovery._alive(b_child.pid)
            finally:
                b_child.kill()
                b_child.wait()
    old.wait(5)


def test_signals_go_through_a_pidfd_after_re_verification(tmp_path, monkeypatch):
    """A pid that exits between the scan and the signal is never signalled (its pidfd shows it
    gone); a pid that no longer belongs to the attempt at re-verification is not signalled."""
    run_id = rid("rc-pidfd")
    tag = dead_attempt(tmp_path, run_id)
    owner = run_ownership.read_mark(tmp_path, run_id).owner
    stranger = _sleeper()                               # not the attempt's
    gone = _sleeper(tag=tag)
    gone.kill()
    gone.wait()
    sent = []
    real = signal.pidfd_send_signal
    monkeypatch.setattr(signal, "pidfd_send_signal",
                        lambda fd, sig, *a: (sent.append(sig), real(fd, sig, *a)))
    try:
        assert run_ownership.stop({stranger.pid, gone.pid}, owner) == []
        assert sent == [] and round_recovery._alive(stranger.pid)
    finally:
        stranger.kill()
        stranger.wait()


# --- bounded housekeeping -------------------------------------------------------------------------------------------

def test_housekeeping_is_bounded_and_converges(pg):
    with pg.writer() as w:
        for i in range(3):
            run_id = rid(f"rc-hk{i}")
            open_run(pg, w, run_id)
            stage(pg, w, run_id, "b1", lambda tx, i=i: tx.upsert_manifest(
                [mrow(f"h{i}-{k}") for k in range(5)]))
            finish(pg, w, run_id)
        dead = rid("rc-hk-dead")
        open_run(pg, w, dead)
        stage(pg, w, dead, "b1", lambda tx: tx.upsert_manifest([mrow("dead")]))
        pg.abort_run(w, dead, reason="test")
        assert staged_runs.housekeeping(pg, w, seconds=0)["budget_exhausted"]
        assert q(pg, "SELECT generation FROM projection_state") == [(None,)]
        rounds = 0
        while True:
            rounds += 1
            out = staged_runs.housekeeping(pg, w, seconds=0.05, purge_grace=0, fold_limit=1)
            if not out["budget_exhausted"]:
                break
            assert rounds < 500
        assert q(pg, "SELECT generation FROM projection_state") == [(2,)]
        assert q(pg, "SELECT count(*) FROM revisions WHERE run_id = %s", [dead]) == [(0,)]
        assert q(pg, "SELECT count(*) FROM purge_queue") == [(0,)]
    with pg.read() as v:
        assert len(v.known(ids=[f"h{i}-{k}" for i in range(3) for k in range(5)]).ids) == 15


def test_attempt_namespaces_name_a_resumed_run_s_batches(monkeypatch):
    round_id = rid("rnd-x")
    client = store_broker.Client("/nonexistent", "cap", round_id)
    monkeypatch.setenv("NEKAISE_RUN_ID", round_id)

    class View:
        def version(self):
            return store.Version(f"pg:stage:{round_id}:3")
    assert store_broker.StepSession(None, "fetch", View(), client=client).invocation is None
    monkeypatch.setenv(store_broker.ATTEMPT_ENV, "2")
    session = store_broker.StepSession(None, "fetch", View(), client=client)
    assert session.identity("ckpt-0001") == f"{round_id}.fetch.a2-ckpt-0001"
    monkeypatch.setenv(store_broker.ATTEMPT_ENV, "1")
    with pytest.raises(store_broker.BrokerError, match="malformed"):
        store_broker.StepSession(None, "fetch", View(), client=client)


# --- the v6 -> v7 migration ------------------------------------------------------------------------------------------

def _load_v6(name: str):
    got = subprocess.run(["git", "-C", str(REPO), "show", f"{V6_COMMIT}:scripts/{name}.py"],
                         capture_output=True)
    if got.returncode:
        pytest.skip(f"{V6_COMMIT} not in this clone")
    spec = importlib.util.spec_from_loader(f"{name}_v6", loader=None)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    try:
        exec(compile(got.stdout, f"{name}_v6.py", "exec"), mod.__dict__)  # noqa: S102
    finally:
        del sys.modules[spec.name]
    return mod


@contextmanager
def v6_code(monkeypatch):
    """The step-3 store_pg AND store_staging, bound to each other (store_pg imports
    store_staging lazily; the current one knows v7)."""
    v6 = _load_v6("store_pg")
    assert v6.SCHEMA_VERSION == 6
    staging = _load_v6("store_staging")
    staging.store_pg = v6
    with monkeypatch.context() as m:
        m.setitem(sys.modules, "store_staging", staging)
        yield v6


def test_v6_runs_migrate_with_owners_backfilled_and_the_purge_queue_filled(tmp_path, monkeypatch):
    import store_pg
    root = tmp_path / "r"
    write_config(root)
    schema = f"m7_{uuid.uuid4().hex[:12]}"
    done, dead, left = rid("v6-done"), rid("v6-dead"), rid("v6-left")
    try:
        with v6_code(monkeypatch) as v6:
            old = v6.PgStore(root, dsn=DSN, schema=schema)
            old.pin_config_from_files()
            with old.writer() as w:
                for run_id in (done, dead, left):
                    old.open_run(w, run_id, producer_commit=SHA, extractor_version="x",
                                 cleaning_ruleset="none", artifacts="unchecked")
                    rec = store_broker.Recorder()
                    rec.upsert_manifest([mrow(f"doc-{run_id}")])
                    with old.read_staged(run_id, writer=w) as v:
                        version = v.version()
                    old.stage_batch(w, run_id, "fetch", "b1", rec.requests,
                                    expected_version=version)
                    if run_id == done:
                        frozen = old.freeze(w, run_id, required_gates=["tests"])
                        old.record_gate(w, frozen, "tests", passed=True)
                        old.promote(w, frozen)
                    elif run_id == dead:
                        old.abort_run(w, run_id, reason="v6 abort")
                opened_by = w.epoch
        new = store_pg.PgStore(root, dsn=DSN, schema=schema)
        assert q(new, "SELECT schema_version FROM state") == [(store_pg.SCHEMA_VERSION,)]
        assert sorted(q(new, "SELECT run_id, owner_epoch, writer_epoch FROM runs")) == sorted(
            (r, opened_by, opened_by) for r in (done, dead, left))
        assert q(new, "SELECT run_id FROM purge_queue") == [(dead,)]
        assert q(new, "SELECT reviewed_through, endorsed_through, verdicts FROM review_state") \
            == [(None, None, 0)]
        with pytest.raises(store.StoreError, match=f"version {store_pg.SCHEMA_VERSION}, "
                                                   "code expects 6"):
            v6.PgStore(root, dsn=DSN, schema=schema)
        with new.read() as v:
            assert v.generation == 0 and v.known(ids=[f"doc-{done}"]).ids
        # the v6 run left open is recovered by the new code: resumed by a new writer (the same
        # commit, configuration and extractor), or aborted
        with new.writer() as w:
            assert adopt(new, w, left).attempt == 2
            stage(new, w, left, "b2", lambda tx: tx.upsert_manifest([mrow("after")]))
            assert finish(new, w, left) == 1
    finally:
        store_pg.PgStore(root, dsn=DSN, schema=schema, create=False).drop()


def test_a_v6_shadow_migrates_to_v7_and_keeps_replicating(tmp_path):
    import pg_shadow
    import store_pg
    import test_pg_shadow as shadow
    from test_store_pg_staging import export_bytes
    v6 = _load_v6("store_pg")
    repo_ = shadow.Repo(tmp_path / "repo")
    repo_.write("registry/backends.json", json.dumps({"find_x": {"script": "x.py", "args": []}}))
    repo_.write("registry/eligibility.json", json.dumps({"version": 1, "restrictions": {}}))
    repo_.write("registry/rotation.json", json.dumps({"find_x": {"flag": "--page", "next": 1}}))
    repo_.write("registry/books.yaml", shadow.yaml_shard("books", [shadow.entry("oer-a")]))
    repo_.write("manifest/books.jsonl", shadow.manifest([shadow.mrow("oer-a"),
                                                         shadow.mrow("oer-b", text_chars=1e20)]))
    repo_.write("pruned_urls.txt", "https://e.org/old\n")
    c1 = repo_.commit("c1")
    schema = f"m7s_{uuid.uuid4().hex[:12]}"
    old = v6.PgStore(repo_.path, dsn=DSN, schema=schema)
    quiet = lambda *_: None  # noqa: E731
    try:
        pg_shadow.do_import(old, c1, repo_.path, log=quiet)
        repo_.write("pruned_urls.txt", "https://e.org/old\nhttps://e.org/v6\n")
        c2 = repo_.commit("c2")
        assert pg_shadow.do_sync(old, c2, repo_.path, log=quiet) == 1
        with old.read() as v:
            before = export_bytes(old, v, tmp_path / "v6")
        before_digests = pg_shadow.pg_digests(old)
        auth = old.authority()
        new = store_pg.PgStore(repo_.path, dsn=DSN, schema=schema)       # migrates 6 -> 7
        assert q(new, "SELECT schema_version FROM state")[0][0] == 7
        assert pg_shadow.pg_digests(new) == before_digests
        with new.read() as v:
            assert export_bytes(new, v, tmp_path / "v7") == before
        assert new.authority() == auth
        assert pg_shadow.do_verify(new, repo_.path, log=quiet)
        repo_.write("pruned_urls.txt", "https://e.org/old\nhttps://e.org/v6\nhttps://e.org/v7\n")
        c3 = repo_.commit("c3")
        assert pg_shadow.do_sync(new, c3, repo_.path, log=quiet) == 1
        assert pg_shadow.do_verify(new, repo_.path, log=quiet)
        with pytest.raises(store.StoreError, match="restart with matching code"):
            with old.writer():
                pass
    finally:
        store_pg.PgStore(repo_.path, dsn=DSN, schema=schema, create=False).drop()
