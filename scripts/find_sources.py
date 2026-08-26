#!/usr/bin/env python3
"""find_sources.py — discover open-access building-energy sources (corpus growth).

Three keyless backends:
  - OpenAlex  : metered scholarly search; we scan ALL OA locations and keep a PDF on a
                download-friendly host (publisher pages 403 bots, so we prefer repository / gov /
                arXiv / PMC / MDPI copies). Anonymous access allows 100 search calls/day, so the
                automated backend advances one query/page cursor position per round.
  - OSTI      : US DOE / national-lab reports (public-domain, downloadable via /servlets/purl).
  - arXiv API : open preprints (always downloadable at arxiv.org/pdf).

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
from urllib.parse import urlparse

import requests
import yaml

import ops
import registry

HERE = Path(__file__).resolve().parents[1]  # repo root (this file lives in scripts/)
MAILTO = "corpus@opennekaise.org"
COOLDOWN_FILE = HERE / "workspace" / "find-sources-cooldowns.json"

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

# hosts that reliably serve a direct PDF to a bot (publisher pages 403, so we whitelist).
WHITELIST = ("arxiv.org", ".gov", "escholarship.org", "ncbi.nlm.nih.gov", "europepmc.org",
             "mdpi.com", "plos.org", "frontiersin.org", "biomedcentral.com")
PERMISSIVE = {"cc-by", "cc-by-sa", "cc0", "public-domain"}


def downloadable(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(w in host for w in WHITELIST)


def entry(title, url, source, license, topic):
    return {"id": f"{source[:3]}-{registry.slug(title)[:46]}", "title": (title or "").strip()[:150],
            "url": url, "source": source, "license": license, "topic": topic, "format": "pdf"}


def from_openalex(term, topic, per, page=1):
    p = {"search": term, "filter": "open_access.is_oa:true,type:article",
         "per-page": per, "page": page,
         "sort": "cited_by_count:desc", "mailto": MAILTO}
    r = requests.get("https://api.openalex.org/works", params=p, timeout=30)
    r.raise_for_status()
    out = []
    for w in r.json().get("results", []):
        locs = [w.get("best_oa_location")] + (w.get("locations") or [])
        pick = next((l for l in locs if l and l.get("pdf_url") and downloadable(l["pdf_url"])), None)
        if not pick:
            continue
        lic = (pick.get("license") or "").lower()
        tag = lic if lic in PERMISSIVE else "open"
        title = w.get("title") or w.get("display_name")
        if title:
            out.append(entry(title, pick["pdf_url"], "openalex", tag, topic))
    return out


def from_osti(term, topic, per, page=1):
    r = requests.get("https://www.osti.gov/api/v1/records",
                     params={"q": term, "rows": per}, timeout=30)
    r.raise_for_status()
    out = []
    for rec in r.json():
        oid = rec.get("osti_id") or rec.get("id")
        ft = any("purl" in (l.get("href") or "") or l.get("rel") == "fulltext"
                 for l in rec.get("links", []))
        if not oid or not ft:
            continue
        out.append(entry(rec.get("title"), f"https://www.osti.gov/servlets/purl/{oid}",
                         "osti", "public-domain", topic))
    return out


def from_arxiv(term, topic, per, page=1):
    time.sleep(3.1)  # arXiv API asks for >=3s between requests (429 otherwise)
    r = requests.get("https://export.arxiv.org/api/query",
                     params={"search_query": f"all:{term}", "max_results": per,
                             "start": (page - 1) * per,
                             "sortBy": "relevance"}, timeout=30)
    r.raise_for_status()
    out = []
    for e in re.findall(r"<entry>(.*?)</entry>", r.text, re.S):
        m = re.search(r"<id>https?://arxiv\.org/abs/([^<]+)</id>", e)
        t = re.search(r"<title>(.*?)</title>", e, re.S)
        if not m:
            continue
        aid = re.sub(r"v\d+$", "", m.group(1).strip())
        out.append(entry((t.group(1) if t else "").strip(),
                         f"https://arxiv.org/pdf/{aid}", "arxiv", "open", topic))
    return out


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


def load_cooldowns(now: float, path: Path | None = None) -> dict[str, float]:
    """Load unexpired local API cooldowns; malformed scratch state never blocks discovery."""
    path = path or COOLDOWN_FILE
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        if not isinstance(data, dict):
            raise ValueError("expected a JSON object")
        return {
            str(backend): float(until)
            for backend, until in data.items()
            if float(until) > now
        }
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(f"# ignoring invalid cooldown state {path}: {exc}", file=sys.stderr)
        return {}


def save_cooldowns(cooldowns: dict[str, float], path: Path | None = None) -> None:
    path = path or COOLDOWN_FILE
    ops.atomic_write_text(path, json.dumps(cooldowns, sort_keys=True) + "\n")


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
    args = ap.parse_args()
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

    urls, titles, reg_ids = registry.existing_keys()
    out, seen = [], set()
    throttled: dict[str, int] = {backend: 0 for backend in backends}
    successful_requests: dict[str, int] = {backend: 0 for backend in backends}
    now = time.time()
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
            for h in hits:
                u, t = h["url"].rstrip("/"), registry.norm(h["title"])
                if not h["title"] or u in urls or t in titles or u in seen:
                    continue
                seen.add(u)
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
    registry.uniquify_ids(out, reg_ids)

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


if __name__ == "__main__":
    sys.exit(main())
