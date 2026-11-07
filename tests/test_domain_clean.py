import json

import domain_clean
import domain_collections as dc
from test_domain_collections import root_with_definitions,fixture_bundle
from test_store_contract import write_config


def test_clean_view_separates_use_and_role_and_keeps_bytes(tmp_path):
    root=root_with_definitions(tmp_path);write_config(root)
    (root/'registry/host_policy.json').write_text('{"version":1,"hosts":{}}')
    bundle=fixture_bundle(tmp_path,[('a','温度 21.5; equation Q=m*c*dT;','core',['modelica']),('b','Reference numeric table 25 30 2400','reference_assets',['modelica'])])
    path=bundle/'dataset/core.manifest.jsonl';row=json.loads(path.read_text());row['license']='cc-by';path.write_text(json.dumps(row)+'\n')
    report=json.loads((bundle/'readiness.json').read_text());report['exports'][path.name]['sha256']=dc.file_sha(path);dc.write_json(bundle/'readiness.json',report)
    snap=dc.build(root,'modelica',[dc.import_bundle(root,bundle)])
    clean=domain_clean.publish(root,snap)
    records=list(dc.rows(clean/'members.jsonl'))
    assert {r['use_class']+'/'+r['role'] for r in records}=={'open/core','unverified/reference_assets'}
    assert (clean/records[0]['view_path']).read_bytes()==dc.verified_ref(root,records[0]['text_artifact']).read_bytes()
    assert domain_clean.verify(root,clean)==2
    assert json.loads((clean/'receipt.json').read_text())['removed_chars']==0
