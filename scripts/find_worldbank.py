#!/usr/bin/env python3
"""find_worldbank.py — World Bank Documents & Reports via the official WDS search API.

The Bank's public Documents & Reports API (documented at
https://documents.worldbank.org/en/publication/documents-reports/api) is a plain REST endpoint:
    https://search.worldbank.org/api/v3/wds?format=json&rows=100&os=<offset>&<field>_exact=...
Response: {"total": N, "documents": {"D<id>": {docty, docdt, display_title, pdfurl, ...}}} — one
JSON dict per document, `pdfurl` is a direct documents.worldbank.org PDF link. It is an API built
for programmatic access (documents.worldbank.org robots.txt for `*`: `Allow:*`, only service
paths disallowed).

Enumeration (re-aimed 2026-10-12). The first design paged a free-text phrase (`qterm`); WDS
searches full text, so deep pages were development-economics reports that mention the phrase
somewhere (paused 2026-08-21). This design sends NO text query. It walks SLICES of the WDS
classification: a research-like major document type (Publications & Research, Economic & Sector
Work, Publications) crossed with a built-environment topic (Urban Development, Transport,
Energy, Water Supply and Sanitation, Communities and Human Settlements, Infrastructure Economics
and Finance), plus the technical series (ESMAP papers, PPIAF, Water and Sanitation Program),
newest first (`srt=docdt&order=desc`), 100 records per request, at most one request per second.

Junk control: only research / sector-work major document types with an explicit multilingual AEC
title anchor are eligible. Operational paperwork (procurement plans, project documents,
disbursement letters, agreements, audits, announcements, speeches) is skipped before it reaches
the registry; the prune gate catches the rest. All languages are kept.

Licence: `unverified` for every new row. WDS records carry no rights field. The World Bank Group
terms (https://www.worldbank.org/en/about/legal/terms-and-conditions, read 2026-10-12) put
datasets and the Open Knowledge Repository under their own terms and say of the rest: "For the
remainder of the Materials, you may make non-commercial uses thereof, but you may not make any
derivative work or commercial use ... without the prior written consent of the relevant member
institution(s)". Many formal publications are CC BY 3.0 IGO in the Open Knowledge Repository,
but that grant is not established for the WDS copy from WDS metadata, and project-cycle
documents include borrower-authored material. So no WDS row is `open` or `cc-by` without
per-document evidence (rows registered before 2026-10-12 keep their recorded tag until the
rights audit, scripts/audit_licence_evidence.py, reviews them).

Rotation (dynamic, `--cursor`), three states; every run reads at most --pages API pages:
  * walk   "s=<index into SLICES (append-only)> o=<record offset> w=<day the walk started>"
           (`w` is added on the first run). Pages of one slice overlap by OVERLAP records.
           After the last slice: "watch:<w> w=<w>" — coverage is complete up to the day the
           walk STARTED, because a record added while it ran sorts ahead of the saved offset.
  * watch  "watch:<boundary day> w=<walk day>": idle. WATCH_DAYS after the boundary a catch-up
           starts; REWALK_DAYS after the walk day the full walk starts again instead.
  * catch  "catch:<boundary day> n=<day this look started> s=<slice> o=<offset> w=<walk day>":
           each slice is read from its newest record back until a page's oldest document date
           is more than BOUNDARY_MARGIN_DAYS before the boundary (or the slice ends). The
           position is persisted, so a look that needs more pages than one run continues in the
           next run; nothing is cut off. After the last slice: "watch:<n> w=<walk day>".
Backdated additions: WDS sorts by document date, so a document added today with a date older
than the margin is not seen by a catch-up. The periodic full re-walk (REWALK_DAYS) reconciles
them; deduplication keeps only the new documents.
Listing shifts. Offset paging over a newest-first listing is exact only while the listing does
not change: additions move records to the front (re-read, never skipped); OVERLAP covers a few
removals; a larger removal before the saved offset skips records, and the same full re-walk
reconciles them.
An unexpected answer (wrong structure, a malformed document value, an empty or short page while
the total says records remain, a slice without any record) holds the cursor and proposes
nothing, also when earlier pages of the same run were good. Out-of-range cursor values and
future dates are refused. The backend never reports EXHAUSTED.

    python scripts/find_worldbank.py --cursor "s=0 o=0" --pages 2 --max 40      # propose
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone

import requests
import yaml

import dedup
import registry
from finder_protocol import Report

API = "https://search.worldbank.org/api/v3/wds"
UA = {"User-Agent": "nekaise-corpus/find_worldbank (research corpus; robots.txt honoured)"}
ROWS = 100           # records per API request
DELAY = 1.0          # seconds between API requests
OVERLAP = 5          # records re-read between consecutive pages of one slice
WATCH_DAYS = 3       # a catch-up look starts this long after the coverage boundary
BOUNDARY_MARGIN_DAYS = 45   # a catch-up reads document dates back to boundary minus this
REWALK_DAYS = 90     # the full walk runs again this long after the last one started
MAX_OFFSET = 1_000_000
FIELDS = "display_title,docdt,pdfurl,docty,majdocty,lang"
SOURCE = "worldbank_wds"
PDF_URL = re.compile(r"^https://documents1?\.worldbank\.org/[^\s]+\.pdf$", re.I)
LICENCE_EVIDENCE = (
    "World Bank Documents & Reports (WDS) record without a rights field. World Bank Group terms "
    "(https://www.worldbank.org/en/about/legal/terms-and-conditions): 'For the remainder of the "
    "Materials, you may make non-commercial uses thereof, but you may not make any derivative "
    "work or commercial use'; a CC BY 3.0 IGO grant (Open Knowledge Repository) is not "
    "established for this copy")

_MAJOR = ("Publications & Research", "Economic & Sector Work", "Publications")
_TOPICS = ("Urban Development", "Transport", "Energy", "Water Supply and Sanitation",
           "Communities and Human Settlements", "Infrastructure Economics and Finance")
# Append-only: the cursor indexes this tuple. Each slice is a set of exact WDS field filters.
SLICES: tuple[dict[str, str], ...] = (
    *({"majdocty_exact": major, "teratopic_exact": topic}
      for major in _MAJOR for topic in _TOPICS),
    {"docty_exact": "ESMAP Paper"},
    {"colti_exact": "Energy Sector Management Assistance Program (ESMAP)"},
    {"colti_exact": "Public-Private Infrastructure Advisory Facility (PPIAF)"},
    {"colti_exact": "Water and sanitation program"},
    {"colti_exact": "Water and Sanitation Program"},
    {"docty_exact": "Water & Sanitation Discussion Paper"},
)
LANGUAGES = {"english": "en", "french": "fr", "spanish": "es", "portuguese": "pt",
             "russian": "ru", "chinese": "zh", "arabic": "ar", "german": "de", "japanese": "ja",
             "turkish": "tr", "vietnamese": "vi", "romanian": "ro", "ukrainian": "uk",
             "polish": "pl", "korean": "ko", "italian": "it", "bahasa (indonesian)": "id",
             "indonesian": "id"}
_DAY = r"(\d{4}-\d{2}-\d{2})"
DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
CURSOR = re.compile(rf"^s=(\d+) o=(\d+)(?: w={_DAY})?$")
WATCH = re.compile(rf"^watch:{_DAY}(?: w={_DAY})?$")
CATCH = re.compile(rf"^catch:{_DAY} n={_DAY} s=(\d+) o=(\d+) w={_DAY}$")


class Unexpected(RuntimeError):
    """An answer that is not the WDS API's JSON document set (maintenance page, error)."""


