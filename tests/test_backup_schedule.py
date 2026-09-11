import hashlib
import json
import os
import subprocess
import time
from pathlib import Path

import pytest

import backup_corpus
import backup_schedule as schedule
import ops


@pytest.fixture
def setup(tmp_path, monkeypatch):
    root = tmp_path / "repo"
    mount = tmp_path / "ssd"
    root.mkdir()
    mount.mkdir()
    monkeypatch.setattr(schedule, "ROOT", root)
    monkeypatch.setattr(schedule, "CONFIG", root / "workspace/backup-config.json")
    monkeypatch.setattr(schedule, "STATUS", root / "workspace/backup-status.json")
    monkeypatch.setattr(ops, "WORKSPACE", root / "workspace")
    monkeypatch.setattr(schedule, "ensure_drive", lambda config: mount)
    config = {"mount": str(mount), "uuid": "abc-123", "interval_hours": 24, "keep": 2}
    return root, mount, config


def archive(mount, number, age=0, partial=False):
    folder = mount / "nekaise-corpus-backups" / f"corpus-20260901T000000Z-{number:08x}"
    if partial:
        folder = folder.with_name(folder.name + ".partial")
    folder.mkdir(parents=True)
    (folder / "corpus.tar.gz").write_bytes(b"test archive")
    if not partial:
        digest = hashlib.sha256(b"test archive").hexdigest()
        (folder / "SHA256SUMS").write_text(f"{digest}  corpus.tar.gz\n")
        (folder / "RESTORE.txt").write_text("restore instructions\n")
    stamp = time.time() - age
    os.utime(folder, (stamp, stamp))
    return folder


def test_recent_backup_skips_copy_without_waiting_for_round(setup, monkeypatch):
    _, mount, config = setup
    latest = archive(mount, 1)
    monkeypatch.setattr(backup_corpus, "backup", lambda *a, **kw: pytest.fail("not due"))
    with ops.named_lock("corpus-round"):
        assert schedule.run(config) == 0
    state = json.loads(schedule.STATUS.read_text())
    assert state["result"] == "up-to-date"
    assert state["latest_backup"] == str(latest)


def test_due_backup_retains_old_copies_until_success_and_leaves_unrelated_files(setup, monkeypatch):
    _, mount, config = setup
    oldest = archive(mount, 1, age=200000)
    recent = archive(mount, 2, age=190000)
    unrelated = oldest.parent / "family-photos"
    unrelated.mkdir()
    (unrelated / "photo.jpg").write_bytes(b"photo")
    abandoned = archive(mount, 3, age=200000, partial=True)
    fresh_partial = archive(mount, 4, partial=True)
    def create(*args, **kwargs):
        assert oldest.exists() and recent.exists()
        assert kwargs["expected_uuid"] == config["uuid"]
        return archive(mount, 5)
    monkeypatch.setattr(backup_corpus, "backup", create)
    assert schedule.run(config) == 0
    assert not oldest.exists()
    assert recent.exists() and unrelated.exists() and fresh_partial.exists()
    assert not abandoned.exists()
    assert len(schedule.candidates(mount)) == 2
    assert json.loads(schedule.STATUS.read_text())["result"] == "completed"


def test_failed_backup_preserves_successes_and_backs_off(setup, monkeypatch):
    _, mount, config = setup
    originals = [archive(mount, n, age=200000) for n in range(4)]
    def fail(*args, **kwargs):
        raise OSError("write failed")
    monkeypatch.setattr(backup_corpus, "backup", fail)
    assert schedule.run(config) == 1
    assert all(p.exists() for p in originals)
    state = json.loads(schedule.STATUS.read_text())
    assert state["result"] == "failed" and state["retry_after"] > time.time()
    monkeypatch.setattr(backup_corpus, "backup", lambda *a, **kw: pytest.fail("retry too early"))
    assert schedule.run(config) == 0


def test_disconnected_drive_records_retryable_status(setup, monkeypatch):
    _, mount, config = setup
    def unavailable(config):
        raise RuntimeError("Drive is missing")
    monkeypatch.setattr(schedule, "ensure_drive", unavailable)
    assert schedule.run(config) == 0
    assert not list(mount.iterdir())
    assert json.loads(schedule.STATUS.read_text())["result"] == "drive-unavailable"


