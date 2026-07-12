#!/usr/bin/env python3
"""find_nrc.py — NRC Canada Publications Archive (nrc-publications.canada.ca).

The National Research Council's publications archive holds ~67k citations / ~23k full-text digital
objects back to 1918, including the Division of Building Research / Institute for Research in
Construction output: the classic Canadian Building Digests (CBD-1..250, the canonical mid-century
building-science shorts), Building Research Notes, Building Practice Notes, DBR papers, and old
National Building Code editions. English + French.

Enumeration: the public search UI is plain server-rendered HTML,
    https://nrc-publications.canada.ca/eng/search/?q=<query>&pg=<N>&ps=50
Each hit is an `<article itemtype="https://schema.org/CreativeWork">` block: title in
`.metadata-title h3 a`, series ("Canadian Building Digest; no. CBD-232") in an `isPartOf` span,
and — only when a digital object exists — a `.metadata-downloads` link
`https://nrc-publications.canada.ca/eng/view/ft/?id=<UUID>` that serves the PDF directly
(verified 2026-07: 200 application/pdf, %PDF magic). Records without a downloads block are
citation-only and skipped. A page past the end renders zero article blocks, so `--pages` is an
upper bound. (The UI's CSV export is an async session-bound job — not worth the state.)

Queries worth walking (rotate via --q; pointer/notes in registry/rotation.json):
    "canadian building digest"      253 hits, ~all with full text  <- default
    "building research note"        DBR short notes
    "building practice note"        IRC practice notes
    "division of building research" the wider DBR report universe
    "national building code"        old PD-era code editions

License: Government of Canada terms — reproduction permitted with attribution (non-commercial
without further permission), same shape as ADEME -> tagged `open`.

    python scripts/find_nrc.py --page 1 --pages 6                    # propose, CBD series
    python scripts/find_nrc.py --q "building research note" --page 1 --pages 4 --max 200 --append
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from urllib.parse import quote_plus

import requests
import yaml
from bs4 import BeautifulSoup

import registry

BASE = "https://nrc-publications.canada.ca"
UA = {"User-Agent": "nekaise-corpus/find_nrc"}

# (title regex -> topic), first match wins; default building_energy.
TOPIC_RULES = [
    (re.compile(r"thermal|insulat|heat loss|energy|humidity|moisture|condensation|vapour|vapor", re.I),
     "building_energy"),
    (re.compile(r"heating|ventilat|air condition|chimney|furnace|boiler", re.I), "equipment_systems"),
    (re.compile(r"concrete|masonry|brick|wood|timber|roofing|sealant|paint|corrosion|material|adhesive", re.I),
     "materials"),
    (re.compile(r"fire|flame|smoke", re.I), "construction"),
    (re.compile(r"code|regulation|standard", re.I), "standards_protocols"),
    (re.compile(r"foundation|soil|frost|structural|load|wind|snow", re.I), "structures_civil"),
    (re.compile(r"acoustic|sound|noise|vibration", re.I), "building_energy"),
]


def classify(title: str) -> str:
    for rx, topic in TOPIC_RULES:
        if rx.search(title):
            return topic
    return "building_energy"


def fetch_page(query: str, page: int) -> list[dict]:
    """One search page -> [{title, pdf_url, series}, ...]. Empty list = past the end."""
    url = f"{BASE}/eng/search/?q={quote_plus(query)}&pg={page}&ps=50"
    r = requests.get(url, headers=UA, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    hits = []
    for art in soup.find_all("article", itemtype="https://schema.org/CreativeWork"):
        title_div = art.find("div", class_="metadata-title")
        a_title = title_div.find("a") if title_div else None
        title = a_title.get_text(strip=True) if a_title else ""
        dl = art.find("div", class_="metadata-downloads")
        ft = dl.find("a", href=re.compile(r"/eng/view/ft/\?id=")) if dl else None
        if not title or not ft:
            continue  # citation-only record (no digital object) -> skip
        series_span = art.find("span", itemprop="isPartOf")
        series = series_span.get_text(strip=True) if series_span else ""
        hits.append({"title": title, "pdf_url": ft["href"].split("&")[0], "series": series})
    return hits


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--q", default="canadian building digest", help="archive search query to walk")
    ap.add_argument("--page", type=int, default=1, help="first search page (50 hits/page)")
    ap.add_argument("--pages", type=int, default=6, help="how many search pages to walk this run")
    ap.add_argument("--max", type=int, default=300, help="cap on new entries this run")
    ap.add_argument("--append", action="store_true", help="append into the registry (registry/canada.yaml)")
    args = ap.parse_args()

    urls, titles, reg_ids = registry.existing_keys()
    out = []
    pages_walked = 0
    for page in range(args.page, args.page + args.pages):
        if len(out) >= args.max:
            break
        try:
            hits = fetch_page(args.q, page)
        except Exception as e:
            print(f"# page {page} fetch failed: {e}", file=sys.stderr)
            break
        pages_walked += 1
        if not hits:
            break  # past the end of this query's results
        for h in hits:
            if len(out) >= args.max:
                break
            # series tag ("Canadian Building Digest; no. CBD-232") disambiguates near-identical titles
            full_title = f"{h['title']} ({h['series']})" if h["series"] else h["title"]
            u, t = h["pdf_url"].rstrip("/"), registry.norm(full_title)
            if u in urls or t in titles:
                continue
            urls.add(u)
            titles.add(t)
            sid = f"nrc-{registry.slug(full_title)[:52]}"
            out.append({"id": sid, "title": full_title[:150], "url": h["pdf_url"],
                        "source": "nrc_canada", "license": "open",
                        "topic": classify(h["title"]), "format": "pdf"})
        time.sleep(1.0)  # politeness between pages

    registry.uniquify_ids(out, reg_ids)
    by_topic: dict = {}
    for h in out:
        by_topic[h["topic"]] = by_topic.get(h["topic"], 0) + 1
    print(f"# {len(out)} NEW nrc-publications hits for {args.q!r} from pages "
          f"{args.page}..{args.page + pages_walked - 1} (deduped vs manifest + registry + blocklist)")
    print(f"# by topic: {by_topic}")
    print("# --- review, then --append, then scripts/build_corpus.py ---")
    print(yaml.safe_dump(out, sort_keys=False, allow_unicode=True))

    if args.append and out:
        counts = registry.append_entries(out)
        print(f"# appended {len(out)} entries to the registry: {counts}", file=sys.stderr)


if __name__ == "__main__":
    main()
