#!/usr/bin/env python3
"""find_books.py — discover open-access CC-BY(-SA)/CC0 books from OAPEN (corpus growth).

OAPEN (library.oapen.org) hosts ~40k open-access academic BOOKS and aggregates DOAB / Springer OA /
Frontiers / hundreds of publishers, HOSTING the PDFs itself (unlike DOAB, whose download links are
external; MDPI Books are Cloudflare-blocked from most hosts). Built-environment / AEC subjects are a
dense, cleanly-licensed vein (whole books, hundreds of pages of prose = CPT gold).

This backend PAGINATES the OAPEN REST search across many built-env subjects, keeps books (ANY
language since 2026-07-09) with a direct PDF bitstream whose license is CC-BY / CC-BY-SA / CC0,
and inserts ready-to-load registry entries.  The automated loop uses a stable integer cursor that
selects one (query, page) pair per round; this avoids bursting every deep query at OAPEN together.

License is NOT in the REST metadata — it lives in the OAI-PMH `xoai` record as a
creativecommons.org/licenses/<code> URL. We keep ONLY `by` / `by-sa` / `publicdomain/zero`; ANY `nc`
or `nd` marker rejects the record (fail-closed). Dedup is checked BEFORE the xoai call, so re-runs
paginate deeper cheaply (only genuinely-new candidates cost an xoai lookup).

Entries use the `oer-` id prefix and land in registry/books.yaml; prune_corpus.py quality-gates
them (length-scaled density rule) like every machine-discovered source.

    python scripts/find_books.py                                   # propose
    python scripts/find_books.py --append                          # insert (then load + prune)
    python scripts/find_books.py --per 25 --depth 150 --max 200 --append
    python scripts/find_books.py --per 25 --cursor 21888 --max 25 --append
"""
from __future__ import annotations

import argparse
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
import yaml

import ops
import registry

HERE = Path(__file__).resolve().parents[1]  # repo root (this file lives in scripts/)
SEARCH = "https://library.oapen.org/rest/search"
OAI = "https://library.oapen.org/oai/request"
UA = {"User-Agent": "nekaise-corpus/find_books"}
LICENSE_CACHE = HERE / "workspace" / "oapen-license-cache.json"
SEARCH_RETRY_DELAYS = (2.0, 4.0, 8.0)

