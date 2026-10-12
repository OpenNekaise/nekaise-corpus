#!/usr/bin/env python3
"""find_cordis.py — public deliverables of EU-funded built-environment projects (CORDIS).

CORDIS (cordis.europa.eu, Publications Office of the EU) publishes the results of Horizon 2020
and Horizon Europe projects. Its EURIO knowledge graph lists every public project deliverable
(211,188 on 2026-10-12) with a title, the project it belongs to and the public download URL in
the Commission's document module (ec.europa.eu/research/participants/documents/downloadPublic,
which scripts/build_corpus.py already follows for the `eud-` rows of find_openaire). OpenAIRE,
the old route, now points most deliverables at DOIs instead of that URL and refuses deep paging.

Access: the public SPARQL endpoint https://cordis.europa.eu/datalab/sparql. CORDIS names it as
the channel for automated access (https://cordis.europa.eu/llms.txt: "Preferred access for
agents: use the SPARQL endpoint or bulk datasets on data.europa.eu for large-scale or automated
access"); robots.txt for `*` does not disallow /datalab/. This finder sends only light queries:
one page of projects by ascending record number (keyset `rcn > N`, no full-text or regex
filter on the server), then the deliverables of the projects it kept, a few projects per query,
at most one request per DELAY seconds. Relevance is decided locally.

Relevance (precision first, the quality gate remains the second safeguard):
  * project: the title names an AEC / built-environment concept, or the objective names at
    least MIN_ABSTRACT_TERMS different ones (buildings and their energy systems, construction,
    building materials, structures, geotechnics, transport / water infrastructure, urban
    planning); a title vetoed by scripts/bes_relevance.py never passes;
  * deliverable: type "Documents, reports", "Other" or "Demonstrations, pilots, prototype", and
    a title that is not project administration (management, quality, ethics and data management
    plans, dissemination / communication / exploitation material, websites, newsletters ...).

Licence: `unverified` for every deliverable. CORDIS states that its CC BY 4.0 reuse licence
"does NOT cover deliverables or publications uploaded by project beneficiaries — those follow
per-project Grant Agreement terms" (llms.txt; legal notice https://cordis.europa.eu/about/legal:
"To use or reproduce content that is not owned by the EU, you may need to seek permission
directly from the respective rightholders"). The documents are public; no reuse licence is
stated, so they are collected into the `unverified` view, never the default corpus.

Rotation (dynamic, `--cursor`): "p=<last project rcn walked>", or "watch:<YYYY-MM-DD>" after the
last project. Running projects publish new deliverables for years, so the walk starts again
WATCH_DAYS after it ended (deduplication keeps only the new documents). An unexpected answer
holds the cursor and proposes nothing. The backend never reports EXHAUSTED.

    python scripts/find_cordis.py --cursor "p=0" --pages 2 --max 50
"""
from __future__ import annotations

import argparse
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone

import requests
import yaml

import bes_relevance
import dedup
import registry
from finder_protocol import Report

ENDPOINT = "https://cordis.europa.eu/datalab/sparql"
UA = {"User-Agent": "nekaise-corpus/find_cordis (research corpus; robots.txt honoured)",
      "Accept": "application/sparql-results+json"}
DELAY = 2.0            # seconds between SPARQL requests
PROJECT_PAGE = 200     # projects per page query
PROJECT_BATCH = 15     # kept projects per deliverable query
WATCH_DAYS = 14        # the full walk starts again this long after it ended
MIN_ABSTRACT_TERMS = 3
SOURCE = "cordis_deliverable"
PREFIX = "eud-"        # shard deliverables.yaml, shared with find_openaire's rows
DOWNLOAD = re.compile(r"^https://(?:www\.)?ec\.europa\.eu/research/participants/documents/"
                      r"downloadPublic\?documentIds=([0-9a-f]{8,40})&appId=PPGMS$")
