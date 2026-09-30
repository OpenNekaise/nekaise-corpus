#!/usr/bin/env python3
"""corpus_v1_night.py — the nightly corpus_v1 job: build, repair, improve, review, merge.

Every night (cron, 02:00 Europe/Stockholm, a 3-hour window) this job moves corpus_v1/ forward
(operator directive 2026-09-29: a training view of EVERY corpus/ document, cleaned; broken text
repaired into normal text; better every night, continuously):

1. build    corpus_v1.py incremental — documents the dig loop added or changed reach corpus_v1/.
2. repair   sonnet_clean.py in the background for the rest of the window — Sonnet 5.5 rewrites the
            most OCR-damaged documents into normal text (checked; see its docstring).
3. improve  a headless Claude Code session (Opus) in a git worktree on branch v1-night/<date>
            follows .claude/skills/corpus-v1-night/SKILL.md: inspect, change rules/prompt/checks,
            test, audit, consult Codex, commit, and write NOTES.md for the next night.
4. gate     this script runs its own Codex (GPT) review of the branch against main with the
            audit evidence. Only 'MERGE AS IS' merges: rebased onto main, tests re-run, then
            fast-forwarded into main under the corpus-round lock (never during a dig round).
            Local commits only; publication stays with the maintainer.
5. rebuild  a ruleset change rebuilds corpus_v1/ with the rest of the window (new docs first);
            what does not finish continues the next night.

    python scripts/corpus_v1_night.py                 # the full night (what cron runs)
    python scripts/corpus_v1_night.py --window 1800 --no-improve   # build + repair only

Everything the night did is in workspace/corpus-v1-night/<date>/ (context, session logs, Codex
verdict, audit, summary.json); NOTES.md next to it is the sessions' memory across nights.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import ops

ROOT = Path(__file__).resolve().parents[1]
PY = Path(os.environ.get("PYTHON_BIN", ROOT / ".venv" / "bin" / "python"))
BASE = ROOT / "workspace" / "corpus-v1-night"
NOTES = BASE / "NOTES.md"
WORKTREE = BASE / "wt"
LOCK = ROOT / "workspace" / ".corpus-v1-night.lock"
SCHEMA = ROOT / "scripts" / "corpus_v1_review.schema.json"
CLAUDE_MODEL = os.environ.get("CORPUS_V1_CLAUDE_MODEL", "claude-opus-5-5")
TESTS = ["tests/test_v1_rules.py", "tests/test_sonnet_clean.py", "tests/test_corpus_v1_safety.py"]
PATHS = [str(Path.home() / ".local/bin"), "/usr/local/bin", "/usr/bin", "/bin"]
# What a gate review vouches for besides the patch itself: the code the patch runs with.
CODE_PATHS = ["scripts", "tests", ".claude/skills/corpus-v1-night", "requirements.txt",
              "requirements.lock"]


def log(msg: str) -> None:
    print(f"[{datetime.now().isoformat(timespec='seconds')}] {msg}", flush=True)


def env(**extra: str) -> dict[str, str]:
    e = os.environ.copy()
    e["PATH"] = os.pathsep.join(dict.fromkeys(PATHS + e.get("PATH", "").split(os.pathsep)))
    e["NO_COLOR"] = "1"
    e.update(extra)
    return e


def run(cmd: list[str], *, cwd: Path = ROOT, timeout: float, out: Path | None = None,
        prompt: str | None = None, extra_env: dict | None = None) -> int:
    """Run supervised in its own process group; the whole group dies on timeout."""
    timeout = max(1.0, timeout)
    fh = out.open("w") if out else subprocess.DEVNULL
    try:
        p = subprocess.Popen(cmd, cwd=cwd, stdin=subprocess.PIPE if prompt else subprocess.DEVNULL,
                             stdout=fh, stderr=subprocess.STDOUT, text=True,
                             env=env(**(extra_env or {})), start_new_session=True)
        try:
            p.communicate(input=prompt, timeout=timeout)
            return p.returncode
        except subprocess.TimeoutExpired:
            log(f"timeout after {timeout:.0f}s: {cmd[0]} {' '.join(cmd[1:3])}")
            return 124
        finally:
            if p.poll() is None:
                os.killpg(p.pid, signal.SIGTERM)
                try:
                    p.wait(20)
                except subprocess.TimeoutExpired:
                    os.killpg(p.pid, signal.SIGKILL)
    finally:
        if out:
            fh.close()


def git(*args: str, cwd: Path = ROOT, timeout: float = 300) -> tuple[int, str]:
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout + p.stderr).strip()


def ruleset(checkout: Path) -> str:
    text = (checkout / "scripts" / "v1_rules.py").read_text()
    return text.split('RULESET_VERSION = "', 1)[1].split('"', 1)[0]


def unmerged_branches() -> list[str]:
    """Earlier night branches with commits main does not have, newest first."""
    _, out = git("for-each-ref", "--sort=-refname", "--format=%(refname:short)", "refs/heads/v1-night/")
    return [b for b in out.split() if git("rev-list", "--count", f"main..{b}")[1].strip() not in ("", "0")]


def prepare_worktree(branch: str) -> str:
    """Check out tonight's branch. It starts from the newest unmerged night branch (work that
    missed its gate — e.g. Codex out of quota — carries over and is gated together with
    tonight's), else from main. Returns the base."""
    git("worktree", "prune")
    if WORKTREE.exists():
        git("worktree", "remove", "--force", str(WORKTREE))
    carried = [b for b in unmerged_branches() if b != branch]
    base = carried[0] if carried else "main"
    code, msg = git("worktree", "add", "-B", branch, str(WORKTREE), base)
    if code:
        log(f"worktree failed: {msg}")
        return ""
    if base != "main":
        code, msg = git("rebase", "main", cwd=WORKTREE, timeout=300)
        if code:
            git("rebase", "--abort", cwd=WORKTREE)
            log(f"carried branch {base} does not rebase onto main; starting tonight from it as is")
    return base


