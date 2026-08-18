#!/usr/bin/env python3
"""Codex-first, Claude-reviewed autonomous maintenance for nekaise-corpus.

The caller must hold workspace/.continuous-dig.lock and workspace/.corpus-round.lock, so this
process always sees a between-round repository state regardless of which round entrypoint was used.
Codex triages read-only.  When it finds actionable work, Claude Code reviews that assessment
read-only and a second Codex run makes the final decision and may edit/commit/push.  Provider
exhaustion is a deferred run, not a reason to hammer the same account or silently promote Claude to
primary maintainer.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


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


def run_command(
    command: list[str],
    *,
    prompt: str | None,
    timeout: int,
    stdout_path: Path,
    stderr_path: Path,
    env: dict[str, str] | None = None,
) -> int:
    with stdout_path.open("w") as stdout, stderr_path.open("w") as stderr:
        try:
            result = subprocess.run(
                command,
                cwd=ROOT,
                input=prompt,
                text=True,
                stdout=stdout,
                stderr=stderr,
                env=env,
                timeout=timeout,
                check=False,
            )
            return result.returncode
        except subprocess.TimeoutExpired:
            stderr.write(f"\nmaintainer timeout after {timeout}s\n")
            return 124


def git(*args: str, timeout: int = 300) -> tuple[int, str]:
    try:
        result = subprocess.run(
            ["git", *args], cwd=ROOT, text=True, capture_output=True, timeout=timeout, check=False
        )
        return result.returncode, (result.stdout + result.stderr).strip()
    except subprocess.TimeoutExpired as exc:
        partial = (exc.stdout or "") + (exc.stderr or "")
        return 124, f"{partial.strip()}\ngit command timed out after {timeout}s".strip()


def repo_snapshot(fetch_result: str) -> dict[str, Any]:
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
    recent_events = history.read_text(errors="replace").splitlines()[-30:] if history.exists() else []
    recent_logs = sorted(LOGS.glob("dig-*.log"), key=lambda path: path.stat().st_mtime)[-3:]
    return {
        "checked_at": utc_now().isoformat(),
        "branch": branch,
        "git_status": status,
        "behind_origin_main": behind,
        "ahead_of_origin_main": ahead,
        "pending_round_snapshots": snapshots,
        "fetch_result": fetch_result[-2000:],
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
    required = {"needs_action", "urgency", "summary", "evidence", "proposed_actions"}
    if not isinstance(value, dict) or set(value) != required:
        raise ValueError("Codex triage did not match the required schema")
    if not isinstance(value["needs_action"], bool):
        raise ValueError("Codex triage needs_action is not boolean")
    if value["urgency"] not in {"none", "routine", "urgent"}:
        raise ValueError("Codex triage urgency is invalid")
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


def main() -> int:
    WORKSPACE.mkdir(exist_ok=True)
    LOGS.mkdir(exist_ok=True)
    run_id = stamp()
    run_dir = LOGS / f"maintainer-{run_id}"
    run_dir.mkdir()
    print(f"[{utc_now().isoformat()}] maintainer start {run_id}", flush=True)

    fetch_rc, fetch_output = git("fetch", "--prune", "origin", timeout=600)
    fetch_result = f"exit={fetch_rc}\n{fetch_output}" if fetch_output else f"exit={fetch_rc}"
    snapshot = repo_snapshot(fetch_result)
    (run_dir / "repo-snapshot.json").write_text(json.dumps(snapshot, indent=2) + "\n")

    codex = resolve_agent("codex", os.environ.get("CODEX_BIN"))
    if codex is None:
        reasons = update_growth_block()
        record({"run_id": run_id, "status": "codex_missing", "growth_blocked": reasons})
        print("Codex CLI is unavailable; Claude was not promoted over the primary maintainer.")
        return 1
    if read_cooldown("codex"):
        reasons = update_growth_block()
        record({"run_id": run_id, "status": "codex_cooldown", "growth_blocked": reasons})
        print("Codex cooldown is still active; deferred without calling Claude.")
        return 0

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
        triage_cmd, prompt=triage_prompt, timeout=int(os.environ.get("CODEX_TRIAGE_TIMEOUT", "2700")),
        stdout_path=triage_events, stderr_path=triage_errors, env=agent_env(codex),
    )
    triage_diagnostics = triage_errors.read_text(errors="replace") + triage_events.read_text(errors="replace")[-10000:]
    if triage_rc != 0 or not triage_out.exists():
        status = "codex_quota" if is_quota_error(triage_diagnostics) else "codex_triage_failed"
        if status == "codex_quota":
            set_cooldown("codex")
        reasons = update_growth_block()
        record({"run_id": run_id, "status": status, "exit": triage_rc, "growth_blocked": reasons})
        print(f"Codex triage deferred: {status} (exit {triage_rc}); Claude was not called.")
        return 0 if status == "codex_quota" else 1

    try:
        triage = load_triage(triage_out)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        reasons = update_growth_block()
        record({"run_id": run_id, "status": "invalid_triage", "error": str(exc), "growth_blocked": reasons})
        print(f"Invalid Codex triage: {exc}")
        return 1

    if not triage["needs_action"]:
        reasons = update_growth_block()
        record({"run_id": run_id, "status": "healthy_no_action", "triage": triage, "growth_blocked": reasons})
        print(f"Codex found no action: {triage['summary']}")
        return 0

    claude = resolve_agent("claude", os.environ.get("CLAUDE_BIN"))
    claude_review = "Claude Code was unavailable; Codex must proceed from its own evidence."
    claude_status = "missing"
    if claude is not None and not read_cooldown("claude"):
        review_prompt = render("claude_review.md", CODEX_TRIAGE=json.dumps(triage, indent=2))
        review_out = run_dir / "claude-review.txt"
        review_errors = run_dir / "claude-review.stderr.log"
        claude_cmd = [
            str(claude), "--print", "--permission-mode", "plan", "--no-session-persistence",
            "--output-format", "text",
        ]
        review_rc = run_command(
            claude_cmd, prompt=review_prompt, timeout=int(os.environ.get("CLAUDE_REVIEW_TIMEOUT", "2700")),
            stdout_path=review_out, stderr_path=review_errors, env=agent_env(claude),
        )
        review_diagnostics = review_errors.read_text(errors="replace") + review_out.read_text(errors="replace")
        if review_rc == 0 and review_out.stat().st_size:
            claude_review = review_out.read_text(errors="replace")[-30000:]
            claude_status = "reviewed"
        elif is_quota_error(review_diagnostics):
            set_cooldown("claude")
            claude_status = "quota"
            claude_review = "Claude Code hit its usage limit. Codex remains primary and may proceed cautiously."
        else:
            claude_status = f"failed_exit_{review_rc}"
            claude_review = "Claude Code review failed for a non-quota reason. Codex remains primary; inspect logs and proceed cautiously."
    elif claude is not None:
        claude_status = "cooldown"
        claude_review = "Claude Code is in a usage cooldown. Codex remains primary and may proceed cautiously."

    action_prompt = render(
        "action.md",
        CODEX_TRIAGE=json.dumps(triage, indent=2),
        CLAUDE_REVIEW=claude_review,
    )
    action_out = run_dir / "codex-action.txt"
    action_events = run_dir / "codex-action.events.jsonl"
    action_errors = run_dir / "codex-action.stderr.log"
    action_cmd = [
        str(codex), "exec", "--ephemeral", "--sandbox", "danger-full-access", "--color", "never",
        "--output-last-message", str(action_out), "--json", "-C", str(ROOT), "-",
    ]
    action_rc = run_command(
        action_cmd, prompt=action_prompt, timeout=int(os.environ.get("CODEX_ACTION_TIMEOUT", "7200")),
        stdout_path=action_events, stderr_path=action_errors, env=agent_env(codex),
    )
    action_diagnostics = action_errors.read_text(errors="replace") + action_events.read_text(errors="replace")[-10000:]
    if action_rc != 0 and is_quota_error(action_diagnostics):
        set_cooldown("codex")
        status = "codex_action_quota"
    else:
        status = "action_completed" if action_rc == 0 else "codex_action_failed"
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


if __name__ == "__main__":
    sys.exit(main())
