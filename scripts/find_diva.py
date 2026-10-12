#!/usr/bin/env python3
"""find_diva.py — built-environment theses and reports from DiVA (Sweden), two families.

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

Families (`--family`, one backend each, same `dva-` shard and source `diva`):
  * `sim` (backend find_diva, the default): the gate above, topic simulation_modeling.
  * `aec` (backend find_diva_aec): every built-environment subject. Sets studentThesis (all
    subjects, a superset of Technology), doctoralThesis, licentiateThesis and report; a record is
    kept when scripts/diva_aec.py passes its titles, author keywords and research-subject (HSV)
    classification; the topic comes from the same module.

Rotation (dynamic, `--cursor`):
  * history `"<set index>[@<walk start date>]:<year>:<resumptionToken|START>"`: an unexpected
    answer holds the cursor, a rejected token restarts its window. The walk start date is set on
    the first run and carried to the end. A full final page closes its window only when
    DiVA's declared total, continuation offset and actual record count agree for the requested
    set and window; an empty continuation never proves completion.
  * past the last set and FLOOR_YEAR the history is complete and the cursor becomes a WATCH
    cursor `"watch:<YYYY-MM-DD>"` (the backend does not report EXHAUSTED: DiVA keeps publishing).
    A watch cursor makes no request until WATCH_DAYS have passed; then it harvests every set by
    OAI datestamp from one day before that date until today
    (`"watch:<since>:<until>:<set index>:<resumptionToken|START>"` while in progress) and ends
    as `"watch:<until>"`. The first watch after a history walk starts at the walk's start date,
    however long the walk took: a record added or modified after its window was walked carries a
    datestamp on or after that date. (A hand-written history cursor without a start date that
    is not the initial position falls back to 1 January of the previous year.)
  * the legacy cursor `END` still reports EXHAUSTED.

    python scripts/find_diva.py --cursor "0:2025:START" --pages 2 --max 20
    python scripts/find_diva.py --family aec --cursor "watch:2026-10-01" --pages 2 --max 20
"""
from __future__ import annotations

import argparse
import fcntl
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
import yaml

import bes_relevance
import dedup
import diva_aec
import licenses
import registry
from finder_protocol import Report

OAI = "https://www.diva-portal.org/dice/oai"
UA = {"User-Agent": "nekaise-corpus/find_diva (research corpus; robots.txt honoured)"}
CRAWL_DELAY = 10.0  # diva-portal.org robots.txt Crawl-delay for *
# The two family backends run in the same round, concurrently: every OAI request goes through
# this file (exclusive lock + time of the last answer) so that together they still make one
# request per CRAWL_DELAY. None disables the gate (tests).
PACE_FILE: Path | None = Path(__file__).resolve().parents[1] / "workspace" / ".diva-oai.pace"
FLOOR_YEAR = 1995
START = "START"
# Append-only: the cursor indexes this tuple.
SETS = ("Technology", "doctoralThesis", "licentiateThesis", "report")
# Append-only too. studentThesis covers every subject (architecture, planning and real-estate
# theses are classified outside Technology); 2026-10-12: 445,377 + 37,257 + 8,150 + 57,436 records.
AEC_SETS = ("studentThesis", "doctoralThesis", "licentiateThesis", "report")
FAMILIES = {"sim": (SETS, "simulation"), "aec": (AEC_SETS, "AEC")}
KNOWN_SETS = frozenset(SETS + AEC_SETS)
WATCH_DAYS = 7              # a watch cursor harvests new datestamps at most this often
DAY = re.compile(r"\d{4}-\d{2}-\d{2}")
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
LANGS = {"eng": "en", "swe": "sv", "nor": "no", "nob": "no", "nno": "no", "dan": "da",
         "fin": "fi", "ger": "de", "fre": "fr", "spa": "es"}
DOC_TYPES = {"studentThesis": "thesis", "doctoralThesis": "thesis", "licentiateThesis": "thesis",
             "report": "report"}


