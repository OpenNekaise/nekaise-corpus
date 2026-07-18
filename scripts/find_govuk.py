#!/usr/bin/env python3
"""find_govuk.py — UK government building-energy publications via the gov.uk Search/Content APIs.

Crown-copyright publications under the Open Government Licence v3.0 (attribution, CC-BY-
interoperable → license `open`, same as the existing hand-curated `gov_uk` entries): SAP/RdSAP,
EPC and ND-NEED statistics, Boiler Upgrade Scheme, Future Homes Standard, building regulations
approved documents, retrofit guidance, DUKES chapters, fuel-poverty and heat-network reports.

Enumeration is clean here — no listing scrape: the documented public Search API
(`/api/search.json`, q + repeated `filter_content_store_document_type` values, `start` offset)
finds publication pages, and each page's Content API JSON (`/api/content/<path>`) lists its
attachments with direct `assets.publishing.service.gov.uk` URLs. Only `application/pdf`
attachments are registered (statistics pages are often spreadsheet-only — skipped). gov.uk
robots.txt for `*` disallows only `/*/print$` and `/search/all*`; the `/api/` endpoints and the
asset host are fair game.

Rotate --page deeper each round (per-query offset into the search results).

    python scripts/find_govuk.py                            # propose (page 1)
    python scripts/find_govuk.py --rows 20 --page 2 --max 250 --append
"""
from __future__ import annotations

import argparse
import sys
import time

import requests
import yaml

import registry

SEARCH = "https://www.gov.uk/api/search.json"
CONTENT = "https://www.gov.uk/api/content{path}"
UA = {"User-Agent": "nekaise-corpus/find_govuk"}
TIMEOUT = 30

# gov.uk content_store_document_type values worth harvesting (multi-value filter = OR).
DOC_TYPES = ["guidance", "policy_paper", "official_statistics", "independent_report",
             "impact_assessment", "research", "notice", "statutory_guidance",
             "national_statistics", "consultation_outcome", "statistical_data_set"]

QUERIES = [
    ("building energy efficiency", "building_energy"),
    ("energy performance certificate", "building_energy"),
    ("standard assessment procedure SAP dwellings", "building_energy"),
    ("Future Homes Standard", "building_energy"),
    ("building regulations approved document", "standards_protocols"),
    ("retrofit insulation homes", "building_energy"),
    ("boiler upgrade scheme", "equipment_systems"),
    ("heat pump", "equipment_systems"),
    ("heat networks", "building_energy"),
    ("fuel poverty", "building_energy"),
    ("non-domestic buildings energy", "building_energy"),
    ("digest of UK energy statistics", "building_energy"),
    ("smart meters energy", "controls_bas"),
    ("building safety", "construction"),
    ("construction industry", "construction"),
    ("ventilation buildings", "equipment_systems"),
    ("overheating dwellings", "building_energy"),
    ("net zero buildings decarbonisation", "building_energy"),
]


def search(term: str, rows: int, page: int) -> list[dict]:
    r = requests.get(SEARCH, params={
        "q": term, "count": rows, "start": (page - 1) * rows,
        "filter_content_store_document_type": DOC_TYPES,
        "fields": "title,link"}, headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    return r.json().get("results") or []


def pdf_attachments(path: str) -> tuple[str, list[tuple[str, str]]]:
    """Content-API page -> (page title, [(attachment title, pdf url), ...])."""
    r = requests.get(CONTENT.format(path=path), headers=UA, timeout=TIMEOUT)
    if r.status_code != 200:
        return "", []
    j = r.json()
    out = []
    for a in (j.get("details") or {}).get("attachments") or []:
        url = a.get("url")
        if a.get("content_type") == "application/pdf" and url:
            out.append(((a.get("title") or "").strip(), url))
    return (j.get("title") or "").strip(), out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=20, help="search results per query page")
    ap.add_argument("--page", type=int, default=1, help="search page (rotate deeper each round)")
    ap.add_argument("--max", type=int, default=250, help="cap on new entries this run")
    ap.add_argument("--append", action="store_true",
                    help="append into the registry (registry/govuk.yaml)")
    args = ap.parse_args()

    urls, titles, reg_ids = registry.existing_keys()
    out, seen_paths = [], set()
    for term, topic in QUERIES:
        if len(out) >= args.max:
            break
        try:
            hits = search(term, args.rows, args.page)
        except Exception as e:
            print(f"# govuk search '{term}' p{args.page} failed: {e}", file=sys.stderr)
            continue
        for res in hits:
            if len(out) >= args.max:
                break
            path = res.get("link")
            if not path or not path.startswith("/") or path in seen_paths:
                continue
            seen_paths.add(path)
            try:
                page_title, atts = pdf_attachments(path)
            except Exception as e:
                print(f"# govuk content {path} failed: {e}", file=sys.stderr)
                continue
            time.sleep(0.4)  # politeness on the content API
            for att_title, url in atts:
                if len(out) >= args.max:
                    break
                title = (f"{page_title} — {att_title}"
                         if att_title and registry.norm(att_title) != registry.norm(page_title)
                         else page_title or att_title)
                u, t = url.rstrip("/"), registry.norm(title)
                if not title or u in urls or t in titles:
                    continue
                urls.add(u)
                titles.add(t)
                out.append({"id": f"guk-{registry.slug(title)[:46]}", "title": title[:180],
                            "url": url, "source": "gov_uk", "license": "open",
                            "topic": topic, "format": "pdf"})
        time.sleep(0.5)

    registry.uniquify_ids(out, reg_ids)

    by_topic: dict = {}
    for h in out:
        by_topic[h["topic"]] = by_topic.get(h["topic"], 0) + 1
    print(f"# {len(out)} NEW gov.uk publications (OGL v3 -> open, deduped vs manifest + registry)")
    print(f"# by topic: {by_topic}")
    print("# --- review, then --append, then scripts/build_corpus.py ---")
    print(yaml.safe_dump(out, sort_keys=False, allow_unicode=True))

    if args.append and out:
        counts = registry.append_entries(out)
        print(f"# appended {len(out)} entries to the registry: {counts}", file=sys.stderr)


if __name__ == "__main__":
    main()
