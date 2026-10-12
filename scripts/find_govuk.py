#!/usr/bin/env python3
"""find_govuk.py — UK government built-environment publications via the GOV.UK Search/Content APIs.

Building regulations and approved documents, building safety, SAP/RdSAP and EPC material, heat
networks and heat pumps, retrofit research, housing surveys, planning guidance, flood and
coastal engineering research, highways and rail infrastructure reports.

Enumeration (re-aimed 2026-10-12). The first design paged 32 free-text queries; at depth the
Search API ranks fuzzy full-text matches and page 905 was mostly off-topic. This design does not
send a text query. It walks ORGANISATIONS (the departments and agencies that own the built
environment: housing, energy, building safety, environment, transport, infrastructure), one
organisation at a time, filtered to publication document types (DOC_TYPES), newest first
(`order=-public_timestamp`), `start` offset paging with an OVERLAP of results between pages.
Each page whose title + description pass the relevance gate is opened through the Content API
(`/api/content/<path>`), which lists its attachments. Registered: `application/pdf` attachments
(direct `assets.publishing.service.gov.uk` URLs), HTML attachments (the publication's own
www.gov.uk pages, which carry most guidance since about 2020) and, for a page without any
attachment, the page itself when it has a real body (detailed guides). Spreadsheets and other
office files are skipped (statistics pages often yield nothing). GOV.UK robots.txt for `*`
disallows only `/*/print$` and `/search/all*`; the `/api/` endpoints and the asset host are not
disallowed.

Coverage exclusions (deliberate, reported on every run that meets one):
  * at most ATTACHMENTS_PER_PAGE documents per page, in page order. An HS2 map book or a
    consultation carries hundreds of PDFs (plans, map sheets); document 13 and later of such a
    page are NOT collected and no later run returns to them. The run prints the page path and
    the counts ("COVERAGE EXCLUSION (attachment cap)").
  * the Search API serves about 10,000 results per filter set; an organisation with more is
    walked to that window only ("COVERAGE EXCLUSION (search window)"; none on 2026-10-12, the
    largest has 4,840).

Licence: `unverified` for every new row. GOV.UK states on every page "All content is available
under the Open Government Licence v3.0, except where otherwise stated", and its terms
(https://www.gov.uk/help/terms-conditions) name exempt content (departmental logos, third-party
material, content marked otherwise). The APIs expose no per-document exception and this finder
reads no document, so the OGL grant is not established for an individual attachment: the row
keeps the statement as evidence text and is collected into the `unverified` view. Rows
registered before 2026-10-12 keep their recorded tag.

Rotation (dynamic, `--cursor`), three states; every run reads at most --pages Search pages and
stops between complete pages once --max new entries are found:
  * walk   "o=<index into ORGS (append-only)> s=<start offset> w=<day the walk started>"
           (`w` is added on the first run). After the last organisation: "watch:<w> w=<w>" —
           coverage is complete up to the day the walk STARTED, because a page published or
           updated while it ran sorts ahead of the saved offset.
  * watch  "watch:<boundary day> w=<walk day>": idle. WATCH_DAYS after the boundary a catch-up
           starts; REWALK_DAYS after the walk day the full walk starts again instead.
  * catch  "catch:<boundary day> n=<day this look started> o=<index> s=<start> w=<walk day>":
           every organisation's pages published or updated since the day before the boundary
           (`filter_public_timestamp=from:<date>`), to the last page. The position is
           persisted, so a backlog larger than one run continues in the next run. After the
           last organisation: "watch:<n> w=<walk day>".
Listing shifts. Offset paging over a newest-first listing is exact only while the listing does
not change. Additions and updates move pages to the front (re-read, never skipped, and read
again by the next catch-up). Withdrawals move later pages up: OVERLAP covers a few, and a
removal of more than OVERLAP results before the saved offset skips pages whose old timestamps
keep them out of every catch-up. The periodic full re-walk (REWALK_DAYS) reconciles those;
deduplication keeps only the new documents.
An unexpected Search or Content answer (wrong structure, an error object, an empty or short
page while results remain, an organisation without any publication) holds the cursor and
proposes nothing, also when earlier pages of the same run were good. Out-of-range cursor values
and future dates are refused. The backend never reports EXHAUSTED.

    python scripts/find_govuk.py --cursor "o=0 s=0" --pages 2 --max 40
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

SEARCH = "https://www.gov.uk/api/search.json"
CONTENT = "https://www.gov.uk/api/content{path}"
UA = {"User-Agent": "nekaise-corpus/find_govuk (research corpus; robots.txt honoured)"}
TIMEOUT = 30
ROWS = 50                  # search results per request
OVERLAP = 5                # results re-read between consecutive pages of one organisation
SEARCH_DELAY = 0.6         # seconds between Search API requests
CONTENT_DELAY = 0.4        # seconds between Content API requests
ATTACHMENTS_PER_PAGE = 12  # a coverage exclusion, see the module docstring
WATCH_DAYS = 2             # a catch-up look starts this long after the coverage boundary
REWALK_DAYS = 90           # the full walk runs again this long after the last one started
MAX_START = 9950           # the Search API returns nothing useful past ~10,000 results
ASSET = re.compile(r"^https://assets\.publishing\.service\.gov\.uk/[^\s]+$")
HTML_PATH = re.compile(r"^/(?:government|guidance)/[A-Za-z0-9/_.-]+$")  # a www.gov.uk page path
MIN_BODY = 4000            # characters of body HTML for a page without attachments
LICENCE_EVIDENCE = (
    "GOV.UK publication page: 'All content is available under the Open Government Licence "
    "v3.0, except where otherwise stated'. GOV.UK terms (https://www.gov.uk/help/"
    "terms-conditions) name exempt content; no document-specific check was made, so the grant "
    "is not established for this document")

# Append-only: the cursor indexes this tuple. Organisation slugs of the GOV.UK Search API.
ORGS = (
    "ministry-of-housing-communities-local-government",
    "department-for-levelling-up-housing-and-communities",
    "building-safety-regulator",
    "department-for-energy-security-and-net-zero",
    "department-for-business-energy-and-industrial-strategy",
    "department-of-energy-climate-change",
    "homes-england",
    "planning-inspectorate",
    "health-and-safety-executive",
    "office-for-product-safety-and-standards",
    "environment-agency",
    "department-for-transport",
    "national-highways",
    "highways-england",
    "highways-agency",
    "high-speed-two-limited",
    "office-of-rail-and-road",
    "infrastructure-and-projects-authority",
    "national-infrastructure-and-service-transformation-authority",
    "national-infrastructure-commission",
    "defence-infrastructure-organisation",
    "mining-remediation-authority",
    "government-property-agency",
    "department-for-education",
)
# GOV.UK content_store_document_type values that are publications with attachments.
DOC_TYPES = ("guidance", "detailed_guide", "statutory_guidance", "research", "independent_report",
             "policy_paper", "official_statistics", "national_statistics", "impact_assessment",
             "consultation_outcome", "standard", "regulation",
             "flood_and_coastal_erosion_risk_management_research_report")

# An organisation walk meets every publication of a department (oil and gas licensing, driving
# tests, school attendance ...). Require an explicit built-environment anchor in the page title
# or description, or in the attachment title, before spending a fetch slot. English only: GOV.UK
# is an English-language source; the corpus-wide quality gate remains multilingual.
TITLE_RELEVANCE = re.compile(
    r"\b(?:"
    r"buildings?|built environment|construction|architectur(?:e|al)|"
    r"housing|house ?building|dwellings?|leasehold(?:ers?)?|"
    r"(?:new|affordable|social|council|energy[- ]efficient|warm|park|empty|decent) homes|"
    r"energy (?:efficien|performance|consumption|saving)|decarbonis|retrofit|insulat|"
    r"energy company obligation|display energy certificates?|digest of uk energy statistics|"
    r"heat pumps?|heat networks?|district heat|heating|cooling|ventilat|overheating|"
    r"boilers?|fuel poverty|smart meters?|"
    r"building regulations?|approved documents?|standard assessment procedure|"
    r"future homes standard|decent homes standard|cladding|"
    r"development sites?|sites? for development|site (?:assessment|allocation)|"
    r"land quality|land use|brownfield|town planning|local plans?|"
    r"planning (?:policy|practice|permission|appeals?|system|reform|guidance|obligations?|"
    r"applications?|inspectorate)|permitted development|nationally significant infrastructure|"
    r"railways?|rail (?:infrastructure|network|upgrade)|high speed (?:two|2|rail)|hs2|"
    r"highways?|road (?:construction|maintenance|network|scheme|design|investment|surfac)|"
    r"roads|potholes?|bridges?|tunnels?|tunnell?ing|pavements?|tactile paving|street works|"
    r"design manual for roads|cycle infrastructure|"
    r"canals?|waterways?|water (?:supply|resources?|pollution|networks?|infrastructure|code)|"
    r"sewerage|sewers?|wastewater|drainage|flood|river restoration|coastal|reservoirs?|"
    r"fire (?:safety|doors?|risk assessments?|resistance|performance|test(?:s|ing)?|spread|"
    r"protection|engineering|statistics)|grenfell|asbestos|legionella|scaffold|demolition|"
    r"concrete|cement|masonry|structural|geotechnical|asphalt|construction materials?|"
    r"construction and demolition waste|contaminated land|"
    r"infrastructure|school (?:rebuilding|estate)|government estate|"
    r"urban environment|urban planning|public realm"
    r")\b|\b(?:SAP|RdSAP|EPCs?|ESOS|RAAC|BIM)\b",
    re.I,
)
# Page classes that pass the anchor gate but carry no technical content: single planning
# casework files, inter-agency memoranda, byelaws, funding administration.
TITLE_NOISE = re.compile(
    r"\bsection 62a planning application|\bmemorandum of understanding|\bbyelaws?\b|"
    r"\bkey information document|\bhomes for ukraine|\bregister of interests|"
    r"\bministerial measures\b|\bspending over|\bprocurement card|\bgifts and hospitality",
    re.I)
# Attachments that are not the publication itself.
ATTACHMENT_SKIP = re.compile(
    r"\bprivacy notice|\bequalit(?:y|ies) (?:impact|analysis|statement)|\bapplication form|"
    r"\bcover(?:ing)? letter|\bletter (?:to|from)\b|\bpress notice|\beasy read\b|"
    r"\bresponse form|\bconsultation questionnaire|\bposter\b|\bleaflet\b", re.I)
# (title regex -> topic), first match wins.
TOPIC_RULES = (
    (re.compile(r"building regulations?|approved document|standard assessment procedure|"
                r"\bSAP\b|RdSAP|future homes|standard\b|code of practice", re.I),
     "standards_protocols"),
    (re.compile(r"heat pump|boiler|heat network|district heat|ventilat|cooling|heating", re.I),
     "equipment_systems"),
    (re.compile(r"smart meter", re.I), "controls_bas"),
    (re.compile(r"energy|fuel poverty|decarbonis|retrofit|insulat|EPC|overheating", re.I),
     "building_energy"),
    (re.compile(r"concrete|cement|masonry|asphalt|materials?|RAAC|asbestos", re.I), "materials"),
    (re.compile(r"structural|bridge|tunnel|geotechnical|reservoir", re.I), "structures_civil"),
    (re.compile(r"highway|road|rail|hs2|high speed|flood|coastal|water|sewer|drainage|"
                r"infrastructure|canal|waterway", re.I), "infrastructure"),
    (re.compile(r"planning|local plan|land use|brownfield|urban|public realm|development",
                re.I), "urban"),
    (re.compile(r"construction|building safety|cladding|fire|demolition|scaffold", re.I),
     "construction"),
    (re.compile(r"housing|homes|dwelling|leasehold|architect|estate", re.I), "architecture"),
)
CURSOR = re.compile(r"^o=(\d+) s=(\d+)(?: w=(\d{4}-\d{2}-\d{2}))?$")
WATCH = re.compile(r"^watch:(\d{4}-\d{2}-\d{2})(?: w=(\d{4}-\d{2}-\d{2}))?$")
CATCH = re.compile(r"^catch:(\d{4}-\d{2}-\d{2}) n=(\d{4}-\d{2}-\d{2}) o=(\d+) s=(\d+) "
                   r"w=(\d{4}-\d{2}-\d{2})$")


class Unexpected(RuntimeError):
    """An answer that is not the Search API's JSON result (maintenance page, challenge, error)."""