LICENCE_EVIDENCE = (
    "CORDIS public project deliverable (EURIO graph). CORDIS: its CC BY 4.0 licence 'does NOT "
    "cover deliverables or publications uploaded by project beneficiaries — those follow "
    "per-project Grant Agreement terms' (https://cordis.europa.eu/llms.txt; "
    "https://cordis.europa.eu/about/legal). No reuse licence is stated for this document.")
PROLOG = ("PREFIX eurio: <http://data.europa.eu/s66#>\n"
          "PREFIX skos: <http://www.w3.org/2004/02/skos/core#>\n"
          "PREFIX xsd: <http://www.w3.org/2001/XMLSchema#>\n")
PROJECT_URI = re.compile(r"^http://data\.europa\.eu/s66/resource/projects/[0-9a-f-]{36}$")
GRANT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,39}$")
CURSOR = re.compile(r"^p=(\d+)$")
WATCH = re.compile(r"^watch:(\d{4}-\d{2}-\d{2})$")
KEEP_TYPES = frozenset({"Documents, reports", "Other", "Demonstrations, pilots, prototype"})

# Unambiguous AEC / built-environment concepts in English project text. Words with a common
# figurative sense in proposals ("concrete actions", "bridge the gap", "system architecture",
# "building blocks", "capacity building") count only in their engineering form.
AEC = re.compile(
    r"\bbuildings\b|\b(?:residential|commercial|public|office|historic|existing|new|smart|"
    r"green|sustainable|energy[- ]efficient|zero[- ]energy|tall|timber|school) building\b|"
    r"\bbuilding (?:stock|sector|envelopes?|energy|materials?|industry|renovation|retrofit|"
    r"automation|management systems?|information model(?:l?ing)?|performance|physics|"
    r"components?|elements?|products?|codes?|owners?|design|operation|skins?|fabric|level|"
    r"integrated|simulation|services|site|permit|typolog)|"
    r"\bbuilt environment\b|\bconstruction (?:sector|industry|sites?|works?|materials?|"
    r"products?|process(?:es)?|compan(?:y|ies)|projects?|waste|and demolition|workers?|"
    r"value chain|techniques?|methods?|robot)|\bcivil engineering\b|\bhvac\b|\bheat pumps?\b|"
    r"\bdistrict (?:heating|cooling|energy)\b|\b(?:building|energy|deep|seismic|home|housing) "
    r"retrofit|\bretrofit(?:ting|ted|s)? (?:of )?(?:existing |old |historic |residential |public )?"
    r"(?:buildings?|homes?|houses?|dwellings?|housing|fa[cç]ades?|envelopes?)|\bdeep renovation|"
    r"\benergy renovation|\brenovation (?:wave|rate|market|of )|\brefurbishment\b|"
    r"\bnzebs?\b|\bnearly zero[- ]energy|\bpositive energy (?:district|building|neighbo)|"
    r"\bindoor (?:air|environment|climate|comfort)|\bthermal comfort\b|\bfa[cç]ades?\b|"
    r"\b(?:thermal|building) insulation\b|\binsulation (?:materials?|panels?|systems?)\b|"
    r"\b(?:reinforced|precast|prestressed|recycled|low[- ]carbon|green|fib(?:er|re)[- ]"
    r"reinforced|self[- ]compacting|ultra[- ]high[- ]performance|3d[- ]printed) concrete\b|"
    r"\bconcrete (?:structures?|elements?|production|industry|mix(?:es|tures?)?|slabs?|beams?|"
    r"walls?|bridges?|pavements?|durability|aggregates?|recycling|printing|infrastructure)\b|"
    r"\bcement(?:itious)?\b|\bmasonry\b|\bcross[- ]laminated timber\b|\btimber (?:construction|"
    r"structures?|buildings?|frame)|\bsteel (?:structures?|construction|bridges?|frames?)\b|"
    r"\b(?:road|railway|rail|highway|steel|suspension|pedestrian|masonry|arch) bridges?\b|"
    r"\bbridges and tunnels\b|\b(?:road|railway|rail|metro|highway) tunnels?\b|"
    r"\btunnell?ing (?:works?|machines?|projects?|industry)|\btunnel (?:construction|boring|"
    r"linings?|excavation)|\bpavements?\b|\basphalt\b|\bgeotechn|"
    r"\bseismic (?:retrofit|design|performance|risk|vulnerability|protection|isolation|"
    r"assessment|resilience)|\bearthquake engineering\b|\bstructural (?:health monitoring|"
    r"engineering|design|safety|integrity of (?:buildings|bridges))|"
    r"\burban (?:planning|planners?|design|regeneration|renewal|development|districts?|"
    r"infrastructures?|heat island|mobility plan|drainage|water)|\bsmart cit(?:y|ies)\b|"
    r"\bwastewater\b|\bsewer|\bsewage\b|\bdrinking water\b|\bwater (?:supply|distribution) "
    r"(?:systems?|networks?)|\bbim\b|\barchitectural (?:design|heritage|engineering)\b|"
    r"\barchitects\b|\bheritage buildings?\b|\bhousing\b|\bdwellings?\b|\bfire safety\b|"
    r"\b(?:railway|road|transport|port|airport) infrastructures?\b|\bventilation\b|"
    r"\bdaylight|\bglazing\b|\bbipv\b|\bbuilding[- ]integrated\b|\bheating and cooling\b|"
    r"\bspace heating\b|\bdomestic hot water\b|\bsmart homes?\b|\bhome automation\b|"
    r"\benergy performance (?:certificates?|contract|of buildings)|\bepbd\b|"
    r"\benergy communit(?:y|ies)\b|\bthermal energy storage\b|\bsolar thermal\b|"
    r"\bfacilit(?:y|ies) management\b|\bprefabricat|\bmodular construction\b|"
    r"\bdemolition\b|\bgreen roofs?\b|\bcool roofs?\b|\bflood (?:protection|defen[cs]e)|"
    r"\bcoastal (?:protection|structures?|engineering)|\bdams?\b(?= (?:and|safety|construction))",
    re.I,
)
ADMIN = re.compile(
    r"\bdata management plan|\bdmp\b|\bdissemination|\bcommunication (?:plan|strateg|kit|"
    r"material|activit|report|tools?)|\bexploitation|\bproject (?:management|handbook|website|"
    r"web ?site|logo|identity|brochure|leaflet|newsletter|video|quality|presentation|"
    r"templates?|flyer|poster|portal|plan\b|web)|\bweb ?sites?\b|\bweb ?portal|\bkick[- ]?off|"
    r"\bethic|\brisk (?:management|register|assessment plan)|\bquality (?:assurance|management|"
    r"plan|control plan)|\b(?:periodic|progress|management|interim|activity) reports?\b|"
    r"\bipr\b|\bintellectual property|\bpress releases?|\bsocial media|\bnewsletter|\bflyer|"
    r"\bleaflet|\bposter|\bbrochure|\bminutes\b|\bgender\b|\bconsortium (?:agreement|meeting)|"
    r"\brequirement no\b|\bpopd\b|\bopen research data|\bcommunication and|\bbranding|"
    r"\bvisual identity|\bproject meetings?\b|\badvisory board|\bstakeholder (?:list|database)|"
    r"\bbusiness (?:plan|model)s?\b|\bmarket(?:ing)? (?:plan|strategy)|\bworkshops? report|"
    r"\bfinal (?:conference|event)|\btraining (?:plan|events?)|\bsummer school|\bnetworking|"
    r"\bclustering|\bpromotional|\bvideos?\b|\bwebinars?\b|\bself[- ]assessment plan|"
    r"\bplan for (?:the )?(?:dissemination|exploitation|communication)|\bpedr\b|\bpudf\b",
    re.I,
)
# (pattern -> topic) over project title + objective; first match wins.
TOPIC_RULES = (
    (re.compile(r"\bbuilding automation|\bbuilding management system|\bsmart homes?\b|"
                r"\bhome automation|\bdemand response|\bmodel predictive", re.I), "controls_bas"),
    (re.compile(r"\bheat pumps?\b|\bhvac\b|\bventilation\b|\bthermal energy storage|"
                r"\bsolar thermal|\bdomestic hot water|\bboilers?\b|\bchillers?\b", re.I),
     "equipment_systems"),
    (re.compile(r"\bcement|\bconcrete\b|\binsulation|\btimber\b|\bmasonry\b|"
                r"\bconstruction materials?|\bbuilding materials?", re.I), "materials"),
    (re.compile(r"\bseismic|\bearthquake|\bstructural|\bbridges?\b|\btunnel|\bgeotechn",
                re.I), "structures_civil"),
    (re.compile(r"\bconstruction (?:sector|industry|sites?|works?|process|compan|projects?|"
                r"robot)|\bbim\b|\bprefabricat|\bdemolition", re.I), "construction"),
    (re.compile(r"\bwastewater|\bsewer|\bsewage|\bdrinking water|\bpavement|\basphalt|"
                r"\b(?:railway|road|transport|port|airport) infrastructure|\bflood", re.I),
     "infrastructure"),
    (re.compile(r"\bsmart cit|\burban\b|\bdistrict (?:heating|cooling|energy)|"
                r"\bpositive energy district|\benergy communit", re.I), "urban"),
    (re.compile(r"\barchitect|\bheritage\b|\bfa[cç]ades?\b|\bdaylight", re.I), "architecture"),
)


