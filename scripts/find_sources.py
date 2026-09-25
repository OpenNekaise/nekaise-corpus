#!/usr/bin/env python3
"""find_sources.py — discover open-access building-energy sources (corpus growth).

Backends:
  - OpenAlex  : metered scholarly search. Every location of a work is inspected and a copy is kept
                only when it sits on an allowed download host (exact host or subdomain match) AND
                carries accepted rights evidence for that very copy (CC BY / BY-SA / CC0 /
                verified public domain; scripts/oa_resolution.py). An unknown licence is never
                registered as `open`. Anonymous access allows 100 search calls/day, so the legacy
                family advances one query/page cursor position per round, and shares that budget
                with the query families (see below).
  - OSTI / arXiv : FAIL CLOSED. Their APIs carry no per-record reuse licence here (OSTI is not
                blanket public domain; arXiv's default licence is not an open grant), so they
                propose nothing until a per-record rights source is wired in.

Query families (scripts/openalex_families.py) are separately configured, independently
versioned OpenAlex query lists, each with its own backend and dynamic cursor, sharing ONE search
per round with the legacy queries — `simulation` (find_openalex_sim) and `building-ai`
(find_openalex_ai):

    python scripts/find_sources.py --family simulation --per 100 --max 25 --lookup-max 25 \
      --family-cursor "sim1 t=0 q=0 w=0 p=1 k=0 sq=0 sw=0 sp=1 sk=0"

Keeps candidates with a fetchable PDF, dedups against the current manifest + registry, and PROPOSES
ready-to-paste registry entries. Review, then `--append` and run the loader.

    python scripts/find_sources.py --per 100 --backends openalex \
      --query-cursor 0 --query-count 1                          # query 0 on page 1
    python scripts/find_sources.py --per 100 --backends openalex \
      --query-cursor 105 --query-count 1                        # query 0 on page 2
"""
from __future__ import annotations

import argparse
import email.utils
import json
import math
import os
import re
import sys
import time
from pathlib import Path

import requests
import yaml

import ops
import dedup
import oa_resolution as oar
import openalex_families
import openalex_state
import registry
import store

HERE = Path(__file__).resolve().parents[1]  # repo root (this file lives in scripts/)
MAILTO = oar.MAILTO
# Per-host API cooldowns live in ONE machine-level file shared by every checkout, worktree and
# concurrent finder (openalex_state.Cooldowns: locked max-merge writes, re-read before every
# request). None = that file; tests point it elsewhere.
COOLDOWN_FILE: Path | None = None

