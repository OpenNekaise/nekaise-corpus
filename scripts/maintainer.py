#!/usr/bin/env python3
"""Bounded Codex-first maintenance with optional independent Claude review.

Take settled snapshots and perform mutations between rounds. Release growth locks during
read-only deliberation; reacquire and refresh evidence before any action. Provider exhaustion
defers that participant without silently promoting another model.
"""

from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
from collections import deque
from contextlib import ExitStack, contextmanager, nullcontext
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import ops
import round_recovery
import run_round
import store
import store_broker


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
PROMPTS = SCRIPTS / "maintainer_prompts"
SCHEMA = SCRIPTS / "maintainer_triage.schema.json"
WORKSPACE = ROOT / "workspace"
LOGS = ROOT / "logs"
HISTORY = LOGS / "maintainer_history.jsonl"
BLOCKED = WORKSPACE / ".maintenance-blocked"
QUOTA_RE = re.compile(
    r"usage limit|rate limit|limit reached|insufficient_quota|out of (?:usage )?credits|"
    r"too many requests|(?:http |status(?: code)? )429|exhausted your|resets? at",
    re.IGNORECASE,
)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def stamp(now: datetime | None = None) -> str:
    return (now or utc_now()).strftime("%Y%m%dT%H%M%SZ")


def _version_key(path: Path) -> tuple[int, ...]:
    match = re.search(r"/v(\d+(?:\.\d+)*)/", str(path))
    return tuple(int(piece) for piece in match.group(1).split(".")) if match else ()


def resolve_agent(name: str, override: str | None = None) -> Path | None:
    """Resolve cron-safe CLI paths, including Codex installed under nvm."""
    if override:
        path = Path(override).expanduser()
        return path if path.is_file() and os.access(path, os.X_OK) else None
    found = shutil.which(name)
    if found:
        return Path(found)
    candidates: list[Path] = []
    if name == "codex":
        candidates.extend(Path.home().glob(".nvm/versions/node/*/bin/codex"))
    candidates.append(Path.home() / ".local" / "bin" / name)
    usable = [path for path in candidates if path.is_file() and os.access(path, os.X_OK)]
    if not usable:
        return None
    return max(usable, key=lambda path: (_version_key(path), path.stat().st_mtime))


def agent_env(binary: Path) -> dict[str, str]:
    env = os.environ.copy()
    additions = [str(binary.parent), str(Path.home() / ".local/bin"), "/usr/local/bin", "/usr/bin", "/bin"]
    existing = env.get("PATH", "").split(os.pathsep)
    env["PATH"] = os.pathsep.join(dict.fromkeys(additions + existing))
    env["NO_COLOR"] = "1"
    return env


def is_quota_error(text: str) -> bool:
    return bool(QUOTA_RE.search(text))


def read_cooldown(provider: str, now: datetime | None = None) -> bool:
    path = WORKSPACE / f".maintainer-{provider}-cooldown"
    try:
        until = datetime.fromtimestamp(int(path.read_text().strip()), timezone.utc)
    except (FileNotFoundError, ValueError, OSError):
        return False
    return until > (now or utc_now())


def set_cooldown(provider: str, hours: int = 2, now: datetime | None = None) -> None:
    until = (now or utc_now()) + timedelta(hours=hours)
    path = WORKSPACE / f".maintainer-{provider}-cooldown"
    path.write_text(str(int(until.timestamp())) + "\n")


class MaintenanceBusy(RuntimeError):
    pass


# The store writer of the open maintenance window (this process holds the round lock), if any.
_WINDOW_WRITER = None  # (store, writer token, staged) while a window is open


@contextmanager
def window_writer(st, writer, staged: bool = False):
    global _WINDOW_WRITER
    previous, _WINDOW_WRITER = _WINDOW_WRITER, (st, writer, staged)
    try:
        yield writer
    finally:
        _WINDOW_WRITER = previous