# (OAPEN subject term -> our corpus topic). Broad built-environment coverage; pagination goes deep.
QUERIES = [
    ("architecture design", "architecture"), ("architectural history", "architecture"),
    ("architectural theory", "architecture"), ("landscape architecture", "architecture"),
    ("heritage conservation", "architecture"), ("historic preservation", "architecture"),
    ("building acoustics", "architecture"), ("architectural acoustics", "architecture"),
    ("lighting design", "building_energy"), ("facade design", "architecture"),
    ("building envelope", "architecture"), ("fire safety engineering", "architecture"),
    ("housing design", "architecture"), ("interior architecture", "architecture"),
    ("building construction", "construction"), ("construction management", "construction"),
    ("construction technology", "construction"), ("building information modeling", "construction"),
    ("prefabrication building", "construction"), ("digital fabrication architecture", "construction"),
    ("construction robotics", "construction"), ("circular economy construction", "construction"),
    ("building materials", "materials"), ("concrete technology", "materials"),
    ("reinforced concrete", "structures_civil"), ("prestressed concrete", "structures_civil"),
    ("timber engineering", "materials"), ("mass timber", "materials"),
    ("masonry construction", "materials"), ("composite materials structures", "structures_civil"),
    ("steel structures", "structures_civil"), ("structural engineering", "structures_civil"),
    ("structural mechanics", "structures_civil"), ("structural dynamics", "structures_civil"),
    ("finite element analysis", "structures_civil"), ("structural health monitoring", "structures_civil"),
    ("earthquake engineering", "structures_civil"), ("seismic design", "structures_civil"),
    ("wind engineering", "structures_civil"), ("bridge engineering", "structures_civil"),
    ("bridge maintenance", "structures_civil"), ("tensile membrane structures", "structures_civil"),
    ("geotechnical engineering", "structures_civil"), ("soil mechanics", "structures_civil"),
    ("rock mechanics", "structures_civil"), ("foundation engineering", "structures_civil"),
    ("tunnelling", "structures_civil"), ("ground improvement", "structures_civil"),
    ("slope stability", "structures_civil"), ("landslides", "structures_civil"),
    ("soil dynamics", "structures_civil"), ("offshore engineering", "structures_civil"),
    ("building physics", "building_energy"), ("energy efficient buildings", "building_energy"),
    ("sustainable architecture", "building_energy"), ("green building", "building_energy"),
    ("daylighting", "building_energy"), ("thermal comfort", "building_energy"),
    ("ventilation indoor air", "building_energy"), ("passive house", "building_energy"),
    ("building retrofit", "building_energy"), ("zero energy building", "building_energy"),
    ("solar energy buildings", "building_energy"), ("renewable energy systems", "building_energy"),
    ("building services engineering", "equipment_systems"), ("heating cooling systems", "equipment_systems"),
    ("refrigeration", "equipment_systems"), ("thermal energy storage", "equipment_systems"),
    ("district heating", "building_energy"), ("heat pump", "equipment_systems"),
    ("urban planning", "urban"), ("urban design", "urban"), ("smart cities", "urban"),
    ("urban morphology", "urban"), ("urban climate", "urban"), ("urban heat island", "urban"),
    ("climate adaptation cities", "urban"), ("regional planning", "urban"),
    ("transport planning", "infrastructure"), ("traffic engineering", "infrastructure"),
    ("sustainable mobility", "infrastructure"), ("railway engineering", "infrastructure"),
    ("pavement engineering", "infrastructure"), ("asphalt materials", "infrastructure"),
    ("highway engineering", "infrastructure"), ("hydraulic engineering", "infrastructure"),
    ("water resources engineering", "infrastructure"), ("hydrology", "infrastructure"),
    ("coastal engineering", "infrastructure"), ("flood risk management", "infrastructure"),
    ("stormwater management", "infrastructure"), ("wastewater treatment", "infrastructure"),
    ("dam engineering", "infrastructure"), ("infrastructure resilience", "infrastructure"),
    ("disaster risk reduction", "infrastructure"), ("geodesy surveying", "infrastructure"),
    ("remote sensing", "infrastructure"), ("geographic information systems", "infrastructure"),
    ("life cycle assessment building", "standards_protocols"), ("embodied carbon", "standards_protocols"),
    # native-language queries (all-language corpus since 2026-07-09) — OAPEN subject search is
    # language-literal, so non-English books need non-English terms
    ("Gebäude", "building_energy"), ("Bauwesen", "construction"), ("Architektur", "architecture"),
    ("Stadtplanung", "urban"), ("Baugeschichte", "architecture"), ("Denkmalpflege", "architecture"),
    ("architettura", "architecture"), ("edilizia", "construction"), ("urbanistica", "urban"),
    ("architecture urbaine", "urban"), ("patrimoine architectural", "architecture"),
    ("arquitectura", "architecture"), ("urbanismo", "urban"), ("construcción", "construction"),
    ("arquitetura", "architecture"), ("stedenbouw", "urban"), ("architectuur", "architecture"),
]

# Cursor coordinates use fixed-width rows so appending queries does not remap already-committed
# excavation state.  Keep existing queries in place and append new ones; widening beyond 128 terms
# requires a reviewed cursor migration.
CURSOR_STRIDE = 128


def cursor_target(cursor: int, per: int) -> tuple[str, str, int] | None:
    """Map a stable cursor to one query and page; spare slots intentionally return no work."""
    if len(QUERIES) > CURSOR_STRIDE:
        raise RuntimeError(
            f"{len(QUERIES)} OAPEN queries exceed the {CURSOR_STRIDE}-slot cursor; "
            "perform a reviewed cursor migration"
        )
    if cursor < 0:
        raise ValueError("cursor must be non-negative")
    if per <= 0:
        raise ValueError("per must be positive")
    page, query_index = divmod(cursor, CURSOR_STRIDE)
    if query_index >= len(QUERIES):
        return None
    term, topic = QUERIES[query_index]
    return term, topic, page * per


