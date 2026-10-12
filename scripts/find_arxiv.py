#!/usr/bin/env python3
"""find_arxiv.py — built-environment preprints from arXiv through its OAI-PMH interface.

The old arXiv backend paged the search API (find_sources.py --backends arxiv): by page 8 the
ranked results were off-domain, 108 serial queries per round drew HTTP 429, and the search API
carries no licence. This finder reads the interface arXiv provides for harvesting instead:
OAI-PMH at https://oaipmh.arxiv.org/oai, metadataPrefix `arXivRaw` (title, abstract, categories,
versions, DOI and the licence URI of the current version).

Access rules followed (read 2026-10-12):
  * Terms of Use for arXiv APIs (https://info.arxiv.org/help/api/tou.html): "When using the
    legacy APIs (including OAI-PMH, RSS, and the arXiv API), make no more than one request every
    three seconds, and limit requests to a single connection at a time." One request per
    OAI_DELAY seconds, serial. The same terms allow to "Retrieve, store, and use the content of
    arXiv e-prints for your own personal use, or for research purposes" and forbid to "Store
    and serve arXiv e-prints ... from your servers" (the corpus never redistributes bytes).
  * OAI notes (https://info.arxiv.org/help/oa/index.html): "the interface is designed to support
    copying and synchronization of a complete set of arXiv metadata"; a copy is kept current
    "by making incremental harvesting requests with the from date set to the date of last
    harvest". Selection by submission date is not supported, so "newest first" is not possible:
    each set is walked in datestamp order and then watched with `from=`.
  * Full text: arxiv.org/robots.txt for `*` has `Allow: /pdf` and `Crawl-delay: 15`;
    export.arxiv.org/robots.txt is `Disallow: /` (the bulk-data page asks harvesters to use
    export.arxiv.org; its exclusion is not bypassed). The proposed URL is therefore
    https://arxiv.org/pdf/<id>v<N>, and the loader fetches arxiv.org serially, 15 s apart, with the honest tool UA, at most ARXIV cap PDFs per round
    (scripts/build_corpus.py). "Indiscriminate automated downloads from this site are not
    permitted" (https://info.arxiv.org/help/robots.html): only gated records are proposed.

Relevance (arXiv is ~99% off-domain, so precision first): a record is kept only when its TITLE
names an engineering-sense built-environment concept (buildings and their energy systems, HVAC,
construction, structures, building materials, geotechnics, infrastructure, urban planning), or
when the abstract alone names MIN_ABSTRACT_TERMS distinct ones. Concepts that are generic in
control and machine-learning papers (demand response, microgrids, smart cities ...) never count.
A title vetoed by scripts/bes_relevance.py or by the arXiv veto (wireless indoor positioning,
molecular "building blocks", ...) never passes.

Licence, per record, from the `<license>` URI of the current version through the audited
classifier (scripts/audit_licence_evidence.classify_licence_url): a canonical open Creative
Commons grant keeps its tag, NC/ND keep theirs, arXiv's non-exclusive distribution licence (and
a record with no licence element) is `arxiv-nonexclusive`, anything else `unverified`. Nothing
is skipped for its licence; the class picks the view. The record carries the licence of its
CURRENT version only, and arXiv lets versions differ in licence, so the proposed URL pins that
version (`/pdf/<id>v<N>`): a replacement that appears between discovery and the fetch, or
before a later restoration, cannot put other bytes under the recorded rights.

Dedup: the arXiv DOI (10.48550/arXiv.<id>) and the published DOI are persistent identifiers, so a
paper held through OpenAlex is not fetched again; the unversioned URL, every versioned URL
and the title are checked too, so a paper held in any version is not proposed again.

Rotation (dynamic, `--cursor "s=<set index> t=<resumptionToken|START> f=<from>,... w=<date|->"`):
`f` holds one harvested-through datestamp per set (`-`: from the beginning), `w` the day the
last full cycle ended. A run reads at most --pages OAI pages. A rejected token restarts its set
from the stored datestamp; an unexpected answer holds the cursor. After the last set (all of
arXiv) the cursor waits WATCH_DAYS, then harvests the changes since the stored datestamp. The
backend never reports EXHAUSTED.

    python scripts/find_arxiv.py --pages 1 --max 40
"""
from __future__ import annotations

