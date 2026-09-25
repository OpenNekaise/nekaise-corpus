#!/usr/bin/env python3
"""find_boverket.py — enumerate Boverket (Swedish national building authority) publications.

Targets the Nordic cell of the coverage radar (0.4% of the corpus, coverage_matrix.py
2026-07-23). boverket.se lists ~350 publications (35 listing pages × ~10) at
/sv/om-boverket/publicerat-av-boverket/publikationer/?page=N, each publication page carrying
a direct /globalassets/*.pdf link — building regulations (BBR/EKS, some in English), energy &
climate-declaration guidance, planning/housing analyses. Mostly Swedish -> also feeds the sv
language gap; the all-language gate keeps what is on-topic. Agency publications are freely
downloadable/shareable -> license=open.

robots.txt sets Crawl-delay: 10 — this finder sleeps 10s between EVERY fetch (and
build_corpus.py carries a matching HOST_DELAY), so keep --pages small: one listing page ≈
1 + ~10 fetches ≈ 2 min. Rotation pointer: registry/rotation.json find_boverket (--page).

    python scripts/find_boverket.py --page 1 --pages 2           # propose
    python scripts/find_boverket.py --page 1 --pages 2 --append

--mode bfs (backend find_boverket_bfs, Codex decision 2026-09-25): Boverkets författningssamling
— every BFS (grundförfattning, ändrings- and upphävandeförfattning, incl. all BBR/EKS versions and
their allmänna råd) from the official rättsinformation Atom feed rinfo.boverket.se/index.atom
(one request lists all ~460 with a direct PDF each), plus Boverket's published consolidations,
which exist only for some amendments at /<grund>/dok/<amendment>_Konsolidering.pdf and are found
by probing each amendment once (HEAD; a 404 is remembered in workspace/ for PROBE_TTL_DAYS).
Statutory text: public domain under upphovsrättslagen (1960:729) 9 §. Ids are deterministic
(`bov-bfs-<grund>-<doc>`, consolidation `…-kons`), titles start with the BFS number so versions
never collapse under title dedup. Dynamic cursor: START | <index into the published-date-sorted
feed>:<feed fingerprint> | watch:<YYYY-MM-DD> (after a full pass the feed is re-read at most every WATCH_DAYS days;
new BFS enter through the same dedup). --max is a hard cap on proposed entries and
--max-requests on HTTP requests (robots.txt fetches excluded); a capped run reports NEXT at the
first unprocessed entry, an access deferral reports HOLD.

    python scripts/find_boverket.py --mode bfs --cursor START --max 24 --max-requests 8
"""
from __future__ import annotations

import argparse
import html as htmllib
import json
import re
import sys
import time
import xml.etree.ElementTree as ET
from datetime import date, timedelta

import requests

import dedup
import registry

BASE = "https://www.boverket.se"
LIST = BASE + "/sv/om-boverket/publicerat-av-boverket/publikationer/?page={n}"
UA = {"User-Agent": "nekaise-corpus/find_boverket (research corpus; honors Crawl-delay 10)"}
DELAY = 10.0  # robots.txt Crawl-delay
PUB_RE = re.compile(r'href="(https://www\.boverket\.se/sv/om-boverket/publikationer/2\d{3}/[^"]+)"')
PDF_RE = re.compile(r'href="(/globalassets/[^"]+\.pdf)"', re.I)
TITLE_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.S)

TOPICS = [  # Swedish keyword -> registry topic; first match wins
    (re.compile(r"energi|klimatdeklaration|laddinfrastruktur|värme", re.I), "building_energy"),
    (re.compile(r"byggregler|\bbbr\b|\beks\b|föreskrift|konstruktionsregler", re.I), "standards_protocols"),
    (re.compile(r"brand", re.I), "architecture"),
    (re.compile(r"konstruktion|bärverk|bärande", re.I), "structures_civil"),
    (re.compile(r"plan(ering)?|översiktsplan|detaljplan|stadsutveckling", re.I), "urban"),
    (re.compile(r"bygg|renovering|ombyggnad", re.I), "construction"),
]


def fetch(url: str) -> str:
    time.sleep(DELAY)
    r = requests.get(url, headers=UA, timeout=45)
    r.raise_for_status()
    return r.text


def topic_for(title: str) -> str:
    for p, t in TOPICS:
        if p.search(title):
            return t
    return "urban"  # housing/planning authority default