def title_relevant(title: str) -> bool:
    """Whether a GOV.UK publication title contains high-precision AEC evidence."""
    return bool(TITLE_RELEVANCE.search(title or "")) and not TITLE_NOISE.search(title or "")


def topic_for(title: str) -> str:
    for pattern, topic in TOPIC_RULES:
        if pattern.search(title or ""):
            return topic
    return "building_energy"


def search(org: str, start: int, since: str | None = None) -> tuple[int, list[dict]]:
    """One Search API page of an organisation's publications, newest first -> (total, results).
    Every result must be an object with a string `link` and `title`; anything else (an error
    object served with HTTP 200, a malformed result) is Unexpected."""
    params = {"filter_organisations": org, "filter_content_store_document_type": list(DOC_TYPES),
              "order": "-public_timestamp", "count": ROWS, "start": start,
              "fields": "title,link,description"}
    if since:
        params["filter_public_timestamp"] = f"from:{since}"
    response = requests.get(SEARCH, params=params, headers=UA, timeout=TIMEOUT)
    if response.status_code != 200:
        raise Unexpected(f"GOV.UK search answered HTTP {response.status_code}")
    try:
        answer = response.json()
        total, results = answer["total"], answer["results"]
    except (ValueError, KeyError, TypeError) as exc:
        raise Unexpected(f"GOV.UK search answer is not a result list: {exc}") from exc
    if isinstance(total, bool) or not isinstance(total, int) or total < 0:
        raise Unexpected("GOV.UK search answer has no integer total")
    if not isinstance(results, list) or len(results) > ROWS:
        raise Unexpected("GOV.UK search results are not a list of at most one page")
    for result in results:
        if not (isinstance(result, dict) and isinstance(result.get("link"), str)
                and result["link"].startswith("/") and isinstance(result.get("title"), str)
                and isinstance(result.get("description", ""), (str, type(None)))):
            raise Unexpected("GOV.UK search result without a string link and title")
    return total, results