# Major document types centered on publications, research, and sector analysis. Borrower-authored
# safeguards and other project-cycle paperwork live under Project Documents, so fail closed on
# missing or unknown values. Some API values are semicolon-separated (for example
# "Publications; Publications & Research").
ALLOWED_MAJOR_TYPES = frozenset({"Publications & Research", "Economic & Sector Work"})

# Document types that remain low-value operational/institutional furniture even when WDS places
# them under an allowed major type.  The live API calls the president memo "Memorandum &
# Recommendation of the President", not the older denylist's "memorandum of the president".
JUNK_DOCTY = re.compile(
    r"procurement plan|disbursement|agreement|auditing document|audit report|agenda|"
    r"month(ly)? operational summary|statement of loans|notice|contract|invitation|"
    r"letter|memorandum\s*(?:&|and)\s*recommendation of the president|"
    r"staff appraisal report|project information document|implementation status (?:and|&) "
    r"results report|\bISR\b|environmental and social review summary|\bESRS\b|"
    r"stakeholder engagement plan|announcement|newsletter|project completion report|"
    r"chairman summary|board summary|president'?s speech|procedure and checklist|"
    r"access to information|annual report|directory|world bank atlas|financial flows", re.I)
JUNK_TITLE = re.compile(
    r"procurement plan|disbursement|audit(ed)? report|board meeting calendar|"
    r"memorandum\s*(?:&|and)\s*recommendation of the president", re.I)

