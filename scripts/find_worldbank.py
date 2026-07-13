#!/usr/bin/env python3
"""find_worldbank.py — World Bank Documents & Reports via the official WDS search API.

The Bank's public Documents & Reports API (documented at
https://documents.worldbank.org/en/publication/documents-reports/api) is a plain REST endpoint:
    https://search.worldbank.org/api/v3/wds?format=json&qterm=<q>&rows=50&os=<offset>
Response: {"total": N, "documents": {"D<id>": {docty, docdt, display_title, pdfurl, guid, ...},
"facets": {...}}} — one JSON dict per doc, `pdfurl` is a direct documents.worldbank.org PDF link
(the same host our hand-curated wb-* ESMAP PDFs already load from). This is an API built for
programmatic access — no scraping, no wall. "building energy efficiency" alone = ~17k records
(2026-07), so the vein is deep; rotate `--os` via registry/rotation.json and switch `--q` between
rounds (see the rotation note).

License: World Bank Open Access Policy — formal publications and research are CC-BY (3.0 IGO).
Operational project documents carry the Bank's open Access-to-Information terms; tagged cc-by like
the existing curated worldbank entries.

Junk control: bulk operational paperwork (procurement plans, disbursement letters, agreements,
audits) is skipped by docty/title before it ever hits the registry; the prune gate catches the
rest.

    python scripts/find_worldbank.py --q "building energy efficiency" --os 0 --pages 4   # propose
    python scripts/find_worldbank.py --os 200 --max 200 --append
"""
from __future__ import annotations

import argparse
import re
import sys
import time

import requests
import yaml

import registry

API = "https://search.worldbank.org/api/v3/wds"
UA = {"User-Agent": "nekaise-corpus/find_worldbank"}

# document types that are operational paperwork, not prose worth CPT tokens
JUNK_DOCTY = re.compile(
    r"procurement plan|disbursement|agreement|auditing document|audit report|agenda|"
    r"month(ly)? operational summary|statement of loans|notice|contract|invitation|"
    r"letter|memorandum of the president|chairman summary|board summary", re.I)
JUNK_TITLE = re.compile(r"procurement plan|disbursement|audit(ed)? report", re.I)

# (title regex -> topic), first match wins; default building_energy.
TOPIC_RULES = [
    (re.compile(r"urban|city|cities|municipal", re.I), "urban"),
    (re.compile(r"district heat|heat pump|cooling|heating|boiler|chiller|appliance", re.I),
     "equipment_systems"),
    (re.compile(r"code|standard|regulation|certification|labell?ing", re.I), "standards_protocols"),
    (re.compile(r"cement|concrete|material|brick|timber", re.I), "materials"),
    (re.compile(r"construction|housing|building sector", re.I), "construction"),
    (re.compile(r"grid|transmission|infrastructure|road|transport", re.I), "infrastructure"),
]


def classify(title: str) -> str:
    for rx, topic in TOPIC_RULES:
        if rx.search(title):
            return topic
    return "building_energy"


def fetch_page(query: str, rows: int, offset: int) -> tuple[int, list[dict]]:
    """One API page -> (total, [{title, pdf_url, docty}, ...])."""
    r = requests.get(API, params={
        "format": "json", "qterm": query, "rows": rows, "os": offset,
        "fl": "display_title,docdt,pdfurl,docty"}, headers=UA, timeout=30)
    r.raise_for_status()
    js = r.json()
    docs = []
    for key, d in js.get("documents", {}).items():
        if key == "facets" or not isinstance(d, dict):
            continue
        title = (d.get("display_title") or "").strip()
        pdf = (d.get("pdfurl") or "").strip()
        docty = (d.get("docty") or "").strip()
        if not title or not pdf:
            continue
        if JUNK_DOCTY.search(docty) or JUNK_TITLE.search(title):
            continue
        docs.append({"title": title, "pdf_url": pdf.replace("http://", "https://", 1),
                     "docty": docty})
    return int(js.get("total", 0)), docs


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--q", default="building energy efficiency", help="WDS search query")
    ap.add_argument("--os", type=int, default=0, help="starting record offset")
    ap.add_argument("--rows", type=int, default=50, help="records per API page")
    ap.add_argument("--pages", type=int, default=4, help="API pages to walk this run")
    ap.add_argument("--max", type=int, default=200, help="cap on new entries this run")
    ap.add_argument("--append", action="store_true",
                    help="append into the registry (registry/worldbank.yaml)")
    args = ap.parse_args()

    urls, titles, reg_ids = registry.existing_keys()
    out = []
    total = 0
    for page in range(args.pages):
        if len(out) >= args.max:
            break
        offset = args.os + page * args.rows
        try:
            total, docs = fetch_page(args.q, args.rows, offset)
        except Exception as e:
            print(f"# os={offset} fetch failed: {e}", file=sys.stderr)
            break
        if offset >= total or not docs and offset > total - args.rows:
            break  # past the end of this query's results
        for d in docs:
            if len(out) >= args.max:
                break
            u, t = d["pdf_url"].rstrip("/"), registry.norm(d["title"])
            if u in urls or t in titles:
                continue
            urls.add(u)
            titles.add(t)
            sid = f"wbd-{registry.slug(d['title'])[:52]}"
            out.append({"id": sid, "title": d["title"][:150], "url": d["pdf_url"],
                        "source": "worldbank_wds", "license": "cc-by",
                        "topic": classify(d["title"]), "format": "pdf"})
        time.sleep(1.0)  # politeness between API pages

    registry.uniquify_ids(out, reg_ids)
    by_topic: dict = {}
    for h in out:
        by_topic[h["topic"]] = by_topic.get(h["topic"], 0) + 1
    print(f"# {len(out)} NEW World Bank docs for {args.q!r} at os {args.os}..+{args.pages}x{args.rows} "
          f"of {total} total (deduped vs manifest + registry + blocklist)")
    print(f"# by topic: {by_topic}")
    print("# --- review, then --append, then scripts/build_corpus.py ---")
    print(yaml.safe_dump(out, sort_keys=False, allow_unicode=True))

    if args.append and out:
        counts = registry.append_entries(out)
        print(f"# appended {len(out)} entries to the registry: {counts}", file=sys.stderr)


if __name__ == "__main__":
    main()
