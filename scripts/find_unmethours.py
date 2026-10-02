#!/usr/bin/env python3
"""find_unmethours.py — Unmet Hours questions (building energy simulation Q&A, CC BY-SA 3.0).

Unmet Hours (https://unmethours.com, run by Big Ladder Software) is the practitioner Q&A site of
building energy modelling: EnergyPlus, OpenStudio, Radiance, eQUEST/DOE-2, IES VE, DesignBuilder,
TRNSYS, Modelica, calibration, ASHRAE 90.1 Appendix G. Every page footer states "User
contributions licensed under the Creative Commons Attribution Share Alike 3.0 License"
(creativecommons.org/licenses/by-sa/3.0/legalcode); robots.txt is `User-agent: * Allow: /` and
names the sitemap, which lists every question page (13,298 on 2026-10-02). Before 2026-10 the
corpus held 176 of them through a one-off crawl_docs run (crawl-unmethours-* ids).

The finder reads the sitemap (one request) and the questions index page (one request: the
footer licence statement must still be there, else nothing is proposed and the cursor holds),
then proposes question pages in ascending question-id order. Pages are HTML, fetched by the
loader one at a time with a delay (scripts/build_corpus.py HOST_CONCURRENCY / HOST_DELAY).

Rotation (dynamic, `--cursor`): "<last question id walked>" or "watch:<YYYY-MM-DD>:<id>". A run
proposes questions with a larger id until --max new entries; when the sitemap is walked to its
end the cursor becomes a watch cursor and the sitemap is read again at most every WATCH_DAYS
days, so new questions keep arriving without a request every round.

    python scripts/find_unmethours.py --cursor 0 --max 20
"""
from __future__ import annotations

import argparse
import re
import sys
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone

import requests
import yaml
from bs4 import BeautifulSoup

import dedup
import licenses
import registry
from finder_protocol import Report

SITE = "https://unmethours.com"
SITEMAP = f"{SITE}/sitemap.xml"
INDEX = f"{SITE}/questions/"
UA = {"User-Agent": "nekaise-corpus/find_unmethours (research corpus; robots.txt honoured)"}
LICENCE_URL = "https://creativecommons.org/licenses/by-sa/3.0/legalcode"
STATEMENT = ("User contributions licensed under the Creative Commons Attribution Share Alike "
             "3.0 License")
QUESTION = re.compile(r"^https?://unmethours\.com/question/(\d+)/([a-z0-9_%-]+)/?$", re.I)
SM_NS = "{http://www.sitemaps.org/schemas/sitemap/0.9}"
WATCH_DAYS = 7
SOURCE = "unmethours"
TOPIC = "simulation_modeling"
PREFIX = "umh-"


class Unexpected(RuntimeError):
    """Not the expected sitemap / index page (maintenance, challenge, error, licence change)."""


def get(url: str) -> str:
    response = requests.get(url, headers=UA, timeout=90)
    if response.status_code != 200:
        raise Unexpected(f"{url} answered HTTP {response.status_code}")
    return response.text


def licence_problem(html: str) -> str | None:
    """None when the page footer (`#ground .copyright`, comments ignored) states the CC BY-SA 3.0
    grant and every Creative Commons link in it is the canonical BY-SA 3.0 licence (read by the
    shared fail-closed parser); otherwise what is missing or conflicting."""
    soup = BeautifulSoup(html, "html.parser")
    footer = soup.select_one("#ground .copyright")
    if footer is None:
        return "no #ground .copyright footer"
    if STATEMENT not in " ".join(footer.get_text(" ").split()):
        return "footer does not state the CC BY-SA 3.0 grant"
    links = [a.get("href", "") for a in footer.find_all("a")
             if "creativecommons" in a.get("href", "").lower()]
    if not links:
        return "footer has no Creative Commons licence link"
    for href in links:
        tag, url = licenses.cc_license([href])
        if tag != "cc-by-sa" or "/by-sa/3.0/" not in (url or ""):
            return f"footer links a different licence: {href}"
    return None


def parse_sitemap(xml_text: str) -> list[tuple[int, str]]:
    """Sorted unique (question id, slug) pairs of the sitemap's question pages."""
    try:
        root = ET.fromstring(xml_text.encode("utf-8"))
    except ET.ParseError as exc:
        raise Unexpected(f"sitemap is not XML: {exc}") from exc
    if root.tag != f"{SM_NS}urlset":
        raise Unexpected(f"sitemap root element is {root.tag!r}, not urlset")
    found: dict[int, str] = {}
    for loc in root.iter(f"{SM_NS}loc"):
        if match := QUESTION.match((loc.text or "").strip()):
            found.setdefault(int(match.group(1)), match.group(2).lower())
    if not found:
        raise Unexpected("sitemap lists no question pages")
    return sorted(found.items())