def test_wrong_uuid_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "is_mount", lambda p: True)
    monkeypatch.setattr(backup_corpus.subprocess, "check_output", lambda *a, **k: "other-drive\n")
    with pytest.raises(RuntimeError, match="Wrong drive"):
        backup_corpus.require_mount(tmp_path, "expected-drive")


def test_retention_ignores_symlinks_and_unknown_contents(setup):
    _, mount, _ = setup
    unknown = archive(mount, 1)
    (unknown / "personal.txt").write_text("keep")
    linked = archive(mount, 2)
    (linked / "corpus.tar.gz").unlink()
    (linked / "corpus.tar.gz").symlink_to(unknown / "corpus.tar.gz")
    incomplete = archive(mount, 3)
    (incomplete / "SHA256SUMS").write_text("bad checksum")
    assert schedule.candidates(mount) == []


def test_forced_schedule_uses_real_backup_pipeline(setup, monkeypatch):
    root, mount, config = setup
    for name in ("corpus", "manifest", "registry", "scripts"):
        (root / name).mkdir()
    (root / "corpus/.ruleset").write_text("none\n")
    (root / "corpus/sample.md").write_text("Concrete structures\n")
    (root / "scripts/clean_corpus.py").write_text("raise SystemExit(0)\n")
    (root / "pruned_urls.txt").write_text("")
    (root / "requirements.lock").write_text("")
    monkeypatch.setattr(backup_corpus, "require_mount", lambda *a: None)
    assert schedule.run(config, force=True) == 0
    completed = schedule.candidates(mount)
    assert len(completed) == 1
    digest = hashlib.sha256((completed[0] / "corpus.tar.gz").read_bytes()).hexdigest()
    assert (completed[0] / "SHA256SUMS").read_text().startswith(digest)


def test_install_is_idempotent_and_preserves_other_cron_jobs(setup, monkeypatch):
    _, mount, _ = setup
    monkeypatch.setattr(schedule, "drive_uuid", lambda path: "abc-123")
    state = {"text": "MAILTO=owner@example.test\n0 2 * * * unrelated-job\n"}
    monkeypatch.setattr(schedule, "crontab_text", lambda: state["text"])
    def write_cron(cmd, *, input, **kwargs):
        assert cmd == ["crontab", "-"]
        state["text"] = input
    monkeypatch.setattr(schedule.subprocess, "run", write_cron)
    schedule.install(mount)
    schedule.install(mount)
    assert state["text"].count(schedule.TAG) == 1
    assert "MAILTO=owner@example.test\n0 2 * * * unrelated-job\n" in state["text"]
    assert json.loads(schedule.CONFIG.read_text())["uuid"] == "abc-123"


def test_install_failure_restores_previous_config(setup, monkeypatch):
    _, mount, _ = setup
    ops.atomic_write_text(schedule.CONFIG, '{"previous": true}\n')
    monkeypatch.setattr(schedule, "drive_uuid", lambda path: "abc-123")
    monkeypatch.setattr(schedule, "crontab_text", lambda: "")
    def fail(*a, **kw):
        raise subprocess.CalledProcessError(1, "crontab")
    monkeypatch.setattr(schedule.subprocess, "run", fail)
    with pytest.raises(subprocess.CalledProcessError):
        schedule.install(mount)
    assert json.loads(schedule.CONFIG.read_text()) == {"previous": True}


def test_mount_table_is_idempotent_and_preserves_system_mounts(setup):
    _, _, config = setup
    original = "# system mounts\nUUID=root / ext4 defaults 0 1\n"
    updated = schedule.mount_table(config, original)
    assert updated.startswith(original)
    assert "UUID=abc-123" in updated
    assert "nofail,user,x-systemd.device-timeout=5s" in updated
    assert schedule.mount_table(config, updated) == updated


def test_mount_table_refuses_conflicting_entries(setup):
    _, _, config = setup
    with pytest.raises(RuntimeError, match="existing fstab entry"):
        schedule.mount_table(config, "UUID=abc-123 /different ext4 defaults 0 2\n")
    with pytest.raises(RuntimeError, match="existing fstab entry"):
        schedule.mount_table(config, f"UUID=another {config['mount']} ext4 defaults 0 2\n")