class Unexpected(RuntimeError):
    """Not a SPARQL JSON result (maintenance page, proxy error, challenge)."""


def sparql(query: str) -> list[dict]:
    response = requests.get(ENDPOINT, params={"query": PROLOG + query}, headers=UA, timeout=120)
    if response.status_code != 200:
        raise Unexpected(f"CORDIS SPARQL answered HTTP {response.status_code}")
    try:
        bindings = response.json()["results"]["bindings"]
    except (ValueError, KeyError, TypeError) as exc:
        raise Unexpected(f"CORDIS SPARQL answer is not a result set: {exc}") from exc
    if not isinstance(bindings, list):
        raise Unexpected("CORDIS SPARQL bindings are not a list")
    rows = []
    for binding in bindings:
        if not isinstance(binding, dict):
            raise Unexpected("CORDIS SPARQL binding is not an object")
        rows.append({k: " ".join(str(v.get("value", "")).split())
                     for k, v in binding.items() if isinstance(v, dict)})
    return rows


def project_page(after: int) -> list[dict]:
    """The next projects with rcn > after, ascending (at most PROJECT_PAGE bindings; fewer
    distinct projects when one repeats). Rows: uri, rcn (int), grant, title, abstract, acronym.
    An empty list means no project is left."""
    rows = sparql(
        "SELECT ?uri ?rcn ?grant ?title ?abstract ?acronym WHERE { "
        "?uri a eurio:Project ; eurio:rcn ?rcn ; eurio:identifier ?grant ; eurio:title ?title . "
        "OPTIONAL { ?uri eurio:abstract ?abstract } "
        "OPTIONAL { ?uri eurio:hasAcronym ?a . ?a eurio:shortForm ?acronym } "
        f"FILTER(xsd:integer(?rcn) > {int(after)}) }} "
        f"ORDER BY xsd:integer(?rcn) LIMIT {PROJECT_PAGE}")
    out, seen = [], set()
    for row in rows:
        if not (row.get("rcn") or "").isdigit() or not PROJECT_URI.match(row.get("uri") or ""):
            raise Unexpected(f"CORDIS project row without a numeric rcn / project URI: {row}")
        if not GRANT.match(row.get("grant") or "") or not row.get("title"):
            raise Unexpected(f"CORDIS project row without a grant identifier / title: {row}")
        row["rcn"] = int(row["rcn"])
        if row["rcn"] <= after:
            raise Unexpected(f"CORDIS project page is not after rcn {after}")
        if row["uri"] in seen:      # a second acronym repeats the row
            continue
        seen.add(row["uri"])
        out.append(row)
    return out


