#!/usr/bin/env python3
"""find_ojs.py — open-access built-environment journals and proceedings on Open Journal Systems.

Many small AEC venues (university presses, societies, institutes, in many languages) run Open
Journal Systems (OJS), which exposes every article over OAI-PMH at `<journal>/oai` with Dublin
Core `dc:rights` (the article's licence URL), `dc:relation` (the galley views,
`<journal>/article/view/<article>/<galley>`) and `dc:format` (the galleys' media types). The
galley's direct download is `<journal>/article/download/<article>/<galley>`. One generic backend
covers them all; scripts/ojs_sites.py lists the reviewed venues (append-only) with the evidence
checked for each: OAI answers, robots.txt allows the OAI and download paths and names no
AI-crawler opt-out, sampled galleys are real PDFs for an honest User-Agent.

Access is re-checked on every visit and on every request: a venue on a fetch-suspended host
(registry/host_policy.json) or whose robots.txt now disallows the OAI or download path is
skipped, and so is one whose robots.txt opts out of AI training (a Disallow for a named AI /
dataset crawler, or Content-Signal ai-train=no: robots_policy.ai_training_refused). Every
request this finder makes (OAI pages, their redirects, robots.txt) goes through
ONE request-start clock per host, at max(DELAY, the venue's reviewed delay, robots Crawl-delay)
seconds — two venues on one host share the clock. The loader checks robots.txt again for every
actual download URL and redirect hop (build_corpus.ROBOTS_CHECKED_PREFIXES).

Licence = classification, never a filter (collect regardless of licence, AGENTS.md). PER RECORD
and FAIL-CLOSED from `dc:rights`:
  * a canonical Creative Commons BY / BY-SA / CC0 / Public Domain Mark URL with no conflicting
    value (shared parser scripts/licenses.py): that open tag;
  * otherwise, when the canonical Creative Commons URLs present name exactly ONE restricted
    licence (NC / ND variants): that exact tag (cc-by-nc, cc-by-nc-sa, cc-by-nd,
    cc-by-nc-nd);
  * in BOTH cases every other value must be a BARE copyright line: marker + year(s), alone
    or followed by a holder made only of names from the record's own dc:creator /
    dc:publisher / dc:source or the venue name ("Copyright (c) 2022 A. Author"). Any other
    word (a reservation, a reuse condition, licence prose) fails closed;
  * anything else (no licence URL, a bare "Copyright (c) <author>" line, prose, conflicting
    URLs): `unverified`. The journal-level licence in ojs_sites.py is review evidence only and
    never licenses a record.

Records without a title or a PDF galley, and front matter (editorials, contents, covers), are
skipped; a venue marked `gate` (a broad engineering journal) keeps only records whose title or
subjects carry built-environment evidence (scripts/ojs_gate.py: an unambiguous term, or an
ambiguous stem inside a built-environment phrase).

Rotation (dynamic, `--cursor "f=<date> s=<date> i=<n> a=<n> d=<bits>"`), never EXHAUSTED:
  * BACKFILL: `d` has one bit per venue (index into SITES; a missing bit is 0). Every run visits
    up to --sites venues whose bit is 0, round-robin from index `a`, and keeps at most --max new
    records per venue. A venue is complete (bit 1) only when ONE walk from its first OAI page
    reaches the end of the list. A capped walk resumes later from its resumption token, kept as
    scratch under workspace/ojs-resume/ (OJS tokens live about a day); a resumed walk that
    reaches the end only clears the token, and the next walk from the first page proves
    completion (a rolled-back round can therefore never hide records).
  * WATCH: `s` is the day the current incremental cycle started, `f` its OAI `from` date, `i`
    the next venue of the cycle. Complete venues are read with `from=f`; after the last venue
    the cursor idles until s + WATCH_DAYS, then a new cycle starts with f = s - 1 day. A venue
    that fails during a watch cycle loses its bit, so the backfill walks it again (dedup makes
    that idempotent) and no window is lost.
A failing venue never blocks the others: its records of this run are dropped (and nothing of
a failed walk is reserved in the dedup keys), the cursor moves on and the venue is retried on
the next pass.

    python scripts/find_ojs.py --site-key jfde --max 20          # one venue, dry run
    python scripts/find_ojs.py --cursor "f=2026-10-12 s=2026-10-12 i=149 a=0 d="
"""
from __future__ import annotations

