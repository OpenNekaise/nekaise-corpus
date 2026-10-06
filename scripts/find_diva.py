#!/usr/bin/env python3
"""find_diva.py — building energy / physical simulation theses and reports from DiVA (Sweden).

DiVA (Digitala Vetenskapliga Arkivet, diva-portal.org) is the shared repository of ~50 Swedish
universities and agencies (KTH, Linköping, Uppsala, Luleå, Mälardalen, Gävle, Umeå, ...). It
states that "metadata and files can be accessed freely via a joint portal, local search services,
and with OAI-PMH". Its OAI-PMH endpoint (https://www.diva-portal.org/dice/oai; robots.txt for `*`:
Crawl-delay 10, OAI and /smash/get/ not disallowed) serves `swepub_mods` records that carry the
free full-text URL (`<location><url displayLabel="fulltext" note="free">`), keywords, the HSV
subject and any licence (`accessCondition`).

The finder walks SETS (student theses in technology, doctoral and licentiate theses, reports),
each by datestamp YEAR windows from the current year down to FLOOR_YEAR, and keeps a record only
when it has a free full text, is not vetoed off-domain (scripts/bes_relevance.py), and its titles
plus keywords (Swedish and English) name BOTH a building / indoor / energy-in-buildings concept
and a simulation / modelling concept (IDA ICE, EnergyPlus, TRNSYS, Modelica, VIP-Energy, CFD,
"simulering", "energiberäkning", ...). The full text is proposed at the canonical host
www.diva-portal.org/smash/get/diva2:<n>/FULLTEXT01.pdf (serves every member institution), which
the loader fetches as a polite host (honest UA, serial, robots Crawl-delay 10).

Licence: from every `accessCondition` link AND text. An open Creative Commons grant comes from the
shared fail-closed parser (scripts/licenses.py); a canonical NC/ND licence URL that is the only
statement keeps its exact tag (cc-by-nc, cc-by-nc-sa, cc-by-nd, cc-by-nc-nd);
anything else, including no statement, is `unverified` (DiVA marks the file free to read but
states no reuse licence).

Rotation (dynamic, `--cursor "<set index>:<year>:<resumptionToken|START>"`): an unexpected answer
holds the cursor, a rejected token restarts its year window, past the last set and FLOOR_YEAR the
backend reports EXHAUSTED. A full final page closes its window only when DiVA's declared total,
continuation offset and actual record count agree; an empty continuation never proves completion.

    python scripts/find_diva.py --cursor "0:2025:START" --pages 2 --max 20
"""
from __future__ import annotations

import argparse
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone

import requests
import yaml

import bes_relevance
import dedup
import licenses
import registry
from finder_protocol import Report

OAI = "https://www.diva-portal.org/dice/oai"
UA = {"User-Agent": "nekaise-corpus/find_diva (research corpus; robots.txt honoured)"}
CRAWL_DELAY = 10.0  # diva-portal.org robots.txt Crawl-delay for *
FLOOR_YEAR = 1995
START = "START"
# Append-only: the cursor indexes this tuple.
SETS = ("Technology", "doctoralThesis", "licentiateThesis", "report")
SOURCE = "diva"
TOPIC = "simulation_modeling"
PREFIX = "dva-"
NS = {"o": "http://www.openarchives.org/OAI/2.0/", "m": "http://www.loc.gov/mods/v3"}
XLINK = "{http://www.w3.org/1999/xlink}href"
FULLTEXT = re.compile(r"^https?://(?:[a-z0-9-]+\.)?diva-portal\.org/smash/get/(diva2:\d+)/"
                      r"(FULLTEXT\d+\.pdf)$", re.I)
SV_BUILT = re.compile(
    r"byggnad|bostad|bostäder|flerbostadshus|småhus|kontorsbyggnad|skolbyggnad|värmepump|"
    r"\bventilation|inomhusklimat|inneklimat|fjärrvärme|fjärrkyla|klimatskal|dagsljus|"
    r"termisk komfort|energiprestanda|passivhus|lågenergihus|byggnaders|uppvärmning", re.I)
SIM = re.compile(
    r"simul|ida[ -]?ice|energy ?plus|trnsys|modelica|vip[- ]?energy|designbuilder|\bcfd\b|"
    r"computational fluid|digital twin|energiberäkning|energimodell|energy model|"
    r"building performance model|calibrat|kalibrer|\bbes\b|dynamic model|dynamisk modell", re.I)
