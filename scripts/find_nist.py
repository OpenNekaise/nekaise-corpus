#!/usr/bin/env python3
"""Enumerate NIST/NBS Technical Series publications via the Crossref API.

NIST (and its predecessor, the National Bureau of Standards) mints its Technical Series DOIs
under prefix 10.6028. The prefix includes public-domain US-government building science, fire,
structures, materials, and building-systems reports, but also a large amount of non-AEC work.

The old finder issued relevance-ranked bibliographic queries and eventually walked into dry
deep-result tails. This version cursor-enumerates the prefix exactly once in re-derivable
creation-month segments. Crossref cursor tokens live only for this process; committed rotation
state is the numeric month index ``year * 12 + month - 1``. A retry therefore starts the small
month again, and registry/blocklist dedup makes that harmless.

Only direct nvlpubs.nist.gov PDFs whose titles pass the conservative AEC title gate are proposed.
The normal loader and quality gate remain downstream safeguards.

    python scripts/find_nist.py --month-index 24143             # 2011-12, propose
    python scripts/find_nist.py --month-index 24143 --append    # append after review
"""
from __future__ import annotations

import argparse
import calendar
import os
import re
import sys
import time

import requests
import yaml

import registry

API = "https://api.crossref.org/works"
MAILTO = "nekaise-corpus@example.org"
PREFIX = "10.6028"
FIRST_MONTH_INDEX = 2011 * 12 + 11  # Earliest prefix record is from 2011-12.

# Require an unambiguous building-systems concept (or meaningful conjunction) before spending a
# download slot. This is intentionally the reviewed wave-3 gate, now applied to every record in
# the prefix instead of trusting Crossref's fuzzy relevance ranking.
TITLE_ANCHOR = re.compile(
    r"\bcommissioning\b|\bhvac\b|\bbacnet\b|heat[ -]?pump|"
    r"(?=.*(?:fault|diagnos))(?=.*(?:\bhvac\b|air[ -]?handl|build))|"
    r"(?=.*ventilat)(?=.*(?:build|house|attic|indoor|smoke|fire|pressure|natural|manufactur))|"
    r"(?=.*(?:code|standard))(?=.*(?:build|seismic|structur|fire|energy))|"
    r"(?=.*build)(?=.*(?:automation|control|interoperab|sensor|grid[ -]?interactive|cyber))",
    re.I,
)


def title_in_scope(title: str) -> bool:
    return bool(TITLE_ANCHOR.search(title or ""))


def classify_topic(title: str) -> str | None:
    """Return a conservative topic for an accepted title, else ``None``."""
    if not title_in_scope(title):
        return None
    lowered = title.lower()
    if "commissioning" in lowered or re.search(r"fault|diagnos", lowered):
        return "commissioning_fdd"
    if re.search(r"\bbacnet\b|\bcodes?\b|standard", lowered):
        return "standards_protocols"
    if re.search(r"smoke|fire", lowered):
        return "building_energy"
    if "build" in lowered and re.search(
        r"automation|control|interoperab|sensor|grid[ -]?interactive|cyber", lowered
    ):
        return "controls_bas"
    if re.search(r"\bhvac\b|heat[ -]?pump|air[ -]?handl", lowered):
        return "equipment_systems"
    return "building_energy"


def month_bounds(month_index: int) -> tuple[str, str]:
    """Translate the stable numeric rotation watermark to inclusive ISO date bounds."""
    if month_index < FIRST_MONTH_INDEX:
        raise ValueError(
            f"month index {month_index} predates the first NIST Crossref record "
            f"({FIRST_MONTH_INDEX})"
        )
    year, zero_based_month = divmod(month_index, 12)
    month = zero_based_month + 1
    last_day = calendar.monthrange(year, month)[1]
    return f"{year:04d}-{month:02d}-01", f"{year:04d}-{month:02d}-{last_day:02d}"