def improve(night: Path, branch: str, deadline: float) -> dict:
    """The Claude session; returns what it left on the branch."""
    context = {
        "date": night.name, "branch": branch, "deadline_unix": int(deadline),
        "build_report": subprocess.run([str(PY), "scripts/corpus_v1.py", "--report"], cwd=ROOT,
                                       capture_output=True, text=True).stdout,
        "last_sonnet_runs": [json.loads(p.read_text()) for p in
                             sorted((ROOT / "corpus_v1" / ".log").glob("*.summary.json"))[-3:]],
        "last_nights": [json.loads(p.read_text()) for p in
                        sorted(BASE.glob("*/summary.json"))[-3:]],
    }
    (night / "context.json").write_text(json.dumps(context, indent=1))
    prompt = (
        f"Tonight's corpus_v1 night session ({night.name}). You run unattended: nobody will answer "
        f"questions. Follow .claude/skills/corpus-v1-night/SKILL.md exactly.\n"
        f"NIGHT_DIR={night}\nCORPUS_V1_DATA={ROOT}\nDEADLINE={int(deadline)} "
        f"(stop and commit before it; now is {int(time.time())}).\n"
        f"Branch {branch} is checked out in your working directory. Notes from earlier nights: "
        f"{NOTES}. Context: {night / 'context.json'}.")
    code = run(["claude", "-p", "--model", CLAUDE_MODEL, "--permission-mode", "bypassPermissions",
                "--output-format", "json", "--no-session-persistence"],
               cwd=WORKTREE, timeout=deadline - time.time(), out=night / "claude-session.json",
               prompt=prompt, extra_env={"CORPUS_V1_DATA": str(ROOT), "NIGHT_DIR": str(night),
                                         "DEADLINE": str(int(deadline)),
                                         "PATH": f"{PY.parent}{os.pathsep}{env()['PATH']}"})
    _, ahead = git("rev-list", "--count", f"main..{branch}")
    return {"claude_exit": code, "commits": int(ahead or 0)}


