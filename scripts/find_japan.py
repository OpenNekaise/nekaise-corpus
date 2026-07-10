#!/usr/bin/env python3
"""find_japan.py — Japanese building-research reports from two government institutes.

BRI (建築研究所 / Building Research Institute, kenken.go.jp) and NILIM (国土技術政策総合研究所 /
National Institute for Land and Infrastructure Management, nilim.go.jp) both publish decades of
dense building/structural/fire/seismic research as freely-downloadable PDFs. The corpus accepts
all languages — the quality gate's DOMAIN vocabulary already covers Japanese building terms
(建築/耐震/空調/断熱/構造/コンクリート/...) — so this is unmined open volume, not a new language
the pruner needs teaching.

Both sites mix page encodings (some pages Shift_JIS, some UTF-8, sometimes by page vintage, not
by site) — every page fetch here tries strict UTF-8 first and falls back to Shift_JIS/cp932 on a
UnicodeDecodeError, which classified every page sampled during scouting correctly.

BRI: `report.html` lists `report/<N>/index.html` (N ~139..156+; N<139 are pre-digitization
abstract-only pages with no PDF — they 404 against the folder URL and are skipped for free).
Each report folder's chapter PDFs are linked with visible label text; the whole-document PDF is
whichever link's label is exactly/contains "全文" (full text) — NOT reliably named `all.pdf`
(seen as `all.pdf`, `<N>-all.pdf`, and even a plain numbered `6.pdf` with label "全文"). When that
link resolves (HEAD, GET-magic-byte fallback) we register ONE entry for the whole report; otherwise
one entry per chapter PDF (each titled with its own chapter label so registry title-dedup doesn't
collapse them). NOTE: the live site's own heading calls this series 建築研究報告 ("Building
Research Report"); 建築研究資料 ("Building Research Data") is a *different*, messier BRI series at
`data.html` with inconsistent PDF naming and no reliable "full text" link — not covered here, and
worth its own pass later.

NILIM: `report.html` lists year pages `<year>report/index.htm` (seen back to 2012). Each year page
links every article as its own numbered PDF (`arYYYYhpNNN.pdf` in recent years; older years use a
per-item filename with no shared pattern — handled the same way since we regex any `.pdf` href, not
just the `ar...` shape) with its title as the link text; per the source brief we register each item
individually rather than the bundled "全ページ" (all-pages) PDF, which is skipped.

Both institutes: government-run, "All Rights Reserved" footers, but the PDFs are published for
free public download with no paywall/registration — tagged `open` like the corpus's other OA
government-report veins.

    python scripts/find_japan.py --series bri --start 150 --count 5      # propose, BRI reports 150-154
    python scripts/find_japan.py --series nilim --start 2024 --count 2 --max 200 --append
"""
from __future__ import annotations

import argparse
import html
import re
import sys
import time
from pathlib import PurePosixPath
from urllib.parse import urljoin

import requests
import yaml

import registry

UA = {"User-Agent": "nekaise-corpus/find_japan"}
TIMEOUT = 30

BRI_REPORT = "https://www.kenken.go.jp/japanese/contents/publications/report/{n}/index.html"
NILIM_YEAR = "https://www.nilim.go.jp/lab/bcg/siryou/{year}report/index.htm"

TAG_RE = re.compile(r"<[^>]+>")
PDF_LINK_RE = re.compile(r'<a\s+href="([^"]+\.pdf)(?:#[^"]*)?"[^>]*>(.*?)</a>', re.S | re.I)
BRI_MARKER = re.compile(r"建築研究報告</b>")

# ordered (regex, topic) title-keyword remap — first match wins, default building_energy.
TOPIC_RULES = [
    (re.compile("耐震|構造|地震"), "structures_civil"),
    (re.compile("火災|防火"), "architecture"),
    (re.compile("空調|設備|換気"), "equipment_systems"),
    (re.compile("材料|コンクリート|木造"), "materials"),
    (re.compile("都市"), "urban"),
]


def classify_topic(text: str) -> str:
    for rx, topic in TOPIC_RULES:
        if rx.search(text or ""):
            return topic
    return "building_energy"


def fetch(session: requests.Session, url: str) -> requests.Response | None:
    try:
        r = session.get(url, headers=UA, timeout=TIMEOUT)
    except requests.RequestException as e:
        print(f"# fetch failed {url}: {e}", file=sys.stderr)
        return None
    if r.status_code != 200:
        return None
    return r


def decode(resp: requests.Response) -> str:
    """Strict UTF-8, else Shift_JIS/cp932 — both sites serve a mix of encodings page-to-page."""
    try:
        return resp.content.decode("utf-8")
    except UnicodeDecodeError:
        return resp.content.decode("shift_jis", "ignore")


def clean_label(raw: str) -> str:
    t = html.unescape(TAG_RE.sub(" ", raw)).replace("　", " ")
    return re.sub(r"\s+", " ", t).strip()


def pdf_links(txt: str, page_url: str) -> list[tuple[str, str]]:
    """[(absolute pdf url, cleaned link-text label)], deduped by url (a fragment-only variant of
    the same href, e.g. `#page=1` / `#page=2` anchors into one PDF, keeps the first label seen)."""
    seen: dict[str, str] = {}
    for href, label in PDF_LINK_RE.findall(txt):
        u = urljoin(page_url, href)
        seen.setdefault(u, clean_label(label))
    return list(seen.items())