class Unexpected(RuntimeError):
    """Not an OAI-PMH ListRecords answer (challenge, maintenance page, error)."""


def parse_history(cursor: str) -> tuple[int, date | None, int, str | None]:
    """(set index, walk start date or None, year, token) of a history cursor."""
    parts = cursor.split(":", 2)
    index, _, started = parts[0].partition("@")
    if (len(parts) != 3 or not index.isdigit() or not parts[1].isdigit() or not parts[2]
            or ("@" in parts[0] and not DAY.fullmatch(started))):
        raise ValueError("cursor must be '<set>[@<YYYY-MM-DD>]:<year>:<token|START>': "
                         f"{cursor!r}")
    try:
        start = date.fromisoformat(started) if started else None
    except ValueError as exc:
        raise ValueError(f"cursor names an impossible date: {cursor!r}") from exc
    return int(index), start, int(parts[1]), None if parts[2] == START else parts[2]


def parse_cursor(cursor: str) -> tuple[int, int, str | None]:
    index, _start, year, token = parse_history(cursor)
    return index, year, token


def _day(text: str) -> date:
    if not DAY.fullmatch(text):
        raise ValueError(f"not a YYYY-MM-DD date: {text!r}")
    return date.fromisoformat(text)


def parse_watch(cursor: str) -> tuple[date, date | None, int, str | None]:
    """(since, until, set index, token) of a watch cursor: 'watch:<since>' (idle: until is None)
    or 'watch:<since>:<until>:<set index>:<token|START>' (a harvest in progress)."""
    parts = cursor.split(":", 4)
    try:
        if parts[0] != "watch" or len(parts) not in (2, 5):
            raise ValueError("shape")
        since = _day(parts[1])
        if len(parts) == 2:
            return since, None, 0, None
        until = _day(parts[2])
        if until < since or not parts[3].isdigit() or not parts[4]:
            raise ValueError("fields")
        return since, until, int(parts[3]), None if parts[4] == START else parts[4]
    except ValueError as exc:
        raise ValueError("cursor must be 'watch:<YYYY-MM-DD>' or 'watch:<since>:<until>:<set>:"
                         f"<token|START>': {cursor!r}") from exc


def year_window(year: int) -> tuple[str, str]:
    return f"{year}-01-01T00:00:00Z", f"{year}-12-31T23:59:59Z"


def watch_window(since: date, until: date) -> tuple[str, str]:
    """One day of overlap: a record stamped on the day of the previous walk, after it passed."""
    return f"{since - timedelta(days=1)}T00:00:00Z", f"{until}T23:59:59Z"


def fetch_page(setname: str, window: tuple[str, str], token: str | None) -> bytes:
    params = ({"verb": "ListRecords", "resumptionToken": token} if token else
              {"verb": "ListRecords", "metadataPrefix": "swepub_mods", "set": setname,
               "from": window[0], "until": window[1]})
    response = paced_get(params)
    if response.status_code != 200:
        raise Unexpected(f"OAI answered HTTP {response.status_code}")
    return response.content


def paced_get(params: dict):
    """One OAI request, no sooner than CRAWL_DELAY after the last answer to ANY find_diva
    process on this machine (the families share the host's robots Crawl-delay)."""
    if PACE_FILE is None:
        return requests.get(OAI, params=params, headers=UA, timeout=180)
    PACE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with PACE_FILE.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        handle.seek(0)
        try:
            last = float(handle.read().strip() or 0)
        except ValueError:
            last = 0.0
        wait = last + CRAWL_DELAY - time.time()
        if wait > 0:
            # never longer than one delay (a clock set back must not stall the round), never
            # skipped (the stored time is rounded and may be slightly ahead of this clock)
            time.sleep(min(wait, CRAWL_DELAY))
        try:
            return requests.get(OAI, params=params, headers=UA, timeout=180)
        finally:
            handle.seek(0)
            handle.truncate()
            handle.write(f"{time.time():.6f}")
            handle.flush()


