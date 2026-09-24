#!/usr/bin/env python3
"""find_ojs.py — harvest small open-access built-environment journals/proceedings on OJS.

Many small, openly licensed AEC venues run Open Journal Systems (OJS), which exposes every
article over OAI-PMH at `<journal>/oai` with Dublin Core `dc:rights` (the article's licence URL)
and `dc:relation` (the galley view, `<journal>/article/view/<article>/<galley>`). The galley's
direct download is `<journal>/article/download/<article>/<galley>` (served as application/pdf).
One generic backend therefore covers them all; SITES lists the venues (checked 2026-09-24:
robots.txt of both TU Delft OPEN hosts and jfde.eu disallows only /cache/).

The licence gate is PER RECORD and FAIL-CLOSED: only a `dc:rights` Creative Commons BY / BY-SA
URL, CC0 or the Public Domain Mark is kept. NC / ND licences, bare "Copyright (c) <author>" lines
and records without any licence URL are skipped — several TU Delft journals (A+BE, Footprint,
DASH) carry no licence URL on most records, so they contribute only their explicitly CC-BY ones.

Rotation: `--site N` harvests SITES[N] completely (every OAI page, 1 s apart; the largest venue
is ~600 records = 6 pages). A run that hits `--max` requests a rotation HOLD so the same site is
drained next round; a completed site advances the pointer; an index past the end reports the
backend exhausted. OJS resumption tokens expire after ~24 h, so they are never persisted.

    python scripts/find_ojs.py --site 0 --max 50            # CLIMA 2022, dry run
    python scripts/find_ojs.py --site-key jfde --append     # one venue by key
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path

import requests
import yaml

import registry

UA = {"User-Agent": "nekaise-corpus/find_ojs (research)"}
NS = {"oai": "http://www.openarchives.org/OAI/2.0/", "dc": "http://purl.org/dc/elements/1.1/"}

# key: id namespace (ojs-<key>-...) and --site-key; base: the OJS journal root (no trailing /).
# Order is the rotation order — APPEND new venues at the end, never reorder (the committed
# pointer in registry/rotation.json is an index into this list).
SITES = [
    {"key": "clima2022", "base": "https://proceedings.open.tudelft.nl/clima2022",
     "source": "clima2022", "topic": "equipment_systems", "type": "conference-paper",
     "name": "CLIMA 2022 (14th REHVA HVAC World Congress)"},
    {"key": "jfde", "base": "https://jfde.eu/index.php/jfde",
     "source": "jfde", "topic": "architecture", "type": "journal-article",
     "name": "Journal of Facade Design and Engineering"},
    {"key": "coastlab24", "base": "https://proceedings.open.tudelft.nl/coastlab24",
     "source": "coastlab24", "topic": "infrastructure", "type": "conference-paper",
     "name": "Coastlab 2024 (physical modelling in coastal engineering)"},
    {"key": "jchs", "base": "https://journals.open.tudelft.nl/jchs",
     "source": "jchs", "topic": "infrastructure", "type": "journal-article",
     "name": "Journal of Coastal and Hydraulic Structures"},
    {"key": "jcrfr", "base": "https://journals.open.tudelft.nl/jcrfr",
     "source": "jcrfr", "topic": "infrastructure", "type": "journal-article",
     "name": "Journal of Coastal and Riverine Flood Risk"},
    {"key": "jdu", "base": "https://journals.open.tudelft.nl/jdu",
     "source": "jdu", "topic": "urban", "type": "journal-article",
     "name": "Journal of Delta Urbanism"},
    {"key": "writingplace", "base": "https://journals.open.tudelft.nl/writingplace",
     "source": "writingplace", "topic": "architecture", "type": "journal-article",
     "name": "Writingplace (literature and architecture)"},
    {"key": "abe", "base": "https://journals.open.tudelft.nl/abe",
     "source": "abe", "topic": "architecture", "type": "thesis",
     "name": "A+BE Architecture and the Built Environment (TU Delft theses)"},
]

# Canonical Creative Commons licence / public-domain tool URLs. A rights value is DECISIVE only if
# it is exactly one of these URLs (whitespace trimmed) — a CC-looking path on another host, or a
# URL embedded in prose ("not licensed under https://creativecommons.org/...") never is.
_CC_URL = re.compile(
    r"https?://(?:www\.)?creativecommons\.org/"
    r"(?:licenses/(?P<lic>by|by-sa|by-nc|by-nd|by-nc-sa|by-nc-nd)/(?P<ver>\d\.\d)"
    r"(?:/[a-z]{2}(?:-[a-z]{2})?)?"
    r"|publicdomain/(?P<pd>zero|mark)/1\.0)"
    r"(?:/(?:legalcode(?:\.[a-z]{2})?|deed\.[a-z]{2}(?:-[a-z]{2})?)?)?/?", re.I)
_OPEN_TAGS = {"by": "cc-by", "by-sa": "cc-by-sa", "zero": "cc0", "mark": "public-domain"}
# Any of these anywhere in ANY rights value is conflicting evidence and rejects the record.
_RESTRICTIVE = re.compile(
    r"licenses/by-(?:nc|nd)|\bby-n[cd]\b|non-?commercial|no-?deriv|\bnc\b|\bnd\b"
    r"|all rights reserved|not (?:be )?(?:licen[cs]ed|re-?used|redistribut)", re.I)
# A free-text rights value that talks about licensing without being a canonical URL is
# unverifiable (e.g. "CC BY-like terms", a negated statement): fail closed.
_LICENSE_TALK = re.compile(r"creative\s*commons|creativecommons|licen[cs]|\bcc[ -]?(?:by|0)\b",
                           re.I)
_XML_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
LANG3 = {"eng": "en", "nld": "nl", "dut": "nl", "deu": "de", "ger": "de", "fra": "fr",
         "fre": "fr", "spa": "es", "por": "pt", "ita": "it"}


def license_for(rights: list[str]) -> tuple[str, str] | tuple[None, None]:
    """(licence tag, canonical licence URL) or (None, None) — FAIL-CLOSED, order-independent.

    Accept only when (a) at least one rights value is exactly a canonical CC BY / BY-SA / CC0 /
    PDM URL, (b) no rights value carries NC / ND / all-rights-reserved / negation evidence, and
    (c) every other value is a plain copyright line, not unverifiable licence prose. Two
    different open grants resolve to the more restrictive tag (BY-SA over BY)."""
    decided: dict[str, str] = {}
    for value in rights:
        value = value.strip()
        if _RESTRICTIVE.search(value):
            return None, None
        m = _CC_URL.fullmatch(value)
        if m:
            key = m.group("lic") or m.group("pd")
            tag = _OPEN_TAGS.get(key.lower())
            if tag is None:
                return None, None
            decided.setdefault(tag, value)
        elif _LICENSE_TALK.search(value):
            return None, None
    for tag in ("cc-by-sa", "cc-by", "cc0", "public-domain"):
        if tag in decided:
            return tag, decided[tag]
    return None, None


def galley_download(base: str, relations: list[str]) -> str | None:
    """The first `article/view/<a>/<g>` galley of this journal as its direct download URL."""
    pat = re.compile(re.escape(base) + r"/article/view/(\d+)/(\d+)/?$")
    for rel in relations:
        m = pat.match(rel.strip().replace("http://", "https://", 1))
        if m:
            return f"{base}/article/download/{m.group(1)}/{m.group(2)}"
    return None


def fetch_page(base: str, token: str) -> str:
    params = ({"verb": "ListRecords", "resumptionToken": token} if token else
              {"verb": "ListRecords", "metadataPrefix": "oai_dc"})
    r = requests.get(f"{base}/oai", params=params, headers=UA, timeout=60)
    r.raise_for_status()
    return r.text


class UnexpectedResponse(RuntimeError):
    """The endpoint answered with something other than an OAI-PMH ListRecords page."""


def parse_page(xml_text: str) -> tuple[list[dict], str]:
    """-> (live records, next resumption token or '' when the list is complete)."""
    # OJS copies abstracts verbatim from submissions; C0 control characters that XML 1.0 forbids
    # (CLIMA 2022 page 3 carries U+0002 in "step-by\x02step") would otherwise fail the whole page.
    try:
        root = ET.fromstring(_XML_ILLEGAL.sub(" ", xml_text))
    except ET.ParseError as exc:
        raise UnexpectedResponse(f"response is not XML: {exc}") from exc
    # A well-formed HTML/XHTML page (maintenance notice, WAF, login) must never read as "zero
    # records, list complete": require the OAI-PMH envelope and a ListRecords answer.
    if root.tag != f"{{{NS['oai']}}}OAI-PMH":
        raise UnexpectedResponse(f"not an OAI-PMH envelope (root element {root.tag!r})")
    err = root.find("oai:error", NS)
    if err is not None:
        if err.get("code") == "noRecordsMatch":
            return [], ""
        raise RuntimeError(f"OAI error {err.get('code')}: {err.text}")
    if root.find("oai:ListRecords", NS) is None:
        raise UnexpectedResponse("OAI-PMH response carries no ListRecords element")
    records = []
    for rec in root.findall(".//oai:record", NS):
        header = rec.find("oai:header", NS)
        if header is None or header.get("status") == "deleted":
            continue
        dc = rec.find("oai:metadata", NS)
        dc = dc[0] if dc is not None and len(dc) else None
        if dc is None:
            continue

        def vals(tag: str) -> list[str]:
            return [(e.text or "").strip() for e in dc.findall(f"dc:{tag}", NS) if e.text]

        titles = vals("title")
        records.append({
            "oai_id": (header.findtext("oai:identifier", default="", namespaces=NS)).strip(),
            "title": re.sub(r"\s+", " ", titles[0]) if titles else "",
            "rights": vals("rights"), "relations": vals("relation"),
            "identifiers": vals("identifier"), "date": (vals("date") or [""])[0],
            "language": (vals("language") or [""])[0],
        })
    tok = root.find(".//oai:resumptionToken", NS)
    return records, ((tok.text or "").strip() if tok is not None else "")


def entry_for(site: dict, rec: dict, today: str) -> dict | None:
    tag, lic_url = license_for(rec["rights"])
    if tag is None or not rec["title"]:
        return None
    url = galley_download(site["base"], rec["relations"])
    if url is None:
        return None
    entry = {
        "id": f"ojs-{site['key']}-{registry.slug(rec['title'])}"[:63].rstrip("-"),
        "title": rec["title"][:150], "url": url, "source": site["source"], "license": tag,
        "topic": site["topic"], "format": "pdf", "document_type": site["type"],
        "license_url": lic_url,
        "license_evidence": (f"{site['base']}/oai?verb=GetRecord&metadataPrefix=oai_dc"
                             f"&identifier={rec['oai_id']}"),
        "rights_verified_at": today,
    }
    doi = next((i for i in rec["identifiers"] if i.startswith("10.")), "")
    if doi:
        entry["persistent_id"] = f"https://doi.org/{doi}"
    lang = rec["language"].lower()
    lang = LANG3.get(lang, lang if len(lang) == 2 else "")
    if lang:
        entry["language"] = lang
    if re.match(r"\d{4}-\d{2}-\d{2}$", rec["date"]):
        entry["published_at"] = rec["date"]
    return entry


def harvest(site: dict, known_urls: set, known_titles: set, maxn: int,
            delay: float = 1.0) -> tuple[list[dict], int, bool]:
    """-> (new entries, records scanned, site completed?). Finishes the page that crosses maxn."""
    out: list[dict] = []
    scanned, token, first = 0, "", True
    seen_tokens: set[str] = set()
    today = date.today().isoformat()
    while first or token:
        if not first:
            time.sleep(delay)
        first = False
        records, token = parse_page(fetch_page(site["base"], token))
        if token and token in seen_tokens:
            raise UnexpectedResponse(f"repeated resumptionToken {token!r} (paging loop)")
        seen_tokens.add(token)
        for rec in records:
            scanned += 1
            entry = entry_for(site, rec, today)
            if entry is None:
                continue
            u, t = entry["url"].rstrip("/"), registry.norm(entry["title"])
            if u in known_urls or t in known_titles:
                continue
            known_urls.add(u)
            known_titles.add(t)
            out.append(entry)
        if len(out) >= maxn and token:
            return out, scanned, False
    return out, scanned, True


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--site", type=int, default=0, help="rotation index into SITES")
    ap.add_argument("--site-key", default="", help="harvest one venue by key (manual run)")
    ap.add_argument("--max", type=int, default=500,
                    help="page-granular cap on new entries this run (the last page may exceed it)")
    ap.add_argument("--delay", type=float, default=1.0, help="seconds between OAI pages")
    ap.add_argument("--append", action="store_true")
    args = ap.parse_args()

    if args.site_key:
        sites = [s for s in SITES if s["key"] == args.site_key]
        if not sites:
            raise SystemExit(f"unknown --site-key {args.site_key}; known: "
                             f"{', '.join(s['key'] for s in SITES)}")
        site = sites[0]
    elif args.site >= len(SITES):
        print(f"# all {len(SITES)} OJS venues harvested (site index {args.site})")
        if name := os.environ.get("NEKAISE_BACKEND_EXHAUSTED_FILE"):
            Path(name).write_text(f"all {len(SITES)} OJS venues harvested\n")
        return
    else:
        site = SITES[args.site]

    urls, titles, reg_ids = registry.existing_keys()
    try:
        out, scanned, complete = harvest(site, urls, titles, args.max, args.delay)
    except Exception as exc:
        print(f"# {site['key']} OAI harvest failed: {exc}", file=sys.stderr)
        print("# refusing a partial append so rotation does not advance", file=sys.stderr)
        raise SystemExit(1)
    registry.uniquify_ids(out, reg_ids)

    by_lic: dict[str, int] = {}
    for e in out:
        by_lic[e["license"]] = by_lic.get(e["license"], 0) + 1
    print(f"# {len(out)} NEW {site['name']} records ({site['key']}; scanned {scanned}; "
          f"CC-BY/BY-SA/CC0/PD licence URL + PDF galley only; deduped vs manifest + registry + "
          f"blocklist){'' if complete else ' -- capped, site not finished'}")
    print(f"# by licence: {by_lic}")
    print("# --- review, then --append, then scripts/build_corpus.py ---")
    print(yaml.safe_dump(out, sort_keys=False, allow_unicode=True))

    if args.append and out:
        counts = registry.append_entries(out)
        print(f"# appended {len(out)} entries to the registry: {counts}", file=sys.stderr)
    if not complete and not args.site_key and (
            hold := os.environ.get("NEKAISE_ROTATION_HOLD_FILE")):
        Path(hold).write_text(f"{site['key']} capped at --max {args.max}; drain next round\n")


if __name__ == "__main__":
    main()
