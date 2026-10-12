#!/usr/bin/env python3
"""find_kitopen.py — harvest KIT's open repository (KITopen) via OAI-PMH.

publikationen.bibliothek.kit.edu exposes KIT's institutional repository over OAI-PMH (`/oai`,
the repository's harvesting interface). KITopen files the built environment under two Dewey
sets, walked in this order (SETS, append-only):

  * `ddc:720` Architektur, Städtebau (1,438 records on 2026-10-12, about a third with a full
    text): architecture, urban design and planning.
  * `ddc:690` Hausbau, Bauhandwerk (9,346 records, about half with a full text): KIT files its
    whole civil-engineering output here — structures, concrete, geotechnics, construction
    management, building physics, transport planning.

`ddc:620` (engineering, 98,039 records) stays out: sampled 2026-09-02 and again 2026-10-12 with
a title gate, it is batteries, fusion, electronics and generic materials (6 of 222 full texts
passed the gate and none was built environment). The corpus takes all languages, so German and
English work are both kept. Both sets were walked once before for CC BY / BY-SA / CC0 records
only; this walk collects regardless of licence.

A record is kept if one of its `dc:identifier` values is a publikationen.bibliothek.kit.edu URL
with path depth >= 2 (`/{recordId}/{fulltextId}` — the direct-PDF link); the bare record-landing
URL (depth 1) and DOI-only identifiers are not full text and are skipped.

Licence, from the record's `dc:rights` values (the access flags `info:eu-repo/semantics/...`
say who may read, not what reuse is granted, and are ignored):
  * an open Creative Commons / public-domain grant by the shared fail-closed parser
    (scripts/licenses.py): `cc-by`, `cc-by-sa`, `cc0`, `public-domain`;
  * exactly one canonical NC / ND Creative Commons URL and no other statement: its exact tag
    (`cc-by-nc`, `cc-by-nc-sa`, `cc-by-nd`, `cc-by-nc-nd`);
  * the "KITopen License" alone: `publisher-oa` — free to read in KITopen; all rights stay with
    the authors, use for scholarly and personal purposes only, no commercial use or modification
    (https://publikationen.bibliothek.kit.edu/kitopen-lizenz);
  * anything else, including no statement or conflicting statements: `unverified`.

Rotation (dynamic, `--cursor`), three forms:
  * `walk:<walk start date>:<set index>:<resumptionToken|START>` — the history walk, set by
    set. The short form `<set index>:<resumptionToken|START>` starts a walk today.
  * `watch:<YYYY-MM-DD>` — history is walked; nothing is requested until WATCH_DAYS after that
    date. Then a watch pass starts.
  * `watch:<since>:<started>:<set index>:<resumptionToken|START>` — a watch pass: every set
    again with the OAI bound `from=<since minus OVERLAP_DAYS>` (records created or changed
    since; KITopen re-stamps many records, the URL / title dedup absorbs them). When it ends
    the cursor becomes `watch:<started>`.
The walk ends in `watch:<walk start date>`, so the first pass reads everything deposited or
changed since the walk began, however long the walk took; never in EXHAUSTED: KIT keeps
depositing. A set index outside SETS is rejected (an error, not a finished walk). A rejected or
expired resumptionToken (KITopen tokens live one day) restarts its set; any answer that is not
an OAI-PMH ListRecords page HOLDs the cursor.

    python scripts/find_kitopen.py --cursor 0:START --pages 2 --max 15
"""
from __future__ import annotations

import argparse
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from urllib.parse import urlparse

import requests
import yaml

import dedup
import licenses
import registry
from finder_protocol import Report

# robots.txt (2026-10-12): `Disallow: /oai/`, `Allow: /`. The harvesting endpoint is the path
# `/oai` (no trailing slash), which that rule does not match, and it answers 200 directly, with
# no redirect (verified 2026-10-12). fetch_page never follows a redirect: any 3xx, e.g. to an
# excluded `/oai/...` path, is an Unexpected answer and HOLDs the cursor.
OAI = "https://publikationen.bibliothek.kit.edu/oai"
UA = {"User-Agent": "nekaise-corpus/find_kitopen (research corpus)"}
FULLTEXT_HOST = "publikationen.bibliothek.kit.edu"
DELAY = 1.0  # politeness between OAI pages
START = "START"
WATCH_DAYS = 7
OVERLAP_DAYS = 2
# Append-only: the cursor indexes this tuple. (OAI set, default topic)
SETS = (("ddc:720", "architecture"), ("ddc:690", "structures_civil"))

NS = {
    "oai": "http://www.openarchives.org/OAI/2.0/",
    "dc": "http://purl.org/dc/elements/1.1/",
}

