#!/usr/bin/env python3
"""find_sdz.py — Austrian BMK-funded building-research project reports (German).

nachhaltigwirtschaften.at hosts the project-report archives of two sibling BMK ("Bundesministerium
für Klimaschutz", formerly bmvit) research programmes:
    sdz  "Stadt der Zukunft"  (city/quarter-scale, ~2016-present)
    hdz  "Haus der Zukunft"   (building-scale, ~2004-2018, program since folded into sdz — its
                               listing pages still serve the older reports plus some sdz overlap)
Each programme has one paginated listing,
    https://nachhaltigwirtschaften.at/de/{series}/publikationen/projektberichte.php?page=N
~20 reports per page, rendered as `<div class="searchresult">` blocks: title in
`h3.searchresult__header > a`, one or more downloads in
`a.searchresult__publication-download-link` (the first is always the Endbericht; a few items also
list Anhänge/attachments as extra download links — we only take the first, main report). PDF hrefs
are a mix of site-relative (`/resources/sdz_pdf/...`) and protocol-relative to a sibling domain
(`//klimaneutralestadt.at/resources/pdf/...`) — `urljoin` against the listing page's own URL
handles both. A page past the end of the series 200s with zero `searchresult` blocks (verified
2026-07: sdz ends around page 16, hdz around page 25) — this backend stops walking a series early
when a page comes back empty, so `--pages` is just an upper bound, not a promise.

These are German-language final reports from a public research-funding programme (BMK) — openly
published deliverables, no paywall, no login. Tagged license `open`. The corpus accepts all
languages and the quality gate has German building vocabulary, so no translation/filtering needed
here beyond the usual dedup.

Topic is guessed from the title via an ordered regex list (first match wins; default
`building_energy`):
    Sanierung/Dämm(ung)/Fassade  -> building_energy   (retrofit/insulation/envelope)
    Quartier/Stadt/urban         -> urban
    BIM/Digital                  -> construction
    Speicher/Wärmepumpe/Anergie/Netz -> equipment_systems
    Holz/Beton/Baustoff          -> materials

    python scripts/find_sdz.py --series sdz --page 1 --pages 5           # propose
    python scripts/find_sdz.py --series hdz --page 1 --pages 3 --max 200 --append
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from urllib.parse import urljoin

import requests
import yaml
from bs4 import BeautifulSoup

import registry

BASE = "https://nachhaltigwirtschaften.at"
UA = {"User-Agent": "nekaise-corpus/find_sdz"}

# (title regex -> topic), first match wins; default building_energy.
TOPIC_RULES = [
    (re.compile(r"Sanierung|D[äa]mm|Fassade", re.I), "building_energy"),
    (re.compile(r"Quartier|Stadt|urban", re.I), "urban"),
    (re.compile(r"BIM|Digital", re.I), "construction"),
    (re.compile(r"Speicher|W[äa]rmepumpe|Anergie|Netz", re.I), "equipment_systems"),
    (re.compile(r"Holz|Beton|Baustoff", re.I), "materials"),
]


def classify(title: str) -> str:
    for rx, topic in TOPIC_RULES:
        if rx.search(title):
            return topic
    return "building_energy"


def fetch_page(series: str, page: int) -> list[dict]:
    """One listing page -> [{title, pdf_url}, ...] in document order. Empty list = past the end."""
    url = f"{BASE}/de/{series}/publikationen/projektberichte.php?page={page}"
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    reports = []
    for block in soup.find_all("div", class_="searchresult"):
        h3 = block.find("h3", class_="searchresult__header")
        a_title = h3.find("a") if h3 else None
        title = a_title.get_text(strip=True) if a_title else ""
        downloads = block.find_all("a", class_="searchresult__publication-download-link")
        pdfs = [d for d in downloads if (d.get("href") or "").lower().endswith(".pdf")]
        if not title or not pdfs:
            continue  # no title or no direct PDF link -> not fetchable, skip
        pdf_url = urljoin(url, pdfs[0]["href"])
        reports.append({"title": title, "pdf_url": pdf_url})
    return reports


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", default="sdz", choices=["sdz", "hdz"], help="programme series")
    ap.add_argument("--page", type=int, default=1, help="start page")
    ap.add_argument("--pages", type=int, default=5, help="how many listing pages to walk this run")
    ap.add_argument("--max", type=int, default=200, help="cap on new entries this run")
    ap.add_argument("--append", action="store_true", help="append into the registry (registry/austria.yaml)")
    args = ap.parse_args()

    urls, titles, reg_ids = registry.existing_keys()
    out = []
    pages_walked = 0
    for page in range(args.page, args.page + args.pages):
        if len(out) >= args.max:
            break
        try:
            reports = fetch_page(args.series, page)
        except Exception as e:
            print(f"# {args.series} page {page} fetch failed: {e}", file=sys.stderr)
            break
        pages_walked += 1
        if not reports:
            break  # past the end of this series' listing
        for rep in reports:
            if len(out) >= args.max:
                break
            title, pdf_url = rep["title"], rep["pdf_url"]
            u, t = pdf_url.rstrip("/"), registry.norm(title)
            if u in urls or t in titles:
                continue
            urls.add(u)
            titles.add(t)
            sid = f"sdz-{registry.slug(title)[:52]}"
            out.append({"id": sid, "title": title[:150], "url": pdf_url,
                        "source": f"{args.series}_at", "license": "open",
                        "topic": classify(title), "format": "pdf"})
        time.sleep(0.5)  # politeness between pages

    registry.uniquify_ids(out, reg_ids)
    by_topic: dict = {}
    for h in out:
        by_topic[h["topic"]] = by_topic.get(h["topic"], 0) + 1
    print(f"# {len(out)} NEW {args.series} reports from pages {args.page}..{args.page + pages_walked - 1} "
          f"(deduped vs manifest + registry + blocklist)")
    print(f"# by topic: {by_topic}")
    print("# --- review, then --append, then scripts/build_corpus.py ---")
    print(yaml.safe_dump(out, sort_keys=False, allow_unicode=True))

    if args.append and out:
        counts = registry.append_entries(out)
        print(f"# appended {len(out)} entries to the registry: {counts}", file=sys.stderr)


if __name__ == "__main__":
    main()
