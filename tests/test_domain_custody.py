from contextlib import contextmanager
from types import SimpleNamespace

import pytest

import domain_collections as dc
import domain_custody as custody
from test_domain_collections import root_with_definitions


def test_recovered_original_survives_publisher_id_change_but_corruption_fails(tmp_path,monkeypatch):
    root=root_with_definitions(tmp_path);base=dc.home(root)
    ref=dc.pin_bytes(root,b'Original publisher PDF bytes')
    inv=base/'_imports/test';inv.mkdir(parents=True)
    dc.write_json(inv/'receipt.json',{})
    dc.write_json(inv/'documents.jsonl',{'id':'old-id','origin':'existing_corpus','publisher_original_sha256':ref['sha256'],'import_id':'test'})
    previous=base/'_custody/previous.jsonl';previous.parent.mkdir()
    dc.write_json(previous,{'id':'old-id','claimed_sha256':ref['sha256'],'status':'verified','original':ref,'publisher_row':{'id':'new-id'}})
    dc.write_json(previous.parent/'latest.json',{'inventory':str(previous.relative_to(root)),'sha256':dc.file_sha(previous)})
    @contextmanager
    def read(**kw):yield SimpleNamespace(get_manifest=lambda ids:{})
    monkeypatch.setattr(custody.store,'open',lambda **kw:SimpleNamespace(read=read))
    monkeypatch.setattr(custody.domain_storage,'require_space',lambda root:base)
    assert custody.complete(root)['counts']=={'verified':1}
    path=root/ref['path'];path.chmod(0o644);path.write_bytes(b'Corrupt publisher PDF bytes')
    with pytest.raises(ValueError,match='Artifact mismatch'):custody.complete(root)
