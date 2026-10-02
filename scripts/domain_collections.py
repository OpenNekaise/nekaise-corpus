#!/usr/bin/env python3
"""Publish isolated, immutable domain collections from verified preparation bundles.

This is a corpus-owned raw-source publication layer, NOT an import into the global
default corpus view. It never changes that view, cleaning rules, use classes or
training state. Canonical payload versions shared by all domains live under
workspace/domains/_store/artifacts; the separate root avoids global artifact-GC
misclassification. Inventories/snapshots are reference roots and are never deleted.
"""
from __future__ import annotations

import argparse
import base64
from collections import Counter, defaultdict
import hashlib
from itertools import chain
import json
import os
from pathlib import Path
import re
import shutil
import tempfile

from artifact_store import LocalArtifacts
import ops
import store

ROLES = ('core', 'research_candidates', 'patent_supplement', 'reference_assets', 'reference_history', 'reference_pdf')


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':')).encode()


def sha(data):
    return hashlib.sha256(data).hexdigest()


def file_sha(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for part in iter(lambda: f.read(1024 * 1024), b''):
            h.update(part)
    return h.hexdigest()


def safe(root, relative):
    result = (root / relative).resolve()
    if not result.is_relative_to(root.resolve()):
        raise ValueError('Path escapes root: ' + str(relative))
    return result


def body_text(text):
    if text.startswith('# ') and '\nsource: ' in text[:2000] and '\n---\n' in text[:4000]:
        return text.split('\n---\n', 1)[1].strip()
    return text.strip()


def rows(path):
    with Path(path).open() as f:
        for line in f:
            yield json.loads(line)


def emit(f, value):
    f.write(canonical(value).decode() + '\n')


def write_json(path, value):
    ops.atomic_write_bytes(path, canonical(value) + b'\n')


def home(root):
    return root / 'workspace' / 'domains'


def pool(root):
    return LocalArtifacts(home(root) / '_store')


def artifact_ref(root, artifact):
    return {'stage': artifact.stage, 'sha256': artifact.sha256, 'bytes': artifact.size,
            'path': str((home(root) / '_store' / artifact.locator).relative_to(root))}


def pin_file(root, source, expected=None, stage='raw'):
    if expected and file_sha(source) != expected:
        raise ValueError('Source hash mismatch: ' + str(source))
    a = pool(root).put_file(stage, source)
    if expected and a.sha256 != expected:
        raise ValueError('Source changed while copying: ' + str(source))
    return artifact_ref(root, a)


def pin_bytes(root, data, stage='raw'):
    return artifact_ref(root, pool(root).put_bytes(stage, data))


def verified_ref(root, ref):
    path = safe(root, ref['path'])
    if file_sha(path) != ref['sha256'] or path.stat().st_size != ref['bytes']:
        raise ValueError('Artifact mismatch: ' + ref['path'])
    return path


def studio_artifact(bundle, key):
    if not re.fullmatch('[0-9a-f]{64}', key):
        raise ValueError('Invalid source artifact key')
    raw = safe(bundle, f'web-acquisition/artifacts/{key[:2]}/{key}.json').read_bytes()
    if sha(raw) != key:
        raise ValueError('Source evidence artifact mismatch')
    return json.loads(raw), raw


def import_bundle(root, bundle):
    """Verify all inputs before publishing an inventory; failed imports stay unpublished."""
    raw_report = (bundle / 'readiness.json').read_bytes()
    report = json.loads(raw_report)
    if report.get('readiness') != 'integrity_verified_core_available':
        raise ValueError('Bundle is not verified ready')
    inventory_id = sha(raw_report)
    dest = home(root) / '_imports' / inventory_id
    if (dest / 'receipt.json').exists():
        verify_inventory(root, dest)
        return dest
    for name, evidence in report['exports'].items():
        if file_sha(safe(bundle, 'dataset/' + name)) != evidence['sha256']:
            raise ValueError('Bundle export hash mismatch: ' + name)
    pending = Path(tempfile.mkdtemp(prefix='.import-', dir=home(root)))
    archives, archive_receipts = {}, []
    for receipt in report.get('source_receipts', []):
        if receipt.get('archive_sha256') and receipt.get('commit'):
            repo = receipt['repo']
            archive = safe(bundle, 'repositories/' + repo.replace('/', '__') + '/' + receipt['commit'] + '.tar.gz')
            ref = pin_file(root, archive, receipt['archive_sha256'])
            archives[(repo, receipt['commit'])] = ref
            archive_receipts.append({'source': repo, 'commit': receipt['commit'], 'archive': ref})
    counts, raw_gaps = Counter(), []
    with (pending / 'documents.jsonl').open('w') as target:
        for role in ROLES:
            name = role + '.manifest.jsonl'
            if name not in report['exports']:
                continue
            for row in rows(bundle / 'dataset' / name):
                source = safe(bundle, row['object_path'])
                content = source.read_bytes()
                text = content.decode('utf-8-sig')
                if not text.strip() or '\x00' in text or sha(body_text(text).encode()) != row['duplicate_key']:
                    raise ValueError('Invalid prepared body: ' + row['id'])
                text_ref = pin_file(root, source, row['text_sha256'], 'text')
                entry = {k: v for k, v in row.items() if k not in
                         {'object_path', 'input_catalog', 'original_pdf', 'raw_response_artifact', 'robots_artifact'}}
                entry.update(split=role, text_artifact=text_ref, import_id=inventory_id,
                             preparation={'method': row.get('representation'), 'input_manifest_sha256': report['exports'][name]['sha256']})
                if (row.get('source'), row.get('commit')) in archives:
                    entry['original_archive'] = archives[(row['source'], row['commit'])]
                if row.get('original_pdf'):
                    entry['original_pdf_artifact'] = pin_file(root, safe(bundle, row['original_pdf']), row.get('source_sha256'))
                if row.get('raw_response_artifact'):
                    response, response_bytes = studio_artifact(bundle, row['raw_response_artifact'])
                    original = base64.b64decode(response['body_base64'], validate=True)
                    if sha(original) != row['source_sha256']:
                        raise ValueError('Original HTTP body mismatch')
                    entry['original_http_body'] = pin_bytes(root, original)
                    entry['http_receipt'] = pin_bytes(root, canonical({k: v for k, v in response.items() if k != 'body_base64'}))
                    if row.get('robots_artifact'):
                        _, robots = studio_artifact(bundle, row['robots_artifact'])
                        entry['robots_receipt'] = pin_bytes(root, robots)
                if row['origin'] == 'existing_corpus':
                    # Prepared text is an immutable version, independent of future recleaning.
                    # The original publisher claim remains explicit; do not fabricate raw bytes.
                    entry['publisher_original_sha256'] = row.get('sha256')
                    entry['publisher_manifest'] = row.get('manifest')
                    entry['raw_provenance_status'] = 'publisher_claim_preserved_in_pinned_source_manifest'
                emit(target, entry)
                counts[role] += 1
    evidence = {'readiness': pin_bytes(root, raw_report), 'archives': archive_receipts}
    snapshots = bundle / 'source-manifests'
    if snapshots.exists():
        evidence['publisher_manifests'] = [pin_file(root, p) for p in sorted(snapshots.iterdir()) if p.is_file()]
    for name in ('quality-review.json', 'web-summary.json', 'manifest-snapshot.json', 'publisher-policy.json'):
        if (bundle / name).is_file():
            evidence[name] = pin_file(root, bundle / name)
    receipt = {'schema': 1, 'id': inventory_id, 'counts': counts, 'evidence': evidence,
               'documents_sha256': file_sha(pending / 'documents.jsonl'), 'raw_gaps': raw_gaps,
               'publication': 'local domain source inventory; not global default corpus admission'}
    write_json(pending / 'receipt.json', receipt)
    dest.parent.mkdir(parents=True, exist_ok=True)
    pending.rename(dest)
    verify_inventory(root, dest)
    return dest


def refs(value):
    if isinstance(value, dict):
        if set(('stage', 'sha256', 'bytes', 'path')) <= value.keys():
            yield value
        else:
            for v in value.values():
                yield from refs(v)
    elif isinstance(value, list):
        for v in value:
            yield from refs(v)


def verify_inventory(root, directory):
    receipt = json.loads((directory / 'receipt.json').read_text())
    if file_sha(directory / 'documents.jsonl') != receipt['documents_sha256']:
        raise ValueError('Inventory digest mismatch')
    seen = set()
    for value in chain([receipt], rows(directory / 'documents.jsonl')):
        for ref in refs(value):
            if (ref['stage'], ref['sha256']) not in seen:
                verified_ref(root, ref)
                seen.add((ref['stage'], ref['sha256']))
    return len(seen)


def selected(row, definition):
    if row['split'] not in definition['roles']:
        return False
    rule = definition['selector']
    return (rule.get('all_declared_domain_sources', False)
            or bool(set(row.get('categories', [])) & set(rule.get('categories', [])))
            or any(row.get('path', '').lower().endswith(x) for x in rule.get('extensions', []))
            or bool(rule.get('source_pattern') and re.search(rule['source_pattern'], row.get('source', ''), re.I))
            or bool(rule.get('title_pattern') and re.search(rule['title_pattern'], row.get('title', ''))))


def build(root, domain, inventories, definition_path=None):
    if not re.fullmatch(r'[a-z][a-z0-9-]{0,63}', domain):
        raise ValueError('Invalid domain name')
    definition = json.loads((definition_path or store.config_path('domains/' + domain + '.json', root)).read_text())
    if definition['id'] != domain:
        raise ValueError('Domain ID mismatch')
    if len({p.resolve() for p in inventories}) != len(inventories):
        raise ValueError('Repeated inventory input')
    receipts = []
    for inventory in inventories:
        verify_inventory(root, inventory)
        r = json.loads((inventory / 'receipt.json').read_text())
        receipts.append({'id': r['id'], 'documents_sha256': r['documents_sha256']})
    identity = {'schema': 1, 'definition': definition, 'inventories': sorted(receipts, key=lambda x: x['id']),
                'deduplication': 'exact prepared body hash, core preferred; aliases retained', 'builder': 'domain_collections_v1'}
    snapshot_id = sha(canonical(identity))
    target = home(root) / domain / 'snapshots' / snapshot_id
    if target.exists():
        verify_snapshot(root, target)
    else:
        work = Path(tempfile.mkdtemp(prefix='.build-', dir=home(root)))
        chosen, aliases = {}, []
        rank = {role: i for i, role in enumerate(ROLES)}
        for inventory in sorted(inventories, key=lambda p: p.name):
            for row in rows(inventory / 'documents.jsonl'):
                if not selected(row, definition):
                    continue
                key = row['duplicate_key']
                if key not in chosen:
                    chosen[key] = row
                elif (rank[row['split']], row['id']) < (rank[chosen[key]['split']], chosen[key]['id']):
                    aliases.append(chosen[key]); chosen[key] = row
                else:
                    aliases.append(row)
        counts, sources = Counter(), Counter()
        with (work / 'members.jsonl').open('w') as members:
            for key, row in sorted(chosen.items()):
                emit(members, row)
                counts[row['split']] += 1
                sources[row['source']] += 1
                # A generated view, with a stable name even when titles contain hostile paths.
                folder = re.sub(r'[^A-Za-z0-9_.-]', '_', row['source'])[:90]
                ext = Path(row.get('path', '')).suffix
                if not re.fullmatch(r'\.[A-Za-z0-9]{1,8}', ext): ext = '.txt'
                label = re.sub(r'[^\w.-]', '_', row.get('path') or row.get('title') or row['id'])[-100:]
                label = label.encode('utf-8')[-120:].decode('utf-8', errors='ignore')
                link = work / 'browse' / row['split'] / folder / (key + '--' + label + ext)
                link.parent.mkdir(parents=True, exist_ok=True)
                final_link = target / link.relative_to(work)
                link.symlink_to(os.path.relpath(safe(root, row['text_artifact']['path']), final_link.parent))
        with (work / 'aliases.jsonl').open('w') as f:
            for row in sorted(aliases, key=lambda r: (r['duplicate_key'], r['id'], r['import_id'])):
                emit(f, row)
        write_json(work / 'definition.json', definition)
        receipt = {**identity, 'id': snapshot_id, 'counts': counts, 'sources': sources,
                   'aliases': len(aliases), 'members_sha256': file_sha(work / 'members.jsonl'),
                   'aliases_sha256': file_sha(work / 'aliases.jsonl'), 'training_started': False,
                   'meaning': 'Prepared domain source text; reference roles are separate, no claim of simulation validation or knowledge completeness.'}
        write_json(work / 'snapshot.json', receipt)
        target.parent.mkdir(parents=True, exist_ok=True)
        work.rename(target)
        verify_snapshot(root, target)
    write_json(home(root) / domain / 'latest.json', {'id': snapshot_id, 'path': str(target.relative_to(root)),
                                                   'snapshot_sha256': file_sha(target / 'snapshot.json')})
    # Convenience links are rebuildable; latest.json is the publication pointer.
    current = home(root) / domain / 'current'
    temporary = current.with_name('.current-next')
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(os.path.relpath(target, current.parent))
    temporary.replace(current)
    return target


def verify_snapshot(root, target):
    report = json.loads((target / 'snapshot.json').read_text())
    identity = {k: report[k] for k in ('schema', 'definition', 'inventories', 'deduplication', 'builder')}
    if sha(canonical(identity)) != report['id'] or target.name != report['id']:
        raise ValueError('Snapshot identity mismatch')
    if file_sha(target / 'members.jsonl') != report['members_sha256'] or file_sha(target / 'aliases.jsonl') != report['aliases_sha256']:
        raise ValueError('Snapshot member/alias digest mismatch')
    count = Counter()
    for row in rows(target / 'members.jsonl'):
        verified_ref(root, row['text_artifact'])
        count[row['split']] += 1
    if dict(count) != report['counts']:
        raise ValueError('Snapshot counts mismatch')
    return sum(count.values())


def export(root, snapshot, role, destination):
    verify_snapshot(root, snapshot)
    with destination.open('x') as out:
        for row in rows(snapshot / 'members.jsonl'):
            if row['split'] == role:
                text = verified_ref(root, row['text_artifact']).read_bytes().decode('utf-8-sig')
                emit(out, {**row, 'text': text})


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    commands = parser.add_subparsers(dest='command', required=True)
    imp = commands.add_parser('import-bundle'); imp.add_argument('bundle', type=Path)
    make = commands.add_parser('build'); make.add_argument('domain'); make.add_argument('inventories', nargs='+', type=Path); make.add_argument('--definition', type=Path)
    check = commands.add_parser('verify'); check.add_argument('snapshot', type=Path)
    exp = commands.add_parser('export'); exp.add_argument('snapshot', type=Path); exp.add_argument('--role', choices=ROLES, default='core'); exp.add_argument('--output', type=Path, required=True)
    args = parser.parse_args(); root = args.root.resolve(); home(root).mkdir(parents=True, exist_ok=True)
    with ops.named_lock('domain-collections', timeout=0, workspace=root / 'workspace'):
        if args.command == 'import-bundle': print(import_bundle(root, args.bundle.resolve()), flush=True)
        elif args.command == 'build': print(build(root, args.domain, [p.resolve() for p in args.inventories], args.definition), flush=True)
        elif args.command == 'verify': print(verify_snapshot(root, args.snapshot.resolve()), flush=True)
        else: export(root, args.snapshot.resolve(), args.role, args.output)


if __name__ == '__main__':
    main()
