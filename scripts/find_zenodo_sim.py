#!/usr/bin/env python3
"""find_zenodo_sim.py — building energy / physical simulation publications on Zenodo.

Zenodo (CERN's open repository) holds conference papers, theses, project reports and preprints
on building simulation that other indexes miss (IBPSA and Modelica community uploads, EU project
deliverables, theses). The general find_zenodo backend (disabled, exhausted) searched broad AEC
terms and kept CC BY / BY-SA / CC0 only; this backend walks a focused, multilingual simulation
query family through the public records API (https://zenodo.org/api/records, anonymous: at most
25 results per page, ~1 request per 2.5 s here) and collects regardless of licence.

A record is kept when it is a `publication` with open access, has a PDF file, and its title passes
the building-science gate (scripts/bes_relevance.py strict, or a simulation-tool name).
Licence = the record's structured `metadata.license.id` set by its depositor:
cc-by* / cc-by-sa* / cc-zero / cc-by-nc* / cc-by-nd* / cc-by-nc-sa* / cc-by-nc-nd* map to the
exact tag; anything else (missing, "other-open", "zenodo-freetoread-1.0", ...) is `unverified`.
Records with a DOI carry it as persistent_id (dedup against OpenAlex and Zenodo copies).

Rotation (dynamic, `--cursor "q=<query index> p=<page>"`): query-major, each query to its last
(short) page, results sorted oldest-first so later deposits append at the end (a deleted record
can shift one result back across a page boundary: re-aim after exhaustion to sweep stragglers); HTTP 429 or any unexpected answer HOLDs the cursor; past the last query the backend
reports EXHAUSTED.

    python scripts/find_zenodo_sim.py --cursor "q=0 p=1" --pages 4 --max 50
"""
from __future__ import annotations

import argparse
import html
import re
import sys
import time
from datetime import datetime, timezone

import requests
import yaml

import bes_relevance
import dedup
import registry
from finder_protocol import Report

API = "https://zenodo.org/api/records"
UA = {"User-Agent": "nekaise-corpus/find_zenodo_sim (research corpus; robots.txt honoured)"}
DELAY = 2.5
ROWS = 25  # anonymous API hard cap
SOURCE = "zenodo_sim"
TOPIC = "simulation_modeling"
PREFIX = "zns-"

# Append-only: the cursor indexes this tuple.
QUERIES = (
    '"EnergyPlus"',
    '"OpenStudio"',
    '"Modelica" AND (building OR HVAC OR "district heating" OR "heat pump")',
    '"building performance simulation"',
    '"building energy simulation"',
    '"building energy model"',
    '"energy simulation" AND building',
    '"TRNSYS"',
    '"IDA ICE"',
    '"DesignBuilder"',
    '"BOPTEST"',
    '"urban building energy"',
    '"urban energy model"',
    '"co-simulation" AND building',
    '"digital twin" AND (building OR HVAC)',
    '"model predictive control" AND building',
    '"reinforcement learning" AND (HVAC OR "building energy")',
    '"Radiance" AND daylight',
    '"daylight simulation"',
    '"hygrothermal"',
    '"computational fluid dynamics" AND (building OR indoor OR ventilation)',
    '"CONTAM"',
    'calibration AND "energy model"',
    '"thermal comfort" AND simulation',
    '"IBPSA"',
    '"Gebäudesimulation" OR "thermische Gebäudesimulation"',
    '"simulation thermique dynamique"',
    '"simulación energética" AND edificio',
    '"simulazione energetica" AND edificio',
)
SIM_TOOL = re.compile(
    r"energy ?plus|openstudio|\btrnsys\b|ida[ -]?ice|designbuilder|\bboptest\b|\bcontam\b|"
    r"\bwufi\b|\bdelphin\b|\besp-r\b|\bdaysim\b|citysim|urbanopt|gebäudesimulation|"
    r"simulation thermique|simulación energética|simulazione energetica",
    re.I,
)
# Complete Zenodo licence ids only (SPDX-style with optional version and jurisdiction, or the
# legacy unversioned form); any other id, e.g. "cc-by-invalid", is unverified.
CC_ID = re.compile(r"cc-(by|by-sa|by-nd|by-nc|by-nc-sa|by-nc-nd)(?:-\d\.\d(?:-[a-z]{2,3})?)?")
CC0_ID = re.compile(r"cc-zero|cc0(?:-1\.0)?")
CURSOR = re.compile(r"^q=(\d+) p=(\d+)$")


class Unexpected(RuntimeError):
    """Rate limit, error or a body that is not the records API's JSON."""


def parse_cursor(cursor: str) -> tuple[int, int]:
    match = CURSOR.match(cursor.strip())
    if not match or int(match.group(2)) < 1:
        raise ValueError(f"cursor must be 'q=<index> p=<page>' (page >= 1): {cursor!r}")
    return int(match.group(1)), int(match.group(2))


def fetch_page(query: str, page: int) -> list[dict]:
    # sort=oldest (creation time ascending): new deposits append at the END, so a persisted page
    # offset cannot skip records the way relevance ranking (Zenodo's default) can.
    response = requests.get(API, params={"q": query, "type": "publication", "size": ROWS,
                                         "page": page, "sort": "oldest"}, headers=UA, timeout=60)
    if response.status_code != 200:
        raise Unexpected(f"Zenodo answered HTTP {response.status_code}")
    try:
        hits = response.json()["hits"]["hits"]
    except (ValueError, KeyError, TypeError) as exc:
        raise Unexpected(f"Zenodo answer is not a records list: {exc}") from exc
    if not isinstance(hits, list):
        raise Unexpected("Zenodo hits is not a list")
    return hits


