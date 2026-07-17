#!/usr/bin/env python3
"""find_jrc.py — EU Joint Research Centre science-for-policy reports (EUR series and siblings).

The JRC Publications Repository (publications.jrc.ec.europa.eu, ~38k records) holds the EUR
science-for-policy report series — building-stock/renovation analyses, Energy Performance of
Buildings Directive support studies, Eurocode background documents, energy-poverty and district-
heating work. Its search UI, OAI-PMH endpoint and robots.txt are all F5-WAF-walled (see the
no-go notes: don't engineer around the WAF), but individual *handle* pages and the direct
`/repository/bitstream/JRCnnnnn/*.pdf` full texts serve plain 200s.

Route: OpenAIRE Graph API filtered to the repository's datasource id (so enumeration happens on
OpenAIRE's side, not against the WAF) -> each record's handle page -> its `citation_doi` meta +
bitstream link. Kept only when the DOI carries an EU Publications Office prefix (10.2760 etc.),
which marks the document as Commission-own — CC-BY under Commission Decision 2011/833/EU — AND a
bitstream PDF exists. Journal postprints (external DOIs) and metadata-only records are skipped:
their rights are per-publisher, not Commission CC-BY.

Multi-word `search` terms behave oddly with the datasource filter when combined with `type`
(observed 0 hits), so no `type` param is sent; the DOI+bitstream gate does the filtering.

    python scripts/find_jrc.py                             # propose (page 1)
    python scripts/find_jrc.py --rows 25 --page 2 --max 200 --append
"""
from __future__ import annotations

import argparse
import re
import sys
import time

import requests
import yaml

import registry

API = "https://api.openaire.eu/graph/v1/researchProducts"
JRC_DATASOURCE = "opendoar____::752d25a1f8dbfb2d656bac3094bfb81c"  # "JRC Publications Repository"
HANDLE_HOST = "publications.jrc.ec.europa.eu"
UA = {"User-Agent": "nekaise-corpus/find_jrc"}
TIMEOUT = 30

# EU Publications Office DOI prefixes seen on Commission-own JRC reports (10.2760 current;
# 10.2788/10.2790/10.2789 older EUR reports; 10.2905 JRC data). Publisher DOIs (10.1016, ...)
# mean a journal postprint — skipped.
OP_DOI_PREFIXES = ("10.2760/", "10.2788/", "10.2790/", "10.2789/", "10.2905/")

CITATION_DOI_RE = re.compile(r'<meta[^>]+name="citation_doi"[^>]+content="([^"]+)"')
BITSTREAM_RE = re.compile(
    r'href="(https?://publications\.jrc\.ec\.europa\.eu/repository/bitstream/[^"]+\.pdf)"', re.I)

QUERIES = [
    ("building renovation", "building_energy"),
    ("energy performance buildings", "building_energy"),
    ("energy efficiency", "building_energy"),
    ("building stock", "building_energy"),
    ("energy poverty", "building_energy"),
    ("heating cooling", "equipment_systems"),
    ("heat pump", "equipment_systems"),
    ("district heating", "building_energy"),
    ("photovoltaic building", "building_energy"),
    ("energy consumption households", "building_energy"),
    ("Eurocode", "structures_civil"),
    ("seismic buildings", "structures_civil"),
    ("construction products", "materials"),
    ("cement concrete", "materials"),
    ("smart cities energy", "urban"),
    # second wave (07-17): the first 15 went dry at p5 while 'energy' alone still counts 6.3k —
    # JRC policy-support vocabulary the head queries miss.
    ("energy performance certificates", "building_energy"),
    ("cost-optimal energy", "building_energy"),
    ("nearly zero-energy", "building_energy"),
    ("renovation wave", "building_energy"),
    ("energy communities", "building_energy"),
    ("ecodesign energy label", "equipment_systems"),
    ("air conditioning cooling demand", "equipment_systems"),
    ("solar thermal", "equipment_systems"),
    ("insulation thermal", "materials"),
    ("construction demolition waste", "materials"),
    ("green public procurement buildings", "standards_protocols"),
    ("energy consumption tertiary sector", "building_energy"),
    ("urban heat island", "urban"),
    ("climate adaptation buildings", "urban"),
    ("housing conditions", "construction"),
]


