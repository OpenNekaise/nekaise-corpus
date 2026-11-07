"""Host-local placement for domain payloads; fail closed when the SSD is absent.

The small host binding remains in workspace/domain-storage.json. Payload locators
keep their repository-relative names so immutable inventory identities do not change.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess
import time

import ops

_checked = {}
_bindings = {}


def binding(root):
    path = Path(root) / 'workspace/domain-storage.json'
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    signature = (stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns)
    cached = _bindings.get(path)
    if cached is None or cached[0] != signature:
        cached = (signature, json.loads(path.read_text()))
        _bindings[path] = cached
    # Callers may extend a binding before writing it atomically.
    return json.loads(json.dumps(cached[1]))


def mounted_uuid(mount):
    result = subprocess.run(['findmnt', '-rn', '-M', str(mount), '-o', 'UUID'],
                            capture_output=True, text=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else ''


def validate(config, *, fresh=False):
    mount, target = Path(config['mount']), Path(config['target'])
    if not mount.is_absolute() or not target.is_relative_to(mount) or target == mount:
        raise ValueError('Invalid domain storage binding')
    key = (str(mount), config['uuid'])
    if fresh or time.monotonic() - _checked.get(key, -100) > 1:
        if not config['uuid'] or mounted_uuid(mount) != config['uuid']:
            raise RuntimeError('Domain SSD is not mounted with the configured UUID: ' + str(mount))
        _checked[key] = time.monotonic()
    if not target.resolve().is_relative_to(mount.resolve()):
        raise ValueError('Domain storage target escapes mounted volume')
    return target


def home(root):
    root = Path(root)
    logical = root / 'workspace/domains'
    config = binding(root)
    if config:
        target = validate(config)
        if not logical.is_symlink() or logical.resolve() != target.resolve():
            raise RuntimeError('Domain compatibility link does not match storage binding')
    elif logical.is_symlink():
        raise RuntimeError('External domain storage requires an explicit host binding')
    return logical


def resolve(root, relative):
    root, relative = Path(root), Path(relative)
    if relative.is_absolute() or '..' in relative.parts:
        raise ValueError('Path escapes root: ' + str(relative))
    result = (root / relative).resolve()
    if relative.parts[:2] == ('workspace', 'domains'):
        allowed = home(root).resolve()
        if result.is_relative_to(allowed):
            return result
    elif result.is_relative_to(root.resolve()):
        return result
    raise ValueError('Path escapes root: ' + str(relative))


def require_space(root, *, reserve_bytes=128 * 1024**3):
    config = binding(root)
    if config:
        validate(config, fresh=True)
    directory = home(root)
    if shutil.disk_usage(directory).free < reserve_bytes:
        raise RuntimeError('Domain growth held: less than 128 GiB reserve available')
    return directory


def migrate(root, mount, target, uuid):
    """Caller owns round/domain locks. Preserve the local recovery copy until verified."""
    root, mount, target = Path(root), Path(mount), Path(target)
    config = {'schema': 1, 'mount': str(mount), 'target': str(target), 'uuid': uuid}
    validate(config, fresh=True)
    source = root / 'workspace/domains'
    recovery = root / 'workspace/domains-before-ssd'
    if binding(root) or source.is_symlink() or recovery.exists() or target.exists():
        raise RuntimeError('Migration requires an unused destination and unbound local source')
    if shutil.disk_usage(mount).free < 160 * 1024**3:
        raise RuntimeError('Insufficient SSD reserve for migration')
    target.mkdir()
    subprocess.run(['rsync', '-a', '--', str(source) + '/', str(target) + '/'], check=True)
    # A checksum comparison covers every file, including unreferenced scratch and receipts.
    compare = subprocess.run(['rsync', '-rlnci', '--delete', '--', str(source) + '/', str(target) + '/'],
                             check=True, capture_output=True, text=True)
    if compare.stdout:
        raise RuntimeError('Migration checksum/link comparison failed: ' + compare.stdout[:2000])
    source.rename(recovery)
    try:
        source.symlink_to(target, target_is_directory=True)
        ops.atomic_write_text(root / 'workspace/domain-storage.json', json.dumps(config, indent=2) + '\n')
        validate(config, fresh=True)
    except BaseException:
        source.unlink(missing_ok=True)
        recovery.rename(source)
        (root / 'workspace/domain-storage.json').unlink(missing_ok=True)
        raise
    receipt = {'schema': 1, 'source': str(recovery), 'target': str(target), 'uuid': uuid,
               'verification': 'rsync checksum and symlink comparison; zero differences',
               'completed_at': time.time(), 'recovery_copy_retained': True}
    ops.atomic_write_text(target / 'migration.json', json.dumps(receipt, indent=2) + '\n')
    return receipt
