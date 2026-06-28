#!/usr/bin/env python3
"""find_sources.py — discover open-access building-energy sources via OpenAlex (corpus growth).

Programmatic enlargement: queries the free OpenAlex API for OPEN-ACCESS works across the corpus
topics, keeps those with a fetchable PDF and a usable license, dedups against the current
manifest + registry, and PROPOSES ready-to-paste `sources.yaml` entries. It does NOT auto-append
-- a human or agent reviews the candidates, pastes the good ones under `sources:` in sources.yaml,
then runs the loader (`build_corpus.py`) to fetch + verify.

    python find_sources.py --per 15

OpenAlex is free and needs no API key (we send a mailto for the polite pool).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

import requests
import yaml

HERE = Path(__file__).resolve().parent
API = "https://api.openalex.org/works"
MAILTO = "corpus@opennekaise.org"

# (OpenAlex search term -> our corpus topic)
QUERIES = [
    ("HVAC control sequences building automation BACnet", "controls_bas"),
    ("building fault detection diagnostics commissioning HVAC", "commissioning_fdd"),
    ("chiller heat pump air handling unit performance HVAC", "equipment_systems"),
    ("building energy modeling EnergyPlus retrofit envelope efficiency", "building_energy"),
    ("building data model ontology Brick Haystack semantic interoperability", "standards_protocols"),
]
PERMISSIVE = {"cc-by", "cc-by-sa", "cc0", "public-domain"}


def norm(s: str) -> str:
    return re.sub(r"\W+", " ", (s or "").lower()).strip()


def slug(s: str) -> str:
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", (s or "").lower())).strip("-")


def existing_keys():
    urls, titles = set(), set()
    mp = HERE / "manifest.jsonl"
    if mp.exists():
        for line in mp.read_text().splitlines():
            if line.strip():
                r = json.loads(line)
                urls.add((r.get("url") or "").rstrip("/"))
                titles.add(norm(r.get("title")))
    try:
        for s in yaml.safe_load((HERE / "sources.yaml").read_text())["sources"]:
            urls.add((s.get("url") or "").rstrip("/"))
    except Exception:
        pass
    return urls, titles


def search(term: str, topic: str, per: int) -> list[dict]:
    params = {"search": term, "filter": "open_access.is_oa:true,type:article",
              "per-page": per, "sort": "cited_by_count:desc", "mailto": MAILTO}
    r = requests.get(API, params=params, timeout=30)
    r.raise_for_status()
    out = []
    for w in r.json().get("results", []):
        loc = w.get("best_oa_location") or {}
        pdf = loc.get("pdf_url")
        if not pdf:
            continue
        lic = (loc.get("license") or "").lower()
        tag = lic if lic in PERMISSIVE else ("open" if (lic or w.get("open_access", {}).get("is_oa")) else None)
        if not tag:
            continue
        title = (w.get("title") or w.get("display_name") or "").strip()
        if not title:
            continue
        out.append({"id": f"oa-{slug(title)[:48]}", "title": title[:150], "url": pdf,
                    "source": "openalex", "license": tag, "topic": topic, "format": "pdf"})
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per", type=int, default=15, help="results per topic query")
    args = ap.parse_args()

    urls, titles = existing_keys()
    out, seen = [], set()
    for term, topic in QUERIES:
        try:
            hits = search(term, topic, args.per)
        except Exception as e:
            print(f"# query [{topic}] failed: {e}", file=sys.stderr)
            continue
        for h in hits:
            u, t = h["url"].rstrip("/"), norm(h["title"])
            if u in urls or t in titles or u in seen:
                continue
            seen.add(u)
            out.append(h)

    by_topic = {}
    for h in out:
        by_topic[h["topic"]] = by_topic.get(h["topic"], 0) + 1
    by_lic = {l: sum(1 for h in out if h["license"] == l) for l in sorted({h["license"] for h in out})}
    print(f"# {len(out)} NEW open-access candidates (deduped vs manifest + sources.yaml)")
    print(f"# by topic:   {by_topic}")
    print(f"# by license: {by_lic}")
    print("# --- review, paste accepted entries under `sources:` in sources.yaml, run build_corpus.py ---")
    print(yaml.safe_dump(out, sort_keys=False, allow_unicode=True))


if __name__ == "__main__":
    main()
