import os
import shutil
import subprocess
import sys
from pathlib import Path

import ops


ROOT = Path(__file__).resolve().parents[1]


def copy_script(root: Path, name: str) -> Path:
    scripts = root / "scripts"
    scripts.mkdir(parents=True, exist_ok=True)
    target = scripts / name
    shutil.copy2(ROOT / "scripts" / name, target)
    return target


def test_maintainer_waits_for_canonical_round_lock(tmp_path, monkeypatch):
    script = copy_script(tmp_path, "run_maintainer.sh")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "scripts" / "maintainer.py").write_text(
        "from pathlib import Path\nPath('maintainer-ran').write_text('yes')\n"
    )
    monkeypatch.setattr(ops, "WORKSPACE", workspace)
    env = {
        **os.environ,
        "MAINTAINER_LOCK_WAIT_SECONDS": "0.1",
        "PYTHON_BIN": sys.executable,
    }

    with ops.named_lock("corpus-round"):
        owner = (workspace / ".corpus-round.lock").read_text()
        result = subprocess.run(
            ["bash", str(script)], cwd=tmp_path, env=env, timeout=5, check=False
        )
        assert (workspace / ".corpus-round.lock").read_text() == owner

    assert result.returncode == 0
    assert not (tmp_path / "maintainer-ran").exists()
    assert not (workspace / ".maintenance-requested").exists()
    log = next((tmp_path / "logs").glob("maintainer-*.log")).read_text()
    assert "corpus round remained active" in log


def test_marathon_stops_cleanly_on_maintenance_block(tmp_path):
    script = copy_script(tmp_path, "marathon.sh")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".maintenance-blocked").write_text("tracked worktree is not clean\n")
    env = {
        **os.environ,
        "HOURS": "1",
        "MIN_FREE_KB": "0",
        "PYTHON_BIN": sys.executable,
    }

    result = subprocess.run(
        ["bash", str(script)], cwd=tmp_path, env=env, timeout=5, check=False
    )

    assert result.returncode == 0
    log = next((tmp_path / "logs").glob("marathon-*.log")).read_text()
    assert "maintenance blocked growth: tracked worktree is not clean" in log
    assert "round 1 start" not in log


def test_marathon_waits_for_shared_growth_window(tmp_path, monkeypatch):
    script = copy_script(tmp_path, "marathon.sh")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(ops, "WORKSPACE", workspace)
    env = {
        **os.environ,
        "HOURS": "1",
        "MARATHON_WINDOW_WAIT_SECONDS": "0.1",
        "MIN_FREE_KB": "0",
        "PYTHON_BIN": sys.executable,
    }

    with ops.named_lock("continuous-dig"):
        result = subprocess.run(
            ["bash", str(script)], cwd=tmp_path, env=env, timeout=5, check=False
        )

    assert result.returncode == 1
    log = next((tmp_path / "logs").glob("marathon-*.log")).read_text()
    assert "growth window unavailable for 0.1s" in log
    assert "round 1 start" not in log


def test_cron_removal_recognizes_legacy_continuous_tag(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    state = tmp_path / "crontab"
    state.write_text(
        "MAILTO=owner@example.test\n"
        "*/5 * * * * old-command # nekaise-corpus continuous dig\n"
        "0 2 * * * new-command # nekaise-corpus daily dig\n"
        "17 */6 * * * maintainer # nekaise-corpus ai maintainer\n"
    )
    crontab = fake_bin / "crontab"
    crontab.write_text(
        "#!/bin/sh\n"
        "if [ \"${1:-}\" = -l ]; then cat \"$FAKE_CRONTAB\"; "
        "else next=\"$FAKE_CRONTAB.new\"; cat > \"$next\"; mv \"$next\" \"$FAKE_CRONTAB\"; fi\n"
    )
    crontab.chmod(0o755)
    env = {
        **os.environ,
        "FAKE_CRONTAB": str(state),
        "PATH": str(fake_bin) + os.pathsep + os.environ["PATH"],
    }

    result = subprocess.run(
        ["bash", str(ROOT / "scripts" / "install_cron.sh"), "--remove"],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        timeout=5,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    remaining = state.read_text()
    assert "daily dig" not in remaining
    assert "continuous dig" not in remaining
    assert "ai maintainer" in remaining
    assert "MAILTO=owner@example.test" in remaining
