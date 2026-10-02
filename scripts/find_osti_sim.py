#!/usr/bin/env python3
"""find_osti_sim.py — building energy and physical SIMULATION modelling records from OSTI.

The general find_osti backend walks broad building subjects page by page; this backend walks a
focused query family about simulation and modelling of buildings, HVAC and the urban built
environment (EnergyPlus, OpenStudio, Modelica, DOE-2/eQUEST, TRNSYS, CONTAM, CFD, Radiance,
WUFI/hygrothermal, ResStock/ComStock/URBANopt, calibration, co-simulation/FMI, MPC and RL
control, digital twins, reduced-order models, prototype building models). It reads the public
OSTI records API (https://www.osti.gov/api/v1/records; robots.txt for `*` disallows only
account/search UI paths, not /api/ or /servlets/purl/) at most once per second.

Each query is walked page by page (query-major) to its last page. OSTI searches full text, so
deep pages drift off the topic; the title gate, not a depth cap, keeps precision (a capped walk
would drop the rest of a query's results). A record is kept only when it
  * has a full-text link (the /servlets/purl/<id> PDF),
  * is a Technical Report, Conference paper, Journal Article or Thesis/Dissertation (software,
    datasets, patents and slide/program documents are skipped), and
  * has a building-science title: scripts/bes_relevance.py strict gate, or a simulation-tool
    name in the title (DOE-2, eQUEST, TRNSYS, CONTAM, WUFI, ...) without an off-domain veto.
Calibrated 2026-10-02 on 4,883 records (two pages of each query): the title gate kept 1,580.

Licence classification (collect regardless of licence; the class picks the view):
  * a canonical Creative Commons / public-domain URL in the record's `rights` field (shared
    fail-closed parser scripts/licenses.py): that licence; any other rights text: `unverified`;
  * Journal Article, `article_type` Accepted Manuscript: `publisher-oa` (free to read under the
    DOE Public Access Plan; the publisher holds the copyright, no reuse licence is granted);
  * everything else (reports, theses, conference papers, other articles): `unverified`. OSTI
    distinguishes public access from public domain (https://www.osti.gov/disclaim), and a
    national-laboratory contractor report is not a US Government work, so no record is
    `public-domain` without evidence for that copy.
Records with a DOI carry it as persistent_id, so a paper held through OpenAlex is not fetched
again.

Rotation (dynamic, `--cursor "q=<query index> p=<page>"`): the cursor names the next page to
read. A run reads at most --pages API pages and stops after the page that reaches --max. An
unexpected answer holds the cursor; past the last query the backend reports EXHAUSTED.

    python scripts/find_osti_sim.py --cursor "q=0 p=1" --pages 2 --max 50
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import datetime, timezone

import requests
import yaml

import bes_relevance
import dedup
import licenses
import registry
from finder_protocol import Report

API = "https://www.osti.gov/api/v1/records"
UA = {"User-Agent": "nekaise-corpus/find_osti_sim (research corpus; robots.txt honoured)"}
DELAY = 1.0          # seconds between API requests
ROWS = 100           # records per API page
SOURCE = "osti_sim"
TOPIC = "simulation_modeling"
PREFIX = "osm-"

# Append-only: the cursor indexes this list. Change a query only by appending a new one.
QUERIES = (
    '"EnergyPlus"',
    '"OpenStudio"',
    '"Modelica"',
    '"building energy simulation"',
    '"building energy modeling"',
    '"building performance simulation"',
    '"whole building simulation"',
    '"energy simulation" AND building',
    '"DOE-2"',
    '"eQUEST"',
    'TRNSYS',
    '"CONTAM" AND airflow',
    'multizone AND airflow',
    '"computational fluid dynamics" AND (indoor OR "natural ventilation" OR "urban wind" OR HVAC)',
    '"urban building energy"',
    '"co-simulation" AND building',
    '"Functional Mock-up"',
    'calibration AND "building energy model"',
    'Radiance AND daylighting',
    '"daylighting simulation"',
    '"ResStock"',
    '"ComStock"',
    '"URBANopt"',
    '"BEopt"',
    '"prototype building"',
    '"BOPTEST"',
    '"Spawn of EnergyPlus"',
    '"WUFI"',
    'hygrothermal AND (wall OR envelope OR building)',
    '"model predictive control" AND building',
    '"reinforcement learning" AND (HVAC OR "building energy")',
    '"digital twin" AND (HVAC OR "building energy")',
    '"reduced-order model" AND (HVAC OR "building energy" OR "thermal zone")',
    '"ground heat exchanger" AND model',
    '"Fire Dynamics Simulator"',
    '"CFAST"',
)
# OSTI product type -> registry document_type (the vocabulary other finders use)
KEEP_TYPES = {"Technical Report": "technical-report", "Conference": "conference-paper",
              "Journal Article": "journal-article", "Thesis/Dissertation": "thesis"}
LANGUAGES = {"english": "en", "german": "de", "french": "fr", "spanish": "es", "japanese": "ja",
             "chinese": "zh", "russian": "ru", "italian": "it", "portuguese": "pt"}
# A simulation tool named in a title is building science even without a BUILT term.
SIM_TOOL = re.compile(
    r"\bdoe-?2(?:\.\d\w*)?\b|\bequest\b|\btrnsys\b|\bcontam\b|\bwufi\b|\bboptest\b|"
    r"\bdaysim\b|\besp-r\b|\bbcvtb\b|\bcfast\b|fire dynamics simulator|\bsmokeview\b|"
    r"\bblast\b(?= (?:program|energy|simulation))",
    re.I,
)
CURSOR = re.compile(r"^q=(\d+) p=(\d+)$")
ENTITIES = (("&amp;", "&"), ("<sub>", ""), ("</sub>", ""), ("<sup>", ""), ("</sup>", ""))


class Unexpected(RuntimeError):
    """An answer that is not the records API's JSON list (maintenance page, challenge, error)."""


