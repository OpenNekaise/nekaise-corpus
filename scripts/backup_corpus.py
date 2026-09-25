#!/usr/bin/env python3
"""Write a verified, dated archive of every CLEANED view + provenance to a mounted backup drive.

Scope (operator, 2026-09-25): the external SSD stores the cleaned corpus only — the default
corpus/ view, every classified view collection/<class>/corpus/ (restricted-use classes are
backed up exactly like default content), and the provenance metadata (registry, manifest,
blocklist, requirements.lock). raw/ and text/ (and the rebuildable collection/<class>/{raw,text}
link views) are NOT archived here: their durability is an open question for the stage-5
content-addressed / object store (docs/decisions/0001-storage-architecture.md).

Each archive directory carries SCOPE.json (format 2, scope "cleaned-views", the views it holds
and the classification-policy version); backup_schedule.py only counts such archives as fresh.
A capacity guard refuses to write unless the drive keeps a safety margin free afterwards (the
SSD also holds the PostgreSQL WAL and base backups)."""
from __future__ import annotations

import argparse
import hashlib
import json
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
import store
import store_authority

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_MOUNT = Path("/media/zengp/ssd")
# the tracked provenance layout the file store owns, and the dependency lock (with the views)
PROVENANCE = (*store.TRACKED_PATHS, "requirements.lock")
CHUNK = 4 * 1024 * 1024
SCOPE_FORMAT = 2
SCOPE = "cleaned-views"
# Free space that must REMAIN on the drive after the archive is written: the SSD is shared with
# the PostgreSQL WAL archive and base backups, which must never be starved by a corpus backup.
SAFETY_MARGIN_BYTES = 64 * 2**30
SAFETY_MARGIN_FRACTION = 0.03


def views(root: Path) -> list[str]:
    """The cleaned-view directories to archive: corpus/ always, and each existing
    collection/<class>/corpus/ (never the raw/text link views beside them)."""
    import registry
    out = ["corpus"]
    for view in registry.CLASSIFIED_VIEWS:
        rel = registry.view_root(view)
        if (root / rel).is_dir():
            out.append(rel)
    return out


def contents(root: Path) -> tuple[str, ...]:
    return (*views(root), *PROVENANCE)


def safety_margin(mount: Path) -> int:
    return max(SAFETY_MARGIN_BYTES, int(shutil.disk_usage(mount).total * SAFETY_MARGIN_FRACTION))


def require_mount(mount: Path, expected_uuid: str | None = None) -> None:
    if not mount.is_mount():
        raise RuntimeError(f"Backup drive is not mounted at {mount}; refusing to write locally")
    if expected_uuid:
        actual = subprocess.check_output(
            ["findmnt", "-rn", "-o", "UUID", "--mountpoint", str(mount)], text=True,
        ).strip()
        if actual != expected_uuid:
            raise RuntimeError(f"Wrong drive at {mount}; expected UUID {expected_uuid}, found {actual}")


def inventory(root: Path, names: tuple[str, ...] | None = None) -> tuple[int, int]:
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

    for name in names if names is not None else contents(root):
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


def write_archive(root: Path, path: Path, names: tuple[str, ...] | None = None) -> str:
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
             "--file=-", "--", *(names if names is not None else contents(root))],
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
    names = contents(root)
    print(f"Measuring the cleaned views ({', '.join(views(root))}) and provenance…", flush=True)
    count, required = inventory(root, names)
    free = shutil.disk_usage(mount).free
    margin = safety_margin(mount)
    print(f"{count:,} entries; need at most about {required / 2**30:.1f} GiB; "
          f"{free / 2**30:.1f} GiB free on {mount}; keeping {margin / 2**30:.0f} GiB free "
          "for the database backups", flush=True)
    if free < required:
        raise RuntimeError("Not enough free space for a complete backup")
    if free - required < margin:
        raise RuntimeError(f"Not enough free space: the archive would leave less than the "
                           f"{margin / 2**30:.0f} GiB safety margin on {mount}")
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
    expected = write_archive(root, archive, names)
    if stamp.read_text() != ruleset:
        raise RuntimeError("Cleaning ruleset changed during backup; backup remains incomplete")
    print("Reading the archive back to verify SHA-256…", flush=True)
    if hash_file(archive) != expected:
        raise RuntimeError("Backup checksum mismatch; backup remains incomplete")
    ops.atomic_write_text(partial / "SHA256SUMS", f"{expected}  corpus.tar.gz\n")
    import registry
    held = views(root)
    ops.atomic_write_text(partial / "SCOPE.json", json.dumps({
        "format": SCOPE_FORMAT, "scope": SCOPE, "views": held,
        "class_policy": registry.CLASS_POLICY_VERSION, "ruleset": ruleset.strip(),
        "not_included": "raw/, text/ and collection/<class>/{raw,text}/ (the SSD holds the "
                        "cleaned corpus only; see ADR 0001, raw/text durability)"},
        indent=2, sort_keys=True) + "\n")
    ops.atomic_write_text(partial / "RESTORE.txt",
                          "Verify from this directory: sha256sum -c SHA256SUMS\n"
                          "Restore into an empty directory: tar -xzf corpus.tar.gz -C /path/to/restore\n"
                          "Contains the default cleaned view corpus/ (including .ruleset), every "
                          "classified cleaned view collection/<class>/corpus/ listed in "
                          "SCOPE.json, manifest/, registry/, pruned_urls.txt and "
                          "requirements.lock. raw/ and text/ are not included.\n"
                          "A training run reads corpus/ only; collection/<class>/corpus/ holds "
                          "restricted-use classes (registry.use_class). Before training, review "
                          "the restored eligibility policy against current "
                          "registry/eligibility.json.\n")
    partial.rename(final)
    directory_fd = os.open(destination, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    print(f"Backup complete: {final}\nSHA-256: {expected}", flush=True)
    return final


def locked_backup(root: Path, mount: Path, *, dry_run: bool = False,
                  expected_uuid: str | None = None) -> Path | None:
    """The backup itself, called with the corpus-round lock held. The archive holds the tracked
    file layout as provenance: only meaningful while the files are authoritative
    (scripts/store_authority.py), checked here under the lock, since authority can move while
    this process waits for it. Under PostgreSQL authority the database backups (pg_backup.py)
    replace it (until stage 4 step 5): refuse rather than archive frozen files."""
    store_authority.require_file_mode(root, "backup_corpus.py")
    return backup(root, mount, dry_run=dry_run, expected_uuid=expected_uuid)


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
            locked_backup(ROOT, args.mount, dry_run=args.dry_run)
    except (OSError, RuntimeError, subprocess.CalledProcessError) as error:  # incl. AuthorityError
        print(f"Backup failed: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("Backup interrupted; any .partial directory is incomplete", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
