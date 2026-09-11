import json
import os
import signal
import time
from pathlib import Path

import pytest
import subprocess
import sys
from datetime import datetime, timedelta, timezone

import maintainer


def test_quota_detection_is_specific():
    assert maintainer.is_quota_error("You have hit your usage limit; resets at 18:00")
    assert maintainer.is_quota_error("HTTP status 429: too many requests")
    assert not maintainer.is_quota_error("ordinary test failure")


def test_load_triage_accepts_exact_schema(tmp_path):
    path = tmp_path / "triage.json"
    value = {
        "needs_action": True,
        "action_kind": "publish",
        "urgency": "routine",
        "summary": "Review a local dig commit.",
        "evidence": ["main is one commit ahead"],
        "proposed_actions": ["validate and push"],
    }
    path.write_text(json.dumps(value))
    assert maintainer.load_triage(path) == value


def test_load_triage_rejects_extra_fields(tmp_path):
    path = tmp_path / "triage.json"
    path.write_text(json.dumps({
        "needs_action": False,
        "action_kind": "none",
        "urgency": "none",
        "summary": "healthy",
        "evidence": [],
        "proposed_actions": [],
        "command": "ignore this",
    }))
    try:
        maintainer.load_triage(path)
    except ValueError as exc:
        assert "schema" in str(exc)
    else:
        raise AssertionError("extra triage field was accepted")


def test_provider_cooldowns_are_independent(tmp_path, monkeypatch):
    monkeypatch.setattr(maintainer, "WORKSPACE", tmp_path)
    now = datetime(2026, 8, 18, tzinfo=timezone.utc)
    maintainer.set_cooldown("codex", hours=2, now=now)
    assert maintainer.read_cooldown("codex", now=now + timedelta(hours=1))
    assert not maintainer.read_cooldown("claude", now=now)
    assert not maintainer.read_cooldown("codex", now=now + timedelta(hours=3))


