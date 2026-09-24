#!/usr/bin/env python3
"""find_ibpsa.py — enumerate IBPSA-family building-simulation conference proceedings.

publications.ibpsa.org hosts every IBPSA-family conference's proceedings as free PDFs. Building
simulation papers are our CORE domain, and this single WordPress site holds thousands of them
across eight conference series going back to 1985.

Site shape (probed 2026-07, re-verified 2026-09-24): each conference+year has ONE listing page,
    https://publications.ibpsa.org/conference/?id={conf}{year}
that renders every accepted paper as `<a class="paper_title" href=".../conference/paper/?id=...">`
(the landing page) with a sibling `<a href=".../proceedings/{conf}/{year}/papers/....pdf">` (the
direct PDF) in the same `<tr>`. Some conferences (bs) additionally group papers under topic `<h4>`
headings interleaved with the paper `<table>`s in document order; others put everything in one
flat table. This backend walks the listing page's direct children in order, tracks the current
heading, and tags a paper `controls_bas` if the heading looks controls-flavored, else
`building_energy`.

Access (probed 2026-09-24): robots.txt only disallows /wp-admin/, /wp-includes/ and an icon
folder. The SiteGround WAF 403s the spoofed-Chrome UA the loader uses by default (it also 403s
some descriptive UAs with parentheses), while short honest tool UAs such as `nekaise-corpus/…`
get 200 — see UA below and build_corpus.HOST_UA. The earlier pause was a RATE-triggered
sgcaptcha: bursts of fetches get HTTP 202 + an HTML challenge page instead of the PDF (881 of
917 bs2023/bs2025 rows failed that way on 2026-08-05). Hence: exactly ONE listing request per
run (rounds are minutes apart), a bounded `--max` per round, one-at-a-time 3 s pacing plus a
per-run challenge circuit breaker in the loader (build_corpus.HOST_CONCURRENCY / HOST_DELAY /
CHALLENGE_TRIP_HOSTS), and a clean rotation HOLD (exit 0, nothing proposed) whenever the site
answers with a challenge instead of content.

Rotation walks the fixed (conf, year) UNIVERSE below by index (`--slot`), newest editions first.
The list is APPEND-ONLY: existing positions are committed rotation state, so new editions go at
the end. A listing with more new papers than `--max` requests a hold, so the same slot is
drained over several rounds before the pointer moves. When the LAST slot is drained the backend
reports itself exhausted; the runner then advances the pointer to len(UNIVERSE), the first
unvisited index, so an edition appended later is the next slot after re-enabling. A pointer past
the end only holds. bscairo is hosted off-site (iopscience) and is not part of the universe.

    python scripts/find_ibpsa.py --slot 0 --max 20            # propose from bs2025
    python scripts/find_ibpsa.py --conf esim --year 2018      # one explicit listing
"""
from __future__ import annotations

import argparse
import os
import re
import sys
import time
from pathlib import Path

import requests
import yaml
from bs4 import BeautifulSoup

import registry

BASE = "https://publications.ibpsa.org"
# Short honest tool UA: the WAF 403s python-requests' default on some paths and the spoofed
# Chrome UA everywhere (verified 2026-09-24), but lets this through.
UA = {"User-Agent": "nekaise-corpus/find_ibpsa"}
CONTROLS_RE = re.compile(r"control|commissioning|automation|\bbas\b|fault detection|\bfdd\b", re.I)
CHALLENGE_RE = re.compile(rb"sgcaptcha|captcha|challenge-platform", re.I)

