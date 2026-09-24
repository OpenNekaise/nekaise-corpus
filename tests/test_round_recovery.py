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
    interrupted_round(repo, "rnd-c", commit=True)
    committed = tracked(repo, journal=True)
    head = git(repo, "rev-parse", "HEAD")
    assert recover(entry, repo, "rnd-c", monkeypatch) == 0
    assert tracked(repo, journal=True) == committed  # the commit stands
    assert git(repo, "status", "--porcelain") == ""
    assert not ops.StateSnapshot.pending()
    assert ("round_already_committed", {"commit": head}) in events
    fields = {"committed": head} | ({"recovered_by": "ai_maintainer"}
                                    if entry == "maintainer" else {})
    assert ("run_recovered", fields) in events


@pytest.mark.parametrize("entry", ["recover", "maintainer"])
def test_an_uncommitted_round_is_restored(repo, events, monkeypatch, entry):
    before = interrupted_round(repo, "rnd-u", commit=False)
    git(repo, "add", "manifest")  # a partial stage the round left behind
    assert recover(entry, repo, "rnd-u", monkeypatch) == 0
    assert tracked(repo, journal=True) == before
    assert git(repo, "status", "--porcelain") == ""  # unstaged and restored
    assert not ops.StateSnapshot.pending()
    assert not any(e == "round_already_committed" for e, _ in events)


@pytest.mark.parametrize("entry", ["recover", "maintainer"])
def test_a_committed_round_changed_since_is_left_for_an_operator(repo, events, monkeypatch, entry):
    interrupted_round(repo, "rnd-d", commit=True)
    (repo / "pruned_urls.txt").write_text("https://e.org/edited-after-the-commit\n")
    now = tracked(repo, journal=True)
    assert recover(entry, repo, "rnd-d", monkeypatch) == 1
    assert tracked(repo, journal=True) == now  # neither restored nor discarded
    assert ops.StateSnapshot.pending() == ["rnd-d"]


def test_a_known_commit_without_its_trailer_is_never_restored(repo):
    interrupted_round(repo, "rnd-k", commit=False)
    st = store.FileStore(repo)
    now = tracked(repo, journal=True)
    with st.writer(round_id="rnd-k", recovering=True) as w:
        with pytest.raises(round_recovery.RecoveryError, match="reports its commit"):
            round_recovery.recover_round(st, w, "rnd-k", root=repo,
                                         snapshot_paths=run_round.SNAPSHOT_PATHS,
                                         known_committed=True)
    assert tracked(repo, journal=True) == now
    assert ops.StateSnapshot.pending() == ["rnd-k"]


@pytest.mark.parametrize("entry", ["recover", "maintainer"])
def test_the_round_s_orphaned_processes_are_stopped_first(repo, events, monkeypatch, entry):
    """A killed round's children outlive it (a fetch still downloading, a prune still moving
    bytes). Recovery stops every process tagged with the round's run id before it resolves
    anything, and leaves other processes alone."""
    interrupted_round(repo, "rnd-o", commit=False)
    sleeper = [sys.executable, "-c", "import time; time.sleep(60)"]
    orphan = subprocess.Popen(sleeper, env={**os.environ, "NEKAISE_RUN_ID": "rnd-o"},
                              start_new_session=True)
    other = subprocess.Popen(sleeper, env={**os.environ, "NEKAISE_RUN_ID": "rnd-other"},
                             start_new_session=True)
    try:
        assert recover(entry, repo, "rnd-o", monkeypatch) == 0
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
                                      "--commit", "--push", branch, "--run-id", "rnd-push"])
    assert run_round.main() == 1
    assert "Corpus run: rnd-push" in git(repo, "log", "-1", "--format=%B")
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
                                      "--commit", "--run-id", "rnd-fail"])
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
    interrupted_round(repo, "rnd-g", commit=True)
    committed = tracked(repo, journal=True)
    if fail in ("symbolic-ref", "for-each-ref"):  # reached only when HEAD does not resolve
        monkeypatch.setattr(round_recovery, "_git", _failing_git(
            _failing_git(round_recovery._git, fail="rev-parse"), fail=fail))
    else:
        monkeypatch.setattr(round_recovery, "_git", _failing_git(round_recovery._git, fail=fail))
    assert recover(entry, repo, "rnd-g", monkeypatch) == 1
    assert tracked(repo, journal=True) == committed  # never restored over the commit
    assert ops.StateSnapshot.pending() == ["rnd-g"]


@pytest.mark.parametrize("corruption", ["garbage", "dangling", "missing-branch"])
def test_a_corrupt_head_keeps_the_committed_round_and_its_snapshot(repo, events, monkeypatch,
                                                                     corruption):
    interrupted_round(repo, "rnd-h", commit=True)
    committed = tracked(repo, journal=True)
    head = repo / ".git" / "HEAD"
    if corruption == "garbage":
        head.write_text("this is not a ref\n")
    elif corruption == "dangling":
        head.write_text("0123456789abcdef0123456789abcdef01234567\n")  # no such object
    else:  # HEAD names a branch that does not exist, while the commits are on another
        head.write_text("ref: refs/heads/no-such-branch\n")
    assert recover("recover", repo, "rnd-h", monkeypatch) == 1
    assert tracked(repo, journal=True) == committed
    assert ops.StateSnapshot.pending() == ["rnd-h"]


def test_an_unborn_repository_is_a_legitimate_no_commit(tmp_path, events, monkeypatch):
    """A repository without any commit cannot hold a committed round: recovery restores."""
    root = write_repo(tmp_path / "u", entries=[ENTRY], manifest=[ROW])
    git(root, "init", "-q")
    git(root, "add", "manifest")  # staged, never committed
    monkeypatch.setattr(run_round, "ROOT", root)
    monkeypatch.setattr(ops, "SNAPSHOTS", root / "workspace" / "round-snapshots")
    monkeypatch.setattr(ops, "WORKSPACE", root / "workspace")
    assert round_recovery.repo_state(root) == "unborn"
    before = interrupted_round(root, "rnd-n", commit=False)
    assert recover("recover", root, "rnd-n", monkeypatch) == 0
    assert tracked(root, journal=True) == before
    assert not ops.StateSnapshot.pending()
    assert "manifest" not in git(root, "diff", "--cached", "--name-only")  # unstaged


def test_no_repository_is_a_legitimate_no_commit(tmp_path):
    root = tmp_path / "plain"
    root.mkdir()
    assert round_recovery.repo_state(root) == "none"
    assert round_recovery.committed_round(root, "rnd-x") is None


# --- a deleted tracked path is a change too (Codex review of step 7, P2) ----------------------

@pytest.mark.parametrize("entry", ["recover", "maintainer"])
def test_a_tracked_path_deleted_after_the_commit_keeps_the_snapshot(repo, events, monkeypatch,
                                                                    entry):
    interrupted_round(repo, "rnd-x", commit=True)
    (repo / "pruned_urls.txt").unlink()
    now = tracked(repo, journal=True)
    assert recover(entry, repo, "rnd-x", monkeypatch) == 1
    assert tracked(repo, journal=True) == now
    assert not (repo / "pruned_urls.txt").exists()
    assert ops.StateSnapshot.pending() == ["rnd-x"]
    assert not any(e == "round_already_committed" for e, _ in events)
