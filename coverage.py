#!/usr/bin/env python3
"""coverage.py — what the corpus HAS vs. what it's MISSING, straight from the manifest.

The mission is to find *all* open building-energy data, so the agent needs a live denominator:
which KINDS of data we already hold and which are gaps. This reads manifest.jsonl, buckets every
fetched doc into a genre (the "universe" of building-energy data below), and prints have-vs-target
with the gaps flagged. It is self-updating: run it after every load and the numbers refresh. A
source it does not recognize is reported as "uncategorized" so the map can't silently drift -- add
that source to SOURCE_GENRE (one line) when it shows up.

    python coverage.py            # print the coverage table (gaps first)
    python coverage.py --sources  # also dump the raw per-source counts
"""
from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path

HERE = Path(__file__).resolve().parent
MANIFEST = HERE / "manifest.jsonl"

# The UNIVERSE of building-energy data kinds (the target list). Curated + slowly grown: add a row
# only when a genuinely new KIND of source appears. The "have" count is computed, never hand-typed.
GENRES = [
    ("research_papers",      "arXiv · OpenAlex · OA journals"),
    ("us_gov_lab_reports",   "OSTI · NREL · PNNL · LBNL · ORNL · DOE/FEMP"),
    ("international_bodies",  "IEA EBC · EU JRC · IPEEC · REHVA"),
    ("codes_standards",      "IECC · ASHRAE 90.1 / G36 · Title 24 · ISO/EN"),
    ("public_datasets",      "ResStock/ComStock · OpenEI · EIA CBECS/RECS · Bldg Data Genome"),
    ("equipment_mfr_docs",   "Carrier / Trane / Daikin / JCI manuals (proprietary → pointer-only)"),
    ("software_sim_docs",    "EnergyPlus · OpenStudio · Modelica · DOE-2 · VOLTTRON"),
    ("ontology_dataspec",    "Brick · Haystack · ASHRAE 223P · BACnet · gbXML · IFC"),
    ("practitioner_qa",      "Unmet Hours · HVAC-Talk · StackExchange"),
    ("encyclopedic",         "Wikipedia · glossaries"),
    ("industry_ngo_utility", "ACEEE · RMI · USGBC/LEED · utility programs"),
    ("patents",              "Google Patents · USPTO (public-domain text)"),
]

# source bucket -> genre. Add a line whenever coverage.py reports an uncategorized source.
SOURCE_GENRE = {
    "arxiv": "research_papers", "openalex": "research_papers", "academic": "research_papers",
    "osti": "us_gov_lab_reports", "gov_osti": "us_gov_lab_reports", "gov_pnnl": "us_gov_lab_reports",
    "gov_lbnl": "us_gov_lab_reports", "gov_doe": "us_gov_lab_reports", "gov_ca": "us_gov_lab_reports",
    "nist": "us_gov_lab_reports", "gsa": "us_gov_lab_reports", "naseo": "us_gov_lab_reports",
    "calnext": "us_gov_lab_reports", "wbdg": "us_gov_lab_reports", "energystar": "us_gov_lab_reports",
    "web": "us_gov_lab_reports", "eere": "us_gov_lab_reports",
    "iea_ebc": "international_bodies", "eu_jrc": "international_bodies", "iea": "international_bodies",
    "ashrae": "codes_standards", "energycodes": "codes_standards",
    "eia": "public_datasets", "openei": "public_datasets", "datasets": "public_datasets",
    "volttron": "software_sim_docs", "modelica_buildings": "software_sim_docs", "docs": "software_sim_docs",
    "brick": "ontology_dataspec", "haystack": "ontology_dataspec", "open223": "ontology_dataspec",
    "unmethours": "practitioner_qa",
    "wikipedia": "encyclopedic",
    "aceee": "industry_ngo_utility",
    "patents": "patents",
}


def status(n: int) -> str:
    if n == 0:
        return "NONE  ← gap"
    if n < 16:
        return "thin  ← grow"
    if n < 61:
        return "some"
    return "heavy"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sources", action="store_true", help="also dump raw per-source counts")
    args = ap.parse_args()

    rows = [json.loads(l) for l in MANIFEST.read_text().splitlines() if l.strip()]
    ok = [r for r in rows if r.get("status") == "ok"]
    by_source = Counter(r.get("source") for r in ok)

    counts = {g: 0 for g, _ in GENRES}
    uncategorized: Counter = Counter()
    for src, n in by_source.items():
        g = SOURCE_GENRE.get(src)
        if g:
            counts[g] += n
        else:
            uncategorized[src] += n

    print(f"corpus coverage — {len(ok)} docs across {len(GENRES)} target genres\n")
    print(f"  {'genre':22} {'have':>5}  {'status':14} where it lives")
    print("  " + "-" * 92)
    # gaps first: NONE, then thin, then the well-covered ones
    order = sorted(GENRES, key=lambda gh: (counts[gh[0]] >= 16, counts[gh[0]] >= 61, counts[gh[0]]))
    for g, hint in order:
        print(f"  {g:22} {counts[g]:>5}  {status(counts[g]):14} {hint}")

    gaps = [g for g, _ in GENRES if counts[g] == 0]
    thin = [g for g, _ in GENRES if 0 < counts[g] < 16]
    print()
    if gaps:
        print(f"  MISSING entirely : {', '.join(gaps)}")
    if thin:
        print(f"  thin (grow next) : {', '.join(thin)}")
    if uncategorized:
        print(f"  ⚠ uncategorized sources (add to SOURCE_GENRE): {dict(uncategorized)}")

    if args.sources:
        print("\nby source:")
        for s, n in by_source.most_common():
            print(f"  {s:20} {n:>4}  -> {SOURCE_GENRE.get(s, '??')}")


if __name__ == "__main__":
    main()
