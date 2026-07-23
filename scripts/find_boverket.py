#!/usr/bin/env python3
"""find_boverket.py — enumerate Boverket (Swedish national building authority) publications.

Targets the Nordic cell of the coverage radar (0.4% of the corpus, coverage_matrix.py
2026-07-23). boverket.se lists ~350 publications (35 listing pages × ~10) at
/sv/om-boverket/publicerat-av-boverket/publikationer/?page=N, each publication page carrying
a direct /globalassets/*.pdf link — building regulations (BBR/EKS, some in English), energy &
climate-declaration guidance, planning/housing analyses. Mostly Swedish -> also feeds the sv
language gap; the all-language gate keeps what is on-topic. Agency publications are freely
downloadable/shareable -> license=open.

robots.txt sets Crawl-delay: 10 — this finder sleeps 10s between EVERY fetch (and
build_corpus.py carries a matching HOST_DELAY), so keep --pages small: one listing page ≈
1 + ~10 fetches ≈ 2 min. Rotation pointer: registry/rotation.json find_boverket (--page).

    python scripts/find_boverket.py --page 1 --pages 2           # propose
    python scripts/find_boverket.py --page 1 --pages 2 --append
"""
from __future__ import annotations

import argparse
import html as htmllib
import re
import sys
import time

import requests

import registry

BASE = "https://www.boverket.se"
LIST = BASE + "/sv/om-boverket/publicerat-av-boverket/publikationer/?page={n}"
UA = {"User-Agent": "nekaise-corpus/find_boverket (research corpus; honors Crawl-delay 10)"}
DELAY = 10.0  # robots.txt Crawl-delay
PUB_RE = re.compile(r'href="(https://www\.boverket\.se/sv/om-boverket/publikationer/2\d{3}/[^"]+)"')
PDF_RE = re.compile(r'href="(/globalassets/[^"]+\.pdf)"', re.I)
TITLE_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S)

TOPICS = [  # Swedish keyword -> registry topic; first match wins
    (re.compile(r"energi|klimatdeklaration|laddinfrastruktur|värme", re.I), "building_energy"),
    (re.compile(r"byggregler|\bbbr\b|\beks\b|föreskrift|konstruktionsregler", re.I), "standards_protocols"),
    (re.compile(r"brand", re.I), "architecture"),
    (re.compile(r"konstruktion|bärverk|bärande", re.I), "structures_civil"),
    (re.compile(r"plan(ering)?|översiktsplan|detaljplan|stadsutveckling", re.I), "urban"),
    (re.compile(r"bygg|renovering|ombyggnad", re.I), "construction"),
]


def fetch(url: str) -> str:
    time.sleep(DELAY)
    r = requests.get(url, headers=UA, timeout=45)
    r.raise_for_status()
    return r.text


def topic_for(title: str) -> str:
    for p, t in TOPICS:
        if p.search(title):
            return t
    return "urban"  # housing/planning authority default


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--page", type=int, default=1, help="listing page to start at (1-based)")
    ap.add_argument("--pages", type=int, default=2, help="listing pages to walk this run")
    ap.add_argument("--max", type=int, default=40, help="cap on new entries this run")
    ap.add_argument("--append", action="store_true",
                    help="append into the registry (registry/nordic.yaml)")
    args = ap.parse_args()

    urls, titles, reg_ids = registry.existing_keys()
    out: list[dict] = []
    scanned = 0
    for n in range(args.page, args.page + args.pages):
        if len(out) >= args.max:
            break
        try:
            listing = fetch(LIST.format(n=n))
        except Exception as e:
            print(f"# listing fetch failed p{n}: {e}", file=sys.stderr)
            sys.exit(1)  # abort WITHOUT advancing rotation
        pubs = sorted(set(PUB_RE.findall(listing)))
        if not pubs:
            print(f"# no publications on listing p{n} — end of archive?", file=sys.stderr)
            break
        for purl in pubs:
            if len(out) >= args.max:
                break
            scanned += 1
            try:
                page = fetch(purl)
            except Exception as e:
                print(f"# pub fetch failed {purl}: {e}", file=sys.stderr)
                continue
            mpdf = PDF_RE.search(page)
            mtitle = TITLE_RE.search(page)
            if not mpdf or not mtitle:
                continue  # some entries are web-only guidance without a PDF
            title = htmllib.unescape(re.sub(r"<[^>]+>", "", mtitle.group(1))).strip()
            pdf = BASE + mpdf.group(1)
            if pdf.rstrip("/") in urls or registry.norm(title) in titles:
                continue
            urls.add(pdf.rstrip("/"))
            titles.add(registry.norm(title))
            out.append({"id": f"bov-{registry.slug(title)[:50]}", "title": title[:150],
                        "url": pdf, "source": "boverket", "license": "open",
                        "topic": topic_for(title), "format": "pdf"})

    registry.uniquify_ids(out, reg_ids)
    print(f"# {len(out)} NEW Boverket publications (listing p{args.page}..{args.page+args.pages-1}, "
          f"{scanned} pub pages scanned; deduped vs manifest + registry + blocklist)")
    by_topic: dict = {}
    for h in out:
        by_topic[h["topic"]] = by_topic.get(h["topic"], 0) + 1
    print(f"# by topic: {by_topic}")
    if args.append and out:
        counts = registry.append_entries(out)
        print(f"# appended {len(out)} entries: {counts}", file=sys.stderr)


if __name__ == "__main__":
    main()