import argparse
import contextlib
import json
import re
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
from datetime import date, timedelta
from urllib.parse import urljoin

import requests
import yaml

import dedup
import host_policy
import licenses
import ojs_gate
import ops
import registry
import robots_policy
import store
from finder_protocol import Report
from ojs_sites import SITES

UA = {"User-Agent": "nekaise-corpus/find_ojs (research corpus; robots.txt honoured)"}
NS = {"oai": "http://www.openarchives.org/OAI/2.0/", "dc": "http://purl.org/dc/elements/1.1/"}
DELAY = 1.0            # minimum seconds between request starts against one host
MAX_REDIRECTS = 5
MAX_DELAY = 15.0       # a longer robots Crawl-delay skips the venue (review it instead)
WATCH_DAYS = 7         # an incremental cycle over the complete venues starts at most this often
MAX_PAGES = 400        # paging-loop guard for one walk (the largest venue is ~100 pages)
RESUME_DIR = ops.WORKSPACE / "ojs-resume"
RESUME_TTL = 20 * 3600  # OJS resumption tokens expire after about a day
CURSOR = re.compile(r"^f=(\d{4}-\d{2}-\d{2}) s=(\d{4}-\d{2}-\d{2}) i=(\d+) a=(\d+) d=([01]*)$")

_XML_ILLEGAL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")
LANG3 = {"eng": "en", "nld": "nl", "dut": "nl", "deu": "de", "ger": "de", "fra": "fr",
         "fre": "fr", "spa": "es", "por": "pt", "ita": "it", "ind": "id", "tur": "tr",
         "pol": "pl", "ces": "cs", "cze": "cs", "rus": "ru", "ukr": "uk", "swe": "sv",
         "nor": "no", "dan": "da", "fin": "fi", "cat": "ca", "hrv": "hr", "srp": "sr",
         "slv": "sl", "ron": "ro", "rum": "ro", "hun": "hu", "lit": "lt", "zho": "zh",
         "chi": "zh", "jpn": "ja", "kor": "ko", "ara": "ar", "fas": "fa", "per": "fa",
         "msa": "ms", "may": "ms", "ell": "el", "gre": "el", "bul": "bg", "slk": "sk"}
# Canonical restricted Creative Commons licence URL (the open ones go through licenses.py).
RESTRICTED_CC = re.compile(
    r"https?://(?:www\.)?creativecommons\.org/licenses/(by-nc-nd|by-nc-sa|by-nc|by-nd)/\d\.\d"
    r"(?:/[a-z]{2}(?:-[a-z]{2})?)?(?:/(?:legalcode(?:\.[a-z]{2})?|deed\.[a-z]{2}(?:-[a-z]{2})?)?)?/?",
    re.I)
GALLEY = re.compile(r"/article/view/(\d+)/(\d+)/?$")
# Front matter and other non-article records (whole title, any case).
NON_ARTICLE = re.compile(
    r"(?:editorial(?: board| team| note)?|front ?matter|back ?matter|table of contents|"
    r"contents|cover(?: page)?|masthead|preface|foreword|index|errat(?:um|a)|corrigend(?:um|a)|"
    r"full issue|complete issue|list of reviewers|reviewers?|acknowledge?ments?|imprint|"
    r"editorial|presentaci[oó]n|[ií]ndice|sum[aá]rio|sumario|expediente|apresenta[cç][aã]o|"
    r"portada|capa|cr[eé]ditos|pr[oó]logo|pref[aá]cio|editoriale|indice|inhalt(?:sverzeichnis)?|"
    r"vorwort|impressum|sommaire|[ée]ditorial|avant-propos|daftar isi|halaman (?:depan|sampul)|"
    r"i[çc]indekiler|kapak|jenerik|spis tre[śs]ci|od redakcji|obsah|"
    r"содержание|от редакции|редакционная статья|зміст|від редакції)"
    r"(?:\s*(?:vol|v|n|no|nr|n[úu]m|issue|\d)[\w .,:()/-]*)?", re.I)
