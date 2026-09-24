#!/usr/bin/env python3
"""Operational primitives shared by the corpus control plane.

The registry and manifest are Git-tracked state, while multiple entrypoints (an interactive
agent, cron, and marathon) can mutate them.  This module provides:

* atomic file replacement (a crash never leaves half a JSON/YAML/README);
* advisory repository and named locks (two operators never run a growth round concurrently);
* a local append-only run ledger under logs/ for diagnosing interrupted automation.

The run ledger is deliberately git-ignored operational state.  Durable corpus provenance remains
in registry/, manifest/, and registry/pruned-*.jsonl.
"""
from __future__ import annotations

import fcntl
import json
import os
import shutil
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = ROOT / "workspace"
LOGS = ROOT / "logs"
RUN_LEDGER = LOGS / "run_history.jsonl"
SNAPSHOTS = WORKSPACE / "round-snapshots"


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Replace *path* atomically with *data*, fsyncing the file before os.replace."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        if path.exists():
            os.chmod(tmp, path.stat().st_mode)
        os.replace(tmp, path)
        # Persist the directory entry as well as the file contents. Without this fsync, a power
        # loss immediately after os.replace can still lose the rename on some filesystems.
        dir_fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    finally:
        if tmp.exists():
            tmp.unlink()


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode())


@contextmanager
def named_lock(name: str, timeout: float = 0, workspace: Path | None = None):
    """Hold an advisory lock in workspace/ (or another repo root's workspace/).

    timeout=0 fails immediately; a positive timeout waits that many seconds; timeout<0 waits
    forever.  The lock file contains the owning PID for useful error messages.
    """
    workspace = Path(workspace) if workspace is not None else WORKSPACE
    workspace.mkdir(parents=True, exist_ok=True)
    path = workspace / f".{name}.lock"
    f = path.open("a+")
    deadline = None if timeout < 0 else time.monotonic() + timeout
    while True:
        try:
            fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except BlockingIOError:
            if deadline is not None and time.monotonic() >= deadline:
                f.seek(0)
                owner = f.read().strip() or "unknown"
                f.close()
                raise RuntimeError(f"lock '{name}' is held by pid {owner}")
            time.sleep(0.1)
    f.seek(0)
    f.truncate()
    f.write(str(os.getpid()))
    f.flush()
    try:
        yield path
    finally:
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)
        f.close()


def append_jsonl(path: Path, row: dict) -> None:
    """Durably append one compact JSON row. Callers coordinate with an appropriate named lock."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as f:
        f.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        f.flush()
        os.fsync(f.fileno())


def run_event(run_id: str, event: str, **fields) -> None:
    """Append a local control-plane event; failures here must never hide the underlying result."""
    row = {
        "run_id": run_id,
        "event": event,
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        **fields,
    }
    try:
        with named_lock("run-ledger", timeout=10):
            append_jsonl(RUN_LEDGER, row)
    except Exception:
        pass


class StateSnapshot:
    """Persistent pre-round copy of tracked mutable state.

    Normal failures restore and delete it automatically. SIGKILL/power loss leaves it under
    workspace/round-snapshots so a later operator can explicitly recover without guessing which
    partially-written shards belong to the interrupted run.
    """

    def __init__(self, run_id: str, root: Path = ROOT):
        self.run_id = run_id
        self.root = Path(root)
        self.path = SNAPSHOTS / run_id
        self.meta_path = self.path / "snapshot.json"

    @classmethod
    def capture(cls, run_id: str, paths: tuple[str, ...], root: Path = ROOT):
        snap = cls(run_id, root)
        if snap.path.exists():
            raise RuntimeError(f"snapshot already exists for {run_id}")
        # Keep exclusive creation outside the cleanup guard: never remove an existing snapshot.
        snap.path.mkdir(parents=True)
        try:
            present = []
            for rel in paths:
                src = snap.root / rel
                if not src.exists():
                    continue
                present.append(rel)
                dst = snap.path / "state" / rel
                dst.parent.mkdir(parents=True, exist_ok=True)
                if src.is_dir():
                    shutil.copytree(src, dst)
                else:
                    shutil.copy2(src, dst)
            # Publish the recovery marker last. Callers must not mutate state until we return.
            atomic_write_text(snap.meta_path, json.dumps({
                "run_id": run_id,
                "root": str(snap.root),
                "paths": list(paths),
                "present": present,
            }, indent=2) + "\n")
        except BaseException:
            try:
                # A metadata fsync can fail after rename. Unpublish before deleting saved state
                # so a cleanup failure cannot leave a partial copy marked as recoverable.
                snap.meta_path.unlink(missing_ok=True)
                shutil.rmtree(snap.path)
            except OSError as cleanup_error:
                run_event(run_id, "snapshot_cleanup_failed", error=str(cleanup_error))
            raise
        return snap

    @classmethod
    def open(cls, run_id: str, root: Path = ROOT):
        snap = cls(run_id, root)
        if not snap.meta_path.exists():
            raise RuntimeError(f"no pending snapshot for {run_id}")
        return snap

    @classmethod
    def pending(cls) -> list[str]:
        if not SNAPSHOTS.exists():
            return []
        return sorted(p.name for p in SNAPSHOTS.iterdir() if (p / "snapshot.json").exists())

    @classmethod
    def incomplete_captures(cls) -> list[str]:
        """Report captures interrupted before the recovery marker; inspect under the round lock.

        SIGKILL can bypass capture's cleanup. Without metadata these directories cannot be
        restored, and no round mutation has begun, so they do not block later rounds.
        """
        if not SNAPSHOTS.exists():
            return []
        pending = set(cls.pending())
        return sorted(p.name for p in SNAPSHOTS.iterdir() if p.is_dir() and p.name not in pending)

    def restore(self) -> None:
        meta = json.loads(self.meta_path.read_text())
        present = set(meta["present"])
        for rel in meta["paths"]:
            target = self.root / rel
            if target.is_dir():
                shutil.rmtree(target)
            elif target.exists():
                target.unlink()
            if rel not in present:
                continue
            saved = self.path / "state" / rel
            target.parent.mkdir(parents=True, exist_ok=True)
            if saved.is_dir():
                shutil.copytree(saved, target)
            else:
                shutil.copy2(saved, target)

    def discard(self) -> None:
        if self.path.exists():
            shutil.rmtree(self.path)
