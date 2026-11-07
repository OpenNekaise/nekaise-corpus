#!/usr/bin/env python3
"""Bounded resumable domain collection on the configured SSD.

Pinned repository archives and public document pages are treated only as data.
Every attempt has a receipt; each successful wave imports, deduplicates, classifies,
verifies and publishes before it records completion. No translation or model calls.
"""
from __future__ import annotations

import argparse
from collections import Counter, deque
from contextlib import ExitStack
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import time
from urllib.parse import urldefrag, urljoin, urlsplit

from bs4 import BeautifulSoup
import requests

import compliance_common
import domain_archives as da
import domain_backup
import domain_clean
import domain_collections as dc
import domain_storage
import ops
import polite_http
import registry
import robots_policy
import store
import stream_guard

DISCOVERY_QUERIES = [
    'building energy simulation', 'BACnet building automation', 'PLC IEC61131',
    'building energy rating', 'Modelica HVAC', '楼宇 自动化', '建筑 能耗 模拟',
    'Gebäude Energiesimulation', 'simulation énergétique bâtiment',
    'simulación energética edificios', '建物 エネルギー', '건물 에너지',
    'моделирование энергопотребления зданий', 'simulação energética edifícios',
    'simulazione energetica edifici', 'gebouw energie simulatie',
]
DOMAIN = re.compile(r'building[ _-]+(?:energy|simulat|automation|physics|performance|control)|'
                    r'energy[ _-]+(?:model|simulat)|hvac|bacnet|modbus|opc.?ua|\bknx\b|\bplc\b|iec.?61131|modelica|energyplus|'
                    r'楼宇|建筑|能耗|gebäude|energiesimulation|bâtiment|énergétique|edificios|建物|건물', re.I)


def discover(root, state):
    """One paginated query per round; keep evidence and a durable next cursor."""
    cursor = state.setdefault('discovery', {'query': 0, 'page': 1})
    query = DISCOVERY_QUERIES[cursor['query'] % len(DISCOVERY_QUERIES)]
    response = polite_http.get('https://api.github.com/search/repositories',
        params={'q': query, 'per_page': 20, 'page': cursor['page'], 'sort': 'updated'},
        expect='json', max_bytes=4*1024**2)
    data = response.json()
    evidence = dc.pin_bytes(root, response.content)
    leads = state.setdefault('discovered_repositories', {})
    for item in data.get('items', []):
        name = item.get('full_name', '')
        text = name + ' ' + (item.get('description') or '')
        if not re.fullmatch(r'[\w.-]+/[\w.-]+', name) or not DOMAIN.search(text) or da.EXCLUDE.search(text):
            continue
        if item.get('fork') or name.split('/')[0].lower() == name.split('/')[1].lower():
            continue
        leads.setdefault(name, {'repo': name, 'ref': 'HEAD', 'categories': ['domain_discovery'],
                               'candidate_only': True,
                               'discovery_query': query, 'discovery_evidence': evidence,
                               'selection': 'Domain keyword in repository name/description; paths inventoried, exact content deduplicated'})
    # Search API only exposes the first 1000 results. Interleave languages every round.
    cursor['query'] = (cursor['query'] + 1) % len(DISCOVERY_QUERIES)
    if cursor['query'] == 0:
        cursor['page'] = cursor['page'] % 50 + 1
    return {'query': query, 'results': len(data.get('items', [])), 'known_leads': len(leads), 'evidence': evidence}


def configure_http(root, recipes):
    with store.open(root=root).read(timeout=5) as view:
        restrictions, hosts = store.pinned_policy(view)
    # Reviewed source hosts are scoped to this CLI process, not global corpus policy.
    allowed = {'api.github.com', 'codeload.github.com', 'github.com', 'raw.githubusercontent.com',
               'assets.publishing.service.gov.uk'}
    for item in recipes.get('web_and_pdf_sources', []):
        allowed.add(urlsplit(item['url']).hostname)
        allowed.update(item.get('redirect_hosts', []))
    for host in allowed:
        compliance_common.PROGRAMME_HOSTS.setdefault(host, (2.0, 24))
    polite_http.set_policy(hosts)
    robots_policy.CACHE_DIR = dc.home(root) / '_cache/robots'
    return restrictions


def eligible(spec, restrictions):
    probe = {'id': spec.get('id', 'domain-git-' + spec.get('repo', '')), 'source': spec.get('source', spec.get('repo', spec.get('id'))), 'license': 'unverified'}
    if not registry.is_collection_eligible(probe, restrictions):
        raise ValueError('Collection denied by pinned eligibility policy')