# (title regex -> topic), first match wins; default: the set's topic.
TOPIC_RULES = [
    (re.compile(r"energie|wärme|heiz|energy|thermal", re.I), "building_energy"),
    (re.compile(r"beton|holz|baustoff|werkstoff|concrete|cement|timber", re.I), "materials"),
    (re.compile(r"stadt|urban|quartier|\bcity\b|cities", re.I), "urban"),
    (re.compile(r"brücke|tunnel|tragwerk|seismic|erdbeben|bridge|geotechn|structural", re.I),
     "structures_civil"),
    (re.compile(r"klima|lüftung|hvac", re.I), "equipment_systems"),
    (re.compile(r"verkehr|transport|travel|mobility|mobilität|traffic", re.I), "infrastructure"),
]

ACCESS_FLAG = re.compile(r"info:eu-repo/semantics/\w+", re.I)
# A canonical restricted Creative Commons licence URL (the open ones go through licenses.py).
RESTRICTED_CC = re.compile(r"https?://(?:www\.)?creativecommons\.org/licenses/"
                           r"(by-nc-nd|by-nc-sa|by-nc|by-nd)/\d\.\d(?:/[a-z]{2})?"
                           r"(?:/(?:legalcode(?:\.[a-z]{2})?|deed\.[a-z]{2}))?/?", re.I)
KITOPEN_LICENSE = re.compile(
    r"KITopen License, https://publikationen\.bibliothek\.kit\.edu/kitopen-lizenz/?", re.I)
KITOPEN_LICENSE_URL = "https://publikationen.bibliothek.kit.edu/kitopen-lizenz"
DOI_RELATION = re.compile(r"info:eu-repo/semantics/altIdentifier/doi/(10\.\S+)", re.I)
DOC_TYPES = {"doc-type:doctoralThesis": "thesis", "doc-type:masterThesis": "thesis",
             "doc-type:bachelorThesis": "thesis", "doc-type:StudyThesis": "thesis",
             "doc-type:report": "report", "doc-type:article": "journal-article",
             "doc-type:conferenceObject": "conference-paper", "doc-type:book": "book",
             "doc-type:bookPart": "book-chapter"}
LANGS = {"eng": "en", "ger": "de", "deu": "de", "fre": "fr", "fra": "fr", "spa": "es",
         "ita": "it", "por": "pt"}
DAY = r"\d{4}-\d{2}-\d{2}"
WALK = re.compile(rf"^(?:walk:({DAY}):)?(\d+):(.+)$", re.S)
IDLE = re.compile(rf"^watch:({DAY})$")
PASS = re.compile(rf"^watch:({DAY}):({DAY}):(\d+):(.+)$", re.S)


class Unexpected(RuntimeError):
    """Not an OAI-PMH ListRecords answer (maintenance page, challenge, error)."""


def topic_for(title: str, default: str = "structures_civil") -> str:
    for pat, topic in TOPIC_RULES:
        if pat.search(title or ""):
            return topic
    return default


def license_for(rights: list[str]) -> tuple[str, str | None, str]:
    """(licence tag, licence URL, evidence) from dc:rights; see the module docstring. Access
    flags are not licence statements. Fail-closed: any combination that is not exactly one
    recognised grant is `unverified`."""
    values = [" ".join(v.split()) for v in rights if v and v.strip()]
    statements = [v for v in values if not ACCESS_FLAG.fullmatch(v)]
    if not statements:
        return "unverified", None, ("KITopen dc:rights states no licence"
                                    + (f" (access flag {values[0]})" if values else ""))
    shown = "; ".join(statements)[:200]
    tag, url = licenses.cc_license(statements)
    if tag and all(licenses.cc_license([v])[0] for v in statements):
        return tag, url, f"KITopen dc:rights: {shown}"
    if len(statements) == 1:
        if match := RESTRICTED_CC.fullmatch(statements[0]):
            return f"cc-{match.group(1).lower()}", statements[0], f"KITopen dc:rights: {shown}"
        if KITOPEN_LICENSE.fullmatch(statements[0]):
            return "publisher-oa", KITOPEN_LICENSE_URL, (
                "KITopen dc:rights: KITopen License — free to read in KITopen; rights stay with "
                "the authors, scholarly and personal use only, no commercial use or modification")
    return "unverified", None, f"KITopen dc:rights is not one recognised licence: {shown}"


def fulltext_url(identifiers: list[str]) -> str | None:
    """The identifier that's a direct KITopen fulltext link: same host, path depth >= 2
    (`/{recordId}/{fulltextId}`). Bare record pages (depth 1) and DOIs are not fulltext."""
    for ident in identifiers:
        u = urlparse(ident)
        if u.netloc != FULLTEXT_HOST:
            continue
        parts = [p for p in u.path.split("/") if p]
        if len(parts) >= 2:
            return ident
    return None


def _day(text: str) -> date:
    return date.fromisoformat(text)