def from_openaire(term: str, rows: int, page: int) -> list[tuple[str, str]]:
    """[(title, handle url)] for JRC-repository records matching `term`."""
    r = requests.get(API, params={"search": term, "pageSize": rows, "page": page,
                                  "relCollectedFromDatasourceId": JRC_DATASOURCE}, timeout=45)
    r.raise_for_status()
    out = []
    for rec in r.json().get("results") or []:
        title = rec.get("mainTitle")
        handle = next((u for inst in rec.get("instances") or []
                       for u in inst.get("urls") or []
                       if HANDLE_HOST in u and "/handle/" in u), None)
        if title and handle:
            out.append((title, handle))
    return out


def resolve_bitstream(session: requests.Session, handle_url: str) -> str | None:
    """Handle page -> direct bitstream PDF url, or None if the record is not a Commission-own
    open report (no OP-prefix citation_doi, or no bitstream — e.g. journal postprints)."""
    try:
        r = session.get(handle_url, headers=UA, timeout=TIMEOUT)
    except requests.RequestException as e:
        print(f"# handle fetch failed {handle_url}: {e}", file=sys.stderr)
        return None
    if r.status_code != 200:
        return None
    dois = CITATION_DOI_RE.findall(r.text)
    if not any(d.startswith(OP_DOI_PREFIXES) for d in dois):
        return None
    m = BITSTREAM_RE.search(r.text)
    return m.group(1) if m else None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=25, help="records per query page")
    ap.add_argument("--page", type=int, default=1, help="search page (rotate deeper each round)")
    ap.add_argument("--max", type=int, default=200, help="cap on new entries this run")
    ap.add_argument("--append", action="store_true",
                    help="append into the registry (registry/jrc.yaml)")
    args = ap.parse_args()

    urls, titles, reg_ids = registry.existing_keys()
    session = requests.Session()
    out, seen_handles = [], set()
    for term, topic in QUERIES:
        if len(out) >= args.max:
            break
        try:
            hits = from_openaire(term, args.rows, args.page)
        except Exception as e:
            print(f"# openaire jrc '{term}' p{args.page} failed: {e}", file=sys.stderr)
            continue
        for title, handle in hits:
            if len(out) >= args.max:
                break
            t = registry.norm(title)
            if t in titles or handle in seen_handles:
                continue
            seen_handles.add(handle)
            pdf = resolve_bitstream(session, handle)
            time.sleep(0.6)  # politeness on the handle host
            if not pdf or pdf.rstrip("/") in urls:
                continue
            urls.add(pdf.rstrip("/"))
            titles.add(t)
            out.append({"id": f"jrc-{registry.slug(title)[:46]}", "title": title.strip()[:150],
                        "url": pdf, "source": "jrc", "license": "cc-by",
                        "topic": topic, "format": "pdf"})
        time.sleep(1.0)

    registry.uniquify_ids(out, reg_ids)

    by_topic: dict = {}
    for h in out:
        by_topic[h["topic"]] = by_topic.get(h["topic"], 0) + 1
    print(f"# {len(out)} NEW JRC reports (cc-by, OP-prefix DOI + bitstream verified, "
          f"deduped vs manifest + registry)")
    print(f"# by topic: {by_topic}")
    print("# --- review, then --append, then scripts/build_corpus.py ---")
    print(yaml.safe_dump(out, sort_keys=False, allow_unicode=True))

    if args.append and out:
        counts = registry.append_entries(out)
        print(f"# appended {len(out)} entries to the registry: {counts}", file=sys.stderr)


if __name__ == "__main__":
    main()