# A copyright line (the OJS copyright statement, localized): marker, optional (c)/©, year(s)
# and an optional holder. The holder is NOT judged by its wording (a blacklist of restriction
# words fails open): it must be the record's own author(s), publisher or journal, or the venue.
COPYRIGHT_LINE = re.compile(
    r"(?:copyright|©|\(c\)|derechos de autor|direitos? (?:de autor|autorais)|droits d'auteur|"
    r"hak cipta|авторское право|авторське право|авторські права|telif hakk[ıi]|"
    r"prawa autorskie|urheberrecht|diritti d'autore)\s*(?:\(c\)|©)?\s*:?\s*"
    r"(?:19|20)\d{2}(?:\s*[-–]\s*(?:19|20)\d{2})?(?P<holder>[^\n]{0,300})", re.I)
_AND = frozenset({"and", "y", "e", "et", "und", "dan", "ve", "i"})
# The only words a holder may carry besides names of the record: "the author(s)" for its
# creators and "published by" before its publisher (the OJS default statement of several
# presses: "Copyright (c) 2017 The Author(s). Published by <dc:publisher>.").
_GENERIC_HOLDER = frozenset(
    {"the", "author", "authors", "s", "autor", "autores", "es", "los", "el", "os", "o",
     "penulis", "автор", "авторы", "автори", "published", "by", "publicado", "por",
     "diterbitkan", "oleh"})


def _tokens(text: str) -> list[str]:
    """Case- and accent-insensitive word tokens."""
    text = unicodedata.normalize("NFKD", text.casefold())
    return re.findall(r"[^\W_]+", "".join(c for c in text if not unicodedata.combining(c)))


def bare_copyright_line(value: str, creators=(), others=()) -> bool:
    """A copyright line that states year(s) and nothing else, or year(s) and a holder made
    ONLY of names in the record's own metadata — its dc:creator values together with at most
    one of `others` (dc:publisher, dc:source, the venue name) — and the fixed words "the
    author(s)" / "published by" (_GENERIC_HOLDER). An initial stands for a name that starts
    with it. Any other word makes the line a statement: not bare."""
    m = COPYRIGHT_LINE.fullmatch(value.strip())
    if not m:
        return False
    want = [t for t in _tokens(m.group("holder")) if t not in _AND]
    if not want:
        return not m.group("holder").strip(" .,;")
    authors = {t for c in creators for t in _tokens(c)}
    for other in [(), *others]:
        names = authors | {*_tokens(other)} if other else authors
        if all(t in names or t in _GENERIC_HOLDER
               or (len(t) == 1 and any(n.startswith(t) for n in names)) for t in want):
            return True
    return False


class UnexpectedResponse(RuntimeError):
    """The endpoint answered with something other than an OAI-PMH ListRecords page."""


class BadToken(RuntimeError):
    """The repository rejected a resumption token (expired or unknown)."""


class Skipped(RuntimeError):
    """Policy or availability keeps the venue out of this run (no request to its OAI path)."""


# ------------------------------------------------------------------------------------ licence
def license_for(rights: list[str], creators=(), others=()) -> tuple[str, str | None]:
    """(licence tag, canonical licence URL or None) for one record's dc:rights; fail-closed to
    ("unverified", None). `creators` / `others` are the record's own holder names (see
    bare_copyright_line). See the module docstring."""
    values = [v.strip() for v in rights if v and v.strip()]
    urls = [v for v in values if licenses._CC_URL.fullmatch(v)]
    # every value that is not a canonical Creative Commons URL must be a bare copyright line
    if not urls or any(not bare_copyright_line(v, creators, others)
                       for v in values if v not in urls):
        return "unverified", None
    tag, url = licenses.cc_license(urls)
    if tag:
        return tag, url
    restricted: dict[str, str] = {}
    for value in urls:
        if m := RESTRICTED_CC.fullmatch(value):
            restricted.setdefault(m.group(1).lower(), value)
        else:
            return "unverified", None     # an open CC URL beside a restricted one
    if len(restricted) == 1:
        (kind, url), = restricted.items()
        return f"cc-{kind}", url
    return "unverified", None


