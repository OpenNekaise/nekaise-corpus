"""Domain snapshots are independent of mutable preparation and other domain views."""
import importlib.util
import json
from pathlib import Path
import shutil
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'scripts'))
import domain_collections as dc


def fixture_bundle(tmp_path, texts=None):
    bundle = tmp_path / 'bundle'; bundle.mkdir()
    data = bundle / 'dataset'; data.mkdir()
    objects = bundle / 'objects'; objects.mkdir()
    texts = texts or [('a', 'model Wall\n Real T; equation der(T)=-T;\nend Wall;\n', 'core', ['modelica'])]
    catalogs = {}
    for sid, text, role, categories in texts:
        raw = text.encode(); digest = dc.sha(raw); (objects / digest).write_bytes(raw)
        row = {'id': sid, 'title': sid, 'url': 'https://example.org/' + sid, 'source': 'fixture-library',
               'origin': 'existing_corpus', 'object_path': 'objects/' + digest, 'text_sha256': digest,
               'duplicate_key': dc.sha(dc.body_text(text).encode()), 'split': role, 'categories': categories}
        catalogs.setdefault(role, []).append(row)
    for role, values in catalogs.items():
        with (data / (role + '.manifest.jsonl')).open('w') as f:
            for row in values: dc.emit(f, row)
    report = {'readiness': 'integrity_verified_core_available', 'source_receipts': [],
              'exports': {p.name: {'sha256': dc.file_sha(p)} for p in data.iterdir()}}
    dc.write_json(bundle / 'readiness.json', report)
    return bundle


def root_with_definitions(tmp_path):
    root = tmp_path / 'corpus'; root.mkdir(); dc.home(root).mkdir(parents=True)
    target = root / 'registry' / 'domains'; target.mkdir(parents=True)
    for name in ['modelica', 'energy-physical-modeling']:
        src = Path(__file__).resolve().parents[1] / 'registry' / 'domains' / (name + '.json')
        shutil.copyfile(src, target / src.name)
    return root


def test_import_is_independent_of_original_bundle_and_shared_across_domains(tmp_path):
    root = root_with_definitions(tmp_path); bundle = fixture_bundle(tmp_path)
    inventory = dc.import_bundle(root, bundle)
    assert dc.import_bundle(root, bundle) == inventory
    shutil.rmtree(bundle)
    modelica = dc.build(root, 'modelica', [inventory])
    general = dc.build(root, 'energy-physical-modeling', [inventory])
    assert dc.verify_snapshot(root, modelica) == dc.verify_snapshot(root, general) == 1
    m, = dc.rows(modelica / 'members.jsonl'); g, = dc.rows(general / 'members.jsonl')
    assert m['text_artifact'] == g['text_artifact']
    assert list((modelica / 'browse').rglob('*.txt'))[0].read_text().startswith('model Wall')
    before = (modelica / 'snapshot.json').read_bytes()
    assert dc.build(root, 'modelica', [inventory]) == modelica
    assert (modelica / 'snapshot.json').read_bytes() == before
    assert not (root / 'manifest').exists() and not (root / 'corpus').exists()


def test_exact_duplicates_keep_provenance_and_reference_roles_separate(tmp_path):
    root = root_with_definitions(tmp_path)
    bundle = fixture_bundle(tmp_path, [('a','equation der(T)=-T;','core',['modelica']),
        ('b','equation der(T)=-T;','research_candidates',['modelica']),
        ('c','PDF formula extraction needing visual review','reference_pdf',['modelica'])])
    inventory = dc.import_bundle(root, bundle); snapshot = dc.build(root, 'modelica', [inventory])
    receipt = json.loads((snapshot / 'snapshot.json').read_text())
    assert receipt['counts'] == {'core': 1, 'reference_pdf': 1} and receipt['aliases'] == 1
    assert next(dc.rows(snapshot / 'aliases.jsonl'))['id'] == 'b'
    out = tmp_path / 'train.jsonl'; dc.export(root, snapshot, 'core', out)
    assert len(list(dc.rows(out))) == 1


def test_corrupt_input_never_publishes_inventory(tmp_path):
    root = root_with_definitions(tmp_path); bundle = fixture_bundle(tmp_path)
    next((bundle / 'objects').iterdir()).write_text('damaged')
    with pytest.raises(ValueError): dc.import_bundle(root, bundle)
    assert not (dc.home(root) / '_imports').exists()


def test_corrupt_export_is_rejected_before_import(tmp_path):
    root = root_with_definitions(tmp_path); bundle = fixture_bundle(tmp_path)
    next((bundle / 'dataset').iterdir()).write_text('{}\n')
    with pytest.raises(ValueError, match='export hash'): dc.import_bundle(root, bundle)


def test_member_file_and_payload_hashes_are_required(tmp_path):
    root = root_with_definitions(tmp_path); bundle = fixture_bundle(tmp_path)
    snapshot = dc.build(root, 'modelica', [dc.import_bundle(root, bundle)])
    member = next(dc.rows(snapshot / 'members.jsonl')); path = root / member['text_artifact']['path']
    path.chmod(0o644); path.write_text('modified')
    with pytest.raises(ValueError, match='Artifact mismatch'): dc.verify_snapshot(root, snapshot)


def test_domain_path_escape_and_unknown_definition_fail(tmp_path):
    root = root_with_definitions(tmp_path)
    with pytest.raises(ValueError, match='escapes'): dc.safe(root, '../outside')
    with pytest.raises(Exception): dc.build(root, '../../outside', [])


def test_multilingual_browse_names_fit_filesystem_and_current_is_published(tmp_path):
    root = root_with_definitions(tmp_path)
    bundle = fixture_bundle(tmp_path, [('建筑物理建模' * 40, 'model Wall end Wall;', 'core', ['modelica'])])
    snapshot = dc.build(root, 'modelica', [dc.import_bundle(root, bundle)])
    assert (dc.home(root) / 'modelica/current').resolve() == snapshot
    link, = (snapshot / 'browse').rglob('*.txt')
    assert len(link.name.encode()) < 255 and link.read_text() == 'model Wall end Wall;'