@contextmanager
def exported_env(values: dict[str, str]):
    """Export `values` to every child started while the context is open (agent_env copies
    os.environ), restoring the previous values afterwards."""
    previous = {k: os.environ.get(k) for k in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, value in previous.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


@contextmanager
def hidden_env(*names: str):
    """Remove `names` from this process's environment for the block, restoring them after."""
    saved = {k: os.environ.pop(k) for k in names if k in os.environ}
    try:
        yield
    finally:
        os.environ.update(saved)


def open_file_store(what: str):
    """The store the host authority record selects (scripts/store_authority.py), which must be
    the file store (legacy callers). Any other authority raises (AuthorityError)."""
    import store_authority
    st = store.open(root=ROOT)
    store_authority.require_file_authority(st, f"maintainer ({what})")
    return st


def open_store(what: str):
    """(store, staged): the store the host authority record selects — the file store (the
    legacy window, broker and snapshot recovery), or, only when the record makes PostgreSQL
    authoritative, the PostgreSQL store (staged runs: ADR 0001 stage 4 step 4). An unbound
    PostgreSQL store raises (AuthorityError), never falls back."""
    import staged_runs
    import store_authority
    st = store.open(root=ROOT)
    if staged_runs.staged_authority(st):
        return st, True
    store_authority.require_file_authority(st, f"maintainer ({what})")
    return st, False


def is_staged() -> bool:
    """The open window's store is PostgreSQL-authoritative."""
    return _WINDOW_WRITER is not None and _WINDOW_WRITER[2]


class Window:
    """A held maintenance window: its writer, the broker its children write through (None when
    it serves none), and — under PostgreSQL authority, in the action phase — the maintenance
    run those children stage into (a store_broker.StagedRound).

    drain(): stop accepting mutations and wait for the executing one (cancellation-safe).
    conclude(ok): the window's outcome, decided once, after draining and before settled state is
    judged. File store: nothing more (every batch was its own transaction). Staged run: `ok` and
    something staged -> freeze, the standalone gates against the frozen state, promotion and its
    completion; otherwise abort (unpromoted runs default to abort). Returns what happened."""

    def __init__(self, st, writer, broker, run=None, staged=False, window_id=""):
        self.st, self.writer, self.broker, self.run = st, writer, broker, run
        self.staged, self.window_id = staged, window_id
        self.outcome: dict | None = None

    def drain(self) -> None:
        if self.broker is not None:
            self.broker.drain()

    def conclude(self, ok: bool) -> dict:
        self.drain()
        if self.outcome is not None:
            return self.outcome
        # this process itself reads committed state from here on: the run's pin and broker are
        # exported for the window's children only
        with hidden_env(store_broker.BROKER_ENV, store_broker.CAP_ENV, store_broker.ROUND_ENV,
                        store_broker.ATTEMPT_ENV, "NEKAISE_STORE_STAGE"):
            try:
                return self._conclude(ok)
            except Exception as exc:
                # decided now by the durable status (a promotion stands), so the growth block
                # judged right after never reports this window's own run as unfinished
                if self.run is not None and self.run.generation is None:
                    try:
                        round_recovery.recover_staged(
                            self.st, self.writer, self.run.run.run_id, root=ROOT, stop=False,
                            finish=False, reason=f"concluding failed: {exc}"[:500])
                    except Exception as rexc:
                        exc.add_note(f"recovering the maintenance run failed too: {rexc}")
                raise

    def _conclude(self, ok: bool) -> dict:
        import staged_runs
        import store_staging
        if self.run is None:
            self.outcome = {"run": None}
            return self.outcome
        run_id = self.run.run.run_id
        self.outcome = {"run": run_id, "status": "aborted"}
        status = store_staging.run_status(self.st, self.writer, run_id)
        if status is None or status["status"] != "open":
            self.outcome = {"run": run_id, "status": None if status is None else status["status"]}
            return self.outcome
        reason = None
        if not ok:
            reason = "the maintenance action did not succeed"
        elif status["staged_seq"] == 0:
            reason = "no-op: nothing staged"
        elif changed := staged_runs.identity_changed(ROOT, status):
            # the agent changed code or configuration meanwhile: its data mutations ran under
            # other code than the run records — promoted separately, in a later window
            reason = f"not promoted: {changed} changed during the window"
        if reason is not None:
            self.st.abort_run(self.writer, run_id, reason=reason)
            self.outcome["reason"] = reason
            return self.outcome
        self.run.freeze(list(staged_runs.STANDALONE_GATES))
        try:
            staged_runs.run_gates(self.run, ROOT, staged_runs.STANDALONE_GATES)
        except staged_runs.GateFailed as exc:
            self.st.abort_run(self.writer, run_id, reason=f"gates: {exc}"[:500])
            self.outcome["reason"] = str(exc)
            return self.outcome
        generation = self.run.promote()
        self.outcome = {"run": run_id, "status": "promoted", "generation": generation}
        try:
            staged_runs.after_promotion(self.st, self.writer, ROOT)
        except Exception as exc:   # the repair stands; recovery completes it
            self.outcome["completion_error"] = str(exc)
        return self.outcome


@contextmanager
def maintenance_window(phase: str):
    """Only the single maintainer owner may request a gap; lock order matches dig."""
    request = WORKSPACE / ".maintenance-requested"
    started = time.monotonic()
    wait = float(os.environ.get("MAINTAINER_LOCK_WAIT_SECONDS", "11700"))
    request.write_text(f"{os.getpid()} {phase} {utc_now().isoformat()}\n")
    try:
        with ExitStack() as locks:
            st, staged = open_store("the maintenance window")
            lifecycle = None
            try:
                locks.enter_context(ops.named_lock("continuous-dig", timeout=wait))
                remaining = max(0, wait - (time.monotonic() - started))
                if staged:
                    # the lifecycle lock before the writer (held through this window's snapshot
                    # phase, or until its maintenance run's mark is installed); a dead
                    # coordinator's forks may hold its writer session: they are stopped first
                    import run_ownership
                    lifecycle = locks.enter_context(run_ownership.lifecycle(ROOT, remaining))
                    run_ownership.sweep_dead(ROOT)
                    remaining = max(0, wait - (time.monotonic() - started))
                # The canonical round lock, held as the store's writer: while it is held every
                # child inherits read access (ops.named_lock exports it), and the window's broker
                # below gives children write access, so an agent's prune/rotation/blocklist
                # mutation runs as a store transaction instead of waiting on this very lock.
                # (PostgreSQL: the database's writer lock; children stage into the window's run.)
                writer = locks.enter_context(st.writer(timeout=remaining))
            except RuntimeError as exc:
                raise MaintenanceBusy(str(exc)) from exc
            locks.enter_context(window_writer(st, writer, staged))
            window_id = f"maint-{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{phase}"
            if staged:   # a run id: unique even for two windows within one second
                window_id += f"-{os.urandom(4).hex()}"
                window = locks.enter_context(_staged_window(st, writer, window_id, phase,
                                                            lifecycle))
            else:
                broker = store_broker.Broker(st, writer, window_id)
                locks.enter_context(broker.serving())
                locks.enter_context(exported_env(broker.env()))
                window = Window(st, writer, broker, window_id=window_id)
            request.unlink(missing_ok=True)
            acquired = time.monotonic()
            print(f"Maintenance {phase}: acquired growth locks after {acquired - started:.1f}s", flush=True)
            try:
                yield window
            finally:
                print(f"Maintenance {phase}: released growth locks after {time.monotonic() - acquired:.1f}s", flush=True)
    finally:
        request.unlink(missing_ok=True)


@contextmanager
def _staged_window(st, writer, window_id: str, phase: str, lifecycle=None):
    """The PostgreSQL window: the snapshot phase serves no broker (nothing may mutate while
    evidence is taken); the action phase first recovers any run a round left unfinished while
    the models deliberated (the shared routine), then opens ONE maintenance run whose staged
    broker the agent's store mutations go through. Leaving the window without a conclusion
    aborts that run (store_broker.staged_round recovers it by its durable status)."""
    import staged_runs
    if phase != "action":
        yield Window(st, writer, None, staged=True, window_id=window_id)
        return
    for out in round_recovery.recover_staged(st, writer, None, root=ROOT, finish=False,
                                             reason="recovered by the maintainer's action window"):
        ops.run_event(out.run_id, "run_recovered", recovered_by="ai_maintainer",
                      action=out.action, status=out.status)
    try:
        staged_runs.refuse_unfinished(st, writer)
        ident = staged_runs.identity(st, ROOT)
    except Exception as exc:
        print(f"Maintenance action: no maintenance run ({exc}); store mutations are refused in "
              "this window", flush=True)
        yield Window(st, writer, None, staged=True, window_id=window_id)
        return
    # tag=True: the window's agent (and everything it execs) carries NEKAISE_RUN_OWNER=<run>
    # from exec, and forks of this process inherit the run's ownership mark — so if the
    # maintainer is killed, any later recovery finds and stops them before it aborts the run
    with store_broker.staged_round(st, writer, window_id, kind="maintenance",
                                   producer_commit=ident.producer_commit,
                                   extractor_version=ident.extractor_version,
                                   cleaning_ruleset=ident.cleaning_ruleset,
                                   config_documents=ident.config, tag=True,
                                   lifecycle=lifecycle) as rnd:
        window = Window(st, writer, rnd.broker, rnd, staged=True, window_id=window_id)
        with exported_env(rnd.broker.env()):
            yield window
        window.conclude(ok=False)   # a window left without a conclusion aborts its run


@contextmanager
def adopt_agent_children():
    """Linux: keep orphaned tool sessions owned here even if the agent CLI exits first."""
    libc = ctypes.CDLL(None, use_errno=True)
    previous = ctypes.c_int()
    if libc.prctl(37, ctypes.byref(previous), 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "cannot inspect child-subreaper state")
    if libc.prctl(36, 1, 0, 0, 0) != 0:
        raise OSError(ctypes.get_errno(), "cannot supervise orphaned agent children")
    try:
        yield
    finally:
        libc.prctl(36, previous.value, 0, 0, 0)


descendants = round_recovery.descendants  # shared with the round recovery routine


def stop_process_group(process: subprocess.Popen, *, existing: set[int], grace: float = 2) -> None:
    """Stop and reap the owned tree, including tool sessions that called setsid()."""
    def stop(sig: int) -> set[int]:
        # Adopted orphans are now our children, so the CLI exiting cannot hide them.
        owned = descendants(os.getpid()) - existing
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
        for pid in owned:
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                pass
        return owned

    stop(signal.SIGTERM)
    deadline = time.monotonic() + grace
    while True:
        process.poll()
        owned = descendants(os.getpid()) - existing
        for pid in owned - {process.pid}:
            try:
                os.waitpid(pid, os.WNOHANG)
            except ChildProcessError:
                pass  # Still a grandchild until its immediate parent exits.
        if not (descendants(os.getpid()) - existing):
            break
        if time.monotonic() >= deadline:
            stop(signal.SIGKILL)
        time.sleep(0.05)
    process.wait()


def run_command(
    command: list[str],
    *,
    prompt: str | None,
    timeout: int,
    stdout_path: Path,
    stderr_path: Path,
    env: dict[str, str] | None = None,
    report: dict | None = None,
) -> int:
    """Run `command` supervised. `report`, when given, receives "survivors": the processes the
    command left running after it exited (stopped here) — work the command did not finish."""
    with adopt_agent_children(), stdout_path.open("w") as stdout, stderr_path.open("w") as stderr:
        existing = descendants(os.getpid())
        process = subprocess.Popen(
            command, cwd=ROOT, stdin=subprocess.PIPE if prompt is not None else subprocess.DEVNULL,
            text=True, stdout=stdout, stderr=stderr, env=env, start_new_session=True,
        )
        try:
            process.communicate(input=prompt, timeout=timeout)
            if report is not None:
                report["survivors"] = sorted(
                    pid for pid in descendants(os.getpid()) - existing - {process.pid}
                    if round_recovery._alive(pid))
            return process.returncode
        except subprocess.TimeoutExpired:
            stderr.write(f"\nmaintainer timeout after {timeout}s\n")
            return 124
        finally:
            stop_process_group(process, existing=existing)


def provider_quota(exit_code: int, stderr: Path, events: Path | None = None) -> bool:
    """Tool output and model prose are not provider errors; timeouts are never quota."""
    if exit_code in (0, 124):
        return False
    messages = [stderr.read_text(errors="replace")]
    if events is not None:
        with events.open(errors="replace") as handle:
            for line in handle:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(event, dict):
                    continue
                if event.get("type") in {"error", "turn.failed"}:
                    messages.append(json.dumps(event))
                elif event.get("type") == "result" and event.get("is_error") is True:
                    messages.append(str(event.get("result", "")))
    return is_quota_error("\n".join(messages))


def git(*args: str, timeout: int = 300) -> tuple[int, str]:
    with tempfile.TemporaryDirectory(prefix="maintainer-git-") as directory:
        stdout = Path(directory) / "stdout"
        stderr = Path(directory) / "stderr"
        code = run_command(
            ["git", *args], prompt=None, timeout=timeout, stdout_path=stdout, stderr_path=stderr,
        )
        return code, (stdout.read_text(errors="replace") + stderr.read_text(errors="replace")).strip()


def recover_pending_round() -> str | None:
    """Recover one interrupted round with the shared routine (scripts/round_recovery.py) under
    the canonical round lock: the open maintenance window's writer, or (outside a window) a
    writer taken here. Stops the round's orphaned processes, resolves store transactions, keeps a
    round that already committed (discarding its snapshot) or restores its snapshot, settles its
    prune quarantine, then discards the snapshot. Raises, keeping the snapshot, on any failure.

    PostgreSQL authority: the staged form of the same routine (round_recovery.recover_staged)
    over every unfinished run — orphans stopped, durable status decides (promoted stands,
    unpromoted is aborted), temporaries swept, the current generation's completion caught up.
    Returns the recovered run ids (comma-separated) or None."""
    with ExitStack() as stack:
        if _WINDOW_WRITER is not None:
            st, writer, staged = _WINDOW_WRITER
        else:
            st, staged = open_store("round recovery")
            if staged:   # the lifecycle lock and the dead coordinators' sweep, then the writer
                import run_ownership
                stack.enter_context(run_ownership.lifecycle(ROOT))
                run_ownership.sweep_dead(ROOT)
            writer = stack.enter_context(st.writer(timeout=0)) if staged else None
        if staged:
            outcomes = round_recovery.recover_staged(
                st, writer, None, root=ROOT, reason="recovered by the maintainer")
            for out in outcomes:
                ops.run_event(out.run_id, "run_recovered", recovered_by="ai_maintainer",
                              action=out.action, status=out.status)
            return ", ".join(o.run_id for o in outcomes) or None
    pending = ops.StateSnapshot.pending()
    if not pending:
        return None
    if len(pending) != 1:
        raise RuntimeError(f"refusing ambiguous recovery of {len(pending)} snapshots: {pending}")
    run_id = pending[0]
    with ExitStack() as stack:
        if _WINDOW_WRITER is not None:
            st, writer, _ = _WINDOW_WRITER
        else:
            st = open_file_store("round recovery")
            writer = stack.enter_context(st.writer(timeout=0))
        outcome = round_recovery.recover_round(st, writer, run_id, root=ROOT,
                                               snapshot_paths=run_round.SNAPSHOT_PATHS)
    fields = {"committed": outcome.commit} if outcome.action == "kept_committed" else {}
    ops.run_event(run_id, "run_recovered", recovered_by="ai_maintainer", **fields)
    return run_id


def verify_recovered_corpus() -> None:
    """Fail closed when restored tracked state disagrees with derived corpus files. (PostgreSQL
    authority: corpus/ must be a complete materialization of the current generation.)"""
    if is_staged():
        import staged_runs
        if not staged_runs.materialization_current(_WINDOW_WRITER[0], ROOT):
            raise RuntimeError("post-recovery check failed: corpus/ is not a complete "
                               "materialization of the current generation")
        return
    with tempfile.TemporaryDirectory(prefix="maintainer-check-") as directory:
        stdout = Path(directory) / "stdout"
        stderr = Path(directory) / "stderr"
        code = run_command(
            [sys.executable, str(SCRIPTS / "clean_corpus.py"), "--check"],
            prompt=None, timeout=900, stdout_path=stdout, stderr_path=stderr,
        )
        if code == 0:
            return
        diagnostics = (stdout.read_text(errors="replace") + stderr.read_text(errors="replace")).strip()[-4000:]
    detail = f": {diagnostics}" if diagnostics else ""
    raise RuntimeError(f"post-recovery corpus check failed (exit {code}){detail}")


def _runtime_fields(state: Any) -> tuple[bool, str | None]:
    """(enabled, reason) of one runtime backend state (store.BackendState or its JSON dict)."""
    if isinstance(state, dict):
        enabled, reason = state.get("enabled", True), state.get("reason")
    else:
        enabled, reason = getattr(state, "enabled", True), getattr(state, "reason", None)
    return enabled is not False, reason if isinstance(reason, str) else None


def summarize_backend_health(
    history: Path,
    backend_config: dict[str, Any],
    runtime: dict[str, Any] | None = None,
    *,
    window: int = 40,
) -> dict[str, Any]:
    """Summarize recent completed discovery rounds without scanning corpus metadata.

    Backends are reported by EFFECTIVE enablement: enabled in the configuration AND in the
    runtime state (`runtime`, name -> store.BackendState or {"enabled", "reason"}; ADR 0001 stage
    3, step 5). Configured backends that runtime state pauses (finder-reported exhaustion) are
    listed under "runtime_paused" with their reason and the completed round that disabled them.

    This is observation only: the result is included in the maintainer prompt and never feeds
    backend enablement, rotation, or growth-block decisions. Malformed local ledger lines are
    ignored so one damaged diagnostic event cannot prevent maintenance triage.
    """
    runtime = runtime or {}
    disabled_by: dict[str, dict[str, Any]] = {}
    window = max(1, window)
    completed: deque[dict[str, Any]] = deque(maxlen=window)
    pending: dict[str, dict[str, Any]] = {}
    lifetime: dict[str, dict[str, Any]] = {}
    try:
        lines = history.open(errors="replace")
    except OSError:
        lines = nullcontext(())
    with lines as handle:
        for line in handle:
            try:
                row = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(row, dict):
                continue
            run_id = row.get("run_id")
            event = row.get("event")
            if not isinstance(run_id, str) or not isinstance(event, str):
                continue
            if event not in {
                "discovery_merged",
                "discovery_degraded",
                "rotation_advanced",
                "rotation_held",
                "backend_disabled",
                "run_completed",
                "run_failed",
                "run_recovered",
            }:
                continue
            state = pending.setdefault(run_id, {
                "accepted": {},
                "degraded": set(),
                "rotations": [],
                "disabled": [],
                "merged_at": None,
                "completed_at": None,
            })
            if event == "discovery_merged":
                accepted = row.get("accepted")
                if isinstance(accepted, dict):
                    state["accepted"] = {
                        name: count
                        for name, count in accepted.items()
                        if isinstance(name, str)
                        and isinstance(count, int)
                        and not isinstance(count, bool)
                        and count >= 0
                    }
                if isinstance(row.get("at"), str):
                    state["merged_at"] = row["at"]
            elif event == "discovery_degraded":
                failures = row.get("failures")
                if isinstance(failures, dict):
                    state["degraded"].update(name for name in failures if isinstance(name, str))
            elif event in {"rotation_advanced", "rotation_held"}:
                backend = row.get("backend")
                if isinstance(backend, str):
                    rotation = {
                        "status": "advanced" if event == "rotation_advanced" else "held",
                        "at": row.get("at") if isinstance(row.get("at"), str) else None,
                    }
                    if event == "rotation_held" and isinstance(row.get("reason"), str):
                        rotation["reason"] = row["reason"]
                    if event == "rotation_held" and isinstance(row.get("detail"), str):
                        rotation["detail"] = row["detail"]
                    state["rotations"].append((backend, rotation))
            elif event == "backend_disabled":
                backend = row.get("backend")
                if isinstance(backend, str):
                    state["disabled"].append((backend, {
                        "run_id": run_id,
                        "at": row.get("at") if isinstance(row.get("at"), str) else None,
                        "reason": row.get("reason") if isinstance(row.get("reason"), str) else None,
                    }))
            elif event == "run_completed":
                state["completed_at"] = row.get("at") if isinstance(row.get("at"), str) else None
                # Only a committed round's exhaustion took effect (failed rounds roll back).
                for backend, disabled in state["disabled"]:
                    disabled_by[backend] = disabled
                for backend in state["accepted"].keys() | state["degraded"]:
                    health = lifetime.setdefault(backend, {
                        "degraded_streak": 0,
                        "zero_streak": 0,
                        "last_nonzero_at": None,
                        "last_rotation": None,
                        "last_hold_reason": None,
                        "last_hold_detail": None,
                    })
                    if backend in state["degraded"]:
                        health["degraded_streak"] += 1
                    else:
                        health["degraded_streak"] = 0
                    if backend in state["accepted"]:
                        count = state["accepted"][backend]
                        health["zero_streak"] = health["zero_streak"] + 1 if count == 0 else 0
                        if count > 0:
                            health["last_nonzero_at"] = (
                                state["merged_at"] or state["completed_at"]
                            )
                for backend, rotation in state["rotations"]:
                    health = lifetime.setdefault(backend, {
                        "degraded_streak": 0,
                        "zero_streak": 0,
                        "last_nonzero_at": None,
                        "last_rotation": None,
                        "last_hold_reason": None,
                        "last_hold_detail": None,
                    })
                    health["last_rotation"] = rotation
                    if rotation["status"] == "held":
                        health["last_hold_reason"] = rotation.get("reason")
                        health["last_hold_detail"] = rotation.get("detail")
                completed.append(state)
                pending.pop(run_id, None)
            elif event in {"run_failed", "run_recovered"}:
                pending.pop(run_id, None)

    configured = {
        name: config
        for name, config in backend_config.items()
        if isinstance(name, str) and isinstance(config, dict) and config.get("enabled") is True
    }
    selected = {
        name: config for name, config in configured.items()
        if _runtime_fields(runtime.get(name, {}))[0]
    }
    runtime_paused = {
        name: {
            "reason": _runtime_fields(runtime[name])[1],
            "disabled_by_round": disabled_by.get(name),
        }
        for name in sorted(set(configured) - set(selected))
    }
    total_accepted = sum(
        count
        for run in completed
        for count in run["accepted"].values()
    )
    backends: dict[str, dict[str, Any]] = {}
    for name in sorted(selected):
        observed = [
            run for run in completed
            if name in run["accepted"] or name in run["degraded"]
        ]
        rotations = [
            rotation
            for run in completed
            for backend, rotation in run["rotations"]
            if backend == name
        ]
        accepted_count = sum(run["accepted"].get(name, 0) for run in completed)
        config = selected[name]
        health = lifetime.get(name, {
            "degraded_streak": 0,
            "zero_streak": 0,
            "last_nonzero_at": None,
            "last_rotation": None,
            "last_hold_reason": None,
            "last_hold_detail": None,
        })
        backends[name] = {
            "observed_rounds": len(observed),
            "accepted": accepted_count,
            "accepted_share": round(accepted_count / total_accepted, 4) if total_accepted else None,
            "consecutive_degraded_rounds": health["degraded_streak"],
            "consecutive_zero_accepted_rounds": health["zero_streak"],
            "last_nonzero_at": health["last_nonzero_at"],
            "rotates": config.get("rotation", True) is not False,
            "pointer_advanced_in_window": any(
                rotation["status"] == "advanced" for rotation in rotations
            ),
            "last_rotation": health["last_rotation"],
            "last_hold_reason": health["last_hold_reason"],
            "last_hold_detail": health["last_hold_detail"],
        }
    return {
        "window_limit": window,
        "completed_rounds": len(completed),
        "total_accepted": total_accepted,
        "streaks_scope": "all_completed_rounds",
        "backends": backends,
        "runtime_paused": runtime_paused,
    }


def backend_control_state() -> tuple[dict[str, Any], dict[str, Any], str]:
    """(backend configuration, runtime backend state, source) for the health snapshot.

    Read through one store view — inside a maintenance window with the window's writer token (this
    process holds the round lock, so it may not wait for it). When no view can be opened (e.g. an
    unrecovered round snapshot) the same two documents are read as files, and the source says so.
    """
    try:
        with ExitStack() as stack:
            if _WINDOW_WRITER is not None:
                st, writer, _ = _WINDOW_WRITER
                view = stack.enter_context(st.read(writer=writer))
            else:
                view = stack.enter_context(store.open(root=ROOT).read(timeout=0))
            config = view.config_get().backends
            runtime = {name: {"enabled": state.enabled, "reason": state.reason}
                       for name, state in view.backend_state_get().items()}
            return config, runtime, "store_view"
    except (RuntimeError, OSError, ValueError, TypeError) as exc:
        if isinstance(exc, store.AuthorityError):
            raise  # never read around the authority record
        # unfenced reads of the same two documents (store.FileStore.peek / config_documents)
        st = store.open(root=ROOT)
        config = st.config_documents().get("backends.json", {})
        runtime = st.peek("backend_state")
        return config, runtime, f"files ({type(exc).__name__}: {exc})"[:300]


def repo_snapshot(
    fetch_result: str,
    *,
    automatic_recovery: str | None = None,
    automatic_recovery_error: str | None = None,
) -> dict[str, Any]:
    _, head = git("rev-parse", "HEAD")
    _, branch = git("branch", "--show-current")
    _, status = git("status", "--short", "--branch")
    rc, counts = git("rev-list", "--left-right", "--count", "origin/main...HEAD")
    behind = ahead = None
    if rc == 0:
        try:
            behind, ahead = (int(value) for value in counts.split())
        except (TypeError, ValueError):
            pass
    snapshots = ops.StateSnapshot.pending()
    history = LOGS / "run_history.jsonl"
    recent_events = []
    if history.exists():
        with history.open(errors="replace") as handle:
            recent_events = [line.rstrip() for line in deque(handle, maxlen=30)]
    recent_logs = sorted(LOGS.glob("dig-*.log"), key=lambda path: path.stat().st_mtime)[-3:]
    try:
        backend_config, runtime, source = backend_control_state()
        backend_health = summarize_backend_health(history, backend_config, runtime)
        backend_health["state_source"] = source
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        backend_health = {"error": f"could not summarize backend health: {exc}"}
    return {
        "checked_at": utc_now().isoformat(),
        "head": head,
        "branch": branch,
        "git_status": status,
        "behind_origin_main": behind,
        "ahead_of_origin_main": ahead,
        "pending_round_snapshots": snapshots,
        "incomplete_captures": ops.StateSnapshot.incomplete_captures(),
        "automatic_recovery": automatic_recovery,
        "automatic_recovery_error": automatic_recovery_error,
        "fetch_result": fetch_result[-2000:],
        "backend_health": backend_health,
        "recent_run_events": recent_events,
        "recent_dig_logs": [str(path.relative_to(ROOT)) for path in recent_logs],
        **({"store": staged_evidence()} if is_staged() else {}),
    }


# How long a triage pin keeps its generation reconstructible if the maintainer never releases it
# (it dies): the lock wait plus the triage, review and action budgets, rounded up.
TRIAGE_PIN_HOURS = 8


def staged_evidence() -> dict[str, Any]:
    """PostgreSQL authority: the store facts the models judge (read under the window's writer):
    the current generation, the projection and materialization it is folded / materialized to,
    and the runs left unfinished. Failures are reported, never hidden."""
    import materialize
    import store_staging
    st, writer, _ = _WINDOW_WRITER
    try:
        with st.read(writer=writer) as view:
            generation = view.generation
            provenance = view.provenance()
        unfinished = [{"run_id": r["run_id"], "status": r["status"], "kind": r["kind"],
                       "parent_generation": r["parent_generation"]}
                      for r in store_staging.unfinished_runs(st, writer)]
        stamp = materialize.read_stamp(ROOT / "corpus") or {}
        return {"authority": "postgres", "generation": generation,
                "cleaning_ruleset": (provenance or {}).get("cleaning_ruleset"),
                "unfinished_runs": unfinished,
                "materialization": {k: stamp.get(k) for k in ("state", "generation", "mode")}}
    except Exception as exc:
        return {"authority": "postgres", "error": f"{type(exc).__name__}: {exc}"[:500]}


def pin_triage_generation(holder: str) -> int | None:
    """Triage pins the generation it judges (without holding growth locks afterwards): the fold
    keeps it reconstructible (store_staging.read_generation) until the action releases the pin,
    or TRIAGE_PIN_HOURS pass. None: not PostgreSQL, or no generation yet."""
    if not is_staged():
        return None
    import store_staging
    st, writer, _ = _WINDOW_WRITER
    with st.read(writer=writer) as view:
        generation = view.generation
    if generation is None:
        return None
    until = (utc_now() + timedelta(hours=TRIAGE_PIN_HOURS)).isoformat()
    store_staging.pin_generation(st, writer, generation, holder=holder,
                                 reason="maintainer triage evidence", until=until)
    return generation


def release_triage_pin(holder: str, generation: int | None) -> None:
    """Release the triage pin (store_staging.release_pin: no writer needed — dropping a pin only
    lets the fold proceed). Called on every way out of a pass; the pin's expiry is the backstop."""
    if generation is None:
        return
    import store_staging
    st = _WINDOW_WRITER[0] if _WINDOW_WRITER is not None else open_store("triage pin")[0]
    store_staging.release_pin(st, generation, holder=holder)


def staged_block_reasons() -> list[str]:
    """PostgreSQL authority: settled-state reasons to block growth (read under the window's
    writer; a failed read blocks): unfinished runs, and an open integrity finding of the
    generation-range review."""
    import generation_review
    import store_staging
    st, writer, _ = _WINDOW_WRITER
    reasons = []
    try:
        left = store_staging.unfinished_runs(st, writer)
        if left:
            reasons.append(f"{len(left)} unfinished staged run(s): "
                           f"{', '.join(r['run_id'] for r in left)}")
        if why := generation_review.growth_block(st, writer):
            reasons.append(why)
    except Exception as exc:
        reasons.append(f"cannot read durable run or review state: {type(exc).__name__}: {exc}"
                       [:300])
    return reasons


def review_evidence(run_dir: Path, through: int | None) -> dict | None:
    """PostgreSQL authority: the generation-range review's evidence for the unreviewed range up
    to `through` (the pinned triage generation), read under the window's writer. The full
    evidence goes to run_dir/review-evidence.json, a compact summary (with its digest) into the
    snapshot. None when there is no generation to review."""
    import generation_review
    if not is_staged() or through is None:
        return None
    st, writer, _ = _WINDOW_WRITER
    try:
        ev = generation_review.evidence(st, writer, through=through, root=ROOT)
    except Exception as exc:
        return {"error": f"{type(exc).__name__}: {exc}"[:500]}
    (run_dir / "review-evidence.json").write_text(json.dumps(ev, indent=2) + "\n")
    return {**generation_review.summary(ev),
            "evidence_file": str((run_dir / "review-evidence.json").relative_to(ROOT)),
            "review_state": generation_review.state(st, writer)}


def generation_review_env() -> str:
    import generation_review
    return generation_review.VERDICT_ENV


def record_review_verdict(path: Path, shown: dict | None) -> dict | None:
    """PostgreSQL authority, action window: the verdict the action agent wrote (if any) is
    validated and recorded under the window's writer. It must be about exactly the range and
    evidence digest the maintainer showed triage and the action (`shown`, the snapshot's
    generation_review), and the evidence is recomputed and must still have that digest; the
    database keeps verdicts contiguous. Returns what happened (never raises: a refused verdict is
    reported and the range stays unreviewed)."""
    import generation_review
    if not is_staged() or not path.exists():
        return None
    st, writer, _ = _WINDOW_WRITER
    try:
        doc = generation_review.verdict_from_file(path)
        if not shown or "digest" not in shown:
            raise generation_review.ReviewError("no review evidence was shown in this pass")
        if (doc["through"], doc["evidence_digest"]) != (shown["range"][1], shown["digest"]):
            raise generation_review.ReviewError(
                f"the verdict is about generations through {doc['through']} with digest "
                f"{doc['evidence_digest'][:12]}; this pass showed through {shown['range'][1]} "
                f"with digest {shown['digest'][:12]}")
        after = generation_review.record(
            st, writer, through=doc["through"], verdict=doc["verdict"],
            reviewer="codex-maintainer", evidence_digest=doc["evidence_digest"],
            detail={"summary": doc["summary"], "findings": doc.get("findings", [])},
            resolves=doc.get("resolves", []), root=ROOT)
        return {"recorded": doc["verdict"], "through": doc["through"],
                "reviewed_through": after["reviewed_through"],
                "endorsed_through": after["endorsed_through"],
                "open_findings": after["open_findings"], "open_integrity": after["open_integrity"]}
    except Exception as exc:
        return {"refused": f"{type(exc).__name__}: {exc}"[:500]}


def block_reasons() -> list[str]:
    reasons: list[str] = []
    rc, branch = git("branch", "--show-current")
    if rc != 0 or branch != "main":
        reasons.append(f"expected branch main, found {branch or 'unknown'}")
    rc, porcelain = git("status", "--porcelain", "--untracked-files=no")
    if rc != 0 or porcelain:
        reasons.append("tracked worktree is not clean")
    snapshots = ops.StateSnapshot.pending()
    if snapshots:
        reasons.append(f"{len(snapshots)} interrupted round snapshot(s) pending")
    git_dir = ROOT / ".git"
    for marker in ("MERGE_HEAD", "REBASE_HEAD", "CHERRY_PICK_HEAD", "BISECT_LOG"):
        if (git_dir / marker).exists():
            reasons.append(f"Git operation is incomplete: {marker}")
    rc, counts = git("rev-list", "--left-right", "--count", "origin/main...HEAD")
    if rc == 0:
        try:
            behind, ahead = (int(value) for value in counts.split())
            if behind:
                reasons.append(f"local main is {behind} commit(s) behind origin/main (ahead {ahead})")
        except ValueError:
            pass
    if is_staged():
        reasons.extend(staged_block_reasons())
    return reasons


def update_growth_block() -> list[str]:
    reasons = block_reasons()
    if reasons:
        BLOCKED.write_text("; ".join(reasons) + "\n")
    else:
        BLOCKED.unlink(missing_ok=True)
    return reasons


def record(event: dict[str, Any]) -> None:
    event = {"at": utc_now().isoformat(), **event}
    with HISTORY.open("a") as handle:
        handle.write(json.dumps(event, sort_keys=True) + "\n")


def load_triage(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text())
    required = {"needs_action", "action_kind", "urgency", "summary", "evidence", "proposed_actions"}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("Codex triage did not match the required schema")
    if not isinstance(value["needs_action"], bool):
        raise ValueError("Codex triage needs_action is not boolean")
    if value["urgency"] not in {"none", "routine", "urgent"}:
        raise ValueError("Codex triage urgency is invalid")
    if value["action_kind"] not in {"none", "publish", "repair", "improve"}:
        raise ValueError("Codex triage action_kind is invalid")
    if value["needs_action"] != (value["action_kind"] != "none"):
        raise ValueError("Codex triage action_kind contradicts needs_action")
    if not isinstance(value["summary"], str):
        raise ValueError("Codex triage summary is not text")
    for key in ("evidence", "proposed_actions"):
        if not isinstance(value[key], list) or not all(isinstance(item, str) for item in value[key]):
            raise ValueError(f"Codex triage {key} is not a list of text")
    return value


def render(name: str, **replacements: str) -> str:
    text = (PROMPTS / name).read_text()
    for key, value in replacements.items():
        text = text.replace("{{" + key + "}}", value)
    return text


def run_maintenance() -> int:
    WORKSPACE.mkdir(exist_ok=True)
    LOGS.mkdir(exist_ok=True)
    run_id = stamp()
    run_dir = LOGS / f"maintainer-{run_id}"
    run_dir.mkdir()
    print(f"[{utc_now().isoformat()}] maintainer start {run_id}", flush=True)

    codex = resolve_agent("codex", os.environ.get("CODEX_BIN"))
    if codex is None:
        record({"run_id": run_id, "status": "codex_missing"})
        print("Codex CLI is unavailable; Claude was not promoted over the primary maintainer.")
        return 1
    if read_cooldown("codex"):
        record({"run_id": run_id, "status": "codex_cooldown"})
        print("Codex cooldown is still active; deferred without pausing growth or calling Claude.")
        return 0

    # Ref updates do not touch the growing corpus; do not hold growth locks during network I/O.
    fetch_rc, fetch_output = git("fetch", "--prune", "origin", timeout=120)
    fetch_result = f"exit={fetch_rc}\n{fetch_output}" if fetch_output else f"exit={fetch_rc}"
    with maintenance_window("snapshot") as window:
        recovered = None
        recovery_error = None
        try:
            recovered = recover_pending_round()
            if recovered:
                verify_recovered_corpus()
                print(f"Recovered interrupted corpus round {recovered} before agent triage.", flush=True)
        except Exception as exc:
            recovery_error = str(exc)
            print(f"Automatic round recovery needs agent attention: {recovery_error}", flush=True)
        window.drain()  # nothing may still be writing when settled state is judged
        snapshot = repo_snapshot(
            fetch_result, automatic_recovery=recovered, automatic_recovery_error=recovery_error,
        )
        pin_holder = f"maintainer-{run_id}"
        try:
            snapshot["triage_generation"] = pin_triage_generation(pin_holder)
        except Exception as exc:
            snapshot["triage_generation"] = None
            snapshot["triage_pin_error"] = f"{type(exc).__name__}: {exc}"[:300]
        if is_staged():   # publication review is a generation-range review under PostgreSQL
            snapshot["generation_review"] = review_evidence(run_dir,
                                                            snapshot["triage_generation"])
        reasons = update_growth_block()
    (run_dir / "repo-snapshot.json").write_text(json.dumps(snapshot, indent=2) + "\n")

    def triage_and_act() -> int:
        nonlocal reasons

        staged_notes = render("staged.md") if "store" in snapshot else ""   # PostgreSQL authority
        triage_prompt = render("triage.md") + staged_notes + "\n\n<repository_snapshot>\n" + json.dumps(snapshot, indent=2) + "\n</repository_snapshot>\n"
        triage_out = run_dir / "codex-triage.json"
        triage_events = run_dir / "codex-triage.events.jsonl"
        triage_errors = run_dir / "codex-triage.stderr.log"
        triage_cmd = [
            str(codex), "exec", "--ephemeral", "--sandbox", "read-only", "--color", "never",
            "--output-schema", str(SCHEMA), "--output-last-message", str(triage_out), "--json",
            "-C", str(ROOT), "-",
        ]
        triage_rc = run_command(
            triage_cmd, prompt=triage_prompt, timeout=int(os.environ.get("CODEX_TRIAGE_TIMEOUT", "600")),
            stdout_path=triage_events, stderr_path=triage_errors, env=agent_env(codex),
        )
        if triage_rc != 0 or not triage_out.exists():
            status = "codex_quota" if provider_quota(triage_rc, triage_errors, triage_events) else "codex_triage_failed"
            if status == "codex_quota":
                set_cooldown("codex")
            record({"run_id": run_id, "status": status, "exit": triage_rc, "growth_blocked": reasons})
            print(f"Codex triage deferred: {status} (exit {triage_rc}); Claude was not called.")
            return 0 if status == "codex_quota" else 1

        try:
            triage = load_triage(triage_out)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            record({"run_id": run_id, "status": "invalid_triage", "error": str(exc), "growth_blocked": reasons})
            print(f"Invalid Codex triage: {exc}")
            return 1

        if not triage["needs_action"]:
            record({"run_id": run_id, "status": "healthy_no_action", "triage": triage, "growth_blocked": reasons})
            print(f"Codex found no action: {triage['summary']}")
            return 0

        claude = resolve_agent("claude", os.environ.get("CLAUDE_BIN"))
        claude_review = "Claude Code was unavailable; Codex must proceed from its own evidence."
        claude_status = "missing"
        if triage["action_kind"] == "publish":
            claude_status = "not_needed"
            claude_review = "Publication-only pass: Codex reviews the complete outgoing range under the action lock. No machinery or policy edits are authorized by this proposal."
        elif claude is not None and not read_cooldown("claude"):
            review_prompt = render("claude_review.md", CODEX_TRIAGE=json.dumps(triage, indent=2),
                                   REPOSITORY_SNAPSHOT=json.dumps(snapshot, indent=2))
            review_out = run_dir / "claude-review.json"
            review_errors = run_dir / "claude-review.stderr.log"
            claude_cmd = [
                str(claude), "--print", "--no-session-persistence", "--tools", "",
                "--disable-slash-commands", "--output-format", "json",
                "--model", os.environ.get("CLAUDE_REVIEW_MODEL", "claude-opus-5-5"),
                "--effort", os.environ.get("CLAUDE_REVIEW_EFFORT", "xhigh"),
            ]
            review_rc = run_command(
                claude_cmd, prompt=review_prompt, timeout=int(os.environ.get("CLAUDE_REVIEW_TIMEOUT", "300")),
                stdout_path=review_out, stderr_path=review_errors, env=agent_env(claude),
            )
            try:
                review = json.loads(review_out.read_text())
                valid_review = (isinstance(review, dict) and review.get("is_error") is not True
                                and isinstance(review.get("result"), str) and bool(review["result"].strip()))
            except (ValueError, OSError):
                valid_review = False
            if review_rc == 0 and valid_review:
                claude_review = review["result"][-12000:]
                claude_status = "reviewed"
            elif provider_quota(review_rc or 1, review_errors, review_out):
                set_cooldown("claude")
                claude_status = "quota"
                claude_review = "Claude Code hit its usage limit. Codex remains primary and may proceed cautiously."
            else:
                claude_status = f"failed_exit_{review_rc}"
                claude_review = "Claude Code review failed for a non-quota reason. Codex remains primary; inspect logs and proceed cautiously."
        elif claude is not None:
            claude_status = "cooldown"
            claude_review = "Claude Code is in a usage cooldown. Codex remains primary and may proceed cautiously."

        with maintenance_window("action") as window:
            # A round may have completed or failed while models deliberated. Never recover or
            # act on the earlier snapshot without refreshing state under both locks.
            action_snapshot = repo_snapshot(fetch_result, automatic_recovery=recovered,
                                            automatic_recovery_error=recovery_error)
            action_snapshot["triage_head"] = snapshot["head"]
            action_snapshot["changed_since_triage"] = action_snapshot["head"] != snapshot["head"]
            if "store" in action_snapshot:   # code AND generation evidence are refreshed
                action_snapshot["triage_generation"] = snapshot.get("triage_generation")
                action_snapshot["changed_since_triage"] |= (
                    action_snapshot["store"].get("generation") != snapshot.get("triage_generation"))
            (run_dir / "action-snapshot.json").write_text(json.dumps(action_snapshot, indent=2) + "\n")
            action_prompt = render(
                "action.md",
                CODEX_TRIAGE=json.dumps(triage, indent=2),
                CLAUDE_REVIEW=claude_review,
                ACTION_SNAPSHOT=json.dumps(action_snapshot, indent=2),
            ) + (render("staged.md") if "store" in action_snapshot else "")
            action_out = run_dir / "codex-action.txt"
            action_events = run_dir / "codex-action.events.jsonl"
            action_errors = run_dir / "codex-action.stderr.log"
            action_cmd = [
                str(codex), "exec", "--ephemeral", "--sandbox", "danger-full-access", "--color", "never",
                "--output-last-message", str(action_out), "--json", "-C", str(ROOT), "-",
            ]
            action_rc, status = None, "codex_action_interrupted"
            verdict_file = run_dir / "review-verdict.json"
            action_env = agent_env(codex)
            if "store" in action_snapshot:
                action_env[generation_review_env()] = str(verdict_file)
            action_report: dict = {}
            try:
                action_rc = run_command(
                    action_cmd, prompt=action_prompt, timeout=int(os.environ.get("CODEX_ACTION_TIMEOUT", "1800")),
                    stdout_path=action_events, stderr_path=action_errors, env=action_env,
                    report=action_report,
                )
                if action_rc != 0 and provider_quota(action_rc, action_errors, action_events):
                    set_cooldown("codex")
                    status = "codex_action_quota"
                else:
                    status = "action_completed" if action_rc == 0 else "codex_action_failed"
            finally:
                # Killing a timed-out agent does not stop a transaction the window's broker is
                # executing for it: stop accepting and drain BEFORE judging settled state, still under
                # both locks (drain defers an interrupt until it is done). A staged window then
                # promotes its maintenance run (gated) or aborts it.
                outcome = review = None
                try:
                    try:
                        # a repair is promoted only when the action finished: exit 0 and no tool of
                        # it left running (those were just stopped — their work is incomplete)
                        outcome = window.conclude(ok=action_rc == 0 and status == "action_completed"
                                                  and not action_report.get("survivors"))
                        # still under both locks: the verdict on exactly the range triage showed
                        review = record_review_verdict(verdict_file, snapshot.get("generation_review"))
                    finally:
                        release_triage_pin(f"maintainer-{run_id}", snapshot.get("triage_generation"))
                finally:
                    reasons = update_growth_block()
            record({
                "run_id": run_id,
                "status": status,
                "exit": action_rc,
                "triage": triage,
                "claude_status": claude_status,
                "growth_blocked": reasons,
                **({"maintenance_run": outcome} if outcome and outcome.get("run") else {}),
                **({"review": review} if review else {}),
            })
            print(f"Codex action: {status}; Claude: {claude_status}; growth block: {reasons or 'none'}")
            if action_out.exists():
                print(action_out.read_text(errors="replace")[-8000:])
            return 0 if action_rc == 0 or status == "codex_action_quota" else 1

    try:
        return triage_and_act()
    finally:
        # on every way out (no action, a failed or invalid triage, the action's end): the
        # generation triage pinned is released (the pin's expiry is only the backstop)
        release_triage_pin(f"maintainer-{run_id}", snapshot.get("triage_generation"))


def main() -> int:
    WORKSPACE.mkdir(exist_ok=True)
    LOGS.mkdir(exist_ok=True)
    previous = signal.getsignal(signal.SIGTERM)

    def terminate(signum, frame):
        # Raise through run_command/finally before releasing any action lock.
        raise KeyboardInterrupt("maintenance terminated")

    signal.signal(signal.SIGTERM, terminate)
    try:
        with ExitStack() as owner:
            try:
                owner.enter_context(ops.named_lock("maintainer"))
            except RuntimeError:
                print("Another maintainer is active; deferred.")
                return 0
            try:
                return run_maintenance()
            except MaintenanceBusy as exc:
                record({"status": "window_unavailable", "error": str(exc)})
                print(f"Maintenance deferred: {exc}")
                return 0
    except KeyboardInterrupt:
        print("Maintenance stopped; owned agent processes were terminated.", flush=True)
        return 130
    finally:
        signal.signal(signal.SIGTERM, previous)


if __name__ == "__main__":
    sys.exit(main())
