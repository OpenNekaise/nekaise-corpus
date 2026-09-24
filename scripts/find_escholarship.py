#!/usr/bin/env python3
"""find_escholarship.py — LBNL / UC Berkeley building research from eScholarship (GraphQL API).

eScholarship (the University of California's open repository) holds Lawrence Berkeley National
Laboratory's publication record and UC Berkeley's Center for the Built Environment — core
building-energy-simulation, windows/daylighting, HVAC, indoor-environment and grid-interactive
buildings research. The public GraphQL endpoint (https://escholarship.org/graphql) pages a unit's
items with an opaque `more` cursor; each item carries `rights` (a Creative Commons URL or null),
`contentLink` (the direct PDF) and subject/keyword metadata.

Units, walked in this order (verified 2026-09-24; item totals then):
    cedr_cbe       Center for the Built Environment (UC Berkeley)       636  dedicated: loose gate
    lbnl_et_btus   LBNL Energy Technologies / Bldg Technology Urban Sys 1,790  dedicated: loose gate
    lbnl_et        LBNL Energy Technologies Area                        6,056  strict gate
    lbnl_rw        LBL Publications (all of LBNL)                      88,761  strict gate
Order is ADDED_ASC so a walked position is stable and newly deposited items appear at the tail.

Rights gate (FAIL-CLOSED, matching find_kitopen/find_zenodo/find_scielo): keep only CC BY,
CC BY-SA, CC0 or the Public Domain Mark. NC/ND variants and items with no `rights` at all (most
pre-2016 LBNL reports) are skipped. Only items with an escholarship.org `contentLink` PDF are
proposed. Relevance: scripts/bes_relevance.py (loose for the two dedicated building units,
strict title-only for the lab-wide units); measured 2026-09-24 at 139/166 CC-BY items kept
in lbnl_et_btus and 22/226 CC-BY PDFs kept in a recent lbnl_rw sample, all building-science.

Access: robots.txt allows everything but /search with `Crawl-delay: 4`; this finder waits >= 4 s
between API requests and build_corpus.HOST_DELAY spaces PDF fetches the same way. CloudFront
403s self-identified bot UAs and answers bare clients with an HTTP 202 challenge; only a
browser-impersonating UA got through (2026-09-24). POLICY (Codex, 2026-09-24): browser
impersonation where an identified bot is refused is WAF avoidance, so this finder uses an honest
UA only and ships DISABLED in registry/backends.json. Re-enable only if an honest identity or an
explicitly permitted API/download route works. A 202/403/429/503 is treated as a refusal:
rotation HOLD, nothing proposed, no identity cycling.

Rotation pointer (dynamic, `--cursor`): "<unit>:<more-token>" or "<unit>:START". When a unit
ends the walk moves to the next unit; at the end of the last unit the pointer stays on the final
page's token, so later rounds cheaply re-probe that page for newly deposited items.

    python scripts/find_escholarship.py --cursor cedr_cbe:START --pages 2 --max 20
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
import yaml

import bes_relevance
import dedup
import licenses
import registry

API = "https://escholarship.org/graphql"
# Honest identity only (policy above); never a browser UA, never a different UA after a refusal.
UA = "nekaise-corpus/find_escholarship"
CRAWL_DELAY = 4.0  # robots.txt Crawl-delay
PAGE_SIZE = 100
UNITS: tuple[tuple[str, bool], ...] = (  # (unit id, strict relevance gate)
    ("cedr_cbe", False),
    ("lbnl_et_btus", False),
    ("lbnl_et", True),
    ("lbnl_rw", True),
)
START = "START"
QUERY = """query($id: ID!, $first: Int!, $more: String) {
  unit(id: $id) {
    items(first: $first, order: ADDED_ASC, more: $more) {
      total
      more
      nodes { id title rights contentLink contentType status keywords subjects }
    }
  }
}"""

LICENSE_RULES = (
    (re.compile(r"creativecommons\.org/publicdomain/zero/", re.I), "cc0"),
    (re.compile(r"creativecommons\.org/publicdomain/mark/", re.I), "public-domain"),
    (re.compile(r"creativecommons\.org/licenses/by-sa/", re.I), "cc-by-sa"),
    (re.compile(r"creativecommons\.org/licenses/by/", re.I), "cc-by"),
)


class Challenge(RuntimeError):
    """The CDN answered with a challenge / throttle instead of API data."""


def license_for(rights: str | None) -> str | None:
    """Redistributable tag for an item's rights URL, else None (NC/ND/unknown/missing). Strict:
    only a canonical creativecommons.org URL counts (licenses.cc_license, shared with find_ojs);
    lookalike hosts and negated or prose statements fail closed."""
    tag, _ = licenses.cc_license([rights]) if rights else (None, None)
    return tag


def pdf_link(node: dict) -> str | None:
    link = node.get("contentLink") or ""
    parsed = urlparse(link)
    if parsed.scheme != "https" or parsed.netloc != "escholarship.org":
        return None
    if not parsed.path.lower().endswith(".pdf"):
        return None
    if node.get("contentType") not in (None, "application/pdf"):
        return None
    return link


def parse_cursor(cursor: str) -> tuple[int, str | None]:
    """"<unit>:<token>" -> (unit index, token or None for the unit's first page)."""
    unit, sep, token = cursor.partition(":")
    names = [name for name, _ in UNITS]
    if not sep or unit not in names or not token:
        raise ValueError(f"cursor must be '<unit>:<token|START>' with unit in {names}: {cursor!r}")
    return names.index(unit), (None if token == START else token)


def format_cursor(index: int, token: str | None) -> str:
    return f"{UNITS[index][0]}:{token or START}"


def fetch_page(unit: str, token: str | None) -> tuple[list[dict], str | None]:
    """One API page -> (nodes, next token or None when the unit is exhausted)."""
    response = requests.post(
        API,
        json={"query": QUERY, "variables": {"id": unit, "first": PAGE_SIZE, "more": token}},
        headers={"User-Agent": UA, "Accept": "application/json"},
        timeout=60,
    )
    if response.status_code in (202, 403, 429, 503):
        raise Challenge(f"{unit} page answered HTTP {response.status_code}")
    response.raise_for_status()
    payload = response.json()
    if payload.get("errors"):
        raise RuntimeError(f"GraphQL errors: {payload['errors'][:1]}")
    unit_data = (payload.get("data") or {}).get("unit")
    if not unit_data:
        raise RuntimeError(f"unknown eScholarship unit {unit}")
    items = unit_data["items"]
    nodes = items.get("nodes")
    if not isinstance(nodes, list):
        raise RuntimeError("GraphQL response has no nodes list")
    return nodes, items.get("more") or None


def candidate(node: dict, strict: bool) -> dict | None:
    """A registry entry for one API node, or None when a gate rejects it."""
    title = " ".join((node.get("title") or "").split())
    tag = license_for(node.get("rights"))
    url = pdf_link(node)
    if not title or tag is None or url is None:
        return None
    if node.get("status") not in (None, "PUBLISHED"):
        return None
    if not bes_relevance.relevant(title, node.get("keywords"), node.get("subjects"),
                                  strict=strict):
        return None
    entry = {
        "id": f"esc-{registry.slug(title)[:52]}", "title": title[:150], "url": url,
        "source": "escholarship", "license": tag, "topic": bes_relevance.topic_for(title),
        "format": "pdf", "license_url": node["rights"],
        "license_evidence": "eScholarship item rights field",
    }
    if node.get("id"):
        entry["persistent_id"] = node["id"]
    return entry


def request_rotation_hold(reason: str) -> None:
    if hold_name := os.environ.get("NEKAISE_ROTATION_HOLD_FILE"):
        Path(hold_name).write_text(reason + "\n", encoding="utf-8")
    print(f"# rotation hold requested: {reason}", file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cursor", default=f"{UNITS[0][0]}:{START}",
                    help="'<unit>:<more-token>' or '<unit>:START' (rotation pointer)")
    ap.add_argument("--pages", type=int, default=10, help="max API pages (100 items) this run")
    ap.add_argument("--max", type=int, default=40,
                    help="page-granular target for new entries (the last page may exceed it)")
    ap.add_argument("--append", action="store_true",
                    help="append into the registry (registry/escholarship.yaml)")
    args = ap.parse_args()
    if args.pages < 1 or args.max < 1:
        ap.error("--pages and --max must be positive")
    try:
        index, token = parse_cursor(args.cursor)
    except ValueError as exc:
        ap.error(str(exc))

    keys = dedup.open_keys()
    urls, titles = keys.urls, keys.titles
    out: list[dict] = []
    scanned = pages = 0
    frontier = False
    while pages < args.pages and len(out) < args.max:
        unit, strict = UNITS[index]
        if pages:
            time.sleep(CRAWL_DELAY)
        try:
            nodes, next_token = fetch_page(unit, token)
        except Challenge as exc:
            request_rotation_hold(f"{exc}; nothing proposed, retrying this cursor next round")
            print("# 0 NEW eScholarship items (CDN challenge)")
            return
        except Exception as exc:
            print(f"# ERROR: eScholarship page failed at {format_cursor(index, token)}: {exc}; "
                  "refusing a partial append so rotation does not advance", file=sys.stderr)
            raise SystemExit(1)
        pages += 1
        page = [candidate(node, strict) for node in nodes]
        keys.prefetch(**dedup.page_keys(entry for entry in page if entry))
        for entry in page:
            scanned += 1
            if entry is None:
                continue
            u, t = entry["url"].rstrip("/"), registry.norm(entry["title"])
            if u in urls or t in titles:
                continue
            urls.add(u)
            titles.add(t)
            out.append(entry)
        if next_token:
            token = next_token
        elif index + 1 < len(UNITS):
            index, token = index + 1, None
        else:
            frontier = True  # re-probe this last page for new deposits in later rounds
            break

    keys.uniquify_ids(out)
    next_cursor = format_cursor(index, token)
    by_license: dict[str, int] = {}
    for entry in out:
        by_license[entry["license"]] = by_license.get(entry["license"], 0) + 1
    print(f"# {len(out)} NEW eScholarship items (kept {len(out)}/{scanned} scanned over {pages} "
          f"pages; CC BY/BY-SA/CC0/PD building research PDFs, deduped vs manifest + registry + "
          f"blocklist)")
    print(f"# by license: {by_license}")
    if frontier:
        print("# reached the tail of the last unit; the cursor re-probes it for new deposits")
    print("# --- review, then --append, then scripts/build_corpus.py ---")
    print(yaml.safe_dump(out, sort_keys=False, allow_unicode=True))

    if args.append and out:
        counts = registry.append_entries(out)
        print(f"# appended {len(out)} entries to the registry: {counts}", file=sys.stderr)
    if next_name := os.environ.get("NEKAISE_ROTATION_NEXT_FILE"):
        Path(next_name).write_text(next_cursor + "\n", encoding="utf-8")
    print(f"# next-cursor: {next_cursor}")


if __name__ == "__main__":
    main()