def _position(token: str | None) -> tuple[int, int, list[str]] | None:
    """Recognize only DiVA's observed set + datestamp-window tokens; unknown formats stay opaque."""
    parts = (token or "").split("/")
    if (len(parts) != 9 or parts[1:3] != ["diva", "swepub_mods"] or parts[8]
            or parts[5] not in KNOWN_SETS
            or not all(re.fullmatch(r"[0-9]+", parts[i]) for i in (0, 3, 4))
            or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T00:00:00Z", parts[6])
            or not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T23:59:59Z", parts[7])
            or parts[6] > parts[7]):
        return None
    offset, size = int(parts[3]), int(parts[4])
    if not size or offset % size:
        return None
    # Timestamps change on every response. Page size, set and date bounds must not.
    return offset, size, parts[4:]


def _full_final_page(token_el: ET.Element, request_token: str | None,
                     record_count: int, scope: tuple[str, str, str] | None = None) -> bool:
    """DiVA can issue a token one page past the end when the total is a page multiple.

    Live ListRecords and ListIdentifiers probes corroborate that DiVA's cursor attribute names
    the NEXT offset. Require a full, contiguous page and matching total before ignoring that
    spurious token. This is provider-specific, not a generic OAI cursor interpretation.
    `scope` is the requested (set, from, until): a token for another set or window proves nothing.
    """
    successor = _position((token_el.text or "").strip())
    if successor is None:
        return False
    end, size, token_scope = successor
    if scope is not None and token_scope[1:4] != list(scope):
        return False
    previous = (_position(request_token) if request_token is not None
                else (0, size, token_scope))
    if previous is None or previous[1:] != successor[1:]:
        return False
    return (record_count == size and previous[0] + record_count == end
            and token_el.get("completeListSize") == str(end)
            and token_el.get("cursor") == str(end))