# ------------------------------------------------------------------------------ BFS (rinfo) mode
FEED = "https://rinfo.boverket.se/index.atom"
ATOM = {"a": "http://www.w3.org/2005/Atom"}
PDF_PATH_RE = re.compile(
    r"^https://rinfo\.boverket\.se/(?P<grund>[A-Z]{2,5}\d{4}-\d+)/pdf/(?P<doc>[A-Z]{2,5}\d{4}-\d+)\.pdf$")
BFS_DELAY = 2.0          # Codex pacing for rinfo.boverket.se (no robots Crawl-delay)
WATCH_DAYS = 7
PROBE_TTL_DAYS = 90
LICENSE_URL = "https://www.riksdagen.se/sv/dokument-och-lagar/dokument/svensk-forfattningssamling/lag-1960729-om-upphovsratt-till-litterara-och_sfs-1960-729/"


def bfs_label(code: str) -> str:
    """'BFS2011-6' -> 'BFS 2011:6' (also BOFS/other series prefixes)."""
    m = re.fullmatch(r"([A-Z]+)(\d{4})-(\d+)", code)
    return f"{m.group(1)} {m.group(2)}:{m.group(3)}" if m else code


def parse_feed(xml_text: str) -> list[dict]:
    """rinfo Atom -> [{rinfo_id, title, published, pdf, grund, doc, kind}], sorted by
    (published, doc) so new BFS land at the end. Entries without a well-formed PDF are dropped."""
    root = ET.fromstring(xml_text.lstrip("﻿"))
    if root.tag != f"{{{ATOM['a']}}}feed":
        raise ValueError(f"not an Atom feed (root {root.tag!r})")
    out = []
    for e in root.findall("a:entry", ATOM):
        content = e.find("a:content", ATOM)
        src = (content.get("src") if content is not None else "") or ""
        m = PDF_PATH_RE.match(src.strip())
        if not m:
            continue
        title = re.sub(r"\s+", " ", (e.findtext("a:title", "", ATOM) or "")).strip().rstrip(";")
        grund, doc = m.group("grund"), m.group("doc")
        kind = ("grundförfattning" if grund == doc else
                "upphävandeförfattning" if re.search(r"upphäv", title, re.I) else
                "ändringsförfattning")
        out.append({"rinfo_id": (e.findtext("a:id", "", ATOM) or "").strip(), "title": title,
                    "published": (e.findtext("a:published", "", ATOM) or "")[:10],
                    "pdf": src.strip(), "grund": grund, "doc": doc, "kind": kind})
    out.sort(key=lambda x: (x["published"], x["doc"], x["grund"]))
    return out


def bfs_entry(item: dict, today: str) -> dict:
    grund, doc = item["grund"], item["doc"]
    label = bfs_label(doc)
    rel = "" if grund == doc else f" (ändrar {bfs_label(grund)})"
    title = f"{label}{rel} — {item['title']}"
    sid = f"bov-bfs-{registry.slug(grund)}" + ("" if grund == doc else f"-{registry.slug(doc)}")
    entry = {
        "id": sid, "title": title[:180], "url": item["pdf"], "source": "boverket_bfs",
        "license": "public-domain", "topic": bfs_topic(item["title"]),
        "format": "pdf", "language": "sv", "jurisdiction": "SE", "document_type": item["kind"],
        "persistent_id": item["rinfo_id"],
        "license_url": LICENSE_URL,
        "license_evidence": ("Upphovsrättslagen (1960:729) 9 § 1: författningar och beslut av "
                             "myndigheter omfattas inte av upphovsrätt; official BFS PDF listed "
                             f"in the Boverket rättsinformation feed {FEED} as {item['rinfo_id']}"),
        "rights_verified_at": today,
    }
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", item["published"]):
        entry["published_at"] = item["published"]
    return entry


def bfs_topic(title: str) -> str:
    """Subject topic when the title names one; otherwise standards_protocols (regulation)."""
    topic = topic_for(title)
    return "standards_protocols" if topic == "urban" and not re.search(
        r"plan(ering)?|översiktsplan|detaljplan|stadsutveckling", title, re.I) else topic


def consolidation_url(item: dict) -> str:
    return f"https://rinfo.boverket.se/{item['grund']}/dok/{item['doc']}_Konsolidering.pdf"


