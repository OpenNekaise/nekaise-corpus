#!/usr/bin/env python3
"""pg_backup.py — base backups and restore drills for the local PostgreSQL store (ADR 0001).

WAL is archived continuously to the backup SSD by the server's archive_command
(~/.local/share/nekaise-pg/archive_wal.sh). This adds the other two halves of point-in-time
recovery:

    python scripts/pg_backup.py base            # compressed, manifest-checksummed base backup
    python scripts/pg_backup.py restore-test    # restore the newest base + WAL into a scratch
                                                # instance, compare row counts and watermark
    python scripts/pg_backup.py prune --keep 7  # keep the newest N bases; WAL older than the
                                                # oldest kept base is deleted

A restore drill that does not match the live server exits non-zero. Paths default to this host's
layout and can be overridden with NEKAISE_PG_BIN / NEKAISE_PG_SOCKET / NEKAISE_PG_BACKUP_DIR /
NEKAISE_PG_WAL_DIR.
"""
from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PGBIN = Path(os.environ.get("NEKAISE_PG_BIN", "/home/zengp/miniconda3/envs/nekaise-pg/bin"))
SOCKET = os.environ.get("NEKAISE_PG_SOCKET", "/home/zengp/.local/share/nekaise-pg/run")
BASES = Path(os.environ.get("NEKAISE_PG_BACKUP_DIR", "/media/zengp/ssd/nekaise-pg-base"))
WAL = Path(os.environ.get("NEKAISE_PG_WAL_DIR", "/media/zengp/ssd/nekaise-pg-wal"))
SCRATCH = Path(os.environ.get("NEKAISE_PG_SCRATCH", "/home/zengp/.local/share/nekaise-pg"))
COUNTS = ("SELECT (SELECT count(*) FROM nekaise.entries), (SELECT count(*) FROM nekaise.manifest), "
          "(SELECT count(*) FROM nekaise.blocklist), (SELECT count(*) FROM nekaise.ledger), "
          "(SELECT count(*) FROM nekaise.events), (SELECT replication->>'watermark' FROM nekaise.state)")


def run(*args, **kw) -> str:
    return subprocess.run([str(a) for a in args], check=True, text=True, capture_output=True,
                          **kw).stdout.strip()


def psql(socket: str, query: str, db: str = "nekaise") -> str:
    return run(PGBIN / "psql", "-h", socket, "-d", db, "-Atc", query)


def base() -> Path:
    if not BASES.parent.is_mount() and not BASES.exists():
        raise SystemExit(f"backup location {BASES} is unavailable (SSD not mounted?)")
    dest = BASES / time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    BASES.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(f".{dest.name}.partial")
    run(PGBIN / "pg_basebackup", "-h", SOCKET, "-D", tmp, "-Ft", "-z", "-X", "none", "-c", "fast",
        "--manifest-checksums=SHA256")
    run(PGBIN / "pg_verifybackup", "-n", "-m", tmp / "backup_manifest", tmp)
    tmp.rename(dest)  # a base only becomes visible once it is complete and verified
    print(f"base backup {dest} ({sum(f.stat().st_size for f in dest.iterdir()) / 1e9:.2f} GB)")
    return dest


def restore_test() -> bool:
    bases = sorted(p for p in BASES.iterdir() if p.is_dir() and not p.name.startswith("."))
    if not bases:
        raise SystemExit("no base backup to restore")
    latest = bases[-1]
    run(PGBIN / "pg_verifybackup", "-n", "-m", latest / "backup_manifest", latest)
    psql(SOCKET, "SELECT pg_switch_wal()", "postgres")  # make the live tail archivable
    live = psql(SOCKET, COUNTS)
    time.sleep(5)
    work = Path(tempfile.mkdtemp(prefix="restore-test-", dir=SCRATCH))
    data, sock = work / "data", work / "run"
    try:
        data.mkdir(mode=0o700)
        sock.mkdir()
        run("tar", "-xzf", latest / "base.tar.gz", "-C", data)
        with (data / "postgresql.conf").open("a") as f:
            f.write(f"\n# restore drill\nunix_socket_directories = '{sock}'\narchive_mode = off\n"
                    f"shared_buffers = 1GB\nrestore_command = 'cp {WAL}/%f %p'\n"
                    "recovery_target_action = 'promote'\n")
        (data / "recovery.signal").touch()
        run(PGBIN / "pg_ctl", "-D", data, "-l", work / "log", "-w", "-t", "900", "start")
        for _ in range(450):
            if psql(str(sock), "SELECT pg_is_in_recovery()", "postgres") == "f":
                break
            time.sleep(2)
        restored = psql(str(sock), COUNTS)
    finally:
        subprocess.run([str(PGBIN / "pg_ctl"), "-D", str(data), "-w", "stop"], capture_output=True)
        shutil.rmtree(work, ignore_errors=True)
    ok = restored == live
    print(f"restore drill from {latest.name}: {'OK' if ok else 'MISMATCH'}\n  live     {live}\n"
          f"  restored {restored}")
    return ok


def prune(keep: int) -> None:
    bases = sorted(p for p in BASES.iterdir() if p.is_dir() and not p.name.startswith("."))
    for old in bases[:-keep]:
        shutil.rmtree(old)
        print(f"removed base {old.name}")
    kept = bases[-keep:]
    if kept:  # WAL older than the oldest kept base's start segment is no longer needed
        segment = start_segment(kept[0])
        run(PGBIN / "pg_archivecleanup", WAL, segment)
        print(f"WAL archive cleaned up to {segment}")


def start_segment(base_dir: Path) -> str:
    """WAL segment file holding a base backup's start LSN (16 MiB segments), per its manifest."""
    import json
    wal = json.loads((base_dir / "backup_manifest").read_text())["WAL-Ranges"][0]
    hi, lo = (int(x, 16) for x in wal["Start-LSN"].split("/"))
    segno = ((hi << 32) | lo) // (16 * 1024 * 1024)
    return f"{wal['Timeline']:08X}{segno // 256:08X}{segno % 256:08X}"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("command", choices=("base", "restore-test", "prune"))
    ap.add_argument("--keep", type=int, default=7)
    args = ap.parse_args()
    if args.command == "base":
        base()
    elif args.command == "restore-test":
        return 0 if restore_test() else 1
    else:
        prune(args.keep)
    return 0


if __name__ == "__main__":
    sys.exit(main())
