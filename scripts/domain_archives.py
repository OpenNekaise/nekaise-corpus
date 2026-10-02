#!/usr/bin/env python3
"""Inspect pinned public repository archives as data, recovering domain text.

Produces preparation catalogs with every regular member accounted for, dependency
leads and explicit omissions. Never imports/builds/executes downloaded source code.
No network: archive acquisition and the declared public source inventory are separate.
"""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import tempfile

from domain_collections import body_text, emit, file_sha, sha, write_json

TEXT = {'.mo', '.mos', '.py', '.cpp', '.cc', '.c', '.h', '.hh', '.hpp', '.f90', '.f', '.idf', '.idd',
        '.cal', '.rad', '.order', '.md', '.rst', '.tex', '.adoc', '.1', '.html', '.htm', '.txt', '.xml', '.xsd', '.json', '.csv', '.yaml', '.yml'}
EXCLUDE = re.compile(r'gpqa|mmlu|nemotron.cc|fineweb|dclm|dolma|modigen|modbench|xmufst|nekaise.bench', re.I)
OMIT = {'.git', '.github', 'node_modules', 'third_party', 'third-party', 'vendor', 'vendors', '_build', 'build', 'dist'}


def pin(out, raw, extension='.txt'):
    digest = sha(raw); path = out / 'objects' / digest[:2] / (digest + extension)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if file_sha(path) != digest: raise ValueError('Existing object corrupt')
    else:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as f:
            f.write(raw); temporary = Path(f.name)
        temporary.replace(path)
    return digest, str(path.relative_to(out))


def dependencies(text):
    """Bounded lexical leads, not a Modelica compiler or version compatibility check."""
    # Do not interpret tutorial strings or commented-out annotations as dependencies.
    lexer = re.compile(r'(?P<comment>//[^\n]*|/\*.*?\*/)|(?P<string>"(?:\\.|[^"\\])*")|(?P<id>[A-Za-z_]\w*)|(?P<symbol>\S)', re.S)
    tokens = [(m.lastgroup, m.group()) for m in lexer.finditer(text) if m.lastgroup != 'comment']
    found = []
    for index in range(len(tokens) - 1):
        if tokens[index] != ('id', 'uses') or tokens[index + 1][1] != '(': continue
        depth = 1; end = index + 2
        for end in range(index + 2, min(len(tokens), index + 10000)):
            if tokens[end] == ('symbol', '('): depth += 1
            elif tokens[end] == ('symbol', ')'):
                depth -= 1
                if not depth: break
        if depth: continue
        for pos in range(index + 2, end - 4):
            chunk = tokens[pos:pos + 5]
            if chunk[0][0] == 'id' and [t[1] for t in chunk[1:4]] == ['(', 'version', '='] and chunk[4][0] == 'string':
                found.append({'library': chunk[0][1], 'version': chunk[4][1][1:-1]})
    return found


def role_for(path, text, encoding):
    lower = '/' + path.lower()
    if path.lower().endswith('.pdf'): return 'reference_pdf'
    if encoding != 'utf-8-sig': return 'reference_history'
    if any(x in lower for x in ['/referenceresults/', '/obsolete/', '/idd/versions/', '/weatherdata/']): return 'reference_assets'
    if lower.endswith(('.csv', '.order')) or 'precipitationschedules' in lower: return 'reference_assets'
    if lower.endswith(('.json', '.yaml', '.yml')) and not any(x in lower for x in ['schema', 'specification']): return 'reference_assets'
    if text.startswith('#1') and re.search(r'\bdouble\s+\w+\(', text[:1000]): return 'reference_assets'
    if len(text) > 100000 and sum(c.isdigit() for c in text) / len(text) > 0.45: return 'reference_assets'
    return 'core'


