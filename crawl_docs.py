#!/usr/bin/env python3
"""crawl_docs.py — discover the pages of a documentation site and add them to the registry.

The single-URL loader can't reach multi-page doc sites (sphinx / readthedocs / mkdocs). This
BFS-crawls a seed within ONE domain + path prefix, collects content-page URLs, and PROPOSES them as
`html` sources in sources.yaml. build_corpus then fetches each page on its own, so every page keeps
its own sha256 and the crawl stays REPRODUCIBLE: the registry freezes the page list, and a clone
fetches that frozen list -- it does not re-crawl, so it cannot drift.

    python crawl_docs.py --seed https://eclipse-volttron.readthedocs.io/en/latest/ \
        --prefix /en/latest/ --source volttron --topic controls_bas --license open --max 80
    # add --append to write the discovered pages into sources.yaml, then run build_corpus.py

Crawled pages get id prefix `crawl-`, so prune_corpus.py quality-gates them like discovered sources.
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from collections import deque
from pathlib import Path
from urllib.parse import urldefrag, urljoin, urlparse

import requests
import yaml
from bs4 import BeautifulSoup

HERE = Path(__file__).resolve().parent
UA = "nekaise-studio-hvac-corpus/0.1 (research)"
SKIP_EXT = re.compile(r"\.(pdf|zip|png|jpe?g|gif|svg|js|css|woff2?|ico|tar|gz|whl|epub|json|xml)$", re.I)


def slug(s: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", (s or "").lower())).strip("-")


def crawl(seed: str, prefix: str, maxp: int) -> list[str]:
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
        time.sleep(0.3)  # be polite
    return pages


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", required=True)
    ap.add_argument("--prefix", default="", help="only follow links whose path starts with this")
    ap.add_argument("--source", required=True, help="source tag / id namespace, e.g. volttron")
    ap.add_argument("--topic", required=True)
    ap.add_argument("--license", default="open")
    ap.add_argument("--max", type=int, default=80)
    ap.add_argument("--append", action="store_true")
    args = ap.parse_args()

    pages = crawl(args.seed, args.prefix, args.max)
    print(f"# crawled {len(pages)} pages from {args.seed}", file=sys.stderr)

    rows, used = [], set()
    for u in pages:
        rel = urlparse(u).path
        if args.prefix and rel.startswith(args.prefix):
            rel = rel[len(args.prefix):]
        sid = f"crawl-{args.source}-{slug(rel) or 'index'}"[:62]
        while sid in used:
            sid += "x"
        used.add(sid)
        rows.append({"id": sid, "title": f"{args.source} docs: {rel.strip('/') or 'index'}"[:150],
                     "url": u, "source": args.source, "license": args.license,
                     "topic": args.topic, "format": "html"})
    print(yaml.safe_dump(rows, sort_keys=False, allow_unicode=True))

    if args.append and rows:
        blk = ""
        for r in rows:
            d = yaml.safe_dump([r], sort_keys=False, allow_unicode=True)
            blk += "".join(("  " + ln + "\n") if ln else "\n" for ln in d.splitlines())
        with open(HERE / "sources.yaml", "a") as f:
            f.write(f"\n  # --- crawled docs: {args.source} ---\n" + blk)
        print(f"# appended {len(rows)} pages to sources.yaml", file=sys.stderr)


if __name__ == "__main__":
    main()