def from_crossref(month_index: int, rows: int) -> list[tuple[str, str, str]]:
    """Cursor-enumerate one creation month and return title-gated direct PDFs.

    The cursor is deliberately never persisted. Missing/repeated cursors and short pages fail
    closed so the runner retains the numeric month watermark and retries the whole segment.
    """
    if not 1 <= rows <= 1000:
        raise ValueError("rows must be between 1 and Crossref's 1000-record maximum")
    start, end = month_bounds(month_index)
    cursor = "*"
    seen_cursors: set[str] = set()
    fetched = 0
    total: int | None = None
    out: list[tuple[str, str, str]] = []

    while total is None or fetched < total:
        response = requests.get(
            API,
            params={
                "filter": (
                    f"prefix:{PREFIX},from-created-date:{start},until-created-date:{end}"
                ),
                "rows": rows,
                "cursor": cursor,
                "mailto": MAILTO,
            },
            timeout=45,
        )
        response.raise_for_status()
        message = response.json().get("message", {})
        items = message.get("items")
        if not isinstance(items, list):
            raise RuntimeError("Crossref response has no items list")
        reported_total = message.get("total-results")
        if not isinstance(reported_total, int) or reported_total < 0:
            raise RuntimeError("Crossref response has no valid total-results")
        if total is None:
            total = reported_total
        elif reported_total != total:
            raise RuntimeError(
                f"Crossref total-results changed during cursor walk: {total} -> {reported_total}"
            )
        if not items and fetched < total:
            raise RuntimeError(f"Crossref cursor ended early after {fetched} of {total} records")

        fetched += len(items)
        if fetched > total:
            raise RuntimeError(f"Crossref returned {fetched} records but reported only {total}")
        for item in items:
            titles = item.get("title") or []
            title = titles[0] if titles else None
            url = ((item.get("resource") or {}).get("primary") or {}).get("URL")
            if not title or not url:
                continue
            if "nvlpubs.nist.gov" not in url.lower() or not url.lower().endswith(".pdf"):
                continue
            topic = classify_topic(title)
            if topic:
                out.append((title.strip(), url, topic))

        if fetched >= total:
            break
        next_cursor = message.get("next-cursor")
        if not isinstance(next_cursor, str) or not next_cursor:
            raise RuntimeError(f"Crossref omitted next-cursor after {fetched} of {total} records")
        if next_cursor == cursor or next_cursor in seen_cursors:
            raise RuntimeError(f"Crossref repeated a cursor after {fetched} of {total} records")
        seen_cursors.add(cursor)
        cursor = next_cursor
        time.sleep(0.5)

    return out


def request_rotation_hold(reason: str) -> None:
    if hold_name := os.environ.get("NEKAISE_ROTATION_HOLD_FILE"):
        with open(hold_name, "w", encoding="utf-8") as handle:
            handle.write(reason + "\n")
    print(f"# rotation hold requested: {reason}", file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--rows", type=int, default=1000, help="records per cursor request (max 1000)")
    ap.add_argument(
        "--month-index",
        type=int,
        default=FIRST_MONTH_INDEX,
        help="creation month watermark encoded as year * 12 + month - 1",
    )
    ap.add_argument("--max", type=int, default=300, help="cap on new entries this run")
    ap.add_argument("--append", action="store_true", help="append into the registry")
    args = ap.parse_args()
    if args.max < 1:
        ap.error("--max must be positive")

    start, _end = month_bounds(args.month_index)
    try:
        hits = from_crossref(args.month_index, args.rows)
    except Exception as exc:
        print(
            f"# ERROR: NIST discovery incomplete for {start[:7]}: {exc}; "
            "refusing a partial append so rotation does not advance",
            file=sys.stderr,
        )
        raise SystemExit(1)

    urls, titles, reg_ids = registry.existing_keys()
    candidates, seen = [], set()
    for title, url, topic in hits:
        normalized_url = url.rstrip("/")
        normalized_title = registry.norm(title)
        if normalized_url in urls or normalized_title in titles or normalized_url in seen:
            continue
        seen.add(normalized_url)
        titles.add(normalized_title)
        candidates.append(
            {
                "id": f"nst-{registry.slug(title)[:52]}",
                "title": title[:150],
                "url": url,
                "source": "nist_crossref",
                "license": "public-domain",
                "topic": topic,
                "format": "pdf",
            }
        )

    overflow = len(candidates) > args.max
    out = candidates[: args.max]
    if overflow:
        request_rotation_hold(
            f"{start[:7]} has {len(candidates)} new candidates; emitted {len(out)}"
        )
    registry.uniquify_ids(out, reg_ids)

    by_topic: dict[str, int] = {}
    for hit in out:
        by_topic[hit["topic"]] = by_topic.get(hit["topic"], 0) + 1
    print(
        f"# {len(out)} NEW NIST/NBS technical series PDFs "
        f"({start[:7]}; cursor-enumerated {len(hits)} title-gated PDFs; "
        "public-domain, deduped vs manifest + registry + blocklist)"
    )
    print(f"# by topic: {by_topic}")
    print("# --- review, then --append, then scripts/build_corpus.py ---")
    print(yaml.safe_dump(out, sort_keys=False, allow_unicode=True))

    if args.append and out:
        counts = registry.append_entries(out)
        print(f"# appended {len(out)} entries to the registry: {counts}", file=sys.stderr)


if __name__ == "__main__":
    main()