# (search term -> our corpus topic). Many specific sub-topic queries -> more unique results.
QUERIES = [
    ("HVAC supervisory control sequences ASHRAE Guideline 36", "controls_bas"),
    ("model predictive control building HVAC energy", "controls_bas"),
    ("reinforcement learning HVAC control building", "controls_bas"),
    ("BACnet Modbus building automation communication protocol", "controls_bas"),
    ("demand controlled ventilation occupancy based control", "controls_bas"),
    ("PID control loop tuning air handling unit", "controls_bas"),
    ("automated fault detection diagnostics HVAC chiller", "commissioning_fdd"),
    ("building retro-commissioning energy savings", "commissioning_fdd"),
    ("air handling unit fault detection sensor diagnostics", "commissioning_fdd"),
    ("anomaly detection building energy operation", "commissioning_fdd"),
    ("monitoring based commissioning building performance", "commissioning_fdd"),
    ("rooftop unit fault detection diagnostics RTU", "commissioning_fdd"),
    ("chiller plant optimization performance modeling", "equipment_systems"),
    ("heat pump performance coefficient of performance", "equipment_systems"),
    ("variable refrigerant flow VRF system performance", "equipment_systems"),
    ("cooling tower condenser water system", "equipment_systems"),
    ("energy recovery ventilation enthalpy wheel", "equipment_systems"),
    ("boiler hydronic heating system efficiency", "equipment_systems"),
    ("building energy simulation EnergyPlus calibration", "building_energy"),
    ("building envelope thermal performance retrofit", "building_energy"),
    ("net zero energy building design renewable", "building_energy"),
    ("building electricity load forecasting machine learning", "building_energy"),
    ("building electrification heat pump decarbonization", "building_energy"),
    ("occupant thermal comfort energy efficiency building", "building_energy"),
    ("urban building energy modeling stock", "building_energy"),
    ("Brick schema building ontology metadata", "standards_protocols"),
    ("Project Haystack semantic tagging building data", "standards_protocols"),
    ("building information modeling IFC interoperability", "standards_protocols"),
    ("semantic data model building automation 223P", "standards_protocols"),
    ("digital twin building automation systems", "standards_protocols"),
    ("grid interactive efficient buildings demand flexibility", "standards_protocols"),
    # --- depth + under-pumped veins (mission: MORE good building-energy text) ---
    ("advanced rooftop unit controller retrofit savings", "controls_bas"),
    ("data center cooling control optimization efficiency", "controls_bas"),
    ("lighting controls daylighting commercial building", "controls_bas"),
    ("ASHRAE Guideline 36 high performance control sequences", "controls_bas"),
    ("ongoing commissioning energy information system", "commissioning_fdd"),
    ("chiller plant fault detection field demonstration", "commissioning_fdd"),
    ("automated fault detection diagnostics building portfolio", "commissioning_fdd"),
    ("cold climate air source heat pump field performance", "equipment_systems"),
    ("heat pump water heater field performance", "equipment_systems"),
    ("thermal energy storage building cooling load shifting", "equipment_systems"),
    ("dedicated outdoor air system DOAS design performance", "equipment_systems"),
    ("electrification gas to heat pump retrofit building", "equipment_systems"),
    ("building stock energy model ResStock ComStock", "building_energy"),
    ("commercial buildings energy consumption survey end use", "building_energy"),
    ("residential energy consumption end use load profile", "building_energy"),
    ("deep energy retrofit measured savings case study", "building_energy"),
    ("embodied carbon building life cycle assessment", "building_energy"),
    ("ASHRAE 90.1 energy savings determination", "standards_protocols"),
    ("residential energy code cost effectiveness IECC", "standards_protocols"),
    ("building energy code compliance field study", "standards_protocols"),
    ("measurement and verification IPMVP savings protocol", "standards_protocols"),
    # --- adjacent thermal-science / energy-systems veins (imagination: what feeds building energy) ---
    ("vapor compression refrigeration cycle thermodynamics", "equipment_systems"),
    ("heat exchanger design effectiveness NTU method", "equipment_systems"),
    ("psychrometrics moist air humidity dehumidification HVAC", "equipment_systems"),
    ("computational fluid dynamics indoor airflow ventilation", "equipment_systems"),
    ("heat transfer conduction convection radiation building", "equipment_systems"),
    ("absorption chiller thermally driven cooling performance", "equipment_systems"),
    ("radiant heating cooling ceiling panel thermal comfort", "equipment_systems"),
    ("solar photovoltaic building integrated performance", "building_energy"),
    ("solar thermal collector domestic hot water system", "equipment_systems"),
    ("ground source heat pump geothermal borehole design", "equipment_systems"),
    ("district heating cooling thermal network fifth generation", "building_energy"),
    ("combined heat and power cogeneration building energy", "equipment_systems"),
    ("phase change material thermal energy storage building", "equipment_systems"),
    ("battery energy storage building peak demand management", "building_energy"),
    ("electric motor variable frequency drive fan pump efficiency", "equipment_systems"),
    ("daylighting illuminance visual comfort electric lighting", "controls_bas"),
    ("indoor air quality ventilation contaminant removal effectiveness", "building_energy"),
    ("natural ventilation passive cooling building design", "building_energy"),
    ("hygrothermal moisture transport building envelope", "building_energy"),
    ("window glazing solar heat gain coefficient daylight", "equipment_systems"),
    ("life cycle assessment embodied carbon building materials", "standards_protocols"),
    ("smart grid demand response transactive energy building", "controls_bas"),
    # === AEC / built-environment broadening (mission widened: whole built environment) ===
    # structures / civil / bridges / geotech / seismic
    ("reinforced concrete structural design flexure shear", "structures_civil"),
    ("structural steel design connection stability", "structures_civil"),
    ("seismic design performance based earthquake building", "structures_civil"),
    ("bridge load rating fatigue evaluation", "structures_civil"),
    ("finite element structural analysis nonlinear", "structures_civil"),
    ("geotechnical foundation bearing capacity settlement", "structures_civil"),
    ("slope stability soil mechanics retaining wall", "structures_civil"),
    ("wind engineering structural response tall building", "structures_civil"),
    ("cross laminated mass timber structural performance", "structures_civil"),
    ("bridge inspection structural health monitoring", "structures_civil"),
    # construction / management / safety
    ("construction project management scheduling delay", "construction"),
    ("construction cost estimating productivity analysis", "construction"),
    ("construction safety fall protection hazard", "construction"),
    ("prefabrication modular offsite construction", "construction"),
    ("concrete construction curing formwork quality control", "construction"),
    ("building information modeling construction coordination", "construction"),
    # materials
    ("concrete durability chloride corrosion service life", "materials"),
    ("supplementary cementitious materials fly ash slag", "materials"),
    ("asphalt binder pavement material characterization", "materials"),
    ("fiber reinforced polymer strengthening structures", "materials"),
    ("fracture fatigue material testing structural steel", "materials"),
    # architecture / codes / fire
    ("fire protection engineering egress smoke evacuation", "architecture"),
    ("building code structural fire safety compliance", "architecture"),
    ("architectural facade daylighting design performance", "architecture"),
    ("accessibility universal design built environment", "architecture"),
    ("historic building preservation adaptive reuse", "architecture"),
    # urban / infrastructure / environment
    ("urban planning land use transportation built environment", "urban"),
    ("pavement design highway infrastructure management", "infrastructure"),
    ("water distribution wastewater infrastructure hydraulic", "infrastructure"),
    ("flood risk resilience coastal infrastructure", "infrastructure"),
    ("geospatial GIS surveying mapping built environment", "infrastructure"),
]
QUERY_CURSOR_WIDTH = 105  # Changing this query universe requires a reviewed cursor migration.