import argparse
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

import requests
import yaml

import audit_licence_evidence
import bes_relevance
import dedup
import registry
from finder_protocol import Report

OAI = "https://oaipmh.arxiv.org/oai"
UA = {"User-Agent": "nekaise-corpus/find_arxiv (research corpus; robots.txt honoured)"}
OAI_DELAY = 3.1      # arXiv API terms: at most one request every three seconds, one connection
WATCH_DAYS = 2       # a finished cycle is followed by an incremental one this many days later
MIN_ABSTRACT_TERMS = 3
SOURCE = "arxiv"
PREFIX = "arx-"
START = "START"
# Append-only: the cursor indexes this tuple. Dedicated sets first (quick, dense yield); the
# last entry (None = no set) is all of arXiv and the only one the watch cycles revisit.
SETS = ("eess:eess:SY", "physics:physics:soc-ph", "cs:cs:CE", "physics:physics:flu-dyn",
        "physics:physics:app-ph", None)
ALL = len(SETS) - 1
NS = {"o": "http://www.openarchives.org/OAI/2.0/", "a": "http://arxiv.org/OAI/arXivRaw/"}
ARXIV_ID = re.compile(r"^(?:[a-z][a-z\-]*(?:\.[A-Z]{2})?/\d{7}|\d{4}\.\d{4,5})$")
CURSOR = re.compile(r"^s=(\d+) t=(\S+) f=(\S+) w=(\d{4}-\d{2}-\d{2}|-)$")
DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
VERSION = re.compile(r"^v([1-9]\d{0,3})$")