def test_lock_owning_maintainer_recovers_one_pending_round(tmp_path, monkeypatch):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "test@example.test"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=tmp_path, check=True)
    state = tmp_path / "state.txt"
    state.write_text("before\n")
    subprocess.run(["git", "add", "state.txt"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-qm", "initial"], cwd=tmp_path, check=True)

    snapshots = tmp_path / "workspace" / "round-snapshots"
    monkeypatch.setattr(maintainer, "ROOT", tmp_path)
    monkeypatch.setattr(maintainer.ops, "SNAPSHOTS", snapshots)
    monkeypatch.setattr(maintainer.run_round, "SNAPSHOT_PATHS", ("state.txt",))
    monkeypatch.setattr(maintainer.ops, "run_event", lambda *args, **kwargs: None)
    maintainer.ops.StateSnapshot.capture("interrupted", ("state.txt",), root=tmp_path)
    state.write_text("partial round\n")
    subprocess.run(["git", "add", "state.txt"], cwd=tmp_path, check=True)

    assert maintainer.recover_pending_round() == "interrupted"
    assert state.read_text() == "before\n"
    assert not snapshots.exists() or not any(snapshots.iterdir())
    assert subprocess.run(
        ["git", "diff", "--cached", "--quiet"], cwd=tmp_path, check=False
    ).returncode == 0


@pytest.mark.parametrize("code, expected", [(0, None), (1, "1 unprovenanced file"), (124, "exit 124")])
def test_post_recovery_check_is_supervised(tmp_path, monkeypatch, code, expected):
    def fake_run(command, **kwargs):
        assert command == [sys.executable, str(tmp_path / "clean_corpus.py"), "--check"]
        assert kwargs["timeout"] == 900
        assert kwargs["prompt"] is None
        kwargs["stdout_path"].write_text("DRIFT: 1 unprovenanced file")
        kwargs["stderr_path"].write_text("")
        return code

    monkeypatch.setattr(maintainer, "SCRIPTS", tmp_path)
    monkeypatch.setattr(maintainer, "run_command", fake_run)
    if expected:
        with pytest.raises(RuntimeError, match=expected):
            maintainer.verify_recovered_corpus()
    else:
        maintainer.verify_recovered_corpus()


def write_history(path, *rows):
    path.write_text("".join(
        row + "\n" if isinstance(row, str) else json.dumps(row) + "\n"
        for row in rows
    ))


def test_backend_health_handles_empty_history(tmp_path):
    summary = maintainer.summarize_backend_health(
        tmp_path / "missing.jsonl",
        {"finder": {"enabled": True}},
    )

    assert summary == {
        "window_limit": 40,
        "completed_rounds": 0,
        "total_accepted": 0,
        "streaks_scope": "all_completed_rounds",
        "backends": {
            "finder": {
                "observed_rounds": 0,
                "accepted": 0,
                "accepted_share": None,
                "consecutive_degraded_rounds": 0,
                "consecutive_zero_accepted_rounds": 0,
                "last_nonzero_at": None,
                "rotates": True,
                "pointer_advanced_in_window": False,
                "last_rotation": None,
                "last_hold_reason": None,
                "last_hold_detail": None,
            }
        },
    }


def test_backend_health_streaks_reset_on_success(tmp_path):
    history = tmp_path / "history.jsonl"
    write_history(
        history,
        {"run_id": "one", "event": "discovery_degraded", "failures": {"finder": 1}},
        {"run_id": "one", "event": "discovery_merged", "at": "2026-09-01T00:01:00Z", "accepted": {"finder": 0}},
        {"run_id": "one", "event": "rotation_held", "at": "2026-09-01T00:01:00Z", "backend": "finder", "reason": "finder_requested"},
        {"run_id": "one", "event": "run_completed", "at": "2026-09-01T00:02:00Z"},
        {"run_id": "two", "event": "discovery_merged", "at": "2026-09-01T01:01:00Z", "accepted": {"finder": 7}},
        {"run_id": "two", "event": "rotation_advanced", "at": "2026-09-01T01:01:00Z", "backend": "finder"},
        {"run_id": "two", "event": "run_completed", "at": "2026-09-01T01:02:00Z"},
    )

    finder = maintainer.summarize_backend_health(
        history, {"finder": {"enabled": True}}
    )["backends"]["finder"]

    assert finder["consecutive_degraded_rounds"] == 0
    assert finder["consecutive_zero_accepted_rounds"] == 0
    assert finder["last_nonzero_at"] == "2026-09-01T01:01:00Z"
    assert finder["pointer_advanced_in_window"] is True
    assert finder["last_rotation"]["status"] == "advanced"
    assert finder["last_hold_reason"] == "finder_requested"
    assert finder["last_hold_detail"] is None


def test_backend_health_retains_hold_detail_after_advance_and_resets_on_legacy_hold(tmp_path):
    history = tmp_path / "history.jsonl"
    rows = []
    for run_id, event, fields, expected in [
        ("cap", "rotation_held", {"detail": "candidate cap reached"}, "candidate cap reached"),
        ("month", "rotation_held", {"detail": "open UTC month"}, "open UTC month"),
        ("advance", "rotation_advanced", {}, "open UTC month"),
        ("legacy", "rotation_held", {}, None),
        ("malformed", "rotation_held", {"detail": ["bad"]}, None),
    ]:
        rows.extend([
            {"run_id": run_id, "event": "discovery_merged", "accepted": {"finder": 0}},
            {"run_id": run_id, "event": event, "backend": "finder",
             "reason": "finder_requested", **fields},
            {"run_id": run_id, "event": "run_completed"},
        ])
        write_history(history, *rows)
        finder = maintainer.summarize_backend_health(
            history, {"finder": {"enabled": True}}
        )["backends"]["finder"]
        assert finder["last_hold_reason"] == "finder_requested"
        assert finder["last_hold_detail"] == expected
        assert finder["last_rotation"].get("detail") == (
            expected if event == "rotation_held" else None
        )


def test_backend_health_skips_malformed_lines(tmp_path):
    history = tmp_path / "history.jsonl"
    write_history(
        history,
        "not json",
        "[]",
        {"run_id": "one", "event": "discovery_merged", "accepted": {"finder": 0}},
        {"run_id": "one", "event": "run_completed", "at": "2026-09-01T00:02:00Z"},
    )

    summary = maintainer.summarize_backend_health(
        history, {"finder": {"enabled": True}}
    )

    assert summary["completed_rounds"] == 1
    assert summary["backends"]["finder"]["consecutive_zero_accepted_rounds"] == 1


def test_backend_health_ignores_unselected_and_reports_unobserved_backend(tmp_path):
    history = tmp_path / "history.jsonl"
    write_history(
        history,
        {"run_id": "one", "event": "discovery_merged", "accepted": {"disabled": 9}},
        {"run_id": "one", "event": "run_completed", "at": "2026-09-01T00:02:00Z"},
    )

    summary = maintainer.summarize_backend_health(
        history,
        {"disabled": {"enabled": False}, "new": {"enabled": True, "rotation": False}},
    )

    assert set(summary["backends"]) == {"new"}
    assert summary["total_accepted"] == 9
    assert summary["backends"]["new"]["observed_rounds"] == 0
    assert summary["backends"]["new"]["rotates"] is False


def test_backend_health_retains_last_nonzero_before_concentration_window(tmp_path):
    history = tmp_path / "history.jsonl"
    write_history(
        history,
        {"run_id": "one", "event": "discovery_merged", "at": "2026-09-01T00:01:00Z", "accepted": {"finder": 4}},
        {"run_id": "one", "event": "run_completed", "at": "2026-09-01T00:02:00Z"},
        {"run_id": "two", "event": "discovery_merged", "accepted": {"finder": 0}},
        {"run_id": "two", "event": "run_completed", "at": "2026-09-01T01:02:00Z"},
        {"run_id": "three", "event": "discovery_merged", "accepted": {"finder": 0}},
        {"run_id": "three", "event": "run_completed", "at": "2026-09-01T02:02:00Z"},
    )

    summary = maintainer.summarize_backend_health(
        history, {"finder": {"enabled": True}}, window=1
    )
    finder = summary["backends"]["finder"]

    assert summary["completed_rounds"] == 1
    assert summary["total_accepted"] == 0
    assert finder["consecutive_zero_accepted_rounds"] == 2
    assert finder["last_nonzero_at"] == "2026-09-01T00:01:00Z"


def test_repo_snapshot_includes_backend_health(tmp_path, monkeypatch):
    registry_dir = tmp_path / "registry"
    logs = tmp_path / "logs"
    workspace = tmp_path / "workspace"
    registry_dir.mkdir()
    logs.mkdir()
    workspace.mkdir()
    (registry_dir / "backends.json").write_text(json.dumps({
        "finder": {"enabled": True},
    }))
    write_history(
        logs / "run_history.jsonl",
        {"run_id": "one", "event": "discovery_merged", "accepted": {"finder": 3}},
        {"run_id": "one", "event": "run_completed", "at": "2026-09-01T00:02:00Z"},
    )
    monkeypatch.setattr(maintainer, "ROOT", tmp_path)
    monkeypatch.setattr(maintainer, "LOGS", logs)
    monkeypatch.setattr(maintainer, "WORKSPACE", workspace)
    monkeypatch.setattr(
        maintainer,
        "git",
        lambda *args: (0, "0 0" if args[0] == "rev-list" else "main"),
    )

    snapshot = maintainer.repo_snapshot("exit=0")

    assert snapshot["backend_health"]["total_accepted"] == 3
    assert snapshot["backend_health"]["backends"]["finder"]["accepted_share"] == 1.0


def configure_maintenance(tmp_path, monkeypatch):
    workspace = tmp_path / 'workspace'
    logs = tmp_path / 'logs'
    workspace.mkdir()
    logs.mkdir()
    monkeypatch.setattr(maintainer, 'ROOT', tmp_path)
    monkeypatch.setattr(maintainer, 'WORKSPACE', workspace)
    monkeypatch.setattr(maintainer.ops, 'WORKSPACE', workspace)
    monkeypatch.setattr(maintainer, 'LOGS', logs)
    monkeypatch.setattr(maintainer, 'HISTORY', logs / 'history.jsonl')
    monkeypatch.setattr(maintainer, 'BLOCKED', workspace / '.maintenance-blocked')
    monkeypatch.setenv('MAINTAINER_LOCK_WAIT_SECONDS', '0.1')
    for name in ('CODEX_TRIAGE_TIMEOUT', 'CODEX_ACTION_TIMEOUT', 'CLAUDE_REVIEW_TIMEOUT',
                 'CLAUDE_REVIEW_MODEL', 'CLAUDE_REVIEW_EFFORT'):
        monkeypatch.delenv(name, raising=False)
    return workspace


def assert_growth_locked():
    for name in ('continuous-dig', 'corpus-round'):
        with pytest.raises(RuntimeError, match='held by pid'):
            with maintainer.ops.named_lock(name):
                pass


def assert_growth_unlocked():
    for name in ('continuous-dig', 'corpus-round'):
        with maintainer.ops.named_lock(name):
            pass


@pytest.mark.parametrize('name', ['continuous-dig', 'corpus-round'])
def test_window_timeout_cleans_request_and_preserves_owner(tmp_path, monkeypatch, name):
    workspace = configure_maintenance(tmp_path, monkeypatch)
    with maintainer.ops.named_lock(name) as lock:
        owner = lock.read_text()
        with pytest.raises(maintainer.MaintenanceBusy):
            with maintainer.maintenance_window('test'):
                pytest.fail('entered a busy window')
        assert lock.read_text() == owner
    assert not (workspace / '.maintenance-requested').exists()
    assert_growth_unlocked()


@pytest.mark.parametrize('kind', ['none', 'publish', 'repair', 'improve'])
@pytest.mark.parametrize('cancel_action', [False, True])
def test_maintenance_phases_refresh_state_and_hold_only_mutation_locks(tmp_path, monkeypatch, kind, cancel_action):
    workspace = configure_maintenance(tmp_path, monkeypatch)
    state = {'head': 'before', 'blocked_checks': 0, 'calls': []}
    monkeypatch.setattr(maintainer, 'resolve_agent', lambda name, override=None: Path('/bin') / name)

    def git(*args, **kwargs):
        assert args[0] == 'fetch'
        assert_growth_unlocked()
        return 0, ''

    def recover():
        assert_growth_locked()

    def snapshot(*args, **kwargs):
        assert_growth_locked()
        return {'head': state['head'], 'pending_round_snapshots': []}

    def check_block():
        assert_growth_locked()
        state['blocked_checks'] += 1
        return []

    def run(command, **kwargs):
        kwargs['stdout_path'].write_text('')
        kwargs['stderr_path'].write_text('')
        prompt = kwargs['prompt']
        if '--output-schema' in command:
            assert_growth_unlocked()
            assert kwargs['timeout'] == 600
            assert 'before' in prompt
            out = Path(command[command.index('--output-last-message') + 1])
            out.write_text(json.dumps({
                'needs_action': kind != 'none', 'action_kind': kind,
                'urgency': 'none' if kind == 'none' else 'routine',
                'summary': 'test', 'evidence': ['measured'], 'proposed_actions': ['check'],
            }))
            # Simulate a completed growth round during unlocked deliberation.
            state['head'] = 'after'
            state['calls'].append('triage')
        elif '--print' in command:
            assert_growth_unlocked()
            assert kwargs['timeout'] == 300
            assert '--permission-mode' not in command
            assert command[command.index('--tools') + 1] == ''
            assert command[command.index('--model') + 1] == 'claude-opus-5'
            assert command[command.index('--effort') + 1] == 'xhigh'
            assert 'before' in prompt and '{{' not in prompt
            kwargs['stdout_path'].write_text(json.dumps({'type': 'result', 'is_error': False, 'result': 'Check stale evidence.'}))
            state['calls'].append('review')
        else:
            assert_growth_locked()
            assert kwargs['timeout'] == 1800
            assert '"head": "after"' in prompt
            assert '"triage_head": "before"' in prompt
            assert '"changed_since_triage": true' in prompt
            assert '{{' not in prompt
            state['calls'].append('action')
            if cancel_action:
                raise KeyboardInterrupt()
        return 0

    monkeypatch.setattr(maintainer, 'git', git)
    monkeypatch.setattr(maintainer, 'recover_pending_round', recover)
    monkeypatch.setattr(maintainer, 'repo_snapshot', snapshot)
    monkeypatch.setattr(maintainer, 'update_growth_block', check_block)
    monkeypatch.setattr(maintainer, 'run_command', run)
    expected = 130 if cancel_action and kind != 'none' else 0
    assert maintainer.main() == expected
    assert state['calls'] == (['triage'] if kind == 'none' else
                              ['triage', 'action'] if kind == 'publish' else
                              ['triage', 'review', 'action'])
    assert state['blocked_checks'] == (1 if kind == 'none' else 2)
    assert not (workspace / '.maintenance-requested').exists()
    assert_growth_unlocked()


def test_cooldown_does_not_pause_growth_or_inspect_live_state(tmp_path, monkeypatch):
    workspace = configure_maintenance(tmp_path, monkeypatch)
    monkeypatch.setattr(maintainer, 'resolve_agent', lambda *args: Path('/bin/codex'))
    maintainer.set_cooldown('codex')
    monkeypatch.setattr(maintainer, 'git', lambda *args, **kwargs: pytest.fail('cooldown touched git'))
    monkeypatch.setattr(maintainer, 'update_growth_block', lambda: pytest.fail('inspected live state'))
    with maintainer.ops.named_lock('corpus-round'):
        assert maintainer.main() == 0
    assert not (workspace / '.maintenance-requested').exists()


@pytest.mark.parametrize('code, stderr, event, expected', [
    (124, 'HTTP status 429', {'type': 'error', 'message': 'usage limit'}, False),
    (1, '', {'type': 'item.completed', 'item': {'output': 'HTTP status 429'}}, False),
    (1, '', {'type': 'result', 'is_error': False, 'result': 'HTTP status 429'}, False),
    (1, '', {'type': 'turn.failed', 'error': {'message': 'usage limit'}}, True),
    (1, '', {'type': 'result', 'is_error': True, 'result': 'usage limit'}, True),
    (1, 'HTTP status 429', {}, True),
    (0, 'HTTP status 429', {}, False),
])
def test_quota_uses_provider_errors_not_tool_output(tmp_path, code, stderr, event, expected):
    errors = tmp_path / 'stderr'
    events = tmp_path / 'events'
    errors.write_text(stderr)
    events.write_text(json.dumps(event) + '\ninvalid json\n')
    assert maintainer.provider_quota(code, errors, events) is expected


def process_running(pid):
    try:
        return Path(f'/proc/{pid}/stat').read_text().split(') ', 1)[1].split()[0] != 'Z'
    except FileNotFoundError:
        return False


@pytest.mark.parametrize('exit_parent', [False, True])
@pytest.mark.parametrize('detached', [False, True])
def test_timeout_and_normal_exit_stop_descendants(tmp_path, monkeypatch, exit_parent, detached):
    monkeypatch.setattr(maintainer, 'ROOT', tmp_path)
    child = tmp_path / 'child.pid'
    script = (
        'import subprocess,sys,time\n'
        'from pathlib import Path\n'
        f'p=subprocess.Popen([sys.executable,"-c","import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)"], start_new_session={detached!r})\n'
        f'Path({str(child)!r}).write_text(str(p.pid))\n'
        'time.sleep(0.3)\n' + ('' if exit_parent else 'time.sleep(60)\n')
    )
    code = maintainer.run_command(
        [sys.executable, '-c', script], prompt=None, timeout=1,
        stdout_path=tmp_path / 'stdout', stderr_path=tmp_path / 'stderr',
    )
    pid = int(child.read_text())
    try:
        for _ in range(30):
            if not process_running(pid):
                break
            time.sleep(0.05)
        assert not process_running(pid)
        assert code == (0 if exit_parent else 124)
    finally:
        if process_running(pid):
            os.kill(pid, signal.SIGKILL)


def test_sigterm_cleans_processes_before_unlock(tmp_path):
    scripts = Path(maintainer.__file__).parent
    child = tmp_path / 'child.pid'
    code = f'''
import sys,os
from pathlib import Path
sys.path.insert(0,{str(scripts)!r})
import maintainer
maintainer.ROOT=Path({str(tmp_path)!r})
maintainer.WORKSPACE=maintainer.ROOT/'workspace'
maintainer.ops.WORKSPACE=maintainer.WORKSPACE
maintainer.LOGS=maintainer.ROOT/'logs'
maintainer.HISTORY=maintainer.LOGS/'history.jsonl'
def run():
    with maintainer.maintenance_window('test'):
        return maintainer.run_command(
            [sys.executable,'-c', "import os,time; from pathlib import Path; Path({str(child)!r}).write_text(str(os.getpid())); time.sleep(60)"],
            prompt=None,timeout=60,stdout_path=maintainer.LOGS/'out',stderr_path=maintainer.LOGS/'err')
maintainer.run_maintenance=run
sys.exit(maintainer.main())
'''
    process = subprocess.Popen([sys.executable, '-c', code], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.monotonic() + 10
        while not child.exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(0.05)
        assert child.exists()
        pid = int(child.read_text())
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=10)
        assert process.returncode == 130, (stdout, stderr)
        assert not process_running(pid)
        assert not (tmp_path / 'workspace/.maintenance-requested').exists()
        import fcntl
        for name in ('continuous-dig', 'corpus-round', 'maintainer'):
            with (tmp_path / f'workspace/.{name}.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        if child.exists() and process_running(int(child.read_text())):
            os.kill(int(child.read_text()), signal.SIGKILL)


def test_supervision_preserves_preexisting_children(tmp_path, monkeypatch):
    monkeypatch.setattr(maintainer, 'ROOT', tmp_path)
    unrelated = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])
    try:
        assert maintainer.run_command(
            [sys.executable, '-c', "print('done')"], prompt=None, timeout=5,
            stdout_path=tmp_path / 'out', stderr_path=tmp_path / 'err',
        ) == 0
        assert unrelated.poll() is None
    finally:
        unrelated.terminate()
        unrelated.wait(timeout=5)


def test_second_maintainer_leaves_owner_request_alone(tmp_path, monkeypatch):
    workspace = configure_maintenance(tmp_path, monkeypatch)
    request = workspace / '.maintenance-requested'
    request.write_text('existing owner request')
    monkeypatch.setattr(maintainer, 'run_maintenance', lambda: pytest.fail('started concurrent maintenance'))
    with maintainer.ops.named_lock('maintainer'):
        assert maintainer.main() == 0
    assert request.read_text() == 'existing owner request'
