#!/usr/bin/env python3
"""find_zenodo.py — discover open, CC-licensed, PDF-bearing publications on Zenodo.

Zenodo (zenodo.org) is CERN's general-purpose open-research repository — conference papers,
theses, project reports, and grey literature that OpenAlex/OSTI/arXiv don't index, much of it
explicitly CC-BY / CC0. This backend queries the anonymous REST API (`/api/records`) per
built-environment term, keeps `publication`-type records with `access_right: open`, filters by
license CLIENT-SIDE (the API's own access_right filter says nothing about the license terms), picks
the first `.pdf` file's direct-download link, dedups vs the registry + blocklist, and appends
`zen-` entries to registry/zenodo.yaml (discovered → pruned normally).

Rate limit (anonymous, no API key): `size` is hard-capped at 25 per page (400 above that), and the
window allows ~30 requests (see the `X-RateLimit-*` response headers) — this backend sleeps 2s
between requests and backs off 60s on an HTTP 429 before one retry. Keep --max modest; rotate
--page deeper each round like find_osti / find_openaire.

    python scripts/find_zenodo.py                            # propose (page 1)
    python scripts/find_zenodo.py --rows 25 --page 2 --max 150 --append
"""
from __future__ import annotations

import argparse
import html
import sys
import time

import requests
import yaml

import registry

API = "https://zenodo.org/api/records"
UA = {"User-Agent": "nekaise-corpus/find_zenodo"}

# (query term, topic). Multi-word terms are quoted in the q param. Covers every AGENTS.md topic;
# the prune DOMAIN gate is the real relevance filter for anything that slips in off-topic.
QUERIES = [
    ("building energy simulation", "building_energy"),
    ("district heating", "building_energy"),
    ("building retrofit", "building_energy"),
    ("net zero energy building", "building_energy"),
    ("building envelope thermal performance", "building_energy"),
    ("energy efficient buildings", "building_energy"),
    ("heat pump", "equipment_systems"),
    ("chiller efficiency", "equipment_systems"),
    ("boiler performance", "equipment_systems"),
    ("thermal energy storage", "equipment_systems"),
    ("ventilation system", "equipment_systems"),
    ("HVAC control", "controls_bas"),
    ("building automation system", "controls_bas"),
    ("model predictive control building", "controls_bas"),
    ("smart building energy management", "controls_bas"),
    ("building commissioning", "commissioning_fdd"),
    ("fault detection diagnostics HVAC", "commissioning_fdd"),
    ("building energy code", "standards_protocols"),
    ("life cycle assessment building", "standards_protocols"),
    ("structural health monitoring", "structures_civil"),
    ("earthquake engineering", "structures_civil"),
    ("bridge structural design", "structures_civil"),
    ("geotechnical engineering", "structures_civil"),
    ("building information modeling", "construction"),
    ("construction management", "construction"),
    ("modular construction", "construction"),
    ("concrete durability", "materials"),
    ("sustainable building materials", "materials"),
    ("timber construction materials", "materials"),
    ("architectural design sustainability", "architecture"),
    ("building facade design", "architecture"),
    ("pavement engineering", "infrastructure"),
    ("water infrastructure", "infrastructure"),
    ("urban infrastructure resilience", "infrastructure"),
    ("urban heat island", "urban"),
    ("smart city energy", "urban"),
    ("urban planning sustainability", "urban"),
]


def map_license(lic: dict | None) -> str | None:
    """CC-BY* / CC-BY-SA* / CC0* only (client-side — the API's access_right says nothing about
    license terms). NC and ND variants are excluded even though access_right may say 'open'."""
    lid = ((lic or {}).get("id") or "").lower()
    if not lid:
        return None
    if lid.startswith("cc-by-nc") or lid.startswith("cc-by-nd"):
        return None
    if lid.startswith("cc-by-sa"):
        return "cc-by-sa"
    if lid.startswith("cc-by"):
        return "cc-by"
    if lid.startswith("cc0") or "public-domain" in lid or "publicdomain" in lid:
        return "cc0"
    return None


def q_param(term: str) -> str:
    return f'"{term}"' if " " in term else term


def fetch(term: str, rows: int, page: int, session: requests.Session) -> list[dict]:
    params = {"q": q_param(term), "type": "publication", "access_right": "open",
              "size": rows, "page": page}
    for attempt in range(2):
        try:
            r = session.get(API, params=params, headers=UA, timeout=45)
        except Exception as e:
            print(f"# zenodo '{term}' failed: {e}", file=sys.stderr)
            return []
        if r.status_code == 429:
            print(f"# zenodo '{term}' rate-limited (429) — backing off 60s", file=sys.stderr)
            time.sleep(60)
            continue
        try:
            r.raise_for_status()
        except Exception as e:
            print(f"# zenodo '{term}' failed: {e}", file=sys.stderr)
            return []
        return r.json().get("hits", {}).get("hits") or []
    return []


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=25, help="results per query page (anonymous API hard-caps at 25)")
    ap.add_argument("--page", type=int, default=1, help="search page (rotate deeper each round)")
    ap.add_argument("--max", type=int, default=150, help="cap on new entries this run (API is rate-limited — keep modest)")
    ap.add_argument("--append", action="store_true", help="append into the registry (registry/zenodo.yaml)")
    args = ap.parse_args()
    args.rows = min(args.rows, 25)

    urls, titles, reg_ids = registry.existing_keys()
    out = []
    session = requests.Session()
    for term, topic in QUERIES:
        if len(out) >= args.max:
            break
        hits = fetch(term, args.rows, args.page, session)
        time.sleep(2)  # politeness — anonymous window is ~30 req
        for rec in hits:
            if len(out) >= args.max:
                break
            meta = rec.get("metadata") or {}
            if meta.get("access_right") != "open":
                continue
            tag = map_license(meta.get("license"))
            if not tag:
                continue
            title = html.unescape((meta.get("title") or "").strip())
            if not title:
                continue
            files = rec.get("files") or []
            pdf = next((f for f in files if (f.get("key") or "").lower().endswith(".pdf")), None)
            if not pdf:
                continue
            url = ((pdf.get("links") or {}).get("self") or "").strip()
            if not url:
                continue
            u, t = url.rstrip("/"), registry.norm(title)
            if u in urls or t in titles:
                continue
            urls.add(u)
            titles.add(t)
            out.append({"id": f"zen-{registry.slug(title)[:52]}", "title": title[:150],
                        "url": url, "source": "zenodo", "license": tag,
                        "topic": topic, "format": "pdf"})

    registry.uniquify_ids(out, reg_ids)

    by_lic: dict = {}
    for h in out:
        by_lic[h["license"]] = by_lic.get(h["license"], 0) + 1
    print(f"# {len(out)} NEW open CC-licensed Zenodo publications (page {args.page}; deduped vs "
          f"manifest + registry + blocklist)")
    print(f"# by license: {by_lic}")
    print("# --- review, then --append, then scripts/build_corpus.py ---")
    print(yaml.safe_dump(out, sort_keys=False, allow_unicode=True))

    if args.append and out:
        counts = registry.append_entries(out)
        print(f"# appended {len(out)} entries to the registry: {counts}", file=sys.stderr)


if __name__ == "__main__":
    main()
