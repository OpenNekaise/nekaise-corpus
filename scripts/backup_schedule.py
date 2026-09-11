#!/usr/bin/env python3
"""Install, run, inspect, or remove automatic verified SSD backups."""
from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

import backup_corpus
import ops

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "workspace/backup-config.json"
STATUS = ROOT / "workspace/backup-status.json"
TAG = "# nekaise-corpus automatic backup"
NAME = re.compile(r"corpus-\d{8}T\d{6}Z-[0-9a-f]{8}")
FILES = {"corpus.tar.gz", "SHA256SUMS", "RESTORE.txt"}


def drive_uuid(mount: Path) -> str:
    backup_corpus.require_mount(mount)
    value = subprocess.check_output(
        ["findmnt", "-rn", "-o", "UUID", "--mountpoint", str(mount)], text=True,
    ).strip()
    if not value or not re.fullmatch(r"[A-Za-z0-9-]+", value):
        raise RuntimeError(f"Cannot identify the filesystem at {mount}")
    return value


def ensure_drive(config: dict) -> Path:
    mount = Path(config["mount"])
    if not mount.is_mount():
        device = Path("/dev/disk/by-uuid") / config["uuid"]
        if device.exists():
            # mount-setup installs a UUID-pinned fstab entry with the user option.
            result = subprocess.run(["mount", str(mount)], capture_output=True, text=True, timeout=30)
            if result.returncode and shutil.which("udisksctl"):
                result = subprocess.run(
                    ["udisksctl", "mount", "--no-user-interaction", "-b", str(device)],
                    capture_output=True, text=True, timeout=30,
                )
            if result.returncode:
                raise RuntimeError("SSD could not be mounted unattended: " + result.stderr.strip())
    if drive_uuid(mount) != config["uuid"]:
        raise RuntimeError(f"Wrong drive at {mount}; expected UUID {config['uuid']}")
    return mount


def candidates(mount: Path, *, partial: bool = False) -> list[Path]:
    """Recognize only this tool's archives; never delete symlinks or unrelated content."""
    parent = mount / "nekaise-corpus-backups"
    if parent.is_symlink():
        raise RuntimeError("Backup directory must not be a symlink")
    found = []
    if not parent.exists():
        return found
    for path in parent.iterdir():
        name = path.name.removesuffix(".partial") if partial else path.name
        if partial and not path.name.endswith(".partial"):
            continue
        if not NAME.fullmatch(name) or path.is_symlink() or not path.is_dir():
            continue
        files = list(path.iterdir())
        if any(p.is_symlink() or not p.is_file() or p.name not in FILES for p in files):
            continue
        if not partial:
            if {p.name for p in files} != FILES:
                continue
            checksum = (path / "SHA256SUMS").read_text()
            if not re.fullmatch(r"[0-9a-f]{64}  corpus\.tar\.gz\n", checksum):
                continue
            if not (path / "corpus.tar.gz").stat().st_size:
                continue
        found.append(path)
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def record(result: str, **fields) -> None:
    previous = json.loads(STATUS.read_text()) if STATUS.exists() else {}
    previous.update(result=result, checked_at=time.time(), **fields)
    ops.atomic_write_text(STATUS, json.dumps(previous, indent=2) + "\n")
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {result}: " + json.dumps(fields), flush=True)


def recent(config: dict, mount: Path) -> bool:
    completed = candidates(mount)
    if not completed:
        return False
    latest = completed[0]
    due = latest.stat().st_mtime + config["interval_hours"] * 3600
    if time.time() < due:
        record("up-to-date", latest_backup=str(latest), next_due=due, error=None)
        return True
    return False


def run(config: dict, *, force: bool = False) -> int:
    with ops.named_lock("backup-schedule"):
        try:
            mount = ensure_drive(config)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            record("drive-unavailable", error=str(exc))
            return 0  # The hourly tick retries when the drive is available.
        try:
            if not force and recent(config, mount):
                return 0
            previous = json.loads(STATUS.read_text()) if STATUS.exists() else {}
            if not force and time.time() < previous.get("retry_after", 0):
                print("Previous attempt failed; next retry at " + time.ctime(previous["retry_after"]))
                return 0
            record("waiting-for-round", error=None)
            with ops.named_lock("corpus-round", timeout=10800):
                mount = ensure_drive(config)
                if not force and recent(config, mount):
                    return 0
                # Canonical lock excludes active writers, including manual backups.
                for path in candidates(mount, partial=True):
                    if time.time() - path.stat().st_mtime > 48 * 3600:
                        print(f"Removing abandoned partial backup: {path}", flush=True)
                        shutil.rmtree(path)
                record("backing-up", error=None)
                completed = backup_corpus.backup(ROOT, mount, expected_uuid=config["uuid"])
                # Never evict a successful backup until its replacement is verified.
                for path in candidates(mount)[config["keep"]:]:
                    if path != completed:
                        print(f"Removing expired backup: {path}", flush=True)
                        shutil.rmtree(path)
                record("completed", latest_backup=str(completed), error=None, retry_after=0,
                       next_due=time.time() + config["interval_hours"] * 3600)
            return 0
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            record("failed", error=str(exc), retry_after=time.time() + 6 * 3600)
            return 1


def crontab_text() -> str:
    result = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    if result.returncode and "no crontab for" not in result.stderr.lower():
        raise RuntimeError("Cannot read crontab: " + result.stderr.strip())
    return result.stdout