# Download hosts, operational pauses (OSTI, PMC) and NO-GO hosts live in scripts/oa_resolution.py
# (exact host / subdomain matching); fetch-suspended hosts (MDPI, eScholarship, SSRN) come from the
# pinned registry/host_policy.json, loaded once per run by load_context().
PAUSED_PDF_HOSTS = oar.PAUSED_PDF_HOSTS
PERMISSIVE = set(oar.ACCEPTED_TAGS)
_POLICY: dict | None = None  # the pinned host policy for this run (load_context)

# OpenAlex's `search` covers full text, so even a specific HVAC query can rank astronomy
# instruments, particle detectors, and medical imaging papers highly because they discuss sensors,
# cooling, or fault detection.  Keep the metadata gate deliberately narrow, then rescue works whose
# titles carry an unambiguous built-environment anchor.  Subfield ids are OpenAlex's stable ASJC ids.
OPENALEX_AEC_SUBFIELDS = frozenset({
    "2205",  # Civil and Structural Engineering
    "2213",  # Safety, Risk, Reliability and Quality
    "2215",  # Building and Construction
    "2216",  # Architecture
    "2305",  # Environmental Engineering
    "2311",  # Waste Management and Disposal
    "2312",  # Water Science and Technology
    "3305",  # Geography, Planning and Development
})
# OpenAlex occasionally assigns an AEC-adjacent subfield to papers whose title makes their actual
# domain unambiguous.  Reject these narrow false-positive classes before the subfield rescue; the
# full-text quality gate cannot distinguish them because they are rich in construction/materials
# vocabulary.
OPENALEX_TITLE_KILL = re.compile(
    r"\b(?:crop phenotyp\w*|dark matter (?:detector|experiment|search)|"
    r"trophic networks?|leptospir\w*)\b",
    re.I,
)
OPENALEX_TITLE_RELEVANCE = re.compile(
    r"\b(?:"
    r"built environment|buildings|smart buildings?|building schools?|"
    r"building[- ](?:energy|automation|control|envelope|information|"
    r"management|operation|performance|retrofit|simulation|stock|systems?)|"
    r"hvac|air[- ]condition|ventilat|heat pump|chiller|boiler|thermostat|refrigerant|f-gases|"
    r"(?:occupant|indoor|human) thermal comfort|thermal comfort (?:in|for) buildings|"
    r"occupant[- ](?:centric|behavior|comfort|related)|indoor air|daylight|glazing|"
    r"district heat|district cool|"
    r"architectur|civil engineering|construction|contractor|scaffold|formwork|"
    r"reinforced concrete|cementitious|cement hydrates?|masonry|structural steel|mass timber|"
    r"pavement|asphalt|bridge (?:deck|inspection|load|design|bearing|girder)|"
    r"geotechn|foundation|soil mechanics|slope stability|retaining wall|land subsidence|"
    r"infrastructur|urban|housing|land use|zoning|traffic|highway|railway|"
    r"transportation (?:infrastructur|planning|network|system|engineering|demand)|"
    r"public transportation|urban transportation|"
    r"water distribution|water treatment|wastewater|stormwater|sewer|drainage|"
    r"fire protection|fire detection|egress|smoke control"
    r")\b|"
    r"gebäude|bauwesen|bâtiment|génie civil|edifici[oo]|construcci[oó]n|construção|"
    r"建築|建筑|暖通|空調|空调|건축|공조|здани|строительств",
    re.I,
)


