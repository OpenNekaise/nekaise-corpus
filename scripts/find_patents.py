#!/usr/bin/env python3
"""find_patents.py — enumerate US built-environment patents from the Google Patents sitemap.

patents.google.com publishes a full crawlable sitemap (`/sitemap/`) with no API key and no
rate-limit gate: a top-level index of weekly buckets (`2026-W20.html` … back to ~1900) plus yearly
buckets for older filings (`1790.html` … `1899.html`), each either a direct list of `<li>`
publication entries or — once a bucket is large — an index of paginated sub-pages
(`2020-W01-p1.html` … `-p5.html`, ~6MB each). `robots.txt` explicitly `Allow`s both `/sitemap/`
and `/patent/`, so this is a robots-compliant, key-free way to reach the ~100 years of US patent
full text that OpenAlex/OSTI/arXiv never index: HVAC, building envelope, structural, and
construction engineering has been patented continuously since the 1800s and every patent is public
domain the moment it's granted.

Fetches one bucket (`--bucket`, e.g. "2020-W01" weekly or "1900" yearly), walks its sub-pages if
it's paginated, regex-parses each `<li>US<number><kind> - <title> :` entry (title + language
links, no HTML parser needed — these pages are ~6MB of flat `<li>` markup), and keeps only US
publications whose TITLE matches a built-environment keyword list — tuned for precision over
recall, since a patent title is the only signal available at discovery time. Topic is assigned by
the first keyword group the title matches (see TOPIC_RULES); the real relevance gate is still
`prune_corpus.py`'s DOMAIN regex over the fetched full text, and the html extractor in
build_corpus.py strips the boilerplate (classification codes, cited-by tables, chemical-compound
index) around the actual description/claims prose — always load + prune after appending.

Dedups against the registry / manifest / pruned-URL blocklist and appends `pat-` entries to
registry/patents.yaml (routed there by scripts/registry.py). Publication numbers are already
globally unique, so the id is derived straight from the pub number (e.g. `pat-us11013822b1`), not
a truncated title slug.

    python scripts/find_patents.py                                    # propose (2020-W01, page 1)
    python scripts/find_patents.py --bucket 2015-W30 --max 400 --append
"""
from __future__ import annotations

import argparse
import html
import re
import sys
import time

import requests
import yaml

import registry

SITEMAP = "https://patents.google.com/sitemap"
UA = {"User-Agent": "nekaise-corpus/find_patents"}

# one <li> line per publication: "US10520091B2 - Double direction seal with locking :"
# followed by one <a href='.../patent/<pub>/<lang>'>lang</a> per available language — we only need
# the pub number + title from the first line; the URL is built directly from the pub number.
ENTRY_RE = re.compile(r"<li>(US[0-9A-Z]+) - (.*) :\s*$")

# (title regex, topic) — first match wins. Tuned for precision: multi-word phrases before single
# words, \b-bounded single words so "pile" doesn't fire on "stockpile" / "compile", "roof" doesn't
# fire on "waterproof". Deliberately excludes generic single words like "frame" or "panel" that
# would swamp discovery with unrelated (furniture, vehicle, picture-frame) hits.
TOPIC_RULES = [
    # HVAC / refrigeration / vertical-transport machinery + wet trades
    (re.compile(r"heat pump|heating|ventilat|air condition|\bHVAC\b|refrigerat|boiler|furnace"
                r"|chiller|elevator|escalator|smoke damper|plumbing", re.I), "equipment_systems"),
    # envelope thermal performance, controls, on-building renewables
    (re.compile(r"thermostat|insulat|solar collector|photovoltaic roof", re.I), "building_energy"),
    # construction methods / temporary works / modular & site process
    (re.compile(r"formwork|scaffold|prefabricat|drywall|\bconstruction\b|\bbuilding\b", re.I),
     "construction"),
    # concrete & masonry building materials
    (re.compile(r"concrete|masonry", re.I), "materials"),
    # load-bearing structural elements & civil structures
    (re.compile(r"foundation|\bpile\b|girder|truss|bridge|tunnel", re.I), "structures_civil"),
    # pavement & water conveyance infrastructure
    (re.compile(r"pavement|asphalt|sewer|drainage", re.I), "infrastructure"),
    # envelope/facade elements & building fire-life-safety
    (re.compile(r"\broof\b|facade|window frame|curtain wall|fire sprinkler", re.I), "architecture"),
]