def deliverables(project_uris: list[str]) -> list[dict]:
    """Every deliverable of these projects. Rows: project (uri), id, title, url, language, type."""
    if not project_uris or not all(PROJECT_URI.match(u) for u in project_uris):
        raise ValueError("deliverables() needs CORDIS project URIs")
    values = " ".join(f"<{u}>" for u in project_uris)
    rows = sparql(
        "SELECT ?project ?id ?title ?url ?language ?type WHERE { "
        f"VALUES ?project {{ {values} }} "
        "?project eurio:hasResult ?d . ?d a eurio:ProjectDeliverable ; eurio:identifier ?id ; "
        "eurio:title ?title ; eurio:url ?url . "
        "OPTIONAL { ?d eurio:language ?language } "
        "OPTIONAL { ?d eurio:hasResultType ?t . ?t skos:prefLabel ?type } } ORDER BY ?id")
    for row in rows:  # every selected mandatory variable must be bound, or the answer is unusable
        if not all(row.get(key) for key in ("project", "id", "title", "url")):
            raise Unexpected(f"CORDIS deliverable row without project / id / title / url: {row}")
    return rows


def aec_terms(text: str) -> set[str]:
    return {" ".join(m.group(0).lower().split()) for m in AEC.finditer(text or "")}


def project_relevant(project: dict) -> bool:
    title = project.get("title") or ""
    if bes_relevance.vetoed(title):
        return False
    return bool(AEC.search(title)) or len(aec_terms(project.get("abstract") or "")) >= \
        MIN_ABSTRACT_TERMS