def page_documents(path: str) -> tuple[str, list[tuple[str, str, str]]]:
    """Content-API page -> (page title, [(document title, url, format), ...]): the page's PDF
    attachments (assets host), its HTML attachments (publication pages on www.gov.uk), or, when
    it has no attachment at all, the page itself if its body is a real text (a detailed guide).
    A page that is gone (HTTP 404/410, or a `gone` / `redirect` content item) gives nothing.
    An answer without the content-item structure (an error object served with HTTP 200, a
    malformed attachment list) is Unexpected."""
    response = requests.get(CONTENT.format(path=path), headers=UA, timeout=TIMEOUT)
    if response.status_code in (404, 410):
        return "", []
    if response.status_code != 200:
        raise Unexpected(f"GOV.UK content API answered HTTP {response.status_code} for {path}")
    try:
        page = response.json()
    except ValueError as exc:
        raise Unexpected(f"GOV.UK content answer is not JSON for {path}: {exc}") from exc
    if not isinstance(page, dict) or not isinstance(page.get("base_path"), str) \
            or not isinstance(page.get("document_type"), str):
        raise Unexpected(f"GOV.UK content answer is not a content item for {path}")
    if page["document_type"] in ("gone", "redirect", "vanish"):
        return "", []
    details, title = page.get("details"), page.get("title")
    if not isinstance(details, dict) or not isinstance(title, str):
        raise Unexpected(f"GOV.UK content item without details and title for {path}")
    attachments = details.get("attachments", [])
    if not isinstance(attachments, list) or not all(
            isinstance(a, dict) and isinstance(a.get("url"), str) for a in attachments):
        raise Unexpected(f"GOV.UK content item with a malformed attachment list for {path}")
    out = []
    for a in attachments:
        url, name = a["url"], " ".join(str(a.get("title") or "").split())
        if a.get("content_type") == "application/pdf" and ASSET.match(url):
            out.append((name, url, "pdf"))
        elif a.get("attachment_type") == "html" and HTML_PATH.match(url):
            out.append((name, f"https://www.gov.uk{url}", "html"))
    body = details.get("body")
    if not attachments and isinstance(body, str) and len(body) >= MIN_BODY \
            and HTML_PATH.match(path):
        out.append(("", f"https://www.gov.uk{path}", "html"))
    return " ".join(title.split()), out