def download_archive(url, destination, max_bytes):
    """The same per-hop access checks as polite_http, with a bounded disk sink."""
    hop = polite_http.prepared(url)
    deadline = time.monotonic() + 600
    temporary = destination.with_suffix('.partial')
    try:
        for _ in range(polite_http.MAX_REDIRECTS + 1):
            delay = polite_http.check(hop)
            polite_http.pace(hop, max(2.0, delay))
            with stream_guard.Deadline(deadline, hop) as guard:
                with requests.get(hop, headers=polite_http.UA, timeout=(10, 45),
                                  stream=True, allow_redirects=False) as response:
                    guard.watch(response)
                    if response.status_code in polite_http.REDIRECTS and response.headers.get('location'):
                        hop = polite_http.prepared(urljoin(hop, response.headers['location']))
                        continue
                    if response.status_code != 200:
                        raise polite_http.Deferred(f'HTTP {response.status_code} from {hop}')
                    total, head = 0, b''
                    with temporary.open('wb') as out:
                        for part in response.iter_content(1024 * 1024):
                            total += len(part)
                            if total > max_bytes:
                                raise polite_http.TooLarge(f'{hop}: archive exceeds {max_bytes} bytes')
                            if len(head) < 2:
                                head = (head + part)[:2]
                                if len(head) == 2 and head != b'\x1f\x8b':
                                    raise ValueError('Archive is not gzip data; possible access challenge')
                            out.write(part)
                        out.flush(); os.fsync(out.fileno())
                    if head != b'\x1f\x8b':
                        raise ValueError('Empty or invalid archive')
            temporary.replace(destination)
            return hop
        raise polite_http.Deferred('Archive redirect limit exceeded')
    finally:
        temporary.unlink(missing_ok=True)


def acquire_repo(root, spec, bundle, restrictions, previous=None):
    eligible(spec, restrictions)
    repo = spec['repo']
    if not re.fullmatch(r'[\w.-]+/[\w.-]+', repo):
        raise ValueError('Invalid GitHub repository name')
    requested = spec.get('commit') or spec.get('ref', 'HEAD')
    metadata = polite_http.get(f'https://api.github.com/repos/{repo}/commits/{requested}', expect='json', max_bytes=8*1024**2)
    resolved = metadata.json()['sha']
    if not re.fullmatch('[0-9a-f]{40}', resolved):
        raise ValueError('GitHub did not return a commit identity')
    if previous and previous.get('commit') == resolved and previous.get('inventory'):
        return {**previous, 'checked_at': time.time(), 'unchanged': True}
    # A later version gets its own preparation directory and cannot overwrite its predecessor.
    bundle = bundle / resolved
    bundle.mkdir(parents=True, exist_ok=True)
    dc.write_json(bundle / 'commit-response.json', metadata.json())
    domain_storage.require_space(root)
    url = f'https://codeload.github.com/{repo}/tar.gz/{resolved}'
    archive = bundle / 'download.tar.gz'
    final_url = download_archive(url, archive, spec.get('max_archive_bytes', 256*1024**2))
    spec = {**spec, 'commit': resolved, 'requested_ref': requested, 'archive': str(archive),
            'archive_sha256': dc.file_sha(archive)}
    dc.write_json(bundle / 'acquisition.json', {'spec': spec, 'url': url, 'final_url': final_url,
                                              'fetched_at': datetime.now(timezone.utc).isoformat()})
    receipt = da.inspect(spec, bundle)
    data = bundle / 'dataset'; data.mkdir(exist_ok=True)
    records = list(dc.rows(bundle / 'repositories' / repo.replace('/', '__') / 'documents.jsonl'))
    for role in dc.ROLES:
        with (data / (role + '.manifest.jsonl')).open('w') as out:
            for row in records:
                if row['preparation_role'] == role:
                    dc.emit(out, row)
    report = {'readiness': 'integrity_verified_core_available', 'source_receipts': [receipt],
              'exports': {p.name: {'sha256': dc.file_sha(p)} for p in data.iterdir()}}
    dc.write_json(bundle / 'readiness.json', report)
    inventory = dc.import_bundle(root, bundle)
    return {'inventory': inventory.name, 'commit': resolved, 'counts': receipt['counts'], 'roles': receipt['roles']}


def extract_page(raw, url, bundle):
    if raw.startswith(b'%PDF-'):
        path = bundle / 'page.pdf'; path.write_bytes(raw)
        text_path = bundle / 'page.txt'
        subprocess.run(['pdftotext', '-layout', str(path), str(text_path)], check=True, timeout=60, capture_output=True)
        text = text_path.read_text()
        if len(text.strip()) < 80:
            raise ValueError('PDF needs OCR; original retained')
        return text, 'reference_pdf', []
    soup = BeautifulSoup(raw, 'html.parser')
    links = [urldefrag(urljoin(url, a['href']))[0] for a in soup.find_all('a', href=True)]
    from build_corpus import extract_html
    return extract_html(raw), 'core', links