# A canonical restricted Creative Commons licence URL (the open ones go through licenses.py).
RESTRICTED_CC = re.compile(r"https?://(?:www\.)?creativecommons\.org/licenses/"
                           r"(by-nc-nd|by-nc-sa|by-nc|by-nd)/\d\.\d(?:/[a-z]{2})?"
                           r"(?:/(?:legalcode(?:\.[a-z]{2})?|deed\.[a-z]{2}))?/?", re.I)
LANGS = {"eng": "en", "swe": "sv", "nor": "no", "dan": "da", "fin": "fi", "ger": "de",
         "fre": "fr", "spa": "es"}
DOC_TYPES = {"studentThesis": "thesis", "doctoralThesis": "thesis", "licentiateThesis": "thesis",
             "report": "report"}


class Unexpected(RuntimeError):
    """Not an OAI-PMH ListRecords answer (challenge, maintenance page, error)."""


def parse_cursor(cursor: str) -> tuple[int, int, str | None]:
    parts = cursor.split(":", 2)
    if len(parts) != 3 or not parts[0].isdigit() or not parts[1].isdigit() or not parts[2]:
        raise ValueError(f"cursor must be '<set>:<year>:<token|START>': {cursor!r}")
    return int(parts[0]), int(parts[1]), None if parts[2] == START else parts[2]


def fetch_page(setname: str, year: int, token: str | None) -> bytes:
    params = ({"verb": "ListRecords", "resumptionToken": token} if token else
              {"verb": "ListRecords", "metadataPrefix": "swepub_mods", "set": setname,
               "from": f"{year}-01-01T00:00:00Z", "until": f"{year}-12-31T23:59:59Z"})
    response = requests.get(OAI, params=params, headers=UA, timeout=180)
    if response.status_code != 200:
        raise Unexpected(f"OAI answered HTTP {response.status_code}")
    return response.content


def _position(token: str | None) -> tuple[int, int, list[str]] | None:
    """Recognize only DiVA's observed year-window tokens; unknown formats stay opaque."""
    parts = (token or "").split("/")
    if (len(parts) != 9 or parts[1:3] != ["diva", "swepub_mods"] or parts[8]
            or parts[5] not in SETS
            or not all(re.fullmatch(r"[0-9]+", parts[i]) for i in (0, 3, 4))
            or not re.fullmatch(r"[0-9]{4}-01-01T00:00:00Z", parts[6])
            or parts[7] != parts[6][:4] + "-12-31T23:59:59Z"):
        return None
    offset, size = int(parts[3]), int(parts[4])
    if not size or offset % size:
        return None
    # Timestamps change on every response. Page size, set and date bounds must not.
    return offset, size, parts[4:]


def _full_final_page(token_el: ET.Element, request_token: str | None,
                     record_count: int) -> bool:
    """DiVA can issue a token one page past the end when the total is a page multiple.

    Live ListRecords and ListIdentifiers probes corroborate that DiVA's cursor attribute names
    the NEXT offset. Require a full, contiguous page and matching total before ignoring that
    spurious token. This is provider-specific, not a generic OAI cursor interpretation.
    """
    successor = _position((token_el.text or "").strip())
    if successor is None:
        return False
    end, size, scope = successor
    previous = _position(request_token) if request_token is not None else (0, size, scope)
    if previous is None or previous[1:] != successor[1:]:
        return False
    return (record_count == size and previous[0] + record_count == end
            and token_el.get("completeListSize") == str(end)
            and token_el.get("cursor") == str(end))


