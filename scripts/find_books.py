#!/usr/bin/env python3
"""find_books.py — discover open-access CC-BY(-SA)/CC0 books from OAPEN (corpus growth).

OAPEN (library.oapen.org) hosts ~40k open-access academic BOOKS and aggregates DOAB / Springer OA /
Frontiers / hundreds of publishers, HOSTING the PDFs itself (unlike DOAB, whose download links are
external; MDPI Books are Cloudflare-blocked from most hosts). Built-environment / AEC subjects are a
dense, cleanly-licensed vein (whole books, hundreds of pages of prose = CPT gold).

This backend PAGINATES the OAPEN REST search across many built-env subjects (offset 0, per, 2·per, …
up to --depth), keeps English books with a direct PDF bitstream whose license is CC-BY / CC-BY-SA /
CC0, and inserts ready-to-load registry entries.

License is NOT in the REST metadata — it lives in the OAI-PMH `xoai` record as a
creativecommons.org/licenses/<code> URL. We keep ONLY `by` / `by-sa` / `publicdomain/zero`; ANY `nc`
or `nd` marker rejects the record (fail-closed). Dedup is checked BEFORE the xoai call, so re-runs
paginate deeper cheaply (only genuinely-new candidates cost an xoai lookup).

Entries use the `oer-` id prefix and land in registry/books.yaml; prune_corpus.py quality-gates
them (length-scaled density rule) like every machine-discovered source.

    python scripts/find_books.py                                   # propose
    python scripts/find_books.py --append                          # insert (then load + prune)
    python scripts/find_books.py --per 25 --depth 150 --max 200 --append
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

import requests
import yaml

import registry

HERE = Path(__file__).resolve().parents[1]  # repo root (this file lives in scripts/)
SEARCH = "https://library.oapen.org/rest/search"
OAI = "https://library.oapen.org/oai/request"
UA = {"User-Agent": "nekaise-corpus/find_books"}

# (OAPEN subject term -> our corpus topic). Broad built-environment coverage; pagination goes deep.
QUERIES = [
    ("architecture design", "architecture"), ("architectural history", "architecture"),
    ("architectural theory", "architecture"), ("landscape architecture", "architecture"),
    ("heritage conservation", "architecture"), ("historic preservation", "architecture"),
    ("building acoustics", "architecture"), ("architectural acoustics", "architecture"),
    ("lighting design", "building_energy"), ("facade design", "architecture"),
    ("building envelope", "architecture"), ("fire safety engineering", "architecture"),
    ("housing design", "architecture"), ("interior architecture", "architecture"),
    ("building construction", "construction"), ("construction management", "construction"),
    ("construction technology", "construction"), ("building information modeling", "construction"),
    ("prefabrication building", "construction"), ("digital fabrication architecture", "construction"),
    ("construction robotics", "construction"), ("circular economy construction", "construction"),
    ("building materials", "materials"), ("concrete technology", "materials"),
    ("reinforced concrete", "structures_civil"), ("prestressed concrete", "structures_civil"),
    ("timber engineering", "materials"), ("mass timber", "materials"),
    ("masonry construction", "materials"), ("composite materials structures", "structures_civil"),
    ("steel structures", "structures_civil"), ("structural engineering", "structures_civil"),
    ("structural mechanics", "structures_civil"), ("structural dynamics", "structures_civil"),
    ("finite element analysis", "structures_civil"), ("structural health monitoring", "structures_civil"),
    ("earthquake engineering", "structures_civil"), ("seismic design", "structures_civil"),
    ("wind engineering", "structures_civil"), ("bridge engineering", "structures_civil"),
    ("bridge maintenance", "structures_civil"), ("tensile membrane structures", "structures_civil"),
    ("geotechnical engineering", "structures_civil"), ("soil mechanics", "structures_civil"),
    ("rock mechanics", "structures_civil"), ("foundation engineering", "structures_civil"),
    ("tunnelling", "structures_civil"), ("ground improvement", "structures_civil"),
    ("slope stability", "structures_civil"), ("landslides", "structures_civil"),
    ("soil dynamics", "structures_civil"), ("offshore engineering", "structures_civil"),
    ("building physics", "building_energy"), ("energy efficient buildings", "building_energy"),
    ("sustainable architecture", "building_energy"), ("green building", "building_energy"),
    ("daylighting", "building_energy"), ("thermal comfort", "building_energy"),
    ("ventilation indoor air", "building_energy"), ("passive house", "building_energy"),
    ("building retrofit", "building_energy"), ("zero energy building", "building_energy"),
    ("solar energy buildings", "building_energy"), ("renewable energy systems", "building_energy"),
    ("building services engineering", "equipment_systems"), ("heating cooling systems", "equipment_systems"),
    ("refrigeration", "equipment_systems"), ("thermal energy storage", "equipment_systems"),
    ("district heating", "building_energy"), ("heat pump", "equipment_systems"),
    ("urban planning", "urban"), ("urban design", "urban"), ("smart cities", "urban"),
    ("urban morphology", "urban"), ("urban climate", "urban"), ("urban heat island", "urban"),
    ("climate adaptation cities", "urban"), ("regional planning", "urban"),
    ("transport planning", "infrastructure"), ("traffic engineering", "infrastructure"),
    ("sustainable mobility", "infrastructure"), ("railway engineering", "infrastructure"),
    ("pavement engineering", "infrastructure"), ("asphalt materials", "infrastructure"),
    ("highway engineering", "infrastructure"), ("hydraulic engineering", "infrastructure"),
    ("water resources engineering", "infrastructure"), ("hydrology", "infrastructure"),
    ("coastal engineering", "infrastructure"), ("flood risk management", "infrastructure"),
    ("stormwater management", "infrastructure"), ("wastewater treatment", "infrastructure"),
    ("dam engineering", "infrastructure"), ("infrastructure resilience", "infrastructure"),
    ("disaster risk reduction", "infrastructure"), ("geodesy surveying", "infrastructure"),
    ("remote sensing", "infrastructure"), ("geographic information systems", "infrastructure"),
    ("life cycle assessment building", "standards_protocols"), ("embodied carbon", "standards_protocols"),
]


def pdf_link(item) -> str | None:
    for b in item.get("bitstreams") or []:
        name = (b.get("name") or "").lower()
        if b.get("mimeType") == "application/pdf" and name.endswith(".pdf") and b.get("retrieveLink"):
            return "https://library.oapen.org" + b["retrieveLink"]
    return None


def is_english_book(item) -> bool:
    md = item.get("metadata") or []
    typ = " ".join(m["value"] for m in md if m.get("key") == "dc.type").lower()
    lang = " ".join(m["value"] for m in md if m.get("key") == "dc.language").lower()
    return ("book" in typ) and (not lang or "english" in lang or lang.strip() in ("en", "eng"))


def license_of(handle: str) -> str | None:
    """cc-by / cc-by-sa / cc0 from the OAPEN xoai record, or None (fail-closed) — ANY nc/nd rejects."""
    try:
        r = requests.get(OAI, params={"verb": "GetRecord", "metadataPrefix": "xoai",
                                      "identifier": f"oai:library.oapen.org:{handle}"},
                         headers=UA, timeout=30)
        if r.status_code != 200:
            return None
        t = r.text
        if re.search(r"creativecommons\.org/publicdomain/zero", t):
            return "cc0"
        codes = set(re.findall(r"creativecommons\.org/licenses/([a-z-]+)", t))
        if not codes or any(("nc" in c or "nd" in c) for c in codes):
            return None
        if "by-sa" in codes:
            return "cc-by-sa"
        if "by" in codes:
            return "cc-by"
        return None
    except Exception:
        return None


def emit(e) -> str:
    e = {k: e[k] for k in ("id", "title", "url", "source", "license", "topic", "format")}
    d = yaml.safe_dump([e], sort_keys=False, allow_unicode=True)
    return "".join(("  " + ln + "\n") if ln else "\n" for ln in d.splitlines())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per", type=int, default=25, help="OAPEN page size")
    ap.add_argument("--offset", type=int, default=0,
                    help="starting offset (marathon rotates this per round to harvest OAPEN deeper — "
                         "the search endpoint is ~20s/call, so scan one page per subject per run)")
    ap.add_argument("--depth", type=int, default=25, help="results scanned per subject from --offset")
    ap.add_argument("--max", type=int, default=200, help="cap new books appended per run")
    ap.add_argument("--append", action="store_true")
    args = ap.parse_args()

    urls, titles, reg_ids = registry.existing_keys()
    out, seen = [], set()
    for term, topic in QUERIES:
        if len(out) >= args.max:
            break
        for offset in range(args.offset, args.offset + args.depth, args.per):
            if len(out) >= args.max:
                break
            try:
                r = requests.get(SEARCH, params={"query": term, "expand": "bitstreams,metadata",
                                                 "limit": args.per, "offset": offset},
                                 headers=UA, timeout=45)
                r.raise_for_status()
                items = r.json()
            except Exception as e:
                print(f"# search '{term}' @{offset} failed: {e}", file=sys.stderr)
                break
            if not items:
                break  # end of results for this subject
            for it in items:
                if len(out) >= args.max:
                    break
                title, url, handle = it.get("name"), pdf_link(it), it.get("handle")
                if not (title and url and handle):
                    continue
                u, t = url.rstrip("/"), registry.norm(title)
                if u in urls or t in titles or u in seen:
                    continue  # dedup BEFORE the (costly) license lookup
                if not is_english_book(it):
                    continue
                lic = license_of(handle)
                time.sleep(0.2)  # be polite to the OAI-PMH endpoint
                if not lic:
                    continue
                seen.add(u)
                out.append({"id": f"oer-{registry.slug(title)[:52]}", "title": title.strip()[:150], "url": url,
                            "source": "oapen", "license": lic, "topic": topic, "format": "pdf"})
            time.sleep(0.2)

    registry.uniquify_ids(out, reg_ids)

    by_lic: dict = {}
    for h in out:
        by_lic[h["license"]] = by_lic.get(h["license"], 0) + 1
    print(f"# {len(out)} NEW OAPEN books (English, CC-BY/-SA/0, deduped vs manifest + registry)")
    print(f"# by license: {by_lic}")
    print("# --- review, then --append (inserts ABOVE the discovered marker), then build_corpus.py ---")
    print(yaml.safe_dump(out, sort_keys=False, allow_unicode=True))

    if args.append and out:
        counts = registry.append_entries(out)
        print(f"# appended {len(out)} book entries to the registry: {counts}", file=sys.stderr)


if __name__ == "__main__":
    main()