# (conf, year) listings verified on the per-series proceedings index pages 2026-09-24, newest
# editions first. APPEND-ONLY: the rotation pointer is an index into this tuple.
UNIVERSE: tuple[tuple[str, int], ...] = (
    ("esim", 2026), ("simbuild", 2026),
    ("bs", 2025),
    ("simbuild", 2024), ("bsa", 2024), ("esim", 2024), ("bausim", 2024), ("usim", 2024),
    ("asim", 2024),
    ("bs", 2023),
    ("simbuild", 2022), ("bsa", 2022), ("esim", 2022), ("bso", 2022), ("bausim", 2022),
    ("usim", 2022),
    ("bs", 2021),
    ("simbuild", 2020), ("esim", 2020), ("bso", 2020), ("bausim", 2020), ("usim", 2020),
    ("bs", 2019), ("bsa", 2019),
    ("simbuild", 2018), ("esim", 2018), ("bso", 2018), ("bausim", 2018), ("usim", 2018),
    ("bs", 2017), ("bsa", 2017),
    ("simbuild", 2016), ("esim", 2016), ("bso", 2016), ("bausim", 2016), ("asim", 2016),
    ("bs", 2015), ("bsa", 2015),
    ("simbuild", 2014), ("esim", 2014), ("bso", 2014), ("bausim", 2014), ("asim", 2014),
    ("bs", 2013), ("bsa", 2013),
    ("simbuild", 2012), ("esim", 2012), ("bso", 2012), ("bausim", 2012), ("asim", 2012),
    ("bs", 2011),
    ("simbuild", 2010), ("esim", 2010), ("bausim", 2010),
    ("bs", 2009),
    ("simbuild", 2008), ("esim", 2008), ("bausim", 2008),
    ("bs", 2007),
    ("simbuild", 2006), ("esim", 2006), ("bausim", 2006),
    ("bs", 2005),
    ("simbuild", 2004), ("esim", 2004),
    ("bs", 2003),
    ("esim", 2002),
    ("bs", 2001), ("esim", 2001),
    ("bs", 1999), ("bs", 1997), ("bs", 1995), ("bs", 1993), ("bs", 1991), ("bs", 1989),
    ("bs", 1985),
)


class Challenge(RuntimeError):
    """The site answered with a captcha / WAF challenge instead of content."""


def is_challenge(response: requests.Response) -> bool:
    """Rate captcha (HTTP 202 + HTML), WAF refusal (403) or throttling (429/503)."""
    if response.status_code in (202, 403, 429, 503):
        return True
    return response.status_code == 200 and bool(CHALLENGE_RE.search(response.content[:4000])) \
        and b"paper_title" not in response.content


def fetch_papers(conf: str, year: int) -> list[dict]:
    """One conference+year listing page -> [{title, paper_url, pdf_url, topic}, ...] in doc order."""
    url = f"{BASE}/conference/?id={conf}{year}"
    r = requests.get(url, headers=UA, timeout=30)
    if is_challenge(r):
        raise Challenge(f"{conf}{year} listing answered HTTP {r.status_code} with a challenge")
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    content = soup.find("div", class_="entry-content")
    if content is None:
        return []

    papers = []
    heading = None
    for child in content.find_all(["h4", "table"], recursive=False):
        if child.name == "h4":
            text = child.get_text(strip=True)
            heading = text if text and text.lower() != "list of topics" else heading
            continue
        # child.name == "table": every paper_title anchor in this table belongs to `heading`
        topic = "controls_bas" if heading and CONTROLS_RE.search(heading) else "building_energy"
        for a in child.find_all("a", class_="paper_title"):
            title = a.get_text(strip=True)
            paper_url = a.get("href")
            tr = a.find_parent("tr")
            pdf = tr.find("a", href=lambda h: h and h.lower().endswith(".pdf")) if tr else None
            if not title or not pdf:
                continue  # no direct PDF sibling (rare) -> not fetchable, skip
            papers.append({"title": title, "paper_url": paper_url,
                           "pdf_url": pdf.get("href"), "topic": topic})
    return papers


def request_rotation_hold(reason: str) -> None:
    if hold_name := os.environ.get("NEKAISE_ROTATION_HOLD_FILE"):
        Path(hold_name).write_text(reason + "\n", encoding="utf-8")
    print(f"# rotation hold requested: {reason}", file=sys.stderr)


