#!/usr/bin/env python3
"""crawl_docs.py — discover the pages of a documentation site and add them to the registry.

The single-URL loader can't reach multi-page doc sites (sphinx / readthedocs / mkdocs). This
BFS-crawls a seed within ONE domain + path prefix, collects content-page URLs, and PROPOSES them as
`html` sources in registry/crawl.yaml. build_corpus then fetches each page on its own, so every page keeps
its own sha256 and the crawl stays REPRODUCIBLE: the registry freezes the page list, and a clone
fetches that frozen list -- it does not re-crawl, so it cannot drift.

    python scripts/crawl_docs.py --seed https://eclipse-volttron.readthedocs.io/en/latest/ \
        --prefix /en/latest/ --source volttron --topic controls_bas --license open --max 80
    # add --append to write the discovered pages into the registry, then run scripts/build_corpus.py

Crawled pages get id prefix `crawl-`, so prune_corpus.py quality-gates them like discovered sources.
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from collections import deque
from datetime import date
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse

import requests
import yaml
from bs4 import BeautifulSoup

import dedup
import registry
import store

HERE = Path(__file__).resolve().parents[1]  # repo root (this file lives in scripts/)
UA = "nekaise-studio-hvac-corpus/0.1 (research)"
SKIP_EXT = re.compile(r"\.(pdf|zip|png|jpe?g|gif|svg|js|css|woff2?|ico|tar|gz|whl|epub|json|xml)$", re.I)


def crawl(seed: str, prefix: str, maxp: int, delay: float = 0.3) -> list[str]:
    host = urlparse(seed).netloc
    seen: set[str] = set()
    pages: list[str] = []
    q: deque[str] = deque([seed])
    while q and len(pages) < maxp:
        url = urldefrag(q.popleft())[0]
        if url in seen:
            continue
        seen.add(url)
        try:
            r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
            if r.status_code != 200 or "html" not in r.headers.get("content-type", ""):
                continue
        except Exception:
            continue
        pages.append(url)
        for a in BeautifulSoup(r.text, "html.parser").find_all("a", href=True):
            nu = urldefrag(urljoin(url, a["href"]))[0]
            p = urlparse(nu)
            if p.netloc != host or (prefix and not p.path.startswith(prefix)):
                continue
            if SKIP_EXT.search(p.path):
                continue
            if nu not in seen:
                q.append(nu)
        time.sleep(delay)  # be polite; raise via --delay for sites with a robots Crawl-delay
    return pages


def pinned_restrictions() -> dict:
    """The eligibility restrictions pinned in a store read view (store.pinned_policy): the
    committed, validated policy, failing closed."""
    with dedup.read_view() as view:
        restrictions, _ = store.pinned_policy(view)
    return restrictions


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", required=True)
    ap.add_argument("--prefix", default="", help="only follow links whose path starts with this")
    ap.add_argument("--source", required=True, help="source tag / id namespace, e.g. volttron")
    ap.add_argument("--topic", required=True)
    ap.add_argument("--license", default="open")
    ap.add_argument("--max", type=int, default=80)
    ap.add_argument("--delay", type=float, default=0.3,
                    help="seconds between requests (set to the site's robots Crawl-delay)")
    ap.add_argument("--license-url", default="",
                    help="canonical URL of the exact licence (e.g. LGPL-2.1 text) for provenance")
    ap.add_argument("--license-evidence", default="",
                    help="where the grant is stated, e.g. 'SPDX LGPL-2.1: <LICENSE url>'")
    ap.add_argument("--append", action="store_true")
    args = ap.parse_args()

    # A reviewed rights decision (registry/eligibility.json) outranks a one-shot crawl: refuse
    # before any request instead of registering pages the loader would never fetch.
    probe = {"id": f"crawl-{args.source}-", "source": args.source}
    if hit := registry.restriction_for(probe, pinned_restrictions()):
        raise SystemExit(f"source {args.source!r} is restricted by eligibility rule "
                         f"{hit[0]!r} ({hit[1]['decided_at']}): {hit[1]['reason']}")

    pages = crawl(args.seed, args.prefix, args.max, args.delay)
    print(f"# crawled {len(pages)} pages from {args.seed}", file=sys.stderr)

    keys = dedup.open_keys()
    urls_known = keys.urls
    keys.prefetch(urls=[u.rstrip("/") for u in pages])
    rows = []
    for u in pages:
        if u.rstrip("/") in urls_known:
            continue  # already registered or previously pruned (blocklist)
        rel = urlparse(u).path
        if args.prefix and rel.startswith(args.prefix):
            rel = rel[len(args.prefix):]
        sid = f"crawl-{args.source}-{registry.slug(rel) or 'index'}"[:62]
        row = {"id": sid, "title": f"{args.source} docs: {rel.strip('/') or 'index'}"[:150],
               "url": u, "source": args.source, "license": args.license,
               "topic": args.topic, "format": "html"}
        if args.license_url:
            row["license_url"] = args.license_url
        if args.license_evidence:
            row["license_evidence"] = args.license_evidence
        if args.license_url or args.license_evidence:
            row["rights_verified_at"] = date.today().isoformat()
        rows.append(row)
    keys.uniquify_ids(rows)
    print(yaml.safe_dump(rows, sort_keys=False, allow_unicode=True))

    if args.append and rows:
        counts = registry.append_entries(rows)
        print(f"# appended {len(rows)} pages to the registry: {counts}", file=sys.stderr)


if __name__ == "__main__":
    main()
