#!/usr/bin/env python3
"""find_osti.py — deep-harvest US DOE / national-lab reports from OSTI (corpus growth at SCALE).

OSTI (osti.gov) indexes 200k+ PUBLIC-DOMAIN records per built-environment query and paginates cleanly,
so it's the scalable, reachable, reliable vein for volume (the path toward ~1B tokens). This backend
paginates the OSTI API across many built-env subjects, keeps records with a downloadable full-text
(`/servlets/purl/<id>`) link, dedups vs the registry, and appends entries to registry/reports.yaml (ost- ids,
public-domain, discovered → pruned normally).

The marathon rotates --page each round to harvest DEEPER; prune's DOMAIN gate drops the DOE-science
tail (fusion / bio / HEP …) that isn't built-environment.

    python scripts/find_osti.py                                    # propose (page 1)
    python scripts/find_osti.py --rows 50 --pages 2 --page 1 --max 400 --append
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
API = "https://www.osti.gov/api/v1/records"

# built-environment subjects (OSTI is DOE-science-heavy, so prune trims the off-topic tail).
QUERIES = [
    ("building energy efficiency", "building_energy"), ("building envelope thermal", "building_energy"),
    ("commercial building energy", "building_energy"), ("residential building energy", "building_energy"),
    ("net zero energy building", "building_energy"), ("building retrofit weatherization", "building_energy"),
    ("building simulation EnergyPlus", "building_energy"), ("moisture building envelope", "building_energy"),
    ("indoor air quality ventilation", "building_energy"), ("occupant thermal comfort", "building_energy"),
    ("HVAC system performance", "equipment_systems"), ("heat pump building", "equipment_systems"),
    ("chiller boiler efficiency", "equipment_systems"), ("thermal energy storage", "equipment_systems"),
    ("district heating cooling", "building_energy"), ("geothermal heat pump", "equipment_systems"),
    ("refrigeration systems", "equipment_systems"), ("combined heat power cogeneration", "equipment_systems"),
    ("energy recovery ventilation", "equipment_systems"), ("variable refrigerant flow", "equipment_systems"),
    ("building controls automation", "controls_bas"), ("grid interactive efficient buildings", "controls_bas"),
    ("model predictive control building", "controls_bas"), ("sensors controls buildings", "controls_bas"),
    ("data center cooling efficiency", "controls_bas"), ("demand response buildings", "controls_bas"),
    ("fault detection diagnostics building", "commissioning_fdd"), ("building commissioning", "commissioning_fdd"),
    ("monitoring based commissioning", "commissioning_fdd"), ("chiller plant optimization", "commissioning_fdd"),
    ("building materials durability", "materials"), ("concrete performance", "materials"),
    ("corrosion reinforcement", "materials"), ("advanced building materials", "materials"),
    ("structural engineering seismic", "structures_civil"), ("geotechnical foundation", "structures_civil"),
    ("bridge infrastructure", "structures_civil"), ("wind engineering structures", "structures_civil"),
    ("construction technology", "construction"), ("infrastructure resilience", "infrastructure"),
    ("water treatment infrastructure", "infrastructure"), ("pavement asphalt", "infrastructure"),
    ("solar photovoltaic building", "building_energy"), ("energy storage battery building", "building_energy"),
    ("lighting energy efficiency", "building_energy"), ("windows glazing energy", "equipment_systems"),
    ("energy codes standards buildings", "standards_protocols"), ("life cycle assessment building", "standards_protocols"),
    ("measurement verification savings", "standards_protocols"), ("fire safety buildings", "architecture"),
    ("urban energy systems", "urban"), ("electrification buildings decarbonization", "building_energy"),
]


def from_osti(term: str, rows: int, page: int):
    r = requests.get(API, params={"q": term, "rows": rows, "page": page}, timeout=45)
    r.raise_for_status()
    out = []
    for rec in r.json():
        oid = rec.get("osti_id") or rec.get("id")
        ft = any("purl" in (l.get("href") or "") or l.get("rel") == "fulltext"
                 for l in rec.get("links", []))
        if oid and ft:
            out.append((rec.get("title"), f"https://www.osti.gov/servlets/purl/{oid}"))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=50, help="records per page")
    ap.add_argument("--pages", type=int, default=2, help="pages per subject per run")
    ap.add_argument("--page", type=int, default=1, help="starting page (marathon rotates deeper each round)")
    ap.add_argument("--max", type=int, default=400, help="cap new records appended per run")
    ap.add_argument("--append", action="store_true")
    args = ap.parse_args()

    urls, titles, reg_ids = registry.existing_keys()
    out, seen = [], set()
    for term, topic in QUERIES:
        if len(out) >= args.max:
            break
        for p in range(args.page, args.page + args.pages):
            if len(out) >= args.max:
                break
            try:
                hits = from_osti(term, args.rows, p)
            except Exception as e:
                print(f"# osti '{term}' p{p} failed: {e}", file=sys.stderr)
                break
            if not hits:
                break  # no more pages for this subject
            for title, url in hits:
                if len(out) >= args.max:
                    break
                if not title:
                    continue
                u, t = url.rstrip("/"), registry.norm(title)
                if u in urls or t in titles or u in seen:
                    continue
                seen.add(u)
                out.append({"id": f"ost-{registry.slug(title)[:46]}", "title": title.strip()[:150], "url": url,
                            "source": "osti", "license": "public-domain", "topic": topic, "format": "pdf"})
            time.sleep(0.3)

    registry.uniquify_ids(out, reg_ids)

    by_topic: dict = {}
    for h in out:
        by_topic[h["topic"]] = by_topic.get(h["topic"], 0) + 1
    print(f"# {len(out)} NEW OSTI full-text records (public-domain, deduped vs manifest + registry)")
    print(f"# by topic: {by_topic}")
    print("# --- review, then --append, then scripts/build_corpus.py ---")
    print(yaml.safe_dump(out, sort_keys=False, allow_unicode=True))

    if args.append and out:
        counts = registry.append_entries(out)
        print(f"# appended {len(out)} entries to the registry: {counts}", file=sys.stderr)


if __name__ == "__main__":
    main()