def gate(night: Path, branch: str, deadline: float) -> dict:
    """Tests + an independent Codex review + ff-merge under the round lock."""
    res: dict = {}
    code = run([str(PY), "-m", "pytest", "-q", *TESTS], cwd=WORKTREE, timeout=900,
               out=night / "gate-tests.log")
    if code:
        return {"merged": False, "why": "tests failed on branch"}
    # Review the patch on top of current main: rebase first, then the reviewed base is the
    # branch's ACTUAL base (merge-base), whatever main does afterwards.
    code, msg = git("rebase", "main", cwd=WORKTREE, timeout=300)
    if code:
        git("rebase", "--abort", cwd=WORKTREE)
        return {"merged": False, "why": f"rebase conflict before review: {msg[-300:]}"}
    code_b, review_base = git("merge-base", "main", branch)
    if code_b or not review_base.strip():
        return {"merged": False, "why": "cannot resolve the branch base: no review"}
    review_base = review_base.strip()
    code, diff = git("diff", f"{review_base}..{branch}", timeout=60)
    if code or not diff.strip():
        return {"merged": False, "why": f"cannot read the branch diff (git exit {code}): no review"}
    audit = night / "audit" / "summary.json"
    prompt = (
        "You are the gate reviewer for a nightly change to corpus_v1, the cleaned training view of a "
        "built-environment corpus (every document kept, broken text repaired, never invented). Review "
        "the diff adversarially: can a rule remove real content (prose, tables, CJK, lists, "
        "references)? Can a model repair now introduce or guess numbers or drop content? Are tests "
        "adequate (KEEP cases)? Read the code in this checkout and the audit evidence. Answer "
        "'MERGE AS IS' only if you would ship it unchanged.\n\n"
        f"Audit summary ({audit}):\n{audit.read_text() if audit.exists() else 'MISSING'}\n\n"
        f"Audit details: {night / 'audit' / 'changes.md'}\n\n<diff>\n{diff[:200_000]}\n</diff>\n")
    # Approval binds to exactly the change reviewed: a fresh verdict file per attempt (an old
    # verdict can never be reused), a successful Codex exit, and the diff's fingerprint, which
    # must be unchanged after the rebase under the lock (fail closed on any drift).
    reviewed = hashlib.sha256(diff.encode()).hexdigest()
    verdict_path = night / f"codex-gate-{int(time.time())}.json"
    verdict_path.unlink(missing_ok=True)
    started = time.time()
    code = run(["codex", "exec", "--ephemeral", "--sandbox", "read-only", "--color", "never",
                "--output-schema", str(SCHEMA), "--output-last-message", str(verdict_path),
                "-C", str(WORKTREE), "-"], cwd=WORKTREE,
               timeout=min(1800, deadline - time.time()), out=night / "codex-gate.log", prompt=prompt)
    log_text = ""
    if (night / "codex-gate.log").exists():
        log_text = (night / "codex-gate.log").read_text(errors="replace")[-3000:]
    if code != 0:
        if "usage limit" in log_text or "rate limit" in log_text:
            return {"merged": False, "why": "Codex usage limit: branch carries over to the next night"}
        return {"merged": False, "why": f"Codex review failed (exit {code}): no approval"}
    try:
        if verdict_path.stat().st_mtime < started - 2:  # coarse fs clock; the name is fresh too
            raise ValueError("stale verdict file")
        verdict = json.loads(verdict_path.read_text())
    except (OSError, ValueError):
        return {"merged": False, "why": "no fresh Codex verdict: no approval"}
    res["codex"] = verdict
    res["reviewed_diff_sha256"] = reviewed
    if verdict.get("verdict") != "MERGE AS IS":
        return res | {"merged": False, "why": "Codex: changes required"}
    # Under the round lock no dig round can commit: rebase onto the current main, re-test,
    # fast-forward. The lock is held for about a minute.
    try:
        with ops.named_lock(ops.ROUND_LOCK, timeout=min(3600, max(60, deadline - time.time()))):
            _, dirty = git("status", "--porcelain", "--untracked-files=no")
            if dirty:
                return res | {"merged": False, "why": "main worktree not clean"}
            # The review covered the patch AND the code it runs with. Dig commits (registry,
            # manifest) may land meanwhile; a change to main's code may not: re-review first.
            code, _ = git("diff", "--quiet", review_base, "main", "--", *CODE_PATHS)
            if code != 0:
                return res | {"merged": False, "why": "main's code changed since the review "
                              "(or git failed): the branch carries over for a fresh review"}
            code, msg = git("rebase", "main", cwd=WORKTREE, timeout=300)
            if code:
                git("rebase", "--abort", cwd=WORKTREE)
                return res | {"merged": False, "why": f"rebase conflict: {msg[-300:]}"}

            def fingerprint() -> tuple[str, str] | None:
                c1, head = git("rev-parse", "--verify", f"{branch}^{{commit}}")
                c2, d = git("diff", f"main...{head.strip()}", timeout=60) if not c1 else (1, "")
                # main...head is the patch on its new base; same text as reviewed unless it drifted
                return None if c1 or c2 else (head.strip(), hashlib.sha256(d.encode()).hexdigest())

            before = fingerprint()
            if not before or before[1] != reviewed:
                return res | {"merged": False,
                              "why": "the change differs from what Codex reviewed (rebase drift)"}
            if run([str(PY), "-m", "pytest", "-q", *TESTS], cwd=WORKTREE, timeout=900,
                   out=night / "gate-tests-rebased.log"):
                return res | {"merged": False, "why": "tests failed after rebase"}
            after = fingerprint()
            if after != before:  # anything moved during the tests: fail closed
                return res | {"merged": False, "why": "the branch changed during the final tests"}
            # merge the immutable commit that was reviewed and tested, never the moving name
            code, msg = git("merge", "--ff-only", before[0])
    except RuntimeError as e:
        return res | {"merged": False, "why": f"round lock busy: {e}"}
    return res | {"merged": code == 0, "why": msg[-300:]}