def topic_for(title: str) -> str | None:
    for rx, topic in TOPIC_RULES:
        if rx.search(title):
            return topic
    return None


def fetch(url: str) -> str:
    r = requests.get(url, headers=UA, timeout=60)
    r.raise_for_status()
    return r.text


def subpages(bucket: str, index_html: str) -> list[str]:
    """If `index_html` is an index-of-subpages bucket, return the sub-page filenames in order
    (p1, p2, ...). Returns [] if the bucket page holds entries directly (small/old buckets)."""
    nums = sorted(set(int(n) for n in
                       re.findall(rf"href='{re.escape(bucket)}-p(\d+)\.html'", index_html)))
    return [f"{bucket}-p{n}.html" for n in nums]


def parse_entries(page_html: str):
    for line in page_html.splitlines():
        m = ENTRY_RE.search(line)
        if not m:
            continue
        pubnum = m.group(1)
        # sitemap titles are (sometimes doubly-) HTML-entity-escaped, e.g. "&amp;#39;" for an
        # apostrophe — unescape twice to fully recover it.
        title = html.unescape(html.unescape(m.group(2))).strip()
        yield pubnum, title


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--bucket", default="2020-W01",
                     help="sitemap bucket name: weekly 'YYYY-WNN' or yearly 'YYYY' (pre-1900)")
    ap.add_argument("--max", type=int, default=400, help="cap on new entries this run")
    ap.add_argument("--append", action="store_true", help="append into the registry (registry/patents.yaml)")
    args = ap.parse_args()

    urls, titles, reg_ids = registry.existing_keys()

    index_url = f"{SITEMAP}/{args.bucket}.html"
    try:
        index_text = fetch(index_url)
    except Exception as e:
        print(f"# sitemap fetch failed for bucket {args.bucket!r}: {e}", file=sys.stderr)
        sys.exit(1)

    pages = subpages(args.bucket, index_text)
    # (label, sub-page url) to fetch, in order; a single-page bucket has one (bucket, None) entry
    # meaning "use index_text, already fetched — don't fetch it again".
    page_specs = [(p, f"{SITEMAP}/{p}") for p in pages] if pages else [(args.bucket, None)]

    out: list[dict] = []
    us_scanned = 0
    for i, (label, purl) in enumerate(page_specs):
        if len(out) >= args.max:
            break
        if purl is None:
            text = index_text  # single-page bucket, already fetched
        else:
            if i:
                time.sleep(0.5)  # politeness between sub-page fetches
            try:
                text = fetch(purl)
            except Exception as e:
                print(f"# fetch failed {purl}: {e}", file=sys.stderr)
                continue
        for pubnum, title in parse_entries(text):
            us_scanned += 1
            if len(out) >= args.max:
                break
            if not title:
                continue
            topic = topic_for(title)
            if topic is None:
                continue
            url = f"https://patents.google.com/patent/{pubnum}/en"
            u, t = url.rstrip("/"), registry.norm(title)
            if u in urls or t in titles:
                continue
            urls.add(u)
            titles.add(t)
            out.append({"id": f"pat-{registry.slug(pubnum)}", "title": title[:150],
                        "url": url, "source": "google_patents", "license": "public-domain",
                        "topic": topic, "format": "html"})

    registry.uniquify_ids(out, reg_ids)

    by_topic: dict = {}
    for h in out:
        by_topic[h["topic"]] = by_topic.get(h["topic"], 0) + 1
    npages = len(page_specs)
    print(f"# {len(out)} NEW built-environment US patents (bucket {args.bucket}, {npages} "
          f"sitemap page(s), {us_scanned} US entries scanned; deduped vs manifest + registry + blocklist)")
    print(f"# by topic: {by_topic}")
    print("# --- review, then --append, then scripts/build_corpus.py ---")
    print(yaml.safe_dump(out, sort_keys=False, allow_unicode=True))

    if args.append and out:
        counts = registry.append_entries(out)
        print(f"# appended {len(out)} entries to the registry: {counts}", file=sys.stderr)


if __name__ == "__main__":
    main()