def report_exhausted(reason: str) -> None:
    if name := os.environ.get("NEKAISE_BACKEND_EXHAUSTED_FILE"):
        Path(name).write_text(reason + "\n", encoding="utf-8")
    print(f"# backend exhausted: {reason}", file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--slot", type=int, default=None,
                    help="rotation index into UNIVERSE (overrides --conf/--year)")
    ap.add_argument("--conf", default="bs",
                    help="conference code, e.g. bs esim simbuild bso asim bausim bsa usim")
    ap.add_argument("--year", type=int, default=2023, help="conference year")
    ap.add_argument("--max", type=int, default=60, help="cap on new entries this run")
    ap.add_argument("--append", action="store_true",
                    help="append into the registry (registry/ibpsa.yaml)")
    args = ap.parse_args()
    if args.max < 1:
        ap.error("--max must be positive")

    if args.slot is not None:
        if args.slot < 0:
            ap.error("--slot must be >= 0")
        if args.slot >= len(UNIVERSE):
            # Hold, never advance: this index is the first unvisited slot, where the next
            # appended edition will land.
            request_rotation_hold(
                f"slot {args.slot} is past the {len(UNIVERSE)}-listing universe; "
                "append new editions to UNIVERSE"
            )
            print(f"# slot {args.slot} is past the {len(UNIVERSE)}-listing universe")
            return
        conf, year = UNIVERSE[args.slot]
    else:
        conf, year = args.conf, args.year

    try:
        papers = fetch_papers(conf, year)
    except Challenge as exc:
        request_rotation_hold(f"{exc}; back off and retry the same listing next round")
        print(f"# 0 NEW papers from {conf}{year} (site challenge, nothing proposed)")
        return
    except Exception as exc:
        print(f"# ERROR: {conf}{year} listing fetch failed: {exc}; "
              "refusing a partial append so rotation does not advance", file=sys.stderr)
        raise SystemExit(1)

    urls, titles, reg_ids = registry.existing_keys()
    candidates = []
    for p in papers:
        pdf_url = p["pdf_url"]
        if pdf_url.startswith("/"):
            pdf_url = BASE + pdf_url
        u, t = pdf_url.rstrip("/"), registry.norm(p["title"])
        if u in urls or t in titles:
            continue
        urls.add(u)
        titles.add(t)
        candidates.append({
            "id": f"ibp-{registry.slug(p['title'])[:52]}", "title": p["title"].strip()[:150],
            "url": pdf_url, "source": "ibpsa", "license": "open", "topic": p["topic"],
            "format": "pdf", "document_type": "conference-paper",
        })

    out = candidates[: args.max]
    if len(candidates) > args.max:
        request_rotation_hold(
            f"{conf}{year} has {len(candidates)} new papers; emitted {len(out)}, "
            "draining the same listing next round"
        )
    elif args.slot == len(UNIVERSE) - 1:
        # The runner advances the pointer by one before disabling, which leaves it on the first
        # unvisited index (len(UNIVERSE)) - exactly where a newly appended edition goes.
        report_exhausted(f"all {len(UNIVERSE)} IBPSA (conf, year) listings walked")
    registry.uniquify_ids(out, reg_ids)
    by_topic: dict = {}
    for h in out:
        by_topic[h["topic"]] = by_topic.get(h["topic"], 0) + 1
    print(f"# {len(out)} NEW papers from {conf}{year} "
          f"({len(papers)} on the listing page, {len(candidates)} new after dedup vs "
          "manifest + registry + blocklist)")
    print(f"# by topic: {by_topic}")
    print("# --- review, then --append, then scripts/build_corpus.py ---")
    print(yaml.safe_dump(out, sort_keys=False, allow_unicode=True))

    if args.append and out:
        counts = registry.append_entries(out)
        print(f"# appended {len(out)} entries to the registry: {counts}", file=sys.stderr)


if __name__ == "__main__":
    main()