def parse_page(body: bytes, *, request_token: str | None = None,
               scope: tuple[str, str, str] | None = None
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
    if token and _full_final_page(token_el, request_token, len(records), scope):
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


def subjects_of(mods: ET.Element) -> tuple[list[str], list[tuple[str, str]]]:
    """(author keywords, [(HSV code, English leaf name)]). The research-subject classification
    (`<subject authority="hsv" xlink:href="20103">`) is kept apart from the free keywords."""
    keywords: list[str] = []
    codes: list[tuple[str, str]] = []
    for subject in mods.iterfind("m:subject", NS):
        topics = _text(subject.iterfind("m:topic", NS))
        if subject.get("authority") != "hsv":
            keywords.extend(topics)
        elif (code := (subject.get(XLINK) or "").strip()).isdigit():
            name = topics[-1] if topics and subject.get("lang") == "eng" else ""
            if (code, name) not in codes:
                codes.append((code, name))
    return keywords, codes


def sim_gate(titles: list[str], keywords: list[str]) -> bool:
    """A building concept AND a simulation concept in titles + keywords (Swedish or English)."""
    if any(bes_relevance.vetoed(t) for t in titles):
        return False
    text = " ; ".join(titles + keywords)
    # Modelica is building-specific in a lab repository, not in a university one ("inverted
    # pendulum in Modelica"): it counts as simulation evidence only.
    anchor_text = re.sub(r"\bmodelica\b", " ", text, flags=re.I)
    built = bes_relevance.BUILT.search(anchor_text) or SV_BUILT.search(anchor_text)
    return bool(built and SIM.search(text))


def candidate(mods: ET.Element, today: str, family: str = "sim") -> dict | None:
    titles = full_titles(mods)
    if not titles:
        return None
    url = None
    for loc in mods.iterfind("m:location/m:url", NS):
        match = FULLTEXT.match((loc.text or "").strip())
        if match and loc.get("note") == "free" and loc.get("displayLabel") == "fulltext":
            url = f"https://www.diva-portal.org/smash/get/{match.group(1)}/{match.group(2)}"
            break
    if url is None:
        return None
    if family == "sim":
        # the simulation family reads every subject topic, the classification names included
        if not sim_gate(titles, _text(mods.iterfind("m:subject/m:topic", NS))):
            return None
        topic = TOPIC
    else:
        keywords, codes = subjects_of(mods)
        if not diva_aec.relevant(titles, keywords, codes):
            return None
        topic = diva_aec.topic(titles, keywords, codes)
    tag, licence_url, conditions = licence_of(mods)
    title = titles[0]
    entry = {
        "id": f"{PREFIX}{registry.slug(title)[:52]}", "title": title[:150], "url": url,
        "source": SOURCE, "license": tag, "topic": topic, "format": "pdf",
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


class Walk:
    """Where the next request goes: a history year window or a watch window of one set."""

    def __init__(self, sets: tuple[str, ...], cursor: str, today: date):
        self.sets, self.today = sets, today
        self.since: date | None = None   # watch mode when set
        self.until: date | None = None
        self.started: date | None = None  # history mode: the day the walk began
        if cursor.startswith("watch"):
            self.since, self.until, self.s, self.token = parse_watch(cursor)
            self.year = today.year
            active = self.until is not None
        else:
            self.s, self.started, self.year, self.token = parse_history(cursor)
            active = True
            if self.started is None and (self.s, self.year, self.token) == (0, today.year, None):
                self.started = today   # the initial position: the walk begins now
        if active and self.s >= len(sets):
            # an out-of-range index would end the walk without a single request
            raise ValueError(f"cursor names set {self.s}, the family has {len(sets)}: {cursor!r}")

    @property
    def watching(self) -> bool:
        return self.since is not None

    def due(self) -> bool:
        """An idle watch cursor waits WATCH_DAYS; a harvest in progress always continues."""
        return self.until is not None or self.today >= self.since + timedelta(days=WATCH_DAYS)

    def begin(self) -> None:
        if self.watching and self.until is None:
            self.until, self.s, self.token = max(self.today, self.since), 0, None

    @property
    def done(self) -> bool:
        return self.s >= len(self.sets)

    def window(self) -> tuple[str, str]:
        return (watch_window(self.since, self.until) if self.watching
                else year_window(self.year))

    def scope(self) -> tuple[str, str, str]:
        return (self.sets[self.s], *self.window())

    def where(self) -> str:
        return self.cursor() if not self.done else "the end"

    def restart(self) -> None:
        self.token = None

    def advance(self, next_token: str | None) -> None:
        if next_token:
            self.token = next_token
        elif self.watching or self.year <= FLOOR_YEAR:
            self.s, self.year, self.token = self.s + 1, self.today.year, None
        else:
            self.year, self.token = self.year - 1, None

    def cursor(self) -> str:
        """The cursor to persist. A finished history becomes a watch from the day the walk
        began; a finished watch becomes idle at the day its window ended."""
        if self.watching:
            if self.done:
                return f"watch:{self.until}"
            return f"watch:{self.since}:{self.until}:{self.s}:{self.token or START}"
        if self.done:
            # no start date (a hand-written mid-walk cursor): a bounded, conservative watermark
            return f"watch:{self.started or date(self.today.year - 1, 1, 1)}"
        index = f"{self.s}@{self.started}" if self.started else str(self.s)
        return f"{index}:{self.year}:{self.token or START}"


def main() -> None:
    ap = argparse.ArgumentParser()
    now = datetime.now(timezone.utc)
    ap.add_argument("--family", choices=sorted(FAMILIES), default="sim",
                    help="sim: building AND simulation concept (find_diva); aec: every "
                         "built-environment subject (find_diva_aec)")
    ap.add_argument("--cursor", default=f"0:{now.year}:{START}",
                    help="'<set index>[@<start date>]:<year>:<resumptionToken|START>', "
                         "'watch:<YYYY-MM-DD>' or "
                         "'watch:<since>:<until>:<set index>:<token|START>' (rotation pointer)")
    ap.add_argument("--pages", type=int, default=6, help="max OAI pages this run")
    ap.add_argument("--max", type=int, default=40,
                    help="page-granular target for new entries (the last page may exceed it)")
    ap.add_argument("--append", action="store_true", help="append into registry/diva.yaml")
    args = ap.parse_args()
    if args.pages < 1 or args.max < 1:
        ap.error("--pages and --max must be positive")
    sets, label = FAMILIES[args.family]
    report = Report()
    if args.cursor == "END":
        report.exhausted(f"all {len(sets)} DiVA sets walked down to {FLOOR_YEAR}")
        print(f"# 0 NEW DiVA {label} records (cursor END)")
        return
    try:
        walk = Walk(sets, args.cursor, now.date())
    except ValueError as exc:
        ap.error(str(exc))
    if walk.watching and not walk.due():
        print(f"# 0 NEW DiVA {label} records (datestamps harvested to {walk.since}; next look "
              f"{walk.since + timedelta(days=WATCH_DAYS)})")
        report.next(args.cursor)
        return
    walk.begin()

    today = now.date().isoformat()
    keys = dedup.open_keys()
    out: list[dict] = []
    scanned = pages = 0
    restarted = False
    while not walk.done and pages < args.pages and len(out) < args.max:
        if pages:
            time.sleep(CRAWL_DELAY)
        try:
            mods, next_token = parse_page(
                fetch_page(sets[walk.s], walk.window(), walk.token),
                request_token=walk.token, scope=walk.scope())
        except LookupError as exc:
            if restarted or walk.token is None:
                report.hold(f"{exc} at {walk.where()}; nothing proposed")
                print(f"# 0 NEW DiVA {label} records (rejected token)")
                return
            print(f"# {exc}; restarting {sets[walk.s]} {walk.window()[0][:10]}", file=sys.stderr)
            restarted = True
            walk.restart()
            pages += 1
            continue
        except Unexpected as exc:
            report.hold(f"{exc} at {walk.where()}; nothing proposed")
            print(f"# 0 NEW DiVA {label} records (unexpected OAI answer)")
            return
        except Exception as exc:
            print(f"# ERROR: DiVA OAI page failed at {walk.where()}: {exc}; "
                  "refusing a partial append so rotation does not advance", file=sys.stderr)
            raise SystemExit(1)
        pages += 1
        scanned += len(mods)
        entries = [e for e in (candidate(m, today, args.family) for m in mods) if e]
        keys.prefetch(**dedup.page_keys(entries))
        for entry in entries:
            u, t = entry["url"].rstrip("/"), registry.norm(entry["title"])
            if u in keys.urls or t in keys.titles or keys.identity_known(entry):
                continue
            keys.urls.add(u)
            keys.titles.add(t)
            keys.add_identity(entry)
            out.append(entry)
        walk.advance(next_token)

    keys.uniquify_ids(out)
    by_licence: dict[str, int] = {}
    by_topic: dict[str, int] = {}
    for entry in out:
        by_licence[entry["license"]] = by_licence.get(entry["license"], 0) + 1
        by_topic[entry["topic"]] = by_topic.get(entry["topic"], 0) + 1
    print(f"# {len(out)} NEW DiVA {label} records (kept {len(out)}/{scanned} over {pages} OAI "
          f"pages; deduped vs manifest + registry + blocklist)")
    print(f"# by licence: {by_licence}")
    print(f"# by topic: {by_topic}")
    print("# --- review, then --append, then scripts/build_corpus.py ---")
    print(yaml.safe_dump(out, sort_keys=False, allow_unicode=True))
    if args.append and out:
        counts = registry.append_entries(out)
        print(f"# appended {len(out)} entries to the registry: {counts}", file=sys.stderr)
    report.next(walk.cursor())


if __name__ == "__main__":
    main()