def pdf_link(item) -> str | None:
    for b in item.get("bitstreams") or []:
        name = (b.get("name") or "").lower()
        if b.get("mimeType") == "application/pdf" and name.endswith(".pdf") and b.get("retrieveLink"):
            return "https://library.oapen.org" + b["retrieveLink"]
    return None


def is_book(item) -> bool:
    # ALL LANGUAGES kept since 2026-07-09 (the quality gate's DOMAIN vocabulary is multilingual);
    # only the document TYPE is filtered here.
    md = item.get("metadata") or []
    typ = " ".join(m["value"] for m in md if m.get("key") == "dc.type").lower()
    return "book" in typ


def license_of(handle: str) -> str | None:
    """Resolve an allowed license; incompatible rights return None, request failures propagate."""
    r = requests.get(OAI, params={"verb": "GetRecord", "metadataPrefix": "xoai",
                                  "identifier": f"oai:library.oapen.org:{handle}"},
                     headers=UA, timeout=30)
    r.raise_for_status()
    t = r.text
    if re.search(r"creativecommons\.org/publicdomain/zero", t):
        return "cc0"
    codes = set(re.findall(r"creativecommons\.org/licenses/([a-z-]+)", t))
    if not codes or any(("nc" in c or "nd" in c) for c in codes):
        return None
    if "by-sa" in codes:
        return "cc-by-sa"
    if "by" in codes:
        return "cc-by"
    return None


def load_license_cache() -> dict[str, str | None]:
    try:
        return {
            handle: license_name
            for handle, license_name in json.loads(LICENSE_CACHE.read_text()).items()
            if license_name
        }
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_license_cache(cache: dict[str, str | None]) -> None:
    LICENSE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    ops.atomic_write_text(
        LICENSE_CACHE,
        json.dumps(cache, ensure_ascii=False, sort_keys=True) + "\n",
    )


def search_subject(
    term: str,
    topic: str,
    offset: int,
    depth: int,
    per: int,
) -> tuple[list[tuple], str | None]:
    """Fetch one subject's pages, returning any request failure separately from a dry page."""
    found = []
    for page_offset in range(offset, offset + depth, per):
        # OAPEN deep-offset pages (expand=bitstreams,metadata @1600+) measured 26-45s in
        # 2026-08 and occasionally hang past any timeout. Transient 5xx responses also caused
        # repeated whole-round failures, so retry those boundedly while permanent failures still
        # fail immediately. Partial appends remain refused if every attempt is exhausted.
        items = None
        attempts = len(SEARCH_RETRY_DELAYS) + 1
        for attempt in range(1, attempts + 1):
            try:
                r = requests.get(
                    SEARCH,
                    params={
                        "query": term,
                        "expand": "bitstreams,metadata",
                        "limit": per,
                        "offset": page_offset,
                    },
                    headers=UA,
                    timeout=120,
                )
                r.raise_for_status()
                items = r.json()
                break
            except Exception as exc:
                response = getattr(exc, "response", None)
                status = getattr(response, "status_code", None)
                retryable = (
                    isinstance(exc, (requests.Timeout, requests.ConnectionError))
                    or (
                        isinstance(exc, requests.HTTPError)
                        and status is not None
                        and 500 <= status < 600
                    )
                )
                if not retryable or attempt == attempts:
                    print(
                        f"# search '{term}' @{page_offset} failed after {attempt} "
                        f"attempt{'s' if attempt != 1 else ''}: {exc}",
                        file=sys.stderr,
                    )
                    return found, f"'{term}' @{page_offset}"
                time.sleep(SEARCH_RETRY_DELAYS[attempt - 1])
        if not items:
            break
        found.extend((term, topic, item) for item in items)
    return found, None


