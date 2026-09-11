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
import run_round


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


@contextmanager
def maintenance_window(phase: str):
    """Only the single maintainer owner may request a gap; lock order matches dig."""
    request = WORKSPACE / ".maintenance-requested"
    started = time.monotonic()
    wait = float(os.environ.get("MAINTAINER_LOCK_WAIT_SECONDS", "11700"))
    request.write_text(f"{os.getpid()} {phase} {utc_now().isoformat()}\n")
    try:
        with ExitStack() as locks:
            try:
                locks.enter_context(ops.named_lock("continuous-dig", timeout=wait))
                remaining = max(0, wait - (time.monotonic() - started))
                locks.enter_context(ops.named_lock("corpus-round", timeout=remaining))
            except RuntimeError as exc:
                raise MaintenanceBusy(str(exc)) from exc
            request.unlink(missing_ok=True)
            acquired = time.monotonic()
            print(f"Maintenance {phase}: acquired growth locks after {acquired - started:.1f}s", flush=True)
            try:
                yield
            finally:
                print(f"Maintenance {phase}: released growth locks after {time.monotonic() - acquired:.1f}s", flush=True)
    finally:
        request.unlink(missing_ok=True)


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


def descendants(parent: int) -> set[int]:
    children: dict[int, list[int]] = {}
    for stat in Path("/proc").glob("[0-9]*/stat"):
        try:
            fields = stat.read_text().rsplit(") ", 1)[1].split()
            children.setdefault(int(fields[1]), []).append(int(stat.parent.name))
        except (OSError, ValueError, IndexError):
            continue  # Processes can exit during enumeration.
    found: set[int] = set()
    pending = list(children.get(parent, []))
    while pending:
        pid = pending.pop()
        if pid not in found:
            found.add(pid)
            pending.extend(children.get(pid, []))
    return found


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
) -> int:
    with adopt_agent_children(), stdout_path.open("w") as stdout, stderr_path.open("w") as stderr:
        existing = descendants(os.getpid())
        process = subprocess.Popen(
            command, cwd=ROOT, stdin=subprocess.PIPE if prompt is not None else subprocess.DEVNULL,
            text=True, stdout=stdout, stderr=stderr, env=env, start_new_session=True,
        )
        try:
            process.communicate(input=prompt, timeout=timeout)
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
    """Restore one interrupted round while the caller owns the canonical round lock."""
    pending = ops.StateSnapshot.pending()
    if not pending:
        return None
    if len(pending) != 1:
        raise RuntimeError(f"refusing ambiguous recovery of {len(pending)} snapshots: {pending}")
    run_id = pending[0]
    snapshot = ops.StateSnapshot.open(run_id, root=ROOT)
    code, diagnostics = git("restore", "--staged", "--", *run_round.SNAPSHOT_PATHS)
    if code != 0:
        raise RuntimeError(f"could not unstage interrupted round state: {diagnostics}")
    snapshot.restore()
    snapshot.discard()
    ops.run_event(run_id, "run_recovered", recovered_by="ai_maintainer")
    return run_id


def verify_recovered_corpus() -> None:
    """Fail closed when restored tracked state disagrees with derived corpus files."""
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


