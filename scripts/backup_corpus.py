#!/usr/bin/env python3
"""Write a verified, dated corpus + provenance tar.gz archive to a mounted backup drive."""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import stat
import subprocess
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

import ops

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MOUNT = Path("/media/zengp/ssd")
CONTENTS = ("corpus", "manifest", "registry", "pruned_urls.txt", "requirements.lock")
CHUNK = 4 * 1024 * 1024


def require_mount(mount: Path, expected_uuid: str | None = None) -> None:
    if not mount.is_mount():
        raise RuntimeError(f"Backup drive is not mounted at {mount}; refusing to write locally")
    if expected_uuid:
        actual = subprocess.check_output(
            ["findmnt", "-rn", "-o", "UUID", "--mountpoint", str(mount)], text=True,
        ).strip()
        if actual != expected_uuid:
            raise RuntimeError(f"Wrong drive at {mount}; expected UUID {expected_uuid}, found {actual}")


def inventory(root: Path) -> tuple[int, int]:
    """Estimate tar storage and reject links/special files instead of backing up pointers."""
    count = size = 0

    def visit(path: Path) -> None:
        nonlocal count, size
        info = path.lstat()
        count += 1
        if stat.S_ISREG(info.st_mode):
            size += ((info.st_size + 511) // 512) * 512
        elif stat.S_ISDIR(info.st_mode):
            with os.scandir(path) as entries:
                for entry in entries:
                    visit(Path(entry.path))
        else:
            raise RuntimeError(f"Unsupported backup input (link or special file): {path}")

    for name in CONTENTS:
        visit(root / name)
    # Allow for tar headers, long path records, and spare filesystem space.
    return count, size + count * 2048 + 64 * 1024 * 1024


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    size = 0
    last = time.monotonic()
    with path.open("rb") as stream:
        while block := stream.read(CHUNK):
            digest.update(block)
            size += len(block)
            if time.monotonic() - last >= 30:
                print(f"Verified {size / 2**30:.1f} GiB", flush=True)
                last = time.monotonic()
    return digest.hexdigest()


def write_archive(root: Path, path: Path) -> str:
    """Hash the tar stream as it is written; propagate tar's changed-file/read errors."""
    digest = hashlib.sha256()
    size = 0
    last = time.monotonic()
    env = dict(os.environ)
    env.pop("TAR_OPTIONS", None)
    env.pop("GZIP", None)
    env.pop("PIGZ", None)
    compressor = "pigz -1 -p 8" if shutil.which("pigz") else "gzip -1"
    with path.open("xb") as output:
        with subprocess.Popen(
            ["tar", "--create", "--format=posix", f"--use-compress-program={compressor}",
             "--file=-", "--", *CONTENTS],
            cwd=root, env=env, stdout=subprocess.PIPE,
        ) as process:
            try:
                while block := process.stdout.read(CHUNK):
                    output.write(block)
                    digest.update(block)
                    size += len(block)
                    if time.monotonic() - last >= 30:
                        print(f"Copied {size / 2**30:.1f} GiB", flush=True)
                        last = time.monotonic()
                if process.wait():
                    raise RuntimeError("tar failed; backup remains incomplete")
                output.flush()
                os.fsync(output.fileno())
            except BaseException:
                if process.poll() is None:
                    process.terminate()
                process.wait()
                raise
    return digest.hexdigest()


def backup(root: Path, mount: Path, *, dry_run: bool = False,
           expected_uuid: str | None = None) -> Path | None:
    require_mount(mount, expected_uuid)
    if not shutil.which("tar") or not shutil.which("gzip"):
        raise RuntimeError("tar and gzip are required")
    stamp = root / "corpus" / ".ruleset"
    ruleset = stamp.read_text()
    if ruleset.startswith("IN-PROGRESS"):
        raise RuntimeError("Corpus cleaning is incomplete; finish cleaning before backing up")
    print("Measuring corpus and provenance…", flush=True)
    count, required = inventory(root)
    free = shutil.disk_usage(mount).free
    print(f"{count:,} entries; need at most about {required / 2**30:.1f} GiB; "
          f"{free / 2**30:.1f} GiB free on {mount}", flush=True)
    if free < required:
        raise RuntimeError("Not enough free space for a complete backup")
    if dry_run:
        return None

    # Validate training eligibility and provenance before copying, under the round lock.
    subprocess.run([sys.executable, str(root / "scripts" / "clean_corpus.py"), "--check"],
                   cwd=root, check=True)
    require_mount(mount, expected_uuid)
    destination = mount / "nekaise-corpus-backups"
    if destination.is_symlink():
        raise RuntimeError(f"Backup directory must not be a symlink: {destination}")
    destination.mkdir(exist_ok=True)
    name = datetime.now(timezone.utc).strftime("corpus-%Y%m%dT%H%M%SZ-") + uuid.uuid4().hex[:8]
    partial = destination / (name + ".partial")
    final = destination / name
    partial.mkdir()
    archive = partial / "corpus.tar.gz"
    print(f"Writing {archive}", flush=True)
    expected = write_archive(root, archive)
    if stamp.read_text() != ruleset:
        raise RuntimeError("Cleaning ruleset changed during backup; backup remains incomplete")
    print("Reading the archive back to verify SHA-256…", flush=True)
    if hash_file(archive) != expected:
        raise RuntimeError("Backup checksum mismatch; backup remains incomplete")
    ops.atomic_write_text(partial / "SHA256SUMS", f"{expected}  corpus.tar.gz\n")
    ops.atomic_write_text(partial / "RESTORE.txt",
                          "Verify from this directory: sha256sum -c SHA256SUMS\n"
                          "Restore into an empty directory: tar -xzf corpus.tar.gz -C /path/to/restore\n"
                          "Contains corpus/ (including .ruleset), manifest/, registry/, "
                          "pruned_urls.txt and requirements.lock.\n"
                          "Before training, review the restored eligibility policy against "
                          "current registry/eligibility.json.\n")
    partial.rename(final)
    directory_fd = os.open(destination, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    print(f"Backup complete: {final}\nSHA-256: {expected}", flush=True)
    return final


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mount", type=Path, default=DEFAULT_MOUNT,
                        help=f"existing mounted backup drive (default: {DEFAULT_MOUNT})")
    parser.add_argument("--dry-run", action="store_true", help="check inputs and space; write no backup")
    parser.add_argument("--lock-timeout", type=float, default=0,
                        help="seconds to wait for a corpus round (default: fail if busy; -1: forever)")
    args = parser.parse_args()
    try:
        require_mount(args.mount)
        print("Acquiring corpus-round lock…", flush=True)
        with ops.named_lock("corpus-round", timeout=args.lock_timeout):
            backup(ROOT, args.mount, dry_run=args.dry_run)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:
        print(f"Backup failed: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Backup interrupted; any .partial directory is incomplete", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