# WDS full-text search becomes very fuzzy at depth: generic country-economic reports can rank for
# an AEC query because a phrase appears somewhere in the body.  Require title-level evidence that
# is specific enough to survive this source's development-economics background.  Unlike the
# English-only gov.uk finder, WDS is multilingual, so the anchors cover the same major language
# families as the corpus quality gate.
TITLE_RELEVANCE = re.compile(
    r"\b(?:"
    r"built environment|buildings?|construction(?: industry| sector| materials?| technology)?|"
    r"architectur(?:e|al)|housing|dwellings?|slum upgrading|"
    r"urban (?:planning|design|development|infrastructure|transport|mobility|water|sanitation|"
    r"housing|regeneration)|cities? (?:planning|infrastructure|transport|water|sanitation|housing)|"
    r"building energy|energy efficien(?:cy|t)|energy performance|retrofit|insulat|"
    r"district heat|heat pumps?|heating|cooling|ventilat|air conditioning|hvac|"
    r"roads?|highways?|bridges?|tunnels?|railways?|railroads?|mass transit|public transport|"
    r"pavements?|ports? infrastructure|water supply|water infrastructure|water networks?|"
    r"sanitation|sewerage|wastewater|drainage|stormwater|flood|irrigation|"
    r"concrete|cement|masonry|timber construction|structural engineering|geotechnical|"
    r"fire safety|building codes?|construction standards?|"
    # French
    r"bâtiments?|génie civil|\bBTP\b|travaux publics|secteur routier|logements?|urbanisme|"
    r"infrastructures?|routes?|ponts?|"
    r"assainissement|eau potable|béton|chauffage|isolation|efficacité énergétique|"
    # Spanish / Portuguese / Italian
    r"edificios?|edifícios?|construcción|construção|viviendas?|habitação|urbanización|urbanismo|"
    r"infraestructuras?|infraestruturas?|carreteras?|rodovias?|puentes?|pontes?|saneamiento|"
    r"saneamento|agua potable|abastecimento de água|hormigón|calefacción|aislamiento|"
    r"eficiencia energética|efficienza energetica|edilizia|calcestruzzo|riscaldamento|"
    # German / Dutch / Nordic
    r"gebäude|bauwesen|baustoff|heizung|lüftung|dämmung|tragwerk|brandschutz|"
    r"gebouw|bouwkunde|verwarming|byggnad|bygning|rakennus|uppvärmning|"
    # Russian / Ukrainian
    r"здания?|строительство|гражданское строительство|инфраструктура|дороги|мосты|"
    r"водоснабжение|канализация|бетон|отопление|вентиляция|энергоэффективность"
    r")\b|"
    # CJK and Arabic scripts do not use Latin word boundaries.
    r"建筑|建築|结构|構造|混凝土|暖通|空调|空調|通风|換気|節能|节能|断熱|桥梁|橋梁|隧道|"
    r"施工|城市规划|都市計画|コンクリート|건축|구조|공조|난방|단열|콘크리트|"
    r"المباني|البناء|العمارة|الهندسة المدنية|الخرسانة|كفاءة الطاقة|الطرق|الجسور|"
    r"المياه|الصرف الصحي|التخطيط الحضري",
    re.I,
)