# ------------------------------------------------------------------------------------ records
def galley_download(base: str, relations: list[str], formats: list[str] = ()) -> str | None:
    """The PDF galley of this journal as its direct download URL: the `article/view/<a>/<g>`
    relation whose dc:format is application/pdf when formats pair up with the galley relations,
    else the first galley relation."""
    pat = re.compile(re.escape(base.split("://", 1)[1]) + r"/article/view/(\d+)/(\d+)/?$")
    galleys = []
    for rel in relations:
        rel = rel.strip()
        if m := pat.fullmatch(rel.split("://", 1)[1] if "://" in rel else rel):
            galleys.append(m.groups())
    if not galleys:
        return None
    pick = galleys[0]
    media = [f.strip().lower() for f in formats]
    if len(media) == len(galleys):
        pdf = [g for g, f in zip(galleys, media) if f == "application/pdf"]
        if not pdf:
            return None       # every galley is HTML / XML / EPUB: nothing the PDF loader reads
        pick = pdf[0]
    return f"{base}/article/download/{pick[0]}/{pick[1]}"


# One request-start clock per host for EVERYTHING this finder requests (OAI pages of every
# venue on the host, their redirects, robots.txt): _host_delay is the pace learnt for a host
# (venue review delay, robots Crawl-delay), _clock the start of its last request.
_clock: dict[str, float] = {}
_host_delay: dict[str, float] = {}
POLICY: dict | None = None      # pinned host fetch policy (set by main)


def pace(url: str) -> None:
    """Wait until this host's next request may start, then take the slot."""
    host = host_policy.canonical_host(url)
    last = _clock.get(host)
    if last is not None:
        wait = last + _host_delay.get(host, DELAY) - time.monotonic()
        if wait > 0:
            time.sleep(wait)
    _clock[host] = time.monotonic()


@contextlib.contextmanager
def _robots_pace(url: str):
    """robots_policy pacer: a robots.txt request takes a slot of the same host clock."""
    pace(url)
    yield


def check_access(url: str, policy: dict | None, floor: float = 0.0) -> float:
    """Host policy and robots.txt for one URL BEFORE it is requested; records and returns the
    host's pace. Raises Skipped (suspended host; robots Disallow, AI-training opt-out,
    unavailable robots.txt or long Crawl-delay)."""
    if policy is not None and host_policy.suspended(url, policy):
        raise Skipped(f"{host_policy.canonical_host(url)} is fetch-suspended "
                      "(registry/host_policy.json)")
    try:
        allowed, delay = robots_policy.decision(url)
    except robots_policy.RobotsUnavailable as exc:
        raise Skipped(f"robots.txt unavailable: {exc}") from exc
    if not allowed:
        raise Skipped(f"robots.txt disallows {url}")
    try:
        opt_out = robots_policy.ai_training_refused(url)
    except robots_policy.RobotsUnavailable as exc:
        raise Skipped(f"robots.txt unavailable: {exc}") from exc
    if opt_out:
        raise Skipped(f"robots.txt AI-training opt-out ({opt_out}) for {url}")
    host = host_policy.canonical_host(url)
    delay = max(DELAY, floor, float(delay or 0), _host_delay.get(host, 0.0))
    if delay > MAX_DELAY:
        raise Skipped(f"robots Crawl-delay {delay:g} s exceeds {MAX_DELAY:g} s")
    _host_delay[host] = delay
    return delay


def _get(url: str, params: dict) -> requests.Response:
    """GET with MANUAL redirects: every hop is policy- and robots-checked and paced."""
    hop = requests.Request("GET", url, params=params).prepare().url
    for _ in range(MAX_REDIRECTS + 1):
        check_access(hop, POLICY)
        pace(hop)
        r = requests.get(hop, headers=UA, timeout=(10, 90), allow_redirects=False)
        if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
            hop = urljoin(hop, r.headers["location"])
            continue
        return r
    raise UnexpectedResponse(f"more than {MAX_REDIRECTS} redirects from {url}")


def fetch_page(base: str, token: str, since: str = "") -> str:
    if token:
        params = {"verb": "ListRecords", "resumptionToken": token}
    else:
        params = {"verb": "ListRecords", "metadataPrefix": "oai_dc"}
        if since:
            params["from"] = since
    r = _get(f"{base}/oai", params)
    if r.status_code != 200:
        raise UnexpectedResponse(f"OAI answered HTTP {r.status_code}")
    return r.text


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
        if err.get("code") == "badResumptionToken":
            raise BadToken(f"badResumptionToken: {err.text}")
        raise UnexpectedResponse(f"OAI error {err.get('code')}: {err.text}")
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
            return [(e.text or "").strip() for e in dc.findall(f"dc:{tag}", NS)
                    if (e.text or "").strip()]

        titles = vals("title")
        records.append({
            "oai_id": (header.findtext("oai:identifier", default="", namespaces=NS)).strip(),
            "title": re.sub(r"\s+", " ", titles[0]) if titles else "",
            "rights": vals("rights"), "relations": vals("relation"),
            "formats": vals("format"), "subjects": vals("subject"),
            "creators": vals("creator"), "publishers": vals("publisher"),
            "sources": vals("source"),
            "identifiers": vals("identifier"), "date": (vals("date") or [""])[0],
            "language": (vals("language") or [""])[0],
        })
    tok = root.find(".//oai:resumptionToken", NS)
    return records, ((tok.text or "").strip() if tok is not None else "")