def acquire_web(root, spec, directory, restrictions, page_budget=6):
    eligible(spec, restrictions)
    state_path = directory / 'crawl-state.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {'queue': [spec['url']], 'seen': [], 'success': 0}
    seen = set(state['seen']); queue = deque(state['queue']); pending = []
    prefix = spec.get('prefix') or spec['url']
    records, failures = [], []
    # Durable queue advances only after a page has a published source inventory.
    for _ in range(min(page_budget, len(queue) or 1)):
        if not queue: break
        url = queue.popleft()
        if url in seen: continue
        original = None
        try:
            domain_storage.require_space(root)
            response = polite_http.get(url, max_bytes=80*1024**2)
            original = dc.pin_bytes(root, response.content)
            text, role, links = extract_page(response.content, response.url, directory)
            if not text.strip() or '\x00' in text:
                raise ValueError('Empty or NUL-containing extraction; original retained')
            ref = dc.pin_bytes(root, text.encode(), 'text')
            key = dc.sha(dc.body_text(text).encode())
            row = {'id': 'web:' + dc.sha(url.encode()), 'source': spec['id'], 'origin': 'public_web_snapshot',
                   'title': spec.get('title', spec['id']) + ': ' + url, 'url': url, 'resolved_url': response.url,
                   'text_artifact': ref, 'original_http_body': original, 'duplicate_key': key,
                   'split': role, 'categories': spec['categories'], 'language_hint': spec.get('language_hint', 'und'),
                   'license': spec.get('license', 'unverified'), 'license_url': spec.get('license_url', ''),
                   'license_evidence': spec.get('license_evidence', 'Per-document rights review pending; original retained'),
                   'fetched_at': datetime.now(timezone.utc).isoformat(),
                   'preparation': {'method': 'pdftotext-layout' if role == 'reference_pdf' else 'existing corpus HTML extraction'}}
            records.append(row); seen.add(url); state['success'] += 1
            for link in links:
                parsed = urlsplit(link)
                if parsed.scheme == 'https' and link.startswith(prefix) and not parsed.query and not re.search(r'\.(png|jpg|svg|js|css|zip|gz|mp4|exe)(?:$|\?)', link, re.I) and link not in seen and link not in queue:
                    queue.append(link)
            if state['success'] >= spec.get('max_pages', 80):
                break
        except Exception as exc:
            failures.append({'url': url, 'reason': str(exc)[:600], 'original': original}); pending.append(url)
    inventory_id = None
    if records:
        identity = {'schema': 1, 'source': spec, 'payloads': sorted((r['url'], r['original_http_body']['sha256']) for r in records)}
        inventory_id = dc.sha(dc.canonical(identity))
        dest = dc.home(root) / '_imports' / inventory_id
        if not dest.exists():
            work = directory / ('inventory-' + inventory_id); work.mkdir(exist_ok=True)
            with (work / 'documents.jsonl').open('w') as out:
                for row in records: dc.emit(out, {**row, 'import_id': inventory_id})
            dc.write_json(work / 'receipt.json', {'schema': 1, 'id': inventory_id, 'identity': identity,
                'counts': Counter(r['split'] for r in records), 'documents_sha256': dc.file_sha(work / 'documents.jsonl'),
                'raw_gaps': [], 'evidence': {'acquisition': dc.pin_bytes(root, dc.canonical(identity))}})
            dest.parent.mkdir(parents=True, exist_ok=True)
            work.rename(dest)
        dc.verify_inventory(root, dest)
    state.update(queue=list(queue) + pending, seen=sorted(seen), failures=failures,
                 complete=not queue and not pending or state['success'] >= spec.get('max_pages', 80))
    dc.write_json(state_path, state)
    return {'inventory': inventory_id, 'documents': len(records), 'failures': failures,
            'remaining_pages': len(state['queue']), 'complete': state['complete']}


