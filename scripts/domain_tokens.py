#!/usr/bin/env python3
"""Exact CPU-only token accounting for a frozen domain snapshot; no model weights."""
import argparse
from collections import Counter, defaultdict
import json
import os
from pathlib import Path

from domain_collections import ROLES, file_sha, home, pin_file, rows, verified_ref, write_json


def count(root, snapshot, tokenizer_path, roles=('core', 'reference_pdf')):
    if not roles or not set(roles) <= set(ROLES):
        raise ValueError('Choose known, nonempty accounting roles')
    os.environ.setdefault('RAYON_NUM_THREADS', '4')
    from tokenizers import Tokenizer
    tokenizer_hash = file_sha(tokenizer_path)
    tokenizer_ref = pin_file(root, tokenizer_path, tokenizer_hash)
    tokenizer = Tokenizer.from_file(str(verified_ref(root, tokenizer_ref)))
    tokenizer.no_truncation(); tokenizer.no_padding()
    manifest = snapshot / 'members.jsonl'; manifest_hash = file_sha(manifest)
    expected = json.loads((snapshot / 'snapshot.json').read_text())
    if manifest_hash != expected['members_sha256']:
        raise ValueError('Snapshot changed before accounting')
    cache_path = home(root) / '_token-cache' / (tokenizer_hash + '.jsonl')
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache = {}
    if cache_path.exists():
        for line in cache_path.read_text().splitlines():
            try:
                entry = json.loads(line)
                cache[entry['text_sha256']] = entry['tokens']
            except (ValueError, KeyError):
                continue  # An interrupted tail is a cache miss, never an accounting record.
    counts, tokens, sources, categories = Counter(), Counter(), defaultdict(Counter), defaultdict(Counter)
    uncounted = Counter()
    batch, chars, finished = [], 0, 0
    out = snapshot / 'accounting'; out.mkdir(exist_ok=True)
    write_json(out / 'summary.json', {'status': 'counting', 'members_sha256': manifest_hash})
    with (out / 'tokens.jsonl').open('w') as records, cache_path.open('a') as cache_out:
        def record(row, n):
            nonlocal finished
            role = row['split']; counts[role] += 1; tokens[role] += n
            sources[role][row['source']] += n
            for c in row.get('categories', []): categories[role][c] += n
            records.write(json.dumps({'id': row['id'], 'text_sha256': row['text_artifact']['sha256'], 'split': role, 'tokens': n}) + '\n')
            finished += 1

        def flush():
            nonlocal batch, chars
            if not batch: return
            encodings = tokenizer.encode_batch([text for _, text in batch], add_special_tokens=False)
            for (row, _), encoding in zip(batch, encodings, strict=True):
                n = len(encoding.ids); key = row['text_artifact']['sha256']; cache[key] = n
                cache_out.write(json.dumps({'text_sha256': key, 'tokens': n}) + '\n')
                record(row, n)
            batch, chars = [], 0
            if finished % 1000 < 64: print(json.dumps({'documents': finished, 'tokens': sum(tokens.values())}), flush=True)

        for row in rows(manifest):
            if row['split'] not in roles:
                uncounted[row['split']] += 1
                continue
            raw = verified_ref(root, row['text_artifact']).read_bytes()
            key = row['text_artifact']['sha256']
            if key in cache:
                record(row, cache[key]); continue
            text = raw.decode('utf-8-sig'); batch.append((row, text)); chars += len(text)
            if len(batch) >= 64 or chars >= 250000: flush()
        flush()
    if file_sha(manifest) != manifest_hash:
        raise ValueError('Snapshot changed during accounting')
    report = {'status': 'complete', 'snapshot_id': expected['id'], 'members_sha256': manifest_hash,
              'tokenizer': tokenizer_ref, 'documents_by_role': counts, 'native_tokens_by_role': tokens,
              'tokens_by_role_and_source': sources, 'tokens_by_role_and_category_overlapping': categories,
              'native_tokens_selected_roles': sum(tokens.values()),
              'selected_roles': list(roles), 'uncounted_documents_by_role': uncounted,
              'special_tokens': False, 'truncation': False, 'model_weights_loaded': False,
              'meaning': 'Each selected normalized body counted once; reference roles kept separate. Exact text tokens are not independent knowledge, training exposure, or semantic deduplication.'}
    write_json(out / 'summary.json', report)
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--snapshot', type=Path, required=True)
    parser.add_argument('--tokenizer', type=Path, required=True)
    parser.add_argument('--roles', nargs='+', choices=ROLES, default=['core', 'reference_pdf'])
    args = parser.parse_args()
    import ops
    with ops.named_lock('domain-token-accounting', workspace=args.root / 'workspace'):
        report = count(args.root.resolve(), args.snapshot.resolve(), args.tokenizer, args.roles)
    print(json.dumps({k: report[k] for k in ('status', 'documents_by_role', 'native_tokens_by_role')}), flush=True)
