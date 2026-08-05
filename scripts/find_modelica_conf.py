#!/usr/bin/env python3
"""find_modelica_conf.py — enumerate Modelica Conference papers via the Crossref API.

The Modelica Association publishes the full proceedings of its conference series (International,
American, Asian, Japanese) through Linköping University Electronic Press under the Crossref prefix
10.3384. Every paper is open access with a direct PDF link — newer records (ecp.ep.liu.se, OJS)
carry an explicit CC-BY 4.0 license; older ones (ep.liu.se/ecp/...) predate LiU's license tagging
but are the same open-access series. This is the core literature of equation-based building /
HVAC / thermal-fluid simulation (Buildings library, AixLib, IDEAS, Spawn, FMI, ...).

Sweeps `api.crossref.org/works?filter=prefix:10.3384,type:proceedings-article` with
`query.container-title=modelica` (offset pagination; the series is ~1.2k papers, well under
Crossref's 10k offset ceiling), keeps items whose container-title actually names a Modelica
conference and whose link is a direct PDF, dedups against registry / manifest / blocklist, and
appends `mod-` entries to registry/modelica.yaml (routed by scripts/registry.py).

    python scripts/find_modelica_conf.py                 # propose (full sweep, print only)
    python scripts/find_modelica_conf.py --max 600 --append
"""
from __future__ import annotations

import argparse
import sys
import time

import requests
import yaml

import registry

API = "https://api.crossref.org/works"
MAILTO = "nekaise-corpus@example.org"
SELECT = "DOI,title,container-title,license,link,published"

# crude radar labels: topic never gates anything, but keeps coverage.py honest
_TOPIC_KEYS = [
    ("building_energy", ("building", "hvac", "heat pump", "district heat", "thermal comfort",
                         "energyplus", "spawn", "boptest", "ventilation", "chiller", "boiler")),
    ("controls_bas", ("control", "mpc", "fmi", "co-simulation", "cosimulation", "real-time",
                      "hardware-in-the-loop")),
]


def _topic(title: str) -> str:
    low = title.lower()
    for topic, keys in _TOPIC_KEYS:
        if any(k in low for k in keys):
            return topic
    return "equipment_systems"


def _pdf_url(item: dict) -> str | None:
    for link in item.get("link") or []:
        url = (link.get("URL") or "").strip()
        if link.get("content-type") == "application/pdf" or url.lower().endswith(".pdf"):
            # ep.liu.se's TLS cert does not cover the www. host old Crossref records carry
            return url.replace("http://", "https://").replace("://www.ep.liu.se/", "://ep.liu.se/")
    return None


def _license(item: dict) -> str:
    for lic in item.get("license") or []:
        if "creativecommons.org/licenses/by/" in (lic.get("URL") or ""):
            return "cc-by"
    return "open"


def sweep(rows: int, offset: int) -> tuple[list[dict], int]:
    r = requests.get(API, params={
        "filter": "prefix:10.3384,type:proceedings-article", "query.container-title": "modelica",
        "rows": rows, "offset": offset, "select": SELECT, "mailto": MAILTO}, timeout=45)
    r.raise_for_status()
    msg = r.json().get("message", {})
    return msg.get("items", []), msg.get("total-results", 0)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=200, help="records per API page")
    ap.add_argument("--max", type=int, default=2000, help="cap on new entries this run")
    ap.add_argument("--append", action="store_true",
                    help="append into the registry (registry/modelica.yaml)")
    args = ap.parse_args()

    urls, titles, reg_ids = registry.existing_keys()
    out, seen = [], set()
    offset, total = 0, 1
    while offset < total and len(out) < args.max:
        try:
            items, total = sweep(args.rows, offset)
        except Exception as e:
            print(f"# crossref sweep offset {offset} failed: {e}", file=sys.stderr)
            break
        for it in items:
            if len(out) >= args.max:
                break
            containers = it.get("container-title") or []
            conf = next((c for c in containers if "modelica" in c.lower()), None)
            title = (it.get("title") or [None])[0]
            url = _pdf_url(it)
            if not conf or not title or not url:
                continue
            title = title.strip()
            u, t = url.rstrip("/"), registry.norm(title)
            if u in urls or t in titles or u in seen:
                continue
            seen.add(u)
            titles.add(t)
            out.append({"id": f"mod-{registry.slug(title)[:52]}", "title": title[:150],
                        "url": url, "source": "modelica_conf", "license": _license(it),
                        "topic": _topic(title), "format": "pdf"})
        offset += args.rows
        time.sleep(1.0)

    registry.uniquify_ids(out, reg_ids)

    by_topic: dict = {}
    for h in out:
        by_topic[h["topic"]] = by_topic.get(h["topic"], 0) + 1
    print(f"# {len(out)} NEW Modelica Conference papers (of {total} under 10.3384; "
          f"deduped vs manifest + registry + blocklist)")
    print(f"# by topic: {by_topic}")
    print("# --- review, then --append, then scripts/build_corpus.py ---")
    print(yaml.safe_dump(out, sort_keys=False, allow_unicode=True))

    if args.append and out:
        counts = registry.append_entries(out)
        print(f"# appended {len(out)} entries to the registry: {counts}", file=sys.stderr)


if __name__ == "__main__":
    main()