def _policy() -> dict:
    if _POLICY is None:
        raise RuntimeError("host policy not loaded: call load_context() first (fail closed)")
    return _POLICY


def downloadable(url: str, policy: dict | None = None) -> bool:
    """Whether a URL may be selected as the copy to fetch (never a licence decision)."""
    return oar.copy_refusal(url, _policy() if policy is None else policy) is None


def load_context(partners=()) -> tuple[dict, dict[str, bool]]:
    """(pinned host policy, {budget partner: effectively enabled}) from ONE store read view
    (inside a round the finder inherits the round's read access)."""
    with dedup.read_view() as view:
        _, policy = store.pinned_policy(view)
        enabled = {}
        for name in partners or ():
            try:
                enabled[name] = view.backend_enabled(name)
            except store.StoreError:
                enabled[name] = False
        return policy, enabled


def entry(title, url, source, license, topic, **metadata):
    return {"id": f"{source[:3]}-{registry.slug(title)[:46]}", "title": (title or "").strip()[:150],
            "url": url, "source": source, "license": license, "topic": topic, "format": "pdf",
            **metadata}


def _openalex_subfield_ids(work: dict) -> set[str]:
    topics = [work.get("primary_topic"), *(work.get("topics") or [])]
    return {
        str(topic.get("subfield", {}).get("id", "")).rstrip("/").rsplit("/", 1)[-1]
        for topic in topics
        if topic and topic.get("subfield", {}).get("id")
    }


def openalex_relevant(work: dict, title: str) -> bool:
    """Admit structured AEC topics or an unambiguous multilingual AEC title."""
    if OPENALEX_TITLE_KILL.search(title or ""):
        return False
    return bool(_openalex_subfield_ids(work) & OPENALEX_AEC_SUBFIELDS) or bool(
        OPENALEX_TITLE_RELEVANCE.search(title or "")
    )


def _openalex_location(work: dict, policy: dict | None = None) -> tuple[dict, str] | None:
    """(OpenAlex location, accepted licence tag) of the copy select_copy() would fetch, or None.
    Compatibility view of oa_resolution.select_copy for the legacy family."""
    res = oar.select_copy(work, _policy() if policy is None else policy)
    if res.status != "resolved":
        return None
    for loc in oar.work_locations(work):
        if (loc.get("pdf_url") or "").strip().rstrip("/") == res.copy.url.rstrip("/"):
            return loc, res.rights.tag
    return None


def openalex_entry(work: dict, res: "oar.Resolution", topic: str, today: str) -> dict:
    """A legacy-family registry entry (id prefix ope-) with the same rights evidence and
    identity fields as the query families."""
    title = work.get("title") or work.get("display_name")
    meta = openalex_families.build_entry(work, res, topic=topic, source="openalex",
                                         family="legacy", today=today)
    for key in ("id", "title", "url", "source", "license", "topic", "format"):
        meta.pop(key, None)
    return entry(title, res.copy.url, "openalex", res.rights.tag, topic, **meta)


