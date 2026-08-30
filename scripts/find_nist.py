#!/usr/bin/env python3
"""find_nist.py — enumerate NIST/NBS Technical Series publications via the Crossref API.

NIST (and its predecessor, the National Bureau of Standards) mints DOIs under the Crossref
prefix 10.6028 for its whole technical-report series: Special Publications, Interagency/Internal
Reports, Building Science Series, Letter Circulars, Monographs, ... Decades of public-domain US
gov fire research, structures, and building-science reports — dense technical prose, direct PDFs,
unambiguously redistributable (public-domain).

Queries `api.crossref.org/works?filter=prefix:10.6028&query.bibliographic=<topic>`, paginates by
plain `offset` (Crossref supports it below ~10k), and keeps only items whose
`resource.primary.URL` is an nvlpubs.nist.gov PDF (the direct full-text link; anything else —
DOI-only stubs, non-NIST hosts — is skipped). Dedups against the registry / manifest / pruned-URL
blocklist and appends `nst-` entries to registry/nist.yaml (routed there by scripts/registry.py).

    python scripts/find_nist.py                              # propose (page 1)
    python scripts/find_nist.py --rows 50 --page 2 --max 400 --append
"""
from __future__ import annotations

import argparse
import re
import sys
import time

import requests
import yaml

import registry

API = "https://api.crossref.org/works"
MAILTO = "nekaise-corpus@example.org"

# (bibliographic search term -> topic). Crossref full-text-searches title/subject/abstract, but
# treats multi-word searches fuzzily: even "building code" can return character-encoding reports.
# Wave 3 therefore combines building-anchored queries with a conservative title gate below.
# Wave 1 covered broad fire/structural/building/HVAC terms; wave 2 covered materials, acoustics,
# moisture, and component-level HVAC. Both reached the deep-offset/dry tail by 2026-08.
QUERY_WAVE = 3
QUERIES = [
    ("building commissioning", "commissioning_fdd"),
    ("HVAC commissioning", "commissioning_fdd"),
    ("fault detection diagnosis HVAC", "commissioning_fdd"),
    ("air handling unit fault detection", "commissioning_fdd"),
    ("BACnet building automation", "standards_protocols"),
    ("building controls interoperability", "standards_protocols"),
    ("building automation controls", "controls_bas"),
    ("commercial building HVAC controls", "controls_bas"),
    ("ventilation control buildings", "building_energy"),
    ("indoor air quality buildings ventilation", "building_energy"),
    ("building code compliance", "standards_protocols"),
    ("seismic building code", "standards_protocols"),
    ("heat pump controls building", "equipment_systems"),
    ("grid interactive efficient buildings", "controls_bas"),
    ("smart building sensors", "controls_bas"),
]

# Crossref's relevance ranking alone is not a domain boundary. Require a title to name an
# unambiguous wave-3 concept (or a meaningful conjunction) before spending a download slot.
# The conjunctions avoid false positives observed in the page-1 review: stochastic "continuous"
# processes, generic IT interoperability/security reports, and punched-card character codes.
TITLE_ANCHOR = re.compile(
    r"commission|\bhvac\b|\bbacnet\b|heat[ -]?pump|"
    r"(?=.*(?:fault|diagnos))(?=.*(?:\bhvac\b|air[ -]?handl|build))|"
    r"(?=.*ventilat)(?=.*(?:build|house|attic|indoor|smoke|fire|pressure|natural|manufactur))|"
    r"(?=.*(?:code|standard))(?=.*(?:build|seismic|structur|fire|energy))|"
    r"(?=.*build)(?=.*(?:automation|control|interoperab|sensor|grid[ -]?interactive|cyber))",
    re.I,
)


def title_in_scope(title: str) -> bool:
    return bool(TITLE_ANCHOR.search(title or ""))


def from_crossref(term: str, rows: int, offset: int):
    r = requests.get(API, params={
        "filter": "prefix:10.6028", "query.bibliographic": term,
        "rows": rows, "offset": offset, "mailto": MAILTO}, timeout=45)
    r.raise_for_status()
    out = []
    for it in r.json().get("message", {}).get("items", []):
        titles = it.get("title") or []
        title = titles[0] if titles else None
        url = ((it.get("resource") or {}).get("primary") or {}).get("URL")
        if not title or not url:
            continue
        if "nvlpubs.nist.gov" not in url.lower() or not url.lower().endswith(".pdf"):
            continue
        if not title_in_scope(title):
            continue
        out.append((title.strip(), url))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=50, help="records per query page")
    ap.add_argument("--page", type=int, default=1, help="search page (rotate deeper each round)")
    ap.add_argument("--max", type=int, default=400, help="cap on new entries this run")
    ap.add_argument("--append", action="store_true", help="append into the registry (registry/nist.yaml)")
    args = ap.parse_args()

    urls, titles, reg_ids = registry.existing_keys()
    out, seen = [], set()
    failed_request = None
    for term, topic in QUERIES:
        if len(out) >= args.max:
            break
        offset = (args.page - 1) * args.rows
        try:
            hits = from_crossref(term, args.rows, offset)
        except Exception as e:
            print(f"# crossref '{term}' p{args.page} failed: {e}", file=sys.stderr)
            failed_request = (term, args.page)
            break
        for title, url in hits:
            if len(out) >= args.max:
                break
            u, t = url.rstrip("/"), registry.norm(title)
            if u in urls or t in titles or u in seen:
                continue
            seen.add(u)
            titles.add(t)
            out.append({"id": f"nst-{registry.slug(title)[:52]}", "title": title[:150],
                        "url": url, "source": "nist_crossref", "license": "public-domain",
                        "topic": topic, "format": "pdf"})
        time.sleep(0.5)

    if failed_request:
        term, page = failed_request
        print(
            f"# ERROR: NIST discovery incomplete at '{term}' p{page}; "
            "refusing a partial append so rotation does not advance",
            file=sys.stderr,
        )
        raise SystemExit(1)

    registry.uniquify_ids(out, reg_ids)

    by_topic: dict = {}
    for h in out:
        by_topic[h["topic"]] = by_topic.get(h["topic"], 0) + 1
    print(f"# {len(out)} NEW NIST/NBS technical series PDFs (page {args.page}; public-domain, "
          f"deduped vs manifest + registry + blocklist)")
    print(f"# by topic: {by_topic}")
    print("# --- review, then --append, then scripts/build_corpus.py ---")
    print(yaml.safe_dump(out, sort_keys=False, allow_unicode=True))

    if args.append and out:
        counts = registry.append_entries(out)
        print(f"# appended {len(out)} entries to the registry: {counts}", file=sys.stderr)


if __name__ == "__main__":
    main()