def parse_cursor(cursor: str) -> tuple[int, int]:
    match = CURSOR.match(cursor.strip())
    if not match or int(match.group(2)) < 1:
        raise ValueError(f"cursor must be 'q=<index> p=<page>' (page >= 1): {cursor!r}")
    return int(match.group(1)), int(match.group(2))


def fetch_page(query: str, page: int) -> list[dict]:
    response = requests.get(API, params={"q": query, "has_fulltext": "true", "rows": ROWS,
                                         "page": page}, headers=UA, timeout=90)
    if response.status_code != 200:
        raise Unexpected(f"OSTI answered HTTP {response.status_code}")
    try:
        records = response.json()
    except ValueError as exc:
        raise Unexpected(f"OSTI answer is not JSON: {exc}") from exc
    if not isinstance(records, list):
        raise Unexpected("OSTI answer is not a list of records")
    return records


def clean_title(title: str) -> str:
    for old, new in ENTITIES:
        title = title.replace(old, new)
    return " ".join(title.split())


def relevant(title: str) -> bool:
    if bes_relevance.relevant(title, strict=True):
        return True
    return bool(SIM_TOOL.search(title)) and not bes_relevance.vetoed(title)


def fulltext_url(record: dict) -> str | None:
    oid = str(record.get("osti_id") or "")
    if not oid.isdigit():
        return None
    for link in record.get("links") or ():
        if isinstance(link, dict) and link.get("rel") == "fulltext":
            return f"https://www.osti.gov/servlets/purl/{oid}"
    return None


def licence(record: dict) -> tuple[str, str, str | None]:
    """(licence tag, evidence, licence URL) for the record's full-text copy. Only a canonical
    Creative Commons / public-domain URL in the record's `rights` field, read by the shared
    fail-closed parser (scripts/licenses.py), is a reuse licence; anything else in that field is
    `unverified`. OSTI distinguishes public access from public domain
    (https://www.osti.gov/disclaim), so a report without such evidence is `unverified` too."""
    kind = record.get("product_type")
    rights = " ".join(str(record.get("rights") or "").split())
    if rights:
        tag, url = licenses.cc_license([rights])
        if tag:
            return tag, f"OSTI record rights field: {rights[:200]}", url
        return ("unverified", f"OSTI record rights field is not a verifiable open licence: "
                f"{rights[:200]}", None)
    if kind == "Journal Article" and record.get("article_type") == "Accepted Manuscript":
        return "publisher-oa", ("OSTI DOE PAGES accepted manuscript of a journal article "
                                f"({record.get('journal_name') or 'journal'}): free to read under "
                                "the DOE Public Access Plan; publisher copyright, no reuse "
                                "licence"), None
    return "unverified", (f"OSTI {kind} full text; the OSTI record states no rights and OSTI "
                          "distinguishes public access from public domain (osti.gov/disclaim)"), None


