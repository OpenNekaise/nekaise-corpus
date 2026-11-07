import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import domain_backup as backup
import domain_storage as ds
from test_domain_collections import root_with_definitions
from test_domain_storage import bind


def test_replica_preserves_overwritten_metadata_and_rejects_same_device(tmp_path,monkeypatch):
    root=root_with_definitions(tmp_path)
    mount=tmp_path/'ssd';mount.mkdir();target=mount/'domains'
    bind(root,target,monkeypatch)
    second=tmp_path/'second';second.mkdir()
    monkeypatch.setattr(ds,'mounted_uuid',lambda p:'test-ssd' if Path(p)==mount else 'backup-uuid')
    monkeypatch.setattr(backup.shutil,'disk_usage',lambda p:SimpleNamespace(free=10**12))
    config=ds.binding(root);config['backup']={'mount':str(second),'target':str(second/'replica'),'uuid':'backup-uuid'}
    path=root/'workspace/domain-storage.json';path.write_text(json.dumps(config))
    (target/'immutable').write_bytes(b'original source')
    (target/'current.json').write_text('old')
    first=backup.backup(root)
    assert first['status']=='verified'
    (target/'current.json').write_text('new metadata')
    backup.backup(root)
    assert (second/'replica/immutable').read_bytes()==b'original source'
    assert any(p.read_text()=='old' for p in (second/'replaced-metadata').rglob('current.json'))
    config['backup']['uuid']='test-ssd';path.write_text(json.dumps(config))
    monkeypatch.setattr(ds,'mounted_uuid',lambda p:'test-ssd');ds._checked.clear()
    with pytest.raises(ValueError,match='different device'):backup.backup(root)