def emit(e) -> str:
    e = {k: e[k] for k in ("id", "title", "url", "source", "license", "topic", "format")}
    d = yaml.safe_dump([e], sort_keys=False, allow_unicode=True)
    return "".join(("  " + ln + "\n") if ln else "\n" for ln in d.splitlines())


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--per", type=int, default=25, help="OAPEN page size")
    position = ap.add_mutually_exclusive_group()
    position.add_argument(
        "--offset",
        type=int,
        help="starting offset for a manual all-query scan (default 0)",
    )
    position.add_argument(
        "--cursor",
        type=int,
        help="stable automated cursor selecting exactly one query/page pair",
    )
    ap.add_argument("--depth", type=int, default=25, help="results scanned per subject from --offset")
    ap.add_argument("--max", type=int, default=200, help="cap new books appended per run")
    ap.add_argument(
        "--workers",
        type=int,
        default=4,
        help="bounded concurrent OAPEN subjects/license lookups (default 4)",
    )
    ap.add_argument("--append", action="store_true")
    args = ap.parse_args()

    urls, titles, reg_ids = registry.existing_keys()
    workers = max(1, args.workers)
    if args.cursor is None:
        offset = args.offset or 0
        searches = [
            (index, term, topic, offset, args.depth)
            for index, (term, topic) in enumerate(QUERIES)
        ]
    else:
        target = cursor_target(args.cursor, args.per)
        searches = [] if target is None else [(0, *target, args.per)]

    pages_by_query: list[list[tuple] | None] = [None] * len(searches)
    failed_searches: list[str] = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                search_subject,
                term,
                topic,
                offset,
                depth,
                args.per,
            ): index
            for index, term, topic, offset, depth in searches
        }
        for future in as_completed(futures):
            pages, failure = future.result()
            pages_by_query[futures[future]] = pages
            if failure:
                failed_searches.append(failure)

    if failed_searches:
        examples = ", ".join(sorted(failed_searches)[:3])
        more = len(failed_searches) - min(3, len(failed_searches))
        suffix = f" (+{more} more)" if more else ""
        print(
            f"# ERROR: OAPEN discovery incomplete at {examples}{suffix}; "
            "refusing a partial append so rotation does not advance",
            file=sys.stderr,
        )
        raise SystemExit(1)

    candidates, seen = [], set()
    for pages in pages_by_query:
        for _term, topic, item in pages or []:
            title, url, handle = item.get("name"), pdf_link(item), item.get("handle")
            if not (title and url and handle) or not is_book(item):
                continue
            u, t = url.rstrip("/"), registry.norm(title)
            if u in urls or t in titles or u in seen:
                continue
            seen.add(u)
            candidates.append((title, url, handle, topic))

    cache = load_license_cache()
    out = []
    # Work in small deterministic waves: bounded concurrency without resolving hundreds of
    # licenses after --max has already been reached.
    wave_size = workers * 4
    for start in range(0, len(candidates), wave_size):
        if len(out) >= args.max:
            break
        wave = candidates[start:start + wave_size]
        resolved: list[str | None] = [None] * len(wave)
        uncached = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for index, (_title, _url, handle, _topic) in enumerate(wave):
                if handle in cache:
                    resolved[index] = cache[handle]
                else:
                    uncached[pool.submit(license_of, handle)] = (index, handle)
            for future in as_completed(uncached):
                index, handle = uncached[future]
                resolved[index] = future.result()
                # Do not cache None: it can mean either incompatible rights or a transient request
                # failure, and fail-closed must not turn an outage into a permanent exclusion.
                if resolved[index]:
                    cache[handle] = resolved[index]
        save_license_cache(cache)
        for (title, url, _handle, topic), lic in zip(wave, resolved):
            if not lic or len(out) >= args.max:
                continue
            out.append({
                "id": f"oer-{registry.slug(title)[:52]}",
                "title": title.strip()[:150],
                "url": url,
                "source": "oapen",
                "license": lic,
                "topic": topic,
                "format": "pdf",
            })

    registry.uniquify_ids(out, reg_ids)

    by_lic: dict = {}
    for h in out:
        by_lic[h["license"]] = by_lic.get(h["license"], 0) + 1
    print(f"# {len(out)} NEW OAPEN books (all languages, CC-BY/-SA/0, deduped vs manifest + registry)")
    print(f"# by license: {by_lic}")
    print("# --- review, then --append (inserts ABOVE the discovered marker), then build_corpus.py ---")
    print(yaml.safe_dump(out, sort_keys=False, allow_unicode=True))

    if args.append and out:
        counts = registry.append_entries(out)
        print(f"# appended {len(out)} book entries to the registry: {counts}", file=sys.stderr)


if __name__ == "__main__":
    main()