def append_notes(summary: dict) -> None:
    BASE.mkdir(parents=True, exist_ok=True)
    if not NOTES.exists():
        NOTES.write_text("# corpus_v1 night notes\n\nThe sessions' memory across nights "
                         "(see .claude/skills/corpus-v1-night/SKILL.md). Ranked backlog first, "
                         "then one entry per night.\n\n## Backlog\n\n## Nights\n")
    gate_res = summary.get("gate") or {}
    line = (f"\n- {summary['date']} [job]: build {summary.get('build', {}).get('built')} docs; "
            f"improve commits={summary.get('improve', {}).get('commits')}; "
            f"merged={gate_res.get('merged')} ({gate_res.get('why', '')[:120]}); "
            f"ruleset {summary.get('ruleset_before')} -> {summary.get('ruleset_after')}\n")
    with NOTES.open("a") as fh:
        fh.write(line)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--window", type=int, default=3 * 3600 - 300, help="seconds for the whole night")
    ap.add_argument("--sonnet-workers", type=int, default=8)
    ap.add_argument("--no-improve", action="store_true")
    ap.add_argument("--no-repair", action="store_true")
    ap.add_argument("--gate-only", metavar="BRANCH",
                    help="run just the gate (tests, Codex review, merge, rebuild) on BRANCH")
    args = ap.parse_args()

    LOCK.parent.mkdir(exist_ok=True)
    lock = LOCK.open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        log("another night job is running")
        return 0
    start = time.time()
    end = start + args.window
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    night = BASE / date
    night.mkdir(parents=True, exist_ok=True)
    summary: dict = {"date": date, "ruleset_before": ruleset(ROOT)}
    log(f"night {date}: window {args.window}s")

    if args.gate_only:  # e.g. a branch that missed its gate; its evidence is in its night dir
        night = BASE / args.gate_only.split("/", 1)[1]
        git("worktree", "prune")
        if WORKTREE.exists():
            git("worktree", "remove", "--force", str(WORKTREE))
        git("worktree", "add", str(WORKTREE), args.gate_only)
        summary["gate"] = gate(night, args.gate_only, end)
        log(f"gate: {summary['gate']}")
        if summary["gate"].get("merged") and ruleset(ROOT) != summary["ruleset_before"]:
            run([str(PY), "scripts/corpus_v1.py"], timeout=end - time.time(), out=night / "rebuild.log")
        (night / "gate-only.json").write_text(json.dumps(summary, indent=1, default=str))
        return 0

    # 1. build: new and changed documents (the day's dig growth) first.
    run([str(PY), "scripts/corpus_v1.py", "--max-seconds", "1800"], timeout=2100,
        out=night / "build.log")
    summary["build"] = {"log": str(night / "build.log")}
    try:
        summary["build"] = json.loads((night / "build.log").read_text().strip().splitlines()[-1])
    except (OSError, ValueError, IndexError):
        pass

    # 2. repair: Sonnet in the background until 20 min before the end.
    sonnet = None
    if not args.no_repair:
        budget = int(end - time.time() - 1200)
        if budget > 600:
            sonnet = subprocess.Popen(
                [str(PY), "scripts/sonnet_clean.py", "--max-seconds", str(budget),
                 "--workers", str(args.sonnet_workers)], cwd=ROOT, env=env(),
                stdout=(night / "sonnet.log").open("w"), stderr=subprocess.STDOUT,
                start_new_session=True)

    # 3-4. improve, then gate.
    if not args.no_improve and end - time.time() > 3600:
        branch = f"v1-night/{date}"
        base = prepare_worktree(branch)
        if base:
            summary["base"] = base
            summary["improve"] = improve(night, branch, end - 2400)  # leave 40 min for the gate
            if summary["improve"]["commits"]:
                summary["gate"] = gate(night, branch, end - 600)
                log(f"gate: {summary['gate'].get('merged')} {summary['gate'].get('why', '')}")

    # 5. rebuild with a new ruleset for what is left of the window.
    summary["ruleset_after"] = ruleset(ROOT)
    left = end - time.time() - 120
    if summary["ruleset_after"] != summary["ruleset_before"] and left > 300:
        run([str(PY), "scripts/corpus_v1.py", "--max-seconds", str(int(left))], timeout=left + 120,
            out=night / "rebuild.log")

    if sonnet is not None:
        try:
            sonnet.wait(max(1, end - time.time()))
        except subprocess.TimeoutExpired:
            os.killpg(sonnet.pid, signal.SIGTERM)
    summary["elapsed_min"] = round((time.time() - start) / 60, 1)
    (night / "summary.json").write_text(json.dumps(summary, indent=1, default=str))
    append_notes(summary)
    log(f"night done: {json.dumps(summary, default=str)[:600]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
