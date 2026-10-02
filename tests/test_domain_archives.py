import io
import json
from pathlib import Path
import sys
import tarfile

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import domain_archives as da


def test_archive_inventory_recovers_encoding_and_preserves_lfs_gaps(tmp_path):
    archive = tmp_path / 'source.tar.gz'
    examples = {
        'Library/package.mo': b'package Library annotation(uses(Modelica(version="4.0.0"), Media(version="1.2"))); end Library;',
        'Library/Old.mo': 'model Old "Wärme" end Old;'.encode('cp1252'),
        'Library/Weather.mos': b'#1\ndouble tab1(2,2)\n0 10\n1 20',
        'Library/Resources/object.fmu': b'version https://git-lfs.github.com/spec/v1\noid sha256:abc\nsize 32\n',
        'Library/pointer.txt': b'version https://git-lfs.github.com/spec/v1\noid sha256:abc\nsize 32\n',
        'Library/evil.py': b'raise RuntimeError("must never execute source")',
        'tasks/GPQA.md': b'Excluded private task-bank marker',
    }
    with tarfile.open(archive, 'w:gz') as tar:
        for name, content in examples.items():
            member = tarfile.TarInfo('repo/' + name); member.size = len(content)
            tar.addfile(member, io.BytesIO(content))
    out = tmp_path / 'output'
    result = da.inspect({'repo':'owner/library','commit':'a'*40,'archive':str(archive),
        'archive_sha256':da.file_sha(archive),'categories':['modelica']},out)
    assert result['counts']['regular_files'] == len(examples)
    assert result['counts']['unresolved_lfs_pointer'] == 1
    assert result['counts']['excluded_evaluation_or_dataset_path'] == 1
    records = [json.loads(x) for x in (out/'repositories/owner__library/documents.jsonl').read_text().splitlines()]
    old = next(x for x in records if x['path'].endswith('Old.mo'))
    assert old['preparation_role'] == 'reference_history'
    assert 'Wärme' in (out/old['object_path']).read_text()
    assert old['source_sha256'] == da.sha(examples['Library/Old.mo'])
    weather = next(x for x in records if x['path'].endswith('Weather.mos'))
    assert weather['preparation_role'] == 'reference_assets'
    deps = json.loads((out/'repositories/owner__library/dependencies.jsonl').read_text().splitlines()[0])
    assert deps['dependencies'] == [{'library':'Modelica','version':'4.0.0'},{'library':'Media','version':'1.2'}]


def test_archive_hash_corruption_fails_before_output(tmp_path):
    archive=tmp_path/'bad.tar.gz'; archive.write_bytes(b'bad')
    with pytest.raises(ValueError,match='Archive hash mismatch'):
        da.inspect({'repo':'owner/library','commit':'a'*40,'archive':str(archive),'archive_sha256':'b'*64},tmp_path/'out')
    assert not (tmp_path/'out').exists()


def test_dependency_leads_handle_multiline_version_annotations():
    assert da.dependencies('uses(\n A(version="1.0"), B(version="2.0")\n)') == [
        {'library':'A','version':'1.0'},{'library':'B','version':'2.0'}]
    assert da.dependencies('uses(B(version="2.0")') == []
    assert da.dependencies('// uses(Fake(version="1"))\nannotation(Documentation(info="uses(Fiction(version=\\"2\\"))"));') == []