def consolidation_entry(item: dict, today: str) -> dict:
    base = bfs_entry(item, today)
    base.update({
        "id": base["id"] + "-kons",
        "title": (f"{bfs_label(item['grund'])} konsoliderad t.o.m. {bfs_label(item['doc'])} — "
                  f"{item['title']}")[:180],
        "url": consolidation_url(item), "document_type": "konsoliderad version",
        "license_evidence": ("Upphovsrättslagen (1960:729) 9 §: författningstext; Boverket's "
                             "informational consolidation published beside the amendment "
                             f"{bfs_label(item['doc'])} on rinfo.boverket.se"),
    })
    base.pop("published_at", None)
    return base


def _probe_memory_path():
    import ops
    return ops.WORKSPACE / "boverket-bfs-probes.json"


def load_probes() -> dict:
    try:
        return json.loads(_probe_memory_path().read_text())
    except (OSError, ValueError):
        return {}


def save_probes(probes: dict) -> None:
    import ops
    path = _probe_memory_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    ops.atomic_write_text(path, json.dumps(probes, sort_keys=True))


class Budget:
    def __init__(self, n: int):
        self.left = n

    def take(self) -> bool:
        if self.left <= 0:
            return False
        self.left -= 1
        return True


def head_exists(url: str) -> bool | None:
    """True/False for a konsolidering PDF; None = could not be established now (defer).
    The HEAD goes through the programme gate (reviewed host, host policy, robots.txt with its
    Crawl-delay, shared host clock); a policy refusal counts as "not collectable" (False)."""
    import polite_http
    try:
        r = polite_http.head(url, delay=BFS_DELAY)
    except polite_http.Deferred:
        return None
    except polite_http.Refused:
        return False
    if r.status_code == 200 and "pdf" in (r.headers.get("content-type") or "").lower():
        return True
    if r.status_code in (404, 410):
        return False
    return None


def run_bfs(cursor: str, maxn: int, max_requests: int, keys, report, today: date | None = None,
            fetch_feed=None, probe=None) -> list[dict]:
    """One BFS run; returns proposed entries and reports exactly one rotation outcome."""
    import polite_http

    today = today or date.today()
    iso = today.isoformat()
    if cursor.startswith("watch:"):
        since = date.fromisoformat(cursor.split(":", 1)[1])
        if today - since < timedelta(days=WATCH_DAYS):
            report.next(cursor)  # watch phase: nothing to do yet, no request
            return []
        cursor = "0"
    raw_index, _, want_fp = ("0" if cursor in ("START", "") else cursor).partition(":")
    index = int(raw_index)
    budget = Budget(max_requests)
    if not budget.take():
        report.hold("--max-requests 0")
        return []
    try:
        text = (fetch_feed or (lambda: polite_http.get(FEED, delay=BFS_DELAY,
                                                       expect="xml").text))()
        items = parse_feed(text)
    except (polite_http.Deferred, polite_http.Refused, polite_http.TooLarge,
            requests.RequestException, ET.ParseError, ValueError) as exc:
        report.hold(f"BFS feed unavailable: {exc}")
        return []
    if not items:
        report.hold("BFS feed parsed to zero entries (refusing to treat as exhaustion)")
        return []
    fp = feed_fingerprint(items)
    if want_fp and want_fp != fp or index > len(items):
        index = 0  # the feed changed under the cursor: re-walk from the start (dedup is safe)
    probe = probe or head_exists
    probes = load_probes()
    out: list[dict] = []
    stop_at = None
    keys.prefetch(urls=[i["pdf"] for i in items[index:]] +
                  [consolidation_url(i) for i in items[index:]],
                  ids=[bfs_entry(i, iso)["id"] for i in items[index:]])
    i = index
    while i < len(items):
        item = items[i]
        entry = bfs_entry(item, iso)
        want = []
        if entry["url"] not in keys.urls and entry["id"] not in keys.ids:
            want.append(entry)
        if item["grund"] != item["doc"]:
            kons = consolidation_entry(item, iso)
            seen = probes.get(kons["url"])
            fresh = bool(seen) and (today - date.fromisoformat(seen["at"])).days < PROBE_TTL_DAYS
            if kons["url"] not in keys.urls and kons["id"] not in keys.ids:
                if fresh:
                    exists = seen["exists"]
                else:
                    if not budget.take():
                        stop_at = i
                        break
                    try:
                        exists = probe(kons["url"])
                    except requests.RequestException:
                        exists = None
                    if exists is None:
                        stop_at = i
                        break
                    probes[kons["url"]] = {"exists": exists, "at": iso}
                if exists:
                    want.append(kons)
        if len(out) + len(want) > maxn:
            stop_at = i
            break
        for e in want:
            keys.urls.add(e["url"])
            keys.ids.add(e["id"])
            out.append(e)
        i += 1
    save_probes(probes)
    if stop_at is None:
        report.next(f"watch:{iso}")
    elif stop_at == index and not out:
        report.hold("no progress possible this run (request budget or access deferral)")
    else:
        report.next(f"{stop_at}:{fp}")
    return out