def entry(qid: int, slug: str, today: str) -> dict:
    words = " ".join(re.sub(r"%[0-9a-f]{2}", " ", slug, flags=re.I).replace("_", " ")
                     .split("-")).split()
    return {
        "id": f"{PREFIX}{qid}-{registry.slug(' '.join(words))[:44]}".rstrip("-"),
        "title": f"Unmet Hours Q{qid}: {' '.join(words)}"[:150],
        "url": f"{SITE}/question/{qid}/{slug}/",
        "source": SOURCE, "license": "cc-by-sa", "topic": TOPIC, "format": "html",
        "language": "en", "document_type": "forum-thread",
        "license_url": LICENCE_URL,
        "license_evidence": ("unmethours.com page footer: 'User contributions licensed under "
                             "the Creative Commons Attribution Share Alike 3.0 License'"),
        "rights_verified_at": today,
    }


def parse_cursor(cursor: str) -> tuple[date | None, int]:
    if match := re.fullmatch(r"watch:(\d{4}-\d{2}-\d{2}):(\d+)", cursor):
        return date.fromisoformat(match.group(1)), int(match.group(2))
    if cursor.isdigit():
        return None, int(cursor)
    raise ValueError(f"cursor must be '<id>' or 'watch:<YYYY-MM-DD>:<id>': {cursor!r}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--cursor", default="0", help="last question id walked, or watch:<date>:<id>")
    ap.add_argument("--max", type=int, default=100, help="new entries this run")
    ap.add_argument("--append", action="store_true",
                    help="append into the registry (registry/unmethours.yaml)")
    args = ap.parse_args()
    if args.max < 1:
        ap.error("--max must be positive")
    try:
        watched, last = parse_cursor(args.cursor)
    except ValueError as exc:
        ap.error(str(exc))
    report = Report()
    now = datetime.now(timezone.utc).date()
    if watched is not None and now < watched + timedelta(days=WATCH_DAYS):
        print(f"# 0 NEW Unmet Hours questions (sitemap walked {watched}; next look "
              f"{watched + timedelta(days=WATCH_DAYS)})")
        report.next(args.cursor)
        return
    try:
        questions = parse_sitemap(get(SITEMAP))
        if problem := licence_problem(get(INDEX)):
            raise Unexpected(f"licence evidence on {INDEX}: {problem}")
    except Unexpected as exc:
        report.hold(f"{exc}; nothing proposed")
        print("# 0 NEW Unmet Hours questions (unexpected answer)")
        return
    except Exception as exc:
        print(f"# ERROR: Unmet Hours request failed: {exc}; nothing proposed", file=sys.stderr)
        raise SystemExit(1)

    today = now.isoformat()
    keys = dedup.open_keys()
    pending = [(q, s) for q, s in questions if q > last]
    out: list[dict] = []
    walked = last
    for start in range(0, len(pending), 200):
        if len(out) >= args.max:
            break
        batch = [entry(q, s, today) for q, s in pending[start:start + 200]]
        keys.prefetch(**dedup.page_keys(batch))
        for (qid, _slug), e in zip(pending[start:start + 200], batch):
            if len(out) >= args.max:
                break
            walked = qid
            u, t = e["url"].rstrip("/"), registry.norm(e["title"])
            if u in keys.urls or t in keys.titles:
                continue
            keys.urls.add(u)
            keys.titles.add(t)
            out.append(e)

    keys.uniquify_ids(out)
    print(f"# {len(out)} NEW Unmet Hours questions (sitemap {len(questions)} questions, "
          f"{len(pending)} after id {last}; cc-by-sa 3.0; deduped vs manifest + registry + "
          "blocklist)")
    print("# --- review, then --append, then scripts/build_corpus.py ---")
    print(yaml.safe_dump(out, sort_keys=False, allow_unicode=True))
    if args.append and out:
        counts = registry.append_entries(out)
        print(f"# appended {len(out)} entries to the registry: {counts}", file=sys.stderr)
    if walked >= questions[-1][0]:
        report.next(f"watch:{today}:{walked}")
    else:
        report.next(str(walked))


if __name__ == "__main__":
    main()