def summarize_backend_health(
    history: Path,
    backend_config: dict[str, Any],
    *,
    window: int = 40,
) -> dict[str, Any]:
    """Summarize recent completed discovery rounds without scanning corpus metadata.

    This is observation only: the result is included in the maintainer prompt and never feeds
    backend enablement, rotation, or growth-block decisions. Malformed local ledger lines are
    ignored so one damaged diagnostic event cannot prevent maintenance triage.
    """
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
                "run_completed",
                "run_failed",
                "run_recovered",
            }:
                continue
            state = pending.setdefault(run_id, {
                "accepted": {},
                "degraded": set(),
                "rotations": [],
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
            elif event == "run_completed":
                state["completed_at"] = row.get("at") if isinstance(row.get("at"), str) else None
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

    selected = {
        name: config
        for name, config in backend_config.items()
        if isinstance(name, str) and isinstance(config, dict) and config.get("enabled") is True
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
    }


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
    snapshots = sorted(
        path.name for path in (WORKSPACE / "round-snapshots").glob("*") if path.is_dir()
    )
    history = LOGS / "run_history.jsonl"
    recent_events = []
    if history.exists():
        with history.open(errors="replace") as handle:
            recent_events = [line.rstrip() for line in deque(handle, maxlen=30)]
    recent_logs = sorted(LOGS.glob("dig-*.log"), key=lambda path: path.stat().st_mtime)[-3:]
    try:
        backend_config = json.loads((ROOT / "registry" / "backends.json").read_text())
        backend_health = summarize_backend_health(history, backend_config)
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
        "automatic_recovery": automatic_recovery,
        "automatic_recovery_error": automatic_recovery_error,
        "fetch_result": fetch_result[-2000:],
        "backend_health": backend_health,
        "recent_run_events": recent_events,
        "recent_dig_logs": [str(path.relative_to(ROOT)) for path in recent_logs],
    }


def block_reasons() -> list[str]:
    reasons: list[str] = []
    rc, branch = git("branch", "--show-current")
    if rc != 0 or branch != "main":
        reasons.append(f"expected branch main, found {branch or 'unknown'}")
    rc, porcelain = git("status", "--porcelain", "--untracked-files=no")
    if rc != 0 or porcelain:
        reasons.append("tracked worktree is not clean")
    snapshots = [path for path in (WORKSPACE / "round-snapshots").glob("*") if path.is_dir()]
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
    with maintenance_window("snapshot"):
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
        snapshot = repo_snapshot(
            fetch_result, automatic_recovery=recovered, automatic_recovery_error=recovery_error,
        )
        reasons = update_growth_block()
    (run_dir / "repo-snapshot.json").write_text(json.dumps(snapshot, indent=2) + "\n")

    triage_prompt = render("triage.md") + "\n\n<repository_snapshot>\n" + json.dumps(snapshot, indent=2) + "\n</repository_snapshot>\n"
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
            "--model", os.environ.get("CLAUDE_REVIEW_MODEL", "claude-opus-5"),
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

    with maintenance_window("action"):
        # A round may have completed or failed while models deliberated. Never recover or
        # act on the earlier snapshot without refreshing state under both locks.
        action_snapshot = repo_snapshot(fetch_result, automatic_recovery=recovered,
                                        automatic_recovery_error=recovery_error)
        action_snapshot["triage_head"] = snapshot["head"]
        action_snapshot["changed_since_triage"] = action_snapshot["head"] != snapshot["head"]
        (run_dir / "action-snapshot.json").write_text(json.dumps(action_snapshot, indent=2) + "\n")
        action_prompt = render(
            "action.md",
            CODEX_TRIAGE=json.dumps(triage, indent=2),
            CLAUDE_REVIEW=claude_review,
            ACTION_SNAPSHOT=json.dumps(action_snapshot, indent=2),
        )
        action_out = run_dir / "codex-action.txt"
        action_events = run_dir / "codex-action.events.jsonl"
        action_errors = run_dir / "codex-action.stderr.log"
        action_cmd = [
            str(codex), "exec", "--ephemeral", "--sandbox", "danger-full-access", "--color", "never",
            "--output-last-message", str(action_out), "--json", "-C", str(ROOT), "-",
        ]
        try:
            action_rc = run_command(
                action_cmd, prompt=action_prompt, timeout=int(os.environ.get("CODEX_ACTION_TIMEOUT", "1800")),
                stdout_path=action_events, stderr_path=action_errors, env=agent_env(codex),
            )
            if action_rc != 0 and provider_quota(action_rc, action_errors, action_events):
                set_cooldown("codex")
                status = "codex_action_quota"
            else:
                status = "action_completed" if action_rc == 0 else "codex_action_failed"
        finally:
            reasons = update_growth_block()
        record({
            "run_id": run_id,
            "status": status,
            "exit": action_rc,
            "triage": triage,
            "claude_status": claude_status,
            "growth_blocked": reasons,
        })
        print(f"Codex action: {status}; Claude: {claude_status}; growth block: {reasons or 'none'}")
        if action_out.exists():
            print(action_out.read_text(errors="replace")[-8000:])
        return 0 if action_rc == 0 or status == "codex_action_quota" else 1


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