def entry_for(site: dict, rec: dict, today: str) -> dict | None:
    title = rec["title"]
    if not title or NON_ARTICLE.fullmatch(title.strip(" .:-")):
        return None
    if site.get("gate") and not ojs_gate.relevant(" ; ".join([title, *rec.get("subjects", ())])):
        return None
    url = galley_download(site["base"], rec["relations"], rec.get("formats", ()))
    if url is None:
        return None
    tag, lic_url = license_for(
        rec["rights"], rec.get("creators", ()),
        [*rec.get("publishers", ()), *rec.get("sources", ()), site["name"]])
    article, galley = url.rsplit("/", 2)[-2:]
    slug = registry.slug(title)
    if len(slug) < 8:          # a title in a non-Latin script has no ASCII slug
        slug = f"article-{article}-{galley}"
    record_url = (f"{site['base']}/oai?verb=GetRecord&metadataPrefix=oai_dc"
                  f"&identifier={rec['oai_id']}")
    entry = {
        "id": f"ojs-{site['key']}-{slug}"[:63].rstrip("-"),
        "title": title[:150], "url": url, "source": site["source"], "license": tag,
        "topic": site["topic"], "format": "pdf", "document_type": site["type"],
    }
    if lic_url:
        entry["license_url"] = lic_url
        entry["license_evidence"] = record_url
    else:
        stated = "; ".join(rec["rights"])[:160]
        entry["license_evidence"] = (
            f"{record_url} : dc:rights carries no single verifiable Creative Commons licence "
            f"URL ({stated or 'no dc:rights'})")
    entry["rights_verified_at"] = today
    doi = next((d for i in rec["identifiers"]
                if (d := dedup.normalize_pid(i)) and d.startswith("doi:")), "")
    if doi:
        entry["persistent_id"] = f"https://doi.org/{doi[4:]}"
    lang = rec["language"].lower()
    lang = LANG3.get(lang, lang[:2] if re.fullmatch(r"[a-z]{2}(?:[-_][a-z]{2})?", lang) else "")
    if lang:
        entry["language"] = lang
    if re.match(r"\d{4}-\d{2}-\d{2}$", rec["date"]):
        entry["published_at"] = rec["date"]
    return entry


# ------------------------------------------------------------------------------------ access
def site_delay(site: dict, policy: dict | None = None) -> float:
    """Re-check access to a venue (its OAI path and its download path) and return the pace of
    its host; raises Skipped. The pace is recorded for the host clock (pace)."""
    floor = float(site.get("delay") or 0)
    check_access(f"{site['base']}/article/download/1/1", policy, floor)
    return check_access(f"{site['base']}/oai?verb=ListRecords&metadataPrefix=oai_dc", policy,
                        floor)


# ------------------------------------------------------------------------------------ resume
def _resume_file(site: dict):
    return RESUME_DIR / f"{site['key']}.json"


