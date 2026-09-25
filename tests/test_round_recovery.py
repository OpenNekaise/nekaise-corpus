"""The shared round recovery routine (scripts/round_recovery.py, ADR 0001 stage 3, step 7) through
each entrypoint — run_round's failure rollback, `run_round --recover`, the maintainer's automatic
recovery — in a real git repository, including a round that had ALREADY COMMITTED when it died:
its commit stands, its snapshot is discarded, never restored over it."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import maintainer
import ops
import round_recovery
import run_round
import store
from pipeline_repo import tracked, write_repo
from runids import rid

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
ROW = {"id": "ost-a", "title": "A", "url": "https://e.org/a.pdf", "source": "osti",
       "license": "public-domain", "topic": "building_energy", "format": "pdf", "status": "ok"}
ENTRY = {k: v for k, v in ROW.items() if k != "status"}


def git(root: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=root, check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture
def events(monkeypatch):
    got: list = []
    monkeypatch.setattr(ops, "run_event", lambda run_id, event, **kw: got.append((event, kw)))
    return got


@pytest.fixture
def repo(tmp_path, monkeypatch, events):
    """A git repository holding a small store; run_round/maintainer/ops pointed at it."""
    root = write_repo(tmp_path / "r", entries=[ENTRY], manifest=[ROW])
    (root / "README.md").write_text("readme\n")
    (root / ".gitignore").write_text("workspace/\n")
    git(root, "init", "-q")
    git(root, "config", "user.email", "t@example.test")
    git(root, "config", "user.name", "T")
    git(root, "add", "-A")
    git(root, "commit", "-qm", "initial")
    monkeypatch.setattr(run_round, "ROOT", root)
    monkeypatch.setattr(maintainer, "ROOT", root)
    monkeypatch.setattr(ops, "SNAPSHOTS", root / "workspace" / "round-snapshots")
    monkeypatch.setattr(ops, "WORKSPACE", root / "workspace")
    return root


def interrupted_round(root: Path, run_id: str, *, commit: bool) -> dict:
    """A round that snapshotted, changed the manifest through the store and (when `commit`)
    committed with run_round's message — then its process died: the snapshot is left behind.
    Returns the pre-round tracked bytes."""
    before = tracked(root, journal=True)
    st = store.FileStore(root)
    with st.writer(round_id=run_id) as w:  # as run_round: the lock first, then the snapshot
        ops.StateSnapshot.capture(run_id, run_round.SNAPSHOT_PATHS, root=root)
        with st.transaction(f"{run_id}.fetch.ckpt-0001", expected_version=st.version(),
                            writer=w) as tx:
            tx.upsert_manifest([{**ROW, "title": "A (fetched again)"}])
    if commit:
        git(root, "add", *run_round.COMMIT_PATHS)
        git(root, "commit", "-qm", "dig: +0 docs", "-m", f"Corpus run: {run_id}")
    return before


def recover(entry: str, root: Path, run_id: str, monkeypatch) -> int:
    if entry == "recover":
        monkeypatch.setattr(sys, "argv", ["run_round.py", "--recover", run_id])
        return run_round.main()
    st = store.FileStore(root)
    with st.writer() as w, maintainer.window_writer(st, w):
        try:
            assert maintainer.recover_pending_round() == run_id
        except Exception:
            return 1
    return 0


@pytest.mark.parametrize("entry", ["recover", "maintainer"])
def test_an_already_committed_round_is_kept_not_restored(repo, events, monkeypatch, entry):
    interrupted_round(repo, rid("rnd-c"), commit=True)
    committed = tracked(repo, journal=True)
    head = git(repo, "rev-parse", "HEAD")
    assert recover(entry, repo, rid("rnd-c"), monkeypatch) == 0
    assert tracked(repo, journal=True) == committed  # the commit stands
    assert git(repo, "status", "--porcelain") == ""
    assert not ops.StateSnapshot.pending()
    assert ("round_already_committed", {"commit": head}) in events
    fields = {"committed": head} | ({"recovered_by": "ai_maintainer"}
                                    if entry == "maintainer" else {})
    assert ("run_recovered", fields) in events


@pytest.mark.parametrize("entry", ["recover", "maintainer"])
def test_an_uncommitted_round_is_restored(repo, events, monkeypatch, entry):
    before = interrupted_round(repo, rid("rnd-u"), commit=False)
    git(repo, "add", "manifest")  # a partial stage the round left behind
    assert recover(entry, repo, rid("rnd-u"), monkeypatch) == 0
    assert tracked(repo, journal=True) == before
    assert git(repo, "status", "--porcelain") == ""  # unstaged and restored
    assert not ops.StateSnapshot.pending()
    assert not any(e == "round_already_committed" for e, _ in events)


@pytest.mark.parametrize("entry", ["recover", "maintainer"])
def test_a_committed_round_changed_since_is_left_for_an_operator(repo, events, monkeypatch, entry):
    interrupted_round(repo, rid("rnd-d"), commit=True)
    (repo / "pruned_urls.txt").write_text("https://e.org/edited-after-the-commit\n")
    now = tracked(repo, journal=True)
    assert recover(entry, repo, rid("rnd-d"), monkeypatch) == 1
    assert tracked(repo, journal=True) == now  # neither restored nor discarded
    assert ops.StateSnapshot.pending() == [rid("rnd-d")]


def test_a_known_commit_without_its_trailer_is_never_restored(repo):
    interrupted_round(repo, rid("rnd-k"), commit=False)
    st = store.FileStore(repo)
    now = tracked(repo, journal=True)
    with st.writer(round_id=rid("rnd-k"), recovering=True) as w:
        with pytest.raises(round_recovery.RecoveryError, match="reports its commit"):
            round_recovery.recover_round(st, w, rid("rnd-k"), root=repo,
                                         snapshot_paths=run_round.SNAPSHOT_PATHS,
                                         known_committed=True)
    assert tracked(repo, journal=True) == now
    assert ops.StateSnapshot.pending() == [rid("rnd-k")]


@pytest.mark.parametrize("entry", ["recover", "maintainer"])
def test_the_round_s_orphaned_processes_are_stopped_first(repo, events, monkeypatch, entry):
    """A killed round's children outlive it (a fetch still downloading, a prune still moving
    bytes). Recovery stops every process tagged with the round's run id before it resolves
    anything, and leaves other processes alone."""
    interrupted_round(repo, rid("rnd-o"), commit=False)
    sleeper = [sys.executable, "-c", "import time; time.sleep(60)"]
    orphan = subprocess.Popen(sleeper, env={**os.environ, "NEKAISE_RUN_ID": rid("rnd-o")},
                              start_new_session=True)
    other = subprocess.Popen(sleeper, env={**os.environ, "NEKAISE_RUN_ID": rid("rnd-other")},
                             start_new_session=True)
    try:
        assert recover(entry, repo, rid("rnd-o"), monkeypatch) == 0
        assert not round_recovery._alive(orphan.pid)
        assert round_recovery._alive(other.pid)
        assert ("round_processes_stopped", {"pids": [orphan.pid]}) in events
    finally:
        for p in (orphan, other):
            p.kill()
            try:
                p.wait(5)
            except (subprocess.TimeoutExpired, ChildProcessError):
                pass


# --- run_round's own rollback: a push failure after the commit ------------------------------------

FETCH_CHILD = """
import sys
sys.path[:0] = ["scripts", "tests"]
import pipeline_repo, store, store_broker
root = sys.argv[1]
pipeline_repo.point(None, root)
st = store.FileStore(pipeline_repo.Path(root))
with store_broker.step_session(st, "fetch") as s:
    row = s.view.get_manifest(["ost-a"])["ost-a"]
    with s.batch("ckpt-0001") as b:
        b.upsert_manifest([{**row, "title": "A (fetched in the round)"}])