def candidate(record: dict, today: str) -> dict | None:
    if record.get("product_type") not in KEEP_TYPES:
        return None
    url = fulltext_url(record)
    title = clean_title(record.get("title") or "")
    if url is None or not title or not relevant(title):
        return None
    tag, evidence, licence_url = licence(record)
    entry = {
        "id": f"{PREFIX}{registry.slug(title)[:52]}", "title": title[:150], "url": url,
        "source": SOURCE, "license": tag, "topic": TOPIC, "format": "pdf",
        "document_type": KEEP_TYPES[record["product_type"]],
        "license_evidence": evidence, "rights_verified_at": today,
    }
    if licence_url:
        entry["license_url"] = licence_url
    if language := LANGUAGES.get(str(record.get("language") or "").strip().lower()):
        entry["language"] = language
    if published := (record.get("publication_date") or "")[:10]:
        entry["published_at"] = published
    if (doi := dedup.normalize_pid(record.get("doi") or "")) and doi.startswith("doi:"):
        entry["persistent_id"] = doi
    return entry


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cursor", default="q=0 p=1", help="'q=<query index> p=<page>' (rotation)")
    ap.add_argument("--pages", type=int, default=6, help="max API pages (100 records) this run")
    ap.add_argument("--max", type=int, default=150,
                    help="page-granular target for new entries (the last page may exceed it)")
    ap.add_argument("--append", action="store_true",
                    help="append into the registry (registry/ostisim.yaml)")
    args = ap.parse_args()
    if args.pages < 1 or args.max < 1:
        ap.error("--pages and --max must be positive")
    report = Report()
    if args.cursor == "END":
        report.exhausted(f"all {len(QUERIES)} OSTI simulation queries walked")
        print("# 0 NEW OSTI simulation records (cursor END)")
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
            records = fetch_page(QUERIES[q], page)
        except Unexpected as exc:
            report.hold(f"{exc} at q={q} p={page}; nothing proposed, retrying next round")
            print("# 0 NEW OSTI simulation records (unexpected answer)")
            return
        except Exception as exc:
            print(f"# ERROR: OSTI request failed at q={q} p={page}: {exc}; refusing a partial "
                  "append so rotation does not advance", file=sys.stderr)
            raise SystemExit(1)
        requests_made += 1
        entries = [e for e in (candidate(r, today) for r in records) if e]
        scanned += len(records)
        keys.prefetch(**dedup.page_keys(entries))
        for entry in entries:
            u, t = entry["url"].rstrip("/"), registry.norm(entry["title"])
            if u in keys.urls or t in keys.titles or keys.identity_known(entry):
                continue
            keys.urls.add(u)
            keys.titles.add(t)
            keys.add_identity(entry)
            out.append(entry)
        if len(records) < ROWS:
            q, page = q + 1, 1
        else:
            page += 1

    keys.uniquify_ids(out)
    by_licence: dict[str, int] = {}
    for entry in out:
        by_licence[entry["license"]] = by_licence.get(entry["license"], 0) + 1
    print(f"# {len(out)} NEW OSTI simulation records (kept {len(out)}/{scanned} over "
          f"{requests_made} API pages; deduped vs manifest + registry + blocklist + DOI)")
    print(f"# by licence: {by_licence}")
    print("# --- review, then --append, then scripts/build_corpus.py ---")
    print(yaml.safe_dump(out, sort_keys=False, allow_unicode=True))
    if args.append and out:
        counts = registry.append_entries(out)
        print(f"# appended {len(out)} entries to the registry: {counts}", file=sys.stderr)
    if q >= len(QUERIES):
        report.exhausted(f"all {len(QUERIES)} OSTI simulation queries walked")
    else:
        report.next(f"q={q} p={page}")


if __name__ == "__main__":
    main()