def load_resume(site: dict) -> str:
    try:
        rec = json.loads(_resume_file(site).read_text())
        if time.time() - float(rec["saved_at"]) < RESUME_TTL and isinstance(rec["token"], str):
            return rec["token"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return ""


def save_resume(site: dict, token: str) -> None:
    """Scratch only: losing it costs one re-walk, and completion never depends on it."""
    try:
        if token:
            RESUME_DIR.mkdir(parents=True, exist_ok=True)
            ops.atomic_write_text(_resume_file(site),
                                  json.dumps({"token": token, "saved_at": time.time()}))
        else:
            _resume_file(site).unlink(missing_ok=True)
    except OSError:
        pass


# ------------------------------------------------------------------------------------ harvest
def harvest(site: dict, keys: dedup.Keys, maxn: int, *,
            since: str = "", token: str = "") -> tuple[list[dict], int, str]:
    """One walk of a venue's OAI list from `token` (or its first page; `since` = OAI from date).
    -> (new entries, records scanned, token of the next unread page or '' at the end of the
    list). Finishes the page that crosses maxn. READS the dedup keys but reserves nothing in
    them: a walk that fails leaves no trace, so its retry sees every record again (reserve()
    commits a successful walk)."""
    out: list[dict] = []
    scanned, first, pages = 0, True, 0
    seen_tokens: set[str] = {token} if token else set()
    urls: set[str] = set()
    titles: set[str] = set()
    pids: set[str] = set()
    today = date.today().isoformat()
    while first or token:
        first = False
        records, token = parse_page(fetch_page(site["base"], token, since))
        pages += 1
        if token and (token in seen_tokens or pages >= MAX_PAGES):
            raise UnexpectedResponse(f"repeated resumptionToken {token[:40]!r} (paging loop)")
        seen_tokens.add(token)
        entries = [e for e in (entry_for(site, r, today) for r in records) if e]
        scanned += len(records)
        keys.prefetch(**dedup.page_keys(entries))
        for entry in entries:
            u, t = entry["url"].rstrip("/"), registry.norm(entry["title"])
            own = dedup.entry_pids(entry)
            if (u in urls or t in titles or pids.intersection(own)
                    or u in keys.urls or t in keys.titles or keys.identity_known(entry)):
                continue
            urls.add(u)
            titles.add(t)
            pids.update(own)
            out.append(entry)
        if len(out) >= maxn and token:
            return out, scanned, token
    return out, scanned, ""


def reserve(keys: dedup.Keys, entries: list[dict]) -> None:
    """Commit a successful walk's entries to this run's dedup keys."""
    for entry in entries:
        keys.urls.add(entry["url"].rstrip("/"))
        keys.titles.add(registry.norm(entry["title"]))
        keys.add_identity(entry)


def visit(site: dict, keys: dedup.Keys, maxn: int, policy: dict | None, *,
          since: str = "") -> tuple[list[dict], int, bool]:
    """One visit of a venue -> (new entries, scanned, complete?). Backfill visits (no `since`)
    resume a capped walk from its saved token; only a walk from the first page that reaches the
    end is complete. A rejected token discards that walk entirely (nothing was reserved) and
    walks again from the first page. Raises Skipped / UnexpectedResponse / requests errors."""
    site_delay(site, policy)
    token = "" if since else load_resume(site)
    try:
        out, scanned, rest = harvest(site, keys, maxn, since=since, token=token)
    except BadToken:
        if not token:
            raise
        save_resume(site, "")
        token = ""
        out, scanned, rest = harvest(site, keys, maxn, since=since)
    reserve(keys, out)
    if not since:
        save_resume(site, rest)
    return out, scanned, not rest and not token


# ------------------------------------------------------------------------------------ cursor
def parse_cursor(cursor: str) -> dict:
    m = CURSOR.match(cursor.strip())
    if not m:
        raise ValueError("cursor must be 'f=<YYYY-MM-DD> s=<YYYY-MM-DD> i=<n> a=<n> d=<bits>': "
                         f"{cursor!r}")
    state = {"f": m.group(1), "s": m.group(2), "i": int(m.group(3)), "a": int(m.group(4)),
             "d": list(m.group(5).ljust(len(SITES), "0"))}
    date.fromisoformat(state["f"])
    date.fromisoformat(state["s"])
    if len(state["d"]) > len(SITES):
        raise ValueError(f"cursor names {len(state['d'])} venues; SITES has {len(SITES)}")
    return state


def initial_cursor(today: date) -> str:
    """The cursor of a fresh vein: every venue to backfill, no watch cycle due yet."""
    return f"f={today} s={today} i={len(SITES)} a=0 d="


def format_cursor(state: dict) -> str:
    return (f"f={state['f']} s={state['s']} i={state['i']} a={state['a']} "
            f"d={''.join(state['d'])}")


def plan(state: dict, sites: int, today: date) -> tuple[list[int], list[int]]:
    """(backfill venue indexes, watch venue indexes) for this run; starts a due watch cycle."""
    n = len(SITES)
    pending = [k for k in range(n) if state["d"][k] == "0"]
    start = state["a"] % n if n else 0
    backfill = ([k for k in pending if k >= start] + [k for k in pending if k < start])[:sites]
    if state["i"] >= n and today >= date.fromisoformat(state["s"]) + timedelta(days=WATCH_DAYS):
        state["f"] = (date.fromisoformat(state["s"]) - timedelta(days=1)).isoformat()
        state["s"], state["i"] = today.isoformat(), 0
    watch = [k for k in range(state["i"], n)][:sites] if state["i"] < n else []
    return backfill, watch


def _policy(required: bool) -> dict | None:
    """The pinned host fetch policy. An appending run requires it; a dry run without a readable
    store continues on robots.txt alone (None)."""
    try:
        with dedup.read_view() as view:
            return store.pinned_policy(view)[1]
    except Exception as exc:
        if required:
            raise
        print(f"# host policy not read ({exc}); robots.txt still applies", file=sys.stderr)
        return None


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cursor", default="", help="rotation cursor (see the module docstring)")
    ap.add_argument("--site-key", default="", help="harvest one venue by key (manual run)")
    ap.add_argument("--sites", type=int, default=5,
                    help="venues per run for the backfill, and again for the watch cycle")
    ap.add_argument("--max", type=int, default=50,
                    help="page-granular cap on new entries per venue visit")
    ap.add_argument("--append", action="store_true")
    args = ap.parse_args()
    if args.sites < 1 or args.max < 1:
        ap.error("--sites and --max must be positive")
    global POLICY
    today = date.today()
    report = Report()
    keys = dedup.open_keys()
    policy = POLICY = _policy(args.append)
    robots_policy.set_policy(policy or {})   # robots.txt redirects obey the same suspensions
    robots_policy.set_pacer(_robots_pace)    # robots.txt requests share the host clock
    out: list[dict] = []
    notes: list[str] = []

    def run(k: int, since: str = "") -> bool | None:
        """True complete, False capped, None failed/skipped."""
        site = SITES[k]
        try:
            entries, scanned, complete = visit(site, keys, args.max, policy, since=since)
        except Skipped as exc:
            notes.append(f"{site['key']}: skipped, {exc}")
            return None
        except Exception as exc:
            notes.append(f"{site['key']}: failed, {type(exc).__name__}: {str(exc)[:160]}")
            return None
        out.extend(entries)
        notes.append(f"{site['key']}: {len(entries)} new / {scanned} scanned"
                     f"{' since ' + since if since else ''}"
                     f"{'' if complete else ' -- capped, venue not finished'}")
        return complete

    if args.site_key:
        hits = [k for k, s in enumerate(SITES) if s["key"] == args.site_key]
        if not hits:
            raise SystemExit(f"unknown --site-key {args.site_key}; known: "
                             f"{', '.join(s['key'] for s in SITES)}")
        run(hits[0])
        state = None
    else:
        try:
            state = parse_cursor(args.cursor or initial_cursor(today))
        except ValueError as exc:
            ap.error(str(exc))
        backfill, watch = plan(state, args.sites, today)
        for k in backfill:
            if run(k) is True:
                state["d"][k] = "1"
            state["a"] = k + 1
        for k in watch:
            if state["d"][k] == "1":
                result = run(k, since=state["f"])
                if result is False:
                    break                     # capped: the same venue continues next run
                if result is None:
                    state["d"][k] = "0"       # failed: the backfill walks it again
            state["i"] = k + 1

    keys.uniquify_ids(out)
    by_lic: dict[str, int] = {}
    for e in out:
        by_lic[e["license"]] = by_lic.get(e["license"], 0) + 1
    print(f"# {len(out)} NEW OJS records (PDF galley; licence per record from dc:rights; "
          "deduped vs manifest + registry + blocklist + DOI)")
    for note in notes:
        print(f"#   {note}")
    print(f"# by licence: {by_lic}")
    print("# --- review, then --append, then scripts/build_corpus.py ---")
    print(yaml.safe_dump(out, sort_keys=False, allow_unicode=True))

    if args.append and out:
        counts = registry.append_entries(out)
        print(f"# appended {len(out)} entries to the registry: {counts}", file=sys.stderr)
    if state is not None:
        report.next(format_cursor(state))


if __name__ == "__main__":
    main()