# Engineering-sense built-environment concepts. Words that arXiv uses figuratively or in other
# fields ("building a model", "building blocks", "indoor localization", "bridge the gap",
# "network architecture", "concrete example", "smart grid") count only in their AEC form.
AEC = re.compile(
    r"\bbuildings\b|\b(?:residential|commercial|office|public|historic|existing|smart|green|"
    r"tall|high[- ]rise|low[- ]rise|multi[- ]stor(?:e)?y|timber|masonry|steel|"
    r"reinforced[- ]concrete|school|university|campus|zero[- ]energy|energy[- ]efficient|"
    r"grid[- ]interactive) buildings?\b|"
    r"\bbuilding (?:energy|stock|sector|envelopes?|materials?|retrofit|renovation|automation|"
    r"management systems?|information model(?:l?ing)?|performance|physics|simulation|"
    r"controls?|operations?|thermal|heating|cooling|climate|occupan\w+|codes?|design|"
    r"fa[cç]ades?|footprints?|heights?|clusters?|flexibility|loads?|electrification|"
    r"decarboni[sz]ation|meters?|aggregat\w+|portfolios?|"
    r"structures?|damage|collapse|evacuation|fires?|ventilation)\b|"
    r"\bbuilt environment\b|\bhvac\b|\bheat pumps?\b|\bair[- ]condition\w*|\bchillers?\b|"
    r"\bdistrict (?:heating|cooling|energy)\b|\bthermal comfort\b|\bindoor (?:air|"
    r"environment\w*|climate|thermal|temperature|comfort|ventilation|airflow)\b|"
    r"\bthermostat\w*|\bspace (?:heating|cooling)\b|\bdomestic hot water\b|"
    r"\b(?:natural|mechanical|displacement) ventilation\b|\bdaylight\w*|\bglazing\b|"
    r"\bfa[cç]ades?\b|\bthermal insulation\b|\bbuilding[- ]integrated\b|\benergy ?plus\b|"
    r"\bmodelica\b|\bopenstudio\b|\btrnsys\b|\bboptest\b|\bcitylearn\b|\bsinergym\b|"
    r"\bcivil (?:engineering|infrastructures?|structures?)\b|\bstructural (?:engineering|"
    r"health monitoring|optimi[sz]ation of (?:buildings|trusses|frames)|damage|reliability)\b|"
    r"\b(?:radiator|hydronic|underfloor|residential|household) heating\b|"
    r"\bresidential (?:cooling|energy|loads?|electricity|sector)\b|"
    r"\b(?:of|for|in|on) (?:\w+ )?bridges\b|"
    r"\b(?:reinforced|prestressed|precast|recycled|fib(?:er|re)[- ]reinforced|high[- ]"
    r"performance|self[- ]compacting|3d[- ]printed|asphalt) concrete\b|"
    r"\bconcrete (?:structures?|beams?|slabs?|columns?|walls?|bridges?|dams?|mix\w*|"
    r"strength|cracking|durability|pavements?|printing|members?|frames?)\b|\bcement\w*|"
    r"\bmasonry\b|\bcross[- ]laminated timber\b|\btimber (?:structures?|buildings?|"
    r"construction|beams?|joints?|connections?)\b|\bsteel (?:structures?|frames?|bridges?|"
    r"beams?|connections?|trusses)\b|\brebars?\b|"
    r"\b(?:highway|railway|road|suspension|cable[- ]stayed|girder|arch|pedestrian|steel|"
    r"concrete|truss|long[- ]span) bridges?\b|\bbridge (?:decks?|piers?|girders?|"
    r"inspection|monitoring|maintenance|engineering|structures?|dynamics|scour)\b|"
    r"\btunnel(?:l?ing)? (?:construction|boring|excavation|linings?|engineering)\b|"
    r"\b(?:road|railway|metro|highway) tunnels?\b|\bpavements?\b|\basphalt\b|\bgeotechn\w*|"
    r"\bsoil[- ]structure interaction\b|\bretaining walls?\b|\bpile foundations?\b|"
    r"\bslope stability\b|\bseismic (?:design|retrofit|performance|response of|isolation|"
    r"vulnerability|fragility|risk|assessment of)\b|\bearthquake engineering\b|"
    r"\bconstruction (?:industry|sites?|projects?|management|safety|workers?|materials?|"
    r"robots?|robotics|schedul\w+|equipment|waste|engineering|progress|automation)\b|"
    r"\bbim\b|\bprefabricat\w*|\bmodular construction\b|\b3d concrete printing\b|"
    r"\burban (?:planning|design|morpholog\w+|heat islands?|microclimate|building|"
    r"energy|form|drainage|water (?:supply|systems?|networks?)|infrastructures?)\b|"
    r"\bcity planning\b|\bstreet canyons?\b|\bsmart cit(?:y|ies)\b|\bland[- ]use planning\b|"
    r"\bwastewater\b|\bsewer\w*|\bwater distribution (?:systems?|networks?)\b|"
    r"\bdrinking water\b|\bstormwater\b|\bfire safety\b|\bfire dynamics simulator\b|\bcompartment fires?\b|\bsmoke control\b|"
    r"\bwind loads?\b|\bwind engineering\b|\barchitectural (?:design|engineering|acoustics)\b|"
    r"\bfloor plans?\b|\broom acoustics\b|\bbuilding acoustics\b|\bhousing\b|\bdwellings?\b|"
    r"\bcool roofs?\b|\bgreen roofs?\b|\bradiant (?:cooling|heating|floor)\b|"
    r"\bthermal energy storage\b|\bphase[- ]change materials?\b|\bfacilit(?:y|ies) management\b|"
    r"\boccupancy (?:detection|estimation|prediction|sensing|schedules?)\b|"
    r"\bdemand response\b|\bhome energy management\b|\bsmart homes?\b|\bmicrogrids?\b",
    re.I,
)
# Concepts that match AEC but are generic in arXiv control / ML papers: they never count.
WEAK = re.compile(r"^(?:demand response|microgrids?|smart cit(?:y|ies)|smart homes?|housing|"
                  r"cement\w*|thermal energy storage|phase[- ]change materials?|modelica|"
                  r"wastewater|drinking water|daylight\w*|thermostat\w*)$", re.I)