def licence(meta: dict) -> tuple[str, str]:
    lid = str(((meta.get("license") or {}).get("id")) or "").strip().lower()
    if match := CC_ID.fullmatch(lid):
        return f"cc-{match.group(1)}", f"Zenodo record metadata.license.id = {lid}"
    if CC0_ID.fullmatch(lid):
        return "cc0", f"Zenodo record metadata.license.id = {lid}"
    return "unverified", (f"Zenodo record metadata.license.id = {lid or 'none'} "
                          "(not a recognised reuse licence)")


def relevant(title: str) -> bool:
    if bes_relevance.relevant(title, strict=True):
        return True
    return bool(SIM_TOOL.search(title)) and not bes_relevance.vetoed(title)


def candidate(rec: dict, today: str) -> dict | None:
    meta = rec.get("metadata") or {}
    if meta.get("access_right") != "open":
        return None
    if ((meta.get("resource_type") or {}).get("type")) not in (None, "publication"):
        return None
    title = " ".join(html.unescape(str(meta.get("title") or "")).split())
    if not title or not relevant(title):
        return None
    pdf = next((f for f in rec.get("files") or []
                if str(f.get("key") or "").lower().endswith(".pdf")), None)
    url = str(((pdf or {}).get("links") or {}).get("self") or "").strip()
    if not url.startswith("https://zenodo.org/"):
        return None
    tag, evidence = licence(meta)
    entry = {
        "id": f"{PREFIX}{registry.slug(title)[:52]}", "title": title[:150], "url": url,
        "source": SOURCE, "license": tag, "topic": TOPIC, "format": "pdf",
        "license_evidence": evidence, "rights_verified_at": today,
    }
    if subtype := (meta.get("resource_type") or {}).get("subtype"):
        entry["document_type"] = str(subtype)[:40]
    if published := str(meta.get("publication_date") or "")[:10]:
        entry["published_at"] = published
    if (doi := dedup.normalize_pid(str(rec.get("doi") or meta.get("doi") or ""))):
        entry["persistent_id"] = doi
    return entry


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cursor", default="q=0 p=1", help="'q=<query index> p=<page>' (rotation)")
    ap.add_argument("--pages", type=int, default=6, help="max API pages (25 records) this run")
    ap.add_argument("--max", type=int, default=60,
                    help="page-granular target for new entries (the last page may exceed it)")
    ap.add_argument("--append", action="store_true",
                    help="append into the registry (registry/zenodosim.yaml)")
    args = ap.parse_args()
    if args.pages < 1 or args.max < 1:
        ap.error("--pages and --max must be positive")
    report = Report()
    if args.cursor == "END":
        report.exhausted(f"all {len(QUERIES)} Zenodo simulation queries walked")
        print("# 0 NEW Zenodo simulation records (cursor END)")
        return
    try:
        q, page = parse_cursor(args.cursor)
    except ValueError as exc:
        ap.error(str(exc))

    today = datetime.now(timezone.utc).date().isoformat()
    keys = dedup.open_keys()
    out: list[dict] = []
    scanned = requests_made = 0
    while q < len(QUERIES) and requests_made < args.pages and len(out) < args.max:
        if requests_made:
            time.sleep(DELAY)
        try:
            hits = fetch_page(QUERIES[q], page)
        except Unexpected as exc:
            report.hold(f"{exc} at q={q} p={page}; nothing proposed, retrying next round")
            print("# 0 NEW Zenodo simulation records (unexpected answer)")
            return
        except Exception as exc:
            print(f"# ERROR: Zenodo request failed at q={q} p={page}: {exc}; refusing a partial "
                  "append so rotation does not advance", file=sys.stderr)
            raise SystemExit(1)
        requests_made += 1
        scanned += len(hits)
        entries = [e for e in (candidate(h, today) for h in hits) if e]
        keys.prefetch(**dedup.page_keys(entries))
        for entry in entries:
            u, t = entry["url"].rstrip("/"), registry.norm(entry["title"])
            if u in keys.urls or t in keys.titles or keys.identity_known(entry):
                continue
            keys.urls.add(u)
            keys.titles.add(t)
            keys.add_identity(entry)
            out.append(entry)
        if len(hits) < ROWS:
            q, page = q + 1, 1
        else:
            page += 1

    keys.uniquify_ids(out)
    by_licence: dict[str, int] = {}
    for entry in out:
        by_licence[entry["license"]] = by_licence.get(entry["license"], 0) + 1
    print(f"# {len(out)} NEW Zenodo simulation records (kept {len(out)}/{scanned} over "
          f"{requests_made} API pages; deduped vs manifest + registry + blocklist + DOI)")
    print(f"# by licence: {by_licence}")
    print("# --- review, then --append, then scripts/build_corpus.py ---")
    print(yaml.safe_dump(out, sort_keys=False, allow_unicode=True))
    if args.append and out:
        counts = registry.append_entries(out)
        print(f"# appended {len(out)} entries to the registry: {counts}", file=sys.stderr)
    if q >= len(QUERIES):
        report.exhausted(f"all {len(QUERIES)} Zenodo simulation queries walked")
    else:
        report.next(f"q={q} p={page}")


if __name__ == "__main__":
    main()
