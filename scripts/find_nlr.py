#!/usr/bin/env python3
"""find_nlr.py — building research reports of the National Laboratory of the Rockies (ex-NREL).

NREL was renamed the National Laboratory of the Rockies (NLR) in 2026: www.nrel.gov no longer
resolves, report PDFs now live at https://docs.nlr.gov/docs/fy{YY}osti/{NNNNN}.pdf, and the
publication record moved to the Elsevier Pure portal at research-hub.nlr.gov, whose OAI-PMH
endpoint (/ws/oai) exposes 53,919 publications (probed 2026-09-24). NREL's buildings research —
EnergyPlus, OpenStudio, ResStock, ComStock, URBANopt, BEopt, grid-interactive buildings — is
core building-energy-simulation material.

Record -> PDF mapping (verified 2026-09-24): oai_dc records of NLR-hosted documents carry
`<dc:identifier type="link">https://www.nlr.gov/docs/fy21osti/76117.pdf</dc:identifier>` (plus
a dead www.nrel.gov twin). www.nlr.gov/docs/... 301-redirects to docs.nlr.gov/docs/..., which
serves the PDF (200 application/pdf) to ordinary clients, so the finder proposes the canonical
docs.nlr.gov URL. Records without such a link (about a third: journal articles hosted only by
their publisher, book chapters) are skipped — guessing the fiscal-year folder from the report
number is not reliable. Presentations (PR) and posters (PO) are skipped: slide decks extract to
thin text that the quality gate would prune anyway.

Relevance: the report number `NREL/<type>-<division>-<number>` (dc:subject) names the issuing
center; division 55xx is the Buildings Technologies & Science Center, so those records pass on
the loose gate. Every other division must pass scripts/bes_relevance.py's strict title gate.

License: `public-domain`, matching the repo's policy for US DOE national-laboratory reports
(find_osti tags every OSTI full-text record public-domain).

Rotation (dynamic, `--cursor`): "<year>:<resumptionToken>" or "<year>:START", walking the Pure
per-year sets (publications:yearYYYY) from the newest year down to FLOOR_YEAR. A rejected or
expired resumptionToken restarts that year (dedup makes the replay harmless). robots.txt asks
for `Crawl-delay: 5`, honoured between OAI requests. The walk is newest-first and does not
come back up: re-aim the pointer to the current year occasionally to pick up new reports.

    python scripts/find_nlr.py --cursor 2024:START --pages 2 --max 30
"""
from __future__ import annotations

import argparse
import html
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

import requests
import yaml

import bes_relevance
import dedup
import registry

OAI = "https://research-hub.nlr.gov/ws/oai"
UA = {"User-Agent": "nekaise-corpus/find_nlr"}
CRAWL_DELAY = 5.0  # research-hub.nlr.gov robots.txt Crawl-delay
FLOOR_YEAR = 1975  # oldest publications:yearYYYY set listed by ListSets
START = "START"
DOCS = "https://docs.nlr.gov"
NS = {
    "oai": "http://www.openarchives.org/OAI/2.0/",
    "dc": "http://purl.org/dc/elements/1.1/",
}
DOC_LINK = re.compile(
    r"^https?://(?:www\.|docs\.)?(?:nlr|nrel)\.gov/docs/(fy\d{2}osti/\d+\.pdf)$", re.I
)
REPORT_NUMBER = re.compile(r"^NREL/([A-Z]{2,3})-([0-9A-Z]{4})-(\d+)", re.I)
SKIP_TYPES = frozenset({"PR", "PO"})
# Pure appends the journal article number to titles ("...Transportation:Article No. 132134"),
# which would defeat title dedup against the same paper registered from OSTI/OpenAlex.
ARTICLE_NO = re.compile(r"\s*:?\s*Article No\.?\s*\S+\s*$", re.I)


def parse_cursor(cursor: str) -> tuple[int, str | None]:
    year, sep, token = cursor.partition(":")
    if not sep or not year.isdigit() or not token:
        raise ValueError(f"cursor must be '<year>:<token|START>': {cursor!r}")
    return int(year), (None if token == START else token)


class Unexpected(RuntimeError):
    """Not an OAI-PMH answer: a challenge, refusal, maintenance or error page."""