def project_topic(project: dict) -> str:
    text = f"{project.get('title') or ''} ; {project.get('abstract') or ''}"
    for pattern, topic in TOPIC_RULES:
        if pattern.search(text):
            return topic
    return "building_energy"


def deliverable_ok(row: dict) -> bool:
    title = row.get("title") or ""
    return (bool(DOWNLOAD.match(row.get("url") or "")) and len(title) >= 8
            and (row.get("type") or "Documents, reports") in KEEP_TYPES
            and not ADMIN.search(title))


def candidate(row: dict, project: dict, today: str) -> dict | None:
    if not deliverable_ok(row):
        return None
    document = DOWNLOAD.match(row["url"]).group(1)
    # The grant identifier keeps titles of different projects distinct (acronyms repeat, and
    # "Final report" is in every project): the finders' title deduplication is global.
    label = f"{(project.get('acronym') or 'EU project').strip()[:30]} ({project['grant']})"
    title = f"{label}: {row['title']}"
    entry = {
        "id": f"{PREFIX}{registry.slug(project['grant'])[:12]}-{registry.slug(row['title'])[:40]}",
        "title": title[:150],
        "url": ("https://ec.europa.eu/research/participants/documents/downloadPublic"
                f"?documentIds={document}&appId=PPGMS"),
        "source": SOURCE, "license": "unverified", "topic": project_topic(project),
        "format": "pdf", "document_type": "report",
        "license_evidence": LICENCE_EVIDENCE, "rights_verified_at": today,
        "persistent_id": f"cordis:{row['id']}",
    }
    language = (row.get("language") or "").lower()
    if re.fullmatch(r"[a-z]{2}", language):
        entry["language"] = language
    return entry