def from_openalex(term, topic, per, page=1):
    p = {"search": term, "filter": "open_access.is_oa:true,type:article|preprint",
         "per-page": per, "page": page,
         "sort": "cited_by_count:desc", "mailto": MAILTO}
    # the same machine-wide spacing, persisted cooldowns and throttle classification as the
    # query families (openalex_state.openalex_get)
    r = openalex_state.openalex_get(requests.get, "https://api.openalex.org/works", params=p,
                                    timeout=30, cooldowns=cooldown_store())
    r.raise_for_status()
    out = []
    today = time.strftime("%Y-%m-%d", time.gmtime())
    unresolved = 0
    for w in r.json().get("results", []):
        title = w.get("title") or w.get("display_name")
        if not title or not openalex_relevant(w, title):
            continue
        res = oar.select_copy(w, _policy())
        if res.status != "resolved":
            unresolved += 1  # no copy with accepted rights: never registered as `open`
            continue
        out.append(openalex_entry(w, res, topic, today))
    if unresolved:
        print(f"# openalex [{topic}] {unresolved} relevant work(s) without an eligible "
              "licensed copy skipped", file=sys.stderr)
    return out


class RightsUnavailable(RuntimeError):
    """A backend whose API exposes no per-record reuse licence: it proposes nothing."""


def from_osti(term, topic, per, page=1):
    # OSTI records are not blanket public domain (contractor copyright, journal versions), and
    # the search API carries no per-record rights statement: fail closed.
    raise RightsUnavailable("osti backend disabled: no per-record rights evidence (fail closed)")


def from_arxiv(term, topic, per, page=1):
    # arXiv's default licence is not an open grant and the query API carries no licence field:
    # fail closed instead of registering unlicensed arXiv bytes as `open`.
    raise RightsUnavailable("arxiv backend disabled: no per-record licence (fail closed)")


BACKENDS = {"openalex": from_openalex, "osti": from_osti, "arxiv": from_arxiv}