def inspect(spec, out):
    repo, commit = spec['repo'], spec['commit']
    if not re.fullmatch(r'[\w.-]+/[\w.-]+', repo) or not re.fullmatch('[a-f0-9]{40}', commit):
        raise ValueError('Invalid source identity')
    source = Path(spec['archive']).resolve()
    if file_sha(source) != spec['archive_sha256']: raise ValueError('Archive hash mismatch')
    directory = out / 'repositories' / repo.replace('/', '__'); directory.mkdir(parents=True, exist_ok=True)
    archive = directory / (commit + '.tar.gz')
    if not archive.exists():
        try: os.link(source, archive)
        except OSError: shutil.copyfile(source, archive)
    counts, role_counts = Counter(), Counter()
    receipt = {'repo': repo, 'source': repo, 'commit': commit, 'archive_sha256': spec['archive_sha256'],
               'archive_bytes': source.stat().st_size, 'requested_ref': spec.get('requested_ref', 'HEAD'),
               'status': 'building', 'training_started': False}
    write_json(directory / 'receipt.json', receipt)
    with tarfile.open(archive, 'r:gz') as tar, (directory / 'documents.jsonl').open('w') as docs, \
            (directory / 'file-inventory.jsonl').open('w') as files, (directory / 'dependencies.jsonl').open('w') as deps:
        for member in tar:
            if not member.isfile():
                if member.issym() or member.islnk(): emit(files, {'path': member.name, 'status': 'archive_link_not_followed'})
                continue
            path = '/'.join(PurePosixPath(member.name).parts[1:]); counts['regular_files'] += 1
            entry = {'path': path, 'bytes': member.size}
            reason = None
            if not path or PurePosixPath(member.name).is_absolute() or '..' in PurePosixPath(member.name).parts: reason = 'unsafe_path'
            elif EXCLUDE.search(path): reason = 'excluded_evaluation_or_dataset_path'
            elif any(x in PurePosixPath(path).parts for x in OMIT): reason = 'dependency_or_build_tree_separate_scope'
            elif spec.get('prefixes') and not any(path.startswith(p) for p in spec['prefixes']): reason = 'outside_declared_prefixes'
            elif member.size > (80_000_000 if path.lower().endswith('.pdf') else 20_000_000): reason = 'oversized_member_retained_in_archive'
            elif PurePosixPath(path).suffix.lower() not in TEXT | {'.pdf'} and not PurePosixPath(path).name.lower().startswith(('readme', 'license', 'copying', '.gitmodules')) and not path.lower().endswith('.idd.in'): reason = 'nontext_or_unsupported_type_retained_in_archive'
            if reason:
                counts[reason] += 1; emit(files, {**entry, 'status': reason}); continue
            raw = tar.extractfile(member).read(); raw_hash = sha(raw)
            if raw.startswith(b'version https://git-lfs.github.com/spec/v1'):
                counts['unresolved_lfs_pointer'] += 1
                emit(files, {**entry, 'status': 'unresolved_lfs_pointer', 'pointer': raw.decode('ascii'), 'raw_sha256': raw_hash}); continue
            pdf_ref = None; encoding = 'utf-8-sig'
            try:
                if path.lower().endswith('.pdf'):
                    _, pdf_ref = pin(out, raw, '.pdf')
                    with tempfile.NamedTemporaryFile(suffix='.txt') as f:
                        subprocess.run(['pdftotext', '-layout', str(out / pdf_ref), f.name], check=True, timeout=45, capture_output=True)
                        text = Path(f.name).read_text()
                else:
                    try: text = raw.decode('utf-8-sig')
                    except UnicodeDecodeError:
                        encoding = 'cp1252-hypothesis'
                        text = raw.decode('cp1252')
                if not text.strip() or '\x00' in text: raise ValueError('Empty or NUL-containing text')
                if len(text.strip()) < 80 and pdf_ref: raise ValueError('PDF needs image/OCR review')
            except (ValueError, OSError, subprocess.SubprocessError) as exc:
                counts['extraction_unresolved'] += 1
                emit(files, {**entry, 'status': 'extraction_unresolved', 'raw_sha256': raw_hash, 'reason': str(exc)[:300]}); continue
            # Preserve original UTF-8 bytes; legacy encodings retain raw bytes in archive.
            prepared = raw if not pdf_ref and encoding == 'utf-8-sig' else text.encode()
            digest, object_path = pin(out, prepared)
            role = role_for(path, text, encoding)
            row = {'id': f'git:{repo}@{commit}:{path}', 'title': repo + ': ' + path,
                   'url': 'https://github.com/' + repo + '/blob/' + commit + '/' + path,
                   'source': repo, 'origin': 'new_repository_snapshot', 'tier': 'core',
                   'path': path, 'commit': commit, 'text_sha256': digest, 'duplicate_key': sha(body_text(text).encode()),
                   'object_path': object_path, 'chars': len(text), 'bytes': len(prepared),
                   'categories': spec['categories'], 'preparation_role': role,
                   'source_sha256': raw_hash, 'encoding': encoding,
                   'representation': 'repository_pdf_layout' if pdf_ref else 'verbatim_repository_file' if encoding == 'utf-8-sig' else 'decoded_legacy_source_requires_review',
                   'license': 'source declaration retained in original archive; no license admission gate'}
            if pdf_ref: row['original_pdf'] = pdf_ref
            if spec.get('canonical_git_url'):
                row['canonical_git_url'] = spec['canonical_git_url']
                row['url'] = spec['canonical_git_url'] + '#' + commit + ':' + path
            emit(docs, row); counts['documents'] += 1; role_counts[role] += 1
            emit(files, {**entry, 'status': 'prepared', 'role': role, 'raw_sha256': raw_hash, 'text_sha256': digest, 'encoding': encoding})
            if path.lower().endswith('.mo'):
                values = dependencies(text)
                if values: emit(deps, {'source': repo, 'path': path, 'dependencies': values, 'method': 'lexical uses annotation leads; compatibility unverified'})
            if PurePosixPath(path).name == '.gitmodules':
                emit(deps, {'source': repo, 'path': path, 'submodules': text, 'method': 'declared submodule metadata; no recursive execution'})
    receipt.update(status='complete' if counts['documents'] else 'empty', counts=counts, roles=role_counts,
                   file_inventory_sha256=file_sha(directory / 'file-inventory.jsonl'), dependencies_sha256=file_sha(directory / 'dependencies.jsonl'))
    write_json(directory / 'receipt.json', receipt)
    print(json.dumps({'repo': repo, 'status': receipt['status'], 'counts': counts, 'roles': role_counts}), flush=True)
    return receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--plan', type=Path, required=True); parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args(); specs = json.loads(args.plan.read_text())['repositories']
    args.out.mkdir(parents=True, exist_ok=True)
    with ThreadPoolExecutor(max_workers=2) as workers:
        receipts = list(workers.map(lambda spec: inspect(spec, args.out.resolve()), specs))
    write_json(args.out / 'repository-summary.json', receipts)
