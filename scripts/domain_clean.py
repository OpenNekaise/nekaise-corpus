"""Materialize verified domain text by use class and role, retaining source bytes.

The initial domain cleaning policy is faithful pass-through. Exact body duplicates
are already consolidated by domain_collections, with every alias preserved. Code,
equations, numeric tables and multilingual prose must survive unchanged.
"""
from collections import Counter
import json
import os
from pathlib import Path
import tempfile

import domain_collections as dc
import registry
import store


def publish(root, snapshot):
    dc.verify_snapshot(root, snapshot)
    with store.open(root=root).read(timeout=5) as view:
        restrictions, _ = store.pinned_policy(view)
    policy_hash = dc.sha(dc.canonical(restrictions))
    target = snapshot.parent.parent / 'clean' / (snapshot.name + '-' + policy_hash[:16])
    if target.exists():
        verify(root, target)
    else:
        work = Path(tempfile.mkdtemp(prefix='.clean-', dir=dc.home(root)))
        counts = Counter()
        with (work / 'members.jsonl').open('w') as out:
            for row in dc.rows(snapshot / 'members.jsonl'):
                # Existing eligibility decisions remain binding; unknown rights stay classified.
                use = registry.use_class(row, restrictions)
                if row.get('publisher_eligible') is False:
                    use = 'policy-held'
                role = row['split']
                ref = row['text_artifact']
                name = f'{use}/{role}/{row["duplicate_key"]}.txt'
                link = work / name; link.parent.mkdir(parents=True, exist_ok=True)
                link.symlink_to(os.path.relpath(dc.safe(root, ref['path']), (target / name).parent.resolve()))
                dc.emit(out, {'id': row['id'], 'use_class': use, 'role': role, 'view_path': name,
                              'text_artifact': ref, 'sha256': ref['sha256'],
                              'license': row.get('license', 'unverified'), 'source': row['source']})
                counts[use + '/' + role] += 1
        dc.write_json(work / 'receipt.json', {'schema': 1, 'snapshot': snapshot.name,
                      'policy': 'faithful-pass-through-v1', 'removed_chars': 0,
                      'eligibility_sha256': policy_hash,
                      'counts': counts, 'members_sha256': dc.file_sha(work / 'members.jsonl'),
                      'default_training_roles': ['open/core'],
                      'reference_roles_require_task_specific_selection': True})
        target.parent.mkdir(exist_ok=True)
        work.rename(target)
        verify(root, target)
    current = snapshot.parent.parent / 'clean-current'
    temporary = current.with_name('.clean-current-next'); temporary.unlink(missing_ok=True)
    temporary.symlink_to(os.path.relpath(target, current.parent)); temporary.replace(current)
    return target


def verify(root, target):
    receipt = json.loads((target / 'receipt.json').read_text())
    if dc.file_sha(target / 'members.jsonl') != receipt['members_sha256']:
        raise ValueError('Cleaned inventory digest mismatch')
    counts = Counter()
    for row in dc.rows(target / 'members.jsonl'):
        original = dc.verified_ref(root, row['text_artifact'])
        if (target / row['view_path']).resolve() != original or not original.is_file():
            raise ValueError('Cleaned view target mismatch')
        counts[row['use_class'] + '/' + row['role']] += 1
    if counts != receipt['counts']:
        raise ValueError('Cleaned view counts mismatch')
    return sum(counts.values())