def fetch_page(year: int, token: str | None) -> str:
    params = ({"verb": "ListRecords", "resumptionToken": token} if token else
              {"verb": "ListRecords", "metadataPrefix": "oai_dc",
               "set": f"publications:year{year}"})
    response = requests.get(OAI, params=params, headers=UA, timeout=90)
    if response.status_code != 200:
        raise Unexpected(f"OAI answered HTTP {response.status_code}")
    return response.text


def parse_page(xml_text: str) -> tuple[list[dict], str | None]:
    """OAI page -> (records, next token or None). noRecordsMatch is an empty, finished set;
    badResumptionToken raises LookupError so the caller can restart the year. Anything that is
    not an OAI-PMH ListRecords answer (an XHTML challenge page parses as valid XML too) raises
    Unexpected, so an unexpected page can never read as "year finished"."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise Unexpected(f"OAI answer is not XML: {exc}") from exc
    if root.tag != f"{{{NS['oai']}}}OAI-PMH":
        raise Unexpected(f"OAI answer has root element {root.tag!r}, not OAI-PMH")
    error = root.find("oai:error", NS)
    if error is not None:
        code = error.get("code")
        if code == "noRecordsMatch":
            return [], None
        if code == "badResumptionToken":
            raise LookupError(f"OAI badResumptionToken: {error.text}")
        raise RuntimeError(f"OAI error {code}: {error.text}")
    if root.find("oai:ListRecords", NS) is None:
        raise Unexpected("OAI-PMH answer has neither ListRecords nor error")
    records = []
    for record in root.findall(".//oai:record", NS):
        header = record.find("oai:header", NS)
        if header is not None and header.get("status") == "deleted":
            continue
        metadata = record.find("oai:metadata", NS)
        dc = metadata[0] if metadata is not None and len(metadata) else None
        if dc is None:
            continue
        subjects = [(e.text or "").strip() for e in dc.findall("dc:subject", NS)]
        records.append({
            # Pure double-escapes some entities ("&amp;apos;"): unescape once more.
            "title": " ".join(html.unescape(
                dc.findtext("dc:title", default="", namespaces=NS) or "").split()),
            "subjects": subjects,
            "links": [(e.text or "").strip() for e in dc.findall("dc:identifier", NS)
                      if e.get("type") == "link"],
        })
    token_el = root.find(".//oai:resumptionToken", NS)
    token = (token_el.text or "").strip() if token_el is not None else ""
    return records, token or None


def report_number(subjects: list[str]) -> tuple[str, str, str] | None:
    for subject in subjects:
        if match := REPORT_NUMBER.match(subject):
            return match.group(1).upper(), match.group(2).upper(), match.group(3)
    return None


def docs_url(links: list[str]) -> str | None:
    """Canonical docs.nlr.gov PDF for the first NLR/NREL-hosted document link."""
    for link in links:
        if match := DOC_LINK.match(link):
            return f"{DOCS}/docs/{match.group(1).lower()}"
    return None


def candidate(record: dict) -> dict | None:
    title = ARTICLE_NO.sub("", record["title"]).strip()
    url = docs_url(record["links"])
    if not title or url is None:
        return None
    number = report_number(record["subjects"])
    kind, division = (number[0], number[1]) if number else ("", "")
    if kind in SKIP_TYPES:
        return None
    buildings_center = division.startswith("55")
    if not bes_relevance.relevant(title, strict=not buildings_center):
        return None
    entry = {
        "id": f"nlr-{registry.slug(title)[:52]}", "title": title[:150], "url": url,
        "source": "nlr", "license": "public-domain", "topic": bes_relevance.topic_for(title),
        "format": "pdf",
        "license_evidence": "US DOE national laboratory report (NLR, formerly NREL); "
                            "same policy as OSTI full-text records",
    }
    if number:
        entry["persistent_id"] = f"NREL/{number[0]}-{number[1]}-{number[2]}"
    return entry


def request_rotation_hold(reason: str) -> None:
    if hold_name := os.environ.get("NEKAISE_ROTATION_HOLD_FILE"):
        Path(hold_name).write_text(reason + "\n", encoding="utf-8")
    print(f"# rotation hold requested: {reason}", file=sys.stderr)


def report_exhausted(reason: str) -> None:
    if name := os.environ.get("NEKAISE_BACKEND_EXHAUSTED_FILE"):
        Path(name).write_text(reason + "\n", encoding="utf-8")
    print(f"# backend exhausted: {reason}", file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cursor", default=f"{datetime.now(timezone.utc).year}:{START}",
                    help="'<year>:<resumptionToken>' or '<year>:START' (rotation pointer)")
    ap.add_argument("--pages", type=int, default=4, help="max OAI pages (100 records) this run")
    ap.add_argument("--max", type=int, default=100,
                    help="page-granular target for new entries (the last page may exceed it)")
    ap.add_argument("--append", action="store_true", help="append into the registry (registry/nlr.yaml)")
    args = ap.parse_args()
    if args.pages < 1 or args.max < 1:
        ap.error("--pages and --max must be positive")
    if args.cursor == "END":
        report_exhausted(f"NLR Pure OAI walked down to {FLOOR_YEAR}")
        if next_name := os.environ.get("NEKAISE_ROTATION_NEXT_FILE"):
            Path(next_name).write_text("END\n", encoding="utf-8")
        print("# 0 NEW NLR reports (cursor END)")
        return
    try:
        year, token = parse_cursor(args.cursor)
    except ValueError as exc:
        ap.error(str(exc))

    keys = dedup.open_keys()
    urls, titles = keys.urls, keys.titles
    out: list[dict] = []
    scanned = pages = 0
    restarted = False
    while year >= FLOOR_YEAR and pages < args.pages and len(out) < args.max:
        if pages:
            time.sleep(CRAWL_DELAY)
        try:
            records, next_token = parse_page(fetch_page(year, token))
        except LookupError as exc:
            if restarted or token is None:
                print(f"# ERROR: {exc}", file=sys.stderr)
                raise SystemExit(1)
            print(f"# {exc}; restarting year {year} from its first page", file=sys.stderr)
            restarted, token = True, None
            pages += 1
            continue
        except Unexpected as exc:
            request_rotation_hold(f"{exc} at {year}:{token or START}; nothing proposed, "
                                  "retrying this cursor next round")
            print("# 0 NEW NLR reports (unexpected OAI answer)")
            return
        except Exception as exc:
            print(f"# ERROR: NLR OAI page failed at {year}:{token or START}: {exc}; "
                  "refusing a partial append so rotation does not advance", file=sys.stderr)
            raise SystemExit(1)
        pages += 1
        for record in records:
            scanned += 1
            entry = candidate(record)
            if entry is None:
                continue
            u, t = entry["url"].rstrip("/"), registry.norm(entry["title"])
            if u in urls or t in titles:
                continue
            urls.add(u)
            titles.add(t)
            out.append(entry)
        if next_token:
            token = next_token
        else:
            year, token = year - 1, None

    keys.uniquify_ids(out)
    next_cursor = f"{year}:{token or START}"
    by_topic: dict[str, int] = {}
    for entry in out:
        by_topic[entry["topic"]] = by_topic.get(entry["topic"], 0) + 1
    print(f"# {len(out)} NEW NLR (ex-NREL) building reports (kept {len(out)}/{scanned} OAI "
          f"records over {pages} pages; docs.nlr.gov PDFs, public-domain, deduped vs manifest + "
          f"registry + blocklist)")
    print(f"# by topic: {by_topic}")
    print("# --- review, then --append, then scripts/build_corpus.py ---")
    print(yaml.safe_dump(out, sort_keys=False, allow_unicode=True))

    if args.append and out:
        counts = registry.append_entries(out)
        print(f"# appended {len(out)} entries to the registry: {counts}", file=sys.stderr)
    if year < FLOOR_YEAR:
        next_cursor = "END"
        report_exhausted(f"NLR Pure OAI walked down to {FLOOR_YEAR}")
    if next_name := os.environ.get("NEKAISE_ROTATION_NEXT_FILE"):
        Path(next_name).write_text(next_cursor + "\n", encoding="utf-8")
    print(f"# next-cursor: {next_cursor}")


if __name__ == "__main__":
    main()
