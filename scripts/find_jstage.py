#!/usr/bin/env python3
"""find_jstage.py — enumerate AIJ (日本建築学会) journal articles via the official J-STAGE API.

Targets the two biggest cells on the coverage radar (coverage_matrix.py, 2026-07-23):
region JP = 0.3% and language ja = 0.5% of the corpus. J-STAGE publishes an official,
key-free search API (https://api.jstage.jst.go.jp/searchapi/do, Atom XML) and hosts the
Architectural Institute of Japan's open-archive journals — thousands of free full-text PDFs
of building-science research that OpenAlex/OSTI never surface:

    環境系論文集  (J. Environmental Engineering, ~3.6k)   -> building_energy
    構造系論文集  (J. Structural & Construction Eng.)      -> structures_civil
    計画系論文集  (J. Architecture & Planning)             -> architecture
    技術報告集    (AIJ Journal of Technology & Design)     -> construction

One run walks `--start..start+count-1` result indices of ONE journal (`--series`); the
rotation pointer (registry/rotation.json, find_jstage) advances `--start` by `--count`.
Article PDF URL is derived from the Atom <link> article URL (…/_article/-char/ja ->
…/_pdf/-char/ja). AIJ opened these archives for free access; per-article copyright stays
with AIJ -> license=open (free full-text, check per-source terms), NOT cc-by.
J-STAGE throttles aggressively (nightly ~00:00 JST maintenance 503s everything): keep
--count <= 200, sleep between API pages, and pair with a build_corpus HOST_DELAY entry.

    python scripts/find_jstage.py --series kankyo --start 1 --count 100          # propose
    python scripts/find_jstage.py --series kankyo --start 1 --count 200 --append
"""
from __future__ import annotations

import argparse
import re
import sys
import time
import xml.etree.ElementTree as ET

import requests

import registry

API = "https://api.jstage.jst.go.jp/searchapi/do"
UA = {"User-Agent": "nekaise-corpus/find_jstage (research corpus; polite)"}
PAGE = 100  # API max rows per call is 1000, keep pages small and polite

SERIES = {
    # key: (material query = exact journal name on J-STAGE, topic)
    "kankyo":  ("日本建築学会環境系論文集", "building_energy"),
    "kouzou":  ("日本建築学会構造系論文集", "structures_civil"),
    "keikaku": ("日本建築学会計画系論文集", "architecture"),
    "gijutsu": ("日本建築学会技術報告集", "construction"),
}
ATOM = "{http://www.w3.org/2005/Atom}"


def fetch_page(material: str, start: int, count: int, from_year: int = 2014) -> list[ET.Element]:
    r = requests.get(API, params={"service": 3, "material": material,
                                  "start": start, "count": count,
                                  "pubyearfrom": from_year},
                     headers=UA, timeout=60)
    r.raise_for_status()
    root = ET.fromstring(r.content)
    return root.findall(f"{ATOM}entry")


def parse(entry: ET.Element) -> tuple[str, str, str] | None:
    """-> (title, article_url, year) or None if the entry lacks a usable link/title."""
    # titles come <article_title><ja>…</ja><en>…</en></article_title>; prefer ja, keep en tail.
    # NB: the feed's DEFAULT xmlns puts even J-STAGE's custom elements in the Atom namespace,
    # so every path step needs the {*} wildcard.
    ja = entry.findtext(".//{*}article_title/{*}ja") or ""
    en = entry.findtext(".//{*}article_title/{*}en") or ""
    title = (ja or en).strip()
    if en and ja:
        title = f"{ja.strip()} ({en.strip()[:70]})"
    link = entry.find("{*}link")
    url = (link.get("href") if link is not None else "") or ""
    year = entry.findtext("{*}pubyear") or ""
    if not title or "/article/" not in url:
        return None
    return title, url, year


def pdf_url(article_url: str) -> str:
    """…/<id>/_article/-char/ja  ->  …/<id>/_pdf/-char/ja (J-STAGE's stable PDF path)."""
    u = re.sub(r"/_article\b", "/_pdf", article_url)
    return u if "/_pdf" in u else article_url.rstrip("/") + "/_pdf"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", default="kankyo", choices=sorted(SERIES),
                    help="AIJ journal to walk (see SERIES)")
    ap.add_argument("--start", type=int, default=1, help="1-based result index to start at")
    ap.add_argument("--count", type=int, default=200, help="how many results to walk this run")
    ap.add_argument("--from-year", type=int, default=2014,
                    help="pubyearfrom API filter: pre-2014 AIJ PDFs lack ToUnicode (mojibake); "
                         "the API's sort order is NOT chronological, so filter at the source")
    ap.add_argument("--append", action="store_true",
                    help="append into the registry (registry/jstage.yaml)")
    args = ap.parse_args()
    material, topic = SERIES[args.series]

    urls, titles, reg_ids = registry.existing_keys()
    out: list[dict] = []
    scanned = 0
    for start in range(args.start, args.start + args.count, PAGE):
        n = min(PAGE, args.start + args.count - start)
        try:
            entries = fetch_page(material, start, n, args.from_year)
        except Exception as e:
            print(f"# API fetch failed at start={start}: {e}", file=sys.stderr)
            sys.exit(1)  # throttle/maintenance window — abort WITHOUT advancing rotation
        if not entries:
            print(f"# no entries at start={start} — series exhausted?", file=sys.stderr)
            break
        for entry in entries:
            scanned += 1
            p = parse(entry)
            if not p:
                continue
            title, aurl, year = p
            u = pdf_url(aurl).rstrip("/")
            if u in urls or registry.norm(title) in titles:
                continue
            aid = re.search(r"/article/[^/]+/(\S+?)/_", pdf_url(aurl))
            sid = f"jst-{args.series}-{registry.slug(aid.group(1) if aid else title[:40])}"
            urls.add(u)
            titles.add(registry.norm(title))
            out.append({"id": sid, "title": title[:150], "url": pdf_url(aurl),
                        "source": "jstage_aij", "license": "open", "topic": topic,
                        "format": "pdf"})
        time.sleep(2.0)  # politeness between API pages

    registry.uniquify_ids(out, reg_ids)
    print(f"# {len(out)} NEW AIJ {args.series} articles (start {args.start}, {scanned} scanned; "
          f"deduped vs manifest + registry + blocklist)")
    print(f"# topic: {topic}, license: open (AIJ free full-text archive)")
    if args.append and out:
        counts = registry.append_entries(out)
        print(f"# appended {len(out)} entries: {counts}", file=sys.stderr)


if __name__ == "__main__":
    main()