VETO = re.compile(
    # in power-systems papers HVAC is high-voltage AC
    r"\bhvdc\b|\bhvac (?:power|transmission|grids?|lines?|networks?|cables?|links?)\b|"
    # in group theory and geometry a "building" is a Tits building
    r"\b(?:euclidean|spherical|affine|hyperbolic|twin|tits|bruhat[- ]tits|kac[- ]moody|"
    r"right[- ]angled|coxeter|moufang|fuchsian|exotic|locally finite|[a-z]-adic) buildings?\b|"
    r"\bbuildings? (?:and|of|for|with) (?:their |its )?(?:automorphism|representation|"
    r"apartments?\b|chambers?\b|type\b)|\bautomorphism groups?\b|"
    r"\bcoxeter (?:groups?|complex)|\brepresentation theory\b|\bbuildings? of type\b|"
    r"\bbuilding blocks?\b|\bindoor (?:localis|localiz|position|navigation|scene|wireless|"
    r"robot|slam)\w*|\bwi-?fi\b|\b(?:5g|6g|mimo|lte)\b|\bquantum\b|\bprotein|\bgenom\w*|"
    r"\bmolecul\w*|\bneutrino|\bgalax\w*|\bcosmolog\w*|\bstellar\b|\bhadron|\bplasma\b|"
    r"\bspacecraft\b|\bsatellite constellation|\blattice qcd\b|\btopological insulator|"
    r"\bgraphene\b|\bsuperconduct\w*|\bknowledge graph construction\b|\bdataset construction\b|"
    r"\bcode generation\b|\bsoftware architecture\b|\bnetwork architecture\b|"
    r"\bneural architecture search\b|\bbridges? (?:the|a) gap\b|\bbridging the gap\b",
    re.I,
)
TOPIC_RULES = (
    (re.compile(r"\bseismic|\bearthquake|\bstructural|\bbridges?\b|\btunnel|\bgeotechn|"
                r"\bfoundations?\b|\bsoil[- ]structure|\bwind loads?", re.I), "structures_civil"),
    (re.compile(r"\bconcrete\b|\bcement|\btimber\b|\bmasonry\b|\basphalt\b|\brebars?\b|"
                r"\bphase[- ]change", re.I), "materials"),
    (re.compile(r"\bconstruction\b|\bbim\b|\bprefabricat", re.I), "construction"),
    (re.compile(r"\bpavements?\b|\bwastewater|\bsewer|\bwater distribution|\bstormwater|"
                r"\bdrinking water", re.I), "infrastructure"),
)


class Unexpected(RuntimeError):
    """Not an OAI-PMH ListRecords answer (rate limit, maintenance page, error)."""


def aec_terms(text: str) -> set[str]:
    return {re.sub(r"[\s-]+", " ", m.group(0).lower()) for m in AEC.finditer(text or "")}


def relevant(title: str, abstract: str) -> bool:
    """A strong title concept, or a dense abstract; vetoes always win."""
    title = title or ""
    if not title.strip() or VETO.search(title) or bes_relevance.vetoed(title):
        return False
    if any(not WEAK.match(t) for t in aec_terms(title)):
        return True
    strong_abstract = {t for t in aec_terms(abstract) if not WEAK.match(t)}
    return len(strong_abstract) >= MIN_ABSTRACT_TERMS and not VETO.search(abstract or "")


def topic_for(title: str) -> str:
    for pattern, topic in TOPIC_RULES:
        if pattern.search(title or ""):
            return topic
    return bes_relevance.topic_for(title)