def page_entries(result: dict, today: str, capped: list[str] | None = None) -> list[dict]:
    """Registry entries for one search result (one Content API request when it is relevant).
    A page with more than ATTACHMENTS_PER_PAGE eligible documents is named in `capped`."""
    path = result["link"]
    page_gate = f"{result.get('title') or ''} ; {result.get('description') or ''}"
    if path.startswith("//") or not title_relevant(page_gate):
        return []
    time.sleep(CONTENT_DELAY)
    page_title, attachments = page_documents(path)
    page_title = page_title or " ".join(result["title"].split())
    eligible = [(t, u, f) for t, u, f in attachments if not ATTACHMENT_SKIP.search(t)]
    if len(eligible) > ATTACHMENTS_PER_PAGE and capped is not None:
        capped.append(f"{path}: first {ATTACHMENTS_PER_PAGE} of {len(eligible)} documents")
    out = []
    for att_title, url, fmt in eligible[:ATTACHMENTS_PER_PAGE]:
        title = (f"{page_title} — {att_title}"
                 if att_title and registry.norm(att_title) != registry.norm(page_title)
                 else page_title or att_title)
        if not title:
            continue
        out.append({"id": f"guk-{registry.slug(title)[:46]}", "title": title[:180],
                    "url": url, "source": "gov_uk", "license": "unverified",
                    "topic": topic_for(title), "format": fmt, "language": "en",
                    "license_evidence": LICENCE_EVIDENCE, "rights_verified_at": today})
    return out