def feed_fingerprint(items: list[dict]) -> str:
    """Of the whole ordered feed: an offset into a changed feed could skip unseen entries."""
    import hashlib
    return hashlib.sha1("\n".join(i["pdf"] for i in items).encode()).hexdigest()[:12]


def main_bfs(args) -> None:
    import compliance_common
    import finder_protocol

    compliance_common.pin_host_policy()
    keys = dedup.open_keys()
    report = finder_protocol.Report()
    out = run_bfs(args.cursor, args.max, args.max_requests, keys, report)
    ok, _held = compliance_common.split_appendable(out)
    print(f"# {len(ok)} NEW Boverket BFS documents (rinfo feed; deduped vs manifest + registry "
          f"+ blocklist)")
    for e in ok:
        print(f"#   {e['id']}  {e['title'][:90]}")
    if args.append and ok:
        counts = registry.append_entries(ok)
        print(f"# appended {len(ok)} entries: {counts}", file=sys.stderr)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=("publications", "bfs"), default="publications")
    ap.add_argument("--cursor", default="START", help="bfs mode: dynamic rotation cursor")
    ap.add_argument("--max-requests", type=int, default=8,
                    help="bfs mode: HTTP request budget this run (robots.txt excluded)")
    ap.add_argument("--page", type=int, default=1, help="listing page to start at (1-based)")
    ap.add_argument("--pages", type=int, default=2, help="listing pages to walk this run")
    ap.add_argument("--max", type=int, default=40, help="cap on new entries this run")
    ap.add_argument("--append", action="store_true",
                    help="append into the registry (registry/nordic.yaml)")
    args = ap.parse_args()
    if args.mode == "bfs":
        if args.max < 2:
            ap.error("--mode bfs needs --max >= 2 (an amendment and its consolidation)")
        main_bfs(args)
        return

    keys = dedup.open_keys()
    urls, titles = keys.urls, keys.titles
    out: list[dict] = []
    scanned = 0
    for n in range(args.page, args.page + args.pages):
        if len(out) >= args.max:
            break
        try:
            listing = fetch(LIST.format(n=n))
        except Exception as e:
            print(f"# listing fetch failed p{n}: {e}", file=sys.stderr)
            sys.exit(1)  # abort WITHOUT advancing rotation
        pubs = sorted(set(PUB_RE.findall(listing)))
        if not pubs:
            print(f"# no publications on listing p{n} — end of archive?", file=sys.stderr)
            break
        for purl in pubs:
            if len(out) >= args.max:
                break
            scanned += 1
            try:
                page = fetch(purl)
            except Exception as e:
                print(f"# pub fetch failed {purl}: {e}", file=sys.stderr)
                continue
            mpdf = PDF_RE.search(page)
            mtitle = TITLE_RE.search(page)
            if not mpdf or not mtitle:
                continue  # some entries are web-only guidance without a PDF
            title = htmllib.unescape(re.sub(r"<[^>]+>", "", mtitle.group(1))).strip()
            pdf = BASE + mpdf.group(1)
            if pdf.rstrip("/") in urls or registry.norm(title) in titles:
                continue
            urls.add(pdf.rstrip("/"))
            titles.add(registry.norm(title))
            out.append({"id": f"bov-{registry.slug(title)[:50]}", "title": title[:150],
                        "url": pdf, "source": "boverket", "license": "open",
                        "topic": topic_for(title), "format": "pdf"})

    keys.uniquify_ids(out)
    print(f"# {len(out)} NEW Boverket publications (listing p{args.page}..{args.page+args.pages-1}, "
          f"{scanned} pub pages scanned; deduped vs manifest + registry + blocklist)")
    by_topic: dict = {}
    for h in out:
        by_topic[h["topic"]] = by_topic.get(h["topic"], 0) + 1
    print(f"# by topic: {by_topic}")
    if args.append and out:
        counts = registry.append_entries(out)
        print(f"# appended {len(out)} entries: {counts}", file=sys.stderr)


if __name__ == "__main__":
    main()