def parse_cursor(cursor: str) -> tuple[int, str | None, list[str | None], date | None]:
    """(set index, token or None, per-set from datestamps, day the last cycle ended)."""
    match = CURSOR.match(cursor.strip())
    if not match:
        raise ValueError(f"cursor must be 's=<i> t=<token|START> f=<d,...> w=<date|->': "
                         f"{cursor!r}")
    s = int(match.group(1))
    froms: list[str | None] = [None if v == "-" else v for v in match.group(3).split(",")]
    if s >= len(SETS) or len(froms) > len(SETS):
        raise ValueError(f"cursor names an unknown set: {cursor!r}")
    for value in froms:
        if value is not None:
            # only YYYY-MM-DD (fromisoformat alone also reads 20260101 and 2026-W01-1), and a
            # day that exists
            if not DATE.fullmatch(value):
                raise ValueError(f"cursor datestamp is not YYYY-MM-DD: {value!r}")
            date.fromisoformat(value)
    froms += [None] * (len(SETS) - len(froms))      # sets appended since the cursor was written
    token = None if match.group(2) == START else match.group(2)
    watched = None if match.group(4) == "-" else date.fromisoformat(match.group(4))
    return s, token, froms, watched


def format_cursor(s: int, token: str | None, froms: list[str | None], watched: date | None) -> str:
    return (f"s={s} t={token or START} f={','.join(v or '-' for v in froms)} "
            f"w={watched.isoformat() if watched else '-'}")


def fetch_page(setspec: str | None, since: str | None, token: str | None) -> bytes:
    if token:
        params = {"verb": "ListRecords", "resumptionToken": token}
    else:
        params = {"verb": "ListRecords", "metadataPrefix": "arXivRaw"}
        if setspec:
            params["set"] = setspec
        if since:
            params["from"] = since
    response = requests.get(OAI, params=params, headers=UA, timeout=180)
    if response.status_code != 200:
        raise Unexpected(f"arXiv OAI answered HTTP {response.status_code}")
    return response.content


def parse_page(body: bytes, *, first: bool) -> tuple[list[dict], str | None, str | None]:
    """(records, next token, last datestamp). An initial noRecordsMatch is an empty finished
    list; badResumptionToken raises LookupError; anything else that is not a ListRecords answer
    raises Unexpected."""
    try:
        root = ET.fromstring(body)
    except ET.ParseError as exc:
        raise Unexpected(f"arXiv OAI answer is not XML: {exc}") from exc
    if root.tag != f"{{{NS['o']}}}OAI-PMH":
        raise Unexpected(f"arXiv OAI answer has root element {root.tag!r}")
    error = root.find("o:error", NS)
    if error is not None:
        code = error.get("code")
        if code == "noRecordsMatch" and first:
            return [], None, None
        if code == "badResumptionToken":
            raise LookupError(f"badResumptionToken: {error.text}")
        raise Unexpected(f"arXiv OAI error {code}: {error.text}")
    listing = root.find("o:ListRecords", NS)
    if listing is None:
        raise Unexpected("arXiv OAI answer has neither ListRecords nor error")
    records, last = [], None
    found = listing.findall("o:record", NS)
    if not found:
        raise Unexpected("ListRecords answer with no records; completion is unproven")
    for record in found:
        header = record.find("o:header", NS)
        stamp = (header.findtext("o:datestamp", default="", namespaces=NS) or "").strip() \
            if header is not None else ""
        try:        # a real calendar day: this value becomes part of the persisted cursor
            if not DATE.match(stamp):
                raise ValueError(stamp)
            date.fromisoformat(stamp)
        except ValueError as exc:
            raise Unexpected(f"an OAI record carries no valid datestamp ({stamp[:20]!r})") from exc
        last = max(last or stamp, stamp)
        if header.get("status") == "deleted":
            continue
        identifier = (header.findtext("o:identifier", default="", namespaces=NS) or "").strip()
        raw = record.find("o:metadata/a:arXivRaw", NS)
        if raw is None:
            raise Unexpected("a non-deleted OAI record carries no arXivRaw metadata")
        text = lambda tag: " ".join((raw.findtext(f"a:{tag}", default="", namespaces=NS)
                                     or "").split())
        # Identity, title and EVERY version are required: the proposed URL names the latest
        # version and carries the record's licence, so a dropped or unreadable version would
        # bind the rights to other bytes, and a dropped record would be skipped for good.
        aid = text("id")
        if not ARXIV_ID.match(aid) or identifier != f"oai:arXiv.org:{aid}":
            raise Unexpected(f"an OAI record has no valid arXiv id ({identifier[:40]!r} / "
                             f"{aid[:30]!r})")
        if not text("title"):
            raise Unexpected(f"arXiv record {aid} has no title")
        versions = []
        for version in raw.iterfind("a:version", NS):
            match = VERSION.match(version.get("version") or "")
            if not match:
                raise Unexpected(f"arXiv record {aid} has a malformed version "
                                 f"({version.get('version')!r})")
            versions.append((int(match.group(1)),
                             version.findtext("a:date", default="", namespaces=NS)))
        numbers = [number for number, _ in versions]
        if not numbers or len(set(numbers)) != len(numbers):
            raise Unexpected(f"arXiv record {aid} has no versions or repeats one ({numbers})")
        records.append({"id": text("id"), "title": text("title"), "abstract": text("abstract"),
                        "categories": text("categories"), "license": text("license"),
                        "doi": text("doi"), "versions": sorted(versions)})
    token_el = listing.find("o:resumptionToken", NS)
    token = (token_el.text or "").strip() if token_el is not None else ""
    return records, token or None, last