def parse_page(body: bytes, *, request_token: str | None = None
               ) -> tuple[list[ET.Element], str | None]:
    """(mods elements, next token). Initial noRecordsMatch = empty finished window; badResumptionToken
    raises LookupError; anything that is not an OAI ListRecords answer raises Unexpected."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise Unexpected(f"OAI answer is not XML: {exc}") from exc
    if root.tag != f"{{{NS['o']}}}OAI-PMH":
        raise Unexpected(f"OAI answer has root element {root.tag!r}")
    error = root.find("o:error", NS)
    if error is not None:
        code = error.get("code")
        if code == "noRecordsMatch" and request_token is None:
            return [], None
        if code == "badResumptionToken":
            raise LookupError(f"badResumptionToken: {error.text}")
        raise Unexpected(f"OAI error {code}: {error.text}")
    if root.find("o:ListRecords", NS) is None:
        raise Unexpected("OAI-PMH answer has neither ListRecords nor error")
    mods, records = [], root.findall(".//o:record", NS)
    for record in records:
        header = record.find("o:header", NS)
        if header is not None and header.get("status") == "deleted":
            continue
        if (m := record.find(".//m:mods", NS)) is None:
            raise Unexpected("a non-deleted OAI record carries no MODS metadata")
        mods.append(m)
    token_el = root.find(".//o:resumptionToken", NS)
    token = (token_el.text or "").strip() if token_el is not None else ""
    if not records:
        raise Unexpected("ListRecords answer with no records; completion is unproven")
    if token and _full_final_page(token_el, request_token, len(records)):
        token = ""
    return mods, token or None


def _text(elements) -> list[str]:
    return [" ".join((e.text or "").split()) for e in elements if (e.text or "").strip()]


def full_titles(mods: ET.Element) -> list[str]:
    """Every titleInfo as 'Title: Subtitle' (the subtitle carries relevance and identity)."""
    out = []
    for info in mods.iterfind("m:titleInfo", NS):
        parts = _text([*info.iterfind("m:title", NS), *info.iterfind("m:subTitle", NS)])
        if parts:
            out.append(": ".join(parts))
    return out


def licence_of(mods: ET.Element) -> tuple[str, str | None, list[str]]:
    """(tag, licence URL, evidence values) from every accessCondition's link AND text. Open
    grants need EVERY value to be a canonical open licence URL (shared fail-closed parser); a
    canonical NC/ND licence URL that is the ONLY statement keeps its exact restricted tag; anything
    else, including free text beside a link, is unverified."""
    values = []
    for cond in mods.iterfind("m:accessCondition", NS):
        for value in (cond.get(XLINK), "".join(cond.itertext())):
            if value and " ".join(value.split()):
                values.append(" ".join(value.split()))
    if not values:
        return "unverified", None, values
    # Like the restricted case below: an open grant only when EVERY value is itself a canonical
    # open licence URL, so free text ("No commercial use") can never ride along with a link.
    if all(licenses.cc_license([v])[0] for v in values):
        tag, url = licenses.cc_license(values)
        if tag:
            return tag, url, values
    restricted = {m.group(1).lower(): m.group(0) for v in values
                  if (m := RESTRICTED_CC.fullmatch(v))}
    # only when EVERY value is that one canonical URL: any other statement may add terms
    if len(restricted) == 1 and all(RESTRICTED_CC.fullmatch(v) for v in values):
        (kind, url), = restricted.items()
        return f"cc-{kind}", url, values
    return "unverified", None, values


def candidate(mods: ET.Element, today: str) -> dict | None:
    titles = full_titles(mods)
    keywords = _text(mods.iterfind("m:subject/m:topic", NS))
    if not titles:
        return None
    url = None
    for loc in mods.iterfind("m:location/m:url", NS):
        match = FULLTEXT.match((loc.text or "").strip())
        if match and loc.get("note") == "free" and loc.get("displayLabel") == "fulltext":
            url = f"https://www.diva-portal.org/smash/get/{match.group(1)}/{match.group(2)}"
            break
    if url is None or any(bes_relevance.vetoed(t) for t in titles):
        return None
    text = " ; ".join(titles + keywords)
    # Modelica is building-specific in a lab repository, not in a university one ("inverted
    # pendulum in Modelica"): it counts as simulation evidence only.
    anchor_text = re.sub(r"\bmodelica\b", " ", text, flags=re.I)
    built = bes_relevance.BUILT.search(anchor_text) or SV_BUILT.search(anchor_text)
    if not (built and SIM.search(text)):
        return None
    tag, licence_url, conditions = licence_of(mods)
    title = titles[0]
    entry = {
        "id": f"{PREFIX}{registry.slug(title)[:52]}", "title": title[:150], "url": url,
        "source": SOURCE, "license": tag, "topic": TOPIC, "format": "pdf",
        "license_evidence": (f"DiVA accessCondition: {'; '.join(conditions)[:200]}"
                             if tag != "unverified" else
                             "DiVA swepub_mods: full text marked free; no verifiable reuse "
                             "licence" + (f" (accessCondition: {'; '.join(conditions)[:150]})"
                                          if conditions else "")),
        "rights_verified_at": today,
    }
    if licence_url:
        entry["license_url"] = licence_url
    genre = next((g.text for g in mods.iterfind("m:genre", NS)
                  if g.get("type") == "publicationTypeCode"), None)
    if genre in DOC_TYPES:
        entry["document_type"] = DOC_TYPES[genre]
    lang = mods.find("m:language/m:languageTerm", NS)
    if lang is not None and (code := LANGS.get((lang.text or "").strip().lower())):
        entry["language"] = code
    if (issued := mods.findtext("m:originInfo/m:dateIssued", default="", namespaces=NS))[:4]:
        if issued[:4].isdigit():
            entry["published_at"] = issued[:4]
    record_id = mods.findtext("m:recordInfo/m:recordIdentifier", default="", namespaces=NS)
    if record_id.startswith("diva2:"):
        entry["persistent_id"] = record_id
    return entry


def main() -> None:
    ap = argparse.ArgumentParser()
    now = datetime.now(timezone.utc)
    ap.add_argument("--cursor", default=f"0:{now.year}:{START}",
                    help="'<set index>:<year>:<resumptionToken|START>' (rotation pointer)")
    ap.add_argument("--pages", type=int, default=6, help="max OAI pages this run")
    ap.add_argument("--max", type=int, default=40,
                    help="page-granular target for new entries (the last page may exceed it)")
    ap.add_argument("--append", action="store_true", help="append into registry/diva.yaml")
    args = ap.parse_args()
    if args.pages < 1 or args.max < 1:
        ap.error("--pages and --max must be positive")
    report = Report()
    if args.cursor == "END":
        report.exhausted(f"all {len(SETS)} DiVA sets walked down to {FLOOR_YEAR}")
        print("# 0 NEW DiVA records (cursor END)")
        return
    try:
        s, year, token = parse_cursor(args.cursor)
    except ValueError as exc:
        ap.error(str(exc))

    today = now.date().isoformat()
    keys = dedup.open_keys()
    out: list[dict] = []
    scanned = pages = 0
    restarted = False
    while s < len(SETS) and pages < args.pages and len(out) < args.max:
        if pages:
            time.sleep(CRAWL_DELAY)
        try:
            mods, next_token = parse_page(fetch_page(SETS[s], year, token), request_token=token)
        except LookupError as exc:
            if restarted or token is None:
                report.hold(f"{exc} at {s}:{year}; nothing proposed")
                print("# 0 NEW DiVA records (rejected token)")
                return
            print(f"# {exc}; restarting {SETS[s]} {year}", file=sys.stderr)
            restarted, token = True, None
            pages += 1
            continue
        except Unexpected as exc:
            report.hold(f"{exc} at {s}:{year}:{token or START}; nothing proposed")
            print("# 0 NEW DiVA records (unexpected OAI answer)")
            return
        except Exception as exc:
            print(f"# ERROR: DiVA OAI page failed at {s}:{year}:{token or START}: {exc}; "
                  "refusing a partial append so rotation does not advance", file=sys.stderr)
            raise SystemExit(1)
        pages += 1
        scanned += len(mods)
        entries = [e for e in (candidate(m, today) for m in mods) if e]
        keys.prefetch(**dedup.page_keys(entries))
        for entry in entries:
            u, t = entry["url"].rstrip("/"), registry.norm(entry["title"])
            if u in keys.urls or t in keys.titles or keys.identity_known(entry):
                continue
            keys.urls.add(u)
            keys.titles.add(t)
            keys.add_identity(entry)
            out.append(entry)
        if next_token:
            token = next_token
        elif year > FLOOR_YEAR:
            year, token = year - 1, None
        else:
            s, year, token = s + 1, now.year, None

    keys.uniquify_ids(out)
    by_licence: dict[str, int] = {}
    for entry in out:
        by_licence[entry["license"]] = by_licence.get(entry["license"], 0) + 1
    print(f"# {len(out)} NEW DiVA simulation records (kept {len(out)}/{scanned} over {pages} OAI "
          f"pages; deduped vs manifest + registry + blocklist)")
    print(f"# by licence: {by_licence}")
    print("# --- review, then --append, then scripts/build_corpus.py ---")
    print(yaml.safe_dump(out, sort_keys=False, allow_unicode=True))
    if args.append and out:
        counts = registry.append_entries(out)
        print(f"# appended {len(out)} entries to the registry: {counts}", file=sys.stderr)
    if s >= len(SETS):
        report.exhausted(f"all {len(SETS)} DiVA sets walked down to {FLOOR_YEAR}")
    else:
        report.next(f"{s}:{year}:{token or START}")


if __name__ == "__main__":
    main()
