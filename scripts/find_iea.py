#!/usr/bin/env python3
"""find_iea.py — enumerate IEA (iea.org) built-environment analysis reports (CC BY 4.0).

New vein 2026-07-24: iea.org lists ~2,900 analysis reports in its sitemap; since ~2021 most
report pages declare "Licence: CC BY 4.0" and link a direct PDF on Azure blob storage
(iea.blob.core.windows.net — plain GET, no WAF from this box). ~290 report slugs match
built-environment keywords (heat pumps, cooling, building envelopes, efficiency, cement,
appliances, district heating…). This finder walks the keyword-filtered slug list, fetches each
report page, and keeps only pages that BOTH declare CC BY 4.0 AND carry a main-report PDF;
translated PDFs (…_Chinese.pdf) are skipped as near-duplicates of the English main report.
Older/ARR reports fail the licence gate and are skipped — never added.

NOTE: the IEA EBC annex library (iea-ebc.org / INIVE) is a DIFFERENT channel with
all-rights-reserved bulk terms (see the no-go list); this finder never touches it.

iea.org robots.txt (2026-07-24) disallows only /vhu7/ and /search — /reports/ is crawlable;
we still sleep 1s between fetches. Rotation pointer: registry/rotation.json find_iea
(--offset into the sorted keyword-filtered slug list; step 40).

    python scripts/find_iea.py --offset 0 --limit 40           # propose
    python scripts/find_iea.py --offset 0 --limit 40 --append
"""
from __future__ import annotations

import argparse
import html as htmllib
import re
import sys
import time

import requests

import registry

SITEMAP = "https://www.iea.org/sitemap.xml"
UA = {"User-Agent": "Mozilla/5.0 (compatible; nekaise-corpus/find_iea; research corpus)"}
DELAY = 1.0

REPORT_RE = re.compile(r"<loc>(https://www\.iea\.org/reports/[a-z0-9-]+)</loc>")
# built-environment slice of the ~2,900-report universe; tune as gaps show up.
# \b matters: bare "city|cities" matched inside "electriCITY"/"capaCITY" and pulled ~95
# power-sector slugs into the first universe (fixed 2026-07-24, mis-scoped entries purged).
KEY_RE = re.compile(
    r"building|heat-pump|space-heating|space-cooling|\bcooling|\bheating|efficien|retrofit|"
    r"renovation|envelope|district-heat|district-energy|cement|construction|appliance|"
    r"lighting|air-condition|\bcooking|cookstove|demand-response|demand-side|smart-grid|"
    r"electrification|energy-poverty|behaviour-change|\bcity|\bcities|\burban", re.I)
LIC_RE = re.compile(r"Licence:\s*CC BY 4\.0")
PDF_RE = re.compile(r'https://iea\.blob\.core\.windows\.net/assets/[^"\'<>\s]+\.pdf')
# translated editions of the same report — near-duplicates of the English main PDF
XLAT_RE = re.compile(
    r"_(Chinese|Japanese|Korean|French|German|Spanish|Polish|Italian|Portuguese|Russian|"
    r"Arabic|Turkish|Ukrainian|Vietnamese|Indonesian|Czech|Norwegian|Swedish|Danish|"
    r"Traditional|Simplified)[A-Za-z]*\.pdf$", re.I)
TITLE_RE = re.compile(r"<title>(.*?)</title>", re.S)

TOPICS = [  # slug keyword -> registry topic; first match wins
    (re.compile(r"heat-pump|cooling|heating|air-condition|appliance|lighting|cooking|cookstove", re.I),
     "equipment_systems"),
    (re.compile(r"cement|material", re.I), "materials"),
    (re.compile(r"city|cities|urban|district", re.I), "urban"),
    (re.compile(r"grid|demand-response|demand-side|digitali", re.I), "controls_bas"),
    (re.compile(r"construction", re.I), "construction"),
    (re.compile(r"standard|certification|labelling", re.I), "standards_protocols"),
]


def fetch(url: str) -> str:
    time.sleep(DELAY)
    r = requests.get(url, headers=UA, timeout=60)
    r.raise_for_status()
    return r.text


def topic_for(slug: str) -> str:
    for p, t in TOPICS:
        if p.search(slug):
            return t
    return "building_energy"  # efficiency/buildings default


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offset", type=int, default=0,
                    help="offset into the sorted keyword-filtered report list")
    ap.add_argument("--limit", type=int, default=40, help="report pages to scan this run")
    ap.add_argument("--max", type=int, default=40, help="cap on new entries this run")
    ap.add_argument("--append", action="store_true",
                    help="append into the registry (registry/iea.yaml)")
    args = ap.parse_args()

    try:
        sitemap = fetch(SITEMAP)
    except Exception as e:
        print(f"# sitemap fetch failed: {e}", file=sys.stderr)
        sys.exit(1)  # abort WITHOUT advancing rotation
    slugs = sorted({u for u in REPORT_RE.findall(sitemap) if KEY_RE.search(u.rsplit("/", 1)[1])})
    window = slugs[args.offset:args.offset + args.limit]
    if not window:
        print(f"# offset {args.offset} past end of {len(slugs)} keyword-matched reports — vein dry",
              file=sys.stderr)
        return

    urls, titles, reg_ids = registry.existing_keys()
    out: list[dict] = []
    scanned = skipped_lic = 0
    for purl in window:
        if len(out) >= args.max:
            break
        scanned += 1
        try:
            page = fetch(purl)
        except Exception as e:
            print(f"# report fetch failed {purl}: {e}", file=sys.stderr)
            continue
        if not LIC_RE.search(page):
            skipped_lic += 1  # ARR / NC / pre-CC report — never add
            continue
        pdfs = [p for p in PDF_RE.findall(page) if not XLAT_RE.search(p)]
        if not pdfs:
            continue  # web-only report (HTML chapters, no PDF)
        pdf = pdfs[0]
        mtitle = TITLE_RE.search(page)
        title = htmllib.unescape(re.sub(r"\s+", " ", mtitle.group(1))).strip() if mtitle else ""
        title = re.sub(r"\s*[–-]\s*Analysis\s*-\s*IEA\s*$", "", title) or purl.rsplit("/", 1)[1]
        if pdf.rstrip("/") in urls or registry.norm(title) in titles:
            continue
        urls.add(pdf.rstrip("/"))
        titles.add(registry.norm(title))
        slug_name = purl.rsplit("/", 1)[1]
        out.append({"id": f"iag-{registry.slug(slug_name)[:50]}", "title": title[:150],
                    "url": pdf, "source": "iea", "license": "cc-by",
                    "topic": topic_for(slug_name), "format": "pdf"})

    registry.uniquify_ids(out, reg_ids)
    print(f"# {len(out)} NEW IEA reports (offset {args.offset}, {scanned} pages scanned, "
          f"{skipped_lic} skipped non-CC-BY; universe {len(slugs)} keyword-matched slugs; "
          f"deduped vs manifest + registry + blocklist)")
    by_topic: dict = {}
    for h in out:
        by_topic[h["topic"]] = by_topic.get(h["topic"], 0) + 1
    print(f"# by topic: {by_topic}")
    if args.append and out:
        counts = registry.append_entries(out)
        print(f"# appended {len(out)} entries: {counts}", file=sys.stderr)


if __name__ == "__main__":
    main()
