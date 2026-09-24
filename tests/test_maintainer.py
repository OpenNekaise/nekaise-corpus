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


def test_incomplete_capture_does_not_block_or_trigger_recovery(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    snapshots = workspace / "round-snapshots"
    fragment = snapshots / "killed" / "state"
    fragment.mkdir(parents=True)
    (fragment / "partial.txt").write_text("partial")
    monkeypatch.setattr(maintainer, "ROOT", tmp_path)
    monkeypatch.setattr(maintainer, "WORKSPACE", workspace)
    monkeypatch.setattr(maintainer, "LOGS", tmp_path / "logs")
    monkeypatch.setattr(maintainer, "BLOCKED", workspace / ".maintenance-blocked")
    monkeypatch.setattr(maintainer.ops, "SNAPSHOTS", snapshots)

    def git(*args):
        assert args[0] != "restore", "an incomplete capture must never be recovered"
        return 0, {"branch": "main", "rev-list": "0 0"}.get(args[0], "")

    monkeypatch.setattr(maintainer, "git", git)
    maintainer.BLOCKED.write_text("1 interrupted round snapshot(s) pending\n")

    assert maintainer.recover_pending_round() is None
    assert maintainer.block_reasons() == []
    assert maintainer.update_growth_block() == []
    assert not maintainer.BLOCKED.exists()
    snapshot = maintainer.repo_snapshot("exit=0")
    assert snapshot["pending_round_snapshots"] == maintainer.ops.StateSnapshot.pending() == []
    assert snapshot["incomplete_captures"] == ["killed"]
    assert (fragment / "partial.txt").read_text() == "partial"

    maintainer.ops.StateSnapshot.capture("complete", (), root=tmp_path)
    snapshot = maintainer.repo_snapshot("exit=0")
    assert snapshot["pending_round_snapshots"] == maintainer.ops.StateSnapshot.pending() == ["complete"]
    assert snapshot["incomplete_captures"] == ["killed"]
    assert maintainer.update_growth_block() == ["1 interrupted round snapshot(s) pending"]
    assert maintainer.BLOCKED.exists()


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
        "runtime_paused": {},
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
    monkeypatch.setattr(maintainer.ops, "SNAPSHOTS", workspace / "round-snapshots")
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
            assert command[command.index('--model') + 1] == 'claude-opus-5-5'
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


# --- the maintenance window writes through its own broker (ADR 0001 stage 3, step 5 review) -------

def maintenance_repo(tmp_path):
    reg = tmp_path / 'registry'
    reg.mkdir()
    (reg / 'backends.json').write_text(json.dumps({
        '_readme': 'control plane',
        'find_x': {'script': 'find_x.py', 'enabled': True},
        'find_kit': {'script': 'find_kit.py', 'enabled': False,
                     'reason': 'exhausted: KIT set fully harvested'},
        'find_dry': {'script': 'find_dry.py', 'enabled': True},
    }, indent=2) + '\n')
    (reg / 'rotation.json').write_text(json.dumps(
        {'find_x': {'flag': '--page', 'next': 3}}, indent=2) + '\n')
    (reg / 'backend_state.json').write_text(json.dumps(
        {'find_dry': {'enabled': False, 'reason': 'exhausted: walked to the end'}}) + '\n')
    (tmp_path / 'pruned_urls.txt').write_text('https://e.org/old\n')


CHILD = '''
import json, sys
from pathlib import Path
sys.path.insert(0, {scripts!r})
root = Path({root!r})
import blocklist, rotation, migrate_backend_state
blocklist.PATH = root / 'pruned_urls.txt'
rotation.PATH = root / 'registry' / 'rotation.json'
rotation.LOCK_TIMEOUT = 0.5
print(json.dumps({{
    'blocklist': blocklist.add(['https://e.org/new/']),
    'rotation': rotation.advance('find_x'),
    'migrate': migrate_backend_state.migrate(root, ['find_kit'], apply=True, timeout=0.5,
                                             log=lambda *_: None),
}}))
'''


def run_child(tmp_path, env):
    code = CHILD.format(scripts=str(Path(maintainer.__file__).parent), root=str(tmp_path))
    return subprocess.run([sys.executable, '-c', code], env=env, capture_output=True, text=True,
                          cwd=tmp_path, timeout=60)


def test_window_children_mutate_through_the_window_broker(tmp_path, monkeypatch):
    """A maintenance agent's mutations (blocklist, rotation CLI, backend-state migration) run as
    store transactions through the window's broker while the parent holds the round lock."""
    configure_maintenance(tmp_path, monkeypatch)
    maintenance_repo(tmp_path)
    with maintainer.maintenance_window('action'):
        env = maintainer.agent_env(Path(sys.executable))
        assert maintainer.store_broker.BROKER_ENV in env
        assert maintainer.ops.inherited_holders(env[maintainer.ops.INHERITED_LOCK_ENV])
        done = run_child(tmp_path, env)
        # the same child without the broker would wait on the parent's lock and fail
        bare = {k: v for k, v in env.items() if not k.startswith('NEKAISE_STORE_')}
        refused = run_child(tmp_path, bare)
    assert done.returncode == 0, done.stderr
    assert json.loads(done.stdout) == {'blocklist': 1, 'rotation': '--page 4', 'migrate': 0}
    assert refused.returncode != 0 and 'corpus-round' in refused.stderr
    assert (tmp_path / 'pruned_urls.txt').read_text() == 'https://e.org/old\nhttps://e.org/new\n'
    assert json.loads((tmp_path / 'registry/rotation.json').read_text())['find_x']['next'] == 4
    config = json.loads((tmp_path / 'registry/backends.json').read_text())
    assert config['find_kit'] == {'script': 'find_kit.py', 'enabled': True}
    st = maintainer.store.FileStore(tmp_path)
    with st.read() as view:
        assert view.backend_state_get('find_kit') == maintainer.store.BackendState(
            False, 'exhausted: KIT set fully harvested')
        assert not view.backend_enabled('find_kit')
        runs = {e['run_id'] for e in view.scan(maintainer.store.Table.EVENTS).rows}
    assert len(runs) == 3 and all(r.startswith('maint-') and '-action.' in r for r in runs)
    # the window's broker and its environment are gone afterwards
    assert maintainer.store_broker.BROKER_ENV not in os.environ
    assert_growth_unlocked()


def test_window_exports_nothing_after_a_busy_lock(tmp_path, monkeypatch):
    configure_maintenance(tmp_path, monkeypatch)
    with maintainer.ops.named_lock('corpus-round'):
        with pytest.raises(maintainer.MaintenanceBusy):
            with maintainer.maintenance_window('test'):
                pass
    assert maintainer.store_broker.BROKER_ENV not in os.environ


def test_backend_health_uses_effective_enablement_and_reports_runtime_pauses(tmp_path):
    history = tmp_path / 'history.jsonl'
    write_history(
        history,
        {'run_id': 'fail', 'event': 'backend_disabled', 'backend': 'finder', 'reason': 'rolled back'},
        {'run_id': 'fail', 'event': 'run_failed'},
        {'run_id': 'one', 'event': 'discovery_merged', 'accepted': {'finder': 2, 'other': 1}},
        {'run_id': 'one', 'event': 'backend_disabled', 'at': '2026-09-24T10:00:00Z',
         'backend': 'finder', 'reason': 'walked to the end'},
        {'run_id': 'one', 'event': 'run_completed', 'at': '2026-09-24T10:01:00Z'},
    )
    summary = maintainer.summarize_backend_health(
        history,
        {'finder': {'enabled': True}, 'other': {'enabled': True},
         'paused': {'enabled': False, 'reason': 'operator'}},
        {'finder': {'enabled': False, 'reason': 'exhausted: walked to the end'},
         'paused': {'enabled': False, 'reason': 'exhausted: old'}},
    )
    assert set(summary['backends']) == {'other'}
    assert summary['runtime_paused'] == {'finder': {
        'reason': 'exhausted: walked to the end',
        'disabled_by_round': {'run_id': 'one', 'at': '2026-09-24T10:00:00Z',
                              'reason': 'walked to the end'},
    }}
    assert summary['total_accepted'] == 3


def test_repo_snapshot_reads_backend_state_through_the_window_view(tmp_path, monkeypatch):
    configure_maintenance(tmp_path, monkeypatch)
    maintenance_repo(tmp_path)
    monkeypatch.setattr(maintainer.ops, 'SNAPSHOTS', tmp_path / 'workspace' / 'round-snapshots')
    monkeypatch.setattr(maintainer, 'git',
                        lambda *args, **kwargs: (0, '0 0' if args[0] == 'rev-list' else 'main'))
    with maintainer.maintenance_window('snapshot'):
        health = maintainer.repo_snapshot('exit=0')['backend_health']
    assert health['state_source'] == 'store_view'
    assert set(health['backends']) == {'find_x'}
    assert health['runtime_paused'] == {'find_dry': {
        'reason': 'exhausted: walked to the end', 'disabled_by_round': None}}
    # an unrecovered round snapshot blocks store views: the same documents are read as files
    (tmp_path / 'workspace' / 'round-snapshots' / 'r1').mkdir(parents=True)
    (tmp_path / 'workspace' / 'round-snapshots' / 'r1' / 'snapshot.json').write_text('{}')
    with maintainer.maintenance_window('snapshot'):
        health = maintainer.repo_snapshot('exit=0')['backend_health']
    assert health['state_source'].startswith('files (PendingTransaction')
    assert set(health['runtime_paused']) == {'find_dry'}


def test_timed_out_action_is_judged_only_after_its_broker_batch_finished(tmp_path, monkeypatch):
    """Codex review P2: the agent is killed on timeout while the window's broker still executes
    its batch; the growth-block check and the recorded outcome must see the settled result, with
    both locks still held."""
    import threading
    configure_maintenance(tmp_path, monkeypatch)
    maintenance_repo(tmp_path)
    started, finished = threading.Event(), threading.Event()
    real = maintainer.store.WriteView.blocklist_add

    def slow(self, urls):
        started.set()
        time.sleep(1.0)
        out = real(self, urls)
        finished.set()
        return out
    monkeypatch.setattr(maintainer.store.WriteView, 'blocklist_add', slow)
    monkeypatch.setattr(maintainer, 'resolve_agent', lambda name, override=None: Path('/bin') / name)
    monkeypatch.setattr(maintainer, 'git', lambda *args, **kwargs: (0, ''))
    monkeypatch.setattr(maintainer, 'recover_pending_round', lambda: None)
    monkeypatch.setattr(maintainer, 'repo_snapshot', lambda *a, **k: {'head': 'h'})
    checks = []

    def check_block():
        if checks or 'action' in calls:  # the action's check
            assert_growth_locked()
            assert finished.is_set(), 'growth block judged while a broker batch was running'
            assert (tmp_path / 'pruned_urls.txt').read_text().endswith('https://e.org/late\n')
        checks.append(finished.is_set())
        return []
    monkeypatch.setattr(maintainer, 'update_growth_block', check_block)
    calls = []

    def run(command, **kwargs):
        kwargs['stdout_path'].write_text('')
        kwargs['stderr_path'].write_text('')
        if '--output-schema' in command:
            out = Path(command[command.index('--output-last-message') + 1])
            out.write_text(json.dumps({
                'needs_action': True, 'action_kind': 'publish', 'urgency': 'routine',
                'summary': 's', 'evidence': ['e'], 'proposed_actions': ['a']}))
            return 0
        calls.append('action')
        code = ('import sys; sys.path.insert(0, %r); import blocklist; from pathlib import Path\n'
                'blocklist.PATH = Path(%r)\nblocklist.add(["https://e.org/late"])\n'
                % (str(Path(maintainer.__file__).parent), str(tmp_path / 'pruned_urls.txt')))
        agent = subprocess.Popen([sys.executable, '-c', code], env=kwargs['env'])
        assert started.wait(10)
        agent.kill()  # the action timed out: its process group is stopped
        agent.wait()
        assert not finished.is_set()
        return 124
    monkeypatch.setattr(maintainer, 'run_command', run)

    assert maintainer.main() == 1
    assert checks == [False, True]
    history = [json.loads(l) for l in (tmp_path / 'logs' / 'history.jsonl').read_text().splitlines()]
    assert history[-1]['status'] == 'codex_action_failed'
    assert_growth_unlocked()


def test_a_round_started_inside_the_window_is_refused_at_once(tmp_path, monkeypatch):
    """Codex review P2: an agent running the canonical round inside the window must not wait on
    its own parent's lock; it is refused immediately and told which gates to run."""
    configure_maintenance(tmp_path, monkeypatch)
    maintenance_repo(tmp_path)
    code = ('import sys; sys.path.insert(0, %r); import run_round; from pathlib import Path\n'
            'run_round.ROOT = Path(%r)\nsys.argv = ["run_round.py", "--commit"]\n'
            'sys.exit(run_round.main())\n' % (str(Path(maintainer.__file__).parent), str(tmp_path)))
    with maintainer.maintenance_window('action'):
        env = maintainer.agent_env(Path(sys.executable))
        started = time.monotonic()
        nested = subprocess.run([sys.executable, '-c', code], env=env, capture_output=True,
                                text=True, timeout=60)
        elapsed = time.monotonic() - started
        recover = subprocess.run([sys.executable, '-c', code.replace('"--commit"', '"--recover", "latest"')],
                                 env=env, capture_output=True, text=True, timeout=60)
    assert nested.returncode == 2 and elapsed < 10
    assert 'cannot run nested' in nested.stderr
    for gate in ('clean_corpus.py --check', 'lint_registry.py', 'check_contracts.py',
                 'pytest -q tests/'):
        assert gate in nested.stderr
    assert recover.returncode == 2 and 'cannot run nested' in recover.stderr
    assert not (tmp_path / 'logs' / 'run_history.jsonl').exists()  # refused before any event


def test_nested_round_detection_ignores_stale_or_foreign_entries(tmp_path, monkeypatch):
    st = maintainer.store.FileStore(tmp_path)
    assert maintainer.run_round.nested_round_owner(st) is None
    lock = tmp_path / 'workspace' / '.corpus-round.lock'
    env = maintainer.ops.with_holder({}, 1, lock, 'old')  # nobody holds it any more
    monkeypatch.setenv(maintainer.ops.INHERITED_LOCK_ENV, env[maintainer.ops.INHERITED_LOCK_ENV])
    (tmp_path / 'workspace').mkdir()
    assert maintainer.run_round.nested_round_owner(st) is None
    other = maintainer.ops.with_holder({}, os.getppid(), tmp_path / 'elsewhere.lock', 'x')
    monkeypatch.setenv(maintainer.ops.INHERITED_LOCK_ENV, other[maintainer.ops.INHERITED_LOCK_ENV])
    assert maintainer.run_round.nested_round_owner(st) is None