def install(mount: Path, interval_hours: int = 24, keep: int = 7) -> None:
    if interval_hours < 1 or keep < 1:
        raise ValueError("interval-hours and keep must be positive")
    config = {"mount": str(mount.resolve()), "uuid": drive_uuid(mount),
              "interval_hours": interval_hours, "keep": keep}
    old_cron = crontab_text()
    (ROOT / "logs").mkdir(exist_ok=True)
    old_config = CONFIG.read_bytes() if CONFIG.exists() else None
    ops.atomic_write_text(CONFIG, json.dumps(config, indent=2) + "\n")
    command = (f"cd {shlex.quote(str(ROOT))} && "
               f"{shlex.join([sys.executable, str(ROOT / 'scripts/backup_schedule.py'), 'run'])} "
               f">> {shlex.quote(str(ROOT / 'logs/backup-scheduled.log'))} 2>&1")
    line = "15 * * * * " + command.replace("%", r"\%") + "  " + TAG
    retained = [s for s in old_cron.splitlines() if not s.endswith(TAG)]
    try:
        subprocess.run(["crontab", "-"], input="\n".join([*retained, line]) + "\n", text=True, check=True)
    except BaseException:
        if old_config is None:
            CONFIG.unlink()
        else:
            ops.atomic_write_bytes(CONFIG, old_config)
        raise
    print(f"Installed hourly check: backup every {interval_hours}h, keep {keep} verified copies.")
    print(f"Drive: {config['mount']} (UUID {config['uuid']})")


def load_config() -> dict:
    config = json.loads(CONFIG.read_text())
    if (not Path(config["mount"]).is_absolute()
            or not re.fullmatch(r"[A-Za-z0-9-]+", config["uuid"])
            or not isinstance(config["keep"], int) or config["keep"] < 1
            or not isinstance(config["interval_hours"], int) or config["interval_hours"] < 1):
        raise ValueError("Invalid backup schedule configuration")
    return config


def mount_table(config: dict, original: str) -> str:
    target = config["mount"]
    if any(c.isspace() for c in target) or "\\" in target or "#" in target:
        raise ValueError("Automatic mount setup requires a mount path without whitespace, # or backslashes")
    device = "UUID=" + config["uuid"]
    line = f"{device} {target} ext4 defaults,nofail,user,x-systemd.device-timeout=5s 0 2"
    for existing in original.splitlines():
        fields = existing.split()
        if not fields or fields[0].startswith("#"):
            continue
        if fields[0] == device or (len(fields) > 1 and fields[1] == target):
            if fields == line.split():
                return original
            raise RuntimeError("An existing fstab entry uses this SSD or mount point; leaving it unchanged")
    return original.rstrip("\n") + "\n\n# nekaise-corpus backup SSD\n" + line + "\n"


def mount_setup(config: dict, *, dry_run: bool = False) -> None:
    mount = Path(config["mount"])
    if drive_uuid(mount) != config["uuid"]:
        raise RuntimeError("Configured SSD must be mounted before setting up automatic remounts")
    kind = subprocess.check_output(
        ["findmnt", "-rn", "-o", "FSTYPE", "--mountpoint", str(mount)], text=True,
    ).strip()
    if kind != "ext4":
        raise RuntimeError("Automatic mount setup currently supports ext4 backup drives")
    table = Path("/etc/fstab")
    original = table.read_text()
    updated = mount_table(config, original)
    if dry_run:
        print(updated)
        return
    if os.geteuid() != 0:
        raise RuntimeError("Run mount-setup with sudo; /etc/fstab requires administrator authentication")
    if updated == original:
        print("Automatic SSD mount is already configured.")
        return
    saved = Path("/etc/fstab.nekaise-corpus.bak")
    if not saved.exists():
        ops.atomic_write_text(saved, original)
    ops.atomic_write_text(table, updated)
    try:
        subprocess.run(["systemctl", "daemon-reload"], check=True)
    except BaseException:
        ops.atomic_write_text(table, original)
        subprocess.run(["systemctl", "daemon-reload"], check=False)
        raise
    print("SSD will mount at boot when connected; scheduled backups can also remount it without a password.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    setup = sub.add_parser("install")
    setup.add_argument("--mount", type=Path, default=backup_corpus.DEFAULT_MOUNT)
    setup.add_argument("--interval-hours", type=int, default=24)
    setup.add_argument("--keep", type=int, default=7)
    execute = sub.add_parser("run")
    execute.add_argument("--force", action="store_true", help="back up now even if a recent copy exists")
    sub.add_parser("status")
    sub.add_parser("remove")
    mount_parser = sub.add_parser("mount-setup", help="configure boot/reconnect mounting (requires sudo)")
    mount_parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "install":
            install(args.mount, args.interval_hours, args.keep)
        elif args.command == "remove":
            remaining = [s for s in crontab_text().splitlines() if not s.endswith(TAG)]
            subprocess.run(["crontab", "-"], input="\n".join(remaining) + "\n", text=True, check=True)
            print("Removed automatic backup cron; existing backups and configuration retained.")
        elif args.command == "mount-setup":
            mount_setup(load_config(), dry_run=args.dry_run)
        elif args.command == "status":
            state = json.loads(STATUS.read_text()) if STATUS.exists() else None
            if state:
                for field in ("checked_at", "next_due", "retry_after"):
                    if state.get(field):
                        state[field] = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(state[field]))
            print(json.dumps({"installed": any(s.endswith(TAG) for s in crontab_text().splitlines()),
                              "config": load_config() if CONFIG.exists() else None,
                              "status": state}, indent=2))
        else:
            return run(load_config(), force=args.force)
    except (OSError, ValueError, KeyError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"Backup schedule: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
