#!/usr/bin/env python3
"""build_corpus.py — fetch & verify the corpus from the registry.

Reads the registry (registry/*.yaml), downloads each source into raw/<source>/<id>.<ext>, extracts
plain text into text/<id>.md, and records everything (incl. sha256 and quality metrics) in the
sharded manifest (manifest/<shard>.jsonl, all I/O via registry.py). The committed manifest is the
REPRODUCIBILITY record: a fresh clone runs this to fetch the SAME bytes, and the run reports how many
reproduced exactly (sha256 matches the manifest) vs drifted (the source changed upstream) vs new.

  python scripts/build_corpus.py            # fetch missing; report reproduced / drifted / new vs manifest
  python scripts/build_corpus.py --force    # re-fetch everything
  python scripts/build_corpus.py --only controls_bas
  python scripts/build_corpus.py --workers 16   # more parallel downloads (default 8, ≤2 per host)
  python scripts/build_corpus.py --verify   # no download: re-hash local raw files against the manifest

Idempotent, dedups identical bytes by sha256, checkpoints the manifest every 25 fetches. Downloads
run in parallel but politely: at most 2 in-flight requests per host regardless of --workers. raw/
and text/ are git-ignored; respect each source's license (see README.md).
"""
from __future__ import annotations

import argparse
import hashlib
import io
import re
import shutil
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from urllib.parse import urlparse

import requests

import quality
import registry

HERE = Path(__file__).resolve().parents[1]  # repo root (this file lives in scripts/)
RAW = HERE / "raw"
TEXT = HERE / "text"
# Browser-like UA: publisher / repository bot-walls (eScholarship, Frontiers, PMC, …) 403 a generic
# UA even for openly-licensed (CC-BY / OA) PDFs we're entitled to fetch. (MDPI sits behind Cloudflare
# and still blocks; those need a headless browser — skipped for now.)
UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0.0.0 Safari/537.36")
TIMEOUT = 45
# plain-text source formats (GitHub READMEs / docs, .rst, etc.): stored verbatim, no parsing.
TEXT_FORMATS = {"md", "rst", "txt"}
# politeness: never more than this many in-flight requests against one host, however many workers.
PER_HOST = 2
# politeness overrides for hosts that need them (currently none). HOST_DELAY: minimum seconds
# between request STARTS against a host, enforced under its semaphore — for hosts that tarpit at
# volume. HOST_UA: per-host User-Agent override, applied to both the requests call and the curl
# fallback — for hosts that block the spoofed-browser UA but pass an honest bot UA. Before adding
# a host here to work around its wall, check its ToS/robots.txt — a wall is sometimes the host
# enforcing terms we must respect (nrc-publications.canada.ca, 07-12: "systematic downloading is
# not permitted" — that vein was reverted, NO-GO).
HOST_DELAY: dict[str, float] = {
    "www.jstage.jst.go.jp": 2.0,  # J-STAGE throttles bulk fetches; nightly ~00:00 JST 503 window
    "www.boverket.se": 10.0,      # robots.txt Crawl-delay: 10 — respect it
}
HOST_UA: dict[str, str] = {}

_host_sems: dict[str, threading.BoundedSemaphore] = {}
_host_sems_lock = threading.Lock()
_host_next: dict[str, float] = {}
_host_next_lock = threading.Lock()


def _host_sem(url: str) -> threading.BoundedSemaphore:
    host = urlparse(url).netloc.lower()
    with _host_sems_lock:
        return _host_sems.setdefault(host, threading.BoundedSemaphore(PER_HOST))


