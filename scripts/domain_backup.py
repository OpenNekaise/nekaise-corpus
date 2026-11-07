"""Incremental second-device replica for SSD domain data, with checksum receipts."""
import json
from pathlib import Path
import shutil
import subprocess
import time

import domain_collections as dc
import domain_storage as ds


def backup(root):
    config = ds.binding(root)
    if not config or not config.get('backup'):
        raise RuntimeError('No independent domain backup destination configured')
    source = ds.home(root)
    destination = ds.validate(config['backup'], fresh=True)
    if config['backup']['uuid'] == config['uuid']:
        raise ValueError('Domain backup must use a different device')
    if shutil.disk_usage(Path(config['backup']['mount'])).free < 64 * 1024**3:
        raise RuntimeError('Backup disk has less than 64 GiB reserve')
    destination.mkdir(parents=True, exist_ok=True)
    # No deletions: published versions and overwritten operational receipts remain recoverable.
    history = destination.parent / 'replaced-metadata' / str(time.time_ns())
    subprocess.run(['rsync', '-aH', '--backup', '--backup-dir=' + str(history), '--',
                    str(source) + '/', str(destination) + '/'], check=True)
    check = subprocess.run(['rsync', '-rlnci', '--', str(source) + '/', str(destination) + '/'],
                           check=True, capture_output=True, text=True)
    if check.stdout:
        raise RuntimeError('Domain backup checksum comparison differs: ' + check.stdout[:1500])
    receipt = {'status': 'verified', 'checked_at': time.time(), 'source': str(source.resolve()),
               'destination': str(destination), 'source_uuid': config['uuid'],
               'backup_uuid': config['backup']['uuid'], 'scope': 'all domain bytes, source inventories, snapshots, custody, acquisition and legacy preparation',
               'verification': 'rsync checksum and symlink comparison; zero differences'}
    dc.write_json(destination.parent / 'latest.json', receipt)
    dc.write_json(source / 'backup-status.json', receipt)
    # Mirror the receipt written after the comparison; it is not payload.
    shutil.copyfile(source / 'backup-status.json', destination / 'backup-status.json')
    return receipt