def parse_cursor(cursor: str) -> tuple[int | None, date | None]:
    """(last rcn walked, None) or (None, date the walk ended)."""
    cursor = cursor.strip()
    if match := CURSOR.match(cursor):
        return int(match.group(1)), None
    if match := WATCH.match(cursor):
        return None, date.fromisoformat(match.group(1))
    raise ValueError(f"cursor must be 'p=<rcn>' or 'watch:<YYYY-MM-DD>': {cursor!r}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cursor", default="p=0", help="'p=<last rcn>' or 'watch:<date>' (rotation)")
    ap.add_argument("--pages", type=int, default=4,
                    help=f"max project pages ({PROJECT_PAGE} projects each) this run")
    ap.add_argument("--max", type=int, default=150,
                    help="page-granular target for new entries (the last page may exceed it)")
    ap.add_argument("--append", action="store_true",
                    help="append into the registry (registry/deliverables.yaml)")
    args = ap.parse_args()
    if args.pages < 1 or args.max < 1:
        ap.error("--pages and --max must be positive")
    try:
        after, watched = parse_cursor(args.cursor)
    except ValueError as exc:
        ap.error(str(exc))
    report = Report()
    now = datetime.now(timezone.utc)
    today = now.date().isoformat()
    if watched is not None:
        if now.date() < watched + timedelta(days=WATCH_DAYS):
            print(f"# 0 NEW CORDIS deliverables (walk ended {watched}; next walk from "
                  f"{watched + timedelta(days=WATCH_DAYS)})")
            report.next(args.cursor.strip())
            return
        after = 0

    keys = dedup.open_keys()
    out: list[dict] = []
    scanned = kept_projects = listed = pages = requests_made = 0
    finished = False
    try:
        while pages < args.pages and len(out) < args.max:
            if requests_made:
                time.sleep(DELAY)
            projects = project_page(after)
            requests_made += 1
            pages += 1
            if not projects:
                finished = True
                break
            scanned += len(projects)
            kept = {p["uri"]: p for p in projects if project_relevant(p)}
            kept_projects += len(kept)
            uris = list(kept)
            entries = []
            for start in range(0, len(uris), PROJECT_BATCH):
                time.sleep(DELAY)
                rows = deliverables(uris[start:start + PROJECT_BATCH])
                requests_made += 1
                listed += len(rows)
                for row in rows:
                    if row.get("project") not in kept:
                        raise Unexpected("CORDIS returned a deliverable of an unrequested project")
                    if entry := candidate(row, kept[row["project"]], today):
                        entries.append(entry)
            keys.prefetch(**dedup.page_keys(entries))
            for entry in entries:
                u, t = entry["url"].rstrip("/"), registry.norm(entry["title"])
                if u in keys.urls or t in keys.titles or keys.identity_known(entry):
                    continue
                keys.urls.add(u)
                keys.titles.add(t)
                keys.add_identity(entry)
                out.append(entry)
            # Only an EMPTY page ends the walk: a project with two acronyms repeats its row,
            # so a full answer can hold fewer than PROJECT_PAGE distinct projects.
            after = projects[-1]["rcn"]
    except Unexpected as exc:
        report.hold(f"{exc} after rcn {after}; nothing proposed, retrying next round")
        print("# 0 NEW CORDIS deliverables (unexpected answer)")
        return
    except requests.RequestException as exc:
        print(f"# ERROR: CORDIS request failed after rcn {after}: {exc}; refusing a partial "
              "append so rotation does not advance", file=sys.stderr)
        raise SystemExit(1)

    keys.uniquify_ids(out)
    by_topic: dict[str, int] = {}
    for entry in out:
        by_topic[entry["topic"]] = by_topic.get(entry["topic"], 0) + 1
    print(f"# {len(out)} NEW CORDIS deliverables ({kept_projects}/{scanned} projects kept over "
          f"{pages} pages, {listed} deliverables listed; deduped vs manifest + registry + "
          "blocklist)")
    print(f"# by topic: {by_topic}")
    print("# --- review, then --append, then scripts/build_corpus.py ---")
    print(yaml.safe_dump(out, sort_keys=False, allow_unicode=True))
    if args.append and out:
        counts = registry.append_entries(out)
        print(f"# appended {len(out)} entries to the registry: {counts}", file=sys.stderr)
    report.next(f"watch:{today}" if finished else f"p={after}")


if __name__ == "__main__":
    main()
