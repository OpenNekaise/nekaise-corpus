#!/usr/bin/env python3
"""find_ibpsa.py — enumerate IBPSA Building Simulation conference proceedings.

publications.ibpsa.org hosts every IBPSA-family conference's proceedings as free PDFs — WAS
captcha-walled, now open (verified 2026-07: direct PDF fetch returns 200 with real %PDF bytes).
Building simulation papers are our CORE domain, and this single WordPress site holds thousands
of them across a dozen conference series going back to 1985.

Site shape (probed 2026-07): each conference+year has ONE listing page,
    https://publications.ibpsa.org/conference/?id={conf}{year}
that renders every accepted paper as `<a class="paper_title" href=".../conference/paper/?id=...">`
(the landing page) with a sibling `<a href=".../proceedings/{conf}/{year}/papers/....pdf">` (the
direct PDF) in the same `<tr>`. Some conferences (bs) additionally group papers under topic `<h4>`
headings (e.g. "Commissioning and control", "HVAC") interleaved with the paper `<table>`s in
document order; others (esim, simbuild, ...) put everything in one flat table with no headings.
Either way this backend walks the listing page's direct children in order, tracks the current
heading, and tags a paper `controls_bas` if the heading looks controls-flavored, else
`building_energy`.

The plain `requests` default User-Agent (`python-requests/...`) is 403'd by the site's WAF; ANY
other UA string (browser or a plain descriptive bot UA) is let through — see UA below. robots.txt
allows `/conference/` and `/proceedings/` for `User-agent: *`.

Known conference codes (id prefix used in the `?id=` query) and roughly which years have a
listing page, current as of 2026-07 — rotate `--year` across rounds per `--conf`:
    bs        Building Simulation (IBPSA world congress, biennial): 1985,1989,1991,1993,1995,1997,
              1999,2001,2003,...,2023,2025 (odd years from 1993 on; 1985/1989/1991 also exist)
    bsa       Building Simulation Applications (US regional): 2013,2015,2017,2019,2022,2024
    esim      eSim (Canada): 2001,2002,2004,2006,...,2024 (even years from 2002 on)
    simbuild  SimBuild (US, ASHRAE/IBPSA-USA): 2004,2006,...,2024,2026
    bso       Building Simulation and Optimization (UK): 2012,2014,2016,2018,2020,2022
    asim      ASim (Asia): 2012,2014,2016,2024
    bausim    BauSim (Germany/Austria/Switzerland): 2006,2008,...,2024
    usim      uSim (urban-scale sim): 2018,2020,2022,2024
    bscairo   Building Simulation Cairo — hosted OFF-SITE on iopscience.iop.org; no local listing
              page, skip.
An invalid `--conf`/`--year` combo 200s with zero `paper_title` anchors (no error) — this backend
just reports 0 new entries for it; the module docstring's year lists above are a rotation guide,
not gospel (re-probe `https://publications.ibpsa.org/{conf}-conference-proceedings/` if a round
comes up empty).

    python scripts/find_ibpsa.py --conf bs --year 2019            # propose
    python scripts/find_ibpsa.py --conf esim --year 2018 --max 200 --append
"""
from __future__ import annotations

import argparse
import re
import sys

import requests
import yaml
from bs4 import BeautifulSoup

import registry

BASE = "https://publications.ibpsa.org"
UA = {"User-Agent": "nekaise-corpus/find_ibpsa"}
CONTROLS_RE = re.compile(r"control|commissioning|automation|\bbas\b|fault detection|\bfdd\b", re.I)


def fetch_papers(conf: str, year: int) -> list[dict]:
    """One conference+year listing page -> [{title, paper_url, pdf_url, topic}, ...] in doc order."""
    url = f"{BASE}/conference/?id={conf}{year}"
    r = requests.get(url, headers=UA, timeout=30)
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


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--conf", default="bs", help="conference code, e.g. bs esim simbuild bso asim bausim bsa usim")
    ap.add_argument("--year", type=int, default=2023, help="conference year")
    ap.add_argument("--max", type=int, default=500, help="cap on new entries this run")
    ap.add_argument("--append", action="store_true", help="append into the registry (registry/ibpsa.yaml)")
    args = ap.parse_args()

    urls, titles, reg_ids = registry.existing_keys()
    try:
        papers = fetch_papers(args.conf, args.year)
    except Exception as e:
        print(f"# {args.conf}{args.year} fetch failed: {e}", file=sys.stderr)
        papers = []

    out = []
    for p in papers:
        if len(out) >= args.max:
            break
        pdf_url = p["pdf_url"]
        if not pdf_url:
            continue
        if pdf_url.startswith("/"):
            pdf_url = BASE + pdf_url
        u, t = pdf_url.rstrip("/"), registry.norm(p["title"])
        if u in urls or t in titles:
            continue
        urls.add(u)
        titles.add(t)
        sid = f"ibp-{registry.slug(p['title'])[:52]}"
        out.append({"id": sid, "title": p["title"].strip()[:150], "url": pdf_url,
                    "source": "ibpsa", "license": "open", "topic": p["topic"], "format": "pdf"})

    registry.uniquify_ids(out, reg_ids)
    by_topic: dict = {}
    for h in out:
        by_topic[h["topic"]] = by_topic.get(h["topic"], 0) + 1
    print(f"# {len(out)} NEW papers from {args.conf}{args.year} "
          f"({len(papers)} total on the listing page, deduped vs manifest + registry)")
    print(f"# by topic: {by_topic}")
    print("# --- review, then --append, then scripts/build_corpus.py ---")
    print(yaml.safe_dump(out, sort_keys=False, allow_unicode=True))

    if args.append and out:
        counts = registry.append_entries(out)
        print(f"# appended {len(out)} entries to the registry: {counts}", file=sys.stderr)


if __name__ == "__main__":
    main()
