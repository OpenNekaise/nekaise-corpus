import json
from pathlib import Path

import pytest

import domain_collections as dc
import domain_storage as ds
from test_domain_collections import root_with_definitions, fixture_bundle


def bind(root, target, monkeypatch):
    monkeypatch.setattr(ds, 'mounted_uuid', lambda _: 'test-ssd')
    ds._checked.clear()
    config = {'mount': str(target.parent), 'target': str(target), 'uuid': 'test-ssd'}
    (root / 'workspace/domain-storage.json').write_text(json.dumps(config))
    (root / 'workspace/domains').rename(target)
    (root / 'workspace/domains').symlink_to(target)


def test_existing_and_new_snapshots_work_after_external_move(tmp_path, monkeypatch):
    root = root_with_definitions(tmp_path)
    inv = dc.import_bundle(root, fixture_bundle(tmp_path))
    snap = dc.build(root, 'modelica', [inv])
    mount = tmp_path / 'ssd'; mount.mkdir()
    bind(root, mount / 'domains', monkeypatch)
    assert dc.verify_snapshot(root, snap) == 1
    assert next((snap / 'browse').rglob('*.txt')).read_text().startswith('model Wall')
    other = dc.build(root, 'energy-physical-modeling', [inv])
    assert dc.verify_snapshot(root, other) == 1
    assert next((other / 'browse').rglob('*.txt')).read_text().startswith('model Wall')
    monkeypatch.setattr(ds, 'mounted_uuid', lambda _: '')
    ds._checked.clear()
    with pytest.raises(RuntimeError, match='not mounted'):
        dc.pin_bytes(root, b'must not write to root disk')


def test_unbound_external_link_and_nested_escape_rejected(tmp_path, monkeypatch):
    root = root_with_definitions(tmp_path)
    mount = tmp_path / 'ssd'; mount.mkdir()
    bind(root, mount / 'domains', monkeypatch)
    (mount / 'domains/escape').symlink_to(tmp_path)
    with pytest.raises(ValueError, match='escapes'):
        dc.safe(root, 'workspace/domains/escape/secret')
    with pytest.raises(ValueError, match='escapes'):
        dc.safe(root, 'workspace/domains/../../secret')
    (root / 'workspace/domain-storage.json').unlink()
    with pytest.raises(RuntimeError, match='binding'):
        dc.home(root)