def sha256_bytes(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def clean_text(s: str) -> str:
    """Drop lone surrogates etc. that pypdf sometimes emits — they crash write_text()."""
    return s.encode("utf-8", "replace").decode("utf-8")


def extract_text_plain(data: bytes) -> str:
    """Decode an already-human-readable text file (markdown / rst / txt) verbatim."""
    return data.decode("utf-8", "ignore").strip()


def extract_for(fmt: str, data: bytes) -> str:
    if fmt == "pdf":
        return extract_pdf(data)
    if fmt == "html":
        return extract_html(data)
    if fmt in TEXT_FORMATS:
        return extract_text_plain(data)
    return ""


def extract_pdf(data: bytes) -> str:
    from pypdf import PdfReader

    try:
        reader = PdfReader(io.BytesIO(data))
        parts = []
        for i, page in enumerate(reader.pages):
            try:
                parts.append(page.extract_text() or "")
            except Exception as e:  # keep going on a bad page
                parts.append(f"[page {i} extract error: {e}]")
        txt = "\n\n".join(parts).strip()
    except Exception:  # broken xref/trailer — pypdf can't even open it; poppler usually can
        txt = ""
    # pdftotext (poppler) rescues two pypdf failure classes: (a) legacy scans whose OCR text layer
    # has no space glyphs ("ThermalAnalysisofEffect...", 259 NBS docs wrongly pruned 07-09), and
    # (b) CID-keyed CJK fonts where pypdf extracts NOTHING (413 Japanese NILIM PDFs, 07-10).
    if shutil.which("pdftotext") and (len(txt) < 500 or _word_glued(txt)):
        alt = _pdftotext(data)
        if len(alt) > max(len(txt), 400) and not _word_glued(alt):
            return alt
    return txt


def _word_glued(t: str) -> bool:
    head = t[:20_000]
    if not head:
        return False
    if len(quality.CJK.findall(head)) / len(head) > 0.10:
        return False  # CJK scripts don't space-separate — that's not gluing
    return head.count(" ") / len(head) < 0.05  # spaced prose runs ~15-18%


def _pdftotext(data: bytes) -> str:
    import tempfile

    with tempfile.NamedTemporaryFile(suffix=".pdf") as f:
        f.write(data)
        f.flush()
        out = subprocess.run(["pdftotext", f.name, "-"], capture_output=True, timeout=300)
    return out.stdout.decode("utf-8", "ignore").strip() if out.returncode == 0 else ""


def extract_html(data: bytes) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(data, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    # main content: MediaWiki first, then common doc-site (sphinx/readthedocs/mkdocs) containers, else body
    main = (soup.select_one("div.mw-parser-output") or soup.select_one("[role=main]")
            or soup.select_one("main") or soup.select_one("article")
            or soup.select_one("div.body") or soup.select_one("div.document")
            or soup.select_one(".md-content") or soup.select_one(".rst-content")
            or soup.body or soup)
    drop = (".reference", ".mw-editsection", "table.navbox", ".navbox",
            ".vertical-navbox", ".reflist", "#toc", ".toc",
            ".navigation-not-searchable", ".hatnote", ".ambox", "table.ambox",
            ".mbox-small", ".metadata", ".sistersitebox", ".shortdescription",
            ".noprint", ".mw-empty-elt", ".mw-jump-link", "#References",
            "#External_links", "#Further_reading", "#See_also",
            # doc-site chrome (sphinx / readthedocs / mkdocs):
            "nav", "header", "footer", ".sphinxsidebar", ".wy-nav-side",
            ".toctree-wrapper", ".headerlink", ".md-sidebar", ".md-header",
            ".md-footer", ".rst-footer-buttons", ".related", "#searchbox",
            ".breadcrumbs", ".wy-breadcrumbs", "[role=navigation]")
    for sel in drop:
        for t in main.select(sel):
            t.decompose()
    text = main.get_text("\n")
    out, blanks = [], 0
    for ln in (l.strip() for l in text.splitlines()):
        if ln:
            out.append(ln)
            blanks = 0
        else:
            blanks += 1
            if blanks <= 1:
                out.append("")
    return "\n".join(out).strip()


def load_manifest() -> dict:
    return {r["id"]: r for r in registry.load_manifest_rows()}


def write_manifest(rows: dict) -> None:
    registry.write_manifest_rows(rows.values())


def _fetch_ec_deliverable(url: str) -> requests.Response:
    """EC 'Documents download module' (Horizon project deliverables, eud- ids): the stable public
    URL returns a JS interstitial whose window.location points at a session-bound tokenized URL —
    follow it with the same cookie jar to get the actual PDF."""
    with requests.Session() as s:
        s.headers.update({"User-Agent": UA})
        first = s.get(url, timeout=TIMEOUT, allow_redirects=True)
        if not first.headers.get("Content-Type", "").startswith("text/html"):
            return first
        m = re.search(r"window\.location='(https://ec\.europa\.eu[^']+)'", first.text)
        if not m:
            return first
        return s.get(m.group(1), timeout=TIMEOUT, allow_redirects=True)


def fetch_one(src: dict) -> dict:
    sid = src["id"]
    fmt = src.get("format", "pdf")
    source = src.get("source", "misc")
    ext = {"pdf": "pdf", "html": "html"}.get(fmt, fmt if fmt in TEXT_FORMATS else "bin")
    rec = {
        "id": sid, "title": src.get("title", sid), "url": src["url"],
        "source": source, "license": src.get("license", "unknown"),
        "topic": src.get("topic", "misc"), "format": fmt,
        "status": "failed", "http_status": None, "sha256": None, "bytes": 0,
        "raw_path": None, "text_path": None, "text_chars": 0,
        "error": None, "fetched_at": None,
    }
    ua = HOST_UA.get(urlparse(src["url"]).netloc.lower(), UA)
    try:
        if "ec.europa.eu/research/participants/documents/downloadPublic" in src["url"]:
            resp = _fetch_ec_deliverable(src["url"])
        else:
            resp = requests.get(src["url"],
                                headers={"User-Agent": ua,
                                         "Accept": "application/pdf,text/html;q=0.9,*/*;q=0.8"},
                                timeout=TIMEOUT, allow_redirects=True)
        rec["http_status"] = resp.status_code
        if resp.status_code in (403, 410, 429, 503):
            # WAFs (Akamai/Cloudflare/Google) block the python client's TLS fingerprint but pass
            # curl's (google patents 503s requests yet 200s curl with the SAME UA). Fall back to
            # curl for openly-licensed docs we're entitled to fetch.
            out = subprocess.run(["curl", "-sSL", "--max-time", str(TIMEOUT), "-A", ua, src["url"]],
                                 capture_output=True, timeout=TIMEOUT + 15)
            body = out.stdout if out.returncode == 0 else b""
            # only trust the fallback if it returned the expected content — not a WAF challenge /
            # "automated queries" block page that curl happily 200s
            good = len(body) > 512 and (
                body[:5] == b"%PDF-" if fmt == "pdf"
                else (b"automated queries" not in body[:4000]
                      and b"unusual traffic" not in body[:4000]
                      and b"Too many requests" not in body[:4000]
                      and b"too many requests" not in body[:4000]))
            if good:
                data, rec["http_status"] = body, 200
            else:
                resp.raise_for_status()
                data = resp.content
        else:
            resp.raise_for_status()
            data = resp.content
        if fmt == "pdf" and not data.startswith(b"%PDF-"):
            # a 200 that isn't a PDF is a WAF interstitial / captcha / error page — without this
            # check it lands in the corpus as an ok row with 0 text chars (IBPSA sgcaptcha, 07-09)
            rec["error"] = f"not-a-pdf (got {data[:12]!r})"
            return rec
        rec["sha256"] = sha256_bytes(data)
        rec["bytes"] = len(data)

        raw_dir = RAW / source
        raw_dir.mkdir(parents=True, exist_ok=True)
        raw_path = raw_dir / f"{sid}.{ext}"
        raw_path.write_bytes(data)
        rec["raw_path"] = str(raw_path.relative_to(HERE))

        try:
            txt = clean_text(extract_for(fmt, data))
        except Exception as e:
            txt, rec["error"] = "", f"text-extract: {e}"
        if txt:
            TEXT.mkdir(parents=True, exist_ok=True)
            header = (f"# {rec['title']}\n\n"
                      f"source: {rec['url']}\nlicense: {rec['license']}\n"
                      f"topic: {rec['topic']}\n\n---\n\n")
            tp = TEXT / f"{sid}.md"
            tp.write_text(header + txt)
            rec["text_path"] = str(tp.relative_to(HERE))
            rec["text_chars"] = len(txt)
            rec["quality"] = quality.metrics(txt)  # prune verdicts read this, not the file

        rec["status"] = "ok"
        rec["fetched_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    except Exception as e:
        rec["error"] = str(e)
    return rec


def fetch_polite(src: dict) -> dict:
    with _host_sem(src["url"]):
        host = urlparse(src["url"]).netloc.lower()
        delay = HOST_DELAY.get(host)
        if delay:
            with _host_next_lock:
                start = max(time.monotonic(), _host_next.get(host, 0.0))
                _host_next[host] = start + delay
            if (wait := start - time.monotonic()) > 0:
                time.sleep(wait)
        return fetch_one(src)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="re-fetch everything")
    ap.add_argument("--workers", type=int, default=8,
                    help="parallel downloads (per-host capped at %d); 1 = sequential" % PER_HOST)
    ap.add_argument("--only", default="", help="comma-separated topics to limit to")
    ap.add_argument("--reextract", action="store_true",
                    help="re-extract text from existing raw files; no download")
    ap.add_argument("--verify", action="store_true",
                    help="re-hash local raw files against the manifest sha256; no download")
    args = ap.parse_args()
    only = {t.strip() for t in args.only.split(",") if t.strip()}

    srcs = registry.load_entries()
    manifest = load_manifest()

    if args.reextract:
        TEXT.mkdir(parents=True, exist_ok=True)
        done = 0
        for r in sorted(manifest.values(), key=lambda x: x["id"]):
            rp = r.get("raw_path")
            if not rp or not (HERE / rp).exists():
                continue
            data = (HERE / rp).read_bytes()
            fmt = r.get("format", "pdf")
            try:
                txt = clean_text(extract_for(fmt, data))
            except Exception as e:
                txt, r["error"] = "", f"reextract: {e}"
            if txt:
                header = (f"# {r['title']}\n\nsource: {r['url']}\n"
                          f"license: {r['license']}\ntopic: {r['topic']}\n\n---\n\n")
                (TEXT / f"{r['id']}.md").write_text(header + txt)
                r["text_path"] = f"text/{r['id']}.md"
                r["text_chars"] = len(txt)
                r["quality"] = quality.metrics(txt)
            done += 1
        write_manifest(manifest)
        tot = sum(r["text_chars"] for r in manifest.values() if r["status"] == "ok")
        print(f"re-extracted {done} docs | total text {tot / 1e6:.2f} M chars")
        return

    if args.verify:
        # reproducibility check: re-hash local raw files against the committed manifest sha256.
        match = miss = mismatch = 0
        for r in manifest.values():
            if r.get("status") != "ok" or not r.get("sha256"):
                continue
            rp = r.get("raw_path")
            if not rp or not (HERE / rp).exists():
                miss += 1
                continue
            if sha256_bytes((HERE / rp).read_bytes()) == r["sha256"]:
                match += 1
            else:
                mismatch += 1
                print(f"  MISMATCH {r['id']}")
        n_ok = sum(1 for r in manifest.values() if r.get("status") == "ok")
        print(f"verify: {match} match | {mismatch} sha256 MISMATCH | {miss} not downloaded "
              f"(of {n_ok} ok docs in manifest)")
        return

    todo = []
    for s in srcs:
        if only and s.get("topic") not in only:
            continue
        cur = manifest.get(s["id"])
        if cur and cur.get("status") == "ok" and not args.force:
            if cur.get("raw_path") and (HERE / cur["raw_path"]).exists():
                continue
        todo.append(s)

    # the committed manifest's sha256 = what WE fetched; compare to detect upstream drift.
    expected = {sid: r.get("sha256") for sid, r in manifest.items() if r.get("sha256")}
    repro = drift = new = done = 0
    print(f"sources: {len(srcs)} total, {len(todo)} to fetch "
          f"({'forced' if args.force else 'missing only'}, {args.workers} workers)")
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = {pool.submit(fetch_polite, s): s for s in todo}
        for fut in as_completed(futures):
            rec = fut.result()
            done += 1
            manifest[rec["id"]] = rec
            if rec["status"] == "ok":
                exp = expected.get(rec["id"])
                tag = "reproduced" if exp == rec["sha256"] else ("DRIFTED" if exp else "new")
                repro += exp == rec["sha256"]
                drift += bool(exp) and exp != rec["sha256"]
                new += not exp
                print(f"[{done}/{len(todo)}] {rec['id']}  ok  {rec['bytes'] // 1024}KB  "
                      f"{rec['text_chars']} chars  [{tag}]", flush=True)
            else:
                print(f"[{done}/{len(todo)}] {rec['id']}  FAIL http={rec['http_status']} "
                      f"{rec.get('error')}", flush=True)
            if done % 25 == 0:
                write_manifest(manifest)  # checkpoint so an interrupted run loses <25 fetches
    if todo:
        write_manifest(manifest)

    seen: dict = {}
    for r in manifest.values():
        if r.get("sha256"):
            seen.setdefault(r["sha256"], []).append(r["id"])
    dups = {h: ids for h, ids in seen.items() if len(ids) > 1}

    ok = sum(1 for r in manifest.values() if r["status"] == "ok")
    by_topic: dict = {}
    for r in manifest.values():
        if r["status"] == "ok":
            by_topic[r["topic"]] = by_topic.get(r["topic"], 0) + 1
    print(f"\nmanifest: {len(manifest)} rows | {ok} ok | {len(manifest) - ok} failed")
    print("ok by topic:", by_topic)
    if repro or drift or new:
        print(f"reproducibility vs manifest: {repro} reproduced (sha256 match) | "
              f"{drift} DRIFTED (source changed) | {new} new")
    if dups:
        print("duplicate bytes (same sha256):", dups)


if __name__ == "__main__":
    main()