def _past_date(text: str, today: date, what: str) -> date:
    try:
        value = date.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{what} is not a date: {text!r}") from exc
    if value > today:
        raise ValueError(f"{what} is in the future: {text!r}")
    return value


def _position(o: str, s: str) -> tuple[int, int]:
    index, start = int(o), int(s)
    if index >= len(ORGS):
        raise ValueError(f"organisation index {index} is out of range (0..{len(ORGS) - 1})")
    if start > MAX_START:
        raise ValueError(f"start offset {start} is past the search window ({MAX_START})")
    return index, start


def parse_cursor(cursor: str, today: date) -> dict:
    """{mode: walk|watch|catch, o, s, walk (day the walk started), boundary (day coverage is
    complete up to; watch and catch), started (day this look started; catch)}."""
    cursor = cursor.strip()
    if match := CURSOR.match(cursor):
        o, s = _position(match.group(1), match.group(2))
        walk = _past_date(match.group(3), today, "walk day") if match.group(3) else today
        return {"mode": "walk", "o": o, "s": s, "walk": walk}
    if match := WATCH.match(cursor):
        boundary = _past_date(match.group(1), today, "watch boundary")
        walk = _past_date(match.group(2), today, "walk day") if match.group(2) else boundary
        if walk > boundary:
            raise ValueError(f"walk day is after the watch boundary: {cursor!r}")
        return {"mode": "watch", "o": 0, "s": 0, "walk": walk, "boundary": boundary}
    if match := CATCH.match(cursor):
        boundary = _past_date(match.group(1), today, "catch-up boundary")
        started = _past_date(match.group(2), today, "catch-up start day")
        o, s = _position(match.group(3), match.group(4))
        walk = _past_date(match.group(5), today, "walk day")
        if not walk <= boundary <= started:
            raise ValueError(f"catch-up days are not in order: {cursor!r}")
        return {"mode": "catch", "o": o, "s": s, "walk": walk, "boundary": boundary,
                "started": started}
    raise ValueError("cursor must be 'o=<index> s=<start> [w=<day>]', 'watch:<day> [w=<day>]' "
                     f"or 'catch:<day> n=<day> o=<index> s=<start> w=<day>': {cursor!r}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cursor", default="o=0 s=0",
                    help="walk 'o=<organisation index> s=<start> [w=<day>]', 'watch:<day> "
                         "[w=<day>]' or 'catch:<day> n=<day> o=<index> s=<start> w=<day>'")
    ap.add_argument("--pages", type=int, default=4,
                    help=f"max Search API pages ({ROWS} results each) this run")
    ap.add_argument("--max", type=int, default=150,
                    help="page-granular target for new entries (the last page may exceed it)")
    ap.add_argument("--append", action="store_true",
                    help="append into the registry (registry/govuk-*.yaml)")
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
    mode, o, start, walk = state["mode"], state["o"], state["s"], state["walk"]
    boundary, started = state.get("boundary"), state.get("started")
    if mode == "watch":
        if now.date() >= walk + timedelta(days=REWALK_DAYS):
            # periodic reconciliation: a page lost to a listing shift during an offset walk has
            # an old timestamp and no watch look returns to it; only a full walk does
            mode, walk = "walk", now.date()
        elif now.date() < boundary + timedelta(days=WATCH_DAYS):
            print(f"# 0 NEW gov.uk publications (covered to {boundary}; next look "
                  f"{boundary + timedelta(days=WATCH_DAYS)})")
            report.next(args.cursor.strip())
            return
        else:
            mode, started = "catch", now.date()
    # a catch-up reads the pages published or updated since the day before the boundary
    since = (boundary - timedelta(days=1)).isoformat() if mode == "catch" else None

    keys = dedup.open_keys()
    out: list[dict] = []
    capped: list[str] = []
    windowed: list[str] = []
    scanned = relevant = pages = 0
    try:
        # both modes stop between complete pages when a budget is spent; the cursor continues
        while o < len(ORGS) and pages < args.pages and len(out) < args.max:
            if pages:
                time.sleep(SEARCH_DELAY)
            total, results = search(ORGS[o], start, since)
            pages += 1
            scanned += len(results)
            if len(results) < ROWS and start + len(results) < total:
                # an empty or short page while the API says more results remain
                raise Unexpected(f"GOV.UK search page has {len(results)} results at start "
                                 f"{start} of {total}")
            if since is None and not total:
                # every organisation had publications on 2026-10-12: an empty unfiltered
                # listing means the slug or a filter value no longer matches
                raise Unexpected(f"GOV.UK search lists no publication for {ORGS[o]}")
            entries = []
            for result in results:
                found = page_entries(result, today, capped)
                relevant += bool(found)
                entries += found
            keys.prefetch(**dedup.page_keys(entries))
            for entry in entries:
                u, t = entry["url"].rstrip("/"), registry.norm(entry["title"])
                if u in keys.urls or t in keys.titles:
                    continue
                keys.urls.add(u)
                keys.titles.add(t)
                out.append(entry)
            if start + len(results) >= total:
                o, start = o + 1, 0
            elif start >= MAX_START:
                windowed.append(f"{ORGS[o]}: {total} results, the Search API window ends at "
                                f"{MAX_START + ROWS}")
                o, start = o + 1, 0
            else:
                # small removals shift later results up: re-read a few; the last page of the
                # window starts exactly at MAX_START so its final results are read too
                start = min(start + ROWS - OVERLAP, MAX_START)
    except Unexpected as exc:
        report.hold(f"{exc} at o={o} s={start}; nothing proposed, retrying next round")
        print("# 0 NEW gov.uk publications (unexpected answer)")
        return
    except requests.RequestException as exc:
        print(f"# ERROR: gov.uk request failed at o={o} s={start}: {exc}; refusing a partial "
              "append so rotation does not advance", file=sys.stderr)
        raise SystemExit(1)

    keys.uniquify_ids(out)
    by_topic: dict = {}
    for h in out:
        by_topic[h["topic"]] = by_topic.get(h["topic"], 0) + 1
    print(f"# {len(out)} NEW gov.uk publications ({mode}: {relevant}/{scanned} pages relevant "
          f"over {pages} search pages; licence unverified, deduped vs manifest + registry + "
          "blocklist)")
    print(f"# by topic: {by_topic}")
    for line in capped:
        print(f"# COVERAGE EXCLUSION (attachment cap) {line}")
    for line in windowed:
        print(f"# COVERAGE EXCLUSION (search window) {line}")
    print("# --- review, then --append, then scripts/build_corpus.py ---")
    print(yaml.safe_dump(out, sort_keys=False, allow_unicode=True))

    if args.append and out:
        counts = registry.append_entries(out)
        print(f"# appended {len(out)} entries to the registry: {counts}", file=sys.stderr)
    if mode == "walk":
        # coverage after a full walk is complete up to the day the walk STARTED: a page
        # published or updated while it ran sorts ahead of the saved offset
        report.next(f"o={o} s={start} w={walk}" if o < len(ORGS) else f"watch:{walk} w={walk}")
    else:
        report.next(f"catch:{boundary} n={started} o={o} s={start} w={walk}"
                    if o < len(ORGS) else f"watch:{started} w={walk}")


if __name__ == "__main__":
    main()