def head_ok(session: requests.Session, url: str) -> bool:
    """HEAD check; falls back to a streamed GET + magic-byte check (%PDF) if HEAD is blocked."""
    try:
        r = session.head(url, headers=UA, timeout=TIMEOUT, allow_redirects=True)
        if r.status_code == 200:
            return True
    except requests.RequestException:
        pass
    try:
        r = session.get(url, headers=UA, timeout=TIMEOUT, stream=True)
        chunk = next(r.iter_content(16), b"")
        r.close()
        return r.status_code == 200 and chunk.startswith(b"%PDF")
    except requests.RequestException:
        return False


def bri_title(txt: str) -> str | None:
    """The report's own title, read out of the info table right after the "■建築研究報告" page
    banner. Format varies by report vintage (quoted in full-width 「」 corner brackets on some,
    plain text on others; sometimes merged onto the "NNN号（year）" line, sometimes its own line
    right after) — the title reliably ends up as the first non-blank line of that table."""
    m = BRI_MARKER.search(txt)
    start = m.end() if m else 0
    j = txt.find("<table", start)
    k = txt.find("</table>", j)
    if j < 0 or k < 0:
        return None
    plain = TAG_RE.sub("", txt[j:k])
    lines = [l.strip() for l in plain.splitlines() if l.strip()]
    return lines[0].strip("「」").strip() if lines else None


def bri_report(session: requests.Session, n: int) -> list[tuple[str, str, str, str]]:
    """One BRI report number -> [(id, title, url, topic), ...]. Empty if the report doesn't exist
    at this number (pre-digitization reports <139 have an abstract-only .htm page with no PDF and
    simply 404 against the folder URL checked here) or has no PDF links."""
    page_url = BRI_REPORT.format(n=n)
    resp = fetch(session, page_url)
    if resp is None:
        return []
    txt = decode(resp)
    title = bri_title(txt) or f"No.{n}"
    links = [(u, l) for u, l in pdf_links(txt, page_url)
             if PurePosixPath(u).stem.lower() != "agreement"]  # e.g. report 149's distribution notice
    if not links:
        return []
    topic = classify_topic(title)
    base_title = f"建築研究報告 No.{n} — {title}"
    full = next(((u, l) for u, l in links if "全文" in l), None)
    if full and head_ok(session, full[0]):
        return [(f"jpn-bri-report-{n}-all", base_title, full[0], topic)]
    rows = []
    for u, l in links:
        if "全文" in l:
            continue  # broken/unreachable whole-doc link — fall back to its sibling chapters only
        stem = registry.slug(PurePosixPath(u).stem) or "pdf"
        chap_title = f"{base_title}｜{l}" if l else base_title
        rows.append((f"jpn-bri-report-{n}-{stem}", chap_title, u, topic))
    return rows


def nilim_year(session: requests.Session, year: int) -> list[tuple[str, str, str, str]]:
    """One NILIM annual-report year -> [(id, title, url, topic), ...], one row per article PDF
    (skips the bundled "全ページ" / all-pages PDF — the source brief wants per-item registration)."""
    page_url = NILIM_YEAR.format(year=year)
    resp = fetch(session, page_url)
    if resp is None:
        return []
    txt = decode(resp)
    rows = []
    i = 0
    for u, label in pdf_links(txt, page_url):
        if "全ページ" in label:
            continue
        i += 1
        stem = PurePosixPath(u).stem
        prefix = f"ar{year}"
        tail = stem[len(prefix):] if stem.lower().startswith(prefix) else stem
        sid = f"jpn-nilim-{year}-{registry.slug(tail) or i}"
        if label:
            title = f"国総研資料 {year} — {label}"
        else:
            title = f"NILIM annual research report {year} item {i:03d}"
        rows.append((sid, title, u, classify_topic(label)))
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--series", choices=["bri", "nilim"], default="bri")
    ap.add_argument("--start", type=int, default=1,
                     help="BRI report number, or NILIM year (e.g. 2024)")
    ap.add_argument("--count", type=int, default=20,
                     help="how many report-numbers/years to walk this run")
    ap.add_argument("--max", type=int, default=300, help="cap on new entries this run")
    ap.add_argument("--append", action="store_true",
                     help="append into the registry (registry/japan.yaml)")
    args = ap.parse_args()

    urls, titles, reg_ids = registry.existing_keys()
    session = requests.Session()
    out: list[dict] = []
    walked = 0

    for i in range(args.count):
        if len(out) >= args.max:
            break
        key = args.start + i
        try:
            rows = bri_report(session, key) if args.series == "bri" else nilim_year(session, key)
        except Exception as e:
            print(f"# {args.series} {key} failed: {e}", file=sys.stderr)
            rows = []
        walked += 1
        for sid, title, url, topic in rows:
            if len(out) >= args.max:
                break
            u, t = url.rstrip("/"), registry.norm(title)
            if u in urls or t in titles:
                continue
            urls.add(u)
            titles.add(t)
            out.append({"id": sid, "title": title[:200], "url": url,
                        "source": "bri_jp" if args.series == "bri" else "nilim_jp",
                        "license": "open", "topic": topic, "format": "pdf"})
        time.sleep(0.5)  # politeness between page fetches

    registry.uniquify_ids(out, reg_ids)

    by_topic: dict = {}
    for h in out:
        by_topic[h["topic"]] = by_topic.get(h["topic"], 0) + 1
    print(f"# {len(out)} NEW {args.series.upper()} entries (walked {walked} "
          f"{'report numbers' if args.series == 'bri' else 'years'} from {args.start}; "
          f"deduped vs manifest + registry + blocklist)")
    print(f"# by topic: {by_topic}")
    print("# --- review, then --append, then scripts/build_corpus.py ---")
    print(yaml.safe_dump(out, sort_keys=False, allow_unicode=True))

    if args.append and out:
        counts = registry.append_entries(out)
        print(f"# appended {len(out)} entries to the registry: {counts}", file=sys.stderr)


if __name__ == "__main__":
    main()