def candidate(record: dict, today: str) -> dict | None:
    aid, title = record.get("id") or "", record.get("title") or ""
    if not ARXIV_ID.match(aid) or not record.get("versions"):
        return None
    if not relevant(title, record.get("abstract") or ""):
        return None
    licence = record.get("license") or ""
    _verdict, tag, reason = audit_licence_evidence.classify_licence_url(licence or None)
    latest = record["versions"][-1][0]
    entry = {
        "id": f"{PREFIX}{registry.slug(title)[:46]}", "title": title[:150],
        "url": f"https://arxiv.org/pdf/{aid}v{latest}", "source": SOURCE, "license": tag,
        "topic": topic_for(title), "format": "pdf", "language": "en",
        "document_type": "preprint",
        "persistent_id": f"doi:10.48550/arxiv.{aid.lower()}",
        "license_url": licence if licence.startswith(("http://", "https://")) else
        f"{OAI}?verb=GetRecord&identifier=oai:arXiv.org:{aid}&metadataPrefix=arXivRaw",
        "license_evidence": (f"arXiv OAI-PMH arXivRaw oai:arXiv.org:{aid} (v{latest}, the "
                             f"current version and the copy this URL names): "
                             f"{licence or 'no licence element'} -> {tag} ({reason})"),
        "rights_verified_at": today,
    }
    try:
        entry["published_at"] = parsedate_to_datetime(record["versions"][0][1]).date().isoformat()
    except (TypeError, ValueError, IndexError):
        pass
    if published := dedup.normalize_pid(record.get("doi") or ""):
        if published != entry["persistent_id"]:
            entry["origin_ids"] = published
    return entry


