import io
import json
from pathlib import Path
import tarfile
from types import SimpleNamespace

import pytest

import domain_acquire as ac
import domain_archives as da
import domain_collections as dc
from test_domain_collections import root_with_definitions


def response(url, body):
    return SimpleNamespace(url=url,content=body,json=lambda:json.loads(body))


def test_repository_wave_keeps_plc_multilingual_source_and_original(tmp_path, monkeypatch):
    root=root_with_definitions(tmp_path)
    monkeypatch.setattr(ac.domain_storage,'require_space',lambda root:dc.home(root))
    text='PROGRAM Heating\n(* 温度制御 · Wärmepumpe *)\nT := 21.5;\nEND_PROGRAM\n'
    buf=io.BytesIO()
    with tarfile.open(fileobj=buf,mode='w:gz') as tar:
        member=tarfile.TarInfo('repo/main.st'); content=text.encode();member.size=len(content)
        tar.addfile(member,io.BytesIO(content))
    def get(url,**kw):
        return response(url,json.dumps({'sha':'a'*40}).encode() if 'api.github' in url else buf.getvalue())
    monkeypatch.setattr(ac.polite_http,'get',get)
    def download(url,path,max_bytes):path.write_bytes(buf.getvalue());return url
    monkeypatch.setattr(ac,'download_archive',download)
    bundle=dc.home(root)/'_acquisition/test';bundle.mkdir(parents=True)
    result=ac.acquire_repo(root,{'repo':'owner/library','categories':['plc_iec61131']},bundle,{})
    inv=dc.home(root)/'_imports'/result['inventory']
    row,=dc.rows(inv/'documents.jsonl')
    assert dc.verified_ref(root,row['text_artifact']).read_text()==text
    assert dc.verified_ref(root,row['original_archive']).read_bytes()==buf.getvalue()
    assert row['license']=='unverified' and row['split']=='core'


def test_failed_web_page_does_not_advance_queue_and_retains_original(tmp_path,monkeypatch):
    root=root_with_definitions(tmp_path)
    monkeypatch.setattr(ac.domain_storage,'require_space',lambda root:dc.home(root))
    monkeypatch.setattr(ac.polite_http,'get',lambda url,**kw:response(url,b'<html><body></body></html>'))
    directory=dc.home(root)/'_acquisition/web';directory.mkdir(parents=True)
    spec={'id':'test','url':'https://example.org/','categories':['building_energy']}
    result=ac.acquire_web(root,spec,directory,{})
    assert not result['complete'] and result['remaining_pages']==1
    assert result['failures'][0]['original']['sha256']==dc.sha(b'<html><body></body></html>')
    assert not list((dc.home(root)/'_imports').glob('*'))


def test_web_publication_is_idempotent_and_preserves_equations_tables_languages(tmp_path,monkeypatch):
    root=root_with_definitions(tmp_path)
    monkeypatch.setattr(ac.domain_storage,'require_space',lambda root:dc.home(root))
    raw='<html><main><h1>建筑能耗</h1><p>Q = m c ΔT</p><table><tr><td>C25/30</td><td>2400</td></tr></table></main></html>'.encode()
    monkeypatch.setattr(ac.polite_http,'get',lambda url,**kw:response(url,raw))
    directory=dc.home(root)/'_acquisition/web';directory.mkdir(parents=True)
    spec={'id':'test','url':'https://example.org/','categories':['building_energy']}
    result=ac.acquire_web(root,spec,directory,{})
    row,=dc.rows(dc.home(root)/'_imports'/result['inventory']/'documents.jsonl')
    text=dc.verified_ref(root,row['text_artifact']).read_text()
    assert all(x in text for x in ['建筑能耗','Q = m c ΔT','C25/30','2400'])
    assert result['complete'] and ac.acquire_web(root,spec,directory,{})['documents']==0


def test_discovery_filters_unrelated_and_excluded_data_and_keeps_cursor_on_failure(tmp_path,monkeypatch):
    root=root_with_definitions(tmp_path);state={}
    data={'items':[{'full_name':'owner/plc','description':'HVAC controls'},
                   {'full_name':'owner/unrelated','description':'cat photos'},
                   {'full_name':'owner/ModiGen','description':'Modelica evaluation bank'}]}
    monkeypatch.setattr(ac.polite_http,'get',lambda url,**kw:response(url,json.dumps(data).encode()))
    ac.discover(root,state)
    assert list(state['discovered_repositories'])==['owner/plc']
    assert state['discovery']['query']==1
    def fail(*a,**k):raise RuntimeError('HTTP 429')
    monkeypatch.setattr(ac.polite_http,'get',fail)
    with pytest.raises(RuntimeError):ac.discover(root,state)
    assert state['discovery']['query']==1


def test_streaming_archive_checks_redirect_hops_and_cleans_partial(tmp_path,monkeypatch):
    calls=[]
    monkeypatch.setattr(ac.polite_http,'check',lambda url:calls.append(url) or 0)
    monkeypatch.setattr(ac.polite_http,'pace',lambda *a:None)
    class Fake:
        def __init__(self,status,headers,parts):self.status_code=status;self.headers=headers;self.parts=parts
        def __enter__(self):return self
        def __exit__(self,*a):pass
        def close(self):pass
        def iter_content(self,n):return iter(self.parts)
    responses=iter([Fake(302,{'location':'https://codeload.github.com/archive'},[]),Fake(200,{},[b'\x1f\x8b',b'123456'])])
    monkeypatch.setattr(ac.requests,'get',lambda *a,**kw:next(responses))
    target=tmp_path/'archive.tar.gz'
    with pytest.raises(ac.polite_http.TooLarge):ac.download_archive('https://github.com/archive',target,4)
    assert calls==['https://github.com/archive','https://codeload.github.com/archive']
    assert not target.exists() and not target.with_suffix('.partial').exists()