# "Building" as a verb of development prose is not a building.
FIGURATIVE = re.compile(
    r"\b(?:capacity|institution(?:al)?|peace|state|nation|consensus|resilience|trust|skills?|"
    r"coalition|knowledge|team|community|confidence)[- ]?building\b|"
    r"\bbuilding (?:blocks?|capacity|capacities|on|up|a|an|the|back|bridges|resilien\w+|trust|"
    r"human capital|institutions?|consensus|skills|partnerships?|momentum|foundations?|"
    r"effective|better|strong|sustainable|inclusive|competitive)\b|"
    r"\b(?:the|a|on the) road (?:to|toward|towards|ahead|forward)\b|\broad ?maps?\b|\bsilk road\b",
    re.I)


def relevant(title: str, docty: str, majdocty: str) -> bool:
    """Whether one WDS result has a research-like type and explicit AEC title evidence."""
    major_types = {part.strip() for part in (majdocty or "").split(";") if part.strip()}
    if not major_types.intersection(ALLOWED_MAJOR_TYPES):
        return False
    if JUNK_DOCTY.search(docty or "") or JUNK_TITLE.search(title or ""):
        return False
    return bool(TITLE_RELEVANCE.search(FIGURATIVE.sub(" ", title or "")))

# (title regex -> topic), first match wins; default building_energy.
TOPIC_RULES = [
    (re.compile(r"urban|city|cities|municipal", re.I), "urban"),
    (re.compile(r"district heat|heat pump|cooling|heating|boiler|chiller|appliance", re.I),
     "equipment_systems"),
    (re.compile(r"code|standard|regulation|certification|labell?ing", re.I), "standards_protocols"),
    (re.compile(r"cement|concrete|material|brick|timber", re.I), "materials"),
    (re.compile(r"construction|housing|building sector", re.I), "construction"),
    (re.compile(r"grid|transmission|infrastructure|road|transport|railway|highway|bridge|"
                r"water|sanitation|sewer|drainage|flood|irrigation|"
                r"assainissement|saneamiento|saneamento|carretera|rodovia", re.I),
     "infrastructure"),
]


def classify(title: str) -> str:
    for rx, topic in TOPIC_RULES:
        if rx.search(title):
            return topic
    return "building_energy"


def fetch_page(filters: dict[str, str], offset: int) -> tuple[int, int, str | None, list[dict]]:
    """One API page of a slice, newest first -> (total, records on the page, oldest document
    date on the page, relevant docs). Every document value must be an object whose title, PDF
    URL and date are strings when present; anything else is Unexpected."""
    response = requests.get(API, params={
        "format": "json", **filters, "rows": ROWS, "os": offset, "srt": "docdt",
        "order": "desc", "fl": FIELDS}, headers=UA, timeout=60)
    if response.status_code != 200:
        raise Unexpected(f"WDS answered HTTP {response.status_code}")
    try:
        js = response.json()
        total, documents = js["total"], js["documents"]
    except (ValueError, KeyError, TypeError) as exc:
        raise Unexpected(f"WDS answer is not a document set: {exc}") from exc
    if isinstance(total, str) and total.isdigit():
        total = int(total)
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise Unexpected("WDS answer has no integer total")
    if not isinstance(documents, dict):
        raise Unexpected("WDS documents is not an object")
    docs, records, dates = [], 0, []
    for key, d in documents.items():
        if key == "facets":
            continue
        if not isinstance(d, dict) or not all(
                isinstance(d.get(field) or "", str)
                for field in ("display_title", "pdfurl", "docty", "majdocty", "lang", "docdt")):
            raise Unexpected(f"WDS document {key} is malformed")
        records += 1
        docdt = (d.get("docdt") or "")[:10]
        if DATE.fullmatch(docdt):
            dates.append(docdt)
        title = " ".join((d.get("display_title") or "").split())
        pdf = (d.get("pdfurl") or "").strip().replace("http://", "https://", 1)
        docty = (d.get("docty") or "").strip()
        majdocty = (d.get("majdocty") or "").strip()
        if not title or not PDF_URL.match(pdf) or not relevant(title, docty, majdocty):
            continue
        docs.append({"title": title, "pdf_url": pdf, "docty": docty, "majdocty": majdocty,
                     "lang": (d.get("lang") or "").strip(), "docdt": docdt})
    if records > ROWS:
        raise Unexpected(f"WDS page has {records} records, more than the {ROWS} requested")
    if records < ROWS and offset + records < total:
        # an empty or short page while the total says records remain: neither its documents
        # nor its oldest date are evidence
        raise Unexpected(f"WDS page has {records} records at offset {offset} of {total}")
    if not total:
        # every slice had records on 2026-10-12: an empty listing means a classification
        # value no longer matches
        raise Unexpected(f"WDS lists no record for {filters}")
    return total, records, min(dates) if dates else None, docs