def query_window(
    queries: list[tuple[str, str]],
    page: int,
    cursor: int | None,
    count: int | None,
) -> list[tuple[str, str, int]]:
    """Return query/topic/page work for either a fixed page or a rotating cursor.

    The cursor flattens pages across the immutable query list: positions 0..N-1 are page 1,
    N..2N-1 are page 2, and so on.  This lets the control plane spend a bounded number of API
    calls per round without repeatedly mining only the head page.
    """
    if not queries:
        return []
    if cursor is None:
        selected = queries if count is None else queries[:count]
        return [(term, topic, page) for term, topic in selected]
    width = len(queries)
    return [
        (*queries[position % width], page + position // width)
        for position in range(cursor, cursor + (count if count is not None else 1))
    ]


def cooldown_store(path: Path | None = None) -> "openalex_state.Cooldowns":
    return openalex_state.Cooldowns(path or COOLDOWN_FILE)


def load_cooldowns(now: float, path: Path | None = None) -> dict[str, float]:
    """Unexpired per-host API cooldowns; malformed state never blocks discovery."""
    store = cooldown_store(path)
    return {k: v for k, v in store._read().items() if v > now}


def save_cooldowns(cooldowns: dict[str, float], path: Path | None = None) -> None:
    """Merge these deadlines into the shared file, keeping the MAXIMUM per host (a locked
    read-merge-write: a process's startup snapshot never erases another's cooldown)."""
    cooldown_store(path).raise_to(cooldowns)


def retry_after_deadline(value: str | None, now: float) -> float | None:
    """Convert an HTTP Retry-After delay or date into an epoch deadline."""
    if not value:
        return None
    try:
        return now + max(0, int(value))
    except ValueError:
        try:
            parsed = email.utils.parsedate_to_datetime(value)
            return max(now, parsed.timestamp())
        except (TypeError, ValueError, OverflowError):
            return None


def request_rotation_hold(reason: str) -> None:
    """Ask run_round to revisit this page after a partial upstream run."""
    if hold_name := os.environ.get("NEKAISE_ROTATION_HOLD_FILE"):
        ops.atomic_write_text(Path(hold_name), reason + "\n")
    print(f"# rotation hold requested: {reason}", file=sys.stderr)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per", type=int, default=15, help="results per backend per topic query")
    ap.add_argument("--page", type=int, default=1, help="result page (rotate deeper each round)")
    ap.add_argument(
        "--query-cursor",
        type=int,
        help="flattened query/page cursor for budgeted rotation (requires --query-count)",
    )
    ap.add_argument(
        "--query-count",
        type=int,
        help="number of consecutive queries to request (default: every query)",
    )
    ap.add_argument("--backends", default="openalex,osti,arxiv")
    ap.add_argument(
        "--circuit-threshold",
        type=int,
        default=3,
        help="stop a backend for this round after this many consecutive 429/503 responses",
    )
    ap.add_argument("--append", action="store_true",
                    help="append candidates into the registry shards (then load + prune)")
    ap.add_argument("--family", choices=sorted(openalex_families.FAMILIES),
                    help="run a versioned OpenAlex query family instead of the legacy queries")
    ap.add_argument("--family-cursor", help="the family's dynamic rotation cursor")
    ap.add_argument("--max", type=int, default=25,
                    help="family: accepted documents per run (a hit keeps the page position)")
    ap.add_argument("--lookup-max", type=int, default=25,
                    help="family: supplementary metadata lookups per run")
    ap.add_argument("--resolution-file",
                    help="family: resolution record path (default workspace/"
                         "openalex-resolution-<family>.jsonl when --append)")
    ap.add_argument("--budget-partner", action="append", default=[],
                    help="legacy: a family backend sharing the one-search-per-round OpenAlex "
                         "budget (repeatable); the legacy family searches only in its own round "
                         "slots, or in a slot whose owning backend is disabled")
    args = ap.parse_args()
    if args.family:
        return main_family(args)
    if args.page < 1:
        ap.error("--page must be at least 1")
    if args.query_cursor is not None and args.query_cursor < 0:
        ap.error("--query-cursor must be at least 0")
    if args.query_cursor is not None and args.query_count is None:
        ap.error("--query-cursor requires --query-count")
    if args.query_cursor is not None and len(QUERIES) != QUERY_CURSOR_WIDTH:
        ap.error(
            f"query cursor expects {QUERY_CURSOR_WIDTH} queries, found {len(QUERIES)}; "
            "migrate the committed cursor before changing the query universe"
        )
    if args.query_count is not None and not 1 <= args.query_count <= len(QUERIES):
        ap.error(f"--query-count must be between 1 and {len(QUERIES)}")
    backends = [b.strip() for b in args.backends.split(",") if b.strip() in BACKENDS]
    if not backends:
        ap.error("--backends did not select any known backend")

    global _POLICY
    try:
        _POLICY, partners_enabled = load_context(args.budget_partner)
    except Exception as exc:  # no pinned host policy: fail closed, the cursor stays
        print(f"# ERROR: cannot load the pinned host policy: {exc}", file=sys.stderr)
        return 1
    if args.budget_partner and args.query_cursor is not None:
        mine, why = openalex_families.legacy_may_search(os.environ.get("NEKAISE_RUN_ID"),
                                                        partners_enabled)
        if not mine:
            print(f"# OpenAlex budget: {why}; the legacy family yields this round")
            request_rotation_hold(f"OpenAlex budget yielded ({why})")
            return 0

    keys = dedup.open_keys()
    urls, titles = keys.urls, keys.titles
    out, seen = [], set()
    throttled: dict[str, int] = {backend: 0 for backend in backends}
    successful_requests: dict[str, int] = {backend: 0 for backend in backends}
    now = time.time()
    if COOLDOWN_FILE is None:  # fold this checkout's pre-2026-09-25 cooldown file in, once
        openalex_state.migrate_legacy_cooldowns(HERE / "workspace")
    cooldowns = load_cooldowns(now)
    disabled = {backend for backend in backends if cooldowns.get(backend, 0) > now}
    incomplete = set(disabled)
    for backend in sorted(disabled):
        remaining = math.ceil(cooldowns[backend] - now)
        print(
            f"# {backend} cooldown active for {remaining}s; skipping it for this round",
            file=sys.stderr,
        )
    query_requests = query_window(
        QUERIES, args.page, args.query_cursor, args.query_count
    )
    for term, topic, query_page in query_requests:
        for b in backends:
            if b in disabled:
                continue
            if until := cooldown_store().active(b):  # re-read: another process may have set it
                disabled.add(b)
                incomplete.add(b)
                print(f"# {b} cooldown active for {math.ceil(until - time.time())}s "
                      "(set meanwhile); skipping it for the rest of this round", file=sys.stderr)
                continue
            try:
                hits = BACKENDS[b](term, topic, args.per, query_page)
            except Exception as e:
                incomplete.add(b)
                print(f"# {b} [{topic}] failed: {e}", file=sys.stderr)
                response = getattr(e, "response", None)
                status = getattr(response, "status_code", None)
                if status in (429, 503):
                    throttled[b] += 1
                    if throttled[b] >= max(1, args.circuit_threshold):
                        disabled.add(b)
                        retry_after = (getattr(response, "headers", {}) or {}).get("Retry-After")
                        suffix = f"; Retry-After={retry_after}" if retry_after else ""
                        if deadline := retry_after_deadline(retry_after, time.time()):
                            cooldowns[b] = max(cooldowns.get(b, 0), deadline)
                            save_cooldowns(cooldowns)
                        print(
                            f"# {b} circuit open after {throttled[b]} throttled requests"
                            f"{suffix}; skipping it for the rest of this round",
                            file=sys.stderr,
                        )
                else:
                    throttled[b] = 0
                continue
            successful_requests[b] += 1
            throttled[b] = 0
            hits = list(hits)
            keys.prefetch(**dedup.page_keys(hits))
            for h in hits:
                u, t = h["url"].rstrip("/"), registry.norm(h["title"])
                if not h["title"] or u in urls or t in titles or u in seen:
                    continue
                if keys.identity_known(h):  # same DOI / OpenAlex work already registered
                    continue
                seen.add(u)
                keys.add_identity(h)
                out.append(h)

    if not any(successful_requests.values()):
        unavailable = ", ".join(backends)
        print(
            f"# ERROR: all selected upstreams were unavailable ({unavailable}); "
            "refusing a false-success discovery run",
            file=sys.stderr,
        )
        return 1

    if incomplete:
        request_rotation_hold(
            "incomplete upstream request(s): " + ", ".join(sorted(incomplete))
        )

    # de-collide ids: truncated title slugs clash across runs; the manifest is id-keyed, so a
    # clash silently overwrites a doc. registry.uniquify_ids guards vs the whole registry.
    keys.uniquify_ids(out)

    by_topic, by_src, by_lic = {}, {}, {}
    for h in out:
        by_topic[h["topic"]] = by_topic.get(h["topic"], 0) + 1
        by_src[h["source"]] = by_src.get(h["source"], 0) + 1
        by_lic[h["license"]] = by_lic.get(h["license"], 0) + 1
    position = (
        f"query cursor {args.query_cursor} ({len(query_requests)} request(s))"
        if args.query_cursor is not None
        else f"page {args.page}"
    )
    print(
        f"# {len(out)} NEW candidates on download-friendly hosts "
        f"({position}; deduped vs manifest + registry)"
    )
    print(f"# by topic:   {by_topic}")
    print(f"# by source:  {by_src}")
    print(f"# by license: {by_lic}")
    print("# --- review, then --append, then run scripts/build_corpus.py ---")
    print(yaml.safe_dump(out, sort_keys=False, allow_unicode=True))

    if args.append and out:
        counts = registry.append_entries(out)
        print(f"# appended {len(out)} entries to the registry: {counts}", file=sys.stderr)
    return 0


def report_next(value: str) -> None:
    """Report a dynamic cursor to run_round (or print it for a standalone run)."""
    if name := os.environ.get("NEKAISE_ROTATION_NEXT_FILE"):
        ops.atomic_write_text(Path(name), value + "\n")
    print(f"# next cursor: {value}", file=sys.stderr)


def main_family(args) -> int:
    global _POLICY
    if not args.family_cursor:
        print("# ERROR: --family requires --family-cursor", file=sys.stderr)
        return 2
    if not 1 <= args.per <= 200 or args.max < 1 or args.lookup_max < 0:
        print("# ERROR: --per must be 1..200, --max >= 1, --lookup-max >= 0", file=sys.stderr)
        return 2
    try:
        _POLICY, _ = load_context()
    except Exception as exc:  # no pinned host policy: fail closed, the cursor stays
        print(f"# ERROR: cannot load the pinned host policy: {exc}", file=sys.stderr)
        return 1
    now = time.time()
    if COOLDOWN_FILE is None:
        openalex_state.migrate_legacy_cooldowns(HERE / "workspace")
    return openalex_families.main_family(
        args, policy=_POLICY, keys=dedup.open_keys(), cooldowns=cooldown_store(),
        get=requests.get, openalex_relevant=openalex_relevant,
        append_entries=registry.append_entries, request_hold=request_rotation_hold,
        report_next=report_next, now=now, run_id=os.environ.get("NEKAISE_RUN_ID"))


if __name__ == "__main__":
    sys.exit(main())
