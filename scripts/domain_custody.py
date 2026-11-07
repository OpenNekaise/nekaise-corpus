"""Pin publisher originals used by domain inventories without altering frozen records."""
from collections import Counter
import json
from pathlib import Path
import time

import domain_collections as dc
import domain_storage
import store


def complete(root):
    base = domain_storage.require_space(root)
    claims = {}
    for inventory in sorted((base / '_imports').iterdir()):
        if not (inventory / 'receipt.json').exists():
            continue
        for row in dc.rows(inventory / 'documents.jsonl'):
            if row.get('origin') == 'existing_corpus':
                claims[(row['id'], row.get('publisher_original_sha256'))] = row
    with store.open(root=root).read(timeout=5) as view:
        publishers = view.get_manifest(sid for sid, digest in claims)
    directory = base / '_custody'; directory.mkdir(exist_ok=True)
    previous = {}
    if (directory / 'latest.json').exists():
        receipt = json.loads((directory / 'latest.json').read_text())
        old = dc.safe(root, receipt['inventory'])
        if dc.file_sha(old) != receipt['sha256']:
            raise ValueError('Previous custody inventory digest mismatch')
        previous = {(r['id'], r['claimed_sha256']): r for r in dc.rows(old) if r['status'] == 'verified'}
    pending = directory / '.publisher-originals.pending.jsonl'
    counts, total = Counter(), 0
    with pending.open('w') as out:
        for (sid, digest), claim in sorted(claims.items()):
            entry = {'id': sid, 'claimed_sha256': digest, 'import_id': claim['import_id']}
            if (sid, digest) in previous:
                old = previous[(sid, digest)]
                if old['original']['sha256'] != digest:
                    raise ValueError('Previous custody original does not match historical claim')
                dc.verified_ref(root, old['original'])
                dc.emit(out, old); counts['verified'] += 1; total += old['original']['bytes']
                continue
            publisher = publishers.get(sid, {})
            path = publisher.get('raw_path')
            if not digest or not path:
                entry.update(status='unresolved', reason='publisher original not located in current manifest')
            else:
                try:
                    original = dc.safe(root, path)
                    if not original.is_file():
                        raise ValueError('publisher original missing')
                    entry.update(status='verified', original=dc.pin_file(root, original, digest),
                                 publisher_row=publisher)
                    total += entry['original']['bytes']
                except (ValueError, OSError) as exc:
                    entry.update(status='unresolved', reason=str(exc))
            counts[entry['status']] += 1
            dc.emit(out, entry)
    digest = dc.file_sha(pending)
    target = directory / (digest + '.jsonl'); pending.replace(target)
    report = {'schema': 1, 'counts': counts, 'bytes': total, 'completed_at': time.time(),
              'inventory': str(target.relative_to(root)), 'sha256': digest,
              'purpose': 'Independent domain custody of publisher originals; historical inventories unchanged'}
    dc.write_json(directory / 'latest.json', report)
    return report