def entry_for(doc: dict, today: str) -> dict:
    entry = {"id": f"wbd-{registry.slug(doc['title'])[:52]}", "title": doc["title"][:150],
             "url": doc["pdf_url"], "source": SOURCE, "license": "unverified",
             "topic": classify(doc["title"]), "format": "pdf",
             "license_evidence": LICENCE_EVIDENCE, "rights_verified_at": today}
    if language := LANGUAGES.get((doc.get("lang") or "").lower()):
        entry["language"] = language
    published = doc.get("docdt") or ""
    if DATE.fullmatch(published) and published <= today:
        entry["published_at"] = published
    return entry


def _past_date(text: str, today: date, what: str) -> date:
    try:
        value = date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{what} is not a date: {text!r}") from exc
    if value > today:
        raise ValueError(f"{what} is in the future: {text!r}")
    return value


def _position(s: str, o: str) -> tuple[int, int]:
    index, offset = int(s), int(o)
    if index >= len(SLICES):
        raise ValueError(f"slice index {index} is out of range (0..{len(SLICES) - 1})")
    if offset > MAX_OFFSET:
        raise ValueError(f"record offset {offset} is past {MAX_OFFSET}")
    return index, offset


def parse_cursor(cursor: str, today: date) -> dict:
    """{mode: walk|watch|catch, s, o, walk (day the walk started), boundary (day coverage is
    complete up to; watch and catch), started (day this look started; catch)}."""
    cursor = cursor.strip()
    if match := CURSOR.match(cursor):
        s, o = _position(match.group(1), match.group(2))
        walk = _past_date(match.group(3), today, "walk day") if match.group(3) else today
        return {"mode": "walk", "s": s, "o": o, "walk": walk}
    if match := WATCH.match(cursor):
        boundary = _past_date(match.group(1), today, "watch boundary")
        walk = _past_date(match.group(2), today, "walk day") if match.group(2) else boundary
        if walk > boundary:
            raise ValueError(f"walk day is after the watch boundary: {cursor!r}")
        return {"mode": "watch", "s": 0, "o": 0, "walk": walk, "boundary": boundary}
    if match := CATCH.match(cursor):
        boundary = _past_date(match.group(1), today, "catch-up boundary")
        started = _past_date(match.group(2), today, "catch-up start day")
        s, o = _position(match.group(3), match.group(4))
        walk = _past_date(match.group(5), today, "walk day")
        if not walk <= boundary <= started:
            raise ValueError(f"catch-up days are not in order: {cursor!r}")
        return {"mode": "catch", "s": s, "o": o, "walk": walk, "boundary": boundary,
                "started": started}
    raise ValueError("cursor must be 's=<slice> o=<offset> [w=<day>]', 'watch:<day> [w=<day>]' "
                     f"or 'catch:<day> n=<day> s=<slice> o=<offset> w=<day>': {cursor!r}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cursor", default="s=0 o=0",
                    help="walk 's=<slice> o=<offset> [w=<day>]', 'watch:<day> [w=<day>]' or "
                         "'catch:<day> n=<day> s=<slice> o=<offset> w=<day>' (rotation)")
    ap.add_argument("--pages", type=int, default=6,
                    help=f"max API pages ({ROWS} records each) this run")
    ap.add_argument("--max", type=int, default=120,
                    help="page-granular target for new entries (the last page may exceed it)")
    ap.add_argument("--append", action="store_true",
                    help="append into the registry (registry/worldbank.yaml)")
    args = ap.parse_args()
    if args.pages < 1 or args.max < 1:
        ap.error("--pages and --max must be positive")
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    try:
        state = parse_cursor(args.cursor, now.date())
    except ValueError as exc:
        ap.error(str(exc))
    report = Report()
    mode, s, offset, walk = state["mode"], state["s"], state["o"], state["walk"]
    boundary, started = state.get("boundary"), state.get("started")
    if mode == "watch":
        if now.date() >= walk + timedelta(days=REWALK_DAYS):
            # periodic reconciliation: a document added with an old date sorts below every
            # catch-up boundary; only a full walk finds it (dedup keeps the new ones)
            mode, walk = "walk", now.date()
        elif now.date() < boundary + timedelta(days=WATCH_DAYS):
            print(f"# 0 NEW World Bank docs (covered to {boundary}; next look "
                  f"{boundary + timedelta(days=WATCH_DAYS)})")
            report.next(args.cursor.strip())
            return
        else:
            mode, started = "catch", now.date()
    # a catch-up reads each slice down to this document date, then moves on
    floor = (boundary - timedelta(days=BOUNDARY_MARGIN_DAYS)).isoformat() \
        if mode == "catch" else None

    keys = dedup.open_keys()
    out: list[dict] = []
    scanned = kept = pages = 0
    try:
        while s < len(SLICES) and pages < args.pages and len(out) < args.max:
            if pages:
                time.sleep(DELAY)
            total, records, oldest, docs = fetch_page(SLICES[s], offset)
            pages += 1
            scanned += records
            kept += len(docs)
            entries = [entry_for(d, today) for d in docs]
            keys.prefetch(**dedup.page_keys(entries))
            for entry in entries:
                u, t = entry["url"].rstrip("/"), registry.norm(entry["title"])
                if u in keys.urls or t in keys.titles:
                    continue
                keys.urls.add(u)
                keys.titles.add(t)
                out.append(entry)
            if offset + records >= total or (floor and oldest is not None and oldest < floor):
                s, offset = s + 1, 0    # slice end, or the previous coverage boundary
            else:
                offset += max(1, records - OVERLAP)  # removed records shift later ones up
    except Unexpected as exc:
        report.hold(f"{exc} at s={s} o={offset}; nothing proposed, retrying next round")
        print("# 0 NEW World Bank docs (unexpected answer)")
        return
    except requests.RequestException as exc:
        print(f"# ERROR: WDS request failed at s={s} o={offset}: {exc}; refusing a partial "
              "append so rotation does not advance", file=sys.stderr)
        raise SystemExit(1)

    keys.uniquify_ids(out)
    by_topic: dict = {}
    for h in out:
        by_topic[h["topic"]] = by_topic.get(h["topic"], 0) + 1
    print(f"# {len(out)} NEW World Bank docs ({mode}: title gate kept {kept}/{scanned} records "
          f"over {pages} API pages; deduped vs manifest + registry + blocklist)")
    print(f"# by topic: {by_topic}")
    print("# --- review, then --append, then scripts/build_corpus.py ---")
    print(yaml.safe_dump(out, sort_keys=False, allow_unicode=True))

    if args.append and out:
        counts = registry.append_entries(out)
        print(f"# appended {len(out)} entries to the registry: {counts}", file=sys.stderr)
    if mode == "walk":
        # coverage after a full walk is complete up to the day the walk STARTED
        report.next(f"s={s} o={offset} w={walk}" if s < len(SLICES)
                    else f"watch:{walk} w={walk}")
    else:
        report.next(f"catch:{boundary} n={started} s={s} o={offset} w={walk}"
                    if s < len(SLICES) else f"watch:{started} w={walk}")


if __name__ == "__main__":
    main()