def parse_cursor(cursor: str, today: date | None = None) -> dict:
    """{'idle': bool, 'set': int, 'token': str|None, 'since': date, 'started': date|None}.
    `since` is the watch date (idle, pass) or the walk's start date (walk; `today` for the
    short form); `started` is set only inside a watch pass. Raises ValueError, also for a set
    index outside SETS."""
    cursor = cursor.strip()
    state = None
    if match := IDLE.match(cursor):
        return {"idle": True, "set": 0, "token": None, "since": _day(match.group(1)),
                "started": None}
    if match := PASS.match(cursor):
        state = {"idle": False, "set": int(match.group(3)),
                 "token": None if match.group(4) == START else match.group(4),
                 "since": _day(match.group(1)), "started": _day(match.group(2))}
    elif (match := WALK.match(cursor)) and not cursor.startswith("watch"):
        since = _day(match.group(1)) if match.group(1) else (
            today or datetime.now(timezone.utc).date())
        state = {"idle": False, "set": int(match.group(2)),
                 "token": None if match.group(3) == START else match.group(3),
                 "since": since, "started": None}
    if state is None:
        raise ValueError("cursor must be '[walk:<date>:]<set index>:<token|START>', "
                         "'watch:<date>' or 'watch:<since>:<started>:<set index>:"
                         f"<token|START>': {cursor!r}")
    if not 0 <= state["set"] < len(SETS):
        raise ValueError(f"set index {state['set']} is outside find_kitopen.SETS "
                         f"(0..{len(SETS) - 1}): {cursor!r}")
    return state


def cursor_text(state: dict) -> str:
    if state["idle"]:
        return f"watch:{state['since']}"
    tail = f"{state['set']}:{state['token'] or START}"
    if state["started"] is not None:
        return f"watch:{state['since']}:{state['started']}:{tail}"
    return f"walk:{state['since']}:{tail}"


def fetch_page(set_: str, token: str | None, since: date | None = None) -> str:
    if token:
        params = {"verb": "ListRecords", "resumptionToken": token}
    else:
        params = {"verb": "ListRecords", "metadataPrefix": "oai_dc", "set": set_}
        if since is not None:
            params["from"] = since.isoformat()
    r = requests.get(OAI, params=params, headers=UA, timeout=120, allow_redirects=False)
    if r.status_code != 200:
        raise Unexpected(f"OAI answered HTTP {r.status_code}")
    return r.text


def parse_page(xml_text: str, *, first: bool = True) -> tuple[list[dict], str | None]:
    """-> (records, next token or None). noRecordsMatch on a first page is an empty, finished
    window; badResumptionToken raises LookupError; anything that is not an OAI-PMH ListRecords
    answer raises Unexpected, so an unexpected page never reads as "set finished"."""
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError as exc:
        raise Unexpected(f"OAI answer is not XML: {exc}") from exc
    if root.tag != f"{{{NS['oai']}}}OAI-PMH":
        raise Unexpected(f"OAI answer has root element {root.tag!r}, not OAI-PMH")
    err = root.find("oai:error", NS)
    if err is not None:
        code = err.get("code")
        if code == "noRecordsMatch" and first:
            return [], None
        if code == "badResumptionToken":
            raise LookupError(f"OAI badResumptionToken: {err.text}")
        raise Unexpected(f"OAI error {code}: {err.text}")
    if root.find("oai:ListRecords", NS) is None:
        raise Unexpected("OAI-PMH answer has neither ListRecords nor error")
    records = []
    raw = root.findall(".//oai:record", NS)
    if not raw:
        # an empty window answers noRecordsMatch; an empty ListRecords proves nothing
        raise Unexpected("ListRecords answer with no records; completion is unproven")
    for rec in raw:
        header = rec.find("oai:header", NS)
        if header is not None and header.get("status") == "deleted":
            continue
        metadata = rec.find("oai:metadata", NS)
        dc = metadata[0] if metadata is not None and len(metadata) else None  # oai_dc:dc
        if dc is None:
            continue

        def values(name: str) -> list[str]:
            return [(e.text or "").strip() for e in dc.findall(f"dc:{name}", NS)
                    if (e.text or "").strip()]

        title = " ".join((dc.findtext("dc:title", default="", namespaces=NS) or "").split())
        if title:
            records.append({"title": title, "rights": values("rights"),
                            "identifiers": values("identifier"), "relations": values("relation"),
                            "types": values("type"), "languages": values("language"),
                            "dates": values("date")})
    tok_el = root.find(".//oai:resumptionToken", NS)
    next_token = (tok_el.text or "").strip() if tok_el is not None else ""
    return records, next_token or None


