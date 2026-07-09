#!/usr/bin/env python3
"""find_openaire.py — EU Horizon/H2020 project deliverables via the OpenAIRE Graph API.

Thousands of open-access built-environment PROJECT DELIVERABLES (retrofit case studies, pilot
reports, design guides) sit behind CORDIS/OpenAIRE — grey literature that never reaches OpenAlex
or OSTI. This backend searches OpenAIRE for `instanceType=Project deliverable` records, keeps
OPEN-access ones whose instance URL is the EC participant-portal `downloadPublic` link (a stable
public URL; build_corpus.py knows how to follow its JS interstitial), dedups vs the registry +
blocklist, and appends to registry/deliverables.yaml (eud- ids → pruned normally).

Rotate --page deeper each round like find_osti. Unauthenticated OpenAIRE allows ~60 req/h, so
keep queries × pages modest.

    python scripts/find_openaire.py                        # propose (page 1)
    python scripts/find_openaire.py --rows 50 --page 2 --max 300 --append
"""
from __future__ import annotations

import argparse
import html
import sys
import time

import requests
import yaml

import registry

API = "https://api.openaire.eu/graph/v1/researchProducts"
EC_DL = "ec.europa.eu/research/participants/documents/downloadPublic"

QUERIES = [
    ("building energy retrofit", "building_energy"),
    ("energy performance buildings", "building_energy"),
    ("deep renovation building", "building_energy"),
    ("nearly zero energy building", "building_energy"),
    ("building stock decarbonisation", "building_energy"),
    ("smart building energy management", "controls_bas"),
    ("building energy flexibility demand response", "controls_bas"),
    ("heat pump heating cooling", "equipment_systems"),
    ("district heating cooling network", "building_energy"),
    ("thermal energy storage building", "equipment_systems"),
    ("indoor environmental quality comfort", "building_energy"),
    ("building information modelling construction", "construction"),
    ("construction materials circular", "materials"),
    ("positive energy district", "urban"),
]


def from_openaire(term: str, rows: int, page: int) -> list[tuple[str, str]]:
    r = requests.get(API, params={"search": term, "type": "publication",
                                  "instanceType": "Project deliverable",
                                  "pageSize": rows, "page": page}, timeout=45)
    r.raise_for_status()
    out = []
    for rec in r.json().get("results") or []:
        if ((rec.get("bestAccessRight") or {}).get("label")) != "OPEN":
            continue
        title = rec.get("mainTitle")
        url = next((html.unescape(u) for inst in rec.get("instances") or []
                    for u in inst.get("urls") or [] if EC_DL in u), None)
        if title and url:
            out.append((title, url))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=50, help="records per query page")
    ap.add_argument("--page", type=int, default=1, help="search page (rotate deeper each round)")
    ap.add_argument("--max", type=int, default=300, help="cap new records appended per run")
    ap.add_argument("--append", action="store_true")
    args = ap.parse_args()

    urls, titles, reg_ids = registry.existing_keys()
    out, seen = [], set()
    for term, topic in QUERIES:
        if len(out) >= args.max:
            break
        try:
            hits = from_openaire(term, args.rows, args.page)
        except Exception as e:
            print(f"# openaire '{term}' p{args.page} failed: {e}", file=sys.stderr)
            continue
        for title, url in hits:
            if len(out) >= args.max:
                break
            u, t = url.rstrip("/"), registry.norm(title)
            if u in urls or t in titles or u in seen:
                continue
            seen.add(u)
            titles.add(t)
            out.append({"id": f"eud-{registry.slug(title)[:46]}", "title": title.strip()[:150],
                        "url": url, "source": "openaire_deliverable", "license": "open",
                        "topic": topic, "format": "pdf"})
        time.sleep(1.0)

    registry.uniquify_ids(out, reg_ids)

    by_topic: dict = {}
    for h in out:
        by_topic[h["topic"]] = by_topic.get(h["topic"], 0) + 1
    print(f"# {len(out)} NEW EU project deliverables (open, deduped vs manifest + registry)")
    print(f"# by topic: {by_topic}")
    print("# --- review, then --append, then scripts/build_corpus.py ---")
    print(yaml.safe_dump(out, sort_keys=False, allow_unicode=True))

    if args.append and out:
        counts = registry.append_entries(out)
        print(f"# appended {len(out)} entries to the registry: {counts}", file=sys.stderr)


if __name__ == "__main__":
    main()