def run(root, *, repositories=6, websites=6, publish=True):
    base = domain_storage.require_space(root)
    recipe = json.loads(store.config_path('domains/sources.json', root).read_text())
    extension = json.loads(store.config_path('domains/expansion.json', root).read_text())
    specs = extension['repositories'] + recipe['repositories']
    web = extension.get('web_and_pdf_sources', []) + recipe['web_and_pdf_sources']
    restrictions = configure_http(root, {'web_and_pdf_sources': web})
    state_path = base / '_acquisition/state.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {'repositories': {}, 'websites': {}}
    declared = {s['repo'] for s in specs}
    specs += [{**s, 'candidate_only': True} for name, s in state.get('discovered_repositories', {}).items() if name not in declared]
    # Existing imported versions are already complete; do not download them again.
    for p in (base / '_imports').glob('*/receipt.json'):
        for archive in json.loads(p.read_text()).get('evidence', {}).get('archives', []):
            state['repositories'].setdefault(archive['source'], {'status': 'imported', 'commit': archive['commit'], 'inventory': p.parent.name})
    report = {'started_at': time.time(), 'repositories': [], 'websites': []}
    attempted = 0
    for spec in specs:
        previous = state['repositories'].get(spec['repo'], {})
        if previous.get('status') == 'imported':
            previous.setdefault('checked_at', time.time())
            if spec.get('commit') or time.time() - previous['checked_at'] < 7*24*3600:
                continue
        if previous.get('retry_after', 0) > time.time(): continue
        if attempted >= repositories: break
        attempted += 1
        directory = base / '_acquisition/repositories' / spec['repo'].replace('/', '__'); directory.mkdir(parents=True, exist_ok=True)
        try:
            result = acquire_repo(root, spec, directory, restrictions, previous=previous)
            result.update(status='imported', checked_at=time.time())
        except Exception as exc:
            result = {'status': 'deferred', 'reason': str(exc)[:600], 'retry_after': time.time() + 24*3600}
        state['repositories'][spec['repo']] = result
        report['repositories'].append({'repo': spec['repo'], **result})
        dc.write_json(state_path, state)
        print(json.dumps(report['repositories'][-1]), flush=True)
    attempted = 0
    for spec in sorted(web, key=lambda s: state['websites'].get(s['id'], {}).get('attempted_at', 0)):
        previous = state['websites'].get(spec['id'], {})
        if previous.get('complete') or previous.get('retry_after', 0) > time.time(): continue
        if attempted >= websites: break
        attempted += 1
        directory = base / '_acquisition/web' / spec['id']; directory.mkdir(parents=True, exist_ok=True)
        try:
            result = acquire_web(root, spec, directory, restrictions)
            if result['failures']: result['retry_after'] = time.time() + 24*3600
        except Exception as exc:
            result = {'complete': False, 'reason': str(exc)[:600], 'retry_after': time.time() + 24*3600}
        result['attempted_at'] = time.time()
        state['websites'][spec['id']] = result
        report['websites'].append({'source': spec['id'], **result})
        dc.write_json(state_path, state)
        print(json.dumps(report['websites'][-1]), flush=True)
    try:
        report['discovery'] = discover(root, state)
    except Exception as exc:
        report['discovery'] = {'deferred': str(exc)[:600]}
    dc.write_json(state_path, state)
    if publish:
        inventories = sorted(p.parent for p in (base / '_imports').glob('*/receipt.json'))
        report['published'] = {}
        for domain in ['modelica', 'energy-physical-modeling', 'building-controls']:
            snapshot = dc.build(root, domain, inventories)
            cleaned = domain_clean.publish(root, snapshot)
            report['published'][domain] = {'snapshot': snapshot.name, 'counts': json.loads((snapshot / 'snapshot.json').read_text())['counts'],
                                          'clean': str(cleaned.relative_to(root))}
    report.update(completed_at=time.time(), known_repositories=len(specs), known_websites=len(web))
    dc.write_json(base / '_acquisition/latest.json', report)
    with (base / '_acquisition/history.jsonl').open('a') as out: dc.emit(out, report)
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--repositories', type=int, default=6)
    parser.add_argument('--websites', type=int, default=6)
    args = parser.parse_args(); root = args.root.resolve()
    try:
        domain_storage.require_space(root)
        with ExitStack() as stack:
            for name in ['continuous-dig', 'corpus-round', 'domain-collections', 'domain-token-accounting']:
                stack.enter_context(ops.named_lock(name, workspace=root / 'workspace'))
            log = dc.home(root) / '_acquisition/cron.log'; log.parent.mkdir(exist_ok=True)
            with log.open('a', buffering=1) as out:
                from contextlib import redirect_stdout, redirect_stderr
                with redirect_stdout(out), redirect_stderr(out):
                    status = dc.home(root) / '_acquisition/job-status.json'
                    dc.write_json(status, {'status': 'running', 'started_at': time.time(), 'pid': os.getpid()})
                    try:
                        report = run(root, repositories=args.repositories, websites=args.websites)
                        report['backup'] = domain_backup.backup(root)
                        dc.write_json(status, {'status': 'complete', 'completed_at': time.time(), 'backup': report['backup']})
                        print(json.dumps(report), flush=True)
                    except BaseException as exc:
                        dc.write_json(status, {'status': 'failed', 'failed_at': time.time(), 'reason': str(exc)[:1000]})
                        raise
    except RuntimeError as exc:
        print(str(exc))
        raise SystemExit(75)


if __name__ == '__main__':
    main()