def candidate(rec: dict, default_topic: str, today: str) -> dict | None:
    url = fulltext_url(rec["identifiers"])
    if url is None:
        return None
    title = rec["title"]
    tag, licence_url, evidence = license_for(rec["rights"])
    entry = {"id": f"kit-{registry.slug(title)[:52]}", "title": title[:150], "url": url,
             "source": "kitopen", "license": tag, "topic": topic_for(title, default_topic),
             "format": "pdf", "license_evidence": evidence, "rights_verified_at": today}
    if licence_url:
        entry["license_url"] = licence_url
    if doc_type := next((DOC_TYPES[t] for t in rec.get("types", ()) if t in DOC_TYPES), None):
        entry["document_type"] = doc_type
    if lang := next((LANGS[v.lower()] for v in rec.get("languages", ())
                     if v.lower() in LANGS), None):
        entry["language"] = lang
    if published := next((d[:10] for d in rec.get("dates", ()) if re.match(r"\d{4}", d)), None):
        entry["published_at"] = published
    for relation in rec.get("relations", ()):
        if (match := DOI_RELATION.fullmatch(relation)) and (
                pid := dedup.normalize_pid(match.group(1))):
            entry["persistent_id"] = pid
            break
    return entry


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cursor", default=f"0:{START}",
                    help="'[walk:<date>:]<set index>:<token|START>', 'watch:<date>' or a "
                         "watch-pass cursor")
    ap.add_argument("--pages", type=int, default=10, help="max OAI pages (100 records) this run")
    ap.add_argument("--max", type=int, default=300,
                    help="page-granular target for new entries (the last page may exceed it)")
    ap.add_argument("--append", action="store_true",
                    help="append into the registry (registry/kitopen.yaml)")
    args = ap.parse_args()
    if args.pages < 1 or args.max < 1:
        ap.error("--pages and --max must be positive")
    report = Report()
    now = datetime.now(timezone.utc).date()
    today = now.isoformat()
    try:
        state = parse_cursor(args.cursor, now)
    except ValueError as exc:
        ap.error(str(exc))
    if state["idle"]:
        due = state["since"] + timedelta(days=WATCH_DAYS)
        if now < due:
            print(f"# 0 NEW KITopen records (watching since {state['since']}; next pass {due})")
            report.next(cursor_text(state))
            return
        state = {"idle": False, "set": 0, "token": None, "since": state["since"],
                 "started": now}

    keys = dedup.open_keys()
    out: list[dict] = []
    scanned = pages = 0
    restarted = False
    while not state["idle"] and pages < args.pages and len(out) < args.max:
        if pages:
            time.sleep(DELAY)
        set_, default_topic = SETS[state["set"]]
        bound = (state["since"] - timedelta(days=OVERLAP_DAYS)
                 if state["started"] is not None else None)
        where = cursor_text(state)
        try:
            records, next_token = parse_page(fetch_page(set_, state["token"], bound),
                                             first=state["token"] is None)
        except LookupError as exc:
            if restarted or state["token"] is None:
                report.hold(f"{exc} at {where}; nothing proposed")
                print("# 0 NEW KITopen records (rejected token)")
                return
            print(f"# {exc}; restarting set {set_}", file=sys.stderr)
            restarted, state = True, {**state, "token": None}
            pages += 1
            continue
        except Unexpected as exc:
            report.hold(f"{exc} at {where}; nothing proposed, retrying next round")
            print("# 0 NEW KITopen records (unexpected OAI answer)")
            return
        except Exception as exc:
            print(f"# ERROR: KITopen OAI page failed at {where}: {exc}; refusing a partial "
                  "append so rotation does not advance", file=sys.stderr)
            raise SystemExit(1)
        pages += 1
        scanned += len(records)
        entries = [e for e in (candidate(r, default_topic, today) for r in records) if e]
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
            state = {**state, "token": next_token}
        elif state["set"] + 1 < len(SETS):
            state = {**state, "set": state["set"] + 1, "token": None}
        else:
            # a pass ends at its own start date; the walk ends at the WALK's start date, so
            # the first pass reads everything that changed while the walk ran
            state = {"idle": True, "set": 0, "token": None,
                     "since": state["started"] or state["since"], "started": None}

    keys.uniquify_ids(out)
    by_licence: dict[str, int] = {}
    for entry in out:
        by_licence[entry["license"]] = by_licence.get(entry["license"], 0) + 1
    print(f"# {len(out)} NEW KITopen records (kept {len(out)}/{scanned} scanned over {pages} OAI "
          "pages; full texts of every licence class, deduped vs manifest + registry + "
          "blocklist + DOI)")
    print(f"# by licence: {by_licence}")
    print("# --- review, then --append, then scripts/build_corpus.py ---")
    print(yaml.safe_dump(out, sort_keys=False, allow_unicode=True))

    if args.append and out:
        counts = registry.append_entries(out)
        print(f"# appended {len(out)} entries to the registry: {counts}", file=sys.stderr)
    report.next(cursor_text(state))


if __name__ == "__main__":
    main()