def url_forms(entry: dict, record: dict) -> list[str]:
    """Every URL spelling under which the registry may already hold this paper: the
    unversioned forms and every version's (the proposed URL is one of them)."""
    base = f"https://arxiv.org/pdf/{record['id']}"
    forms = [base, base + ".pdf", base.replace("/pdf/", "/abs/")]
    for number, _ in record["versions"]:
        forms += [f"{base}v{number}", f"{base}v{number}.pdf"]
    return forms


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cursor", default=format_cursor(0, None, [None] * len(SETS), None),
                    help="'s=<set> t=<token|START> f=<from,...> w=<date|->' (rotation pointer)")
    ap.add_argument("--pages", type=int, default=2, help="max OAI pages this run")
    ap.add_argument("--max", type=int, default=40,
                    help="page-granular target for new entries (the last page may exceed it)")
    ap.add_argument("--append", action="store_true", help="append into registry/papers.yaml")
    args = ap.parse_args()
    if args.pages < 1 or args.max < 1:
        ap.error("--pages and --max must be positive")
    try:
        s, token, froms, watched = parse_cursor(args.cursor)
    except ValueError as exc:
        ap.error(str(exc))
    report = Report()
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    if (watched is not None and s == ALL and token is None
            and now.date() < watched + timedelta(days=WATCH_DAYS)):
        print(f"# 0 NEW arXiv records (cycle ended {watched}; next look "
              f"{watched + timedelta(days=WATCH_DAYS)})")
        report.next(format_cursor(s, token, froms, watched))
        return

    keys = dedup.open_keys()
    out: list[dict] = []
    scanned = pages = 0
    restarted = False
    while pages < args.pages and len(out) < args.max:
        if pages:
            time.sleep(OAI_DELAY)
        try:
            records, next_token, last = parse_page(fetch_page(SETS[s], froms[s], token),
                                                   first=token is None)
        except LookupError as exc:
            if restarted or token is None:
                if not pages:
                    report.hold(f"{exc} at set {s}; nothing proposed")
                    print("# 0 NEW arXiv records (rejected token)")
                    return
                break
            print(f"# {exc}; restarting set {s} from {froms[s] or 'the beginning'}",
                  file=sys.stderr)
            restarted, token = True, None
            pages += 1
            continue
        except Unexpected as exc:
            if not pages:
                report.hold(f"{exc} at set {s}; nothing proposed, retrying next round")
                print("# 0 NEW arXiv records (unexpected OAI answer)")
                return
            print(f"# {exc}; stopping after {pages} complete pages", file=sys.stderr)
            break
        except requests.RequestException as exc:
            if not pages:
                print(f"# ERROR: arXiv OAI request failed at set {s}: {exc}; refusing a partial "
                      "append so rotation does not advance", file=sys.stderr)
                raise SystemExit(1)
            print(f"# {exc}; stopping after {pages} complete pages", file=sys.stderr)
            break
        pages += 1
        scanned += len(records)
        pairs = [(e, r) for r in records if (e := candidate(r, today))]
        keys.prefetch(**dedup.page_keys([e for e, _ in pairs]))
        keys.prefetch(urls=[u for e, r in pairs for u in url_forms(e, r)])
        for entry, record in pairs:
            t = registry.norm(entry["title"])
            if (any(u in keys.urls for u in url_forms(entry, record)) or t in keys.titles
                    or keys.identity_known(entry)):
                continue
            keys.urls.update(url_forms(entry, record))
            keys.titles.add(t)
            keys.add_identity(entry)
            out.append(entry)
        if last:
            froms[s] = max(froms[s] or last, last)
        if next_token:
            token = next_token
        elif s < ALL:
            s, token = s + 1, None
        else:
            s, token, watched = ALL, None, now.date()
            break

    keys.uniquify_ids(out)
    by_licence: dict[str, int] = {}
    for entry in out:
        by_licence[entry["license"]] = by_licence.get(entry["license"], 0) + 1
    print(f"# {len(out)} NEW arXiv built-environment records (kept {len(out)}/{scanned} over "
          f"{pages} OAI pages; deduped vs manifest + registry + blocklist + DOI)")
    print(f"# by licence: {by_licence}")
    print("# --- review, then --append, then scripts/build_corpus.py ---")
    print(yaml.safe_dump(out, sort_keys=False, allow_unicode=True))
    if args.append and out:
        counts = registry.append_entries(out)
        print(f"# appended {len(out)} entries to the registry: {counts}", file=sys.stderr)
    cursor = format_cursor(s, token, froms, watched)
    parse_cursor(cursor)    # a cursor this backend emits must be one it accepts
    report.next(cursor)


if __name__ == "__main__":
    main()