"""


def test_a_push_failure_after_the_commit_keeps_the_committed_round(repo, events, monkeypatch):
    def run(step, cmd, env, run_id):
        if step == "fetch":
            out = subprocess.run([sys.executable, "-c", FETCH_CHILD, str(repo)], env=env,
                                 capture_output=True, text=True, cwd=SCRIPTS.parent)
            assert out.returncode == 0, out.stderr
        elif step == "push":
            raise RuntimeError("push failed: no network")
    monkeypatch.setattr(run_round, "run_command", run)
    monkeypatch.setattr(run_round, "git_clean", lambda: True)
    monkeypatch.setattr(run_round, "doc_stats", lambda view: (1, 10, 0))
    monkeypatch.setattr(run_round, "run_verify_parallel", lambda *a, **k: None)
    branch = git(repo, "branch", "--show-current")
    monkeypatch.setattr(sys, "argv", ["run_round.py", "--skip-discovery", "--skip-tests",
                                      "--commit", "--push", branch, "--run-id", rid("rnd-push")])
    assert run_round.main() == 1
    assert f"Corpus run: {rid('rnd-push')}" in git(repo, "log", "-1", "--format=%B")
    assert git(repo, "status", "--porcelain") == ""
    rows = [json.loads(l) for p in (repo / "manifest").glob("*.jsonl")
            for l in p.read_text().splitlines()]
    assert [r["title"] for r in rows] == ["A (fetched in the round)"]  # not rolled back
    assert not ops.StateSnapshot.pending()
    assert ("committed_round_kept", {}) in events
    assert not any(e in ("state_rolled_back", "rollback_failed") for e, _ in events)


def test_a_failed_uncommitted_round_is_rolled_back_by_the_same_routine(repo, events, monkeypatch):
    before = tracked(repo, journal=True)

    def run(step, cmd, env, run_id):
        if step == "fetch":
            out = subprocess.run([sys.executable, "-c", FETCH_CHILD, str(repo)], env=env,
                                 capture_output=True, text=True, cwd=SCRIPTS.parent)
            assert out.returncode == 0, out.stderr
        elif step == "clean":
            raise RuntimeError("clean failed")
    monkeypatch.setattr(run_round, "run_command", run)
    monkeypatch.setattr(run_round, "git_clean", lambda: True)
    monkeypatch.setattr(run_round, "doc_stats", lambda view: (1, 10, 0))
    monkeypatch.setattr(sys, "argv", ["run_round.py", "--skip-discovery", "--skip-tests",
                                      "--commit", "--run-id", rid("rnd-fail")])
    assert run_round.main() == 1
    assert tracked(repo, journal=True) == before
    assert git(repo, "log", "-1", "--format=%s") == "initial"
    assert not ops.StateSnapshot.pending()
    assert ("state_rolled_back", {}) in events


# --- git inspection failures never lead to a restore (Codex review of step 7, P1) ---------------

def _failing_git(real, *, fail: str):
    """round_recovery._git with one git subcommand failing as a broken git would."""
    def run(root, *args):
        if args[0] == fail:
            return subprocess.CompletedProcess(["git", *args], 128, "", f"fatal: {fail} broke")
        return real(root, *args)
    return run


@pytest.mark.parametrize("fail", ["rev-parse", "log", "symbolic-ref", "for-each-ref"])
@pytest.mark.parametrize("entry", ["recover", "maintainer"])
def test_a_git_failure_keeps_the_committed_round_and_its_snapshot(repo, events, monkeypatch,
                                                                    fail, entry):
    interrupted_round(repo, rid("rnd-g"), commit=True)
    committed = tracked(repo, journal=True)
    if fail in ("symbolic-ref", "for-each-ref"):  # reached only when HEAD does not resolve
        monkeypatch.setattr(round_recovery, "_git", _failing_git(
            _failing_git(round_recovery._git, fail="rev-parse"), fail=fail))
    else:
        monkeypatch.setattr(round_recovery, "_git", _failing_git(round_recovery._git, fail=fail))
    assert recover(entry, repo, rid("rnd-g"), monkeypatch) == 1
    assert tracked(repo, journal=True) == committed  # never restored over the commit
    assert ops.StateSnapshot.pending() == [rid("rnd-g")]


@pytest.mark.parametrize("corruption", ["garbage", "dangling", "missing-branch"])
def test_a_corrupt_head_keeps_the_committed_round_and_its_snapshot(repo, events, monkeypatch,
                                                                     corruption):
    interrupted_round(repo, rid("rnd-h"), commit=True)
    committed = tracked(repo, journal=True)
    head = repo / ".git" / "HEAD"
    if corruption == "garbage":
        head.write_text("this is not a ref\n")
    elif corruption == "dangling":
        head.write_text("0123456789abcdef0123456789abcdef01234567\n")  # no such object
    else:  # HEAD names a branch that does not exist, while the commits are on another
        head.write_text("ref: refs/heads/no-such-branch\n")
    assert recover("recover", repo, rid("rnd-h"), monkeypatch) == 1
    assert tracked(repo, journal=True) == committed
    assert ops.StateSnapshot.pending() == [rid("rnd-h")]


def test_an_unborn_repository_is_a_legitimate_no_commit(tmp_path, events, monkeypatch):
    """A repository without any commit cannot hold a committed round: recovery restores."""
    root = write_repo(tmp_path / "u", entries=[ENTRY], manifest=[ROW])
    git(root, "init", "-q")
    git(root, "add", "manifest")  # staged, never committed
    monkeypatch.setattr(run_round, "ROOT", root)
    monkeypatch.setattr(ops, "SNAPSHOTS", root / "workspace" / "round-snapshots")
    monkeypatch.setattr(ops, "WORKSPACE", root / "workspace")
    assert round_recovery.repo_state(root) == "unborn"
    before = interrupted_round(root, rid("rnd-n"), commit=False)
    assert recover("recover", root, rid("rnd-n"), monkeypatch) == 0
    assert tracked(root, journal=True) == before
    assert not ops.StateSnapshot.pending()
    assert "manifest" not in git(root, "diff", "--cached", "--name-only")  # unstaged


def test_no_repository_is_a_legitimate_no_commit(tmp_path):
    root = tmp_path / "plain"
    root.mkdir()
    assert round_recovery.repo_state(root) == "none"
    assert round_recovery.committed_round(root, rid("rnd-x")) is None


# --- a deleted tracked path is a change too (Codex review of step 7, P2) ----------------------

@pytest.mark.parametrize("entry", ["recover", "maintainer"])
def test_a_tracked_path_deleted_after_the_commit_keeps_the_snapshot(repo, events, monkeypatch,
                                                                    entry):
    interrupted_round(repo, rid("rnd-x"), commit=True)
    (repo / "pruned_urls.txt").unlink()
    now = tracked(repo, journal=True)
    assert recover(entry, repo, rid("rnd-x"), monkeypatch) == 1
    assert tracked(repo, journal=True) == now
    assert not (repo / "pruned_urls.txt").exists()
    assert ops.StateSnapshot.pending() == [rid("rnd-x")]
    assert not any(e == "round_already_committed" for e, _ in events)


# --- only POSITIVELY established absence means "no repository" (Codex second review, P1) ------

def test_an_io_error_during_repository_discovery_keeps_the_committed_round(repo, events,
                                                                         monkeypatch):
    import errno
    interrupted_round(repo, rid("rnd-e"), commit=True)
    committed = tracked(repo, journal=True)
    real = round_recovery._os_stat

    def stat(path):
        if Path(path).name == ".git":
            raise OSError(errno.EIO, "Input/output error", str(path))
        return real(path)
    monkeypatch.setattr(round_recovery, "_os_stat", stat)
    with pytest.raises(round_recovery.RecoveryError, match="Input/output error"):
        round_recovery.repo_state(repo)
    assert recover("recover", repo, rid("rnd-e"), monkeypatch) == 1
    assert tracked(repo, journal=True) == committed
    assert ops.StateSnapshot.pending() == [rid("rnd-e")]


def _scripted_git(outcomes: dict, real=None):
    def run(root, *args):
        if args[0] not in outcomes:
            return real(root, *args)
        code, out, err = outcomes[args[0]]
        return subprocess.CompletedProcess(["git", *args], code, out, err)
    return run


@pytest.mark.parametrize("head", [
    (128, "", "fatal: bad object HEAD"),   # a fatal error, not an unborn HEAD
    (128, "", ""),                          # a fatal exit even without a message
    (1, "", "error: something odd"),        # the right code with unexpected stderr
    (1, "junk", ""),                        # the right code with output
])
def test_only_the_exact_unborn_outcome_counts_as_unborn(repo, events, monkeypatch, head):
    """rev-parse failing for any other reason, followed by a symbolic branch and an empty ref
    listing (as a broken git could answer), is NOT an unborn repository."""
    interrupted_round(repo, rid("rnd-f"), commit=True)
    committed = tracked(repo, journal=True)
    monkeypatch.setattr(round_recovery, "_git", _scripted_git(
        {"rev-parse": head, "symbolic-ref": (0, "refs/heads/main\n", ""),
         "for-each-ref": (0, "", "")}, real=round_recovery._git))
    with pytest.raises(round_recovery.RecoveryError):
        round_recovery.repo_state(repo)
    with pytest.raises(round_recovery.RecoveryError):
        round_recovery.committed_round(repo, rid("rnd-f"))
    assert recover("maintainer", repo, rid("rnd-f"), monkeypatch) == 1
    assert tracked(repo, journal=True) == committed
    assert ops.StateSnapshot.pending() == [rid("rnd-f")]


def test_an_unborn_looking_answer_is_refused_while_refs_exist_on_disk(repo, monkeypatch):
    """git answering exactly as for an unborn repository while refs exist on disk: refuse."""
    monkeypatch.setattr(round_recovery, "_git", _scripted_git({
        "rev-parse": (1, "", ""), "symbolic-ref": (0, "refs/heads/main\n", ""),
        "for-each-ref": (0, "", "")}, real=round_recovery._git))
    with pytest.raises(round_recovery.RecoveryError):
        round_recovery.repo_state(repo)


def test_a_genuine_unborn_repository_still_answers_unborn(tmp_path):
    root = tmp_path / "fresh"
    root.mkdir()
    git(root, "init", "-q")
    assert round_recovery.repo_state(root) == "unborn"
    assert round_recovery.committed_round(root, rid("rnd-any")) is None
    (root / ".git" / "packed-refs").write_text("# pack-refs with: peeled\n")  # header only
    assert round_recovery.repo_state(root) == "unborn"


# --- test executions are isolated (Codex third review, P2) --------------------------------------------

OTHER_EXECUTION = r"""
import json, os, subprocess, sys
sys.path[:0] = [sys.argv[1] + "/tests", sys.argv[1] + "/scripts"]
import round_recovery, runids
mine = runids.rid("rnd-p")              # the same NAME another execution uses
child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                         env={**os.environ, "NEKAISE_RUN_ID": mine}, start_new_session=True)
print(json.dumps({"id": mine, "child": child.pid}), flush=True)
sys.stdin.readline()                     # the parent has run ITS recovery meanwhile
alive_before = round_recovery._alive(child.pid)
stopped = round_recovery.stop_processes(round_recovery.round_processes(mine))
print(json.dumps({"alive_before": alive_before, "stopped": stopped}), flush=True)
"""


def _sleeper(run_id: str) -> subprocess.Popen:
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"],
                            env={**os.environ, "NEKAISE_RUN_ID": run_id}, start_new_session=True)


def _visible(pid: int, run_id: str) -> None:
    import time
    for _ in range(100):  # the child's environment is readable once it has exec'd
        if pid in round_recovery.round_processes(run_id):
            return
        time.sleep(0.05)
    raise AssertionError(f"{pid} never carried {run_id}")


def test_recovery_in_one_test_execution_leaves_another_executions_children_alive():
    """Two concurrent executions of the suite (this process and a second Python process, each
    deriving ids from tests/runids.py) tag children with the same round NAME. Recovery of the
    round in either one stops only its own children: before, a fixed id let an independent
    suite's failing test kill a live round's pytest gate, and the reverse."""
    repo = Path(__file__).resolve().parents[1]
    other = subprocess.Popen([sys.executable, "-c", OTHER_EXECUTION, str(repo)],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    ours, survivor = _sleeper(rid("rnd-p")), None
    try:
        theirs = json.loads(other.stdout.readline())
        assert theirs["id"] != rid("rnd-p") and theirs["id"].startswith("rnd-p-")
        _visible(ours.pid, rid("rnd-p"))
        _visible(theirs["child"], theirs["id"])
        # this execution recovers its round: its child stops, the other execution's does not
        found = round_recovery.round_processes(rid("rnd-p"))
        assert ours.pid in found and theirs["child"] not in found
        round_recovery.stop_processes(found)
        assert not round_recovery._alive(ours.pid)
        # the other execution recovers ITS round: this execution's new child survives
        survivor = _sleeper(rid("rnd-p"))
        _visible(survivor.pid, rid("rnd-p"))
        other.stdin.write("go\n")
        other.stdin.flush()
        report = json.loads(other.stdout.readline())
        assert report["alive_before"] is True                  # our recovery spared it
        assert report["stopped"] == [theirs["child"]]          # theirs spared ours
        assert round_recovery._alive(survivor.pid)
    finally:
        for p in (ours, survivor, other):
            if p is not None:
                p.kill()
                try:
                    p.wait(5)
                except (subprocess.TimeoutExpired, ChildProcessError):
                    pass


def test_no_test_hands_a_fixed_run_id_to_children_or_recovery():
    """Every run id that reaches NEKAISE_RUN_ID, --run-id, --recover, --resume or a recovery
    that stops tagged processes comes from rid()."""
    import re
    # (what recovery matches: a child's NEKAISE_RUN_ID, and the ids rounds run and recover
    # under; a writer's round_id only names the lock holder, NEKAISE_STORE_ROUND is not matched)
    # (stage 4 step 4: --resume, the staged recovery and a failing staged round's recovery stop
    # the processes tagged with their run id too)
    fixed = re.compile(r'"NEKAISE_RUN_ID"\s*[:,]\s*["\'][^"\']|"--run-id",\s*["\']'
                       r'|"--recover",\s*["\'](?!latest)|recover_round\([^)]*,\s*["\'][^"\']'
                       r'|"--resume",\s*["\']|recover_staged\([^)]*,\s*["\'][^"\']'
                       r'|staged_round\([^)]*,\s*["\'][^"\']')
    offenders = []
    for path in sorted(Path(__file__).parent.glob("*.py")):
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if fixed.search(line):
                offenders.append(f"{path.name}:{n}: {line.strip()}")
    assert not offenders, "\n".join(offenders)
